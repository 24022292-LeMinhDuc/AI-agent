"""Tạo mẫu kiểm toán điện, nhiệt và phát thải có thể tái lập."""

from __future__ import annotations

import sys
from pathlib import Path
from tempfile import NamedTemporaryFile

import numpy as np
import pandas as pd

SEED = 42
SENSOR_ERROR_ROWS = (17, 88, 162, 247, 330)
MISSING_LIGHTING_ROWS = (105, 106, 107, 108)

# Giả định dùng riêng để tạo dữ liệu mẫu.
DEMO_TARIFF_VND_PER_KWH = 2500
DEMO_GRID_KGCO2E_PER_KWH = 0.65
DEMO_DO_KGCO2E_PER_KG = 3.2
DEMO_KG_STEAM_PER_KG_DO = 10.5
THERMAL_KWH_PER_RTH = 3.516853  # 12.000 Btu_IT/h theo NIST SP 811.

END_USE_COLUMNS = (
    "Dien_san_xuat_kWh",
    "Dien_chiller_kWh",
    "Dien_khi_nen_kWh",
    "Dien_chieu_sang_kWh",
    "Dien_phu_tro_khac_kWh",
)
SAMPLE_COLUMNS = (
    "Ngay",
    "Loai_co_so",
    "Khung_gio",
    "Trang_thai_van_hanh",
    "So_gio_van_hanh",
    "San_luong_tan",
    "Dien_tich_san_m2",
    "So_nguoi_hoac_ty_le_lap_day_pct",
    "Don_vi_lap_day",
    "Nhiet_do_ngoai_troi_C",
    "Do_am_ngoai_troi_pct",
    *END_USE_COLUMNS,
    "Tong_dien_tieu_thu_kWh",
    "He_so_cong_suat_cos_phi",
    "Cong_suat_cuc_dai_kW",
    "Nhien_lieu_lo_hoi_kg",
    "Loai_nhien_lieu",
    "San_luong_hoi_tan",
    "Tai_lanh_chiller_RTh",
    "SEC_hoac_EUI_kWh_don_vi",
    "EnB_chuan_kWh_don_vi",
    "Chi_phi_nang_luong_VND",
    "He_so_phat_thai_dien_kgCO2_kWh",
    "Phat_thai_CO2_Scope1_tCO2e",
    "Phat_thai_CO2_Scope2_tCO2e",
    "Tong_phat_thai_CO2_tCO2e",
    "Ghi_chu_bat_thuong",
)


