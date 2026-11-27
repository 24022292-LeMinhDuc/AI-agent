"""Gợi ý ánh xạ cột và chuẩn hóa bảng dữ liệu."""

from __future__ import annotations

import json
import os
import re
from collections.abc import Mapping, Sequence
from math import isfinite
from pathlib import Path

import jsonschema
import pandas as pd
import yaml
from google.genai import types

from gemini_service import create_client, generate_content
from tools import finite_readings, normalize_label as _normalize_text, reporting_timestamps


_SCHEMA_HEADERS = {
    "Ngay": "timestamp",
    "Loai_co_so": "facility_type",
    "Khung_gio": "tariff_period",
    "Trang_thai_van_hanh": "operating_status",
    "So_gio_van_hanh": "operating_hours",
    "San_luong_tan": "production_t",
    "Dien_tich_san_m2": "floor_area_m2",
    "So_nguoi_hoac_ty_le_lap_day_pct": "occupancy_value",
    "Don_vi_lap_day": "occupancy_unit",
    "Nhiet_do_ngoai_troi_C": "temp_c",
    "Do_am_ngoai_troi_pct": "outdoor_humidity_pct",
    "Dien_san_xuat_kWh": "kwh_production",
    "Dien_chiller_kWh": "kwh_chiller",
    "Dien_khi_nen_kWh": "kwh_compressed_air",
    "Dien_chieu_sang_kWh": "kwh_lighting",
    "Dien_phu_tro_khac_kWh": "kwh_other",
    "Tong_dien_tieu_thu_kWh": "kwh_total",
    "He_so_cong_suat_cos_phi": "power_factor",
    "Cong_suat_cuc_dai_kW": "peak_demand_kw",
    "Nhien_lieu_lo_hoi_kg": "boiler_fuel_kg",
    "Loai_nhien_lieu": "fuel_type",
    "San_luong_hoi_tan": "steam_t",
    "Tai_lanh_chiller_RTh": "cooling_load_rth",
    "SEC_hoac_EUI_kWh_don_vi": "sec_eui_kwh_per_unit",
    "EnB_chuan_kWh_don_vi": "baseline_kwh_per_unit",
    "Chi_phi_nang_luong_VND": "energy_cost_vnd",
    "He_so_phat_thai_dien_kgCO2_kWh": "grid_emission_factor_kgco2e_per_kwh",
    "Phat_thai_CO2_Scope1_tCO2e": "scope1_tco2e",
    "Phat_thai_CO2_Scope2_tCO2e": "scope2_tco2e",
    "Tong_phat_thai_CO2_tCO2e": "total_emissions_tco2e",
    "Ghi_chu_bat_thuong": "anomaly_note",
}
CANONICAL_COLUMNS = (*_SCHEMA_HEADERS.values(), "occupancy_people", "occupancy_pct")
UNMAPPED = "unmapped"
_CATEGORIES = {
    "facility_type": ("Nha_may", "Toa_nha"),
    "tariff_period": ("Cao_diem", "Binh_thuong", "Thap_diem"),
    "operating_status": ("Hoat_dong", "Nghi_tuan", "Bao_tri", "Le_tet"),
    "occupancy_unit": ("Nguoi", "Pct"),
    "fuel_type": ("Dien_luoi", "Than_da", "Biomass", "DO", "FO", "LPG", "Khi_tu_nhien"),
}
_BOUNDED_FIELDS = {
    "operating_hours": 24,
    "outdoor_humidity_pct": 100,
    "occupancy_pct": 100,
    "power_factor": 1,
}


def _unit_words(normalized_name: str) -> set[str]:
    """Nhận diện cách ghi như kWh, kW h và kW-hour."""
    normalized_name = re.sub(r"\b([kmg]?w)\s+(?:h|hours?)\b", r"\1h", normalized_name)
    return set(normalized_name.split())


