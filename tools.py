"""Cung cấp các công cụ tính toán tất định cho dữ liệu năng lượng."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from math import isfinite

import numpy as np
import pandas as pd


REPORTING_TIMEZONE = "Asia/Ho_Chi_Minh"
TOTAL_METER = "kwh_total"
KW_PER_REFRIGERATION_TON = 3.51685284
_AUDIT_METRICS = {
    "energy_cost_vnd": ("energy_cost_vnd", "sum"),
    "peak_demand_kw": ("peak_demand_kw", "max"),
    "power_factor_mean": ("power_factor", "mean"),
    "scope1_tco2e": ("scope1_tco2e", "sum"),
    "scope2_tco2e": ("scope2_tco2e", "sum"),
    "total_emissions_tco2e": ("total_emissions_tco2e", "sum"),
    "steam_t": ("steam_t", "sum"),
    "operating_hours": ("operating_hours", "sum"),
}
_CONTEXT_COLUMNS = {"facility_type", "tariff_period", "operating_status", "fuel_type", "occupancy_unit"}
_MEASUREMENT_COLUMNS = {
    "production_t", "temp_c", "cdd", "floor_area_m2", "occupancy_value", "occupancy_people",
    "occupancy_pct", "outdoor_humidity_pct", "boiler_fuel_kg", "cooling_load_rth",
    "sec_eui_kwh_per_unit", "baseline_kwh_per_unit", "grid_emission_factor_kgco2e_per_kwh",
    *(source for source, _ in _AUDIT_METRICS.values()),
}


def reporting_timestamps(values: pd.Series) -> pd.Series:
    """Đọc ngày ISO hoặc ngày/tháng/năm theo lịch địa phương Việt Nam."""
    if isinstance(values.dtype, pd.DatetimeTZDtype):
        return values.dt.tz_convert(REPORTING_TIMEZONE).dt.tz_localize(None)
    if pd.api.types.is_datetime64_any_dtype(values):
        return values

    dates = values.astype("string").str.strip()
    dates = dates.str.replace(
        r"^(\d{1,2})/(\d{1,2})/(\d{4})(?=$|[ T])",
        lambda match: f"{match[3]}-{match[2]:0>2}-{match[1]:0>2}",
        regex=True,
    )
    parsed = pd.to_datetime(dates, format="ISO8601", errors="coerce", utc=True)
    with_offset = dates.str.contains(
        r"[Tt ]\d.+(?:[Zz]|[+-]\d{2}(?::?\d{2})?)$", regex=True, na=False
    )
    return parsed.dt.tz_localize(None).mask(
        with_offset, parsed.dt.tz_convert(REPORTING_TIMEZONE).dt.tz_localize(None)
    )


def finite_readings(values: pd.Series) -> pd.Series:
    """Chuẩn hóa số hữu hạn, chuyển số không đọc được thành ô thiếu."""
    numeric = pd.to_numeric(values, errors="coerce").astype(float)
    return numeric.where(np.isfinite(numeric))


def resolve_measurement_rows(df: pd.DataFrame) -> tuple[pd.DataFrame, dict[str, int]]:
    """Giữ một bản sao giống nhau và loại các dòng xung đột cùng mốc đo."""
    quality = dict.fromkeys(
        ("duplicate_rows_removed", "conflicting_timestamp_rows", "conflicting_timestamps"), 0
    )
    quality.update(df.attrs.get("measurement_interval_quality", {}))
    if "timestamp" not in df.columns:
        return df, quality

    timestamps = reporting_timestamps(df["timestamp"]).reset_index(drop=True)
    repeated = timestamps.notna() & timestamps.duplicated(keep=False)
    if not repeated.any():
        return df, quality

    positions = timestamps.index[repeated]
    measurement_cols = [
        column for column in df.columns
        if str(column).startswith("kwh_") or column in _MEASUREMENT_COLUMNS
    ]
    repeated_rows = df.iloc[positions]
    measurements = repeated_rows.loc[:, measurement_cols].apply(finite_readings)
    measurements = measurements.set_axis(positions)
    for column in _CONTEXT_COLUMNS.intersection(df.columns):
        measurements[column] = repeated_rows[column].astype("string").str.strip().to_numpy()
    measurements["timestamp"] = timestamps.loc[positions]
    redundant = measurements.duplicated()
    distinct_timestamps = measurements.loc[~redundant, "timestamp"]
    conflicted_dates = distinct_timestamps.loc[distinct_timestamps.duplicated()].unique()
    conflicted = measurements["timestamp"].isin(conflicted_dates)

    keep = np.ones(len(df), dtype=bool)
    keep[positions] = ~(redundant | conflicted).to_numpy()
    resolved = df.iloc[keep]
    quality["duplicate_rows_removed"] += int((redundant & ~conflicted).sum())
    quality["conflicting_timestamp_rows"] += int(conflicted.sum())
    quality["conflicting_timestamps"] += len(conflicted_dates)
    resolved.attrs["measurement_interval_quality"] = quality.copy()
    return resolved, quality


def _energy_readings(df: pd.DataFrame, kwh_cols: Sequence[str]) -> pd.DataFrame:
    """Lấy số đo năng lượng hữu hạn, có thời gian hợp lệ nếu đã ánh xạ."""
    energy = df.loc[:, list(dict.fromkeys(kwh_cols))].apply(finite_readings)
    energy = energy.where(energy.ge(0))
    if "timestamp" in df.columns:
        energy = energy.where(reporting_timestamps(df["timestamp"]).notna(), axis=0)
    return energy


def submeter_columns(kwh_cols: Sequence[str]) -> list[str]:
    """Chọn cột phụ tải cho biểu đồ thành phần và Pareto."""
    return [column for column in dict.fromkeys(kwh_cols) if column != TOTAL_METER]


def _total_source(kwh_cols: Sequence[str]) -> str | None:
    """Xác định phạm vi số điện tổng."""
    if TOTAL_METER in kwh_cols:
        return "metered_total"
    return "submeter_sum" if len(kwh_cols) else None


def _row_energy_total(energy: pd.DataFrame, *, require_complete: bool = False) -> pd.Series:
    """Ưu tiên công tơ tổng cho toàn kỳ, kể cả khi thiếu số đo ở vài dòng."""
    if TOTAL_METER in energy.columns:
        return energy[TOTAL_METER]
    min_count = max(1, len(energy.columns)) if require_complete else 1
    return energy.sum(axis=1, min_count=min_count).rename(TOTAL_METER)


def _optional_number(value: float) -> float | None:
    return float(value) if pd.notna(value) and isfinite(value) else None


def _factory_rows(df: pd.DataFrame) -> pd.Series:
    """Dùng suất tiêu hao theo tấn cho phạm vi có sản xuất."""
    if "facility_type" not in df:
        return pd.Series(True, index=df.index)
    return df["facility_type"].eq("Nha_may").fillna(False)


def _energy_reconciliation(energy: pd.DataFrame) -> dict[str, float | int | None] | None:
    """Đối chiếu điện tổng và tổng phụ tải trên cùng các khoảng đủ số đo."""
    submeter_cols = submeter_columns(list(energy.columns))
    if TOTAL_METER not in energy or not submeter_cols:
        return None

    paired = energy.dropna()
    total = paired[TOTAL_METER].sum(min_count=1)
    subtotal = paired[submeter_cols].sum().sum() if len(paired) else float("nan")
    return {
        "paired_rows": len(paired),
        "metered_total_kwh": _optional_number(total),
        "submeter_kwh": _optional_number(subtotal),
        "residual_kwh": _optional_number(total - subtotal),
    }


def _building_eui(df: pd.DataFrame, energy: pd.DataFrame) -> dict[str, float | int] | None:
    """Tính kWh/m² trong kỳ cho tòa nhà diện tích cố định."""
    if not {"facility_type", "floor_area_m2"}.issubset(df.columns):
        return None
    if not df["facility_type"].eq("Toa_nha").fillna(False).all():
        return None
    floor_area = finite_readings(df["floor_area_m2"])
    positive_area = floor_area.gt(0)
    areas = floor_area.loc[positive_area].unique()
    totals = _row_energy_total(energy, require_complete=True)
    paired = positive_area & totals.notna()
    if len(areas) != 1 or not paired.any():
        return None

    area = float(areas[0])
    kilowatt_hours = float(totals.loc[paired].sum())
    return {
        "floor_area_m2": area,
        "paired_rows": int(paired.sum()),
        "kwh": kilowatt_hours,
        "kwh_per_m2": kilowatt_hours / area,
    }


def _chiller_performance(df: pd.DataFrame, energy: pd.DataFrame) -> dict[str, float | int] | None:
    """Tính hiệu suất từ điện và tải lạnh ghi trên cùng khoảng chạy."""
    if "kwh_chiller" not in energy or "cooling_load_rth" not in df:
        return None
    chiller_energy = energy["kwh_chiller"]
    cooling = finite_readings(df["cooling_load_rth"])
    paired = chiller_energy.gt(0) & cooling.gt(0)
    if not paired.any():
        return None

    kilowatt_hours = float(chiller_energy.loc[paired].sum())
    ton_hours = float(cooling.loc[paired].sum())
    return {
        "paired_rows": int(paired.sum()),
        "electricity_kwh": kilowatt_hours,
        "cooling_load_rth": ton_hours,
        "kw_per_rt": kilowatt_hours / ton_hours,
        "cop": KW_PER_REFRIGERATION_TON * ton_hours / kilowatt_hours,
    }


def _reported_audit_metrics(df: pd.DataFrame, energy: pd.DataFrame) -> dict[str, object]:
    """Tổng hợp chỉ số đã ghi trên các dòng có thời gian hợp lệ."""
    if "timestamp" not in df:
        return {}
    result: dict[str, object] = {}
    for metric, (column, operator) in _AUDIT_METRICS.items():
        if column not in df:
            continue
        readings = finite_readings(df[column])
        valid = readings.ge(0)
        if metric == "power_factor_mean":
            valid &= readings.le(1)
        readings = readings.loc[valid]
        result[metric] = {
            "value": _optional_number(readings.agg(operator)) if len(readings) else None,
            "rows": len(readings),
        }
    if "boiler_fuel_kg" in df:
        readings = finite_readings(df["boiler_fuel_kg"])
        valid = readings.ge(0)
        labels = (
            df["fuel_type"].astype("string").str.strip().replace("", pd.NA)
            if "fuel_type" in df
            else pd.Series(pd.NA, index=df.index, dtype="string")
        )
        groups = readings.loc[valid].groupby(labels.loc[valid].fillna("Chưa ghi loại"))
        result["boiler_fuel_kg_by_type"] = {
            str(label): {"value": _optional_number(amount.sum()), "rows": len(amount)}
            for label, amount in groups
        }
    if (eui := _building_eui(df, energy)) is not None:
        result["building_eui"] = eui
    if (chiller := _chiller_performance(df, energy)) is not None:
        result["chiller"] = chiller
    if result:
        result["valid_timestamp_rows"] = len(df)
    return result


def _audit_sections(
    df: pd.DataFrame,
    energy: pd.DataFrame,
    audit_df: pd.DataFrame | None,
    *,
    start: pd.Timestamp | None = None,
    end: pd.Timestamp | None = None,
) -> dict[str, object]:
    """Đối chiếu và tính chỉ số trên số đo gốc khi được cung cấp."""
    if audit_df is not None and "timestamp" in audit_df:
        source, _ = resolve_measurement_rows(audit_df)
        timestamps = reporting_timestamps(source["timestamp"])
        valid = timestamps.notna()
        if start is not None and end is not None:
            valid &= timestamps.ge(start) & timestamps.lt(end + pd.Timedelta(days=1))
        df = source.loc[valid]
        energy = _energy_readings(df, list(energy.columns))
    return {
        "energy_reconciliation": _energy_reconciliation(energy),
        "reported_audit": _reported_audit_metrics(df, energy),
    }


def clean(
    df: pd.DataFrame, kwh_cols: Sequence[str]
) -> tuple[pd.DataFrame, dict[str, dict[str, int]]]:
    """Loại số đo bất thường và nội suy các khoảng trống ngắn."""
    intervals_df, _ = resolve_measurement_rows(df)
    clean_df = intervals_df.copy(deep=True)
    report: dict[str, dict[str, int]] = {}
    order = pd.RangeIndex(len(clean_df))
    segments = None
    if "timestamp" in clean_df.columns:
        dates = reporting_timestamps(clean_df["timestamp"]).reset_index(drop=True)
        ordered_dates = dates.dropna().sort_values(kind="stable")
        order = ordered_dates.index
        intervals = ordered_dates.diff()
        cadence = intervals[intervals > pd.Timedelta(0)].mode()
        if len(cadence) == 1:
            segments = intervals.ne(cadence.iloc[0]).cumsum()
        else:
            segments = pd.Series(range(len(order)), index=order)

    for column in kwh_cols:
        raw = clean_df[column]
        values = finite_readings(raw).reset_index(drop=True)
        invalid = (raw.notna().to_numpy() & values.isna()) | values.lt(0)
        nonnegative = values.iloc[order].mask(invalid).dropna()
        percentile_99 = nonnegative.quantile(0.99)
        if pd.notna(percentile_99) and percentile_99 > 0:
            invalid |= values > 3 * percentile_99

        values = values.mask(invalid)
        interpolated_count = 0
        if column != TOTAL_METER:
            ordered_values = values.iloc[order]
            missing_before = ordered_values.isna()
            runs = ordered_values.notna().cumsum()
            short_gaps = missing_before & missing_before.groupby(runs).transform("sum").le(3)
            if segments is not None:
                anchors = segments.where(~missing_before)
                short_gaps &= anchors.ffill().eq(anchors.bfill())
            interpolated = ordered_values.interpolate(limit_area="inside").where(short_gaps)
            values.iloc[order] = ordered_values.fillna(interpolated).to_numpy()
            interpolated_count = int(interpolated.notna().sum())
        clean_df[column] = values.to_numpy()
        report[column] = {
            "removed": int(invalid.sum()),
            "interpolated": interpolated_count,
            "missing": int(values.isna().sum()),
        }

    return clean_df, report


def seu_pareto(
    df: pd.DataFrame, kwh_cols: Sequence[str], threshold: float
) -> pd.DataFrame:
    """Tính Pareto tiêu thụ và đánh dấu các hạng mục SEU."""
    if not 0 <= threshold <= 1:
        raise ValueError("Ngưỡng SEU phải nằm trong khoảng từ 0 đến 1.")
    df, _ = resolve_measurement_rows(df)
    totals = _energy_readings(df, submeter_columns(kwh_cols)).sum(axis=0, min_count=1)
    totals = totals.astype(float).sort_values(ascending=False, kind="stable")
    grand_total = float(totals.sum())

    shares = totals / grand_total if grand_total > 0 else totals * 0.0

    result = pd.DataFrame({"kwh": totals, "share": shares})
    result["cum"] = result["share"].cumsum()
    cumulative_before = result["cum"].shift(1, fill_value=0.0)
    result["is_seu"] = totals.gt(0) & cumulative_before.lt(threshold)
    if not result.empty and grand_total > 0:
        result.iloc[0, result.columns.get_loc("is_seu")] = True
    result.index.name = "item"
    return result


def total_kwh(
    df: pd.DataFrame, kwh_cols: Sequence[str], *, require_complete: bool = False
) -> pd.Series:
    """Lấy số đo tổng hoặc cộng các phụ tải trên từng dòng."""
    return _row_energy_total(_energy_readings(df, kwh_cols), require_complete=require_complete)


def dataset_summary(
    df: pd.DataFrame, kwh_cols: Sequence[str], *, audit_df: pd.DataFrame | None = None,
) -> dict[str, object]:
    """Tóm tắt metadata và các chỉ số có sẵn trong dataset."""
    intervals, quality = resolve_measurement_rows(df)
    valid_timestamps = pd.Series(dtype="datetime64[ns]")
    observed = intervals
    if "timestamp" in intervals.columns:
        timestamps = reporting_timestamps(intervals["timestamp"])
        valid_timestamps = timestamps.dropna()
        observed = intervals.loc[timestamps.notna()]
    energy = _energy_readings(observed, kwh_cols)
    energy_totals = energy.sum(min_count=1)
    row_total = _row_energy_total(energy)
    production = (
        finite_readings(observed["production_t"]).sum(min_count=1)
        if "production_t" in observed.columns
        else float("nan")
    )
    temperature = (
        finite_readings(observed["temp_c"]).mean()
        if "temp_c" in observed.columns
        else float("nan")
    )

    return {
        "rows": int(len(df)),
        "columns": [str(column) for column in df.columns],
        "missing_values_by_column": {
            str(column): int(count) for column, count in df.isna().sum().items()
        },
        "date_start": valid_timestamps.min().date().isoformat() if len(valid_timestamps) else None,
        "date_end": valid_timestamps.max().date().isoformat() if len(valid_timestamps) else None,
        "valid_timestamp_rows": len(valid_timestamps),
        "interval_quality": quality,
        "energy_rows_by_column": energy.count().to_dict(),
        "energy_kwh_by_column": {
            str(column): float(value) if pd.notna(value) else None
            for column, value in energy_totals.items()
        },
        "total_kwh": _optional_number(row_total.sum(min_count=1)),
        "total_source": _total_source(kwh_cols),
        "total_energy_rows": int(row_total.count()),
        **_audit_sections(observed, energy, audit_df),
        "production_tons": float(production) if pd.notna(production) else None,
        "average_temp_c": float(temperature) if pd.notna(temperature) else None,
    }


def dashboard_analysis(
    df: pd.DataFrame,
    level: str,
    kwh_cols: Sequence[str],
    start_date: str,
    end_date: str,
    tariff_vnd_per_kwh: float | None,
    *,
    audit_df: pd.DataFrame | None = None,
) -> dict[str, object]:
    """Tổng hợp biểu đồ và KPI cho khoảng ngày đã chọn."""
    start = pd.Timestamp(start_date)
    end = pd.Timestamp(end_date)
    if start > end:
        raise ValueError("start_date phải trước hoặc bằng end_date.")

    df, quality = resolve_measurement_rows(df)
    timestamps = reporting_timestamps(df["timestamp"])
    mask = timestamps.ge(start) & timestamps.lt(end + pd.Timedelta(days=1))
    filtered_df = df.loc[mask]
    aggregated = aggregate_consumption(filtered_df, level, kwh_cols)
    energy = _energy_readings(filtered_df, kwh_cols)
    category_totals = energy.sum(min_count=1)
    total_energy = aggregated["kwh_total"].sum(min_count=1)
    production_readings = (
        finite_readings(filtered_df["production_t"])
        if "production_t" in filtered_df.columns
        else pd.Series(float("nan"), index=filtered_df.index)
    )
    production = production_readings.sum(min_count=1)
    matched_energy = _row_energy_total(energy, require_complete=True)
    paired_rows = matched_energy.notna() & production_readings.ge(0) & _factory_rows(filtered_df)
    paired_production = production_readings.loc[paired_rows].sum()
    intensity = (
        float(matched_energy.loc[paired_rows].sum() / paired_production)
        if paired_production > 0
        else None
    )
    temperature = (
        finite_readings(filtered_df["temp_c"]).mean()
        if "temp_c" in filtered_df.columns
        else float("nan")
    )
    total_value = float(total_energy) if pd.notna(total_energy) else None
    production_value = float(production) if pd.notna(production) else None
    tariff_value = float(tariff_vnd_per_kwh) if tariff_vnd_per_kwh is not None else None

    return {
        "aggregated": aggregated,
        "summary": {
            "start_date": start.date().isoformat(),
            "end_date": end.date().isoformat(),
            "rows": int(len(filtered_df)),
            "interval_quality": quality,
            "total_kwh": total_value,
            "total_source": _total_source(kwh_cols),
            "total_energy_rows": int(_row_energy_total(energy).count()),
            **_audit_sections(filtered_df, energy, audit_df, start=start, end=end),
            "energy_rows_by_column": energy.count().to_dict(),
            "energy_kwh_by_column": {
                str(column): float(value) if pd.notna(value) else None
                for column, value in category_totals.items()
            },
            "production_tons": production_value,
            "kwh_per_ton": intensity,
            "intensity_rows": int(paired_rows.sum()),
            "average_temp_c": float(temperature) if pd.notna(temperature) else None,
            "tariff_vnd_per_kwh": tariff_value,
            "estimated_cost_vnd": (
                total_value * tariff_value
                if total_value is not None and tariff_value is not None
                else None
            ),
        },
    }


def aggregate_consumption(
    df: pd.DataFrame, level: str, kwh_cols: Sequence[str]
) -> pd.DataFrame:
    """Gom điện, nhiệt độ và sản lượng theo ngày, tuần hoặc tháng."""
    df, _ = resolve_measurement_rows(df)
    timestamps = reporting_timestamps(df["timestamp"])
    if level == "day":
        periods = timestamps.dt.floor("D")
    elif level == "week":
        periods = timestamps.dt.to_period("W-SUN").dt.start_time
    elif level == "month":
        periods = timestamps.dt.to_period("M").dt.start_time
    else:
        raise ValueError("level phải là 'day', 'week' hoặc 'month'.")

    data = df.copy()
    data["_period"] = periods
    data = data.loc[data["_period"].notna()]
    if data.empty:
        return pd.DataFrame(
            columns=["timestamp", *submeter_columns(kwh_cols), TOTAL_METER, "temp_c", "production_t"]
        )

    energy = _energy_readings(data, kwh_cols)
    result = energy.groupby(data["_period"]).sum(min_count=1)
    if TOTAL_METER not in result:
        result[TOTAL_METER] = _row_energy_total(energy).groupby(data["_period"]).sum(min_count=1)
    if "temp_c" in data.columns:
        temperature = finite_readings(data["temp_c"])
        result["temp_c"] = temperature.groupby(data["_period"]).mean()
    else:
        result["temp_c"] = float("nan")
    if "production_t" in data.columns:
        production = finite_readings(data["production_t"])
        result["production_t"] = production.groupby(data["_period"]).sum(min_count=1)
    else:
        result["production_t"] = float("nan")
    result.index.name = "timestamp"
    return result.reset_index()


def enpi_regression(
    df: pd.DataFrame, total_col: str, drivers: Sequence[str]
) -> dict[str, object]:
    """Hồi quy EnPI và tính suất tiêu hao tổng thể, theo tháng."""
    required_columns = list(dict.fromkeys([total_col, *drivers, "production_t"]))
    if total_col not in df.columns:
        raise ValueError(f"Không tìm thấy cột điện {total_col}.")
    df, _ = resolve_measurement_rows(df)
    numeric_data = df.reindex(columns=required_columns).apply(finite_readings)
    timestamps = reporting_timestamps(df["timestamp"])
    valid_energy = timestamps.notna() & numeric_data[total_col].ge(0)
    matched_rows = valid_energy & numeric_data["production_t"].ge(0) & _factory_rows(df)
    matched = numeric_data.loc[matched_rows, [total_col, "production_t"]]
    production_total = matched["production_t"].sum()
    kwh_per_ton = (
        float(matched[total_col].sum() / production_total) if production_total > 0 else None
    )
    months = timestamps.loc[matched_rows].dt.strftime("%Y-%m").rename("month")
    monthly_totals = matched.groupby(months).sum()
    monthly_production = monthly_totals["production_t"]
    monthly_kwh_per_ton = monthly_totals[total_col].div(monthly_production.where(monthly_production.gt(0)))
    monthly_kwh_per_ton.name = "kwh_per_ton"

    driver_labels = list(drivers)
    regression_rows = valid_energy & numeric_data[driver_labels].ge(0).all(axis=1)
    regression_data = numeric_data.loc[regression_rows]
    coefficients, r2, note = _fit_enpi(regression_data, total_col, driver_labels)

    return {
        "coefficients": coefficients,
        "r2": r2,
        "kwh_per_ton": kwh_per_ton,
        "monthly_kwh_per_ton": monthly_kwh_per_ton,
        "regression_rows": int(regression_rows.sum()),
        "intensity_rows": int(matched_rows.sum()),
        "regression_note": note,
    }


def _fit_enpi(
    data: pd.DataFrame, total_col: str, drivers: list[str]
) -> tuple[dict[str, float], float | None, str | None]:
    """Ước lượng OLS, kiểm tra bậc tự do và tính độc lập của biến."""
    parameter_count = 1 + len(drivers)
    if len(data) <= parameter_count:
        return {}, None, f"Cần ít nhất {parameter_count + 1} dòng có đủ điện và biến hồi quy."

    y = data[total_col].to_numpy()
    driver_values = data[drivers].to_numpy()
    centers = driver_values.mean(axis=0)
    scales = driver_values.std(axis=0)
    if np.any(scales == 0):
        return {}, None, "Có biến hồi quy giữ nguyên trong toàn bộ dữ liệu hợp lệ."

    # Chuẩn hóa để kiểm tra hạng không phụ thuộc vào thang đo của các biến.
    design = np.column_stack((np.ones(len(y)), (driver_values - centers) / scales))
    fitted, _, rank, _ = np.linalg.lstsq(design, y, rcond=None)
    if rank != parameter_count:
        return {}, None, "Các biến hồi quy phụ thuộc tuyến tính."

    slopes = fitted[1:] / scales
    coefficients = {
        "intercept": float(fitted[0] - centers @ slopes),
        **dict(zip(drivers, map(float, slopes))),
    }
    total_sum = float(np.sum((y - y.mean()) ** 2))
    if total_sum == 0:
        return coefficients, None, "R² không xác định vì mức điện giữ nguyên."
    residual_sum = float(np.sum((y - design @ fitted) ** 2))
    return coefficients, 1.0 - residual_sum / total_sum, None


def compare_periods(
    df: pd.DataFrame, month_a: str, month_b: str, kwh_cols: Sequence[str]
) -> dict[str, object]:
    """So sánh tháng B với tháng A theo điện, nhiệt độ và sản lượng."""
    df, quality = resolve_measurement_rows(df)
    days = reporting_timestamps(df["timestamp"]).dt.normalize()
    months = days.dt.strftime("%Y-%m")
    meters = list(dict.fromkeys(kwh_cols))
    readings = df.reindex(columns=[*meters, "production_t", "temp_c"]).apply(finite_readings)
    readings[meters] = readings[meters].where(readings[meters].ge(0))
    grouped = readings.groupby(months)
    pair = [month_a, month_b]
    totals = grouped[[*meters, "production_t"]].sum(min_count=1).reindex(pair)
    total_energy = _row_energy_total(readings[meters]).groupby(months).sum(min_count=1).reindex(pair)
    temperature = grouped["temp_c"].mean().reindex(pair)
    differences = totals.iloc[1] - totals.iloc[0]
    kwh_differences = differences.loc[meters]
    submeter_changes = kwh_differences.loc[submeter_columns(meters)]
    positive_kwh_changes = submeter_changes[submeter_changes.gt(0)]
    temperature_difference = temperature.iloc[1] - temperature.iloc[0]
    production_difference = differences["production_t"]
    row_counts = grouped.size().reindex(pair, fill_value=0)
    day_counts = days.groupby(months).nunique().reindex(pair, fill_value=0)
    meter_rows = grouped[meters].count().reindex(pair, fill_value=0)
    daily_energy = readings[meters].groupby(days).sum(min_count=1)
    by_month = daily_energy.groupby(daily_energy.index.strftime("%Y-%m"))
    meter_days = by_month.count().reindex(pair, fill_value=0)
    daily_means = by_month.mean().reindex(pair)
    daily_differences = daily_means.iloc[1] - daily_means.iloc[0]

    coverage = {
        label: {
            "calendar_days": pd.Period(month, freq="M").days_in_month,
            "recorded_days": int(day_counts.iloc[index]),
            "meter_days": meter_days.iloc[index].to_dict(),
            "meter_rows": meter_rows.iloc[index].to_dict(),
        }
        for index, (label, month) in enumerate(zip(("month_a", "month_b"), pair))
    }

    return {
        "month_a": month_a,
        "month_b": month_b,
        "rows_month_a": int(row_counts.iloc[0]),
        "rows_month_b": int(row_counts.iloc[1]),
        "coverage": coverage,
        "interval_quality": quality,
        "total_source": _total_source(kwh_cols),
        "total_kwh_difference": _optional_number(total_energy.iloc[1] - total_energy.iloc[0]),
        "kwh_differences": {
            str(column): float(value) if pd.notna(value) else None
            for column, value in kwh_differences.items()
        },
        "mean_daily_kwh_differences": {
            str(column): float(value) if pd.notna(value) else None
            for column, value in daily_differences.items()
        },
        "temperature_difference_c": (
            float(temperature_difference) if pd.notna(temperature_difference) else None
        ),
        "production_difference_t": float(production_difference) if pd.notna(production_difference) else None,
        "main_driver": str(positive_kwh_changes.idxmax()) if not positive_kwh_changes.empty else None,
        "comparison_note": (
            "Chênh lệch dựa trên điện quan sát trong hai kỳ. Bình quân ngày của mỗi hạng mục dùng "
            "các ngày có ít nhất một số đo hợp lệ. Để đánh giá nguyên nhân, cần đối chiếu độ phủ "
            "khoảng đo, sản lượng, thời tiết và điều kiện vận hành."
        ),
    }


def opportunity(
    name: str,
    base_kwh: float | None,
    saving_pct: float,
    tariff: float,
    capex: float,
    emission_factor: float | None,
) -> dict[str, object]:
    """Ước tính tiết kiệm năng lượng, chi phí, phát thải và hoàn vốn."""
    assumptions = {"Giá điện": tariff, "Vốn đầu tư": capex}
    if base_kwh is not None:
        assumptions["Điện năng cơ sở"] = base_kwh
    if emission_factor is not None:
        assumptions["Hệ số phát thải"] = emission_factor
    for label, value in assumptions.items():
        if not isfinite(value) or value < 0:
            raise ValueError(f"{label} phải là số hữu hạn không âm.")
    if not 0 <= saving_pct <= 1:
        raise ValueError("Tỷ lệ tiết kiệm phải nằm trong khoảng từ 0 đến 1.")

    saved_kwh = float(base_kwh * saving_pct) if base_kwh is not None else None
    saved_vnd = float(saved_kwh * tariff) if saved_kwh is not None else None
    co2_tonnes = (
        float(saved_kwh * emission_factor / 1000)
        if emission_factor is not None and saved_kwh is not None
        else None
    )
    roi_pct = float(saved_vnd / capex * 100) if saved_vnd is not None and capex > 0 else None
    payback_months = (
        float(capex / saved_vnd * 12) if saved_vnd is not None and saved_vnd > 0 else None
    )

    emission_assumption = (
        "chưa có hệ số phát thải"
        if emission_factor is None
        else f"hệ số phát thải {emission_factor:g} kg/kWh"
    )
    assumption = (
        f"GIẢ ĐỊNH - cần kiểm chứng: {name}; tỷ lệ tiết kiệm {saving_pct:.1%}; "
        f"giá điện {tariff:,.0f} VND/kWh; vốn đầu tư {capex:,.0f} VND; "
        f"{emission_assumption}. Tiết kiệm tiền là ước tính mỗi năm; "
        "ROI (%) = tiết kiệm tiền năm / vốn đầu tư × 100; "
        "hoàn vốn (tháng) = vốn đầu tư / tiết kiệm tiền năm × 12."
    )

    return {
        "name": name,
        "saved_kwh": saved_kwh,
        "saved_vnd": saved_vnd,
        "co2_tonnes": co2_tonnes,
        "roi_pct": roi_pct,
        "payback_months": payback_months,
        "assumption": assumption,
    }


def list_opportunities(
    df: pd.DataFrame,
    opportunities: Sequence[Mapping[str, object]],
    tariff: float,
    emission_factor: float | None,
) -> list[dict[str, object]]:
    """Tính cơ hội mẫu theo điện tiêu thụ quy đổi năm."""
    df, _ = resolve_measurement_rows(df)
    days = (
        reporting_timestamps(df["timestamp"]).dt.normalize()
        if "timestamp" in df.columns
        else None
    )
    results: list[dict[str, object]] = []
    for item in opportunities:
        name = str(item["name"])
        applies_to = str(item["applies_to"])
        saving_pct = float(item["saving_pct"])
        capex = float(item["capex_vnd"])

        base_kwh_annual = None
        observed_days = 0
        if applies_to not in df.columns:
            annualization_note = f"Không tìm thấy cột {applies_to} trong dữ liệu."
        elif days is None:
            annualization_note = "Cần thời gian đo để quy đổi tiêu thụ năm."
        else:
            consumption = finite_readings(df[applies_to])
            valid_rows = days.notna() & consumption.ge(0)
            observed_days = int(days.loc[valid_rows].nunique())
            if observed_days:
                base_kwh_annual = float(consumption.loc[valid_rows].sum() / observed_days * 365)
                annualization_note = (
                    f"Quy đổi bình quân {observed_days} ngày có số đo lên 365 ngày, "
                    "giả định số đo trong từng ngày đầy đủ và các ngày đại diện cho cả năm."
                )
            else:
                annualization_note = "Không có số đo kèm ngày hợp lệ để ước tính mức tiêu thụ năm."

        result = opportunity(
            name=name,
            base_kwh=base_kwh_annual,
            saving_pct=saving_pct,
            tariff=tariff,
            capex=capex,
            emission_factor=emission_factor,
        )
        result["applies_to"] = applies_to
        result["base_kwh_annual"] = base_kwh_annual
        result["observed_days"] = observed_days
        result["assumption"] += f" {annualization_note}"
        results.append(result)
    return results
