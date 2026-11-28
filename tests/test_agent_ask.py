"""Kiểm tra vòng lặp hỏi đáp và function calling của Gemini."""

from __future__ import annotations

import copy
import sqlite3
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pandas as pd
import pytest
from google.genai import types

import agent
import db
import gemini_service


class FakeModels:
    """Cung cấp phản hồi Gemini giả và ghi lại yêu cầu."""

    def __init__(self, responses: list[SimpleNamespace]) -> None:
        self.responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    def generate_content(self, **kwargs: Any) -> SimpleNamespace:
        self.calls.append(copy.deepcopy(kwargs))
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def _mock_gemini(
    monkeypatch: pytest.MonkeyPatch, responses: list[SimpleNamespace]
) -> FakeModels:
    """Thay Gemini client bằng phản hồi giả."""
    fake_models = FakeModels(responses)

    class FakeClient:
        def __init__(self, api_key: str, http_options: types.HttpOptions) -> None:
            assert api_key == "test-key"
            assert http_options.retry_options.attempts == 2
            assert http_options.timeout == 20_000
            assert http_options.retry_options.http_status_codes == [500, 503, 504]
            self.models = fake_models

    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    monkeypatch.delenv("GEMINI_MODEL", raising=False)
    monkeypatch.delenv("GEMINI_FALLBACK_MODELS", raising=False)
    monkeypatch.setattr(gemini_service.genai, "Client", FakeClient)
    return fake_models


def _function_response(
    name: str, arguments: dict[str, Any], call_id: str = "call-1"
) -> SimpleNamespace:
    """Tạo phản hồi function call giả."""
    function_call = types.FunctionCall(id=call_id, name=name, args=arguments)
    return SimpleNamespace(
        text=None,
        candidates=[
            SimpleNamespace(
                content=types.Content(
                    role="model",
                    parts=[types.Part(function_call=function_call)],
                )
            )
        ],
    )


def _text_response(text: str) -> SimpleNamespace:
    """Tạo phản hồi văn bản cuối cùng giả."""
    return SimpleNamespace(
        text=text,
        candidates=[
            SimpleNamespace(
                content=types.Content(
                    role="model", parts=[types.Part.from_text(text=text)]
                )
            )
        ],
    )


def _compare_store() -> dict[str, Any]:
    """Tạo dữ liệu máy chủ nhỏ cho so sánh tháng."""
    return {
        "mapping_confirmed": True,
        "df": pd.DataFrame(
            {
                "timestamp": pd.to_datetime(["2025-01-01", "2025-02-01"]),
                "production_t": [10.0, 15.0],
                "temp_c": [20.0, 25.0],
                "kwh_production": [100.0, 140.0],
                "kwh_chiller": [50.0, 110.0],
            }
        ),
        "config": {},
    }


def test_ask_calls_compare_tool_then_returns_answer_with_run_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Kiểm tra hỏi đáp gọi compare_periods rồi trả kết quả cuối."""
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "runs.db")
    fake_models = _mock_gemini(
        monkeypatch,
        [
            _function_response(
                "compare_periods",
                {"month_a": "2025-01", "month_b": "2025-02"},
            ),
            _text_response("Chiller tăng 60 kWh [1]."),
        ],
    )

    result = agent.ask("So sánh tháng 1 và tháng 2.", _compare_store())

    assert result["answer"] == "Chiller tăng 60 kWh [1]."
    assert len(result["run_ids"]) == 1
    assert result["verification"]["verified"] is True
    run = db.get_run(result["run_ids"][0])
    assert run is not None
    assert run["tool"] == "compare_periods"
    assert len(fake_models.calls) == 2
    first_call = fake_models.calls[0]
    assert first_call["model"] == "gemini-3.8-flash"
    assert first_call["config"].automatic_function_calling.disable is True
    function_names = {
        declaration.name
        for declaration in first_call["config"].tools[0].function_declarations
    }
    assert "compare_periods" in function_names


def test_ask_passes_schema_error_back_for_correction(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Kiểm tra Gemini nhận lỗi schema rồi có thể gửi tham số đã sửa."""
    db_path = tmp_path / "runs.db"
    monkeypatch.setattr(db, "DB_PATH", db_path)
    fake_models = _mock_gemini(
        monkeypatch,
        [
            _function_response(
                "compare_periods",
                {"month_a": "2025-1", "month_b": "2025-02"},
                "bad-call",
            ),
            _function_response(
                "compare_periods",
                {"month_a": "2025-01", "month_b": "2025-02"},
                "good-call",
            ),
            _text_response("Đã so sánh xong [1]."),
        ],
    )

    result = agent.ask("So sánh hai tháng.", _compare_store())
    error_response = fake_models.calls[1]["contents"][-1].parts[0].function_response

    assert "run_id" not in result
    assert "[1]" in result["answer"]
    assert error_response.id == "bad-call"
    assert "month_a" in error_response.response["error"]
    with sqlite3.connect(db_path) as connection:
        run_count = connection.execute("SELECT COUNT(*) FROM runs").fetchone()[0]
    assert run_count == 1


