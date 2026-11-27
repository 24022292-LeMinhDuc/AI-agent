"""Cung cấp các công cụ tính toán tất định cho dữ liệu năng lượng."""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Mapping, Sequence
from math import isfinite

import numpy as np
import pandas as pd


REPORTING_TIMEZONE = "Asia/Ho_Chi_Minh"
TOTAL_METER = "kwh_total"
KW_PER_REFRIGERATION_TON = 3.51685284
GJ_PER_KWH = 0.0036
GJ_PER_TOE = 41.868
FUEL_TYPES = ("biomass", "coal", "do_oil", "fo_oil", "lpg", "cng")
_FUEL_NAMES = {
    **{name.replace("_", " "): name for name in FUEL_TYPES},
    "than da": "coal", "do": "do_oil", "fo": "fo_oil", "khi tu nhien": "cng",
}
_PERIOD_NAMES = {
    "cao diem": "peak", "peak": "peak", "binh thuong": "normal", "normal": "normal",
    "thap diem": "off_peak", "off peak": "off_peak",
}
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


def normalize_label(value: object) -> str:
    text = str(value).lower().replace("đ", "d")
    decomposed = unicodedata.normalize("NFD", text)
    without_marks = "".join(char for char in decomposed if unicodedata.category(char) != "Mn")
    return re.sub(r"[^a-z0-9]+", " ", without_marks).strip()


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


def total_meter_columns(df: pd.DataFrame, kwh_cols: Sequence[str]) -> list[str]:
    """Chọn cùng một ranh giới điện tổng cho toàn bộ dữ liệu."""
    if TOTAL_METER in kwh_cols and TOTAL_METER in df:
        intervals, _ = resolve_measurement_rows(df)
        if _energy_readings(intervals, [TOTAL_METER])[TOTAL_METER].notna().any():
            return [TOTAL_METER]
    return submeter_columns(kwh_cols)


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


def _reported_audit_metrics(
    df: pd.DataFrame, energy: pd.DataFrame, total_columns: Sequence[str],
) -> dict[str, object]:
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
    if (eui := _building_eui(df, energy[list(total_columns)])) is not None:
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
    total_columns: Sequence[str],
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
        "energy_reconciliation": _energy_reconciliation(energy) if TOTAL_METER in total_columns else None,
        "reported_audit": _reported_audit_metrics(df, energy, total_columns),
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
    columns = total_meter_columns(df, kwh_cols)
    return _row_energy_total(_energy_readings(df, columns), require_complete=require_complete)


def _audit_readings(df: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame, pd.Series, str | None, dict]:
    rows, quality = resolve_measurement_rows(df)
    if "timestamp" in rows:
        dates = reporting_timestamps(rows["timestamp"])
        quality["invalid_timestamp_rows"] = int(dates.isna().sum())
        rows = rows.assign(timestamp=dates).dropna(subset=["timestamp"])
    rows = rows.reset_index(drop=True)
    meters = [column for column in rows if str(column).startswith("kwh_")]
    energy = _energy_readings(rows, meters)
    boundary = total_meter_columns(rows, meters)
    total = (_row_energy_total(energy[boundary], require_complete=True) if boundary
             else pd.Series(float("nan"), index=rows.index))
    return rows, energy, total, _total_source(boundary), quality


def _reading(df: pd.DataFrame, column: str) -> pd.Series:
    values = finite_readings(df[column]) if column in df else pd.Series(float("nan"), index=df.index)
    return values.where(values.ge(0))


def _energy_amount(series: pd.Series) -> dict:
    gj = _optional_number(series.sum(min_count=1))
    return {"gj": gj, "toe": gj / GJ_PER_TOE if gj is not None else None, "rows": int(series.count())}


def _availability(available: bool, reason: str) -> dict:
    return {"available": bool(available), "reason": None if available else reason}


def _audit_warnings(quality: Mapping) -> list[str]:
    warnings = []
    if quality["conflicting_timestamp_rows"]:
        warnings.append(f"Đã loại {quality['conflicting_timestamp_rows']} dòng xung đột tại cùng mốc đo. "
                        "Cần mốc riêng cho các khoảng đo thuộc khung giá khác nhau.")
    if quality.get("invalid_timestamp_rows"):
        warnings.append(f"Đã loại {quality['invalid_timestamp_rows']} dòng thiếu thời gian hợp lệ.")
    return warnings


