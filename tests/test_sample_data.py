"""Kiểm tra các file dữ liệu mẫu."""

from pathlib import Path

import pandas as pd

from data.make_sample import generate_sample_files


def test_sample_csv_files_have_expected_rows_and_columns(tmp_path: Path) -> None:
    """Kiểm tra số dòng, lỗi chiller và cột ghi chú."""
    paths = generate_sample_files(tmp_path)
    sample_a = pd.read_csv(paths["sample_a"], encoding="utf-8-sig")
    sample_b = pd.read_csv(paths["sample_b"], encoding="utf-8-sig")
    sample_c = pd.read_csv(paths["sample_c"], encoding="utf-8-sig")

    assert len(sample_a) == len(sample_b) == len(sample_c) == 365
    assert sample_a["Ngày"].iloc[0] == "2025-11-01"
    assert sample_a["Ngày"].iloc[-1] == "2026-10-31"
    assert (sample_a["Điện chiller (kWh)"] == -999).sum() == 5
    assert (sample_b["Chiller_kWh"] == -999).sum() == 5
    assert "Ghi chú" in sample_c.columns
