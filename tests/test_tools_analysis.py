"""Kiểm tra hồi quy EnPI, so sánh kỳ và ước tính cơ hội."""

import numpy as np
import pandas as pd

from tools import (
    aggregate_consumption,
    clean,
    compare_periods,
    dashboard_analysis,
    dataset_summary,
    enpi_regression,
    list_opportunities,
    opportunity,
    resolve_measurement_rows,
    seu_pareto,
    total_kwh,
)


def test_total_kwh_and_monthly_aggregation() -> None:
    """Kiểm tra tổng điện theo dòng và gom theo tháng."""
    df = pd.DataFrame(
        {
            "timestamp": pd.to_datetime(
                ["2025-01-01 08:00", "2025-01-01 16:00", "2025-01-08 08:00"]
            ),
            "production_t": [10.0, 20.0, 30.0],
            "temp_c": [20.0, 22.0, 25.0],
            "kwh_production": [100.0, 200.0, 300.0],
            "kwh_chiller": [10.0, 20.0, 30.0],
        }
    )
    kwh_cols = ["kwh_production", "kwh_chiller"]

    row_totals = total_kwh(df, kwh_cols)
    monthly = aggregate_consumption(df, "month", kwh_cols)
    daily = aggregate_consumption(df, "day", kwh_cols)
    weekly = aggregate_consumption(df, "week", kwh_cols)

    assert row_totals.tolist() == [110.0, 220.0, 330.0]
    assert len(daily) == 2
    assert daily.loc[0, "kwh_total"] == 330.0
    assert len(weekly) == 2
    assert monthly.loc[0, "kwh_production"] == 600.0
    assert monthly.loc[0, "kwh_total"] == 660.0
    assert monthly.loc[0, "production_t"] == 60.0
    assert np.isclose(monthly.loc[0, "temp_c"], (20 + 22 + 25) / 3)


def test_dataset_summary_reports_only_available_values() -> None:
    """Metadata tổng hợp phản ánh đúng ngày, thiếu dữ liệu và tổng điện."""
    df = pd.DataFrame(
        {
            "timestamp": pd.to_datetime(["2025-01-01", "2025-01-02"]),
            "production_t": [10.0, 20.0],
            "temp_c": [24.0, None],
            "kwh_chiller": [100.0, 200.0],
        }
    )

    result = dataset_summary(df, ["kwh_chiller"])

    assert result["rows"] == 2
    assert result["date_start"] == "2025-01-01"
    assert result["date_end"] == "2025-01-02"
    assert result["missing_values_by_column"]["temp_c"] == 1
    assert result["energy_kwh_by_column"] == {"kwh_chiller": 300.0}
    assert result["total_kwh"] == 300.0


def test_enpi_regression_recovers_known_coefficients_and_monthly_ratios() -> None:
    """Kiểm tra hồi quy khôi phục hệ số và suất tiêu hao."""
    df = pd.DataFrame(
        {
            "timestamp": pd.to_datetime(
                ["2025-01-01", "2025-01-02", "2025-02-01", "2025-02-02", "2025-02-03"]
            ),
            "production_t": [10.0, 20.0, 30.0, 40.0, 50.0],
            "cdd": [0.0, 1.0, 0.0, 2.0, 1.0],
            "kwh_total": [140.0, 190.0, 220.0, 280.0, 310.0],
        }
    )

    result = enpi_regression(df, "kwh_total", ["production_t", "cdd"])
    coefficients = result["coefficients"]
    monthly = result["monthly_kwh_per_ton"]

    assert np.isclose(coefficients["intercept"], 100.0)
    assert np.isclose(coefficients["production_t"], 4.0)
    assert np.isclose(coefficients["cdd"], 10.0)
    assert np.isclose(result["r2"], 1.0)
    assert np.isclose(result["kwh_per_ton"], 1140 / 150)
    assert np.isclose(monthly.loc["2025-01"], 330 / 30)
    assert np.isclose(monthly.loc["2025-02"], 810 / 120)