def energy_balance_analysis(df: pd.DataFrame, config: Mapping, fuel_type: str | None = None) -> dict:
    """Cân bằng năng lượng đầu vào theo NCV và tính hiệu suất trên số đo ghép cặp."""
    rows, energy, total, boundary, quality = _audit_readings(df)
    branches = energy[submeter_columns(energy.columns)]
    electric = _energy_amount(total * GJ_PER_KWH)
    # Chỉ phân bổ nhánh trong khoảng có công tơ biên, giữ phần dư riêng.
    subtotal = branches.sum(axis=1)
    excess = total.notna() & subtotal.gt(total + 1e-9)
    allocatable = total.notna() & ~excess
    allocation = branches.loc[allocatable].sum(min_count=1).dropna().mul(GJ_PER_KWH).to_dict()
    if electric["gj"] is not None:
        residual = electric["gj"] - sum(allocation.values())
        if residual > 1e-10 or not allocation:
            allocation["unallocated_electricity"] = max(0., residual)

    mass = _reading(rows, "boiler_fuel_kg")
    default = config.get("default_boiler_fuel", "biomass")
    if fuel_type is None and "fuel_type" in rows:
        names = rows["fuel_type"].astype("string").str.strip().replace("", pd.NA).fillna(default)
    else:
        names = pd.Series(fuel_type or default, index=rows.index, dtype="string")
    fuels = names.map(normalize_label).map(_FUEL_NAMES)
    properties = config.get("fuel_properties", {})
    heat = pd.Series(float("nan"), index=rows.index)
    fuel_rows = []
    for name, group in mass.groupby(fuels):
        prop = properties.get(name, {})
        ncv = prop.get("ncv_gj_per_kg")
        if not group.notna().any() or ncv is None or not isfinite(ncv) or ncv <= 0:
            continue
        gj = group * ncv
        heat.loc[group.index] = gj
        amount = _energy_amount(gj)
        factor = prop.get("co2_kg_per_gj")
        fuel_rows.append({
            "fuel_type": name, "kg": float(group.sum()), **amount, "ncv_gj_per_kg": ncv,
            "co2_kg_per_gj": factor,
            "combustion_co2_kg": amount["gj"] * factor if factor is not None else None,
        })
    unknown_mass = float(mass[heat.isna()].sum())
    enthalpy = config.get("steam_enthalpy_gj_per_t", 2.76)
    if not isfinite(enthalpy) or enthalpy <= 0:
        raise ValueError("Nhiệt hữu ích mỗi tấn hơi phải là số dương hữu hạn.")
    steam = _reading(rows, "steam_t") * enthalpy
    cooling = _reading(rows, "cooling_load_rth") * KW_PER_REFRIGERATION_TON * GJ_PER_KWH
    paired = heat.gt(0) & steam.notna()
    boiler = float(100 * steam[paired].sum() / heat[paired].sum()) if paired.any() else None
    chiller = _chiller_performance(rows, energy) or {}
    kw_rt = chiller.get("kw_per_rt")

    flows = [("electricity", key, value) for key, value in allocation.items()]
    flows.extend((row["fuel_type"], "boiler", row["gj"]) for row in fuel_rows)
    nodes = list(dict.fromkeys(node for source, target, _ in flows for node in (source, target)))
    links = [{"source": nodes.index(source), "target": nodes.index(target), "value": value}
             for source, target, value in flows if value > 0]
    uses = dict(allocation)
    if fuel_rows:
        uses["boiler"] = sum(row["gj"] for row in fuel_rows)
    input_gj = sum(uses.values()) if uses else None
    ranked = []
    cumulative = 0.
    for key, gj in sorted(uses.items(), key=lambda item: -item[1]):
        share = 100 * gj / input_gj if input_gj else None
        ranked.append({"use": key, "gj": gj, "toe": gj / GJ_PER_TOE, "share_pct": share,
                       "is_seu": gj > 0 and cumulative < 100 * config.get("seu_threshold", .8)})
        cumulative += share or 0
    available = input_gj is not None or steam.notna().any() or cooling.notna().any()
    warnings = _audit_warnings(quality)
    sections = {
        "boiler": _availability(boiler is not None, "Cần kg nhiên liệu có nhiệt trị và tấn hơi ở cùng khoảng đo."),
        "chiller": _availability(kw_rt is not None, "Cần điện chiller và tải lạnh RTh dương ở cùng khoảng đo."),
    }
    if unknown_mass:
        warnings.append(f"{unknown_mass:g} kg nhiên liệu chưa xác định được nhiệt trị.")
    if excess.any():
        warnings.append("Có khoảng tổng công tơ nhánh vượt công tơ tổng. Điện của khoảng này được giữ ở phần chưa phân bổ.")
    if boiler is not None and boiler > 100:
        warnings.append("Hiệu suất lò vượt 100%. Kiểm tra nhiệt trị, nhiệt hữu ích và ranh giới đo.")
    return {
        **_availability(available, " ".join(warnings) or "Thiếu số đo điện, nhiên liệu quy đổi được, hơi hoặc tải lạnh."),
        "total_input_gj": input_gj, "total_input_toe": input_gj / GJ_PER_TOE if input_gj is not None else None,
        "electricity": electric, "electricity_source": boundary, "fuels": fuel_rows,
        "unconverted_fuel_kg": unknown_mass, "overmetered_rows": int(excess.sum()),
        "electric_branches": {key: _energy_amount(branches[key] * GJ_PER_KWH) for key in branches},
        "outputs": {"steam": _energy_amount(steam), "cooling": _energy_amount(cooling)},
        "boiler_efficiency_pct": boiler, "boiler_paired_rows": int(paired.sum()), "steam_enthalpy_gj_per_t": enthalpy,
        "chiller_kw_per_rt": kw_rt, "chiller_cop": chiller.get("cop"), "chiller_paired_rows": chiller.get("paired_rows", 0),
        "chiller_rating": None if kw_rt is None else ("Tốt" if kw_rt < .70 else "Trung bình" if kw_rt <= .85 else "Kém"),
        "sankey_nodes": nodes, "sankey_links": links, "seu_breakdown": ranked,
        "default_boiler_fuel": default, "fuel_override": fuel_type, "seu_threshold": config.get("seu_threshold", .8),
        "warnings": warnings, "interval_quality": quality, "sections": sections,
    }


