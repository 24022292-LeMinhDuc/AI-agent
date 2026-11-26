"""Kiểm tra đường cơ sở điện năng chuẩn hóa với dữ liệu biết trước."""

from __future__ import annotations

import json

import numpy as np
import pandas as pd
import pytest

from energy_baseline import normalized_baseline


def test_normalizes_energy_to_reporting_conditions_and_compares_same_days() -> None:
    """Các điều kiện hai kỳ khác nhau vẫn thu được chênh lệch biết trước."""
    production = np.array([5, 3, 4, 5, 7, 6, 5, 6, 3], dtype=float)
    cooling = np.array([0, 1, 3, 2, 4, 1, 2, 3, 0], dtype=float)
    electricity = 30 + 2 * production + 4 * cooling
    saved_kwh = np.array([0, 0, 0, 0, 0, 0, 6, 6, 6], dtype=float)
    original = pd.DataFrame(
        {
            "timestamp": pd.date_range("2025-01-26", periods=len(electricity)),
            "production_t": production,
            "cdd": cooling,
            "kwh_production": electricity * 0.75 - saved_kwh,
            "kwh_chiller": electricity * 0.25,
        }
    )

    result = normalized_baseline(
        original,
        ["kwh_production", "kwh_chiller"],
        baseline_start="2025-01-26",
        baseline_end="2025-01-31",
        reporting_start="2025-02-01",
        reporting_end="2025-02-03",
        drivers=["production_t", "cdd"],
    )

    assert result["model"]["coefficients"] == pytest.approx(
        {"intercept": 30, "production_t": 2, "cdd": 4}
    )
    assert result["model"]["r2"] == pytest.approx(1)
    assert result["model"]["adjusted_r2"] == pytest.approx(1)
    assert result["reporting"]["eligible_rows"] == 3
    assert result["reporting"]["coverage_pct"] == 100
    expected = electricity[6:].sum()
    assert result["summary"] == pytest.approx(
        {
            "actual_kwh": expected - 18,
            "expected_kwh": expected,
            "savings_kwh": 18,
            "savings_pct": 100 * 18 / expected,
        }
    )
    assert result["monthly"][0]["savings_kwh"] == pytest.approx(18)
    assert result["warnings"] == []
    assert "actual_kwh" not in original
    json.dumps(result, allow_nan=False)


def test_uses_residual_degrees_of_freedom_for_fit_diagnostics() -> None:
    """SSE 12, mean 17 và SST 82 cho số liệu nhiễu trực giao với biến giải thích."""
    production = np.array([1, 2, 3, 4, 5, 6, 4], dtype=float)
    residual = np.array([1, -2, 1, 1, -2, 1, 0], dtype=float)
    source = pd.DataFrame(
        {
            "timestamp": pd.date_range("2025-01-01", periods=len(production)),
            "production_t": production,
            "kwh_production": 10 + 2 * production + residual,
        }
    )

    result = normalized_baseline(
        source,
        ["kwh_production"],
        baseline_start="2025-01-01",
        baseline_end="2025-01-06",
        reporting_start="2025-01-07",
        reporting_end="2025-01-07",
        drivers=["production_t"],
    )

    model = result["model"]
    assert model["degrees_of_freedom"] == 4
    assert model["r2"] == pytest.approx(1 - 12 / 82)
    assert model["adjusted_r2"] == pytest.approx(1 - (12 / 82) * 5 / 4)
    assert model["cvrmse_pct"] == pytest.approx(np.sqrt(12 / 4) / 17 * 100)
    assert model["nmbe_pct"] == pytest.approx(0, abs=1e-10)
    assert result["summary"]["savings_kwh"] == pytest.approx(0, abs=1e-10)


def test_drops_incomplete_and_conflicting_measurements_before_modeling() -> None:
    dates = [
        "2025-01-01", "2025-01-02", "2025-01-02", "2025-01-03", "2025-01-03",
        "2025-01-05", "2025-02-02", "2025-02-03", "2025-02-04", "invalid",
    ]
    source = pd.DataFrame(
        {
            "timestamp": dates,
            "production_t": [1, 2, 2, 3, 3, 4, 3, 4, 3, 3],
            "kwh_production": [11, 12, 12, 13, 99, 14, 10, 5, 5, 8],
            "kwh_chiller": [1, 2, 2, 3, 3, 4, 2, None, -1, 2],
        }
    )

    result = normalized_baseline(
        source,
        ["kwh_production", "kwh_chiller"],
        baseline_start="2025-01-01",
        baseline_end="2025-01-05",
        reporting_start="2025-02-01",
        reporting_end="2025-02-04",
        drivers=["production_t"],
    )

    baseline = result["baseline"]
    reporting = result["reporting"]
    assert baseline["expected_rows"] == 5
    assert baseline["observed_rows"] == 4
    assert baseline["eligible_rows"] == 3
    assert baseline["duplicate_rows_removed"] == 1
    assert baseline["conflicting_timestamp_rows"] == 2
    assert baseline["missing_rows"] == baseline["incomplete_rows"] == 1
    assert result["invalid_timestamp_rows"] == 1
    assert reporting["expected_rows"] == 4
    assert reporting["observed_rows"] == 3
    assert reporting["incomplete_rows"] == 2
    assert reporting["eligible_rows"] == 1
    assert reporting["coverage_pct"] == 25
    assert result["summary"]["actual_kwh"] == 12
    assert result["summary"]["expected_kwh"] == pytest.approx(16)


