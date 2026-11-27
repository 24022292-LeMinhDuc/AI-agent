"""Kiểm tra đường cơ sở từ công cụ đến hồ sơ bằng chứng."""

import json
from io import BytesIO
from pathlib import Path

import pandas as pd
import pytest
from docx import Document

import db
from agent import _model_result, run_tool
from report import build_report_data, render_docx, render_markdown, render_report_json


BASELINE_ARGS = {
    "baseline_start": "2026-01-01",
    "baseline_end": "2026-01-06",
    "reporting_start": "2026-01-07",
    "reporting_end": "2026-01-10",
    "energy_col": "kwh_total",
    "drivers": ["production_t"],
}


@pytest.mark.parametrize("has_empty_total_meter", [False, True])
def test_baseline_uses_measured_data_and_persists_report_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, has_empty_total_meter: bool,
) -> None:
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "runs.db")
    measured = pd.DataFrame({
        "timestamp": pd.date_range("2026-01-01", periods=10),
        "production_t": [1, 2, 3, 4, 5, 6, 2, 3, 4, 5],
        "kwh_chiller": [102, 104, 106, 108, 110, 112, 94, 96, None, 100],
        "kwh_lighting": [20] * 10,
    })
    if has_empty_total_meter:
        measured["kwh_total"] = float("nan")
    store = {
        "df": measured,
        "mapping_confirmed": True,
        "run_scope_id": "baseline-test",
        "config": {"tariff_vnd_per_kwh": 2500},
    }
    run_tool("data_quality", {}, store)
    cleaned = store["clean_df"]
    assert cleaned.loc[8, "kwh_chiller"] == pytest.approx(98)
    store["df"] = cleaned  # Giao diện giữ bản sạch để hiển thị.

    response = run_tool("energy_baseline", BASELINE_ARGS, store)
    result = response["result"]
    assert result.get("error") is None
    assert result["meter_columns"] == ["kwh_chiller", "kwh_lighting"]
    assert result["reporting"]["eligible_rows"] == 3
    assert result["summary"]["expected_kwh"] == pytest.approx(380)
    assert result["summary"]["actual_kwh"] == pytest.approx(350)
    assert result["summary"]["savings_kwh"] == pytest.approx(30)
    assert _model_result("energy_baseline", result)["reporting"]["eligible_rows"] == 3
    assert "monthly" not in _model_result("energy_baseline", result)
    assert db.get_run(response["run_id"], scope_id="different-session") is None

    store["baseline_args"] = BASELINE_ARGS
    store["analysis_results"] = {"energy_baseline": response}
    report_data = build_report_data(store)
    assert report_data["metadata"]["total_source"] == "submeter_sum"
    assert report_data["enpi_analysis"]["result"]["intensity_rows"] == 10
    assert report_data["enpi_analysis"]["result"]["kwh_per_ton"] > 0
    assert report_data["energy_baseline"]["run_id"] == response["run_id"]
    assert response["run_id"] in report_data["run_ids"]
    assert "Chênh lệch ước tính (kWh) | 30.00" in render_markdown(report_data, "Tổng hợp.")
    document = Document(BytesIO(render_docx(report_data, "Tổng hợp.")))
    assert "Đường cơ sở năng lượng và chênh lệch ước tính" in [p.text for p in document.paragraphs]
    assert '"energy_baseline"' in render_report_json(report_data)


def test_baseline_rejects_invalid_schema_without_execution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = tmp_path / "runs.db"
    monkeypatch.setattr(db, "DB_PATH", db_path)
    bad_args = {**BASELINE_ARGS, "drivers": ["cdd", "cdd"]}

    response = run_tool("energy_baseline", bad_args, {"mapping_confirmed": True})

    assert "drivers" in response["error"]
    assert not db_path.exists()


def test_baseline_records_mv_settings_and_adjustments_as_strict_json(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "runs.db")
    dates = pd.date_range("2026-01-01", periods=10)
    measured = pd.DataFrame({
        "timestamp": dates, "production_t": [1, 2, 3, 4, 5, 6, 2, 3, 4, 5],
        "kwh_total": [102, 104, 106, 108, 110, 112, 94, 96, 98, 100],
    })
    adjustment = {
        "start_date": "2026-01-07", "end_date": "2026-01-08",
        "delta_kwh_per_day": 2, "reason": "Bổ sung tải",
    }
    args = {**BASELINE_ARGS, "model_type": "auto", "non_routine_adjustments": [adjustment]}
    scope = "mv-test"
    store = {
        "df": measured, "mapping_confirmed": True, "run_scope_id": scope,
        "config": {"mv_standards": {"confidence_level": 0.8, "daily": {"max_cv_rmse_pct": 20}}},
    }
    response = run_tool("energy_baseline", args, store)
    run = db.get_run(response["run_id"], scope_id=scope)
    result = run["result"]
    assert run["args"] == args
    assert result == response["result"]
    assert result["ashrae14_check"]["max_cv_rmse_pct"] == 20
    assert result["uncertainty"]["confidence_level"] == 0.8
    assert result["nra_total_kwh"] == 4
    assert result["summary"]["savings_kwh"] == pytest.approx(44)
    assert result["cusum_series"][-1]["cusum_kwh"] == pytest.approx(-44)
    json.dumps(result, allow_nan=False)
    model_result = _model_result("energy_baseline", result)
    assert model_result["nra_total_kwh"] == 4
    assert "cusum_series" not in model_result and "non_routine_adjustments" not in model_result


def test_baseline_rejects_additional_nra_properties_without_logging(tmp_path, monkeypatch):
    db_path = tmp_path / "runs.db"
    monkeypatch.setattr(db, "DB_PATH", db_path)
    args = {**BASELINE_ARGS, "non_routine_adjustments": [{
        "start_date": "2026-01-07", "end_date": "2026-01-08",
        "delta_kwh_per_day": 2, "reason": "Bổ sung tải", "approved": True,
    }]}
    response = run_tool("energy_baseline", args, {"mapping_confirmed": True})
    assert "non_routine_adjustments.0" in response["error"]
    assert not db_path.exists()