def _tariff_periods(df: pd.DataFrame) -> pd.Series:
    labels = df.get("tariff_period", pd.Series(index=df.index, dtype="string"))
    return labels.map(normalize_label, na_action="ignore").map(_PERIOD_NAMES)


def _row_prices(periods: pd.Series, tariff) -> pd.Series:
    if isinstance(tariff, Mapping):
        rates = periods.map(tariff)
    else:
        rates = pd.Series(tariff, index=periods.index, dtype="float64")
    rates = finite_readings(rates)
    return rates.where(rates.ge(0))


def _weighted_price(kwh: pd.Series, rates: pd.Series) -> float | None:
    """Đơn giá bình quân chỉ dùng khi mọi kWh dương đều có giá."""
    if (kwh.gt(0) & rates.isna()).any() or kwh.sum() <= 0:
        return None
    return float((kwh * rates).sum() / kwh.sum())


def tariff_and_power_quality_analysis(df: pd.DataFrame, config: Mapping) -> dict:
    """Phân tích điện năng theo nhãn TOU và chất lượng điện trên cùng khoảng đo."""
    rows, _, total, boundary, quality = _audit_readings(df)
    times = rows.get("timestamp", pd.Series(pd.NaT, index=rows.index, dtype="datetime64[ns]"))
    periods = _tariff_periods(rows)
    tariff = config.get("tariff_vnd_per_kwh")
    rates = _row_prices(periods, tariff)
    costs = total * rates
    known = total.notna() & periods.notna()
    unknown = total.notna() & periods.isna()
    breakdown = []
    for period in ("peak", "normal", "off_peak"):
        mask = known & periods.eq(period)
        price = tariff.get(period) if isinstance(tariff, Mapping) else tariff
        kwh = float(total[mask].sum()) if known.any() else None
        breakdown.append({"period": period, "rows": int(mask.sum()), "kwh": kwh, "rate_vnd_per_kwh": price,
                          "cost_vnd": kwh * price if kwh is not None and price is not None and price >= 0 else None})
    priced = _optional_number(costs.sum(min_count=1))
    estimated = None if (total.gt(0) & rates.isna()).any() else priced
    peak_kwh = breakdown[0]["kwh"]
    peak_share = 100 * peak_kwh / total.sum() if known.any() and not total[unknown].sum() and total.sum() > 0 else None
    peak_rate, off_rate = breakdown[0]["rate_vnd_per_kwh"], breakdown[2]["rate_vnd_per_kwh"]
    shift = None
    if peak_share is not None and peak_rate is not None and off_rate is not None:
        shift = .10 * peak_kwh * max(0., peak_rate - off_rate)

    profile = pd.DataFrame({"month": times.dt.strftime("%Y-%m").fillna("Toàn bộ"), "period": periods, "kwh": total})
    profile = profile[known].groupby(["month", "period"], as_index=False)["kwh"].sum()
    demand = _reading(rows, "peak_demand_kw")
    peak = _optional_number(demand.max())
    peak_time = times[demand.eq(peak)].min() if peak is not None else pd.NaT
    intervals = times.sort_values().diff().dropna().unique()
    load_factor = hours = None
    load_reason = "Cần kWh và đỉnh kW đủ trên các khoảng đo đều, liên tiếp, tối đa một ngày mỗi khoảng."
    warnings = _audit_warnings(quality)
    if len(intervals) == 1 and times.notna().all() and total.notna().all() and demand.notna().all() and peak:
        step = pd.Timedelta(intervals[0]) / pd.Timedelta(hours=1)
        if 0 < step <= 24:
            hours = float(step * len(rows))
            load_factor = float(total.sum() / (hours * peak))
            if load_factor > 1 + 1e-9:
                load_factor = None
                load_reason = "Công suất trung bình từ kWh vượt đỉnh kW. Kiểm tra đơn vị, khoảng đo và ranh giới điện."
                warnings.append(load_reason)

    policy = config.get("power_factor_penalty", {})
    target = float(policy.get("min_pf", .90))
    demand_rate = float(policy.get("demand_charge_vnd_per_kw", 0))
    if not 0 < target <= 1 or not isfinite(demand_rate) or demand_rate < 0:
        raise ValueError("Ngưỡng cos φ phải trong (0, 1] và đơn giá công suất phải không âm, hữu hạn.")
    pf = _reading(rows, "power_factor")
    pf = pf.where(pf.gt(0) & pf.le(1))
    low = pf.lt(target)
    surcharge = costs[low] * (target / pf[low] - 1)
    unknown_pf = pf.isna() & total.ne(0)
    penalty = (float(surcharge.sum()) if pf.notna().any() and surcharge.notna().all()
               and not unknown_pf.any() else None)
    # Ghép kW và PF cùng bản ghi trước khi chọn nhu cầu bù lớn nhất.
    kvar = demand[low] * (np.tan(np.arccos(pf[low])) - np.tan(np.arccos(target)))
    alerts = pd.DataFrame({"day": times[low].dt.strftime("%Y-%m-%d").fillna("Chưa có ngày"),
                           "pf": pf[low], "penalty": surcharge, "kvar": kvar})
    daily = alerts.groupby("day").agg(records=("pf", "size"), min_pf=("pf", "min"), compensation_kvar=("kvar", "max"))
    daily["penalty_vnd"] = alerts.groupby("day")["penalty"].sum(min_count=1)
    incomplete_days = times[unknown_pf | (low & costs.isna())].dt.strftime("%Y-%m-%d").fillna("Chưa có ngày")
    daily.loc[daily.index.isin(incomplete_days), "penalty_vnd"] = np.nan
    monthly_peaks = demand.groupby(times.dt.to_period("M")).max().dropna()
    reported = _reading(rows, "energy_cost_vnd")
    reported_cost = _optional_number(reported.sum(min_count=1))
    cost_difference = costs - reported
    sections = {
        "tou": _availability(known.any(), "Cần kWh và nhãn khung giờ hợp lệ trên cùng khoảng đo."),
        "demand": _availability(peak is not None, "Thiếu số đo công suất đỉnh kW hợp lệ."),
        "load_factor": _availability(load_factor is not None, load_reason),
        "power_factor": _availability(pf.notna().any(), "Cần cos φ đo được trong khoảng (0, 1]."),
    }
    if known.any() and (total.gt(0) & rates.isna()).any():
        warnings.append("Có kWh chưa xác định đơn giá. Bảng chi phí chỉ phản ánh các khoảng đã ghép được giá.")
    if total.notna().any() and total.isna().any():
        warnings.append("Có khoảng thiếu điện năng tại ranh giới đo. Kết quả TOU dùng các khoảng đủ điện.")
    if pf.notna().any() and (unknown_pf.any() or surcharge.isna().any()):
        warnings.append("Chưa đủ cos φ hoặc tiền điện theo khoảng để ước tính tiền phạt cả kỳ.")
    available = bool(known.any() or peak is not None or pf.notna().any() or reported_cost is not None or priced is not None)
    return {
        **_availability(available, " ".join(warnings) or "Cần số đo có khung giá, chi phí, công suất đỉnh hoặc cos φ hợp lệ."),
        "electricity_source": boundary, "tou": breakdown, "tou_profile": profile.to_dict("records"),
        "peak_share_pct": peak_share, "shift_savings_vnd": shift, "shift_fraction": .10,
        "estimated_cost_vnd": estimated, "priced_cost_vnd": priced, "reported_cost_vnd": reported_cost,
        "cost_difference_vnd": _optional_number(cost_difference.sum(min_count=1)), "cost_paired_rows": int(cost_difference.count()),
        "unclassified_kwh": float(total[unknown].sum()), "energy_rows": int(total.count()), "priced_rows": int(costs.count()),
        "peak_demand_kw": peak, "peak_timestamp": peak_time.isoformat() if pd.notna(peak_time) else None,
        "load_factor": load_factor, "period_hours": hours, "min_pf": target,
        "mean_power_factor": _optional_number(pf.mean()), "pf_rows": int(pf.count()),
        "low_pf_records": int(low.sum()) if pf.notna().any() else None,
        "low_pf_days": int(times[low].dt.normalize().nunique()) if pf.notna().any() and times.notna().any() else None,
        "reactive_penalty_vnd": penalty, "penalty_priced_rows": int(surcharge.count()),
        "compensation_kvar": _optional_number(kvar.max()) if low.any() else (0. if pf.notna().any() else None),
        "demand_charge_vnd": float(monthly_peaks.sum() * demand_rate) if len(monthly_peaks) else None,
        "demand_charge_vnd_per_kw": demand_rate,
        "pf_alerts": daily.reset_index().astype(object).where(lambda table: table.notna(), None).to_dict("records"),
        "interval_quality": quality, "warnings": warnings, "sections": sections,
    }


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
    total_columns = total_meter_columns(intervals, kwh_cols)
    row_total = _row_energy_total(energy[total_columns])
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
        "total_source": _total_source(total_columns),
        "total_energy_rows": int(row_total.count()),
        **_audit_sections(observed, energy, audit_df, total_columns),
        "production_tons": float(production) if pd.notna(production) else None,
        "average_temp_c": float(temperature) if pd.notna(temperature) else None,
    }


