"""Kiểm tra làm sạch số đo và phân tích SEU."""

import numpy as np
import pandas as pd
from pandas.testing import assert_frame_equal

from tools import clean, seu_pareto


def test_clean_drops_sensor_errors_and_only_fills_short_gaps() -> None:
    """Kiểm tra số âm, nội suy ngắn và giữ nguyên khoảng trống dài."""
    original = pd.DataFrame(
        {
            "kwh_chiller": [100.0, -999.0, 300.0, np.nan, np.nan, np.nan, np.nan, 200.0],
            "kwh_lighting": [10.0, 10.0, 10.0, 10.0, 10.0, 10.0, 10.0, 10.0],
        }
    )
    original_copy = original.copy(deep=True)

    cleaned, report = clean(original, ["kwh_chiller", "kwh_lighting"])

    assert_frame_equal(original, original_copy)
    assert cleaned.loc[1, "kwh_chiller"] == 200.0
    assert cleaned.loc[3:6, "kwh_chiller"].isna().all()
    assert report["kwh_chiller"] == {
        "removed": 1,
        "interpolated": 1,
        "missing": 4,
    }
    assert report["kwh_lighting"] == {
        "removed": 0,
        "interpolated": 0,
        "missing": 0,
    }


def test_clean_removes_values_over_three_times_p99() -> None:
    """Kiểm tra số đo cực lớn bị loại theo phân vị 99."""
    df = pd.DataFrame({"kwh_equipment": [10.0] * 100 + [1000.0]})

    cleaned, report = clean(df, ["kwh_equipment"])

    assert pd.isna(cleaned.loc[100, "kwh_equipment"])
    assert report["kwh_equipment"] == {
        "removed": 1,
        "interpolated": 0,
        "missing": 1,
    }


def test_seu_pareto_shares_and_includes_item_crossing_threshold() -> None:
    """Kiểm tra tỷ trọng, thứ tự Pareto và quy tắc SEU vượt ngưỡng."""
    df = pd.DataFrame(
        {
            "kwh_chiller": [70.0],
            "kwh_production": [20.0],
            "kwh_lighting": [10.0],
        }
    )

    result = seu_pareto(df, ["kwh_chiller", "kwh_production", "kwh_lighting"], 0.8)

    assert result.index.tolist() == ["kwh_chiller", "kwh_production", "kwh_lighting"]
    assert np.isclose(result["share"].sum(), 1.0)
    assert result["is_seu"].tolist() == [True, True, False]
    assert np.isclose(result.loc["kwh_production", "cum"], 0.9)


def test_clean_interpolates_in_timestamp_order_and_respects_discontinuities() -> None:
    df = pd.DataFrame(
        {
            "timestamp": ["2025-01-03", "2025-01-01", "2025-01-02", "2025-01-04", "2025-02-04"],
            "kwh_chiller": [30, 10, float("inf"), None, 90],
        }
    )

    cleaned, report = clean(df, ["kwh_chiller"])

    assert cleaned["timestamp"].equals(df["timestamp"])
    assert cleaned.loc[2, "kwh_chiller"] == 20
    assert pd.isna(cleaned.loc[3, "kwh_chiller"])
    assert report["kwh_chiller"] == {"removed": 1, "interpolated": 1, "missing": 1}


def test_seu_keeps_unknown_and_zero_readings_distinct() -> None:
    df = pd.DataFrame({"kwh_chiller": [None], "kwh_lighting": [0.0]})

    result = seu_pareto(df, ["kwh_chiller", "kwh_lighting"], 0.8)

    assert not result["is_seu"].any()
    assert result.loc["kwh_lighting", "kwh"] == 0
    assert pd.isna(result.loc["kwh_chiller", "kwh"])
