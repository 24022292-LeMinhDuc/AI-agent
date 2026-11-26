"""Kiểm tra ghi vết SQLite và cổng kiểm tra schema của công cụ."""

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path

import pandas as pd
import pytest

import db
from agent import run_tool


def test_database_logs_runs_and_decisions(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Kiểm tra lưu, đọc lần chạy và ghi quyết định."""
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "runs.db")

    run_id = db.log_run("data_quality", {}, {"kwh_lighting": {"removed": 0}})
    run = db.get_run(run_id)
    db.save_decision("Tối ưu chiller", "Approve")
    decisions = db.list_decisions()

    assert run is not None
    assert run["run_id"] == run_id
    assert run["tool"] == "data_quality"
    assert run["args"] == {}
    assert run["result"] == {"kwh_lighting": {"removed": 0}}
    assert decisions[0]["opportunity_name"] == "Tối ưu chiller"
    assert decisions[0]["status"] == "Approve"
    assert decisions[0]["ts"]


def test_decisions_are_isolated_by_scope(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Quyết định của session khác không xuất hiện trong scope hiện tại."""
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "runs.db")

    db.save_decision("Chiller", "Approve", scope_id="session-a")
    db.save_decision("Chiller", "Reject", scope_id="session-b")
    db.save_decision("Chiller", "Reject", scope_id="session-a")
    db.save_decision("Lighting", "Approve", scope_id="session-a")

    assert db.list_decisions("session-a")[0]["status"] == "Approve"
    assert db.list_decisions("session-b")[0]["status"] == "Reject"
    assert db.latest_decisions("session-a") == {"Chiller": "Reject", "Lighting": "Approve"}
    assert db.latest_decisions("session-b") == {"Chiller": "Reject"}
    assert db.latest_decisions("unknown") == {}


def test_existing_database_is_migrated_once_with_parallel_writers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Nâng cấp cơ sở dữ liệu cũ đồng thời và giữ nguyên lịch sử."""
    db_path = tmp_path / "runs.db"
    monkeypatch.setattr(db, "DB_PATH", db_path)
    with closing(sqlite3.connect(db_path)) as connection, connection:
        connection.execute(
            "CREATE TABLE runs (run_id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL, "
            "tool TEXT NOT NULL, args_json TEXT NOT NULL, result_json TEXT NOT NULL)"
        )
        connection.execute(
            "INSERT INTO runs (ts, tool, args_json, result_json) VALUES (?, ?, ?, ?)",
            ("2025-01-01T00:00:00+00:00", "legacy", "{}", '{"ok": true}'),
        )
        connection.execute(
            "CREATE TABLE decisions (opportunity_name TEXT NOT NULL, status TEXT NOT NULL, ts TEXT NOT NULL)"
        )
        connection.execute(
            "INSERT INTO decisions VALUES (?, ?, ?)",
            ("Legacy", "Approve", "2025-01-01T00:00:00+00:00"),
        )

    def log_scoped_run(scope_id: str) -> int:
        return db.log_run("new", {}, {"ok": True}, scope_id=scope_id)

    scopes = [f"scope-{number}" for number in range(8)]
    with ThreadPoolExecutor(max_workers=len(scopes)) as workers:
        new_runs = list(workers.map(log_scoped_run, scopes))

    assert db.get_run(1)["result"] == {"ok": True}
    assert db.get_run(1, scope_id=scopes[0]) is None
    assert db.get_runs([1, *new_runs, new_runs[0]], scope_id=scopes[0]).keys() == {new_runs[0]}
    assert db.get_runs(new_runs).keys() == set(new_runs)
    with closing(sqlite3.connect(db_path)) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION

    db.save_decision("New", "Reject", scope_id="scope-a")
    assert db.list_decisions()[0]["opportunity_name"] == "Legacy"
    assert db.list_decisions("scope-a")[0]["opportunity_name"] == "New"


def test_valid_tool_call_returns_run_id_and_is_logged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Kiểm tra tham số hợp lệ chạy công cụ và tạo bản ghi."""
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "runs.db")
    store = {
        "df": pd.DataFrame({"kwh_lighting": [100.0, 110.0]}),
        "config": {
            "tariff_vnd_per_kwh": 2_500,
            "emission_factor_kg_per_kwh": None,
            "opportunities": [
                {
                    "name": "LED",
                    "applies_to": "kwh_lighting",
                    "saving_pct": 0.12,
                    "capex_vnd": 30_000_000,
                }
            ]
        },
    }

    response = run_tool("data_quality", {}, store)
    saved_run = db.get_run(response["run_id"])

    assert response["run_id"]
    assert response["result"]["kwh_lighting"] == {
        "removed": 0,
        "interpolated": 0,
        "missing": 0,
    }
    assert saved_run is not None
    assert saved_run["tool"] == "data_quality"
    assert saved_run["result"] == response["result"]

    opportunities = run_tool("list_opportunities", {}, store)
    assert opportunities["result"][0]["assumption"].startswith("GIẢ ĐỊNH - cần kiểm chứng")


def test_dashboard_and_dataset_tools_use_confirmed_server_data(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Dashboard và AI metadata dùng chung dataset, ghi run_id truy vết."""
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "runs.db")
    store = {
        "mapping_confirmed": True,
        "df": pd.DataFrame(
            {
                "timestamp": pd.to_datetime(["2025-01-01", "2025-01-02", "2025-02-01"]),
                "production_t": [10.0, 12.0, 15.0],
                "temp_c": [20.0, 21.0, 25.0],
                "kwh_production": [100.0, 120.0, 150.0],
                "kwh_chiller": [50.0, 60.0, 80.0],
            }
        ),
        "config": {"tariff_vnd_per_kwh": 2_000},
    }

    summary = run_tool("dataset_summary", {}, store)
    dashboard = run_tool(
        "dashboard_data",
        {"level": "day", "start_date": "2025-01-02", "end_date": "2025-01-02"},
        store,
    )

    assert summary["result"]["total_kwh"] == 560.0
    assert summary["result"]["date_start"] == "2025-01-01"
    assert dashboard["run_id"]
    assert len(dashboard["result"]["aggregated"]) == 1
    assert dashboard["result"]["summary"]["total_kwh"] == 180.0
    assert dashboard["result"]["summary"]["estimated_cost_vnd"] == 360_000.0


@pytest.mark.parametrize(
    ("name", "args"),
    [
        ("data_quality", {"unexpected": True}),
        ("seu_analysis", {"threshold": "0.8"}),
        (
            "compare_periods",
            {"month_a": "2025-1", "month_b": "2025-02"},
        ),
    ],
)
def test_invalid_tool_arguments_are_rejected_without_new_run(
    name: str,
    args: dict[str, object],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Kiểm tra args thừa, sai kiểu và sai định dạng không được ghi."""
    db_path = tmp_path / "runs.db"
    monkeypatch.setattr(db, "DB_PATH", db_path)
    initial_run_id = db.log_run("seed", {}, {"ok": True})
    store = {"df": object(), "config": {}}

    response = run_tool(name, args, store)

    assert "error" in response
    with sqlite3.connect(db_path) as connection:
        run_count = connection.execute("SELECT COUNT(*) FROM runs").fetchone()[0]
    assert run_count == 1
    assert db.get_run(initial_run_id) is not None
