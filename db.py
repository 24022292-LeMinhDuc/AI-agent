"""Lưu lịch sử chạy công cụ và quyết định vào SQLite."""

from __future__ import annotations

import json
import math
import sqlite3
from collections.abc import Iterable
from contextlib import closing
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd


DB_PATH = Path(__file__).with_name("runs.db")
SCHEMA_VERSION = 1


def _json_safe(value: Any) -> Any:
    """Chuyển kết quả pandas và NumPy sang kiểu JSON."""
    if value is None or value is pd.NA or value is pd.NaT:
        return None
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    if isinstance(value, pd.DataFrame):
        return _json_safe(value.to_dict())
    if isinstance(value, pd.Series):
        return _json_safe(value.to_dict())
    if isinstance(value, np.ndarray):
        return _json_safe(value.tolist())
    if isinstance(value, np.generic):
        return _json_safe(value.item())
    if isinstance(value, (datetime, date, pd.Timestamp)):
        return value.isoformat()
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


def _ensure_schema(connection: sqlite3.Connection) -> None:
    """Nâng cấp cấu trúc trong một giao dịch, giữ lại lịch sử cũ."""
    if connection.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION:
        return

    with connection:
        connection.execute("BEGIN IMMEDIATE")
        version = connection.execute("PRAGMA user_version").fetchone()[0]
        if version == SCHEMA_VERSION:
            return
        if version != 0:
            raise sqlite3.NotSupportedError(f"Phiên bản SQLite của ứng dụng không được hỗ trợ: {version}.")
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS runs (
                run_id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts TEXT NOT NULL,
                tool TEXT NOT NULL,
                args_json TEXT NOT NULL,
                result_json TEXT NOT NULL,
                scope_id TEXT NOT NULL DEFAULT ''
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS decisions (
                opportunity_name TEXT NOT NULL,
                status TEXT NOT NULL,
                ts TEXT NOT NULL,
                scope_id TEXT NOT NULL DEFAULT ''
            )
            """
        )
        for table in ("runs", "decisions"):
            columns = {row["name"] for row in connection.execute(f"PRAGMA table_info({table})")}
            if "scope_id" not in columns:
                connection.execute(f"ALTER TABLE {table} ADD COLUMN scope_id TEXT NOT NULL DEFAULT ''")
        connection.execute(
            "CREATE INDEX IF NOT EXISTS decisions_scope_name ON decisions (scope_id, opportunity_name)"
        )
        connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")


def _connect() -> sqlite3.Connection:
    """Mở cơ sở dữ liệu và kiểm tra phiên bản cấu trúc."""
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(DB_PATH, timeout=10.0)
    connection.row_factory = sqlite3.Row
    try:
        _ensure_schema(connection)
    except Exception:
        connection.close()
        raise
    return connection


def log_run(tool: str, args: dict[str, Any], result: Any, *, scope_id: str = "") -> int:
    """Ghi một lần chạy theo phạm vi và trả về mã run_id."""
    args_json = json.dumps(_json_safe(args), ensure_ascii=False, allow_nan=False)
    result_json = json.dumps(_json_safe(result), ensure_ascii=False, allow_nan=False, separators=(",", ":"))
    with closing(_connect()) as connection, connection:
        cursor = connection.execute(
            "INSERT INTO runs (ts, tool, args_json, result_json, scope_id) VALUES (?, ?, ?, ?, ?)",
            (datetime.now(timezone.utc).isoformat(), tool, args_json, result_json, scope_id),
        )
        return int(cursor.lastrowid)


def get_runs(
    run_ids: Iterable[int], *, scope_id: str | None = None
) -> dict[int, dict[str, Any]]:
    """Lấy các lần chạy trong phạm vi chỉ định và giải mã kết quả JSON."""
    ids = tuple(dict.fromkeys(run_ids))
    if not ids:
        return {}

    placeholders = ", ".join("?" for _ in ids)
    query = f"SELECT run_id, ts, tool, args_json, result_json FROM runs WHERE run_id IN ({placeholders})"
    params: tuple[int | str, ...] = ids
    if scope_id is not None:
        query += " AND scope_id = ?"
        params += (scope_id,)
    with closing(_connect()) as connection:
        rows = connection.execute(query, params).fetchall()
    return {
        row["run_id"]: {
            **dict(row),
            "args": json.loads(row["args_json"]),
            "result": json.loads(row["result_json"]),
        }
        for row in rows
    }


def get_run(run_id: int, *, scope_id: str | None = None) -> dict[str, Any] | None:
    """Lấy một lần chạy theo mã và phạm vi chỉ định."""
    return get_runs((run_id,), scope_id=scope_id).get(run_id)


def save_decision(name: str, status: str, scope_id: str = "") -> None:
    """Ghi nhận quyết định theo người dùng và bộ dữ liệu hiện tại."""
    with closing(_connect()) as connection, connection:
        connection.execute(
            "INSERT INTO decisions (opportunity_name, status, ts, scope_id) VALUES (?, ?, ?, ?)",
            (name, status, datetime.now(timezone.utc).isoformat(), scope_id),
        )


def list_decisions(scope_id: str | None = None) -> list[dict[str, str]]:
    """Liệt kê quyết định theo phạm vi, hoặc tất cả nếu không chỉ định."""
    with closing(_connect()) as connection:
        if scope_id is None:
            rows = connection.execute(
                "SELECT opportunity_name, status, ts FROM decisions ORDER BY rowid"
            ).fetchall()
        else:
            rows = connection.execute(
                "SELECT opportunity_name, status, ts FROM decisions "
                "WHERE scope_id = ? ORDER BY rowid",
                (scope_id,),
            ).fetchall()
    return [dict(row) for row in rows]


def latest_decisions(scope_id: str = "") -> dict[str, str]:
    """Lấy quyết định mới nhất của mỗi cơ hội trong phạm vi."""
    with closing(_connect()) as connection:
        rows = connection.execute(
            """
            SELECT opportunity_name, status FROM decisions
            WHERE rowid IN (
                SELECT MAX(rowid) FROM decisions WHERE scope_id = ? GROUP BY opportunity_name
            )
            """,
            (scope_id,),
        ).fetchall()
    return {row["opportunity_name"]: row["status"] for row in rows}