def test_ask_stops_after_six_tool_rounds_without_final_answer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Kiểm tra vòng lặp dừng sau sáu lượt gọi Gemini."""
    responses = [
        _function_response("data_quality", {}, f"call-{index}")
        for index in range(6)
    ]
    fake_models = _mock_gemini(monkeypatch, responses)
    run_counter = 0

    def fake_run_tool(name: str, args: dict[str, Any], store: dict[str, Any]) -> dict[str, Any]:
        nonlocal run_counter
        run_counter += 1
        return {"run_id": run_counter, "result": {"ok": True}}

    monkeypatch.setattr(agent, "run_tool", fake_run_tool)

    result = agent.ask("Tiếp tục phân tích.", {"mapping_confirmed": True})

    assert len(fake_models.calls) == 6
    assert len(result["run_ids"]) == 6
    assert "giới hạn 6 vòng" in result["answer"]


class FakeServiceUnavailable(Exception):
    """Mô phỏng Gemini trả lỗi 503 UNAVAILABLE."""

    code = 503
    status = "UNAVAILABLE"


def test_ask_switches_to_fallback_model_after_503(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Kiểm tra 503 ở model chính khiến agent thử model dự phòng."""
    fake_models = _mock_gemini(
        monkeypatch,
        [FakeServiceUnavailable("high demand"), _text_response("Gemini dự phòng đã trả lời.")],
    )

    result = agent.ask("Tóm tắt dữ liệu.", {})

    assert result["answer"] == "Gemini dự phòng đã trả lời."
    assert [call["model"] for call in fake_models.calls] == [
        gemini_service.MODEL_NAME,
        gemini_service.FALLBACK_MODELS[0],
    ]


def test_ask_returns_clear_message_when_all_models_are_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Kiểm tra agent kết thúc rõ ràng nếu cả chuỗi model đều quá tải."""
    fake_models = _mock_gemini(
        monkeypatch,
        [FakeServiceUnavailable("high demand") for _ in range(1 + len(gemini_service.FALLBACK_MODELS))],
    )

    result = agent.ask("Tóm tắt dữ liệu.", {})

    assert "Gemini đang quá tải" in result["answer"]
    assert len(fake_models.calls) == 1 + len(gemini_service.FALLBACK_MODELS)
    assert result["run_ids"] == []


def test_ask_sends_only_recent_history_to_gemini(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Giới hạn ngữ cảnh gửi đi nhưng giữ nguyên lịch sử giao diện."""
    fake_models = _mock_gemini(monkeypatch, [_text_response("Đã nhận.")])
    history = [
        {"role": "user" if index % 2 == 0 else "assistant", "content": f"turn-{index}"}
        for index in range(20)
    ]

    result = agent.ask("Câu hỏi mới", {}, history)

    sent_contents = fake_models.calls[0]["contents"]
    assert result["answer"] == "Đã nhận."
    assert len(sent_contents) == agent.MAX_HISTORY_MESSAGES + 1
    assert sent_contents[0].parts[0].text == "turn-8"
    assert sent_contents[-1].parts[0].text == "Câu hỏi mới"