_HEADER_LOOKUP = {
    _normalize_text(header): target for header, target in _SCHEMA_HEADERS.items()
}
_HEADER_LOOKUP.update({_normalize_text(column): column for column in CANONICAL_COLUMNS})
_HEADER_LOOKUP.update({
    "so nguoi": "occupancy_people",
    "so nguoi hien dien": "occupancy_people",
    "occupant count": "occupancy_people",
    "occupancy count": "occupancy_people",
    "ty le lap day pct": "occupancy_pct",
    "occupancy rate pct": "occupancy_pct",
    "relative humidity pct": "outdoor_humidity_pct",
})
_CATEGORY_LOOKUP = {
    column: {_normalize_text(value): value for value in values}
    for column, values in _CATEGORIES.items()
}
_CATEGORY_LOOKUP["tariff_period"].update({"peak": "Cao_diem", "normal": "Binh_thuong", "off peak": "Thap_diem"})
_CATEGORY_LOOKUP["fuel_type"].update({
    "coal": "Than_da", "do oil": "DO", "fo oil": "FO", "cng": "Khi_tu_nhien",
})
_CATEGORY_LOOKUP["occupancy_unit"].update({
    "people": "Nguoi", "persons": "Nguoi",
    "percent": "Pct", "phan tram": "Pct",
})


def _keyword_mapping(columns: Sequence[object]) -> dict[str, str]:
    """Ánh xạ cột bằng từ khóa tiếng Việt và tiếng Anh."""
    mapping: dict[str, str] = {}
    for source in columns:
        name = str(source)
        normalized = _normalize_text(name)
        words = _unit_words(normalized)
        target = UNMAPPED

        if normalized in _HEADER_LOOKUP:
            mapping[name] = _HEADER_LOOKUP[normalized]
            continue
        if "%" in name and normalized in {"ty le lap day", "occupancy rate"}:
            mapping[name] = "occupancy_pct"
            continue
        if words & {"kw", "mw", "gw", "w"} and not words & {"kwh", "mwh"}:
            if "kw" in words and ("cuc dai" in normalized or "peak demand" in normalized):
                target = "peak_demand_kw"
            mapping[name] = target
            continue
        if words & {"date", "datetime", "timestamp", "time", "ngay"} or "thoi gian" in normalized:
            target = "timestamp"
        elif "do am" in normalized or "relative humidity" in normalized:
            target = "outdoor_humidity_pct"
        elif words & {"temp", "temperature", "ambient"} or "nhiet do" in normalized:
            target = "temp_c"
        else:
            is_energy = bool(words & {"kwh", "mwh", "electric", "electricity", "dien", "power"})
            if is_energy and words & {"total", "tong"}:
                target = "kwh_total"
            elif is_energy and ("phu tro" in normalized or words & {"auxiliary", "other"}):
                target = "kwh_other"
            elif is_energy and words & {"chiller", "chilling"}:
                target = "kwh_chiller"
            elif is_energy and (
                "aircomp" in words
                or "compressedair" in words
                or (
                    "air" in words
                    and bool(words & {"compressed", "compressor", "comp"})
                )
                or "khi nen" in normalized
            ):
                target = "kwh_compressed_air"
            elif is_energy and (
                words & {"light", "lighting", "illumination"}
                or "chieu sang" in normalized
            ):
                target = "kwh_lighting"
            elif is_energy and (
                words & {"prod", "production"}
                or "san xuat" in normalized
            ):
                target = "kwh_production"
            elif words & {"output", "tons", "ton", "production"} or "san luong" in normalized:
                target = "production_t"

        mapping[name] = target
    return mapping


def suggest_mapping(df: pd.DataFrame, *, use_ai: bool = True) -> dict[str, str]:
    """Gợi ý ánh xạ cột, kiểm JSON của Gemini và dự phòng bằng từ khóa."""
    columns = [str(column) for column in df.columns]
    fallback = _keyword_mapping(columns)
    if not use_ai or not os.getenv("GEMINI_API_KEY"):
        return fallback

    input_payload = {"column_names": columns}
    prompt = (
        "Ánh xạ từng cột nguồn vào đúng một tên chuẩn tương ứng. "
        "Chỉ trả về một đối tượng JSON thuần, không markdown hay giải thích. "
        "Điện tổng theo kWh là kwh_total, tách khỏi công tơ phụ. Công suất cực đại theo kW "
        "là peak_demand_kw. Các cột W/kW/MW khác không phải kWh. Tuyệt đối không suy đoán "
        "đơn vị người hoặc phần trăm nếu nhãn cột gộp cả hai. "
        f"Giá trị chỉ được thuộc danh sách {list(CANONICAL_COLUMNS) + [UNMAPPED]}.\n"
        "DATA_JSON:\n"
        + json.dumps(input_payload, ensure_ascii=False, default=str)
    )
    schema = {
        "type": "object",
        "properties": {
            column: {"type": "string", "enum": [*CANONICAL_COLUMNS, UNMAPPED]}
            for column in columns
        },
        "required": columns,
        "additionalProperties": False,
    }

    try:
        response = generate_content(
            create_client(),
            contents=prompt,
            config=types.GenerateContentConfig(
                max_output_tokens=1200,
                response_mime_type="application/json",
                response_json_schema=schema,
            ),
        )
        response_text = response.text or ""
        suggested = json.loads(response_text)
        jsonschema.validate(instance=suggested, schema=schema)
        return {
            column: _HEADER_LOOKUP.get(_normalize_text(column), suggested[column])
            for column in columns
        }
    except Exception:
        return fallback