def test_compare_periods_returns_month_b_minus_month_a() -> None:
    """Kiểm tra chênh lệch tháng, nhiệt độ, sản lượng và hạng mục chính."""
    df = pd.DataFrame(
        {
            "timestamp": pd.to_datetime(["2025-01-01", "2025-02-01"]),
            "production_t": [10.0, 15.0],
            "temp_c": [20.0, 25.0],
            "kwh_production": [100.0, 140.0],
            "kwh_chiller": [50.0, 110.0],
        }
    )

    result = compare_periods(
        df, "2025-01", "2025-02", ["kwh_production", "kwh_chiller"]
    )

    assert result["kwh_differences"] == {
        "kwh_production": 40.0,
        "kwh_chiller": 60.0,
    }
    assert result["temperature_difference_c"] == 5.0
    assert result["production_difference_t"] == 5.0
    assert result["main_driver"] == "kwh_chiller"


def test_opportunity_computes_annual_savings_roi_and_payback() -> None:
    """Kiểm tra phép tính cơ hội bằng các giá trị tròn."""
    result = opportunity("LED", 10_000, 0.1, 2_000, 1_000_000, 0.5)

    assert result["saved_kwh"] == 1_000.0
    assert result["saved_vnd"] == 2_000_000.0
    assert result["co2_tonnes"] == 0.5
    assert result["roi_pct"] == 200.0
    assert result["payback_months"] == 6.0
    assert "GIẢ ĐỊNH" in result["assumption"]


def test_opportunity_handles_missing_emission_and_zero_denominators() -> None:
    """Kiểm tra hệ số phát thải thiếu và trường hợp không chia được."""
    free = opportunity("No capex", 100, 0.1, 2_000, 0, None)
    no_savings = opportunity("No savings", 100, 0, 2_000, 1_000, None)

    assert free["co2_tonnes"] is None
    assert free["roi_pct"] is None
    assert free["payback_months"] == 0.0
    assert no_savings["saved_kwh"] == 0.0
    assert no_savings["saved_vnd"] == 0.0
    assert no_savings["roi_pct"] == 0.0
    assert no_savings["payback_months"] is None


def test_list_opportunities_annualizes_consumption_and_preserves_assumption() -> None:
    """Kiểm tra cơ hội quy đổi tiêu thụ lên năm và gắn nhãn giả định."""
    df = pd.DataFrame(
        {
            "timestamp": pd.to_datetime(["2025-01-01", "2025-01-02"]),
            "kwh_chiller": [100.0, 300.0],
        }
    )
    config = [
        {
            "name": "Tối ưu chiller",
            "applies_to": "kwh_chiller",
            "saving_pct": 0.1,
            "capex_vnd": 100_000,
        }
    ]

    result = list_opportunities(df, config, tariff=2_000, emission_factor=None)[0]

    assert result["base_kwh_annual"] == 73_000.0
    assert result["saved_kwh"] == 7_300.0
    assert result["saved_vnd"] == 14_600_000.0
    assert result["co2_tonnes"] is None
    assert "GIẢ ĐỊNH" in result["assumption"]


def test_dashboard_intensity_uses_only_paired_complete_measurements() -> None:
    df = pd.DataFrame(
        {
            "timestamp": ["2025-01-01", "2025-01-02", "2025-01-03", "invalid"],
            "production_t": [10, 20, None, 30],
            "kwh_chiller": [100, 100, 30, 1000],
            "kwh_lighting": [20, None, 40, 70],
        }
    )
    kwh_cols = ["kwh_chiller", "kwh_lighting"]

    summary = dashboard_analysis(df, "day", kwh_cols, "2025-01-01", "2025-01-04", None)["summary"]

    assert summary["total_kwh"] == 290
    assert summary["production_tons"] == 30
    assert summary["energy_rows_by_column"] == {"kwh_chiller": 3, "kwh_lighting": 2}
    assert summary["intensity_rows"] == 1
    assert summary["kwh_per_ton"] == 12
    assert total_kwh(df, kwh_cols, require_complete=True).dropna().tolist() == [120, 70]


def test_enpi_keeps_intensity_when_regression_drivers_are_collinear() -> None:
    df = pd.DataFrame(
        {
            "timestamp": pd.date_range("2025-01-01", periods=5),
            "production_t": [1, 2, 3, 4, 5],
            "cdd": [2, 4, 6, 8, None],
            "kwh_total": [10, 20, 30, 40, 60],
        }
    )

    result = enpi_regression(df, "kwh_total", ["production_t", "cdd"])

    assert result["coefficients"] == {}
    assert result["r2"] is None
    assert "phụ thuộc tuyến tính" in result["regression_note"]
    assert result["regression_rows"] == 4
    assert result["intensity_rows"] == 5
    assert np.isclose(result["kwh_per_ton"], 160 / 15)


