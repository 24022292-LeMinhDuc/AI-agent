"""Kiểm tra gợi ý, cảnh báo đơn vị và áp dụng ánh xạ cột."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pandas as pd
import pytest

import mapping as mapping_module
from mapping import (
    CANONICAL_COLUMNS,
    apply_mapping,
    detect_unit_issues,
    suggest_mapping,
    validate_mapped_data,
)


DATA_DIR = Path(__file__).resolve().parents[1] / "data"
EXPECTED_MAPPINGS = {
    "sample_a": {
        "Ngày": "timestamp",
        "Sản lượng (tấn)": "production_t",
        "Nhiệt độ ngoài trời": "temp_c",
        "Điện sản xuất (kWh)": "kwh_production",
        "Điện chiller (kWh)": "kwh_chiller",
        "Điện khí nén (kWh)": "kwh_compressed_air",
        "Điện chiếu sáng (kWh)": "kwh_lighting",
    },
    "sample_b": {
        "date": "timestamp",
        "output_tons": "production_t",
        "ambient_C": "temp_c",
        "Prod_kWh": "kwh_production",
        "Chiller_kWh": "kwh_chiller",
        "AirComp_kWh": "kwh_compressed_air",
        "Light_kWh": "kwh_lighting",
    },
    "sample_c": {
        "date": "timestamp",
        "output_tons": "production_t",
        "ambient_C": "temp_c",
        "Prod_kWh": "kwh_production",
        "Chiller_kWh": "kwh_chiller",
        "AirComp_kWh": "kwh_compressed_air",
        "Light_kWh": "kwh_lighting",
        "Ghi chú": "unmapped",
    },
}


def _read_sample(name: str) -> pd.DataFrame:
    """Đọc một file CSV mẫu."""
    return pd.read_csv(DATA_DIR / f"{name}.csv", encoding="utf-8-sig")


@pytest.mark.parametrize("sample_name", ["sample_a", "sample_b", "sample_c"])
def test_suggest_mapping_uses_mocked_gemini_with_only_column_names(
    sample_name: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Chỉ gửi tên cột khi gọi Gemini."""
    df = _read_sample(sample_name)
    expected = EXPECTED_MAPPINGS[sample_name]
    requests: list[dict[str, Any]] = []

    class FakeModels:
        def generate_content(self, **kwargs: Any) -> SimpleNamespace:
            requests.append(kwargs)
            return SimpleNamespace(text=json.dumps(expected, ensure_ascii=False))

    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    monkeypatch.setenv("GEMINI_MODEL", "test-model")
    monkeypatch.setattr(mapping_module, "create_client", lambda: SimpleNamespace(models=FakeModels()))

    result = suggest_mapping(df)
    assert result == expected
    assert len(requests) == 1
    request = requests[0]
    assert request["model"] == "test-model"
    sent_data = json.loads(request["contents"].split("DATA_JSON:\n", maxsplit=1)[1])
    assert sent_data == {"column_names": df.columns.tolist()}
    assert request["config"].response_mime_type == "application/json"

    assert suggest_mapping(df, use_ai=False) == expected
    assert len(requests) == 1


