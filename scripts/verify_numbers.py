"""Đối chiếu số trong câu trả lời với kết quả các run được trích dẫn."""

from __future__ import annotations

import argparse
import json
import math
import re
import sqlite3
import sys
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB_PATH = PROJECT_ROOT / "runs.db"
NUMBER_PATTERN = re.compile(r"(?<![\w])[-+]?\d(?:[\d.,]*\d)?")
BRACKET_RUN_PATTERN = re.compile(
    r"\[\s*(?:run_id\s*[:=#]?\s*)?(\d{1,19})\s*\]", re.IGNORECASE
)
LABELED_RUN_PATTERN = re.compile(r"(?<!\w)run_id\s*[:=#]\s*(\d{1,19})(?!\d)", re.IGNORECASE)


def _parse_number_candidates(raw_value: str, is_percent: bool) -> set[float]:
    """Phân tích số với dấu thập phân hoặc phân tách hàng nghìn."""
    value = raw_value.replace(" ", "")
    candidates: set[float] = set()
    if not value:
        return candidates

    normalized_variants: set[str] = set()
    if "," in value and "." in value:
        if value.rfind(",") > value.rfind("."):
            normalized_variants.add(value.replace(".", "").replace(",", "."))
        else:
            normalized_variants.add(value.replace(",", ""))
    elif "," in value or "." in value:
        separator = "," if "," in value else "."
        left, *right_parts = value.split(separator)
        right = right_parts[-1] if right_parts else ""
        if len(right_parts) > 1 and all(len(part) == 3 for part in right_parts):
            normalized_variants.add(value.replace(separator, ""))
        elif len(right) == 3 and len(left.lstrip("+-")) <= 3 and left.lstrip("+-") != "0":
            normalized_variants.add(value.replace(separator, ""))
            normalized_variants.add(value.replace(separator, ".", 1))
        else:
            normalized_variants.add(value.replace(separator, ".", 1))
    else:
        normalized_variants.add(value)

    for normalized in normalized_variants:
        try:
            number = float(normalized)
        except ValueError:
            continue
        if math.isfinite(number):
            candidates.add(number)
            if is_percent:
                candidates.add(number / 100)
    return candidates


def _numbers_in_text(text: str) -> list[tuple[str, set[float]]]:
    """Trích các số cùng cách hiểu có thể có từ văn bản."""
    found: list[tuple[str, set[float]]] = []
    for match in NUMBER_PATTERN.finditer(text):
        suffix = text[match.end() :]
        is_percent = bool(re.match(r"\s*%", suffix))
        candidates = _parse_number_candidates(match.group(), is_percent)
        if candidates:
            found.append((match.group(), candidates))
    return found


def _collect_result_numbers(value: Any, numbers: set[float]) -> None:
    """Thu thập số từ kết quả JSON đã lưu trong runs.db."""
    if isinstance(value, bool) or value is None:
        return
    if isinstance(value, (int, float)):
        if math.isfinite(float(value)):
            numbers.add(float(value))
        return
    if isinstance(value, dict):
        for key, item in value.items():
            if isinstance(key, str):
                for _, candidates in _numbers_in_text(key):
                    numbers.update(candidates)
            _collect_result_numbers(item, numbers)
        return
    if isinstance(value, (list, tuple)):
        for item in value:
            _collect_result_numbers(item, numbers)
        return
    if isinstance(value, str):
        for _, candidates in _numbers_in_text(value):
            numbers.update(candidates)


def _extract_run_ids(answer: str) -> tuple[list[int], str]:
    """Lấy citation run_id và loại chúng khỏi phần cần kiểm tra số."""
    matches: list[tuple[int, int, int]] = []
    for pattern in (BRACKET_RUN_PATTERN, LABELED_RUN_PATTERN):
        matches.extend(
            (match.start(), match.end(), int(match.group(1)))
            for match in pattern.finditer(answer)
        )
    matches.sort(key=lambda item: item[0])

    run_ids: list[int] = []
    spans: list[tuple[int, int]] = []
    for start, end, run_id in matches:
        if spans and start < spans[-1][1]:
            continue
        spans.append((start, end))
        if run_id not in run_ids:
            run_ids.append(run_id)

    stripped = answer
    for start, end in reversed(spans):
        stripped = stripped[:start] + " " + stripped[end:]
    return run_ids, stripped