def detect_unit_issues(
    df: pd.DataFrame, mapping: Mapping[str, str]
) -> list[dict[str, str]]:
    """Cảnh báo cột điện có tên MWh hoặc giá trị nhỏ bất thường."""
    electrical_columns = [
        (source, target)
        for source, target in mapping.items()
        if target.startswith("kwh_") and source in df.columns
    ]
    positive_means: dict[str, float] = {}
    for source, _ in electrical_columns:
        values = finite_readings(df[source])
        positive_values = values[values > 0]
        if not positive_values.empty:
            positive_means[source] = float(positive_values.mean())

    warnings: list[dict[str, str]] = []
    for source, target in electrical_columns:
        words = _unit_words(_normalize_text(source))
        reasons: list[str] = []
        if "mwh" in words:
            reasons.append("tên cột có đơn vị MWh")

        current_mean = positive_means.get(source)
        other_means = [
            value for other_source, value in positive_means.items()
            if other_source != source and mapping[other_source] != "kwh_total"
        ]
        if current_mean is not None and other_means:
            reference_mean = float(pd.Series(other_means).median())
            if current_mean > 0 and reference_mean / current_mean >= 100:
                reasons.append("giá trị trung bình thấp hơn các cột điện khác ít nhất 100 lần")

        if reasons:
            warnings.append(
                {
                    "column": str(source),
                    "canonical": target,
                    "warning": ". ".join(reasons)
                    + ". Hãy kiểm tra đơn vị trước khi xác nhận.",
                }
            )
    return warnings


def _load_cdd_base_temp() -> float:
    """Đọc nhiệt độ cơ sở CDD từ cấu hình dự án."""
    config_path = Path(__file__).with_name("config.yaml")
    with config_path.open("r", encoding="utf-8") as config_file:
        config = yaml.safe_load(config_file) or {}
    return float(config["cdd_base_temp_c"])


def validate_mapped_data(df: pd.DataFrame) -> list[str]:
    """Kiểm tra cột tối thiểu để phân tích lịch sử năng lượng."""
    errors: list[str] = []
    valid_dates = pd.Series(False, index=df.index)
    if "timestamp" not in df.columns:
        errors.append("Cần ánh xạ một cột sang timestamp.")
    else:
        valid_dates = reporting_timestamps(df["timestamp"]).notna()
        if not valid_dates.any():
            errors.append("Không tìm thấy giá trị thời gian hợp lệ trong timestamp.")

    kwh_columns = [column for column in df.columns if str(column).startswith("kwh_")]
    audit_columns = {"boiler_fuel_kg", "steam_t", "cooling_load_rth", "peak_demand_kw", "power_factor", "energy_cost_vnd"}
    measurements = [*kwh_columns, *audit_columns.intersection(df.columns)]
    if not measurements:
        errors.append("Cần ánh xạ ít nhất một cột điện kWh, nhiên liệu, hơi, tải lạnh, công suất, cos φ hoặc chi phí.")
    elif not any((valid_dates & finite_readings(df[column]).ge(0)).any() for column in measurements):
        errors.append("Cần ít nhất một số đo năng lượng hoặc vận hành hợp lệ đi kèm thời gian.")
    if df.empty:
        errors.insert(0, "File không có dòng dữ liệu sau khi áp dụng mapping.")
    return errors


def _normalize_category(text: pd.Series, column: str) -> tuple[pd.Series, int]:
    """Chuẩn hóa mục nhận diện được và giữ nhãn khác để đối chiếu."""
    value = text.replace("%", "Pct") if column == "occupancy_unit" else text
    known = value.map(_normalize_text, na_action="ignore").map(_CATEGORY_LOOKUP[column])
    return known.fillna(text).astype("string"), int((text.notna() & known.isna()).sum())