def test_compare_periods_leaves_missing_readings_and_optional_metrics_unknown() -> None:
    df = pd.DataFrame(
        {
            "timestamp": ["2025-01-01", "2025-02-01"],
            "kwh_chiller": [100, 70],
            "kwh_lighting": [10, None],
        }
    )
    kwh_cols = ["kwh_chiller", "kwh_lighting"]

    measured = compare_periods(df, "2025-01", "2025-02", kwh_cols)
    missing = compare_periods(df, "2025-01", "2025-03", kwh_cols)

    assert measured["kwh_differences"] == {"kwh_chiller": -30, "kwh_lighting": None}
    assert measured["main_driver"] is None
    assert measured["temperature_difference_c"] is None
    assert measured["production_difference_t"] is None
    assert missing["rows_month_b"] == 0
    assert missing["kwh_differences"] == {"kwh_chiller": None, "kwh_lighting": None}


def test_resolved_intervals_do_not_double_count_electricity_or_production() -> None:
    """Giữ một bản sao cùng thời điểm và loại hết dòng đồng hồ mâu thuẫn."""
    df = pd.DataFrame({
        "timestamp": [
            "2025-01-01T00:00:00+07:00", "2024-12-31T17:00:00Z",
            "2025-01-02", "2025-01-02", "2025-01-02", "2025-01-03",
        ],
        "kwh_chiller": [100, 100, 50, 50, 60, 300],
        "production_t": [10, 10, 5, 5, 5, 30],
    })
    meters = ["kwh_chiller"]
    quality = {
        "duplicate_rows_removed": 1,
        "conflicting_timestamp_rows": 3,
        "conflicting_timestamps": 1,
    }

    resolved, diagnostics = resolve_measurement_rows(df)
    assert resolved.index.tolist() == [0, 5]
    assert diagnostics == quality
    assert len(df) == 6
    assert not df.attrs

    cleaned, _ = clean(df, meters)
    assert dataset_summary(cleaned, meters)["interval_quality"] == quality
    assert dataset_summary(df, meters)["total_kwh"] == 400
    assert seu_pareto(df, meters, 0.8).loc["kwh_chiller", "kwh"] == 400
    assert aggregate_consumption(df, "month", meters).loc[0, "production_t"] == 40
    summary = dashboard_analysis(df, "month", meters, "2025-01-01", "2025-01-03", None)["summary"]
    assert summary["rows"] == 2
    assert summary["interval_quality"] == quality
    assert summary["total_kwh"] == 400
    assert summary["production_tons"] == 40


def test_compare_periods_normalizes_observed_day_energy_by_meter() -> None:
    """So sánh tổng và bình quân ngày, kể cả khi mỗi hạng mục có độ phủ khác nhau."""
    df = pd.DataFrame({
        "timestamp": [
            "2024-01-01 08:00", "2024-01-01 16:00", "2024-01-02",
            "2024-02-01", "2024-02-02", "2024-02-02",
        ],
        "kwh_chiller": [40, 60, 100, 200, 50, 50],
        "kwh_lighting": [20, 30, None, None, 80, 80],
    })

    result = compare_periods(df, "2024-01", "2024-02", ["kwh_chiller", "kwh_lighting"])

    assert result["kwh_differences"] == {"kwh_chiller": 50, "kwh_lighting": 30}
    assert result["mean_daily_kwh_differences"] == {"kwh_chiller": 25, "kwh_lighting": 30}
    assert result["coverage"]["month_a"] == {
        "calendar_days": 31, "recorded_days": 2,
        "meter_days": {"kwh_chiller": 2, "kwh_lighting": 1},
        "meter_rows": {"kwh_chiller": 3, "kwh_lighting": 2},
    }
    assert result["coverage"]["month_b"] == {
        "calendar_days": 29, "recorded_days": 2,
        "meter_days": {"kwh_chiller": 2, "kwh_lighting": 1},
        "meter_rows": {"kwh_chiller": 2, "kwh_lighting": 1},
    }
    assert result["interval_quality"]["duplicate_rows_removed"] == 1


