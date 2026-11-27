"""Kiểm tra cấu trúc báo cáo và truy vết số liệu SEU."""

from __future__ import annotations

import json
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pandas as pd
import pytest
from docx import Document

import db
import gemini_service
from agent import run_tool
from report import (
    FIXED_INTERPRETATION,
    build_report_data,
    generate_interpretation,
    render_docx,
    render_markdown,
    render_report_json,
)
from tools import seu_pareto


def _report_store() -> dict[str, Any]:
    """Tạo store lịch sử nhỏ để kiểm tra báo cáo."""
    return {
        "mapping_confirmed": True,
        "df": pd.DataFrame(
            {
                "timestamp": pd.to_datetime(
                    ["2025-01-01", "2025-01-02", "2025-01-03", "2025-01-04", "2025-01-05"]
                ),
                "production_t": [10.0, 20.0, 30.0, 40.0, 50.0],
                "temp_c": [24.0, 25.0, 24.0, 26.0, 25.0],
                "cdd": [0.0, 1.0, 0.0, 2.0, 1.0],
                "kwh_production": [1800.0, 3600.0, 5400.0, 7200.0, 9000.0],
                "kwh_chiller": [100.0, 110.0, 120.0, 130.0, 140.0],
                "kwh_lighting": [100.0, 100.0, 100.0, 100.0, 100.0],
            }
        ),
        "config": {
            "tariff_vnd_per_kwh": 2500,
            "emission_factor_kg_per_kwh": None,
            "seu_threshold": 0.8,
            "opportunities": [
                {
                    "name": "Chiller Savings",
                    "applies_to": "kwh_chiller",
                    "saving_pct": 0.1,
                    "capex_vnd": 100_000,
                },
                {
                    "name": "Lighting Upgrade",
                    "applies_to": "kwh_lighting",
                    "saving_pct": 0.1,
                    "capex_vnd": 50_000,
                },
            ],
        },
    }


def test_report_works_without_llm_and_seu_kwh_match_tool_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Kiểm tra mẫu offline, Action Plan và mọi kWh SEU."""
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "runs.db")
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    store = _report_store()
    store.update({"filename": "Nhà máy | mẫu.xlsx", "upload_sheet": "Nhật ký điện"})
    db.save_decision("Chiller Savings", "Approve")

    report_data = build_report_data(store)
    interpretation = generate_interpretation(report_data)
    markdown = render_markdown(report_data, interpretation)
    docx_data = render_docx(report_data, interpretation)

    expected = seu_pareto(store["df"], ["kwh_production", "kwh_chiller", "kwh_lighting"], 0.8)
    actual_items = report_data["seu_analysis"]["items"]
    assert [(item["item"], item["kwh"]) for item in actual_items] == [
        (str(item), expected.loc[item, "kwh"]) for item in expected.index
    ]
    for item in actual_items:
        assert f"| {item['item']} | {item['kwh']:,.2f} |" in markdown

    assert interpretation == FIXED_INTERPRETATION
    assert report_data["action_plan"]["approved"][0]["name"] == "Chiller Savings"
    assert report_data["action_plan"]["waiting_for_approval"] == [
        {"name": "Lighting Upgrade", "status": "chờ duyệt"}
    ]
    assert len(report_data["run_ids"]) == 5
    assert "## Giả định và hạn chế" in markdown
    assert "80.00%" in markdown
    run_section = markdown.split("## Run IDs", maxsplit=1)[1]
    assert all(f"| {run_id} |" in run_section for run_id in report_data["run_ids"])
    assert "Nhà máy \\| mẫu.xlsx" in markdown

    exported = json.loads(render_report_json(report_data))
    assert exported == report_data
    assert exported["metadata"]["date_start"] == "2025-01-01"
    assert exported["metadata"]["date_end"] == "2025-01-05"
    assert exported["metadata"]["rows"] == 5
    assert all(run["ts"].endswith("+00:00") for run in exported["tool_runs"])
    document = Document(BytesIO(docx_data))
    document_cells = [cell.text for table in document.tables for row in table.rows for cell in row.cells]
    assert "Nhà máy | mẫu.xlsx" in document_cells
    assert "Nhật ký điện" in document_cells
    assert "2025-01-01" in document_cells
    assert "2025-01-05" in document_cells
    assert all(str(run_id) in document_cells for run_id in report_data["run_ids"])
    assert document.core_properties.language == "vi-VN"


def test_report_ignores_unrelated_runs_and_reflects_latest_decision(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Chỉ tái sử dụng kết quả thuộc phạm vi báo cáo hiện tại."""
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "runs.db")
    foreign_run = db.log_run("seu_analysis", {"threshold": 0.8}, {}, scope_id="other")
    store = _report_store()
    store.update({
        "run_scope_id": "current",
        "decision_scope_id": "current",
        "analysis_results": {"seu": {"run_id": foreign_run}},
    })
    db.save_decision("Chiller Savings", "Approve", scope_id="other")
    first_report = build_report_data(store)

    assert foreign_run not in first_report["run_ids"]
    assert first_report["action_plan"]["approved"] == []
    assert db.get_runs(first_report["run_ids"], scope_id="other") == {}
    db.save_decision("Chiller Savings", "Approve", scope_id="current")
    updated_report = build_report_data(store)
    assert updated_report["run_ids"] == first_report["run_ids"]
    assert updated_report["action_plan"]["approved"][0]["name"] == "Chiller Savings"


