"""Đo thời gian và số phép tính qua luồng Streamlit mẫu."""

from __future__ import annotations

import json
import os
import sys
import tempfile
import time
import tracemalloc
from pathlib import Path
from typing import Any, Callable

import plotly.subplots
from streamlit.testing.v1 import AppTest

ROOT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT_DIR))

import db  # noqa: E402
import mapping  # noqa: E402
import tools  # noqa: E402


def _timed(phases: dict[str, float], name: str, action: Callable[[], Any]) -> Any:
    """Chạy một bước và lưu thời gian thực thi tính bằng giây."""
    started = time.perf_counter()
    result = action()
    phases[name] = round(time.perf_counter() - started, 4)
    return result


def main() -> None:
    """Đo luồng upload, xác nhận, dashboard và SEU/EnPI."""
    root = ROOT_DIR
    original_db_path = db.DB_PATH
    original_api_key = os.environ.pop("GEMINI_API_KEY", None)
    originals: dict[tuple[object, str], Any] = {}
    calls = {
        name: {"calls": 0, "seconds": 0.0}
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
    with tempfile.TemporaryDirectory(prefix="vee-benchmark-") as temp_dir:
        db.DB_PATH = Path(temp_dir) / "runs.db"
        try:
            for module, names in (
                (mapping, ("suggest_mapping", "detect_unit_issues")),
                (tools, ("aggregate_consumption", "seu_pareto", "total_kwh", "enpi_regression")),
                (plotly.subplots, ("make_subplots",)),
            ):
                for name in names:
                    original = getattr(module, name)
                    originals[(module, name)] = original

                    def counted(*args, _name=name, _original=original, **kwargs):
                        started = time.perf_counter()
                        calls[_name]["calls"] += 1
                        try:
                            return _original(*args, **kwargs)
                        finally:
                            calls[_name]["seconds"] += time.perf_counter() - started

                    setattr(module, name, counted)

            phases: dict[str, float] = {}
            tracemalloc.start()
            app = _timed(
                phases,
                "apptest_initial_render",
                lambda: AppTest.from_file(str(root / "app.py")).run(),
            )
            sample = root / "data" / "sample_a.csv"
            _timed(
                phases,
                "upload_and_mapping_suggestion",
                lambda: (
                    app.file_uploader[0].set_value(
                        (sample.name, sample.read_bytes(), "text/csv")
                    ),
                    app.run(),
                ),
            )
            fingerprint = app.session_state["store"]["upload_fingerprint"]
            app.button(key=f"confirm_{fingerprint}").click()
            _timed(phases, "mapping_confirmation_and_clean", app.run)

            for page, phase in (("Dashboard", "dashboard"), ("SEU & EnPI", "seu_enpi")):
                nav_button = next(
                    button
                    for button in app.button
                    if button.key and button.key.startswith("nav_") and page in button.key
                )
                nav_button.click()
                _timed(phases, f"{phase}_first_render", app.run)
                _timed(phases, f"{phase}_same_page_rerun", app.run)

            current_bytes, peak_bytes = tracemalloc.get_traced_memory()
            tracemalloc.stop()
            print(
                json.dumps(
                    {
                        "phases_seconds": phases,
                        "deterministic_function_calls": calls,
                        "rows": len(app.session_state["store"]["df"]),
                        "python_allocations_mib": {
                            "current": round(current_bytes / 1024**2, 2),
                            "peak": round(peak_bytes / 1024**2, 2),
                        },
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            )
        finally:
            for (module, name), original in originals.items():
                setattr(module, name, original)
            db.DB_PATH = original_db_path
            if original_api_key is not None:
                os.environ["GEMINI_API_KEY"] = original_api_key


if __name__ == "__main__":
    main()