def _derive_occupancy(df: pd.DataFrame, issues: dict[str, int]) -> None:
    """Chỉ tạo biến số người hoặc phần trăm khi có đơn vị khai báo."""
    if not {"occupancy_value", "occupancy_unit"}.issubset(df.columns):
        return

    for column, unit in (("occupancy_people", "Nguoi"), ("occupancy_pct", "Pct")):
        has_unit = df["occupancy_unit"].eq(unit).fillna(False)
        if not has_unit.any():
            continue
        values = df["occupancy_value"].where(has_unit)
        if unit == "Pct":
            count = int(values.gt(100).sum())
            if count:
                issues[column] = issues.get(column, 0) + count
            values = values.where(values.le(100))
        df[column] = df[column].combine_first(values) if column in df else values


def apply_mapping(
    df: pd.DataFrame,
    mapping: Mapping[str, str],
    unit_factors: Mapping[str, float] | None,
) -> pd.DataFrame:
    """Đổi tên, áp dụng hệ số đã xác nhận và tính CDD."""
    if df.columns.has_duplicates:
        raise ValueError("Tên cột nguồn phải duy nhất để áp dụng ánh xạ.")
    factors = unit_factors or {}
    renamed: dict[object, str] = {}
    selected_columns: list[object] = []
    seen_targets: set[str] = set()

    for source in df.columns:
        target = mapping.get(str(source), UNMAPPED)
        if target == UNMAPPED:
            continue
        if target not in CANONICAL_COLUMNS:
            raise ValueError(f"Tên cột chuẩn không hợp lệ: {target}")
        if target in seen_targets:
            raise ValueError(f"Nhiều cột nguồn cùng ánh xạ vào {target}.")
        renamed[source] = target
        selected_columns.append(source)
        seen_targets.add(target)

    result = df.loc[:, selected_columns].copy().rename(columns=renamed)
    issues: dict[str, int] = {}
    for source, target in renamed.items():
        raw = result[target]
        if target == "timestamp":
            result[target] = reporting_timestamps(raw)
            continue
        text = raw.astype("string").str.strip().replace("", pd.NA)
        if target == "anomaly_note":
            result[target] = text
            continue
        if target in _CATEGORIES:
            result[target], count = _normalize_category(text, target)
            if count:
                issues[target] = count
            continue
        if target.startswith("kwh_"):
            words = _unit_words(_normalize_text(source))
            if words & {"kw", "mw", "gw", "w"} and not words & {"kwh", "mwh"}:
                raise ValueError(f"Cột {source} chứa công suất. Hãy cung cấp điện năng theo từng khoảng đo (kWh hoặc MWh).")
            factor = float(factors.get(str(source), factors.get(target, 1.0)))
            if not isfinite(factor) or factor <= 0:
                raise ValueError(f"Hệ số quy đổi của cột {source} phải là số hữu hạn dương.")
            if "mwh" in words and factor != 1000:
                raise ValueError(f"Cần xác nhận đổi cột {source} từ MWh sang kWh (×1.000).")
            result[target] = finite_readings(finite_readings(raw) * factor)
            continue

        if target == "peak_demand_kw":
            units = _unit_words(_normalize_text(source))
            if units & {"wh", "kwh", "mwh", "gwh", "w", "mw", "gw"}:
                raise ValueError(f"Cột {source} phải biểu diễn công suất cực đại theo kW.")
        if target in {"outdoor_humidity_pct", "occupancy_pct"}:
            raw = text.str.removesuffix("%")
        values = finite_readings(raw)
        if target in _BOUNDED_FIELDS:
            values = values.where(values.between(0, _BOUNDED_FIELDS[target]))
        elif target != "temp_c":
            values = values.where(values.ge(0))
        result[target] = values
        count = int((text.notna() & values.isna()).sum())
        if count:
            issues[target] = count

    _derive_occupancy(result, issues)

    ordered_columns = [column for column in CANONICAL_COLUMNS if column in result.columns]
    result = result.loc[:, ordered_columns]
    if "temp_c" in result.columns:
        base_temp = _load_cdd_base_temp()
        temperature = result["temp_c"]
        result["cdd"] = (temperature - base_temp).clip(lower=0)
    if issues:
        result.attrs["mapping_issues"] = issues
    return result
