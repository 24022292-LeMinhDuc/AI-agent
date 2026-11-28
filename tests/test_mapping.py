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


def _legacy_headers(name: str) -> pd.DataFrame:
    """Giữ ca hồi quy cho các biến thể tên cột của CSV phiên bản trước."""
    return pd.DataFrame({column: ["giá trị nguồn"] for column in EXPECTED_MAPPINGS[name]})


@pytest.mark.parametrize("sample_name", ["sample_a", "sample_b", "sample_c"])
def test_suggest_mapping_uses_mocked_gemini_with_only_column_names(
    sample_name: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Chỉ gửi tên cột khi gọi Gemini."""
    df = _legacy_headers(sample_name)
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

    assert suggest_mapping(_legacy_headers(sample_name)) == EXPECTED_MAPPINGS[sample_name]


def test_keyword_fallback_is_used_when_gemini_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Kiểm tra lỗi gọi Gemini chuyển sang ánh xạ từ khóa."""
    class FailingModels:
        def generate_content(self, **kwargs: Any) -> None:
            raise RuntimeError("mock Gemini failure")

    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    monkeypatch.setattr(mapping_module, "create_client", lambda: SimpleNamespace(models=FailingModels()))

    assert suggest_mapping(_legacy_headers("sample_b")) == EXPECTED_MAPPINGS["sample_b"]


def test_warns_about_legacy_chiller_unit_scale() -> None:
    """Kiểm tra cột chiller MWh được phát hiện qua độ lớn số đo."""
    df = pd.DataFrame({"Chiller_kWh": [1.8], "Prod_kWh": [560], "Light_kWh": [420]})
    warnings = detect_unit_issues(df, suggest_mapping(df, use_ai=False))

    assert any(
        warning["column"] == "Chiller_kWh" and "đơn vị" in warning["warning"]
        for warning in warnings
    )


@pytest.mark.parametrize(("unit", "factor"), [("Wh", 0.001), ("MWh", 1000), ("GWh", 1_000_000)])
def test_declared_energy_unit_requires_correct_conversion(unit, factor):
    column = f"Chiller electricity {unit}"
    raw = pd.DataFrame({"Ngay": ["2026-01-01"], column: [1500]})
    mapping = suggest_mapping(raw, use_ai=False)
    assert mapping[column] == "kwh_chiller"
    issue, = detect_unit_issues(raw, mapping)
    assert (issue["unit"], issue["factor"]) == (unit, factor)
    with pytest.raises(ValueError, match=unit):
        apply_mapping(raw, mapping, {})
    assert apply_mapping(raw, mapping, {column: factor}).loc[0, "kwh_chiller"] == 1500 * factor


def test_apply_mapping_renames_scales_drops_noise_and_adds_cdd() -> None:
    """Kiểm tra chuẩn hóa tên cột, hệ số đã xác nhận và cột CDD."""
    df = pd.DataFrame({
        "date": ["2025-01-01"], "output_tons": [20], "ambient_C": [27.5],
        "Chiller_kWh": [2.1], "Prod_kWh": [700], "AirComp_kWh": [150],
        "Light_kWh": [90], "Ghi chú": ["Dữ liệu nhập"],
    })
    source_chiller = df["Chiller_kWh"].iloc[0]

    result = apply_mapping(
        df,
        EXPECTED_MAPPINGS["sample_c"],
        {"Chiller_kWh": 1000.0},
    )

    assert result.columns.tolist() == [
        "timestamp", "production_t", "temp_c", "kwh_production", "kwh_chiller",
        "kwh_compressed_air", "kwh_lighting", "cdd",
    ]
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


@pytest.mark.parametrize("sample", ["sample_a", "sample_b", "sample_c"])
def test_enriched_sample_preserves_every_source_column(sample: str) -> None:
    """Các CSV mới ánh xạ đủ cột và giữ riêng tổng kWh với phụ tải."""
    raw = pd.read_csv(DATA_DIR / f"{sample}.csv", encoding="utf-8-sig")
    mapping = suggest_mapping(raw, use_ai=False)
    assert set(mapping.values()) == set(CANONICAL_COLUMNS) - {"occupancy_people", "occupancy_pct"}
    assert len(mapping) == 31
    mapped = apply_mapping(raw, mapping, {})
    assert all(target in mapped.columns for target in mapping.values())
    assert mapping["Tong_dien_tieu_thu_kWh"] == "kwh_total"
    assert mapping["Cong_suat_cuc_dai_kW"] == "peak_demand_kw"
    assert validate_mapped_data(mapped) == []


def test_extended_mapping_keeps_context_and_validates_optional_values() -> None:
    raw = pd.DataFrame({
        "Ngay": ["2025-01-01", "2025-01-02", "2025-01-03"],
        "Loai_co_so": ["Nhà máy", "Toa_nha", "Bối cảnh chưa phân loại"],
        "Khung_gio": ["Cao điểm", "Bình thường", "Thap_diem"],
        "Trang_thai_van_hanh": ["Hoạt động", "Bảo trì", "Lễ tết"],
        "So_gio_van_hanh": [12, 24, 27],
        "So_nguoi_hoac_ty_le_lap_day_pct": [234, 84, 115],
        "Don_vi_lap_day": ["người", "%", "Pct"],
        "Nhiet_do_ngoai_troi_C": [-4, 28, 19],
        "Do_am_ngoai_troi_pct": ["72%", 100, -1],
        "He_so_cong_suat_cos_phi": [0.91, 1, 1.1],
        "Tong_dien_tieu_thu_kWh": [700, 860, 910],
        "Cong_suat_cuc_dai_kW": [88, 97, 110],
        "Loai_nhien_lieu": ["Khí tự nhiên", "Dien_luoi", "LPG"],
        "Phat_thai_CO2_Scope1_tCO2e": [0.22, 0, 0.15],
        "Ghi_chu_bat_thuong": ["Bảo trì tuần", None, "Tăng đột biến"],
    })
    result = apply_mapping(raw, suggest_mapping(raw, use_ai=False), {})

    assert result["facility_type"].tolist() == ["Nha_may", "Toa_nha", "Bối cảnh chưa phân loại"]
    assert result["tariff_period"].tolist() == ["Cao_diem", "Binh_thuong", "Thap_diem"]
    assert result["operating_status"].tolist() == ["Hoat_dong", "Bao_tri", "Le_tet"]
    assert result["fuel_type"].tolist() == ["Khi_tu_nhien", "Dien_luoi", "LPG"]
    assert result.loc[0, "occupancy_people"] == 234
    assert result.loc[1, "occupancy_pct"] == 84
    assert pd.isna(result.loc[2, "occupancy_pct"])
    assert result.loc[2, "occupancy_value"] == 115
    assert result.loc[0, "temp_c"] == -4
    assert result.loc[0, "outdoor_humidity_pct"] == 72
    assert result.loc[1, "scope1_tco2e"] == 0
    assert result.loc[2, "peak_demand_kw"] == 110
    assert result.loc[2, "anomaly_note"] == "Tăng đột biến"
    assert result.attrs["mapping_issues"] == {
        "facility_type": 1, "operating_hours": 1, "outdoor_humidity_pct": 1,
        "occupancy_pct": 1, "power_factor": 1,
    }


def test_does_not_infer_people_or_percent_from_facility_type() -> None:
    raw = pd.DataFrame({
        "Loai_co_so": ["Nha_may", "Toa_nha"],
        "So_nguoi_hoac_ty_le_lap_day_pct": [42, 88],
    })
    mapped = apply_mapping(raw, suggest_mapping(raw, use_ai=False), {})
    assert mapped["occupancy_value"].tolist() == [42, 88]
    assert "occupancy_pct" not in mapped.columns
    assert "occupancy_people" not in mapped.columns


def test_measured_total_alone_passes_validation_and_peak_require_kw() -> None:
    raw = pd.DataFrame({"Ngày": ["2025-12-01"], "Tổng điện (kWh)": [650], "Peak demand (kW)": [42]})
    mapping = suggest_mapping(raw, use_ai=False)
    assert mapping == {
        "Ngày": "timestamp", "Tổng điện (kWh)": "kwh_total", "Peak demand (kW)": "peak_demand_kw",
    }
    assert validate_mapped_data(apply_mapping(raw, mapping, {})) == []
    with pytest.raises(ValueError, match="công suất cực đại theo kW"):
        apply_mapping(raw, {"Tổng điện (kWh)": "peak_demand_kw"}, {})