def test_opportunity_uses_only_dated_readings_and_preserves_unavailable_estimates() -> None:
    df = pd.DataFrame({"timestamp": ["2025-01-01", "invalid"], "kwh_chiller": [100, 9_999]})
    config = [
        {"name": "Chiller", "applies_to": "kwh_chiller", "saving_pct": 0.1, "capex_vnd": 100},
        {"name": "Lighting", "applies_to": "kwh_lighting", "saving_pct": 0.1, "capex_vnd": 100},
    ]

    measured, unavailable = list_opportunities(df, config, tariff=2_000, emission_factor=0.5)

    assert measured["base_kwh_annual"] == 36_500
    assert measured["observed_days"] == 1
    assert unavailable["base_kwh_annual"] is None
    assert unavailable["saved_kwh"] is None
    assert unavailable["co2_tonnes"] is None
    assert unavailable["roi_pct"] is None


def test_authoritative_total_preserves_missing_and_excludes_submeter_double_count() -> None:
    """Công tơ tổng giữ ranh giới kể cả khi có khoảng thiếu và các phụ tải đầy đủ."""
    df = pd.DataFrame({
        "timestamp": ["2025-01-01", "2025-01-02", "2025-01-03", "2025-02-01"],
        "kwh_total": [250, None, 200, 500],
        "kwh_chiller": [100, 130, None, 140],
        "kwh_other": [60, 80, 70, 90],
        "production_t": [10, 50, 15, 20],
    })
    cols = ["kwh_total", "kwh_chiller", "kwh_other"]

    cleaned, quality = clean(df, cols)
    assert pd.isna(cleaned.loc[1, "kwh_total"])
    assert quality["kwh_total"] == {"removed": 0, "interpolated": 0, "missing": 1}
    assert total_kwh(df, cols, require_complete=True).dropna().tolist() == [250, 200, 500]

    monthly = aggregate_consumption(df, "month", cols)
    assert monthly.columns.tolist().count("kwh_total") == 1
    assert monthly["kwh_total"].tolist() == [450, 500]
    assert monthly["kwh_other"].tolist() == [210, 90]

    dataset = dataset_summary(df, cols)
    assert dataset["total_source"] == "metered_total"
    assert dataset["total_kwh"] == 950
    assert dataset["total_energy_rows"] == 3
    assert dataset["energy_reconciliation"] == {
        "paired_rows": 2, "metered_total_kwh": 750, "submeter_kwh": 390, "residual_kwh": 360,
    }

    dashboard = dashboard_analysis(df, "day", cols, "2025-01-01", "2025-01-31", 3)["summary"]
    assert dashboard["total_kwh"] == 450
    assert dashboard["intensity_rows"] == 2
    assert dashboard["kwh_per_ton"] == 18
    assert dashboard["estimated_cost_vnd"] == 1350
    assert dashboard["total_energy_rows"] == 2
    assert dashboard["energy_reconciliation"]["paired_rows"] == 1

    comparison = compare_periods(df, "2025-01", "2025-02", cols)
    assert comparison["total_kwh_difference"] == 50
    assert comparison["kwh_differences"]["kwh_total"] == 50
    assert comparison["main_driver"] is None

    pareto = seu_pareto(df, cols, 0.8)
    assert pareto.index.tolist() == ["kwh_chiller", "kwh_other"]
    assert np.isclose(pareto["share"].sum(), 1)
    assert seu_pareto(df, ["kwh_total"], 0.8).empty


def test_empty_total_column_uses_submeters_without_switching_source_by_period() -> None:
    """Cột công tơ tổng để trống cả tệp khác với một kỳ thiếu số đo."""
    df = pd.DataFrame({
        "timestamp": ["2026-01-01", "2026-02-01"],
        "facility_type": ["Toa_nha", "Toa_nha"],
        "floor_area_m2": [100, 100],
        "kwh_total": [None, None],
        "kwh_chiller": [100, 110],
        "kwh_lighting": [20, 22],
    })
    columns = ["kwh_total", "kwh_chiller", "kwh_lighting"]
    dataset = dataset_summary(df, columns)
    assert dataset["total_source"] == "submeter_sum"
    assert dataset["total_kwh"] == 252
    assert dataset["energy_rows_by_column"]["kwh_total"] == 0
    assert np.isclose(dataset["reported_audit"]["building_eui"]["kwh_per_m2"], 2.52)
    assert compare_periods(df, "2026-01", "2026-02", columns)["total_kwh_difference"] == 12

    february = dashboard_analysis(df, "day", columns, "2026-02-01", "2026-02-01", 2)
    assert february["summary"]["total_kwh"] == 132
    assert february["aggregated"]["kwh_total"].tolist() == [132]

    df["kwh_total"] = [200, None]
    february = dashboard_analysis(df, "day", columns, "2026-02-01", "2026-02-01", 2)
    assert february["summary"]["total_source"] == "metered_total"
    assert february["summary"]["total_kwh"] is None
    assert february["summary"]["energy_rows_by_column"]["kwh_chiller"] == 1
    assert pd.isna(february["aggregated"]["kwh_total"].iloc[0])
    assert compare_periods(df, "2026-01", "2026-02", columns)["total_kwh_difference"] is None


