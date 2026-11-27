"""Đối chiếu số trong câu trả lời với kết quả các run được trích dẫn."""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path
from typing import Any

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from verification import extract_run_ids, verify_results


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB_PATH = PROJECT_ROOT / "runs.db"


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
    run_ids = extract_run_ids(answer)
    return verify_results(answer, _read_run_results(Path(db_path), run_ids))


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
