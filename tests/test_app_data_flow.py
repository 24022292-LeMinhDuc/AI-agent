"""Acceptance checks cho trạng thái upload và handoff giữa các module."""

from __future__ import annotations

import json
from datetime import date, timedelta
from io import BytesIO
from pathlib import Path

import pandas as pd
import pytest
from streamlit.testing.v1 import AppTest

import db
import agent


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_sample_loader_and_dedicated_report_page(monkeypatch, tmp_path: Path) -> None:
    """Dữ liệu mẫu dùng cùng luồng chuẩn hóa và xuất hồ sơ như tệp tải lên."""
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "runs.db")
    app = AppTest.from_file(str(PROJECT_ROOT / "app.py")).run()

    app.button(key="load_sample_dataset").click().run()
    store = app.session_state["store"]
    assert store["filename"] == "sample_a.csv"
    assert len(store["raw_df"]) == 365
    assert store["mapping_confirmed"] is False
    app.button(key=f"confirm_{store['upload_fingerprint']}").click().run()

    app.button(key="nav_6_Báo cáo").click().run()
    assert app.session_state["active_page"] == "Báo cáo"
    app.button(key="export_energy_report").click().run()
    assert not app.exception
    archive = json.loads(app.session_state["store"]["report_export"]["json"])
    assert archive["metadata"]["source_file"] == "sample_a.csv"
    assert archive["run_ids"]


def test_dashboard_keeps_submeter_and_audit_data_when_period_has_no_total(
    monkeypatch, tmp_path: Path,
) -> None:
    """Một kỳ thiếu điện tổng vẫn hiển thị phụ tải và chi phí khai báo."""
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "runs.db")
    app = AppTest.from_file(str(PROJECT_ROOT / "app.py")).run()
    content = (
        b"Ngay,Tong_dien_tieu_thu_kWh,Dien_chiller_kWh,Chi_phi_nang_luong_VND\n"
        b"2026-01-01,200,100,450000\n"
        b"2026-02-01,,110,275000\n"
    )
    app.file_uploader[0].set_value(("meter_gaps.csv", content, "text/csv")).run()
    store = app.session_state["store"]
    app.button(key=f"confirm_{store['upload_fingerprint']}").click().run()
    app.button(key="nav_2_Dashboard").click().run()
    store = app.session_state["store"]
    date_key = f"dashboard_dates_{store['decision_scope_id']}"
    february = date(2026, 2, 1)
    app.date_input(key=date_key).set_value((february, february)).run()

    assert not app.exception
    assert any(panel.label == "Vận hành, nhiệt và phát thải" for panel in app.expander)
    assert app.get("plotly_chart")
    result = app.session_state["store"]["analysis_results"]["dashboard"]["result"]
    assert result["summary"]["total_kwh"] is None
    assert result["summary"]["reported_audit"]["energy_cost_vnd"]["value"] == 275000


def test_replacing_invalid_dataset_never_leaves_previous_dataset_active(
    monkeypatch, tmp_path: Path
) -> None:
    """File mới lỗi phải vô hiệu hóa dữ liệu đã chuẩn hóa trước đó."""
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "runs.db")
    app = AppTest.from_file(str(PROJECT_ROOT / "app.py")).run()
    sample = PROJECT_ROOT / "data" / "sample_a.csv"
    app.file_uploader[0].set_value((sample.name, sample.read_bytes(), "text/csv"))
    app.run()
    fingerprint = app.session_state["store"]["upload_fingerprint"]
    app.button(key=f"confirm_{fingerprint}").click()
    app.run()
    assert app.session_state["store"]["mapping_confirmed"] is True
    previous_scope = app.session_state["store"]["run_scope_id"]
    app.session_state["store"]["chat_history"] = [
        {"role": "assistant", "content": "Số liệu của bộ dữ liệu cũ."}
    ]

    invalid_content = b"unrelated,value\nalpha,1\nbeta,2\n"
    app.file_uploader[0].set_value(("invalid.csv", invalid_content, "text/csv"))
    app.run()

    store = app.session_state["store"]
    assert store["filename"] == "invalid.csv"
    assert store["mapping_confirmed"] is False
    assert "df" not in store
    assert "quality_report" not in store
    assert store["chat_history"] == []
    assert store["run_scope_id"] != previous_scope

    invalid_fingerprint = store["upload_fingerprint"]
    app.button(key=f"confirm_{invalid_fingerprint}").click()
    app.run()
    assert app.session_state["store"]["mapping_confirmed"] is False
    assert any("timestamp" in str(element.value).lower() for element in app.error)

    app.button(key="clear_energy_dataset").click()
    app.run()
    store = app.session_state["store"]
    assert "raw_df" not in store
    assert "df" not in store
    assert app.file_uploader[0].value is None


