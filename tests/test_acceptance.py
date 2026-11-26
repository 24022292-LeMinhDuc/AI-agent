"""Acceptance tests cho mapping, pipeline và kiểm soát schema."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import pandas as pd
import pytest
import yaml

import db
from agent import run_tool
from mapping import apply_mapping, suggest_mapping
from report import build_report_data
from tools import clean, enpi_regression, list_opportunities, seu_pareto, total_kwh


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = PROJECT_ROOT / "data"

EXPECTED_FIELDS = {
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


@pytest.mark.parametrize("sample_name", ["sample_a", "sample_b", "sample_c"])
def test_acceptance_maps_every_expanded_sample_column(sample_name: str) -> None:
    """Ánh xạ đầy đủ các nhóm dữ liệu mẫu bằng từ khóa."""
    source = pd.read_csv(DATA_DIR / f"{sample_name}.csv", encoding="utf-8-sig")
    assert suggest_mapping(source, use_ai=False) == EXPECTED_FIELDS


def test_acceptance_full_year_pipeline_finishes_under_ten_seconds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Chạy clean, SEU, EnPI và cơ hội trên dữ liệu ngày trong giới hạn."""
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    started_at = time.perf_counter()
    raw_df = pd.read_csv(DATA_DIR / "sample_a.csv", encoding="utf-8-sig")
    mapping = suggest_mapping(raw_df)
    mapped_df = apply_mapping(raw_df, mapping, {})
    kwh_cols = [column for column in mapped_df.columns if column.startswith("kwh_")]
    cleaned_df, quality = clean(mapped_df, kwh_cols)
    seu = seu_pareto(cleaned_df, kwh_cols, 0.8)
    enpi_df = cleaned_df.copy()
    enpi_df["kwh_total"] = total_kwh(enpi_df, kwh_cols)
    enpi = enpi_regression(enpi_df, "kwh_total", ["production_t", "cdd"])
    config = yaml.safe_load((PROJECT_ROOT / "config.yaml").read_text(encoding="utf-8"))
    opportunities = list_opportunities(
        cleaned_df,
        config["opportunities"],
        config["tariff_vnd_per_kwh"],
        config["emission_factor_kg_per_kwh"],
    )
    elapsed = time.perf_counter() - started_at

    assert len(cleaned_df) == 365
    assert quality["kwh_chiller"]["removed"] == 0
    assert "kwh_total" not in seu.index
    assert not seu.empty
    assert enpi["coefficients"]
    assert len(opportunities) == 3
    assert elapsed < 10.0


def test_expanded_building_sample_reaches_baseline_and_report(tmp_path: Path, monkeypatch) -> None:
    """Điện tổng, chỉ số tòa nhà và biến EnB đi qua luồng công cụ đã ghi vết."""
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "runs.db")
    source = pd.read_csv(DATA_DIR / "sample_b.csv", encoding="utf-8-sig")
    measured = apply_mapping(source, suggest_mapping(source, use_ai=False), {})
    store = {"df": measured, "mapping_confirmed": True, "config": {"tariff_vnd_per_kwh": 2500}}
    summary = run_tool("dataset_summary", {}, store)["result"]
    assert summary["total_kwh"] == pytest.approx(source["Tong_dien_tieu_thu_kWh"].sum())
    assert summary["total_source"] == "metered_total"
    assert summary["reported_audit"]["building_eui"]["kwh_per_m2"] == pytest.approx(
        summary["total_kwh"] / 20000
    )
    assert summary["reported_audit"]["scope2_tco2e"]["rows"] == 365

    args = {
        "baseline_start": "2025-11-01", "baseline_end": "2026-07-31",
        "reporting_start": "2026-08-01", "reporting_end": "2026-10-31",
        "energy_col": "kwh_total", "drivers": ["cdd", "operating_hours", "occupancy_pct"],
    }
    response = run_tool("energy_baseline", args, store)
    result = response["result"]
    assert result["meter_columns"] == ["kwh_total"]
    assert result["model"]["drivers"] == args["drivers"]
    assert result["reporting"]["eligible_rows"] > 0
    store["baseline_args"] = args
    store["analysis_results"] = {"energy_baseline": response}
    report = build_report_data(store)
    assert report["energy_baseline"]["run_id"] == response["run_id"]
    assert report["reported_audit"]["building_eui"] == summary["reported_audit"]["building_eui"]


@pytest.mark.parametrize(
    ("name", "args"),
    [
        ("data_quality", {"extra": True}),
        ("seu_analysis", {"threshold": "0.8"}),
        ("compare_periods", {"month_a": "2025-1", "month_b": "2026-01"}),
        (
            "dashboard_data",
            {"level": "day", "start_date": "2025-1-1", "end_date": "2025-01-31"},
        ),
    ],
)
def test_acceptance_bad_schema_is_rejected_without_a_run(
    name: str,
    args: dict[str, Any],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tham số sai schema bị từ chối trước khi tạo bản ghi SQLite."""
    db_path = tmp_path / "runs.db"
    monkeypatch.setattr(db, "DB_PATH", db_path)
    store = {"df": pd.DataFrame(), "config": {}}

    response = run_tool(name, args, store)

    assert "error" in response
    assert not db_path.exists()
