"""Đối chiếu hai công cụ kiểm toán bằng các cân bằng và chi phí biết trước."""

import json
from math import acos, tan
from pathlib import Path

import pandas as pd
import pytest
import yaml

import db
from agent import run_tool
from schemas import SCHEMAS, TOOLS
from tools import energy_balance_analysis, tariff_and_power_quality_analysis


@pytest.fixture
def config():
    return yaml.safe_load(Path(__file__).resolve().parents[1].joinpath("config.yaml").read_text(encoding="utf-8"))


@pytest.fixture
def observations():
    return pd.DataFrame({
        "timestamp": pd.date_range("2025-04-01 08:00", periods=3, freq="h"),
        "tariff_period": ["Cao điểm", "normal", "Thap_diem"], "kwh_total": [100, 80, 60],
        "kwh_chiller": [60, 48, 36], "cooling_load_rth": [100, 80, 60],
        "boiler_fuel_kg": [100, 50, 50], "fuel_type": ["DO", "Biomass", "Biomass"],
        "steam_t": [1., .2, None], "peak_demand_kw": [120, 100, 80], "power_factor": [.8, .9, .95],
        "energy_cost_vnd": [315100, 139040, 68760],
    })


def test_energy_inputs_outputs_are_distinct_and_efficiencies_are_paired(observations, config):
    result = energy_balance_analysis(observations, config)
    fuel = 100 * .043 + 100 * .0156
    purchased = 240 * .0036 + fuel
    assert result["available"] and result["total_input_gj"] == pytest.approx(purchased)
    assert result["total_input_toe"] == pytest.approx(purchased / 41.868)
    assert result["outputs"]["steam"]["gj"] == pytest.approx(1.2 * 2.76)
    assert result["outputs"]["cooling"]["gj"] == pytest.approx(240 * 3.51685284 * .0036)
    assert result["boiler_efficiency_pct"] == pytest.approx(100 * 1.2 * 2.76 / (4.3 + .78))
    assert result["chiller_kw_per_rt"] == pytest.approx(.6)
    assert result["chiller_cop"] == pytest.approx(3.51685284 / .6)
    assert result["chiller_rating"] == "Tốt"
    assert sum(row["share_pct"] for row in result["seu_breakdown"]) == pytest.approx(100)
    assert sum(link["value"] for link in result["sankey_links"]) == pytest.approx(purchased)
    assert result["fuels"][0]["combustion_co2_kg"] is not None
    json.dumps(result, allow_nan=False)


def test_tou_and_power_quality(observations, config):
    result = tariff_and_power_quality_analysis(observations, config)
    cost = 100 * 3151 + 80 * 1738 + 60 * 1146
    assert result["estimated_cost_vnd"] == cost
    assert result["reported_cost_vnd"] == cost
    assert result["peak_share_pct"] == pytest.approx(100 / 240 * 100)
    assert result["shift_savings_vnd"] == 10 * (3151 - 1146)
    assert result["peak_demand_kw"] == 120
    assert result["peak_timestamp"] == "2025-04-01T08:00:00"
    assert result["load_factor"] == pytest.approx(240 / (3 * 120))
    assert result["mean_power_factor"] == pytest.approx((.8 + .9 + .95) / 3)
    assert result["low_pf_days"] == result["low_pf_records"] == 1
    assert result["reactive_penalty_vnd"] == pytest.approx(315100 * (.9 / .8 - 1))
    assert result["compensation_kvar"] == pytest.approx(120 * (tan(acos(.8)) - tan(acos(.9))))
    json.dumps(result, allow_nan=False)


def test_partial_data_keeps_missing_values_distinct_from_zero(config):
    empty = pd.DataFrame()
    for function in (energy_balance_analysis, tariff_and_power_quality_analysis):
        result = function(empty, config)
        assert result["available"] is False and result["reason"]
        json.dumps(result, allow_nan=False)
    readings = pd.DataFrame({"kwh_chiller": [10, 4], "kwh_other": [5, None],
                             "tariff_period": ["off_peak", "invalid"], "power_factor": [0, .8]})
    balance = energy_balance_analysis(readings, config)
    assert balance["total_input_gj"] == pytest.approx(15 * .0036)
    assert balance["boiler_efficiency_pct"] is None
    tariff = tariff_and_power_quality_analysis(readings, config)
    assert tariff["estimated_cost_vnd"] == 15 * 1146 and tariff["peak_share_pct"] == 0
    assert tariff["low_pf_records"] == 1 and tariff["low_pf_days"] is None
    assert tariff["compensation_kvar"] is None and tariff["reactive_penalty_vnd"] is None


def test_unknown_fuel_is_excluded_and_override_is_explicit(config):
    df = pd.DataFrame({"boiler_fuel_kg": [10, 20], "fuel_type": [None, "unknown"]})
    balance = energy_balance_analysis(df, config)
    assert balance["total_input_gj"] == pytest.approx(10 * .0156)
    assert balance["unconverted_fuel_kg"] == 20
    assert energy_balance_analysis(df, config, fuel_type="lpg")["total_input_gj"] == pytest.approx(30 * .0473)


@pytest.mark.parametrize(("name", "args"), [("energy_balance", {"fuel_type": "lpg"}), ("tariff_power_quality", {})])
def test_registered_tools_log_original_measurements_and_enforce_schema(name, args, observations, config, tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "runs.db")
    store = {"mapping_confirmed": True, "measured_df": observations, "df": observations.assign(kwh_total=900),
             "config": config, "run_scope_id": "audit"}
    assert any(tool["name"] == name for tool in TOOLS) and not SCHEMAS[name]["additionalProperties"]
    assert "error" in run_tool(name, {"other": 1}, store)
    response = run_tool(name, args, store)
    assert response["result"]["available"]
    assert db.get_run(response["run_id"], scope_id="audit")["result"] == response["result"]
    if name == "tariff_power_quality":
        assert response["result"]["estimated_cost_vnd"] == 522900
