"""Kiểm tra đối chiếu số với kết quả run giả lập."""

from pathlib import Path
import subprocess
import sys

import db
import pytest
from scripts.verify_numbers import verify_answer, verify_results


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


def test_verifies_display_precision_units_and_localized_numbers() -> None:
    from verification import verify_results as shared_verifier

    assert verify_results is shared_verifier
    runs = {12: {"total_kwh": 1234.5674, "cost_vnd": 2712345, "share": 0.123456,
                 "roi_pct": 8.756, "scope2_tco2e": 1.23456}}
    answer = ("Tổng điện **1,234.57** kWh [run_id: 12]. Chi phí 2.712.345 VNĐ [run_id: 12]. "
              "Tỷ trọng 12,35%, ROI 8.76% [run_id: 12]. Phát thải 1,235 tCO₂e [run_id: 12].")
    assert verify_results(answer, runs)["verified"]
    assert verify_results("Tổng điện 1.234,57 kWh [run_id: 12].", runs)["verified"]
    for unsupported in ("1,234.58 kWh", "12,34%", "12,35 kWh", "2.712.345 tCO2e"):
        assert not verify_results(f"{unsupported} [run_id: 12].", runs)["verified"]


def test_verifies_numbers_against_their_own_citation() -> None:
    runs = {1: {"kwh": 1200}, 2: {"kwh": 800}}
    mistaken = verify_results("1. Điện 800 kWh [run_id: 1].\n2. Điện 1,200 kWh [run_id: 2].", runs)
    assert not mistaken["verified"]
    assert [item["text"] for item in mistaken["unmatched_numbers"]] == ["800", "1,200"]
    assert mistaken["missing_citations"] == []
    partial = verify_results("Điện 800 kWh [run_id: 2]. Điện kỳ trước 1200 kWh.", runs)
    assert not partial["verified"]
    assert partial["missing_citations"] == ["1200"]
    assert verify_results("Điện 1200 kWh [run_id: 1], kỳ sau 800 kWh [run_id: 2].", runs)["verified"]


def test_a_meter_reading_cannot_be_verified_using_a_date_count_or_another_meter() -> None:
    runs = {7: {"total_kwh": 1200, "chiller_kwh": 800, "period": "2025-07",
                "energy_rows_by_column": {"kwh_chiller": 45}}}
    for wrong in ("Chiller dùng 1200 kWh", "Điện 2025 kWh", "Điện 45 kWh"):
        assert not verify_results(f"{wrong} [run_id: 7].", runs)["verified"]
    assert verify_results("Chiller dùng 800 kWh [run_id: 7].", runs)["verified"]


@pytest.mark.parametrize(("answer", "payload", "verified"), [
    ("Scope 2 là 0,25 tCO₂e", {"scope2_tco2e": 0.25}, True),
    ("Điện .5 kWh", {"kwh": 5}, False),
    ("Điện 1,000 kWh", {"kwh": 1}, False),
    ("Điện 45 kWh", {"temperature_c": 45}, False),
    ("Điện 500 kWh", {"value": 500, "unit": "VNĐ"}, False),
    ("Điện 1 kWh", {"note": "1e9999999 kWh"}, False),
])
def test_numbers_and_units_cannot_be_misread(answer, payload, verified) -> None:
    assert verify_results(f"{answer} [run_id: 1]", {1: payload})["verified"] is verified


@pytest.mark.parametrize("from_file", [True, False])
def test_cli_runs_from_another_directory(tmp_path: Path, monkeypatch, from_file: bool) -> None:
    path = tmp_path / "runs.db"
    monkeypatch.setattr(db, "DB_PATH", path)
    run_id = db.log_run("tool", {}, {"total_kwh": 1234.567})
    answer = f"1,234.57 kWh [run_id: {run_id}]"
    script = Path(__file__).resolve().parents[1] / "scripts" / "verify_numbers.py"
    arguments = [sys.executable, "-X", "utf8", "-I", str(script), "--db", str(path)]
    if from_file:
        source = tmp_path / "answer.txt"
        source.write_text(answer, encoding="utf-8")
        arguments.extend(["--file", str(source)])
    result = subprocess.run(arguments, cwd=tmp_path, input=answer, capture_output=True, text=True, encoding="utf-8")
    assert result.returncode == 0, result.stderr
    assert "Số không khớp: 0" in result.stdout
