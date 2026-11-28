"""Bảng theo dõi thời tiết theo giờ cho Dashboard."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from math import isfinite
from typing import Any
from zoneinfo import ZoneInfo

import plotly.graph_objects as go
import streamlit as st

from weather_api import WeatherAPIError, WeatherSnapshot, fetch_hourly_weather

Location = tuple[str, float, float]
DEMO_LOCATION: Location = ("TP. Hồ Chí Minh (mẫu)", 10.7769, 106.7009)
ERROR_RETRY_DELAY = timedelta(minutes=5)


@st.cache_data(ttl=7200, max_entries=128, show_spinner=False)
def _cached_hourly_weather(latitude: float, longitude: float, hour: int) -> WeatherSnapshot:
    """Dùng cùng một phản hồi cho tọa độ và giờ UTC đang theo dõi."""
    return fetch_hourly_weather(latitude, longitude)


def _refresh_weather(
    state: dict[str, Any], location: Location, now: datetime
) -> tuple[WeatherSnapshot | None, str | None]:
    """Tải dữ liệu khi sang giờ và giữ kết quả cùng tọa độ khi API lỗi."""
    _, latitude, longitude = location
    coordinates = (latitude, longitude)
    if state.get("coordinates") != coordinates:
        state.clear()
        state["coordinates"] = coordinates

    hour = int(now.timestamp()) // 3600
    retry_after = state.get("retry_after")
    if state.get("hour") != hour and (retry_after is None or now >= retry_after):
        try:
            state["snapshot"] = _cached_hourly_weather(latitude, longitude, hour)
        except (WeatherAPIError, ValueError) as exc:
            state["error"] = str(exc)
            state["retry_after"] = now + ERROR_RETRY_DELAY
        else:
            state["hour"] = hour
            state.pop("error", None)
            state.pop("retry_after", None)
    return state.get("snapshot"), state.get("error")


def _make_location(name: str, latitude: Any, longitude: Any) -> Location | None:
    """Kiểm tra tọa độ nhập qua biểu mẫu hoặc cấu hình khởi động."""
    values = ((latitude, 90), (longitude, 180))
    if any(
        type(value) not in (int, float) or not -limit <= value <= limit or not isfinite(value)
        for value, limit in values
    ):
        return None
    point = name.strip() or f"{latitude:.4f}°, {longitude:.4f}°"
    return point, round(float(latitude), 4), round(float(longitude), 4)


def _temperature_figure(snapshot: WeatherSnapshot) -> go.Figure:
    """Vẽ nhiệt độ với trục thời gian thực và nhãn giờ tại điểm tra cứu."""
    figure = go.Figure()
    for forecast, title, color, dash in (
        (False, "Các giờ gần đây · mô hình", "#138676", "solid"),
        (True, "Dự báo tại lần tải", "#436f94", "dash"),
    ):
        readings = [reading for reading in snapshot.hourly if reading.is_forecast == forecast]
        if not readings:
            continue
        figure.add_scatter(
            x=[int(reading.timestamp.timestamp() * 1000) for reading in readings],
            y=[reading.temperature_c for reading in readings],
            customdata=[reading.timestamp.strftime("%d/%m %H:%M %z") for reading in readings],
            name=title,
            mode="lines+markers",
            line={"color": color, "dash": dash, "width": 2},
            marker={"size": 4},
            hovertemplate="%{customdata}<br>%{y:.1f} °C<extra>" + title + "</extra>",
        )

    ticks = snapshot.hourly[::8]
    figure.update_xaxes(
        type="date",
        tickvals=[int(reading.timestamp.timestamp() * 1000) for reading in ticks],
        ticktext=[reading.timestamp.strftime("%d/%m %H:%M") for reading in ticks],
        title_text="Giờ địa phương",
    )
    figure.update_yaxes(title_text="Nhiệt độ (°C)", gridcolor="#e8efed")
    figure.update_layout(
        height=320,
        paper_bgcolor="#ffffff",
        plot_bgcolor="#ffffff",
        margin={"t": 60, "b": 55, "l": 55, "r": 20},
        legend={"orientation": "h", "y": 1.15},
    )
    return figure


@st.fragment(run_every="1min")
def _show_weather_snapshot(state: dict[str, Any], location: Location) -> None:
    """Kiểm tra giờ hiện tại mà không chạy lại các phép tính năng lượng."""
    now = datetime.now(UTC)
    snapshot, error = _refresh_weather(state, location, now)
    if error:
        message = f"{error} Đang hiển thị lần tải gần nhất." if snapshot else error
        st.warning(message)
    if snapshot is None:
        return

    current = snapshot.current
    temperature = "—" if current.temperature_c is None else f"{current.temperature_c:.1f} °C"
    humidity = "—" if current.humidity_pct is None else f"{current.humidity_pct:.0f}%"
    temperature_col, humidity_col = st.columns(2)
    temperature_col.metric("Nhiệt độ hiện tại · mô hình", temperature, border=True)
    humidity_col.metric("Độ ẩm tương đối · mô hình", humidity, border=True)
    fetched = snapshot.fetched_at.astimezone(ZoneInfo(snapshot.timezone))
    st.caption(
        f"Mốc hiện tại {current.timestamp:%d/%m/%Y %H:%M} · "
        f"Tải lúc {fetched:%d/%m/%Y %H:%M} · {snapshot.timezone}"
    )
    st.plotly_chart(
        _temperature_figure(snapshot),
        width="stretch",
        alt="Nhiệt độ theo giờ, nét liền cho các giờ gần đây và nét đứt cho các giờ dự báo.",
        key="weather_temperature_chart",
    )


def _show_location_form(state: dict[str, Any], location: Location | None) -> None:
    """Xác nhận tọa độ trước khi gửi yêu cầu tới nguồn thời tiết."""
    st.caption("Nhập tọa độ điểm cần theo dõi hoặc thử với vị trí mẫu. Tọa độ được gửi tới Open-Meteo.")
    with st.form("weather_location_form"):
        name = st.text_input("Tên địa điểm", value=location[0] if location else "", key="weather_name")
        lat_col, lon_col = st.columns(2)
        latitude = lat_col.number_input(
            "Vĩ độ (°)", min_value=-90.0, max_value=90.0, value=location[1] if location else None,
            step=0.0001, format="%.4f", key="weather_latitude",
        )
        longitude = lon_col.number_input(
            "Kinh độ (°)", min_value=-180.0, max_value=180.0, value=location[2] if location else None,
            step=0.0001, format="%.4f", key="weather_longitude",
        )
        if st.form_submit_button("Áp dụng vị trí", type="primary", key="weather_save_location"):
            updated = _make_location(name, latitude, longitude)
            if updated is None:
                st.error("Nhập đủ vĩ độ và kinh độ hợp lệ.")
            else:
                state.update(location=updated, editing=False)
                st.rerun()
    if st.button("Dùng vị trí mẫu", key="weather_use_demo"):
        state.update(location=DEMO_LOCATION, editing=False)
        st.rerun()


def show_hourly_weather(config: dict[str, Any] | None = None) -> None:
    """Hiển thị thời tiết sau khi có tọa độ, độc lập với tệp năng lượng."""
    state: dict[str, Any] = st.session_state.setdefault("hourly_weather", {})
    if "location" not in state:
        settings = config if isinstance(config, dict) else {}
        state["location"] = _make_location(
            str(settings.get("location_name") or ""), settings.get("latitude"), settings.get("longitude")
        )
    location = state["location"]
    with st.expander("Thời tiết theo giờ", expanded=location is not None):
        st.caption(
            "Nguồn [Open-Meteo](https://open-meteo.com/). Các giá trị là dữ liệu mô hình. "
            "Bảng cập nhật khi sang giờ trong lúc Dashboard mở. "
            "EnPI và đường cơ sở dùng nhiệt độ đã ánh xạ từ tệp."
        )
        if location is not None:
            site_col, edit_col, stop_col = st.columns([2, 1, 1])
            site_col.text(location[0])
            if edit_col.button("Đổi vị trí", key="weather_edit_location"):
                state["editing"] = not state.get("editing", False)
            if stop_col.button("Ngừng theo dõi", key="weather_stop"):
                state.update(location=None, editing=False)
                state.pop("fetch", None)
                st.rerun()
        if location is None or state.get("editing"):
            _show_location_form(state, location)
        if location is not None:
            _show_weather_snapshot(state.setdefault("fetch", {}), location)