def _facility_sample(dates: pd.DatetimeIndex, rng: np.random.Generator, *, factory: bool) -> pd.DataFrame:
    """Sinh số đo giả lập theo ngày cho một cơ sở."""
    size = len(dates)
    weekends = dates.day_of_week.to_numpy() >= 5
    holidays = dates.strftime("%m-%d").isin(("01-01", "04-30", "05-01"))
    maintenance = dates.strftime("%m-%d").isin(("02-10", "06-09", "09-16"))
    status = np.select(
        (holidays, maintenance, weekends),
        ("Le_tet", "Bao_tri", "Nghi_tuan"),
        default="Hoat_dong",
    )
    working = status == "Hoat_dong"
    hours = np.where(working, rng.uniform(13, 21, size) if factory else rng.uniform(8, 13, size), 0.0)
    hours = np.round(hours, 2)
    occupancy = np.where(working, rng.uniform(48, 93, size), rng.uniform(1, 12, size))
    season = np.cos(2 * np.pi * (dates.day_of_year.to_numpy() - 200) / 365)
    temperature = np.round(np.clip(27 + 6.5 * season + rng.normal(0, 1.5, size), 16, 39), 2)
    humidity = np.round(np.clip(72 + 7 * season + rng.normal(0, 4, size), 35, 98), 2)
    cooling_degrees = np.maximum(temperature - 24.0, 0)

    if factory:
        occupancy = np.rint(occupancy).astype(int)
        production = np.where(working, 18 + 2 * hours + rng.normal(0, 4, size), 0)
        production = np.round(production, 2)
        production_kwh = 180 + 175 * production + rng.normal(0, 70, size)
        chiller_kwh = 1250 + 335 * cooling_degrees + 16 * production + rng.normal(0, 80, size)
        compressed_air_kwh = 210 + 30 * production + rng.normal(0, 25, size)
        floor_area = 12000.0
        steam_t = np.round(production * 0.13, 3)
        fuel_kg = np.round(steam_t * 1000 / DEMO_KG_STEAM_PER_KG_DO, 2)
        intensity_reference = 340.0
    else:
        occupancy = np.round(occupancy, 2)
        production = np.zeros(size)
        production_kwh = np.zeros(size)
        chiller_kwh = 380 + 115 * cooling_degrees + 29 * hours + 6 * occupancy + rng.normal(0, 45, size)
        compressed_air_kwh = np.zeros(size)
        floor_area = 20000.0
        steam_t = np.zeros(size)
        fuel_kg = np.zeros(size)
        intensity_reference = 0.22

    unexpected_weekend_load = weekends & (dates.strftime("%m-%d") == "08-23")
    auxiliary_kwh = (
        390 + 20 * hours + 25 * cooling_degrees + rng.normal(0, 25, size)
        + 650 * unexpected_weekend_load
    )
    end_uses = pd.DataFrame(
        {
            "Dien_san_xuat_kWh": production_kwh,
            "Dien_chiller_kWh": chiller_kwh,
            "Dien_khi_nen_kWh": compressed_air_kwh,
            "Dien_chieu_sang_kWh": 255 + 38 * hours + rng.normal(0, 18, size),
            "Dien_phu_tro_khac_kWh": auxiliary_kwh,
        }
    ).clip(lower=0).round(2)
    total_kwh = end_uses.sum(axis=1).round(2)
    peak_kw = (total_kwh / 24 * rng.uniform(1.30, 1.65, size)).round(2)
    power_factor = np.round(rng.uniform(0.90, 0.98, size), 3)
    chiller_cop = np.clip(5.1 - 0.09 * cooling_degrees + rng.normal(0, 0.18, size), 3.5, 5.8)
    cooling_rth = (end_uses["Dien_chiller_kWh"] * chiller_cop / THERMAL_KWH_PER_RTH).round(2)

    denominator = pd.Series(production).where(production > 0) if factory else floor_area
    scope1 = np.round(fuel_kg * DEMO_DO_KGCO2E_PER_KG / 1000, 6)
    scope2 = (total_kwh * DEMO_GRID_KGCO2E_PER_KWH / 1000).round(6)
    notes = np.select(
        (maintenance, unexpected_weekend_load),
        ("Bao tri dinh ky", "Tang dot bien cuoi tuan"),
        default="",
    )

    data = pd.DataFrame(
        {
            "Ngay": dates.strftime("%Y-%m-%d"),
            "Loai_co_so": "Nha_may" if factory else "Toa_nha",
            "Khung_gio": "Binh_thuong",  # Nhãn bối cảnh ngày, điện gộp suốt 24 giờ.
            "Trang_thai_van_hanh": status,
            "So_gio_van_hanh": hours,
            "San_luong_tan": production,
            "Dien_tich_san_m2": floor_area,
            "So_nguoi_hoac_ty_le_lap_day_pct": occupancy,
            "Don_vi_lap_day": "Nguoi" if factory else "Pct",
            "Nhiet_do_ngoai_troi_C": temperature,
            "Do_am_ngoai_troi_pct": humidity,
            "Tong_dien_tieu_thu_kWh": total_kwh,
            "He_so_cong_suat_cos_phi": power_factor,
            "Cong_suat_cuc_dai_kW": peak_kw,
            "Nhien_lieu_lo_hoi_kg": fuel_kg,
            "Loai_nhien_lieu": np.where(fuel_kg > 0, "DO", "Dien_luoi"),
            "San_luong_hoi_tan": steam_t,
            "Tai_lanh_chiller_RTh": cooling_rth,
            "SEC_hoac_EUI_kWh_don_vi": (total_kwh / denominator).round(4),
            "EnB_chuan_kWh_don_vi": intensity_reference,
            "Chi_phi_nang_luong_VND": (total_kwh * DEMO_TARIFF_VND_PER_KWH).round().astype("int64"),
            "He_so_phat_thai_dien_kgCO2_kWh": DEMO_GRID_KGCO2E_PER_KWH,
            "Phat_thai_CO2_Scope1_tCO2e": scope1,
            "Phat_thai_CO2_Scope2_tCO2e": scope2,
            "Tong_phat_thai_CO2_tCO2e": (scope1 + scope2).round(6),
            "Ghi_chu_bat_thuong": notes,
        }
    )
    return pd.concat([data, end_uses], axis=1).loc[:, list(SAMPLE_COLUMNS)]


def generate_sample_frames(seed: int = SEED) -> dict[str, pd.DataFrame]:
    """Sinh mẫu nhà máy, tòa nhà và tình huống lỗi đo."""
    rng = np.random.default_rng(seed)
    dates = pd.date_range("2025-11-01", "2026-10-31", freq="D")
    factory = _facility_sample(dates, rng, factory=True)
    building = _facility_sample(dates, rng, factory=False)

    defects = factory.copy()
    defects.loc[list(SENSOR_ERROR_ROWS), "Dien_chiller_kWh"] = -999.0
    defects.loc[list(SENSOR_ERROR_ROWS), "Ghi_chu_bat_thuong"] = "So do chiller am, kiem tra dong ho"
    defects.loc[list(MISSING_LIGHTING_ROWS), "Dien_chieu_sang_kWh"] = np.nan
    defects.loc[list(MISSING_LIGHTING_ROWS), "Ghi_chu_bat_thuong"] = "Thieu so do dien chieu sang"

    return {"sample_a": factory, "sample_b": building, "sample_c": defects}


def generate_sample_files(
    output_dir: Path | None = None, seed: int = SEED
) -> dict[str, Path]:
    """Thay các CSV mẫu bằng tệp hoàn chỉnh đã sinh."""
    target_dir = (output_dir or Path(__file__).resolve().parent).resolve()
    target_dir.mkdir(parents=True, exist_ok=True)

    paths: dict[str, Path] = {}
    for name, frame in generate_sample_frames(seed).items():
        path = target_dir / f"{name}.csv"
        with NamedTemporaryFile(
            dir=target_dir, mode="w", encoding="utf-8-sig", newline="", delete=False
        ) as handle:
            temporary_path = Path(handle.name)
            try:
                frame.to_csv(handle, index=False, lineterminator="\n")
            except BaseException:
                handle.close()
                temporary_path.unlink()
                raise
        try:
            temporary_path.replace(path)
        finally:
            temporary_path.unlink(missing_ok=True)
        paths[name] = path
    return paths


def main() -> None:
    """Tạo ba CSV mẫu và in tổng điện của từng cơ sở."""
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    for path in generate_sample_files().values():
        source = pd.read_csv(path, encoding="utf-8-sig")
        total = source["Tong_dien_tieu_thu_kWh"].sum()
        print(f"{path.name} · {len(source)} ngày · {len(source.columns)} cột · {total:,.2f} kWh")


if __name__ == "__main__":
    main()