def test_opportunity_handoff_carries_saved_run_to_agent(
    monkeypatch, tmp_path: Path
) -> None:
    """Nút hỏi AI gửi run nguồn của cơ hội thay vì tự dựng số liệu."""
    monkeypatch.setenv("GEMINI_API_KEY", "mock-key")
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "runs.db")
    captured: dict[str, object] = {}

    def fake_ask(question, store, history=()):
        captured["question"] = question
        captured["context"] = store.get("selected_context")
        run_ids = store.get("selected_context", {}).get("run_ids", [])
        return {"answer": "Kết quả cần kiểm chứng.", "run_ids": run_ids}

    monkeypatch.setattr(agent, "ask", fake_ask)
    app = AppTest.from_file(str(PROJECT_ROOT / "app.py")).run()
    sample = PROJECT_ROOT / "data" / "sample_a.csv"
    app.file_uploader[0].set_value((sample.name, sample.read_bytes(), "text/csv"))
    app.run()
    fingerprint = app.session_state["store"]["upload_fingerprint"]
    app.button(key=f"confirm_{fingerprint}").click()
    app.run()
    app.button(key="nav_4_Cơ hội").click()
    app.run()

    app.button(key="ask_opportunity_0").click()
    app.run()

    selected_context = captured["context"]
    assert isinstance(selected_context, dict)
    run_id = selected_context["run_ids"][0]
    saved_run = db.get_run(run_id, scope_id=app.session_state["store"]["run_scope_id"])
    assert saved_run is not None
    assert saved_run["tool"] == "list_opportunities"
    assert "cơ hội" in str(captured["question"]).lower()
    assert app.session_state["active_page"] == "Hỏi đáp"
    assert len(app.session_state["store"]["chat_history"]) == 2


def test_chat_displays_verification_badges_and_warning_details(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("GEMINI_API_KEY", "mock-key")
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "runs.db")
    app = AppTest.from_file(str(PROJECT_ROOT / "app.py")).run()
    store = app.session_state["store"]
    store["chat_history"] = [
        {"role": "assistant", "content": status, "verification": {"status": status, "retries": index}}
        for index, status in enumerate(("verified", "repaired"))
    ]
    warning = {
        "status": "warning", "retries": 2, "unmatched_numbers": [{"text": "987", "matched": False}],
        "missing_citations": ["251"], "missing_run_ids": [61],
        "issues": [{"quantity": "987 kWh", "run_ids": [61]}],
    }
    monkeypatch.setattr(agent, "ask", lambda *args: {"answer": "Kết quả cần xem lại.", "verification": warning})
    store["pending_question"] = "Kiểm tra số đo."
    app.button(key="nav_5_Hỏi đáp").click().run()
    for _ in range(2):
        assert not app.exception
        badges = [element.value for element in app.markdown]
        assert ":green-badge[Đã đối chiếu runs.db]" in badges
        assert ":blue-badge[Đã tự hiệu chỉnh & xác thực]" in badges
        assert ":yellow-badge[Cảnh báo có số liệu chưa khớp]" in badges
        captions = [element.value for element in app.caption]
        assert "Số chưa khớp · 987 kWh · run_id 61" in captions
        assert "Số thiếu trích dẫn · 251" in captions
        assert "run_id thiếu nguồn hợp lệ · 61" in captions
        app.run()


def test_dashboard_filter_and_period_compare_use_logged_results(
    monkeypatch, tmp_path: Path
) -> None:
    """Dashboard hiển thị KPI lọc ngày và lưu lần so sánh có run_id."""
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "runs.db")
    app = AppTest.from_file(str(PROJECT_ROOT / "app.py")).run()
    sample = PROJECT_ROOT / "data" / "sample_a.csv"
    app.file_uploader[0].set_value((sample.name, sample.read_bytes(), "text/csv"))
    app.run()
    fingerprint = app.session_state["store"]["upload_fingerprint"]
    app.button(key=f"confirm_{fingerprint}").click()
    app.run()
    app.button(key="nav_2_Dashboard").click()
    app.run()

    assert any(metric.label == "Tổng điện năng (kWh)" for metric in app.metric)
    dashboard_scope = app.session_state["store"]["decision_scope_id"]
    app.button(key=f"compare_periods_{dashboard_scope}").click()
    app.run()

    comparison = app.session_state["store"]["compare_periods_response"]
    assert comparison["run_id"]
    assert comparison["result"]["main_driver"]
    saved_run = db.get_run(
        comparison["run_id"], scope_id=app.session_state["store"]["run_scope_id"]
    )
    assert saved_run is not None
    assert saved_run["tool"] == "compare_periods"


