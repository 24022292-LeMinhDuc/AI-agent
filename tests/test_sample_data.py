"""Kiểm tra cấu trúc và các chỉ số trong dữ liệu mẫu kiểm toán."""

from pathlib import Path

import numpy as np
import pandas as pd

from data.make_sample import (
    DEMO_DO_KGCO2E_PER_KG,
    DEMO_GRID_KGCO2E_PER_KWH,
    DEMO_TARIFF_VND_PER_KWH,
    END_USE_COLUMNS,
    MISSING_LIGHTING_ROWS,
    SAMPLE_COLUMNS,
    SENSOR_ERROR_ROWS,
    THERMAL_KWH_PER_RTH,
    generate_sample_files,
    generate_sample_frames,
)


def test_sample_csv_files_replace_existing_demo_and_can_be_regenerated(tmp_path: Path) -> None:
    """Ghi đè các tệp mẫu và tạo đúng dữ liệu khi dùng cùng seed."""
    legacy = tmp_path / "sample_a.csv"
    legacy.write_text("old_header\n1\n", encoding="utf-8")
    paths = generate_sample_files(tmp_path)
    first_contents = {name: path.read_bytes() for name, path in paths.items()}
    assert paths["sample_a"] == legacy
    assert set(paths) == {"sample_a", "sample_b", "sample_c"}

    expected_dates = pd.date_range("2025-11-01", "2026-10-31", freq="D").strftime("%Y-%m-%d")
    for path in paths.values():
        frame = pd.read_csv(path, encoding="utf-8-sig")
        assert tuple(frame.columns) == SAMPLE_COLUMNS
        assert len(frame.columns) == 31
        assert frame["Ngay"].tolist() == expected_dates.tolist()

    regenerated = generate_sample_files(tmp_path)
    assert {name: path.read_bytes() for name, path in regenerated.items()} == first_contents
    assert set(tmp_path.iterdir()) == set(paths.values())


def test_factory_and_building_have_consistent_denominators_and_heat_inputs() -> None:
    """Phân biệt số người, tỷ lệ lấp đầy, chỉ số điện và tải lạnh."""
    samples = generate_sample_frames()
    factory = samples["sample_a"]
    building = samples["sample_b"]
    assert set(factory["Loai_co_so"]) == {"Nha_may"}
    assert set(building["Loai_co_so"]) == {"Toa_nha"}
    assert set(factory["Don_vi_lap_day"]) == {"Nguoi"}
    assert set(building["Don_vi_lap_day"]) == {"Pct"}
    assert pd.api.types.is_integer_dtype(factory["So_nguoi_hoac_ty_le_lap_day_pct"])
    assert building["So_nguoi_hoac_ty_le_lap_day_pct"].between(0, 100).all()
    assert building["San_luong_tan"].eq(0).all()
    assert building["Nhien_lieu_lo_hoi_kg"].eq(0).all()

    operating = factory["San_luong_tan"].gt(0)
    assert factory.loc[operating, "Nhien_lieu_lo_hoi_kg"].gt(0).all()
    assert factory.loc[operating, "San_luong_hoi_tan"].gt(0).all()
    assert factory.loc[~operating, "SEC_hoac_EUI_kWh_don_vi"].isna().all()
    factory_energy = factory.loc[operating, "Tong_dien_tieu_thu_kWh"]
    production = factory.loc[operating, "San_luong_tan"]
    np.testing.assert_allclose(
        factory.loc[operating, "SEC_hoac_EUI_kWh_don_vi"],
        (factory_energy / production).round(4),
    )
    np.testing.assert_allclose(
        building["SEC_hoac_EUI_kWh_don_vi"],
        (building["Tong_dien_tieu_thu_kWh"] / building["Dien_tich_san_m2"]).round(4),
    )

    for frame in (factory, building):
        thermal_kwh = frame["Tai_lanh_chiller_RTh"] * THERMAL_KWH_PER_RTH
        cop = thermal_kwh / frame["Dien_chiller_kWh"]
        assert cop.between(3.49, 5.81).all()
        assert frame["Cong_suat_cuc_dai_kW"].ge(frame["Tong_dien_tieu_thu_kWh"] / 24).all()
        assert frame["He_so_cong_suat_cos_phi"].between(0, 1).all()


def test_sample_cost_and_emissions_match_metered_totals() -> None:
    """Chi phí và Scope 2 theo công tơ tổng, Scope 1 theo nhiên liệu giả lập."""
    samples = generate_sample_frames()
    for name in ("sample_a", "sample_b"):
        frame = samples[name]
        total = frame["Tong_dien_tieu_thu_kWh"]
        np.testing.assert_allclose(total, frame[list(END_USE_COLUMNS)].sum(axis=1))
        np.testing.assert_allclose(
            frame["Chi_phi_nang_luong_VND"], (total * DEMO_TARIFF_VND_PER_KWH).round()
        )
        assert frame["He_so_phat_thai_dien_kgCO2_kWh"].eq(DEMO_GRID_KGCO2E_PER_KWH).all()
        scope1 = (frame["Nhien_lieu_lo_hoi_kg"] * DEMO_DO_KGCO2E_PER_KG / 1000).round(6)
        scope2 = (total * DEMO_GRID_KGCO2E_PER_KWH / 1000).round(6)
        np.testing.assert_allclose(frame["Phat_thai_CO2_Scope1_tCO2e"], scope1)
        np.testing.assert_allclose(frame["Phat_thai_CO2_Scope2_tCO2e"], scope2)
        np.testing.assert_allclose(frame["Tong_phat_thai_CO2_tCO2e"], scope1 + scope2)


def test_quality_scenario_is_local_to_sample_c_submeters() -> None:
    """Mẫu lỗi có chiller âm và ô chiếu sáng trống, công tơ tổng vẫn đủ số đọc."""
    samples = generate_sample_frames()
    sample_a = samples["sample_a"]
    sample_c = samples["sample_c"]
    assert sample_a[list(END_USE_COLUMNS)].ge(0).all().all()
    assert sample_c.index[sample_c["Dien_chiller_kWh"].lt(0)].tolist() == list(SENSOR_ERROR_ROWS)
    assert sample_c.index[sample_c["Dien_chieu_sang_kWh"].isna()].tolist() == list(MISSING_LIGHTING_ROWS)
    errors = sample_c.loc[list(SENSOR_ERROR_ROWS), "Ghi_chu_bat_thuong"]
    assert errors.eq("So do chiller am, kiem tra dong ho").all()
    pd.testing.assert_series_equal(sample_a["Tong_dien_tieu_thu_kWh"], sample_c["Tong_dien_tieu_thu_kWh"])