def dashboard_analysis(
    df: pd.DataFrame,
    level: str,
    kwh_cols: Sequence[str],
    start_date: str,
    end_date: str,
    tariff_vnd_per_kwh: float | Mapping[str, float] | None,
    *,
    audit_df: pd.DataFrame | None = None,
) -> dict[str, object]:
    """Tổng hợp biểu đồ và KPI cho khoảng ngày đã chọn."""
    start = pd.Timestamp(start_date)
    end = pd.Timestamp(end_date)
    if start > end:
        raise ValueError("start_date phải trước hoặc bằng end_date.")

    df, quality = resolve_measurement_rows(df)
    total_columns = total_meter_columns(df, kwh_cols)
    timestamps = reporting_timestamps(df["timestamp"])
    mask = timestamps.ge(start) & timestamps.lt(end + pd.Timedelta(days=1))
    filtered_df = df.loc[mask]
    aggregated = aggregate_consumption(filtered_df, level, kwh_cols, total_columns=total_columns)
    energy = _energy_readings(filtered_df, kwh_cols)
    category_totals = energy.sum(min_count=1)
    total_energy = aggregated["kwh_total"].sum(min_count=1)
    production_readings = (
        finite_readings(filtered_df["production_t"])
        if "production_t" in filtered_df.columns
        else pd.Series(float("nan"), index=filtered_df.index)
    )
    production = production_readings.sum(min_count=1)
    matched_energy = _row_energy_total(energy[total_columns], require_complete=True)
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
    if isinstance(tariff_vnd_per_kwh, Mapping):
        row_rates = _row_prices(_tariff_periods(filtered_df), tariff_vnd_per_kwh)
        tariff_value = _weighted_price(matched_energy, row_rates)
        estimated_cost = _optional_number((matched_energy * row_rates).sum(min_count=1))
        if (matched_energy.gt(0) & row_rates.isna()).any():
            estimated_cost = None
        if _row_energy_total(energy[total_columns]).gt(0).where(matched_energy.isna(), False).any():
            tariff_value = estimated_cost = None
    else:
        tariff_value = float(tariff_vnd_per_kwh) if tariff_vnd_per_kwh is not None else None
        estimated_cost = total_value * tariff_value if total_value is not None and tariff_value is not None else None

    return {
        "aggregated": aggregated,
        "summary": {
            "start_date": start.date().isoformat(),
            "end_date": end.date().isoformat(),
            "rows": int(len(filtered_df)),
            "interval_quality": quality,
            "total_kwh": total_value,
            "total_source": _total_source(total_columns),
            "total_energy_rows": int(_row_energy_total(energy[total_columns]).count()),
            **_audit_sections(filtered_df, energy, audit_df, total_columns, start=start, end=end),
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
            "estimated_cost_vnd": estimated_cost,
        },
    }