def test_navigation_preserves_upload_and_dashboard_filters(monkeypatch, tmp_path: Path) -> None:
    """Rời và trở lại từng trang không làm mất dữ liệu hoặc bộ lọc đã chọn."""
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "runs.db")
    app = AppTest.from_file(str(PROJECT_ROOT / "app.py")).run()
    sample = PROJECT_ROOT / "data" / "sample_a.csv"
    app.file_uploader[0].set_value((sample.name, sample.read_bytes(), "text/csv")).run()
    fingerprint = app.session_state["store"]["upload_fingerprint"]
    app.button(key=f"confirm_{fingerprint}").click().run()
    store = app.session_state["store"]
    quality_run = store["quality_run_id"]
    decision_scope = store["decision_scope_id"]

    app.button(key="nav_2_Dashboard").click().run()
    app.selectbox(key=f"dashboard_level_{decision_scope}").select("Tuần").run()
    first, last = app.date_input[0].value
    period = (first, min(first + timedelta(days=27), last))
    app.date_input[0].set_value(period).run()
    dashboard_run = app.session_state["store"]["analysis_results"]["dashboard"]["run_id"]
    app.button(key="nav_3_SEU & EnPI").click().run()
    app.slider[0].set_value(0.65).run()
    app.button(key="nav_1_Dữ liệu").click().run()

    assert not app.exception
    assert app.session_state["store"]["mapping_confirmed"] is True
    assert app.session_state["store"]["quality_run_id"] == quality_run
    assert app.session_state["store"]["upload_fingerprint"] == fingerprint
    app.button(key="nav_2_Dashboard").click().run()
    assert app.selectbox(key=f"dashboard_level_{decision_scope}").value == "Tuần"
    assert app.date_input[0].value == period
    assert app.session_state["store"]["analysis_results"]["dashboard"]["run_id"] == dashboard_run
    app.button(key="nav_3_SEU & EnPI").click().run()
    assert app.slider[0].value == 0.65


def test_excel_sheet_changes_invalidate_analysis(monkeypatch, tmp_path: Path) -> None:
    """Mỗi trang tính có mapping và nguồn bằng chứng riêng."""
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "runs.db")
    workbook = BytesIO()
    with pd.ExcelWriter(workbook, engine="openpyxl") as writer:
        for name, kwh in (("2024", 100), ("2025", 200)):
            pd.DataFrame(
                {
                    "timestamp": [f"{name}-01-0{day}" for day in range(1, 5)],
                    "temp_c": [25, 27, 30, 31],
                    "kwh_production": [kwh, kwh + 10, kwh + 24, kwh + 35],
                }
            ).to_excel(writer, sheet_name=name, index=False)

    app = AppTest.from_file(str(PROJECT_ROOT / "app.py")).run()
    app.file_uploader[0].set_value(
        (
            "meters.xlsx",
            workbook.getvalue(),
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )
    ).run()
    fingerprint = app.session_state["store"]["upload_fingerprint"]
    app.button(key=f"confirm_{fingerprint}").click().run()
    store = app.session_state["store"]
    first_scope, first_decision = store["run_scope_id"], store["decision_scope_id"]
    assert store["upload_sheet"] == "2024"
    assert store["df"]["kwh_production"].iat[0] == 100
    app.button(key="nav_3_SEU & EnPI").click().run()
    enpi = app.session_state["store"]["analysis_results"]["enpi"]["result"]
    assert "cdd" in enpi["coefficients"]
    assert enpi["kwh_per_ton"] is None
    app.button(key="nav_1_Dữ liệu").click().run()

    app.selectbox(key=f"upload_sheet_{fingerprint}").select("2025").run()
    store = app.session_state["store"]
    assert store["upload_sheet"] == "2025"
    assert store["mapping_confirmed"] is False
    assert store["run_scope_id"] != first_scope
    assert "df" not in store
    assert "enpi_drivers" not in store
    app.button(key=f"confirm_{fingerprint}").click().run()
    store = app.session_state["store"]
    assert store["mapping_confirmed"] is True
    assert store["decision_scope_id"] != first_decision
    assert store["df"]["kwh_production"].iat[0] == 200


