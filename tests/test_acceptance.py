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
from tools import clean, enpi_regression, list_opportunities, seu_pareto, total_kwh


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = PROJECT_ROOT / "data"

EXPECTED_MAPPINGS: dict[str, dict[str, str]] = {
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


def test_acceptance_mapping_reaches_ninety_percent_on_all_samples(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fallback từ khóa ánh xạ chính xác ít nhất chín phần mười cột."""
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    for sample_name, expected in EXPECTED_MAPPINGS.items():
        df = pd.read_csv(DATA_DIR / f"{sample_name}.csv", encoding="utf-8-sig")
        actual = suggest_mapping(df)
        accuracy = sum(actual.get(column) == target for column, target in expected.items()) / len(expected)
        assert accuracy >= 0.9


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
    assert quality["kwh_chiller"]["removed"] == 5
    assert not seu.empty
    assert enpi["coefficients"]
    assert len(opportunities) == 3
    assert elapsed < 10.0


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