def test_excludes_unseen_drivers_and_negative_predictions() -> None:
    source = pd.DataFrame(
        {
            "timestamp": [*pd.date_range("2025-01-01", periods=8), pd.Timestamp("2025-02-03")],
            "production_t": [0, 1, 2, 3, 4, 0, 5, 2, None],
            "kwh_production": [0, 0, 0, 0, 100, 8, 15, 26, None],
        }
    )

    result = normalized_baseline(
        source,
        ["kwh_production"],
        baseline_start="2025-01-01",
        baseline_end="2025-01-05",
        reporting_start="2025-01-06",
        reporting_end="2025-02-03",
        drivers=["production_t"],
    )

    assert result["reporting"]["eligible_rows"] == 1
    assert result["reporting"]["out_of_range_rows"] == 1
    assert result["reporting"]["negative_prediction_rows"] == 1
    assert result["summary"]["expected_kwh"] == pytest.approx(20)
    assert result["summary"]["savings_kwh"] == pytest.approx(-6)
    assert result["monthly"][1]["month"] == "2025-02"
    assert result["monthly"][1]["eligible_rows"] == 0
    assert result["monthly"][1]["actual_kwh"] is None


def test_keeps_no_comparable_day_distinct_from_zero_energy() -> None:
    source = pd.DataFrame(
        {
            "timestamp": pd.date_range("2025-01-01", periods=5),
            "production_t": [1, 2, 3, 2, 8],
            "kwh_production": [0, 0, 0, 2, 50],
        }
    )
    kwargs = {
        "baseline_start": "2025-01-01",
        "baseline_end": "2025-01-03",
        "drivers": ["production_t"],
    }

    zero = normalized_baseline(
        source, ["kwh_production"], reporting_start="2025-01-04", reporting_end="2025-01-04",
        **kwargs,
    )
    empty = normalized_baseline(
        source, ["kwh_production"], reporting_start="2025-01-05", reporting_end="2025-01-05",
        **kwargs,
    )

    assert zero["model"]["r2"] is None
    assert zero["model"]["cvrmse_pct"] is None
    assert zero["summary"] == {
        "actual_kwh": 2.0, "expected_kwh": 0.0, "savings_kwh": -2.0, "savings_pct": None,
    }
    assert empty["reporting"]["eligible_rows"] == 0
    assert all(value is None for value in empty["summary"].values())


@pytest.mark.parametrize(
    ("change", "expected"),
    [
        ("rank", "phụ thuộc tuyến tính"),
        ("samples", "ít nhất 4 ngày hợp lệ"),
        ("constant", "có biến giữ nguyên"),
        ("overlap", "kết thúc trước ngày bắt đầu"),
        ("calendar", "YYYY-MM-DD hợp lệ"),
        ("outside", "phạm vi ngày của dữ liệu nguồn"),
        ("subdaily", "nhiều mốc đo riêng biệt"),
    ],
)
def test_rejects_unsuitable_periods_or_models(change: str, expected: str) -> None:
    source = pd.DataFrame(
        {
            "timestamp": list(pd.date_range("2025-01-01", periods=7).astype(str)),
            "production_t": [1, 2, 3, 4, 5, 4, 3],
            "cdd": [1, 2, 1, 2, 3, 2, 1],
            "kwh_production": [11, 12, 13, 14, 15, 13, 13],
        }
    )
    args = {
        "baseline_start": "2025-01-01",
        "baseline_end": "2025-01-05",
        "reporting_start": "2025-01-06",
        "reporting_end": "2025-01-07",
        "drivers": ["production_t", "cdd"],
    }
    if change == "rank":
        source["cdd"] = source["production_t"] * 2
    elif change == "samples":
        args["baseline_end"] = "2025-01-03"
    elif change == "constant":
        source["cdd"] = 0
    elif change == "overlap":
        args["reporting_start"] = "2025-01-05"
    elif change == "calendar":
        args["reporting_end"] = "2025-02-30"
    elif change == "outside":
        args["reporting_end"] = "2030-01-01"
    elif change == "subdaily":
        source.loc[5, "timestamp"] = "2025-01-07T01:00:00+07:00"

    with pytest.raises(ValueError, match=expected):
        normalized_baseline(source, ["kwh_production"], **args)
