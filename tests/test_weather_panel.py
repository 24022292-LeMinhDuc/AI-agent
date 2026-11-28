"""Kiểm tra hoạt động cập nhật thời tiết và tính toàn vẹn của số đo gốc."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import pytest
from streamlit.testing.v1 import AppTest

import db
import weather_panel
from weather_api import WeatherAPIError, WeatherReading, WeatherSnapshot

PROJECT_ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 11, 26, 3, 15, tzinfo=UTC)
SITE = ("Khu công nghiệp thử nghiệm", 16.07, 108.22)
OTHER_SITE = ("Điểm thử nghiệm thứ hai", 21.03, 105.85)


def _snapshot(temperature_c: float = 27.0) -> WeatherSnapshot:
    """Tạo phản hồi mẫu có cả nhiệt độ giờ gần đây và dự báo."""
    current = NOW.astimezone(ZoneInfo("Asia/Ho_Chi_Minh"))
    return WeatherSnapshot(
        timezone="Asia/Ho_Chi_Minh",
        fetched_at=NOW,
        current=WeatherReading(current, temperature_c, 72.0),
        hourly=(
            WeatherReading(current - timedelta(minutes=15), temperature_c, 72.0),
            WeatherReading(current + timedelta(minutes=45), temperature_c + 0.5, 70.0, True),
        ),
    )


@pytest.fixture(autouse=True)
def _clear_weather_cache():
    """Mỗi ca kiểm thử có bộ nhớ đệm riêng và dùng thời tiết mô phỏng."""
    weather_panel._cached_hourly_weather.clear()
    yield
    weather_panel._cached_hourly_weather.clear()


def _set_test_location(app: AppTest, location: tuple[str, float, float]) -> None:
    """Điền và lưu vị trí bằng các điều khiển trên Dashboard."""
    if not any(widget.key == "weather_name" for widget in app.text_input):
        app.button(key="weather_edit_location").click().run()
    name, latitude, longitude = location
    app.text_input(key="weather_name").set_value(name)
    app.number_input(key="weather_latitude").set_value(latitude)
    app.number_input(key="weather_longitude").set_value(longitude)
    app.button(key="weather_save_location").click().run()


def test_weather_is_opt_in_and_available_without_energy_data(monkeypatch, tmp_path: Path) -> None:
    """Chỉ gọi API sau khi chọn vị trí, kể cả khi chưa xác nhận dữ liệu điện."""
    calls: list[tuple[float, float]] = []

    def fetch(latitude: float, longitude: float) -> WeatherSnapshot:
        calls.append((latitude, longitude))
        return _snapshot()

    monkeypatch.setattr(weather_panel, "fetch_hourly_weather", fetch)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "runs.db")
    app = AppTest.from_file(str(PROJECT_ROOT / "app.py"), default_timeout=10).run()
    app.button(key="nav_2_Dashboard").click().run()
    assert not app.exception
    assert not calls
    assert not app.session_state["store"].get("mapping_confirmed")

    _set_test_location(app, SITE)

    assert not app.exception
    assert calls == [SITE[1:]]
    assert app.get("plotly_chart")
    assert not app.session_state["store"].get("mapping_confirmed")


def test_weather_persists_across_navigation_without_changing_meter_data(monkeypatch, tmp_path: Path) -> None:
    """Thời tiết lưu theo phiên và giữ nguyên dữ liệu đầu vào kiểm toán."""
    calls: list[tuple[float, float]] = []

    def fetch(latitude: float, longitude: float) -> WeatherSnapshot:
        calls.append((latitude, longitude))
        return _snapshot(temperature_c=38.0)

    monkeypatch.setattr(weather_panel, "fetch_hourly_weather", fetch)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "runs.db")
    app = AppTest.from_file(str(PROJECT_ROOT / "app.py"), default_timeout=10).run()
    app.button(key="load_sample_dataset").click().run()
    fingerprint = app.session_state["store"]["upload_fingerprint"]
    app.button(key=f"confirm_{fingerprint}").click().run()
    store = app.session_state["store"]
    source = store["measured_df"].copy(deep=True)
    cleaned = store["df"].copy(deep=True)
    quality_run = store["quality_run_id"]

    app.button(key="nav_2_Dashboard").click().run()
    original_dashboard = app.session_state["store"]["analysis_results"]["dashboard"]["run_id"]
    _set_test_location(app, SITE)
    _set_test_location(app, OTHER_SITE)
    app.button(key="nav_1_Dữ liệu").click().run()
    app.button(key="nav_2_Dashboard").click().run()

    store = app.session_state["store"]
    assert not app.exception
    assert calls == [SITE[1:], OTHER_SITE[1:]]
    assert len(app.get("plotly_chart")) >= 2
    assert store["quality_run_id"] == quality_run
    assert store["analysis_results"]["dashboard"]["run_id"] == original_dashboard
    pd.testing.assert_frame_equal(store["measured_df"], source)
    pd.testing.assert_frame_equal(store["df"], cleaned)


def test_hourly_refresh_keeps_only_matching_stale_weather_and_retries_after_five_minutes(monkeypatch) -> None:
    """Chia sẻ kết quả theo giờ và hạn chế thử lại khi API gián đoạn."""
    snapshot = _snapshot()
    calls: list[tuple[float, float]] = []

    def fetch(latitude: float, longitude: float) -> WeatherSnapshot:
        calls.append((latitude, longitude))
        if len(calls) == 1:
            return snapshot
        raise WeatherAPIError("Nguồn thời tiết đang gián đoạn.")

    monkeypatch.setattr(weather_panel, "fetch_hourly_weather", fetch)
    state: dict = {}
    assert weather_panel._refresh_weather(state, SITE, NOW) == (snapshot, None)
    same_hour = NOW + timedelta(minutes=30)
    assert weather_panel._refresh_weather({}, SITE, same_hour) == (snapshot, None)
    assert calls == [SITE[1:]]

    next_hour = NOW + timedelta(hours=1)
    reused, error = weather_panel._refresh_weather(state, SITE, next_hour)
    assert reused == snapshot
    assert error
    assert calls == [SITE[1:]] * 2

    assert weather_panel._refresh_weather(state, SITE, next_hour + timedelta(minutes=2)) == (snapshot, error)
    assert calls == [SITE[1:]] * 2
    assert weather_panel._refresh_weather(state, SITE, next_hour + timedelta(minutes=5)) == (snapshot, error)
    assert calls == [SITE[1:]] * 3

    unknown, error = weather_panel._refresh_weather(state, OTHER_SITE, next_hour + timedelta(minutes=6))
    assert unknown is None
    assert error
    assert calls[-1] == OTHER_SITE[1:]
