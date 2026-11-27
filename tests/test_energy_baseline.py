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


def _fit_periods(source: pd.DataFrame, n: int, **kwargs) -> dict:
    dates = source["timestamp"].dt.strftime("%Y-%m-%d")
    return normalized_baseline(
        source, ["kwh_total"],
        baseline_start=dates.iloc[0], baseline_end=dates.iloc[n - 1],
        reporting_start=dates.iloc[n], reporting_end=dates.iloc[-1],
        **kwargs,
    )


@pytest.mark.parametrize(
    ("model_type", "heating_base", "cooling_base"),
    [("3pc", None, 22), ("3ph", 19, None), ("4p", 20, 20), ("5p", 18, 24), ("auto", None, 22)],
)
def test_change_points_recover_known_breaks_and_operating_driver(
    model_type: str, heating_base: float | None, cooling_base: float | None,
) -> None:
    temp = np.tile(np.arange(10., 31., 0.5), 3)
    hours = 8 + np.arange(len(temp)) % 5
    energy = 50 + 2 * hours.astype(float)
    if heating_base is not None:
        energy += 3 * np.maximum(heating_base - temp, 0)
    if cooling_base is not None:
        energy += 4 * np.maximum(temp - cooling_base, 0)
    energy[84:] -= 10
    source = pd.DataFrame({
        "timestamp": pd.date_range("2025-01-01", periods=len(temp)),
        "temp_c": temp, "operating_hours": hours, "kwh_total": energy,
    })
    result = _fit_periods(source, 84, drivers=["operating_hours"], model_type=model_type)
    model = result["model"]
    assert model["model_type"] == ("3pc" if model_type == "auto" else model_type)
    expected_break = {"heating": 18, "cooling": 24} if model_type == "5p" else (
        cooling_base if cooling_base is not None else heating_base
    )
    assert model["balance_temp_c"] == expected_break
    assert model["coefficients"]["operating_hours"] == pytest.approx(2)
    assert result["summary"]["savings_kwh"] == pytest.approx(420)
    assert result["ashrae14_check"]["passed"]
    json.dumps(result, allow_nan=False)


def test_hdd_uses_configurable_base_and_accepts_negative_outdoor_temperatures() -> None:
    temp = np.array([-5, 0, 3, 10, 15, 18, 20, 0, 10, 18])
    energy = 40 + 7 * np.maximum(16 - temp, 0)
    source = pd.DataFrame({
        "timestamp": pd.date_range("2025-01-01", periods=len(temp)),
        "temp_c": temp, "kwh_total": energy,
    })
    result = _fit_periods(source, 7, drivers=["hdd"], hdd_base_temp_c=16)
    assert result["model"]["coefficients"]["hdd"] == pytest.approx(7)
    assert result["reporting"]["eligible_rows"] == 3
    assert result["model"]["hdd_base_temp_c"] == 16


def test_vif_of_correlated_drivers_matches_inverse_correlation() -> None:
    production = np.arange(1., 41.)
    hours = 6 + production / 4 + np.sin(production) / 4
    energy = 80 + 3 * production + 2 * hours + np.cos(production)
    source = pd.DataFrame({
        "timestamp": pd.date_range("2025-01-01", periods=len(production)),
        "production_t": production, "operating_hours": hours, "kwh_total": energy,
    })
    result = _fit_periods(source, 35, drivers=["production_t", "operating_hours"])
    expected = 1 / (1 - np.corrcoef(production[:35], hours[:35])[0, 1] ** 2)
    rows = {row["feature"]: row for row in result["coefficients"]}
    assert rows["intercept"]["vif"] is None
    for name in ("production_t", "operating_hours"):
        assert rows[name]["vif"] == pytest.approx(expected)
        assert rows[name]["p_value"] is not None
    assert result["ashrae14_check"]["vif_warnings"] == ["production_t", "operating_hours"]