def test_ask_includes_selected_backend_context_and_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ngữ cảnh được chọn lấy từ run đã lưu, không từ số liệu do UI tự tạo."""
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "runs.db")
    run_id = db.log_run(
        "list_opportunities",
        {},
        [{"name": "Tối ưu chiller", "saved_kwh": 1200.0, "assumption": "GIẢ ĐỊNH"}],
    )
    fake_models = _mock_gemini(monkeypatch, [_text_response("Cần kiểm tra vận hành chiller.")])
    store = {
        "mapping_confirmed": True,
        "selected_context": {
            "title": "Cơ hội Tối ưu chiller",
            "run_ids": [run_id],
        },
    }

    result = agent.ask("Giải thích cơ hội này.", store)

    sent_prompt = fake_models.calls[0]["contents"][-1].parts[0].text
    assert result["run_ids"] == [run_id]
    assert "1200.0" in sent_prompt


def test_ask_allows_general_questions_without_a_confirmed_dataset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Gemini có thể giải thích kiến thức chung nhưng không gán số cho nhà máy."""
    fake_models = _mock_gemini(monkeypatch, [_text_response("EnPI là chỉ số hiệu suất năng lượng.")])

    result = agent.ask("EnPI là gì?", {})

    system_instruction = fake_models.calls[0]["config"].system_instruction
    assert result["answer"] == "EnPI là chỉ số hiệu suất năng lượng."
    assert "chưa có dataset đã xác nhận" in system_instruction
    assert fake_models.calls[0]["config"].tools is None


def test_ask_does_not_execute_tools_before_mapping_is_confirmed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _mock_gemini(monkeypatch, [_function_response("data_quality", {})])

    def unexpected_call(*args: Any) -> None:
        pytest.fail("Không được gọi công cụ khi chưa xác nhận mapping")

    monkeypatch.setattr(agent, "run_tool", unexpected_call)
    result = agent.ask("Đọc số liệu nhà máy", {"df": pd.DataFrame()})

    assert "Cần xác nhận mapping" in result["answer"]
    assert result["run_ids"] == []


def test_ask_keeps_selected_fallback_for_tool_continuation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "runs.db")
    function_call = _function_response("data_quality", {})
    function_call.candidates[0].content.parts[0].thought_signature = b"gemini-signature"
    fake_models = _mock_gemini(
        monkeypatch,
        [FakeServiceUnavailable("high demand"), function_call, _text_response("Đã kiểm tra.")],
    )
    monkeypatch.setenv("GEMINI_MODEL", "model-custom")
    monkeypatch.setenv("GEMINI_FALLBACK_MODELS", "model-custom,model-backup")

    result = agent.ask("Kiểm tra chất lượng dữ liệu", _compare_store())

    assert result["answer"] == "Đã kiểm tra."
    assert [call["model"] for call in fake_models.calls] == [
        "model-custom", "model-backup", "model-backup"
    ]
    assert fake_models.calls[2]["contents"][-2].parts[0].thought_signature == b"gemini-signature"


def test_ask_only_uses_cited_evidence_from_active_scope(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "runs.db")
    private = db.log_run("dataset_summary", {}, {"total_kwh": 8800}, scope_id="other")
    local = db.log_run("dataset_summary", {}, {"total_kwh": 1200}, scope_id="active")
    fake_models = _mock_gemini(
        monkeypatch, [_text_response(f"Điện tiêu thụ 1200 kWh [run_id: {private}].")]
    )
    store = {
        "mapping_confirmed": True,
        "run_scope_id": "active",
        "selected_context": {"title": "Tổng hợp", "run_ids": [private, local]},
    }

    result = agent.ask("Tổng điện tiêu thụ là bao nhiêu?", store)

    prompt = fake_models.calls[0]["contents"][-1].parts[0].text
    assert "8800" not in prompt
    assert "1200" in prompt
    assert result["run_ids"] == [local]
    assert result["verification"]["missing_run_ids"] == [private]
    assert result["verification"]["unmatched_numbers"] == [{"text": "1200", "matched": False}]