def aggregate_consumption(
    df: pd.DataFrame, level: str, kwh_cols: Sequence[str], *, total_columns: Sequence[str] | None = None,
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
    if total_columns is None:
        total_columns = total_meter_columns(df, kwh_cols)
    result[TOTAL_METER] = _row_energy_total(energy[list(total_columns)]).groupby(data["_period"]).sum(min_count=1)
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
    total_columns = total_meter_columns(df, meters)
    total_energy = _row_energy_total(readings[total_columns]).groupby(months).sum(min_count=1).reindex(pair)
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
        "total_source": _total_source(total_columns),
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
    tariff: float | None,
    capex: float,
    emission_factor: float | None,
) -> dict[str, object]:
    """Ước tính tiết kiệm năng lượng, chi phí, phát thải và hoàn vốn."""
    assumptions = {"Vốn đầu tư": capex}
    if tariff is not None:
        assumptions["Giá điện"] = tariff
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
    saved_vnd = float(saved_kwh * tariff) if saved_kwh is not None and tariff is not None else None
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
    price_note = f"giá điện {tariff:,.0f} VND/kWh" if tariff is not None else "thiếu đơn giá cho điện quan sát"
    assumption = (
        f"GIẢ ĐỊNH - cần kiểm chứng: {name}; tỷ lệ tiết kiệm {saving_pct:.1%}; "
        f"{price_note}; vốn đầu tư {capex:,.0f} VND; "
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



def evaluate_ecm_financials(opportunity: dict, financial_assumptions: dict, ghg_assumptions: dict) -> dict:
    """Tính toán toàn bộ chỉ số tài chính và GHG cho một cơ hội ECM.

    Parameters
    ----------
    opportunity: dict
        Dữ liệu cơ hội (saving_pct, capex_vnd, applies_to, …).
    financial_assumptions: dict
        Các giả định tài chính toàn cục (discount_rate, energy_escalation_rate, …).
    ghg_assumptions: dict
        Giả định GHG (default_grid_ef_kgco2e_per_kwh, renewable_electricity_share_pct, irec_kwh).

    Returns
    -------
    dict
        Đối tượng cơ hội đã bổ sung:
        - annual_kwh_saved, annual_cost_saved_vnd, annual_co2_saved_t
        - simple_payback_years, discounted_payback_years, npv_vnd, irr_pct, sir, mac_vnd_per_tco2e
        - ghg_scope1_tco2e, ghg_scope2_location_tco2e, ghg_scope2_market_tco2e, total_emissions_tco2e
    """
    # ------------------------------------------------------------------
    # 1️⃣ Annualisation (kWh saved) – dựa trên dữ liệu năm (đơn vị kWh)
    # ------------------------------------------------------------------
    coverage_days = opportunity.get("coverage_days", 365)  # giả sử có trường này, nếu không thì dùng 365
    total_kwh = opportunity.get("annual_kwh", None)
    if total_kwh is None:
        # Nếu không có dữ liệu đầy đủ, dùng saving_pct * kwh_total hiện có (được truyền từ UI)
        total_kwh = opportunity.get("kwh_total", 0) * opportunity.get("saving_pct", 0)
    if coverage_days >= 330:
        annual_kwh_saved = total_kwh
        annualization_method = "direct"
    else:
        # extrapolate
        daily_avg = total_kwh / coverage_days if coverage_days else 0
        annual_kwh_saved = daily_avg * 365
        annualization_method = "extrapolated_warning"
    # ------------------------------------------------------------------
    # 2️⃣ Energy cost & CO₂ saved (sử dụng giả định giá năng lượng)
    # ------------------------------------------------------------------
    tariff = opportunity.get("tariff_vnd_per_kwh", 0)
    annual_cost_saved = annual_kwh_saved * tariff
    # cỡ CO₂ tiết kiệm = kWh_saved * grid emission factor (kgCO2e/kWh) -> tCO2e
    grid_ef = ghg_assumptions.get("default_grid_ef_kgco2e_per_kwh", 0.6592)
    annual_co2_saved = annual_kwh_saved * grid_ef / 1000  # tCO2e
    # ------------------------------------------------------------------
    # 3️⃣ Cash‑flow projection (CAPEX, OPEX, savings) trong thời gian lifetime
    # ------------------------------------------------------------------
    lifetime = opportunity.get("lifetime_years") or financial_assumptions.get("default_lifetime_years", 10)
    discount_rate = financial_assumptions.get("discount_rate", 0.10)
    energy_escal = financial_assumptions.get("energy_escalation_rate", 0.03)
    capex = opportunity.get("capex_vnd", 0)
    # OPEX có thể được ghi đè per‑opportunity, nếu không thì % của CAPEX
    annual_opex = opportunity.get("annual_opex_vnd")
    if annual_opex is None:
        annual_opex = capex * financial_assumptions.get("default_opex_pct_of_capex", 0.02)
    # Cash‑flow list (năm 0 là CAPEX, các năm 1..lifetime là net savings)
    cash_flows = [-capex]
    for year in range(1, int(lifetime) + 1):
        # Escalate energy cost savings each year
        escalated_saving = annual_cost_saved * ((1 + energy_escal) ** (year - 1))
        net = escalated_saving - annual_opex
        cash_flows.append(net)
    # ------------------------------------------------------------------
    # 4️⃣ Tính NPV, IRR, Discounted Payback, SIR, MAC
    # ------------------------------------------------------------------
    npv = sum(cf / ((1 + discount_rate) ** i) for i, cf in enumerate(cash_flows))
    # IRR via bisection on discount rate r where NPV(r)=0
    def npv_at(r):
        return sum(cf / ((1 + r) ** i) for i, cf in enumerate(cash_flows))
    irr = None
    # kiểm tra có dấu chuyển đổi không
    if cash_flows[0] < 0 and any(cf > 0 for cf in cash_flows[1:]):
        low, high = -0.99, 1.0  # r trong (-0.99, 1)
        for _ in range(60):  # đủ độ chính xác 2**-60 ~ 1e-18
            mid = (low + high) / 2
            val = npv_at(mid)
            if val > 0:
                low = mid
            else:
                high = mid
        irr = (low + high) / 2
    # Simple payback (không chiết khấu)
    cumulative = -capex
    simple_payback = None
    for i, cf in enumerate(cash_flows[1:], start=1):
        cumulative += cf
        if cumulative >= 0:
            simple_payback = i
            break
    # Discounted payback
    cumulative_disc = -capex
    discounted_payback = None
    for i, cf in enumerate(cash_flows[1:], start=1):
        cumulative_disc += cf / ((1 + discount_rate) ** i)
        if cumulative_disc >= 0:
            discounted_payback = i
            break
    # SIR = (PV of benefits) / (PV of costs) = (NPV + CAPEX) / CAPEX
    sir = (npv + capex) / capex if capex != 0 else None
    # MAC = (CAPEX - NPV) / total CO2 saved (tCO2e)
    mac = (capex - npv) / annual_co2_saved if annual_co2_saved != 0 else None
    # ------------------------------------------------------------------
    # 5️⃣ GHG Scope calculations (sử dụng dữ liệu nguyên liệu, kWh)
    # ------------------------------------------------------------------
    # Scope 1: dựa trên boiler_fuel_kg * fuel_ef (được truyền trong opportunity)
    fuel_kg = opportunity.get("boiler_fuel_kg", 0)
    fuel_type = opportunity.get("energy_carrier") or "electricity"
    fuel_ef = 0.0
    # Nếu fuel_type là electricity, không có Scope 1; nếu là fuel, lấy từ mapping trong mapping.py
    if fuel_type != "electricity":
        from mapping import fuel_properties
        ef = fuel_properties.get(fuel_type, {}).get("co2_kg_per_gj", 0)
        ncv = fuel_properties.get(fuel_type, {}).get("ncv_gj_per_kg", 0)
        fuel_ef = ef * ncv  # kgCO2e per kg fuel
    scope1 = fuel_kg * fuel_ef / 1000  # tCO2e
    # Scope 2 location‑based
    total_kwh_used = opportunity.get("kwh_total", 0)
    scope2_loc = total_kwh_used * grid_ef / 1000  # tCO2e
    # Scope 2 market‑based – áp dụng renewable share và I‑REC
    renewable_share = ghg_assumptions.get("renewable_electricity_share_pct", 0.0) / 100
    irec_kwh = ghg_assumptions.get("irec_kwh", 0.0)
    # Điện tái tạo được trừ khỏi grid factor
    market_factor = grid_ef * (1 - renewable_share)
    scope2_mkt = (total_kwh_used - irec_kwh) * market_factor / 1000
    total_emissions = scope1 + scope2_loc + scope2_mkt
    # ------------------------------------------------------------------
    # 6️⃣ Đóng gói kết quả
    # ------------------------------------------------------------------
    result = {
        "annual_kwh_saved": annual_kwh_saved,
        "annual_cost_saved_vnd": annual_cost_saved,
        "annual_co2_saved_t": annual_co2_saved,
        "annualization_method": annualization_method,
        "simple_payback_years": simple_payback,
        "discounted_payback_years": discounted_payback,
        "npv_vnd": npv,
        "irr_pct": irr * 100 if irr is not None else None,
        "sir": sir,
        "mac_vnd_per_tco2e": mac,
        # GHG
        "ghg_scope1_tco2e": scope1,
        "ghg_scope2_location_tco2e": scope2_loc,
        "ghg_scope2_market_tco2e": scope2_mkt,
        "total_emissions_tco2e": total_emissions,
    }
    return {**opportunity, **result}

def calculate_sample_opportunities(
    df: pd.DataFrame,
    opportunities: Sequence[Mapping[str, object]],
    tariff: float | Mapping[str, float],
    emission_factor: float | None,
) -> list[dict[str, object]]:
    """Tính cơ hội mẫu theo điện tiêu thụ quy đổi năm.

    Được thiết kế để chịu lỗi khi:
    • Không có cột ``timestamp``.
    • ``applies_to`` không tồn tại trong DataFrame.
    • Không có ngày hợp lệ (``observed_days == 0``).
    • ``row_rates`` tồn tại nhưng ``days`` là ``None``.
    """
    # Chuẩn hoá các hàng đo lường và giữ thông tin chất lượng (không thay đổi nội dung hiện tại)
    df, _ = resolve_measurement_rows(df)

    # 1️⃣ Xác định ngày báo cáo – nếu không có cột timestamp thì để None
    days = (
        reporting_timestamps(df["timestamp"]).dt.normalize()
        if "timestamp" in df.columns
        else None
    )

    # 2️⃣ Giá điện theo khung tarif – chỉ tính khi tariff là mapping và có dữ liệu thời gian hợp lệ
    row_rates = (
        _row_prices(_tariff_periods(df), tariff)
        if isinstance(tariff, Mapping)
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
        # ------------------------------------------------------------
        # Kiểm tra cột ``applies_to`` tồn tại
        # ------------------------------------------------------------
        if applies_to not in df.columns:
            annualization_note = f"Không tìm thấy cột {applies_to} trong dữ liệu."
        # Kiểm tra có ngày đo lường để quy đổi
        elif days is None:
            annualization_note = "Cần thời gian đo để quy đổi tiêu thụ năm."
        else:
            consumption = finite_readings(df[applies_to])
            # Chỉ giữ các dòng có ngày hợp lệ và tiêu thụ >= 0
            valid_rows = days.notna() & consumption.ge(0)
            observed_days = int(days.loc[valid_rows].nunique())
            if observed_days:
                # Tính tiêu thụ trung bình trên năm
                base_kwh_annual = float(
                    consumption.loc[valid_rows].sum() / observed_days * 365
                )
                annualization_note = (
                    f"Quy đổi bình quân {observed_days} ngày có số đo lên 365 ngày, "
                    "giả định số đo trong từng ngày đầy đủ và các ngày đại diện cho cả năm."
                )
            else:
                annualization_note = "Không có số đo kèm ngày hợp lệ để ước tính mức tiêu thụ năm."

        # ------------------------------------------------------------
        # Tính giá điện (tariff) – an toàn khi days None
        # ------------------------------------------------------------
        price = tariff
        if row_rates is not None and days is not None:
            # ``_reading`` trả về Series; .where(days.notna()) bảo an
            price = _weighted_price(_reading(df, applies_to).where(days.notna()), row_rates)

        # ------------------------------------------------------------
        # Xây dựng kết quả cho cơ hội
        # ------------------------------------------------------------
        result = opportunity(
            name=name,
            base_kwh=base_kwh_annual,
            saving_pct=saving_pct,
            tariff=price,
            capex=capex,
            emission_factor=emission_factor,
        )
        # Thêm các trường bổ sung để UI hiển thị
        result["applies_to"] = applies_to
        result["base_kwh_annual"] = base_kwh_annual
        result["observed_days"] = observed_days
        result["assumption"] += f" {annualization_note}"
        results.append(result)
    return results
