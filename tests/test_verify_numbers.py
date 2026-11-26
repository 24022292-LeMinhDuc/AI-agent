"""Kiểm tra đối chiếu số với kết quả run giả lập."""

from pathlib import Path

import db
from scripts.verify_numbers import verify_answer


def test_verify_answer_accepts_numbers_found_in_cited_run(
    tmp_path: Path, monkeypatch
) -> None:
    """Số trong câu trả lời khớp kết quả run được trích dẫn."""
    db_path = tmp_path / "runs.db"
    monkeypatch.setattr(db, "DB_PATH", db_path)
    run_id = db.log_run(
        "opportunity",
        {},
        {"saved_kwh": 1000.0, "roi_pct": 200.0, "period": "2025-07"},
    )

    result = verify_answer(
        f"Tiết kiệm 1,000 kWh, ROI 200% vào tháng 7 [run_id: {run_id}].", db_path
    )

    assert result["verified"] is True
    assert result["unmatched_numbers"] == []


def test_verify_answer_reports_unsupported_number(
    tmp_path: Path, monkeypatch
) -> None:
    """Một số không có trong kết quả run được báo là không khớp."""
    db_path = tmp_path / "runs.db"
    monkeypatch.setattr(db, "DB_PATH", db_path)
    run_id = db.log_run("tool", {}, {"saved_kwh": 1000.0})

    result = verify_answer(f"Tiết kiệm 1,050 kWh [run_id={run_id}].", db_path)

    assert result["verified"] is False
    assert result["unmatched_numbers"] == [{"text": "1,050", "matched": False}]
