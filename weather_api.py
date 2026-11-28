"""Đọc thời tiết theo giờ từ Open-Meteo với thời gian tại điểm tra cứu."""

from __future__ import annotations

import itertools
from dataclasses import dataclass
from datetime import UTC, datetime
from http.client import HTTPException
from json import loads
from math import isfinite
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

_FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
_VARIABLES = "temperature_2m,relative_humidity_2m"
_MAX_RESPONSE_BYTES = 128 * 1024


class WeatherAPIError(RuntimeError):
    """Lỗi có thể hiển thị khi nguồn thời tiết không khả dụng."""


@dataclass(frozen=True, slots=True)
class WeatherReading:
    timestamp: datetime
    temperature_c: float | None
    humidity_pct: float | None
    is_forecast: bool = False


@dataclass(frozen=True, slots=True)
class WeatherSnapshot:
    timezone: str
    fetched_at: datetime
    current: WeatherReading
    hourly: tuple[WeatherReading, ...]


def _validate_coordinate(value: float, limit: int, name: str) -> None:
    """Kiểm tra giá trị số hữu hạn trong phạm vi tọa độ."""
    if (
        isinstance(value, bool)
        or not isinstance(value, (float, int))
        or not -limit <= value <= limit
        or not isfinite(value)
    ):
        raise ValueError(f"{name} phải nằm trong khoảng từ {-limit}° đến {limit}°.")


def _timestamp(value: object, zone: ZoneInfo) -> datetime:
    """Quy đổi mốc Unix sang giờ địa phương, kể cả khi đổi giờ mùa hè."""
    if type(value) is not int:
        raise ValueError("Mốc Unix phải là số nguyên.")
    return datetime.fromtimestamp(value, tz=UTC).astimezone(zone)


def _reading_value(value: object) -> float | None:
    """Giữ số đọc hữu hạn hoặc giá trị thiếu do API cung cấp."""
    if value is None:
        return None
    if type(value) not in (int, float) or not isfinite(value):
        raise ValueError("Giá trị thời tiết phải là số hữu hạn.")
    return float(value)


def _parse_snapshot(body: bytes) -> WeatherSnapshot:
    """Đọc một phản hồi có đúng mốc giờ, kiểu số và múi giờ."""
    try:
        payload = loads(body)
        if not isinstance(payload, dict) or payload.get("error"):
            raise ValueError("Phản hồi phải chứa dữ liệu cho một điểm.")

        zone = ZoneInfo(payload["timezone"])
        current_data = payload["current"]
        current_epoch = current_data["time"]
        current = WeatherReading(
            timestamp=_timestamp(current_epoch, zone),
            temperature_c=_reading_value(current_data["temperature_2m"]),
            humidity_pct=_reading_value(current_data["relative_humidity_2m"]),
        )

        hourly_data = payload["hourly"]
        epochs = hourly_data["time"]
        if not isinstance(epochs, list) or not epochs:
            raise ValueError("Cần có ít nhất một mốc giờ.")
        readings = tuple(
            WeatherReading(
                timestamp=_timestamp(epoch, zone),
                temperature_c=_reading_value(temperature),
                humidity_pct=_reading_value(humidity),
                is_forecast=epoch > current_epoch,
            )
            for epoch, temperature, humidity in zip(
                epochs,
                hourly_data["temperature_2m"],
                hourly_data["relative_humidity_2m"],
                strict=True,
            )
        )
        if any(after <= before for before, after in itertools.pairwise(epochs)):
            raise ValueError("Mốc giờ phải tăng dần theo thời gian.")
    except (KeyError, TypeError, ValueError, OverflowError, OSError) as exc:
        raise WeatherAPIError("Open-Meteo trả dữ liệu thời tiết không hợp lệ.") from exc

    if current.temperature_c is None and all(reading.temperature_c is None for reading in readings):
        raise WeatherAPIError("Open-Meteo chưa có dữ liệu nhiệt độ cho vị trí này.")
    return WeatherSnapshot(zone.key, datetime.now(UTC), current, readings)


def fetch_hourly_weather(
    latitude: float, longitude: float, *, timeout: float = 8.0
) -> WeatherSnapshot:
    """Lấy nhiệt độ và độ ẩm hiện tại cùng chuỗi giờ gần nhất và dự báo."""
    _validate_coordinate(latitude, 90, "Vĩ độ")
    _validate_coordinate(longitude, 180, "Kinh độ")

    query = urlencode({
        "latitude": latitude,
        "longitude": longitude,
        "current": _VARIABLES,
        "hourly": _VARIABLES,
        "past_hours": 24,
        "forecast_hours": 24,
        "timeformat": "unixtime",
        "timezone": "auto",
        "temperature_unit": "celsius",
    })
    request = Request(f"{_FORECAST_URL}?{query}", headers={"Accept": "application/json"})
    try:
        with urlopen(request, timeout=timeout) as response:
            body = response.read(_MAX_RESPONSE_BYTES + 1)
        if len(body) > _MAX_RESPONSE_BYTES:
            raise WeatherAPIError("Phản hồi của Open-Meteo vượt dung lượng dự kiến.")
    except HTTPError as exc:
        if exc.code == 429:
            raise WeatherAPIError("Open-Meteo đang giới hạn lượt truy cập. Hãy thử lại sau.") from exc
        raise WeatherAPIError("Open-Meteo tạm thời không trả được dữ liệu thời tiết.") from exc
    except (URLError, OSError, HTTPException) as exc:
        raise WeatherAPIError("Không kết nối được với Open-Meteo. Hãy kiểm tra mạng.") from exc

    return _parse_snapshot(body)
