"""Tính đường cơ sở điện năng và chênh lệch chuẩn hóa theo ngày."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date
from math import fsum
from typing import Any

import numpy as np
import pandas as pd

from tools import finite_readings, reporting_timestamps, resolve_measurement_rows


BASELINE_DRIVERS = ("production_t", "cdd")


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
    complete = values.ge(0).all(axis=1)
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


def _fit_baseline(
    sample: pd.DataFrame, drivers: list[str]
) -> tuple[dict[str, Any], np.ndarray, np.ndarray, np.ndarray]:
    """Khớp OLS có hệ số chặn và tính chẩn đoán trên kỳ cơ sở."""
    n = len(sample)
    parameters = len(drivers) + 1
    dof = n - parameters
    if dof <= 0:
        raise ValueError(
            f"Cần ít nhất {parameters + 1} ngày hợp lệ trong kỳ cơ sở cho các biến đã chọn."
        )

    x = sample.loc[:, drivers].to_numpy()
    y = sample["actual_kwh"].to_numpy()
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

            slopes = fit[1:] / scales
            intercept = fit[0] - centers @ slopes
            residual = y - design @ fit
            mean_y = float(y.mean())
            sse = float(residual @ residual)
            centered_y = y - mean_y
            sst = float(centered_y @ centered_y)
            r2 = 1.0 - sse / sst if sst > 0 and np.ptp(y) > 0 else None
            adjusted_r2 = 1.0 - (1.0 - r2) * (n - 1) / dof if r2 is not None else None
            cvrmse = 100.0 * np.sqrt(sse / dof) / mean_y if mean_y > 0 else None
            nmbe = 100.0 * residual.sum() / dof / mean_y if mean_y > 0 else None
    except (FloatingPointError, np.linalg.LinAlgError) as exc:
        raise ValueError("Không thể khớp mô hình với thang đo và các biến hiện tại.") from exc

    diagnostics = {
        "method": "OLS",
        "drivers": drivers,
        "training_rows": n,
        "degrees_of_freedom": dof,
        "coefficients": {"intercept": float(intercept), **dict(zip(drivers, map(float, slopes)))},
        "driver_ranges": {
            driver: {"min": float(sample[driver].min()), "max": float(sample[driver].max())}
            for driver in drivers
        },
        "r2": r2,
        "adjusted_r2": adjusted_r2,
        "cvrmse_pct": float(cvrmse) if cvrmse is not None else None,
        "nmbe_pct": float(nmbe) if nmbe is not None else None,
        "condition_number": float(singular_values[0] / singular_values[-1]),
    }
    return diagnostics, fit, centers, scales


def _totals(paired: pd.DataFrame) -> dict[str, float | None]:
    """Tổng hợp thực đo và tham chiếu trên cùng các ngày hợp lệ."""
    if paired.empty:
        return dict.fromkeys(("actual_kwh", "expected_kwh", "savings_kwh", "savings_pct"))

    try:
        actual = fsum(paired["actual_kwh"])
        expected = fsum(paired["expected_kwh"])
        savings = expected - actual
        percentage = 100.0 * (savings / expected) if expected > 0 else None
        if percentage is not None and not np.isfinite(percentage):
            raise OverflowError
    except OverflowError as exc:
        raise ValueError("Kết quả điện năng vượt phạm vi số. Hãy kiểm tra đơn vị đầu vào.") from exc
    return {
        "actual_kwh": actual,
        "expected_kwh": expected,
        "savings_kwh": savings,
        "savings_pct": percentage,
    }


def normalized_baseline(
    df: pd.DataFrame,
    kwh_cols: Sequence[str],
    *,
    baseline_start: str,
    baseline_end: str,
    reporting_start: str,
    reporting_end: str,
    drivers: Sequence[str],
) -> dict[str, Any]:
    """So sánh điện đo theo ngày với đường cơ sở chuẩn hóa theo điều kiện kỳ báo cáo."""
    baseline_period = _period_dates(baseline_start, baseline_end)
    reporting_period = _period_dates(reporting_start, reporting_end)
    if baseline_period[1] >= reporting_period[0]:
        raise ValueError("Kỳ cơ sở phải kết thúc trước ngày bắt đầu kỳ báo cáo.")

    meter_cols = list(kwh_cols)
    driver_cols = list(drivers)
    if not meter_cols or len(set(meter_cols)) != len(meter_cols):
        raise ValueError("Chọn ít nhất một cột điện năng, mỗi cột xuất hiện một lần.")
    if not driver_cols or len(set(driver_cols)) != len(driver_cols) or not set(driver_cols).issubset(BASELINE_DRIVERS):
        raise ValueError("Chọn sản lượng, CDD hoặc cả hai làm biến của đường cơ sở.")
    if df.columns.has_duplicates:
        raise ValueError("Tên cột phải duy nhất để xác định số đo của từng hạng mục.")
    missing_columns = {"timestamp", *meter_cols, *driver_cols}.difference(df.columns)
    if missing_columns:
        raise ValueError(f"Thiếu cột cho đường cơ sở: {', '.join(sorted(missing_columns))}.")

    source = df.assign(timestamp=reporting_timestamps(df["timestamp"]))
    recorded = source["timestamp"].dropna()
    if recorded.empty:
        raise ValueError("Cần mốc thời gian có số đo để chọn kỳ phân tích.")
    if baseline_period[0] < recorded.min().date() or reporting_period[1] > recorded.max().date():
        raise ValueError("Hai kỳ phải nằm trong phạm vi ngày của dữ liệu nguồn.")
    baseline_sample, baseline_coverage = _daily_samples(
        source, baseline_period, meter_cols, driver_cols
    )
    reporting_sample, reporting_coverage = _daily_samples(
        source, reporting_period, meter_cols, driver_cols
    )
    model, fit, centers, scales = _fit_baseline(baseline_sample, driver_cols)

    reporting_drivers = reporting_sample.loc[:, driver_cols]
    within_range = reporting_drivers.ge(baseline_sample[driver_cols].min()).all(axis=1)
    within_range &= reporting_drivers.le(baseline_sample[driver_cols].max()).all(axis=1)
    eligible = reporting_sample.loc[within_range].copy()
    with np.errstate(over="ignore", invalid="ignore"):
        expected = fit[0] + ((eligible[driver_cols].to_numpy() - centers) / scales) @ fit[1:]
    finite = np.isfinite(expected)
    # Triệt sai số dấu quanh 0 do phép khớp và phép nhân ma trận.
    rounding_tol = 32 * np.finfo(float).eps * baseline_sample["actual_kwh"].max()
    negative = finite & (expected < -rounding_tol)
    valid = finite & ~negative
    paired = eligible.loc[valid].copy()
    paired["expected_kwh"] = np.maximum(expected[valid], 0.0)
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
        monthly.append({"month": str(month), "eligible_rows": len(measured), **_totals(measured)})
    summary = _totals(paired)

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

    return {
        "meter_columns": meter_cols,
        "invalid_timestamp_rows": invalid_timestamp_rows,
        "baseline": baseline_coverage,
        "reporting": reporting_coverage,
        "model": model,
        "summary": summary,
        "monthly": monthly,
        "warnings": warnings,
        "assumptions": [
            "Mỗi dòng đủ điều kiện là một ngày đo và ranh giới đo giống nhau ở hai kỳ.",
            "Điện tham chiếu lấy từ mô hình kỳ cơ sở với biến liên quan đo tại kỳ báo cáo.",
            "Kỳ cơ sở cần đại diện cho các chế độ vận hành và mùa liên quan.",
            "Kết quả phản ánh chênh lệch theo mô hình. Cần đánh giá thay đổi vận hành và "
            "ranh giới đo để xác nhận tiết kiệm của một giải pháp.",
        ],
    }