def test_failed_cleaning_keeps_normalized_data_inactive(monkeypatch, tmp_path: Path) -> None:
    """Khi bước làm sạch lỗi, dataset thử nghiệm chưa sẵn sàng để phân tích."""
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "runs.db")

    def failed_clean(name, args, store):
        assert name == "data_quality"
        store["clean_df"] = store["df"]
        return {"run_id": 1, "result": {"error": "Lỗi làm sạch thử nghiệm"}}

    monkeypatch.setattr(agent, "run_tool", failed_clean)
    app = AppTest.from_file(str(PROJECT_ROOT / "app.py")).run()
    content = "timestamp;kwh_production\n2024-01-01;10,5\n".encode("utf-16")
    app.file_uploader[0].set_value(("meters.csv", content, "text/csv")).run()
    store = app.session_state["store"]
    assert store["raw_df"]["kwh_production"].iat[0] == 10.5
    app.button(key=f"confirm_{store['upload_fingerprint']}").click().run()
    store = app.session_state["store"]
    assert store["mapping_confirmed"] is False
    assert "df" not in store
    assert "clean_df" not in store
    assert any("Lỗi làm sạch thử nghiệm" in error.value for error in app.error)


def test_report_export_tracks_current_opportunity_decisions(monkeypatch, tmp_path: Path) -> None:
    """File JSON dùng cùng run với báo cáo và được làm mới khi quyết định đổi."""
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "runs.db")
    app = AppTest.from_file(str(PROJECT_ROOT / "app.py")).run()
    sample = PROJECT_ROOT / "data" / "sample_a.csv"
    app.file_uploader[0].set_value((sample.name, sample.read_bytes(), "text/csv")).run()
    fingerprint = app.session_state["store"]["upload_fingerprint"]
    app.button(key=f"confirm_{fingerprint}").click().run()
    app.button(key="nav_4_Cơ hội").click().run()
    app.button(key="approve_opportunity_0").click().run()
    app.button(key="export_energy_report").click().run()

    assert not app.exception
    report_export = app.session_state["store"]["report_export"]
    audit_record = json.loads(report_export["json"])
    assert audit_record["run_ids"] == report_export["run_ids"]
    assert len(audit_record["action_plan"]["approved"]) == 1
    assert report_export["docx"].startswith(b"PK")
    assert "Báo cáo kiểm toán năng lượng" in report_export["markdown"]

    app.button(key="nav_3_SEU & EnPI").click().run()
    decision_scope = app.session_state["store"]["decision_scope_id"]
    app.selectbox(key=f"enpi_drivers_{decision_scope}").select(("cdd",)).run()
    store = app.session_state["store"]
    assert "report_export" not in store
    enpi_run = store["analysis_results"]["enpi"]["run_id"]
    assert set(store["analysis_results"]["enpi"]["result"]["coefficients"]) == {"intercept", "cdd"}
    app.button(key="nav_4_Cơ hội").click().run()
    app.button(key="export_energy_report").click().run()
    assert enpi_run in app.session_state["store"]["report_export"]["run_ids"]

    app.button(key="reject_opportunity_0").click().run()
    assert "report_export" not in app.session_state["store"]
    app.button(key="export_energy_report").click().run()
    revised_record = json.loads(app.session_state["store"]["report_export"]["json"])
    assert revised_record["action_plan"]["approved"] == []
    assert len(revised_record["action_plan"]["rejected"]) == 1


