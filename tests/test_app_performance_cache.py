"""Kiểm tra tái sử dụng phép tính trong các lần rerun của giao diện."""

from __future__ import annotations

from pathlib import Path

import pandas as pd
import plotly.subplots
import pytest
from streamlit.testing.v1 import AppTest

import agent
import db
import mapping
import tools


@pytest.mark.parametrize("has_total_meter", [True, False])
def test_analysis_and_mapping_are_reused_across_app_reruns(
    monkeypatch, tmp_path: Path, has_total_meter: bool,
) -> None:
    """Một file và cùng lựa chọn giao diện chỉ chạy tính toán một lần."""
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "runs.db")
    counts = {
        name: 0
        for name in (
            "suggest_mapping",
            "detect_unit_issues",
            "aggregate_consumption",
            "seu_pareto",
            "total_kwh",
            "enpi_regression",
            "make_subplots",
        )
    }

    for module, names in (
        (mapping, ("suggest_mapping", "detect_unit_issues")),
        (tools, ("aggregate_consumption", "seu_pareto", "total_kwh", "enpi_regression")),
        (plotly.subplots, ("make_subplots",)),
    ):
        for name in names:
            original = getattr(module, name)

            def counted(*args, _name=name, _original=original, **kwargs):
                counts[_name] += 1
                return _original(*args, **kwargs)

            monkeypatch.setattr(module, name, counted)

    sample = Path(__file__).parents[1] / "data" / "sample_a.csv"
    content = sample.read_bytes()
    if not has_total_meter:
        readings = pd.read_csv(sample).drop(columns="Tong_dien_tieu_thu_kWh")
        content = readings.to_csv(index=False).encode("utf-8")
    app = AppTest.from_file(str(Path(__file__).parents[1] / "app.py")).run()
    app.file_uploader[0].set_value((sample.name, content, "text/csv"))
    app.run()
    fingerprint = app.session_state["store"]["upload_fingerprint"]
    app.button(key=f"confirm_{fingerprint}").click()
    app.run()

    # Rerun Data to verify mapping suggestion and warning analysis stay in-session.
    app.run()
    for page in ("Dashboard", "SEU & EnPI"):
        nav_button = next(
            button
            for button in app.button
            if button.key and button.key.startswith("nav_") and page in button.key
        )
        nav_button.click()
        app.run()
        app.run()

    monkeypatch.setenv("GEMINI_API_KEY", "mock-key")
    monkeypatch.setattr(
        agent,
        "ask",
        lambda question, store, history=(): {
            "answer": "Đã phân tích mẫu.",
            "run_ids": [],
        },
    )
    app.button(key="nav_5_Hỏi đáp").click().run()
    app.button(key="sample_question_0").click()
    app.run()

    assert not app.exception
    assert len(app.session_state["store"]["chat_history"]) == 2
    assert any("Đã phân tích mẫu." in markdown.value for markdown in app.markdown)
    assert counts == {
        "suggest_mapping": 1,
        "detect_unit_issues": 1,
        "aggregate_consumption": 1,
        "seu_pareto": 1,
        "total_kwh": int(not has_total_meter),
        "enpi_regression": 1,
        "make_subplots": 2,
    }