def test_run_tool_rejects_non_finite_json(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = tmp_path / "runs.db"
    monkeypatch.setattr(db, "DB_PATH", db_path)

    response = agent.run_tool("seu_analysis", {"threshold": float("nan")}, _compare_store())

    assert "JSON" in response["error"]
    assert not db_path.exists()


def test_data_quality_retains_previous_state_when_audit_write_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _compare_store()
    previous = store["df"].copy()
    store["clean_df"] = previous

    def fail_write(*args: Any, **kwargs: Any) -> None:
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(db, "log_run", fail_write)
    with pytest.raises(sqlite3.OperationalError, match="database is locked"):
        agent.run_tool("data_quality", {}, store)

    assert store["clean_df"] is previous


def test_dashboard_tool_sends_summary_to_gemini_and_keeps_chart_in_database(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Gemini nhận KPI ngắn; chuỗi biểu đồ đầy đủ chỉ nằm trong runs.db/UI."""
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "runs.db")
    fake_models = _mock_gemini(
        monkeypatch,
        [
            _function_response(
                "dashboard_data",
                {
                    "level": "day",
                    "start_date": "2025-01-01",
                    "end_date": "2025-01-02",
                },
            ),
            _text_response("Dashboard đã được tổng hợp theo ngày."),
        ],
    )
    store = {
        "mapping_confirmed": True,
        "df": pd.DataFrame(
            {
                "timestamp": pd.to_datetime(["2025-01-01", "2025-01-02"]),
                "production_t": [10.0, 12.0],
                "temp_c": [20.0, 21.0],
                "kwh_production": [100.0, 120.0],
            }
        ),
        "config": {"tariff_vnd_per_kwh": 2_000},
    }

    answer = agent.ask("Tóm tắt Dashboard.", store)

    function_response = fake_models.calls[1]["contents"][-1].parts[0].function_response
    assert "summary" in function_response.response["result"]
    assert "aggregated" not in function_response.response["result"]
    saved_run = db.get_run(answer["run_ids"][0])
    assert saved_run is not None
    assert "aggregated" in saved_run["result"]


def _compare_call() -> SimpleNamespace:
    return _function_response("compare_periods", {"month_a": "2025-01", "month_b": "2025-02"})


def test_verification_accepts_the_first_supported_answer(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "runs.db")
    correct = "Điện chiller tăng 60 kWh [run_id: 1]."
    models = _mock_gemini(monkeypatch, [_compare_call(), _text_response(correct)])
    reply = agent.ask("Chiller thay đổi bao nhiêu?", _compare_store())
    assert reply["answer"] == correct
    assert reply["verification"]["status"] == "verified"
    assert reply["verification"]["retries"] == 0
    assert reply["verification"]["missing_citations"] == []
    assert len(models.calls) == 2


@pytest.mark.parametrize(("draft", "problem"), [
    ("Điện chiller tăng 999 kWh [run_id: 1].", "999"),
    ("Điện chiller tăng 60 kWh.", "60"),
])
def test_verification_repairs_with_feedback_outside_the_tool_budget(tmp_path, monkeypatch, draft, problem) -> None:
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "runs.db")
    monkeypatch.setattr(agent, "MAX_TOOL_ROUNDS", 2)
    monkeypatch.setattr(agent, "MAX_TOOL_CALLS", 1)
    correct = "Điện chiller tăng 60 kWh [run_id: 1]."
    models = _mock_gemini(monkeypatch, [_compare_call(), _text_response(draft), _text_response(correct)])
    reply = agent.ask("Chiller thay đổi bao nhiêu?", _compare_store())
    assert reply["answer"] == correct
    assert reply["run_ids"] == [1]
    assert reply["verification"]["status"] == "repaired"
    assert reply["verification"]["retries"] == 1
    assert reply["verification"]["unmatched_numbers"] == []
    assert len(models.calls) == 3
    repair = models.calls[-1]
    assert repair["config"].tools is None
    feedback = repair["contents"][-1].parts[0].text
    assert problem in feedback and "[run_id:" in feedback
    assert db.get_run(2) is None


def test_verification_returns_last_answer_and_issues_after_two_retries(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "runs.db")
    last = "Chiller tăng 903 kWh [run_id: 1]. Điện tổng tăng 701 kWh."
    models = _mock_gemini(monkeypatch, [
        _compare_call(), _text_response("901 kWh [run_id: 1]."),
        _text_response("902 kWh [run_id: 1]."), _text_response(last),
    ])
    reply = agent.ask("So sánh tiêu thụ.", _compare_store())
    assert reply["answer"] == last
    check = reply["verification"]
    assert check["status"] == "warning"
    assert check["verified"] is False and check["retries"] == 2
    assert "903" in [item["text"] for item in check["unmatched_numbers"]]
    assert check["missing_citations"] == ["701"]
    assert "902" in models.calls[-1]["contents"][-1].parts[0].text
    assert len(models.calls) == 4 and reply["run_ids"] == [1]


def test_verification_uses_persisted_payload_and_never_executes_a_repair_tool_call(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "runs.db")
    private = db.log_run("dataset_summary", {}, {"total_kwh": 87654}, scope_id="other")
    same_scope_old = db.log_run("dataset_summary", {}, {"total_kwh": 76543}, scope_id="active")
    calls = []

    def simulated_tool(name, args, store):
        calls.append(name)
        run_id = db.log_run(name, args, {"total_kwh": 500}, scope_id=store["run_scope_id"])
        return {"run_id": run_id, "result": {"total_kwh": 999}}

    monkeypatch.setattr(agent, "run_tool", simulated_tool)
    models = _mock_gemini(monkeypatch, [
        _function_response("dataset_summary", {}),
        _text_response(f"999 kWh [run_id: 3]. 87654 kWh [run_id: {private}]. [run_id: {same_scope_old}]"),
        _function_response("data_quality", {}), _text_response("500 kWh [run_id: 3]."),
    ])
    reply = agent.ask("Tổng điện?", {**_compare_store(), "run_scope_id": "active"})
    assert calls == ["dataset_summary"]
    assert reply["verification"]["status"] == "repaired"
    assert reply["verification"]["retries"] == 2 and reply["run_ids"] == [3]
    feedback = models.calls[2]["contents"][-1].parts[0].text
    assert "999" in feedback and "500" in feedback
    assert "76543" not in feedback


def test_repair_service_failure_preserves_the_last_checked_draft(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "runs.db")
    draft = "Chiller tăng 902 kWh [run_id: 1]."
    _mock_gemini(monkeypatch, [_compare_call(), _text_response(draft), RuntimeError("offline")])
    reply = agent.ask("Chiller thay đổi bao nhiêu?", _compare_store())
    assert reply["answer"] == draft
    check = reply["verification"]
    assert check["status"] == "warning" and check["retries"] == 1
    assert check["issues"] == [{"quantity": "902 kWh", "run_ids": [1]}]
    assert "gián đoạn" in check["detail"]


def test_qualitative_repair_has_no_green_verification_badge(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "runs.db")
    _mock_gemini(monkeypatch, [
        _compare_call(), _text_response("999 kWh [run_id: 1]."), _text_response("Chưa đủ bằng chứng."),
    ])
    reply = agent.ask("Chiller thay đổi bao nhiêu?", _compare_store())
    assert reply["verification"]["status"] == "not_applicable"
    assert reply["verification"]["retries"] == 1