def test_baseline_editor_records_nra_and_displays_mv_outputs(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "runs.db")
    dates = pd.date_range("2026-01-01", periods=45)
    temperature = [17 + i % 9 for i in range(len(dates))]
    measured = pd.DataFrame({
        "timestamp": dates, "temp_c": temperature,
        "kwh_total": [100 + 3 * max(temp - 20, 0) - (5 if i >= 30 else 0)
                      for i, temp in enumerate(temperature)],
    })
    app = AppTest.from_file(str(PROJECT_ROOT / "app.py")).run()
    app.file_uploader[0].set_value(("mv.csv", measured.to_csv(index=False).encode(), "text/csv")).run()
    fingerprint = app.session_state["store"]["upload_fingerprint"]
    app.button(key=f"confirm_{fingerprint}").click().run()
    scope = app.session_state["store"]["run_scope_id"]
    app.button(key="nav_3_SEU & EnPI").click().run()
    app.selectbox(key=f"baseline_model_type_{scope}").select("3pc").run()
    app.date_input(key=f"baseline_dates_{scope}").set_value((dates[0].date(), dates[29].date())).run()
    app.date_input(key=f"reporting_dates_{scope}").set_value((dates[30].date(), dates[-1].date())).run()

    app.session_state[f"baseline_nra_{scope}"] = {
        "edited_rows": {}, "deleted_rows": [], "added_rows": [{
            "start_date": "2026-01-31", "end_date": "2026-01-31",
            "delta_kwh_per_day": 4, "reason": "Bổ sung tải",
        }],
    }
    app.button(key=f"calculate_baseline_{scope}").click().run()
    assert not app.exception and not app.error
    response = app.session_state["store"]["analysis_results"]["energy_baseline"]
    assert response["result"]["model"]["balance_temp_c"] == 20
    assert response["result"]["nra_total_kwh"] == 4
    assert response["result"]["summary"]["savings_kwh"] == pytest.approx(79)
    assert any(metric.label == "FSU (90%)" for metric in app.metric)
    assert any("CUSUM" in chart.proto.spec for chart in app.get("plotly_chart"))
    app.button(key="nav_2_Dashboard").click().run()
    app.button(key="nav_3_SEU & EnPI").click().run()
    store = app.session_state["store"]
    assert store["analysis_results"]["energy_baseline"]["run_id"] == response["run_id"]
    assert store["baseline_args"]["non_routine_adjustments"][0]["delta_kwh_per_day"] == 4


def test_energy_baseline_uses_measured_scope_and_clears_stale_result(
    monkeypatch, tmp_path: Path
) -> None:
    """Luồng EnB chỉ dùng ngày đo hợp lệ và giữ nguồn đúng theo lựa chọn."""
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "runs.db")
    dates = pd.date_range("2026-01-01", periods=45)
    production = [10 + day % 4 for day in range(len(dates))]
    measured = pd.DataFrame(
        {
            "timestamp": dates,
            "production_t": production,
            "kwh_production": [
                100.0 + 2 * output - (10.0 if day >= 30 else 0.0)
                for day, output in enumerate(production)
            ],
            "kwh_chiller": [20.0 + 3 * output for output in production],
        }
    )
    measured.loc[36, "kwh_production"] = float("nan")
    app = AppTest.from_file(str(PROJECT_ROOT / "app.py")).run()
    app.file_uploader[0].set_value(
        ("baseline.csv", measured.to_csv(index=False).encode(), "text/csv")
    ).run()
    fingerprint = app.session_state["store"]["upload_fingerprint"]
    app.button(key=f"confirm_{fingerprint}").click().run()
    store = app.session_state["store"]
    scope = store["run_scope_id"]
    assert pd.isna(store["measured_df"].loc[36, "kwh_production"])
    assert pd.notna(store["clean_df"].loc[36, "kwh_production"])
    app.button(key="nav_3_SEU & EnPI").click().run()
    baseline_period = (dates[0].date(), dates[29].date())
    reporting_period = (dates[30].date(), dates[-1].date())
    app.date_input(key=f"baseline_dates_{scope}").set_value(baseline_period).run()
    app.date_input(key=f"reporting_dates_{scope}").set_value(reporting_period).run()
    app.selectbox(key=f"baseline_energy_col_{scope}").select("kwh_production").run()
    app.button(key=f"calculate_baseline_{scope}").click().run()

    assert not app.exception
    assert not app.error
    store = app.session_state["store"]
    response = store["analysis_results"]["energy_baseline"]
    assert response["result"]["reporting"]["eligible_rows"] == 14
    assert response["result"]["summary"]["savings_kwh"] == pytest.approx(140.0)
    assert any(metric.label == "Tiết kiệm ước tính (kWh)" for metric in app.metric)
    saved_run = db.get_run(response["run_id"], scope_id=scope)
    assert saved_run["tool"] == "energy_baseline"
    assert saved_run["args"]["energy_col"] == "kwh_production"

    app.button(key="nav_2_Dashboard").click().run()
    app.button(key="nav_3_SEU & EnPI").click().run()
    store = app.session_state["store"]
    assert store["analysis_results"]["energy_baseline"]["run_id"] == response["run_id"]
    assert app.date_input(key=f"reporting_dates_{scope}").value == reporting_period

    store["report_export"] = {"json": "prior report"}
    app.date_input(key=f"reporting_dates_{scope}").set_value(
        (dates[31].date(), dates[-1].date())
    ).run()
    store = app.session_state["store"]
    assert "baseline_args" not in store
    assert "energy_baseline" not in store["analysis_results"]
    assert "report_export" not in store