def _read_run_results(db_path: Path, run_ids: list[int]) -> dict[int, Any]:
    """Đọc kết quả run chỉ ở chế độ read-only."""
    if not run_ids or not db_path.exists():
        return {}
    uri = db_path.resolve().as_uri() + "?mode=ro"
    try:
        connection = sqlite3.connect(uri, uri=True)
    except sqlite3.Error:
        return {}
    try:
        results: dict[int, Any] = {}
        for run_id in run_ids:
            if run_id > 2**63 - 1:
                continue
            row = connection.execute(
                "SELECT result_json FROM runs WHERE run_id = ?", (run_id,)
            ).fetchone()
            if row is not None:
                results[run_id] = json.loads(row[0])
        return results
    except (sqlite3.Error, json.JSONDecodeError):
        return {}
    finally:
        connection.close()


def verify_answer(answer: str, db_path: Path | str = DEFAULT_DB_PATH) -> dict[str, Any]:
    """Kiểm tra số trong câu trả lời với kết quả các run được trích dẫn."""
    run_ids, _ = _extract_run_ids(answer)
    return verify_results(answer, _read_run_results(Path(db_path), run_ids))


def verify_results(answer: str, results: dict[int, Any]) -> dict[str, Any]:
    """Đối chiếu với kết quả mà người gọi được phép sử dụng."""
    run_ids, answer_without_citations = _extract_run_ids(answer)
    missing_run_ids = [run_id for run_id in run_ids if run_id not in results]
    supported_numbers: set[float] = set()
    for run_id in run_ids:
        if run_id in results:
            _collect_result_numbers(results[run_id], supported_numbers)

    checked_numbers: list[dict[str, Any]] = []
    mismatches: list[dict[str, Any]] = []
    for raw_value, candidates in _numbers_in_text(answer_without_citations):
        matched = any(
            math.isclose(candidate, supported, rel_tol=1e-9, abs_tol=1e-9)
            for candidate in candidates
            for supported in supported_numbers
        )
        check = {"text": raw_value, "matched": matched}
        checked_numbers.append(check)
        if not matched:
            mismatches.append(check)

    verified = bool(run_ids) and not missing_run_ids and not mismatches
    return {
        "verified": verified,
        "run_ids": run_ids,
        "missing_run_ids": missing_run_ids,
        "numbers_checked": checked_numbers,
        "unmatched_numbers": mismatches,
    }


def main(argv: list[str] | None = None) -> int:
    """Chạy bộ kiểm chứng từ đối số, file hoặc stdin."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("answer", nargs="?", help="Câu trả lời cần kiểm tra")
    parser.add_argument("--file", type=Path, help="Đọc câu trả lời từ file UTF-8")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH, help="Đường dẫn runs.db")
    args = parser.parse_args(argv)
    if args.answer and args.file:
        parser.error("Chỉ dùng một trong answer hoặc --file.")
    if args.file:
        answer = args.file.read_text(encoding="utf-8")
    elif args.answer:
        answer = args.answer
    else:
        answer = sys.stdin.read()

    result = verify_answer(answer, args.db)
    print(f"run_id được trích dẫn: {', '.join(map(str, result['run_ids'])) or '(không có)'}")
    print(f"Số đã kiểm tra: {len(result['numbers_checked'])}")
    print(f"Số không khớp: {len(result['unmatched_numbers'])}")
    for item in result["unmatched_numbers"]:
        print(f"- Không tìm thấy trong kết quả công cụ: {item['text']}")
    if result["missing_run_ids"]:
        print(f"run_id không tìm thấy: {', '.join(map(str, result['missing_run_ids']))}")
    return 0 if result["verified"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