def test_nra_and_cusum_use_only_matched_days_and_preserve_sign() -> None:
    source = pd.DataFrame({
        "timestamp": pd.date_range("2025-01-01", periods=10),
        "production_t": [1, 2, 3, 4, 5, 6, 2, 3, 4, 10],
        "kwh_total": [102, 104, 106, 108, 110, 112, 99, 100, 103, 110],
    })
    adjustments = [
        {"start_date": "2025-01-06", "end_date": "2025-01-10", "delta_kwh_per_day": 3, "reason": "Add load"},
        {"start_date": "2025-01-08", "end_date": "2025-01-08", "delta_kwh_per_day": -2, "reason": "Remove load"},
    ]
    result = _fit_periods(source, 6, drivers=["production_t"], non_routine_adjustments=adjustments)
    assert result["nra_total_kwh"] == 7
    assert result["summary"]["expected_kwh"] == pytest.approx(318)
    assert result["summary"]["savings_kwh"] == pytest.approx(23)
    assert result["summary"]["savings_pct"] == pytest.approx(100 * 23 / 325)
    assert result["monthly"][0]["nra_total_kwh"] == 7
    assert [point["date"] for point in result["cusum_series"]] == ["2025-01-07", "2025-01-08", "2025-01-09"]
    assert [point["daily_diff_kwh"] for point in result["cusum_series"]] == pytest.approx([-8, -7, -8])
    assert [point["cusum_kwh"] for point in result["cusum_series"]] == pytest.approx([-8, -15, -23])


@pytest.mark.parametrize(("noise", "passes"), [(1, True), (40, False)])
def test_ashrae_daily_check_applies_configured_cv_rmse_threshold(noise: float, passes: bool) -> None:
    production = np.tile([1, 2, 3, 4], 6)
    residual = noise * np.tile([1, -1, -1, 1], 6)
    source = pd.DataFrame({
        "timestamp": pd.date_range("2025-01-01", periods=len(production)),
        "production_t": production, "kwh_total": 60 + 3 * production + residual,
    })
    result = _fit_periods(source, 20, drivers=["production_t"])
    check = result["ashrae14_check"]
    assert check["passed"] is passes
    assert check["cv_rmse_pass"] is passes
    assert check["nmbe_pass"] is True
    assert check["granularity"] == "daily"


def test_monthly_check_uses_only_full_baseline_months() -> None:
    dates = pd.date_range("2025-01-02", "2025-06-05")
    production = np.arange(len(dates)) % 7 + 1
    source = pd.DataFrame({
        "timestamp": dates, "production_t": production, "kwh_total": 120 + production * 4,
    })
    n = int((dates < "2025-06-01").sum())
    result = _fit_periods(source, n, drivers=["production_t"], granularity="monthly")
    check = result["ashrae14_check"]
    assert check["granularity"] == "monthly"
    assert check["observations"] == 4
    assert check["max_cv_rmse_pct"] == 15
    assert check["max_abs_nmbe_pct"] == 5
    assert check["passed"] is True


@pytest.mark.parametrize(("daily_savings", "significant"), [(15, True), (1, False)])
def test_fsu_matches_annex_b_with_serial_correlation(daily_savings: float, significant: bool) -> None:
    from scipy.stats import t

    n, m = 60, 10
    production = np.tile(np.arange(1., 11.), 7)
    energy = 90 + 3 * production
    energy[:n] += 4 * np.sin(np.arange(n) * 0.7)
    energy[n:] -= daily_savings
    source = pd.DataFrame({
        "timestamp": pd.date_range("2025-01-01", periods=n + m),
        "production_t": production, "kwh_total": energy,
    })
    result = _fit_periods(source, n, drivers=["production_t"])
    fitted = result["model"]["coefficients"]
    fit = fitted["intercept"] + fitted["production_t"] * production[:n]
    residual = energy[:n] - fit
    rho = np.corrcoef(residual[1:], residual[:-1])[0, 1]
    assert rho > 0.5
    n_effective = n * (1 - rho) / (1 + rho)
    dof = n_effective - 2
    mse = np.sum(residual ** 2) / dof
    summary = result["summary"]
    scale = 1.26 * (summary["expected_kwh"] / m / energy[:n].mean()) * np.sqrt(mse * (1 + 2 / n_effective) * m)
    uncertainty = result["uncertainty"]
    assert uncertainty["rho"] == pytest.approx(rho)
    assert uncertainty["n_effective"] == pytest.approx(n_effective)
    for level in (90, 95):
        half_width = t.ppf((1 + level / 100) / 2, dof) * scale
        assert uncertainty[f"fsu_{level}_pct"] == pytest.approx(100 * half_width / summary["savings_kwh"])
    assert result["statistically_significant_savings"] is significant