def test_report_preserves_selected_enpi_drivers_and_retries_failed_tools(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Dùng đúng mô hình EnPI và thử lại phép tính lỗi sau khi sửa đầu vào."""
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "runs.db")
    store = _report_store()
    tariff = store["config"].pop("tariff_vnd_per_kwh")
    store["df"] = store["df"].drop(columns="cdd")
    initial = build_report_data(store)
    assert initial["enpi_analysis"]["drivers"] == ["production_t"]
    assert initial["action_plan"]["error"]

    store["clean_df"]["cdd"] = [0.0, 1.0, 0.0, 2.0, 1.0]
    store["enpi_drivers"] = ["cdd"]
    args = {"total_col": "kwh_total", "drivers": store["enpi_drivers"]}
    enpi_response = run_tool("enpi", args, store)
    store["analysis_results"]["enpi"] = enpi_response
    store["config"]["tariff_vnd_per_kwh"] = tariff
    rebuilt = build_report_data(store)
    assert rebuilt["enpi_analysis"]["run_id"] == enpi_response["run_id"]
    assert rebuilt["enpi_analysis"]["drivers"] == ["cdd"]
    assert rebuilt["action_plan"]["error"] is None
    assert "Biến hồi quy · CDD" in render_markdown(rebuilt, FIXED_INTERPRETATION)


def test_generate_interpretation_uses_gemini_without_adding_numbers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Kiểm tra Gemini chỉ bổ sung diễn giải định tính cho dữ liệu báo cáo."""
    expected_text = "Mức tiêu thụ được phân tích theo các hạng mục có số liệu."

    class FakeModels:
        def generate_content(self, **kwargs: Any) -> SimpleNamespace:
            assert kwargs["model"] == "gemini-3.8-flash"
            assert '"run_ids"' in kwargs["contents"]
            assert "ten-tep-noi-bo" not in kwargs["contents"]
            assert "tool_runs" not in kwargs["contents"]
            assert kwargs["config"].system_instruction
            return SimpleNamespace(text=expected_text)

    class FakeClient:
        def __init__(self, api_key: str, http_options: Any) -> None:
            assert api_key == "test-key"
            assert http_options.timeout > 0
            self.models = FakeModels()

    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    monkeypatch.delenv("GEMINI_MODEL", raising=False)
    monkeypatch.setattr(gemini_service.genai, "Client", FakeClient)

    result = generate_interpretation({
        "run_ids": [7],
        "metadata": {"source_file": "ten-tep-noi-bo.xlsx"},
        "tool_runs": [{"run_id": 7, "result": {}}],
    })

    assert result == expected_text


def test_report_includes_energy_balance_and_tariff_when_present(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """Hồ sơ xuất kèm cân bằng GJ/TOE và TOU khi phiên đã tính hai công cụ này."""
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "runs.db")
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    df = pd.DataFrame(
        {
            "timestamp": pd.date_range("2025-04-01 08:00", periods=3, freq="h"),
            "tariff_period": ["peak", "normal", "off_peak"],
            "kwh_total": [100.0, 80.0, 60.0],
            "kwh_chiller": [60.0, 48.0, 36.0],
            "cooling_load_rth": [100.0, 80.0, 60.0],
            "boiler_fuel_kg": [100.0, 50.0, 50.0],
            "fuel_type": ["DO", "Biomass", "Biomass"],
            "steam_t": [1.0, 0.2, None],
            "peak_demand_kw": [120.0, 100.0, 80.0],
            "power_factor": [0.8, 0.9, 0.95],
            "production_t": [10.0, 9.0, 8.0],
            "temp_c": [30.0, 29.0, 27.0],
            "cdd": [6.0, 5.0, 3.0],
        }
    )
    store = {
        "filename": "audit.csv",
        "mapping_confirmed": True,
        "df": df,
        "measured_df": df.copy(),
        "run_scope_id": "scope-balance-tariff",
        "decision_scope_id": "scope-balance-tariff",
        "config": {
            "seu_threshold": 0.8,
            "tariff_vnd_per_kwh": {"peak": 3151, "normal": 1738, "off_peak": 1146},
            "fuel_properties": {
                "do_oil": {"ncv_gj_per_kg": 0.043, "co2_kg_per_gj": 74.1},
                "biomass": {"ncv_gj_per_kg": 0.0156, "co2_kg_per_gj": 112.0},
            },
            "opportunities": [],
        },
    }
    balance_resp = run_tool("energy_balance", {}, store)
    tariff_resp = run_tool("tariff_power_quality", {}, store)
    store["analysis_results"] = {
        "energy_balance": balance_resp,
        "tariff_power_quality": tariff_resp,
    }

    report_data = build_report_data(store)
    assert report_data["energy_balance"]["run_id"] == balance_resp["run_id"]
    assert report_data["tariff_power_quality"]["run_id"] == tariff_resp["run_id"]
    assert balance_resp["run_id"] in report_data["run_ids"]
    assert tariff_resp["run_id"] in report_data["run_ids"]

    markdown = render_markdown(report_data, FIXED_INTERPRETATION)
    assert "Cân bằng năng lượng (GJ/TOE) và hiệu suất" in markdown
    assert "Biểu giá TOU và chất lượng điện" in markdown
    assert "Chi phí TOU ước tính" in markdown

    docx_bytes = render_docx(report_data, FIXED_INTERPRETATION)
    assert len(docx_bytes) > 0
