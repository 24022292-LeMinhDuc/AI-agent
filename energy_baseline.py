"""Tính đường cơ sở điện năng và chênh lệch chuẩn hóa theo ngày."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from itertools import combinations
from math import fsum, isfinite, sqrt
from typing import Any

import numpy as np
import pandas as pd
from scipy.stats import t

from tools import finite_readings, reporting_timestamps, resolve_measurement_rows


BASELINE_DRIVER_LABELS = {
    "production_t": "Sản lượng (tấn)",
    "cdd": "CDD",
    "hdd": "HDD",
    "operating_hours": "Giờ vận hành",
    "outdoor_humidity_pct": "Độ ẩm ngoài trời (%)",
    "floor_area_m2": "Diện tích sàn (m²)",
    "occupancy_people": "Số người",
    "occupancy_pct": "Tỷ lệ lấp đầy (%)",
}
BASELINE_DRIVERS = tuple(BASELINE_DRIVER_LABELS)
BASELINE_MODEL_TYPES = ("ols", "3pc", "3ph", "4p", "5p", "auto")
DEFAULT_HDD_BASE_TEMP_C = 18.0
MV_DEFAULTS = {
    "daily": {"max_cv_rmse_pct": 25.0, "max_abs_nmbe_pct": 10.0},
    "monthly": {"max_cv_rmse_pct": 15.0, "max_abs_nmbe_pct": 5.0},
    "max_p_value": 0.05,
    "max_vif": 5.0,
    "confidence_level": 0.90,
}
_TEMPERATURE_DRIVERS = ("cdd", "hdd")


@dataclass
class _Fit:
    features: list[str]
    centers: np.ndarray
    scales: np.ndarray
    design: np.ndarray
    beta: np.ndarray
    residual: np.ndarray
    condition_number: float

    @property
    def sse(self) -> float:
        return float(self.residual @ self.residual)

    def predict(self, features: pd.DataFrame) -> np.ndarray:
        x = features[self.features].to_numpy()
        return self.beta[0] + ((x - self.centers) / self.scales) @ self.beta[1:]


def _period_dates(start: str, end: str) -> tuple[date, date]:
    """Kiểm tra giới hạn đầu và cuối kỳ theo ngày ISO."""
    try:
        first = date.fromisoformat(start)
        last = date.fromisoformat(end)
    except (TypeError, ValueError) as exc:
        raise ValueError("Ngày bắt đầu và kết thúc phải có dạng YYYY-MM-DD hợp lệ.") from exc
    if first.isoformat() != start or last.isoformat() != end:
        raise ValueError("Ngày bắt đầu và kết thúc phải có dạng YYYY-MM-DD.")
    if first > last:
        raise ValueError("Ngày kết thúc kỳ phải từ ngày bắt đầu trở đi.")
    return first, last


def _daily_samples(
    source: pd.DataFrame,
    period: tuple[date, date],
    meters: list[str],
    drivers: list[str],
) -> tuple[pd.DataFrame, dict[str, Any]]:
    """Chọn các ngày đo có đầy đủ điện năng và biến liên quan của một kỳ."""
    first, last = period
    days = source["timestamp"].dt.normalize()
    sample = source.loc[days.between(pd.Timestamp(first), pd.Timestamp(last))].copy()
    unique_times = sample["timestamp"].drop_duplicates()
    if unique_times.dt.normalize().duplicated().any():
        raise ValueError(
            "Mô hình cần dữ liệu theo ngày. Có ngày chứa nhiều mốc đo riêng biệt."
        )

    sample.attrs.pop("measurement_interval_quality", None)
    observations, interval_quality = resolve_measurement_rows(sample)
    values = observations.loc[:, [*meters, *drivers]].apply(finite_readings)
    complete = values.drop(columns="temp_c", errors="ignore").ge(0).all(axis=1)
    if "temp_c" in drivers:
        complete &= values["temp_c"].between(-100, 70)
    for driver, upper_bound in (("operating_hours", 24), ("outdoor_humidity_pct", 100), ("occupancy_pct", 100)):
        if driver in drivers:
            complete &= values[driver].le(upper_bound)
    if "floor_area_m2" in drivers:
        complete &= values["floor_area_m2"].gt(0)
    totals = values.loc[:, meters].sum(axis=1, min_count=len(meters))
    complete &= np.isfinite(totals)
    matched = values.loc[complete, drivers].copy()
    matched["actual_kwh"] = totals.loc[complete].to_numpy()
    matched.index = pd.DatetimeIndex(
        observations.loc[complete, "timestamp"].dt.normalize(), name="day"
    )
    matched = matched.sort_index()

    expected = (last - first).days + 1
    observed = len(unique_times)
    eligible = len(matched)
    return matched, {
        "start_date": first.isoformat(),
        "end_date": last.isoformat(),
        "expected_rows": expected,
        "observed_rows": observed,
        "eligible_rows": eligible,
        "missing_rows": expected - observed,
        "incomplete_rows": observed - eligible,
        "coverage_pct": 100.0 * eligible / expected,
        **interval_quality,
    }


def _fit_linear(
    features: list[str], x: np.ndarray, y: np.ndarray, *, breakpoints: int = 0,
) -> _Fit:
    """Khớp các hệ số tuyến tính có hệ số chặn tại điểm gãy đã chọn."""
    n = len(y)
    parameters = len(features) + 1
    if n <= parameters + breakpoints:
        raise ValueError(
            f"Cần ít nhất {parameters + breakpoints + 1} ngày hợp lệ trong kỳ cơ sở cho các biến đã chọn."
        )
    try:
        with np.errstate(over="raise", invalid="raise", divide="raise"):
            centers = x.mean(axis=0)
            scales = x.std(axis=0)
            if np.any(scales == 0):
                raise ValueError("Kỳ cơ sở có biến giữ nguyên. Chọn biến có độ biến thiên.")
            design = np.column_stack((np.ones(n), (x - centers) / scales))
            fit, _, rank, singular_values = np.linalg.lstsq(design, y, rcond=None)
            if rank != parameters:
                raise ValueError("Các biến kỳ cơ sở phụ thuộc tuyến tính. Hãy đổi bộ biến.")

            residual = y - design @ fit
            if not np.isfinite(residual @ residual):
                raise ValueError("Sai số mô hình vượt phạm vi tính toán.")
            condition_number = float(singular_values[0] / singular_values[-1])
    except (FloatingPointError, np.linalg.LinAlgError) as exc:
        raise ValueError("Không thể khớp mô hình với thang đo và các biến hiện tại.") from exc

    return _Fit(features, centers, scales, design, fit, residual, condition_number)


def _features(
    sample: pd.DataFrame, drivers: list[str], balance: Mapping[str, float],
) -> pd.DataFrame:
    features = sample[drivers].copy()
    if "heating" in balance:
        features["heating_degree_c"] = (balance["heating"] - sample["temp_c"]).clip(lower=0)
    if "cooling" in balance:
        features["cooling_degree_c"] = (sample["temp_c"] - balance["cooling"]).clip(lower=0)
    return features


def _physical_slopes(fit: _Fit) -> bool:
    thermal = {*_TEMPERATURE_DRIVERS, "heating_degree_c", "cooling_degree_c"}
    return all(value >= 0 for name, value in zip(fit.features, fit.beta[1:]) if name in thermal)


def _change_point_fit(
    sample: pd.DataFrame, drivers: list[str], model_type: str,
) -> tuple[_Fit, dict[str, float]]:
    temperature = sample["temp_c"].to_numpy()
    if not len(temperature):
        raise ValueError("Kỳ cơ sở chưa có ngày đủ số đo nhiệt độ cho Change-Point.")
    lower, upper = np.quantile(temperature, [0.1, 0.9])
    first = int(np.ceil(lower * 2))
    last = int(np.floor(upper * 2))
    grid = [half_degree / 2 for half_degree in range(first, last + 1)]
    observed = np.unique(temperature)
    if len(observed) < 3:
        raise ValueError("Kỳ cơ sở cần nhiều mức nhiệt độ khác nhau để tính Change-Point.")
    # Mỗi nhánh hoạt động cần ít nhất hai mức nhiệt đo khác nhau.
    heating = {point: np.maximum(point - temperature, 0) for point in grid if point > observed[1]}
    cooling = {point: np.maximum(temperature - point, 0) for point in grid if point < observed[-2]}
    hinges = {"heating": heating, "cooling": cooling}
    if model_type == "5p":
        balances = (
            {"heating": h, "cooling": c}
            for h, c in combinations(grid, 2)
            if h in heating and c in cooling and observed.searchsorted(h) < observed.searchsorted(c, side="right")
        )
    else:
        modes = {"3pc": ["cooling"], "3ph": ["heating"], "4p": ["heating", "cooling"]}[model_type]
        balances = (
            {mode: point for mode in modes} for point in grid
            if all(point in hinges[mode] for mode in modes)
        )
    best = None
    best_sse = float("inf")
    y = sample["actual_kwh"].to_numpy()
    other_values = sample[drivers].to_numpy()
    for balance in balances:
        features = [*drivers, *(f"{mode}_degree_c" for mode in balance)]
        x = np.column_stack([other_values, *(hinges[mode][point] for mode, point in balance.items())])
        try:
            fit = _fit_linear(features, x, y, breakpoints=len(set(balance.values())))
        except ValueError:
            continue
        sse = fit.sse
        if sse < best_sse and _physical_slopes(fit):
            best, best_sse = (fit, balance), sse
    if best is None:
        raise ValueError(f"Không đủ dữ liệu hoặc không tìm được mô hình {model_type.upper()} phù hợp vật lý.")
    return best


def _fit_metrics(y: np.ndarray, residual: np.ndarray, dof: int) -> tuple[float | None, float | None]:
    mean = float(y.mean()) if len(y) else 0.0
    if dof <= 0 or mean <= 0:
        return None, None
    rmse = np.linalg.norm(residual) / sqrt(dof)
    return float(100 * (rmse / mean)), float(100 * (residual.mean() / mean) * (len(y) / dof))


def _model_diagnostics(
    sample: pd.DataFrame, fit: _Fit, drivers: list[str], model_type: str, balance: dict[str, float],
    hdd_base: float,
) -> dict[str, Any]:
    n = len(sample)
    dof = n - len(fit.beta) - len(set(balance.values()))
    y = sample["actual_kwh"].to_numpy()
    centered = y - y.mean()
    sst = float(centered @ centered)
    r2 = 1.0 - fit.sse / sst if sst > 0 and np.ptp(y) > 0 else None
    cvrmse, nmbe = _fit_metrics(y, fit.residual, dof)
    slopes = fit.beta[1:] / fit.scales
    intercept = fit.beta[0] - fit.centers @ slopes
    range_columns = [*drivers, "temp_c"] if balance else drivers
    model = {
        "method": model_type.upper(),
        "model_type": model_type,
        "drivers": range_columns,
        "training_rows": n,
        "degrees_of_freedom": dof,
        "coefficients": {"intercept": float(intercept), **dict(zip(fit.features, map(float, slopes)))},
        "driver_ranges": {
            driver: {"min": float(sample[driver].min()), "max": float(sample[driver].max())}
            for driver in range_columns
        },
        "r2": r2,
        "adjusted_r2": 1.0 - (1.0 - r2) * (n - 1) / dof if r2 is not None else None,
        "cvrmse_pct": cvrmse,
        "nmbe_pct": nmbe,
        "condition_number": fit.condition_number,
        "balance_temp_c": balance if model_type == "5p" else next(iter(balance.values()), None),
        "hdd_base_temp_c": hdd_base if "hdd" in drivers else None,
    }
    return model


def _coefficient_tests(fit: _Fit, coefficients: dict[str, float], max_p: float) -> list[dict[str, Any]]:
    n, linear_parameters = fit.design.shape
    dof = n - linear_parameters
    inverse = np.linalg.pinv(fit.design)
    # Chuyển hiệp phương sai từ hệ tọa độ chuẩn hóa về đơn vị của biến gốc.
    unscaled_slopes = inverse[1:] / fit.scales[:, None]
    intercept = inverse[0] - fit.centers @ unscaled_slopes
    standard_errors = sqrt(fit.sse / dof) * np.linalg.norm(
        np.vstack((intercept, unscaled_slopes)), axis=1
    )
    vifs = [None, *(max(1.0, float(value)) for value in n * np.sum(inverse[1:] ** 2, axis=1))]
    rows = []
    for (name, coef), error, vif in zip(coefficients.items(), standard_errors, vifs):
        statistic = float(coef / error) if error > 0 else None
        if statistic is None or not isfinite(statistic):
            p_value = 0.0 if coef != 0 else None
            statistic = None
        else:
            p_value = float(2 * t.sf(abs(statistic), dof))
        rows.append({
            "feature": name, "coef": coef, "std_err": float(error), "t_stat": statistic,
            "p_value": p_value, "vif": vif, "significant": p_value is not None and p_value <= max_p,
        })
    return rows


def _standard_limits(overrides: Mapping[str, Any] | None, granularity: str) -> dict[str, float]:
    if granularity not in ("daily", "monthly"):
        raise ValueError("Độ phân giải kiểm định phải là daily hoặc monthly.")
    overrides = overrides or {}
    defaults = {
        **MV_DEFAULTS[granularity],
        **{name: MV_DEFAULTS[name] for name in ("max_p_value", "max_vif", "confidence_level")},
    }
    provided = {**overrides, **overrides.get(granularity, {})}
    limits = {name: float(provided.get(name, default)) for name, default in defaults.items()}
    if (
        any(not isfinite(value) or value <= 0 for value in limits.values())
        or limits["max_p_value"] > 1 or limits["confidence_level"] >= 1
    ):
        raise ValueError("Ngưỡng M&V phải hữu hạn và nằm trong phạm vi hợp lệ.")
    return limits


def _ashrae_check(
    sample: pd.DataFrame, fit: _Fit, model: dict[str, Any], coefficients: list[dict[str, Any]],
    granularity: str, limits: dict[str, float],
) -> dict[str, Any]:
    y = sample["actual_kwh"].to_numpy()
    residual = fit.residual
    parameters = len(y) - model["degrees_of_freedom"]
    if granularity == "monthly":
        months = sample.index.to_period("M")
        groups = pd.DataFrame({"actual": y, "residual": residual}, index=months).groupby(level=0)
        complete = groups.size()
        complete = complete.eq(complete.index.days_in_month)
        y, residual = groups.sum().loc[complete].to_numpy().T
    cvrmse, nmbe = _fit_metrics(y, residual, len(y) - parameters)
    cv_pass = cvrmse is not None and cvrmse <= limits["max_cv_rmse_pct"]
    nmbe_pass = nmbe is not None and abs(nmbe) <= limits["max_abs_nmbe_pct"]
    drivers = [row for row in coefficients if row["feature"] != "intercept"]
    return {
        "passed": cv_pass and nmbe_pass, "granularity": granularity,
        "cv_rmse_pass": cv_pass, "nmbe_pass": nmbe_pass,
        "vif_warnings": [row["feature"] for row in drivers if row["vif"] > limits["max_vif"]],
        "insignificant_drivers": [row["feature"] for row in drivers if not row["significant"]],
        "observations": len(y), "cvrmse_pct": cvrmse, "nmbe_pct": nmbe,
        "max_cv_rmse_pct": limits["max_cv_rmse_pct"], "max_abs_nmbe_pct": limits["max_abs_nmbe_pct"],
        "max_vif": limits["max_vif"], "max_p_value": limits["max_p_value"],
    }


def _uncertainty(
    sample: pd.DataFrame, fit: _Fit, summary: dict[str, float | None], reporting_rows: int,
    confidence: float,
) -> dict[str, Any]:
    """Ước lượng bất định mô hình theo ASHRAE 14-2023 Phụ lục B, B-27 và B-30–B-33."""
    adjacent = np.diff(sample.index.values) == np.timedelta64(1, "D")
    before, after = fit.residual[:-1][adjacent], fit.residual[1:][adjacent]
    mean_y = float(sample["actual_kwh"].mean())
    rho = None
    if len(before) >= 3 and np.ptp(before) > 0 and np.ptp(after) > 0:
        if np.max(np.abs(fit.residual)) > 32 * np.finfo(float).eps * mean_y:
            rho = float(np.clip(np.corrcoef(before, after)[0, 1], -1.0, 1.0))
    # Không giảm độ bất định khi mẫu cho thấy tự tương quan âm.
    positive_rho = max(0.0, rho or 0.0)
    n_effective = len(sample) * (1 - positive_rho) / (1 + positive_rho)
    # Theo Phụ lục B, giữ điểm gãy cố định khi tính độ bất định.
    dof = n_effective - len(fit.beta)
    savings = summary["savings_kwh"]
    expected = summary["expected_kwh"]
    actual = summary["actual_kwh"]
    result = {
        "rho": rho, "n_effective": n_effective, "degrees_of_freedom": dof,
        "reporting_observations": reporting_rows, "confidence_level": confidence,
        "fsu_90_pct": None, "fsu_95_pct": None, "confidence_interval_kwh": None,
    }
    if dof <= 0 or expected is None or expected <= 0 or savings is None or mean_y <= 0:
        return result
    mse = fit.sse / dof
    factor = 1.26 * ((expected / reporting_rows) / mean_y) * sqrt(
        mse * (1 + 2 / n_effective) * reporting_rows
    )
    for level, key in ((0.90, "fsu_90_pct"), (0.95, "fsu_95_pct"), (confidence, "confidence_interval_kwh")):
        uncertainty = float(t.ppf((1 + level) / 2, dof)) * factor
        if not isfinite(uncertainty):
            continue
        if key == "confidence_interval_kwh":
            lower, upper = savings - uncertainty, savings + uncertainty
            if isfinite(lower) and isfinite(upper):
                result[key] = {"lower": lower, "upper": upper}
        elif abs(savings) > 32 * np.finfo(float).eps * max(expected, actual):
            fsu = 100 * (uncertainty / abs(savings))
            result[key] = fsu if isfinite(fsu) else None
    return result


def _apply_adjustments(paired: pd.DataFrame, adjustments: list[dict] | None) -> list[dict[str, Any]]:
    paired["nra_kwh"] = 0.0
    if adjustments is not None and not isinstance(adjustments, list):
        raise ValueError("NRA phải là một danh sách.")
    applied = []
    for adjustment in adjustments or []:
        if not isinstance(adjustment, dict) or set(adjustment) != {
            "start_date", "end_date", "delta_kwh_per_day", "reason",
        }:
            raise ValueError("Mỗi NRA cần có ngày đầu, ngày cuối, kWh/ngày có dấu và lý do.")
        first, last = _period_dates(adjustment["start_date"], adjustment["end_date"])
        reason, delta = adjustment["reason"], adjustment["delta_kwh_per_day"]
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError("Mỗi NRA cần có lý do.")
        if isinstance(delta, bool) or not isinstance(delta, (int, float)) or not isfinite(delta):
            raise ValueError("Điều chỉnh NRA phải là số kWh/ngày hữu hạn.")
        delta = float(delta)
        applies = (paired.index >= pd.Timestamp(first)) & (paired.index <= pd.Timestamp(last))
        paired.loc[applies, "nra_kwh"] += delta
        applied.append({**adjustment, "delta_kwh_per_day": delta, "reason": reason.strip(),
                        "applied_days": int(applies.sum())})
    if not np.isfinite(paired["nra_kwh"]).all():
        raise ValueError("Điều chỉnh NRA vượt phạm vi tính toán.")
    return applied


def _totals(paired: pd.DataFrame) -> dict[str, float | None]:
    """Tổng hợp thực đo và tham chiếu trên cùng các ngày hợp lệ."""
    if paired.empty:
        return dict.fromkeys(("actual_kwh", "expected_kwh", "savings_kwh", "savings_pct"))

    try:
        actual = fsum(paired["actual_kwh"])
        expected = fsum(paired["expected_kwh"])
        adjusted = fsum((expected, fsum(paired["nra_kwh"])))
        savings = fsum((expected, fsum(paired["nra_kwh"]), -actual))
        percentage = 100.0 * (savings / adjusted) if adjusted > 0 else None
        if percentage is not None and not isfinite(percentage):
            raise OverflowError
    except OverflowError as exc:
        raise ValueError("Kết quả điện năng vượt phạm vi số. Hãy kiểm tra đơn vị đầu vào.") from exc
    return {
        "actual_kwh": actual,
        "expected_kwh": expected,
        "savings_kwh": savings,
        "savings_pct": percentage,
    }


def _auto_fit(
    sample: pd.DataFrame, drivers: list[str], linear_drivers: list[str],
) -> tuple[_Fit, str, dict[str, float]]:
    candidates: list[tuple[_Fit, str, dict[str, float]]] = []
    y = sample["actual_kwh"].to_numpy()
    for model_type in BASELINE_MODEL_TYPES[:-1]:
        try:
            if model_type == "ols":
                fit, balance = _fit_linear(drivers, sample[drivers].to_numpy(), y), {}
            else:
                fit, balance = _change_point_fit(sample, linear_drivers, model_type)
        except ValueError:
            continue
        if _physical_slopes(fit):
            candidates.append((fit, model_type, balance))
    if not candidates:
        raise ValueError("Không tìm được mô hình EnB tự động phù hợp với kỳ cơ sở.")
    # Cùng y và cùng mẫu, CV(RMSE) xếp hạng theo sai số hiệu chỉnh bậc tự do.
    tolerance = 32 * np.finfo(float).eps * np.max(np.abs(y))

    def score(candidate: tuple[_Fit, str, dict[str, float]]) -> tuple[float, int]:
        fit, _, balance = candidate
        parameters = len(fit.beta) + len(set(balance.values()))
        rmse = sqrt(fit.sse / (len(y) - parameters))
        return (0.0 if rmse <= tolerance else rmse, parameters)

    return min(candidates, key=score)


def normalized_baseline(
    df: pd.DataFrame,
    kwh_cols: Sequence[str],
    *,
    baseline_start: str,
    baseline_end: str,
    reporting_start: str,
    reporting_end: str,
    drivers: Sequence[str],
    model_type: str = "ols",
    non_routine_adjustments: list[dict] | None = None,
    granularity: str = "daily",
    hdd_base_temp_c: float = DEFAULT_HDD_BASE_TEMP_C,
    mv_standards: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """So sánh điện đo theo ngày với đường cơ sở chuẩn hóa theo điều kiện kỳ báo cáo."""
    baseline_period = _period_dates(baseline_start, baseline_end)
    reporting_period = _period_dates(reporting_start, reporting_end)
    if baseline_period[1] >= reporting_period[0]:
        raise ValueError("Kỳ cơ sở phải kết thúc trước ngày bắt đầu kỳ báo cáo.")
    if model_type not in BASELINE_MODEL_TYPES:
        raise ValueError("Loại mô hình EnB không được hỗ trợ.")
    limits = _standard_limits(mv_standards, granularity)

    meter_cols = list(kwh_cols)
    driver_cols = list(drivers)
    if not meter_cols or len(set(meter_cols)) != len(meter_cols):
        raise ValueError("Chọn ít nhất một cột điện năng, mỗi cột xuất hiện một lần.")
    if (
        (model_type == "ols" and not driver_cols)
        or len(set(driver_cols)) != len(driver_cols)
        or not set(driver_cols).issubset(BASELINE_DRIVERS)
    ):
        raise ValueError("Chọn ít nhất một biến điều chỉnh EnB được hỗ trợ, mỗi biến xuất hiện một lần.")
    if df.columns.has_duplicates:
        raise ValueError("Tên cột phải duy nhất để xác định số đo của từng hạng mục.")
    linear_drivers = [driver for driver in driver_cols if driver not in _TEMPERATURE_DRIVERS]
    training_drivers = driver_cols
    if model_type not in ("ols", "auto"):
        training_drivers = [*linear_drivers, "temp_c"]
    elif model_type == "auto" and not driver_cols:
        training_drivers = driver_cols = ["temp_c"]

    needed = {"timestamp", *meter_cols, *training_drivers}
    if "hdd" in needed:
        needed = (needed - {"hdd"}) | {"temp_c"}
    missing_columns = needed.difference(df.columns)
    if missing_columns:
        raise ValueError(f"Thiếu cột cho đường cơ sở: {', '.join(sorted(missing_columns))}.")

    source = df.assign(timestamp=reporting_timestamps(df["timestamp"]))
    if "hdd" in training_drivers:
        if not isfinite(hdd_base_temp_c):
            raise ValueError("Nhiệt độ cơ sở HDD phải là số hữu hạn.")
        temperature = finite_readings(source["temp_c"])
        source["hdd"] = (hdd_base_temp_c - temperature.where(temperature.between(-100, 70))).clip(lower=0)
    recorded = source["timestamp"].dropna()
    if recorded.empty:
        raise ValueError("Cần mốc thời gian có số đo để chọn kỳ phân tích.")
    if baseline_period[0] < recorded.min().date() or reporting_period[1] > recorded.max().date():
        raise ValueError("Hai kỳ phải nằm trong phạm vi ngày của dữ liệu nguồn.")
    baseline_sample, baseline_coverage = _daily_samples(
        source, baseline_period, meter_cols, training_drivers
    )
    balance: dict[str, float] = {}
    selected_model = model_type
    if model_type == "ols":
        fit = _fit_linear(
            driver_cols, baseline_sample[driver_cols].to_numpy(), baseline_sample["actual_kwh"].to_numpy()
        )
    elif model_type == "auto":
        if "temp_c" in source:
            common_drivers = list(dict.fromkeys([*training_drivers, "temp_c"]))
            common_sample, common_coverage = _daily_samples(source, baseline_period, meter_cols, common_drivers)
            try:
                fit, selected_model, balance = _auto_fit(common_sample, driver_cols, linear_drivers)
            except ValueError:
                if training_drivers == common_drivers:
                    raise
            else:
                baseline_sample, baseline_coverage = common_sample, common_coverage
        if selected_model == "auto":
            fit = _fit_linear(
                driver_cols, baseline_sample[driver_cols].to_numpy(), baseline_sample["actual_kwh"].to_numpy()
            )
            if not _physical_slopes(fit):
                raise ValueError("Không tìm được mô hình EnB tự động có hệ số nhiệt phù hợp vật lý.")
            selected_model = "ols"
    else:
        fit, balance = _change_point_fit(baseline_sample, linear_drivers, model_type)

    fitted_drivers = linear_drivers if balance else driver_cols
    model = _model_diagnostics(baseline_sample, fit, fitted_drivers, selected_model, balance, hdd_base_temp_c)
    model["requested_model_type"] = model_type
    coefficients = _coefficient_tests(fit, model["coefficients"], limits["max_p_value"])
    ashrae_check = _ashrae_check(baseline_sample, fit, model, coefficients, granularity, limits)

    range_columns = model["drivers"]
    reporting_sample, reporting_coverage = _daily_samples(
        source, reporting_period, meter_cols, range_columns
    )

    reporting_drivers = reporting_sample[range_columns]
    within_range = reporting_drivers.ge(baseline_sample[range_columns].min()).all(axis=1)
    within_range &= reporting_drivers.le(baseline_sample[range_columns].max()).all(axis=1)
    eligible = reporting_sample.loc[within_range].copy()
    with np.errstate(over="ignore", invalid="ignore"):
        expected = fit.predict(_features(eligible, fitted_drivers, balance))
    finite = np.isfinite(expected)
    # Triệt sai số dấu quanh 0 do phép khớp và phép nhân ma trận.
    rounding_tol = 32 * np.finfo(float).eps * baseline_sample["actual_kwh"].max()
    negative = finite & (expected < -rounding_tol)
    valid = finite & ~negative
    paired = eligible.loc[valid].copy()
    paired["expected_kwh"] = np.maximum(expected[valid], 0.0)
    applied_nra = _apply_adjustments(paired, non_routine_adjustments)
    reporting_coverage.update(
        eligible_rows=len(paired),
        coverage_pct=100.0 * len(paired) / reporting_coverage["expected_rows"],
        out_of_range_rows=int((~within_range).sum()),
        negative_prediction_rows=int(negative.sum()),
        invalid_prediction_rows=int((~finite).sum()),
    )

    months = pd.period_range(*reporting_period, freq="M")
    groups = dict(tuple(paired.groupby(paired.index.to_period("M"))))
    empty = paired.iloc[:0]
    monthly = []
    for month in months:
        measured = groups.get(month, empty)
        monthly.append({
            "month": str(month), "eligible_rows": len(measured), **_totals(measured),
            "nra_total_kwh": fsum(measured["nra_kwh"]),
        })
    summary = _totals(paired)
    uncertainty = _uncertainty(baseline_sample, fit, summary, len(paired), limits["confidence_level"])
    fsu_90 = uncertainty["fsu_90_pct"]
    significant_savings = fsu_90 is not None and fsu_90 < 50 and summary["savings_kwh"] > 0
    daily_diff = paired["actual_kwh"] - paired["expected_kwh"] - paired["nra_kwh"]
    cusum = daily_diff.cumsum()
    if not np.isfinite(cusum).all():
        raise ValueError("CUSUM vượt phạm vi tính toán.")

    warnings = []
    if baseline_coverage["eligible_rows"] < baseline_coverage["expected_rows"]:
        warnings.append("Kỳ cơ sở thiếu ngày hoặc thiếu số đo hợp lệ; cần xem lại tính đại diện.")
    if reporting_coverage["eligible_rows"] < reporting_coverage["expected_rows"]:
        warnings.append("Kết quả kỳ báo cáo chỉ gồm các ngày đủ điều kiện trong bảng độ phủ.")
    if reporting_coverage["out_of_range_rows"]:
        warnings.append("Đã loại ngày có biến liên quan ngoài khoảng quan sát của kỳ cơ sở.")
    if reporting_coverage["negative_prediction_rows"] or reporting_coverage["invalid_prediction_rows"]:
        warnings.append("Đã loại ngày có điện tham chiếu âm hoặc vượt phạm vi tính toán.")
    if model["r2"] is None:
        warnings.append("Điện kỳ cơ sở không biến thiên, nên R² không xác định.")
    invalid_timestamp_rows = int(source["timestamp"].isna().sum())
    if invalid_timestamp_rows:
        warnings.append("Có bản ghi không đọc được thời gian và không thể xếp vào kỳ phân tích.")
    if any(not adjustment["applied_days"] for adjustment in applied_nra):
        warnings.append("Có NRA không giao với ngày đủ điều kiện của kỳ báo cáo.")
    if (paired["nra_kwh"] < -paired["expected_kwh"]).any():
        warnings.append("Có ngày điện tham chiếu sau NRA âm. Cần xác minh lại điều chỉnh.")

    return {
        "meter_columns": meter_cols,
        "invalid_timestamp_rows": invalid_timestamp_rows,
        "baseline": baseline_coverage,
        "reporting": reporting_coverage,
        "model": model,
        "coefficients": coefficients,
        "ashrae14_check": ashrae_check,
        "uncertainty": uncertainty,
        "statistically_significant_savings": significant_savings,
        "nra_total_kwh": fsum(paired["nra_kwh"]),
        "non_routine_adjustments": applied_nra,
        "cusum_series": [
            {"date": day.date().isoformat(), "daily_diff_kwh": float(diff), "cusum_kwh": float(total)}
            for day, diff, total in zip(paired.index, daily_diff, cusum)
        ],
        "summary": summary,
        "monthly": monthly,
        "warnings": warnings,
        "assumptions": [
            "Mỗi dòng đủ điều kiện là một ngày đo và ranh giới đo giống nhau ở hai kỳ.",
            "Điện tham chiếu lấy từ mô hình kỳ cơ sở với biến liên quan đo tại kỳ báo cáo.",
            "NRA có dấu, cộng dồn khi trùng ngày và áp dụng trên các ngày báo cáo đủ dữ liệu.",
            "FSU theo ngày phản ánh bất định mô hình. Điểm gãy được giữ cố định khi tính FSU. "
            "Sai số công tơ và bất định của NRA cần được đánh giá trong kế hoạch M&V.",
            "Kỳ cơ sở cần đại diện cho các chế độ vận hành và mùa liên quan.",
            "Kết quả phản ánh chênh lệch theo mô hình. Cần đánh giá thay đổi vận hành và "
            "ranh giới đo để xác nhận tiết kiệm của một giải pháp.",
        ],
    }
