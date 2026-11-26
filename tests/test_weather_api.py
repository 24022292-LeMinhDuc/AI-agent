"""Kiểm tra giao thức và chuẩn hóa dữ liệu Open-Meteo bằng phản hồi giả lập."""

from datetime import datetime, timedelta, timezone
from io import BytesIO
from json import dumps
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlsplit

import pytest

import weather_api
from weather_api import WeatherAPIError, fetch_hourly_weather


def _payload() -> dict:
    # Điểm tại múi giờ đổi giờ mùa hè với hai giờ địa phương cùng là 01:00.
    before = int(datetime(2026, 11, 1, 5, tzinfo=timezone.utc).timestamp())
    after = before + 3600
    return {
        "timezone": "America/New_York",
        "current": {"time": before + 2700, "temperature_2m": 18.1, "relative_humidity_2m": 80},
        "hourly": {
            "time": [before, after],
            "temperature_2m": [18.3, None],
            "relative_humidity_2m": [75, 85],
        },
    }


def test_hourly_request_and_timezone_aware_payload(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_urlopen(request, *, timeout):
        url = urlsplit(request.full_url)
        assert url.scheme == "https"
        assert url.netloc == "api.open-meteo.com"
        assert url.path == "/v1/forecast"
        query = parse_qs(url.query)
        assert query == {
            "latitude": ["40.7"],
            "longitude": ["-74.0"],
            "current": ["temperature_2m,relative_humidity_2m"],
            "hourly": ["temperature_2m,relative_humidity_2m"],
            "past_hours": ["24"],
            "forecast_hours": ["24"],
            "timezone": ["auto"],
            "timeformat": ["unixtime"],
            "temperature_unit": ["celsius"],
        }
        assert timeout == 8.0
        return BytesIO(dumps(_payload()).encode())

    monkeypatch.setattr(weather_api, "urlopen", fake_urlopen)
    result = fetch_hourly_weather(40.7, -74.0)

    assert result.timezone == "America/New_York"
    assert result.fetched_at.utcoffset() == timedelta(0)
    assert result.current.temperature_c == 18.1
    assert result.current.humidity_pct == 80
    assert result.current.timestamp.minute == 45
    first, second = result.hourly
    assert first.timestamp.hour == second.timestamp.hour == 1
    assert first.timestamp.utcoffset() == timedelta(hours=-4)
    assert second.timestamp.utcoffset() == timedelta(hours=-5)
    assert second.timestamp.timestamp() - first.timestamp.timestamp() == 3600
    assert not first.is_forecast
    assert second.is_forecast
    assert second.temperature_c is None


@pytest.mark.parametrize(("latitude", "longitude"), [
    (90.1, 106.7), (10.8, -180.1), (float("nan"), 106.7), (10.8, float("inf")), (True, 106.7),
    ("10.8", 106.7),
])
def test_invalid_coordinates_fail_before_request(monkeypatch: pytest.MonkeyPatch, latitude, longitude) -> None:
    def unexpected_request(*args, **kwargs):
        pytest.fail("Tọa độ sai không được gửi tới nhà cung cấp.")

    monkeypatch.setattr(weather_api, "urlopen", unexpected_request)
    with pytest.raises(ValueError, match="độ phải nằm trong khoảng"):
        fetch_hourly_weather(latitude, longitude)


@pytest.mark.parametrize("malformed", ["json", "arrays", "timezone", "unsorted", "overflow"])
def test_invalid_provider_payload_surfaces_a_friendly_error(monkeypatch: pytest.MonkeyPatch, malformed: str) -> None:
    payload = _payload()
    if malformed == "arrays":
        payload["hourly"]["temperature_2m"].pop()
    elif malformed == "timezone":
        payload["timezone"] = "Invalid/WeatherTime"
    elif malformed == "unsorted":
        payload["hourly"]["time"].reverse()
    elif malformed == "overflow":
        payload["current"]["time"] = 10 ** 100
    body = b"<!DOCTYPE html>" if malformed == "json" else dumps(payload).encode()
    monkeypatch.setattr(weather_api, "urlopen", lambda *args, **kwargs: BytesIO(body))

    with pytest.raises(WeatherAPIError, match="dữ liệu thời tiết không hợp lệ"):
        fetch_hourly_weather(10.8, 106.7)


@pytest.mark.parametrize(("error", "message"), [
    (TimeoutError(), "Không kết nối được"),
    (URLError("DNS"), "Không kết nối được"),
    (HTTPError("https://api.open-meteo.com", 429, "Busy", None, None), "đang giới hạn"),
])
def test_provider_errors_are_reported_without_transport_details(
    monkeypatch: pytest.MonkeyPatch, error: Exception, message: str,
) -> None:
    def fail(*args, **kwargs):
        raise error

    monkeypatch.setattr(weather_api, "urlopen", fail)

    with pytest.raises(WeatherAPIError, match=message):
        fetch_hourly_weather(10.8, 106.7)
