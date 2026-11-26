"""Sinh ba bộ dữ liệu kiểm toán năng lượng mẫu có thể tái lập."""

from __future__ import annotations

from pathlib import Path
import sys

import numpy as np
import pandas as pd


SEED = 42
SENSOR_ERROR_ROWS = (17, 88, 162, 247, 330)

SHORT_COLUMNS = (
    "date",
    "output_tons",
    "ambient_C",
    "Prod_kWh",
    "Chiller_kWh",
    "AirComp_kWh",
    "Light_kWh",
)

VIETNAMESE_COLUMNS = (
    "Ngày",
    "Sản lượng (tấn)",
    "Nhiệt độ ngoài trời",
    "Điện sản xuất (kWh)",
    "Điện chiller (kWh)",
    "Điện khí nén (kWh)",
    "Điện chiếu sáng (kWh)",
)


def generate_sample_frames(seed: int = SEED) -> dict[str, pd.DataFrame]:
    """Sinh dữ liệu ngày cho ba định dạng CSV mẫu."""
    rng = np.random.default_rng(seed)
    dates = pd.date_range("2025-11-01", "2026-10-31", freq="D")

    day_of_year = dates.day_of_year.to_numpy()
    temperature = (
        27.0
        + 6.5 * np.cos(2 * np.pi * (day_of_year - 200) / 365)
        + rng.normal(0, 1.5, len(dates))
    )
    temperature = np.clip(temperature, 16, 39)

    weekend_adjustment = np.where(dates.day_of_week.to_numpy() >= 5, -8.0, 0.0)
    production = np.clip(
        52.0 + weekend_adjustment + rng.normal(0, 4.0, len(dates)), 25, None
    )

    production_kwh = production * 180 + rng.normal(0, 200, len(dates))
    chiller_kwh = (
        2200
        + 450 * np.maximum(temperature - 24, 0)
        + rng.normal(0, 150, len(dates))
    )
    compressed_air_kwh = 1800 + 6 * production + rng.normal(0, 90, len(dates))
    lighting_kwh = 1300 + rng.normal(0, 25, len(dates))

    chiller_kwh = np.round(chiller_kwh, 2)
    chiller_kwh[list(SENSOR_ERROR_ROWS)] = -999
    sample_b = pd.DataFrame(
        {
            "date": dates.strftime("%Y-%m-%d"),
            "output_tons": np.round(production, 2),
            "ambient_C": np.round(temperature, 2),
            "Prod_kWh": np.round(production_kwh, 2),
            "Chiller_kWh": chiller_kwh,
            "AirComp_kWh": np.round(compressed_air_kwh, 2),
            "Light_kWh": np.round(lighting_kwh, 2),
        },
        columns=SHORT_COLUMNS,
    )

    sample_a = sample_b.copy()
    sample_a.columns = VIETNAMESE_COLUMNS

    sample_c = sample_b.copy()
    valid_chiller = sample_c["Chiller_kWh"] != -999
    sample_c.loc[valid_chiller, "Chiller_kWh"] /= 1000
    note_rng = np.random.default_rng(seed + 1)
    sample_c["Ghi chú"] = note_rng.choice(
        ["", "", "", "kiểm tra", "bảo trì", "ghi chú"], size=len(dates)
    )

    return {"sample_a": sample_a, "sample_b": sample_b, "sample_c": sample_c}


def generate_sample_files(
    output_dir: Path | None = None, seed: int = SEED
) -> dict[str, Path]:
    """Ghi ba bộ dữ liệu mẫu thành các file CSV."""
    target_dir = output_dir or Path(__file__).resolve().parent
    target_dir.mkdir(parents=True, exist_ok=True)

    paths: dict[str, Path] = {}
    for name, frame in generate_sample_frames(seed).items():
        path = target_dir / f"{name}.csv"
        frame.to_csv(path, index=False, encoding="utf-8-sig")
        paths[name] = path
    return paths


def print_monthly_energy_totals(sample_a: pd.DataFrame) -> None:
    """In tổng điện tháng của file A, bỏ qua lỗi cảm biến -999."""
    date_column = "Ngày"
    energy_columns = [column for column in VIETNAMESE_COLUMNS if column.startswith("Điện")]
    clean_data = sample_a.copy()
    clean_data[energy_columns] = clean_data[energy_columns].replace(-999, np.nan)
    clean_data["Tháng"] = pd.to_datetime(clean_data[date_column]).dt.to_period("M").astype(str)

    monthly_totals = clean_data.groupby("Tháng")[energy_columns].sum().sum(axis=1)
    print("Tổng điện file A theo tháng (kWh; bỏ qua chiller = -999):")
    print(monthly_totals.round(2).to_string())


def main() -> None:
    """Sinh CSV mẫu và in tổng điện theo tháng."""
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    paths = generate_sample_files()
    print("Đã tạo các file:")
    for name, path in paths.items():
        print(f"- {path}")
    print_monthly_energy_totals(pd.read_csv(paths["sample_a"], encoding="utf-8-sig"))


if __name__ == "__main__":
    main()
