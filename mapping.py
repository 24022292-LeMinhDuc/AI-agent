"""Gợi ý ánh xạ cột và chuẩn hóa bảng dữ liệu."""

from __future__ import annotations

import json
import os
import re
import unicodedata
from collections.abc import Mapping, Sequence
from math import isfinite
from pathlib import Path

import jsonschema
import pandas as pd
import yaml
from google.genai import types

from gemini_service import create_client, generate_content
from tools import finite_readings, reporting_timestamps


CANONICAL_COLUMNS = (
    "timestamp",
    "production_t",
    "temp_c",
    "kwh_production",
    "kwh_chiller",
    "kwh_compressed_air",
    "kwh_lighting",
)
UNMAPPED = "unmapped"


def _normalize_text(value: object) -> str:
    """Chuẩn hóa chữ thường và bỏ dấu tiếng Việt."""
    text = str(value).lower().replace("đ", "d").replace("Đ", "d")
    decomposed = unicodedata.normalize("NFD", text)
    without_marks = "".join(char for char in decomposed if unicodedata.category(char) != "Mn")
    return re.sub(r"[^a-z0-9]+", " ", without_marks).strip()


def _unit_words(normalized_name: str) -> set[str]:
    """Nhận diện cách ghi như kWh, kW h và kW-hour."""
    normalized_name = re.sub(r"\b([kmg]?w)\s+(?:h|hours?)\b", r"\1h", normalized_name)
    return set(normalized_name.split())


def _keyword_mapping(columns: Sequence[object]) -> dict[str, str]:
    """Ánh xạ cột bằng từ khóa tiếng Việt và tiếng Anh."""
    mapping: dict[str, str] = {}
    for source in columns:
        name = str(source)
        normalized = _normalize_text(name)
        words = _unit_words(normalized)
        target = UNMAPPED

        if words & {"kw", "mw", "gw", "w"} and not words & {"kwh", "mwh"}:
            mapping[name] = UNMAPPED
            continue
        if words & {"date", "datetime", "timestamp", "time", "ngay"} or "thoi gian" in normalized:
            target = "timestamp"
        elif words & {"temp", "temperature", "ambient"} or "nhiet do" in normalized:
            target = "temp_c"
        else:
            is_energy = bool(words & {"kwh", "mwh", "electric", "electricity", "dien", "power"})
            if is_energy and words & {"chiller", "chilling"}:
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
        "Giữ unmapped khi cột công suất ghi W/kW/MW hoặc tên không đủ để xác định. "
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
        return {column: suggested[column] for column in columns}
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
            value for other_source, value in positive_means.items() if other_source != source
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
    if not kwh_columns:
        errors.append("Cần ánh xạ ít nhất một cột điện sang tên bắt đầu bằng kwh_.")
    elif not any((valid_dates & finite_readings(df[column]).ge(0)).any() for column in kwh_columns):
        errors.append("Cần ít nhất một số đo điện không âm đi kèm giá trị thời gian hợp lệ.")
    if df.empty:
        errors.insert(0, "File không có dòng dữ liệu sau khi áp dụng mapping.")
    return errors


def apply_mapping(
    df: pd.DataFrame,
    mapping: Mapping[str, str],
    unit_factors: Mapping[str, float] | None,
) -> pd.DataFrame:
    """Đổi tên, áp dụng hệ số đã xác nhận và tính CDD."""
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
    for source, target in renamed.items():
        if target == "timestamp":
            result[target] = reporting_timestamps(result[target])
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
            result[target] = finite_readings(finite_readings(result[target]) * factor)
            continue
        result[target] = finite_readings(result[target])

    ordered_columns = [column for column in CANONICAL_COLUMNS if column in result.columns]
    result = result.loc[:, ordered_columns]
    if "temp_c" in result.columns:
        base_temp = _load_cdd_base_temp()
        temperature = result["temp_c"]
        result["cdd"] = (temperature - base_temp).clip(lower=0)
    return result