def test_building_audit_uses_paired_cooling_and_keeps_reported_carbon_separate() -> None:
    """Tổng hợp số đã ghi và tính chiller/EUI theo các dòng ghép hợp lệ."""
    df = pd.DataFrame({
        "timestamp": ["2025-01-01", "2025-01-02", "2025-01-03", "invalid"],
        "facility_type": ["Toa_nha"] * 4,
        "floor_area_m2": [100, 100, 100, 500],
        "production_t": [2, 2, 2, 2],
        "kwh_total": [400, None, 800, 9999],
        "kwh_chiller": [40, 20, 0, 999],
        "cooling_load_rth": [80, 20, 600, 999],
        "energy_cost_vnd": [1000, None, 2000, 4000],
        "peak_demand_kw": [110, 180, 150, 500],
        "power_factor": [0.9, 0.8, 2, 0.1],
        "boiler_fuel_kg": [2, 5, 4, 999],
        "fuel_type": ["LPG", "DO", "LPG", "LPG"],
        "scope1_tco2e": [0.2, None, 0, 99],
        "scope2_tco2e": [None, None, None, 99],
        "total_emissions_tco2e": [1.1, 1.2, 1.3, 99],
    })

    result = dashboard_analysis(df, "day", ["kwh_total", "kwh_chiller"], "2025-01-01", "2025-01-03", None)
    assert result["summary"]["kwh_per_ton"] is None
    audit = result["summary"]["reported_audit"]
    assert audit["valid_timestamp_rows"] == 3
    assert audit["energy_cost_vnd"] == {"value": 3000, "rows": 2}
    assert audit["peak_demand_kw"] == {"value": 180, "rows": 3}
    assert np.isclose(audit["power_factor_mean"]["value"], 0.85)
    assert audit["scope1_tco2e"] == {"value": 0.2, "rows": 2}
    assert audit["scope2_tco2e"] == {"value": None, "rows": 0}
    assert np.isclose(audit["total_emissions_tco2e"]["value"], 3.6)
    assert audit["boiler_fuel_kg_by_type"] == {
        "LPG": {"value": 6, "rows": 2}, "DO": {"value": 5, "rows": 1},
    }
    assert audit["building_eui"] == {"floor_area_m2": 100, "paired_rows": 2, "kwh": 1200, "kwh_per_m2": 12}
    assert audit["chiller"]["paired_rows"] == 2
    assert audit["chiller"]["electricity_kwh"] == 60
    assert audit["chiller"]["cooling_load_rth"] == 100
    assert np.isclose(audit["chiller"]["kw_per_rt"], 0.6)
    assert np.isclose(audit["chiller"]["cop"], 3.51685284 / 0.6)


def test_audit_uses_source_rows_instead_of_interpolated_submeters() -> None:
    """Hiệu suất chiller và đối chiếu điện dùng số gốc, cùng phạm vi ngày đã lọc."""
    measured = pd.DataFrame({
        "timestamp": pd.date_range("2025-01-01", periods=4),
        "kwh_total": [200, 300, 400, 1900],
        "kwh_chiller": [20, None, 60, 90],
        "cooling_load_rth": [30, 40, 50, 160],
    })
    columns = ["kwh_total", "kwh_chiller"]
    cleaned, _ = clean(measured, columns)
    assert cleaned.loc[1, "kwh_chiller"] == 40

    dashboard = dashboard_analysis(
        cleaned, "day", columns, "2025-01-01", "2025-01-03", None, audit_df=measured,
    )["summary"]
    assert dashboard["energy_kwh_by_column"]["kwh_chiller"] == 120
    assert dashboard["energy_reconciliation"]["submeter_kwh"] == 80
    assert dashboard["reported_audit"]["chiller"]["paired_rows"] == 2
    assert dashboard["reported_audit"]["chiller"]["electricity_kwh"] == 80

    dataset = dataset_summary(cleaned, columns, audit_df=measured)
    assert dataset["reported_audit"]["chiller"]["paired_rows"] == 3
    assert dataset["energy_reconciliation"]["submeter_kwh"] == 170