@pytest.mark.parametrize("sample_name", ["sample_a", "sample_b", "sample_c"])
def test_keyword_fallback_maps_sample_files_without_api_key(
    sample_name: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Kiểm tra fallback từ khóa khi không có API key."""
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)

    assert suggest_mapping(_read_sample(sample_name)) == EXPECTED_MAPPINGS[sample_name]


def test_keyword_fallback_is_used_when_gemini_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Kiểm tra lỗi gọi Gemini chuyển sang ánh xạ từ khóa."""
    class FailingModels:
        def generate_content(self, **kwargs: Any) -> None:
            raise RuntimeError("mock Gemini failure")

    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    monkeypatch.setattr(mapping_module, "create_client", lambda: SimpleNamespace(models=FailingModels()))

    assert suggest_mapping(_read_sample("sample_b")) == EXPECTED_MAPPINGS["sample_b"]


def test_sample_c_warns_about_chiller_unit_scale() -> None:
    """Kiểm tra cột chiller MWh được phát hiện qua độ lớn số đo."""
    df = _read_sample("sample_c")

    warnings = detect_unit_issues(df, EXPECTED_MAPPINGS["sample_c"])

    assert any(
        warning["column"] == "Chiller_kWh" and "đơn vị" in warning["warning"]
        for warning in warnings
    )


def test_apply_mapping_renames_scales_drops_noise_and_adds_cdd() -> None:
    """Kiểm tra chuẩn hóa tên cột, hệ số đã xác nhận và cột CDD."""
    df = _read_sample("sample_c")
    source_chiller = df["Chiller_kWh"].iloc[0]

    result = apply_mapping(
        df,
        EXPECTED_MAPPINGS["sample_c"],
        {"Chiller_kWh": 1000.0},
    )

    assert result.columns.tolist() == [*CANONICAL_COLUMNS, "cdd"]
    assert "Ghi chú" not in result.columns
    assert result["kwh_chiller"].iloc[0] == source_chiller * 1000
    assert result["cdd"].iloc[0] == max(df["ambient_C"].iloc[0] - 24, 0)


def test_validate_mapped_data_reports_missing_analysis_inputs() -> None:
    """Thiếu ngày hoặc cột điện được báo trước khi xác nhận dataset."""
    no_timestamp = pd.DataFrame({"kwh_lighting": [100.0]})
    no_energy = pd.DataFrame({"timestamp": ["2025-01-01"], "production_t": [1.0]})

    assert "Cần ánh xạ một cột sang timestamp." in validate_mapped_data(no_timestamp)
    assert any("ít nhất một cột điện" in error for error in validate_mapped_data(no_energy))
    empty_errors = validate_mapped_data(pd.DataFrame())
    assert empty_errors[0] == "File không có dòng dữ liệu sau khi áp dụng mapping."
    assert any("timestamp" in error for error in empty_errors)


def test_apply_mapping_normalizes_local_calendar_and_finite_readings() -> None:
    raw = pd.DataFrame(
        {
            "date": ["2025-01-31T20:00:00Z", "02/02/2025 09:30", 45700, pd.Timestamp("2025-02-04")],
            "Chiller (kW h)": ["30", "Inf", "60", "40"],
        }
    )
    mapped = apply_mapping(raw, suggest_mapping(raw, use_ai=False), {})

    assert mapped.loc[0, "timestamp"] == pd.Timestamp("2025-02-01 03:00")
    assert mapped.loc[1, "timestamp"] == pd.Timestamp("2025-02-02 09:30")
    assert pd.isna(mapped.loc[2, "timestamp"])
    assert mapped.loc[3, "timestamp"] == pd.Timestamp("2025-02-04")
    assert mapped.loc[0, "kwh_chiller"] == 30.0
    assert pd.isna(mapped.loc[1, "kwh_chiller"])
    assert validate_mapped_data(mapped) == []
    assert validate_mapped_data(mapped.iloc[[1, 2]])


def test_mapping_distinguishes_energy_and_power_units() -> None:
    power = pd.DataFrame({"Chiller (kW)": [2]})
    assert suggest_mapping(power, use_ai=False) == {"Chiller (kW)": "unmapped"}
    with pytest.raises(ValueError, match="công suất"):
        apply_mapping(power, {"Chiller (kW)": "kwh_chiller"}, {})

    energy = pd.DataFrame({"Chiller (MWh)": [2]})
    mapping = suggest_mapping(energy, use_ai=False)
    with pytest.raises(ValueError, match="MWh"):
        apply_mapping(energy, mapping, {})
    with pytest.raises(ValueError, match="hữu hạn"):
        apply_mapping(energy, mapping, {"Chiller (MWh)": float("inf")})
    assert apply_mapping(energy, mapping, {"Chiller (MWh)": 1000}).loc[0, "kwh_chiller"] == 2000
