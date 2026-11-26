"""Giao diện Streamlit demo cho kiểm toán năng lượng lịch sử."""

from __future__ import annotations

import csv
import hashlib
import json
import os
import uuid
from collections.abc import Callable
from datetime import timedelta
from html import escape
from io import BytesIO, TextIOWrapper
from pathlib import Path
from typing import Any

import pandas as pd
import plotly.graph_objects as go
import streamlit as st
import yaml
from plotly.subplots import make_subplots

import db
from agent import ask, run_tool
from energy_baseline import BASELINE_DRIVER_LABELS
from mapping import (
    CANONICAL_COLUMNS,
    apply_mapping,
    detect_unit_issues,
    suggest_mapping,
    validate_mapped_data,
)
from report import (
    audit_summary_rows,
    build_report_data,
    generate_interpretation,
    render_docx,
    render_markdown,
    render_report_json,
)
from tools import TOTAL_METER, submeter_columns, total_meter_columns
from weather_panel import show_hourly_weather


CATEGORY_LABELS = {
    "kwh_production": "Điện sản xuất",
    "kwh_chiller": "Điện chiller",
    "kwh_compressed_air": "Điện khí nén",
    "kwh_lighting": "Điện chiếu sáng",
    "kwh_other": "Điện phụ tải khác",
    "kwh_total": "Điện tổng từ tệp",
}
METER_COLORS = {
    "kwh_production": "#138676",
    "kwh_chiller": "#5590b3",
    "kwh_compressed_air": "#d9944c",
    "kwh_lighting": "#907ab8",
    "kwh_other": "#6d9291",
}

SAMPLE_FILES = {
    "Nhà máy": "sample_a.csv",
    "Tòa nhà": "sample_b.csv",
    "Kiểm tra chất lượng dữ liệu": "sample_c.csv",
}

PAGES = {
    "Dữ liệu": (1, ":material/upload_file:"),
    "Dashboard": (2, ":material/monitoring:"),
    "SEU & EnPI": (3, ":material/query_stats:"),
    "Cơ hội": (4, ":material/energy_savings_leaf:"),
    "Báo cáo": (6, ":material/description:"),
    "Hỏi đáp": (5, ":material/forum:"),
}
MAX_DERIVED_CACHE_ENTRIES = 24


def _apply_styles() -> None:
    """Nạp kiểu trình bày dùng chung cho giao diện."""
    st.html(Path(__file__).with_name("assets") / "workspace.css")


def _render_navigation() -> str:
    """Hiển thị điều hướng chính trong thanh bên."""
    current_page = st.session_state.setdefault("active_page", "Dữ liệu")
    if current_page not in PAGES:
        current_page = "Dữ liệu"
        st.session_state["active_page"] = current_page

    with st.sidebar:
        st.html(
            """
            <div class="ve-brand">
                <span class="ve-brand-mark" aria-hidden="true">ϟ</span>
                <span class="ve-brand-copy">
                    <strong>Virtual Energy Engineer</strong>
                    <small>Kiểm toán năng lượng</small>
                </span>
            </div>
            """
        )
        with st.container(key="ve_navigation"):
            for page, (index, icon) in PAGES.items():
                if st.button(
                    page,
                    key=f"nav_{index}_{page}",
                    icon=icon,
                    type="primary" if page == current_page else "tertiary",
                    width="stretch",
                ):
                    st.session_state["active_page"] = page
                    st.rerun()

    return current_page


def _render_dataset_status(store: dict[str, Any]) -> None:
    """Hiển thị trạng thái dữ liệu sau các thao tác của trang hiện tại."""
    with st.sidebar:
        dataset = store.get("filename")
        if store.get("mapping_confirmed"):
            status = "Dữ liệu đã xác nhận"
            detail = dataset or "Sẵn sàng phân tích"
        elif dataset:
            status = "Đang chuẩn bị dữ liệu"
            detail = dataset
        else:
            status = "Chưa nạp dữ liệu"
            detail = "CSV hoặc Excel XLSX"
        st.html(
            f'<div class="ve-dataset-status"><strong>{status}</strong>'
            f'<small>{escape(detail)}</small></div>'
        )


def _page_heading(title: str, description: str, *, entry: bool = False) -> None:
    """Hiển thị tiêu đề ngắn và mục đích của trang."""
    class_name = "ve-page-intro ve-entry" if entry else "ve-page-intro"
    st.html(
        f'<header class="{class_name}"><h1>{escape(title)}</h1>'
        f"<p>{escape(description)}</p></header>"
    )


def _load_config() -> dict[str, Any]:
    """Đọc cấu hình local của ứng dụng."""
    config_path = Path(__file__).with_name("config.yaml")
    with config_path.open("r", encoding="utf-8") as config_file:
        return yaml.safe_load(config_file) or {}


def _read_upload_bytes(
    filename: str, content: bytes, sheet: str | int = 0
) -> pd.DataFrame:
    """Đọc CSV UTF-8/UTF-16 hoặc bảng đã chọn trong tệp Excel."""
    file_bytes = BytesIO(content)
    suffix = Path(filename).suffix.lower()
    if suffix == ".csv":
        encoding = "utf-16" if content.startswith((b"\xff\xfe", b"\xfe\xff")) else "utf-8-sig"
        sample = TextIOWrapper(BytesIO(content), encoding=encoding)
        with sample:
            header = next((line for line in sample if line.strip()), "")
        try:
            delimiter = csv.Sniffer().sniff(header, delimiters=",;\t").delimiter
        except csv.Error:
            delimiter = ","
        return pd.read_csv(
            file_bytes, encoding=encoding, sep=delimiter, decimal="," if delimiter == ";" else "."
        )
    if suffix == ".xlsx":
        return pd.read_excel(file_bytes, sheet_name=sheet)
    raise ValueError("Chỉ hỗ trợ file CSV hoặc XLSX.")


def _invalidate_analysis_state(store: dict[str, Any]) -> None:
    """Xóa phân tích và hội thoại thuộc phiên dữ liệu trước đó."""
    for key in (
        "df",
        "clean_df",
        "measured_df",
        "confirmed_mapping",
        "confirmed_unit_factors",
        "seu_threshold_value",
        "enpi_drivers",
        "quality_report",
        "quality_run_id",
        "opportunities_tool_response",
        "report_export",
        "analysis_results",
        "compare_periods_response",
        "compare_periods_args",
        "dashboard_filters",
        "baseline_filters",
        "baseline_args",
        "chat_history",
        "selected_context",
        "pending_question",
        "decision_scope_id",
        "_derived_cache",
    ):
        store.pop(key, None)
    store["mapping_confirmed"] = False
    store["chat_history"] = []
    store["run_scope_id"] = uuid.uuid4().hex


def _invalidate_sheet_state(store: dict[str, Any]) -> None:
    """Xóa bản nháp ánh xạ và kết quả thuộc bảng nguồn trước đó."""
    _invalidate_analysis_state(store)
    for key in ("raw_df", "mapping", "mapping_seed", "draft_unit_factors", "upload_error"):
        store.pop(key, None)
    store["mapping_revision"] = int(store.get("mapping_revision", 0)) + 1


def _invalidate_dataset_state(store: dict[str, Any]) -> None:
    """Xóa tệp nguồn và mọi kết quả liên quan."""
    _invalidate_sheet_state(store)
    for key in (
        "filename",
        "upload_size_bytes",
        "upload_fingerprint",
        "upload_file_id",
        "upload_sheet",
        "upload_sheet_names",
        "workbook_content",
    ):
        store.pop(key, None)


def _on_upload_change(widget_key: str) -> None:
    """Phân biệt thao tác xóa trên uploader và việc widget được dựng lại."""
    if st.session_state.get(widget_key) is None:
        _invalidate_dataset_state(st.session_state["store"])


def _clear_dataset_state() -> None:
    """Xóa upload, phân tích, hội thoại và quyết định tạm của dataset hiện tại."""
    store = st.session_state.get("store", {})
    _invalidate_dataset_state(store)
    st.session_state["upload_widget_revision"] = int(
        st.session_state.get("upload_widget_revision", 0)
    ) + 1


def _decision_scope_id(store: dict[str, Any]) -> str:
    """Tạo phạm vi ổn định quyết định theo session, file và mapping."""
    scope = {
        "session": store.get("session_scope_id", ""),
        "file": store.get("upload_fingerprint", ""),
        "sheet": store.get("upload_sheet"),
        "mapping": store.get("confirmed_mapping", {}),
        "unit_factors": store.get("confirmed_unit_factors", {}),
    }
    encoded = json.dumps(scope, ensure_ascii=False, sort_keys=True).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _handoff_to_chat(
    store: dict[str, Any], title: str, run_ids: list[int], question: str
) -> None:
    """Chuyển câu hỏi cùng nguồn tính toán sang Hỏi đáp."""
    store["selected_context"] = {"title": title, "run_ids": run_ids}
    store["pending_question"] = question
    st.session_state["active_page"] = "Hỏi đáp"
    st.rerun()


def _session_cached(
    store: dict[str, Any], key: tuple[Any, ...], factory: Callable[[], Any]
) -> Any:
    """Tái sử dụng kết quả trong phiên hiện tại, không chia sẻ giữa người dùng."""
    cache = store.setdefault("_derived_cache", {})
    if key in cache:
        value = cache.pop(key)
    else:
        value = factory()
        if isinstance(value, dict):
            result = value.get("result")
            if "error" in value or (isinstance(result, dict) and "error" in result):
                return value
    cache[key] = value
    if len(cache) > MAX_DERIVED_CACHE_ENTRIES:
        del cache[next(iter(cache))]
    return value


def _build_dashboard_figure(aggregated: pd.DataFrame, kwh_cols: list[str]) -> go.Figure:
    """Dựng biểu đồ tổng điện, nhiệt độ và sản lượng."""
    panels = [("energy", "Điện năng tiêu thụ")]
    if "temp_c" in aggregated.columns and aggregated["temp_c"].notna().any():
        panels.append(("temperature", "Nhiệt độ ngoài trời từ tệp"))
    if "production_t" in aggregated.columns and aggregated["production_t"].notna().any():
        panels.append(("production", "Sản lượng"))
    panel_weights = {"energy": 0.55, "temperature": 0.22, "production": 0.23}
    heights = [panel_weights[name] for name, _ in panels]
    normalized_heights = [weight / sum(heights) for weight in heights]
    figure = make_subplots(
        rows=len(panels),
        cols=1,
        shared_xaxes=True,
        vertical_spacing=0.08 if len(panels) > 1 else 0,
        row_heights=normalized_heights,
        subplot_titles=[title for _, title in panels],
    )
    for column in submeter_columns(kwh_cols):
        figure.add_trace(
            go.Bar(
                x=aggregated["timestamp"],
                y=aggregated[column],
                name=CATEGORY_LABELS.get(column, column),
                marker_color=METER_COLORS.get(column),
            ),
            row=1,
            col=1,
        )
    figure.add_trace(
        go.Scatter(
            x=aggregated["timestamp"],
            y=aggregated["kwh_total"],
            name="Tổng điện",
            mode="lines",
            line={"color": "#183b3a", "width": 2},
        ),
        row=1,
        col=1,
    )
    panel_rows = {name: index for index, (name, _) in enumerate(panels, start=1)}
    if "temperature" in panel_rows:
        figure.add_trace(
            go.Scatter(
                x=aggregated["timestamp"],
                y=aggregated["temp_c"],
                name="Nhiệt độ (°C)",
                mode="lines",
                line={"color": "#b96c25"},
            ),
            row=panel_rows["temperature"],
            col=1,
        )
        figure.update_yaxes(title_text="°C", row=panel_rows["temperature"], col=1)
    if "production" in panel_rows:
        figure.add_trace(
            go.Scatter(
                x=aggregated["timestamp"],
                y=aggregated["production_t"],
                name="Sản lượng (tấn)",
                mode="lines",
                line={"color": "#4275a0"},
            ),
            row=panel_rows["production"],
            col=1,
        )
        figure.update_yaxes(title_text="Tấn", row=panel_rows["production"], col=1)
    figure.update_layout(
        height=330 + 135 * (len(panels) - 1),
        barmode="stack",
        paper_bgcolor="#ffffff",
        plot_bgcolor="#ffffff",
        hovermode="x unified",
        legend={"orientation": "h", "y": 1.03, "x": 0},
        margin={"t": 82, "b": 35, "l": 55, "r": 20},
    )
    figure.update_yaxes(gridcolor="#e8efed", zerolinecolor="#d6e6e2")
    figure.update_yaxes(title_text="kWh", row=1, col=1)
    figure.update_xaxes(title_text="Thời gian", row=len(panels), col=1)
    return figure


def _build_seu_figure(pareto: pd.DataFrame, threshold: float) -> go.Figure:
    """Dựng biểu đồ Pareto và đánh dấu hạng mục SEU."""
    colors = ["#138676" if is_seu else "#b6cdc7" for is_seu in pareto["is_seu"]]
    figure = make_subplots(specs=[[{"secondary_y": True}]])
    figure.add_trace(
        go.Bar(
            x=[CATEGORY_LABELS.get(item, item) for item in pareto.index],
            y=pareto["kwh"],
            marker_color=colors,
            name="Điện năng",
        ),
        secondary_y=False,
    )
    figure.add_trace(
        go.Scatter(
            x=[CATEGORY_LABELS.get(item, item) for item in pareto.index],
            y=pareto["cum"],
            name="Tích lũy",
            mode="lines+markers",
            line={"color": "#325f87", "width": 2},
        ),
        secondary_y=True,
    )
    figure.add_trace(
        go.Scatter(
            x=[CATEGORY_LABELS.get(item, item) for item in pareto.index],
            y=[threshold] * len(pareto),
            name="Ngưỡng SEU",
            mode="lines",
            line={"color": "#ad661e", "dash": "dash"},
        ),
        secondary_y=True,
    )
    figure.update_yaxes(title_text="kWh", secondary_y=False)
    figure.update_yaxes(
        title_text="Tỷ trọng tích lũy",
        tickformat=".0%",
        range=[0, 1.05],
        secondary_y=True,
    )
    figure.update_layout(
        height=400,
        paper_bgcolor="#ffffff",
        plot_bgcolor="#ffffff",
        margin={"t": 62, "b": 42, "l": 50, "r": 50},
        legend={"orientation": "h", "y": 1.12},
    )
    figure.update_yaxes(gridcolor="#e8efed", zerolinecolor="#d6e6e2")
    return figure


def _show_mapping_guide(store: dict[str, Any]) -> bool:
    """Hiện hướng dẫn nếu dữ liệu chưa được xác nhận ánh xạ."""
    if not store.get("mapping_confirmed") or not isinstance(store.get("df"), pd.DataFrame):
        st.info("Tải dữ liệu và xác nhận mapping trong tab **Dữ liệu** để xem phân tích.")
        return False
    return True


def _show_interval_quality(quality: dict[str, int]) -> None:
    """Hiển thị cách xử lý các bản ghi lặp và xung đột thời gian."""
    if duplicate_rows := quality.get("duplicate_rows_removed"):
        st.caption(f"Đã loại {duplicate_rows:,} dòng lặp có số đo giống nhau.")
    if conflicts := quality.get("conflicting_timestamps"):
        rows = quality.get("conflicting_timestamp_rows", 0)
        st.warning(
            f"Có {conflicts:,} mốc thời gian ghi số đo xung đột. "
            f"Phép tính đã loại {rows:,} dòng tại các mốc này."
        )


def _show_dashboard(store: dict[str, Any]) -> None:
    """Hiển thị xu hướng điện, nhiệt độ và sản lượng."""
    if not _show_mapping_guide(store):
        return

    df = store["df"]
    kwh_cols = [column for column in df.columns if str(column).startswith("kwh_")]
    if not kwh_cols:
        st.info("Mapping chưa có cột điện chuẩn bắt đầu bằng `kwh_`.")
        return
    if "timestamp" not in df.columns:
        st.info("Dashboard cần cột thời gian để lọc và sắp xếp biểu đồ.")
        return

    timestamps = pd.to_datetime(df["timestamp"], errors="coerce")
    valid_timestamps = timestamps.dropna()
    if valid_timestamps.empty:
        st.info("Không có ngày hợp lệ trong cột thời gian đã chuẩn hóa.")
        return

    min_date = valid_timestamps.min().date()
    max_date = valid_timestamps.max().date()
    dashboard_state_key = str(
        store.get("decision_scope_id") or store.get("upload_fingerprint", "dataset")
    )
    filters = store.setdefault("dashboard_filters", {})
    filter_column, period_column = st.columns([1.7, 1])
    with filter_column:
        date_selection = st.date_input(
            "Khoảng thời gian",
            value=filters.get("dates", (min_date, max_date)),
            min_value=min_date,
            max_value=max_date,
            key=f"dashboard_dates_{dashboard_state_key}",
        )
    with period_column:
        levels = {"Ngày": "day", "Tuần": "week", "Tháng": "month"}
        selected_level = st.selectbox(
            "Mức tổng hợp",
            list(levels),
            index=list(levels).index(filters.get("level", "Ngày")),
            key=f"dashboard_level_{dashboard_state_key}",
        )
    filters["level"] = selected_level

    if isinstance(date_selection, tuple):
        if len(date_selection) == 2:
            start_date, end_date = date_selection
        else:
            st.info("Chọn ngày bắt đầu và kết thúc để xem Dashboard.")
            return
    else:
        start_date = end_date = date_selection
    if start_date is None or end_date is None:
        st.info("Chọn ngày bắt đầu và kết thúc để xem Dashboard.")
        return
    if start_date > end_date:
        st.warning("Ngày bắt đầu phải nhỏ hơn hoặc bằng ngày kết thúc.")
        return
    filters["dates"] = (start_date, end_date)

    data_key = id(df)
    level = levels[selected_level]
    args = {
        "level": level,
        "start_date": start_date.isoformat(),
        "end_date": end_date.isoformat(),
    }
    dashboard_response = _session_cached(
        store,
        ("dashboard_result", data_key, level, args["start_date"], args["end_date"]),
        lambda: run_tool("dashboard_data", args, store),
    )
    if "run_id" not in dashboard_response:
        st.error(dashboard_response.get("error", "Không thể tổng hợp dữ liệu Dashboard."))
        return
    dashboard_result = dashboard_response.get("result", {})
    if isinstance(dashboard_result, dict) and "error" in dashboard_result:
        st.error(dashboard_result["error"])
        return
    aggregated = dashboard_result["aggregated"]
    summary = dashboard_result["summary"]
    store.setdefault("analysis_results", {})["dashboard"] = dashboard_response

    kpis: list[tuple[str, str]] = []
    if summary.get("total_kwh") is not None:
        kpis.append(("Tổng điện năng (kWh)", _format_value(summary["total_kwh"])))
    if summary.get("production_tons") is not None:
        kpis.append(("Sản lượng (tấn)", _format_value(summary["production_tons"])))
    if summary.get("kwh_per_ton") is not None:
        kpis.append(("Suất tiêu hao (kWh/tấn)", _format_value(summary["kwh_per_ton"])))
    if summary.get("average_temp_c") is not None:
        kpis.append(("Nhiệt độ trung bình (°C)", _format_value(summary["average_temp_c"])))
    if not kpis:
        st.info("Khoảng ngày đã chọn chưa có giá trị cho các KPI tổng quan.")
    else:
        with st.container(key="ve_dashboard_metrics"):
            for column, (label, value) in zip(st.columns(len(kpis)), kpis):
                column.metric(label, value, border=True)
    cost = summary.get("estimated_cost_vnd")
    if cost is not None:
        st.caption(
            f"Chi phí ước tính **{_format_value(cost, 0)} VNĐ** theo giá điện giả định "
            f"{_format_value(summary.get('tariff_vnd_per_kwh'), 0)} VNĐ/kWh."
        )
    st.caption(
        f"Khoảng {start_date:%Y-%m-%d} đến {end_date:%Y-%m-%d} · "
        f"run_id {dashboard_response['run_id']} · chỉ số tính từ dataset đã xác nhận."
    )
    if summary.get("total_source") == "metered_total":
        st.caption("Điện tổng lấy từ cột công tơ tổng. Biểu đồ cột thể hiện các phụ tải.")
    elif summary.get("total_source") == "submeter_sum":
        if TOTAL_METER in df:
            st.caption("Tệp chưa có số đo hợp lệ ở công tơ tổng. Điện tổng được tính từ các phụ tải có số đo.")
        else:
            st.caption("Điện tổng được tính từ các phụ tải có số đo trong phạm vi đã ánh xạ.")
    if audit := summary.get("reported_audit"):
        rows = audit_summary_rows(audit)
        if rows:
            with st.expander("Vận hành, nhiệt và phát thải"):
                st.caption("Chi phí và phát thải được tổng hợp từ cột đã khai báo trong tệp.")
                st.dataframe(
                    pd.DataFrame(rows, columns=["Chỉ số", "Giá trị", "Dòng dùng tính"]),
                    hide_index=True,
                    width="stretch",
                )
    if "anomaly_note" in df:
        in_period = df["timestamp"].ge(pd.Timestamp(start_date)) & df["timestamp"].lt(
            pd.Timestamp(end_date) + pd.Timedelta(days=1)
        )
        notes = df.loc[in_period & df["anomaly_note"].notna(), ["timestamp", "anomaly_note"]]
        if not notes.empty:
            with st.expander("Ghi chú bất thường từ tệp"):
                st.dataframe(
                    notes.rename(columns={"timestamp": "Thời điểm", "anomaly_note": "Ghi chú"}),
                    hide_index=True,
                    width="stretch",
                )
    energy_rows = summary.get("energy_rows_by_column", {})
    if energy_rows:
        with st.expander("Độ phủ số đo trong khoảng đã chọn"):
            coverage = pd.DataFrame(
                {
                    "Hạng mục": [CATEGORY_LABELS.get(name, name) for name in energy_rows],
                    "Dòng có số đo": list(energy_rows.values()),
                }
            )
            st.dataframe(coverage, hide_index=True, width="stretch")
            if summary.get("intensity_rows") is not None:
                st.caption(f"Dòng ghép được điện năng với sản lượng · {summary['intensity_rows']:,}")
            _show_interval_quality(summary.get("interval_quality", {}))
            if reconciliation := summary.get("energy_reconciliation"):
                st.caption(
                    f"Đối chiếu {reconciliation['paired_rows']:,} dòng đủ số liệu. "
                    f"Điện tổng trừ tổng phụ tải: {_format_value(reconciliation['residual_kwh'])} kWh."
                )

    if aggregated.empty:
        st.info("Không có bản ghi trong khoảng ngày đã chọn.")
        return

    figure = _session_cached(
        store,
        ("dashboard_figure", data_key, level, args["start_date"], args["end_date"]),
        lambda: _build_dashboard_figure(aggregated, kwh_cols),
    )
    st.plotly_chart(figure, width="stretch")
    if st.button(
        "Hỏi AI về Dashboard này",
        key=f"ask_dashboard_{dashboard_response['run_id']}",
    ):
        _handoff_to_chat(
            store,
            "Dashboard và KPI đã lọc",
            [int(dashboard_response["run_id"])],
            "Giải thích xu hướng và KPI trong khoảng ngày đã chọn, chỉ dựa trên kết quả có nguồn.",
        )

    months = sorted(valid_timestamps.dt.strftime("%Y-%m").unique().tolist())
    if len(months) < 2:
        st.caption("Cần ít nhất hai tháng có dữ liệu để so sánh giai đoạn.")
        return

    st.subheader("So sánh giai đoạn")
    st.caption("So sánh số đo của hai tháng trong bộ dữ liệu. Chênh lệch được tính theo B − A.")
    month_a_column, month_b_column, compare_column = st.columns([1, 1, 1.2])
    with month_a_column:
        month_a = st.selectbox(
            "Tháng A",
            months,
            index=months.index(filters.get("month_a", months[-2])),
            key=f"compare_month_a_{dashboard_state_key}",
        )
    with month_b_column:
        month_b = st.selectbox(
            "Tháng B",
            months,
            index=months.index(filters.get("month_b", months[-1])),
            key=f"compare_month_b_{dashboard_state_key}",
        )
    with compare_column:
        st.write("")
        st.write("")
        compare_clicked = st.button(
            "So sánh hai tháng",
            type="secondary",
            key=f"compare_periods_{dashboard_state_key}",
            disabled=month_a == month_b,
            width="stretch",
        )
    compare_args = {"month_a": month_a, "month_b": month_b}
    filters.update(compare_args)
    if compare_clicked:
        store["compare_periods_response"] = run_tool("compare_periods", compare_args, store)
        store["compare_periods_args"] = compare_args
        store.setdefault("analysis_results", {})["compare_periods"] = store[
            "compare_periods_response"
        ]

    compare_response = store.get("compare_periods_response")
    if compare_response and store.get("compare_periods_args") == compare_args:
        comparison = compare_response.get("result", {})
        if "run_id" not in compare_response or "error" in comparison:
            st.error(
                compare_response.get("error")
                or comparison.get("error", "Không thể so sánh hai tháng.")
            )
        else:
            st.caption(f"run_id {compare_response['run_id']}")
            if "rows_month_a" in comparison and "rows_month_b" in comparison:
                st.caption(
                    f"Số dòng dữ liệu · {month_a} ({comparison['rows_month_a']:,}) · "
                    f"{month_b} ({comparison['rows_month_b']:,})"
                )
            comparison_coverage = comparison.get("coverage", {})
            coverage_a = comparison_coverage.get("month_a", {})
            coverage_b = comparison_coverage.get("month_b", {})
            if comparison_coverage:
                st.caption(
                    f"Ngày ghi nhận · {month_a} ({coverage_a['recorded_days']:,}/"
                    f"{coverage_a['calendar_days']:,}) · {month_b} "
                    f"({coverage_b['recorded_days']:,}/{coverage_b['calendar_days']:,})"
                )
            daily_differences = comparison.get("mean_daily_kwh_differences", {})
            difference_rows = [
                {
                    "Hạng mục": CATEGORY_LABELS.get(item, item),
                    "Ngày đo A": coverage_a.get("meter_days", {}).get(item),
                    "Ngày đo B": coverage_b.get("meter_days", {}).get(item),
                    "Chênh lệch tổng (kWh)": value,
                    "Chênh lệch bình quân (kWh/ngày đo)": daily_differences.get(item),
                }
                for item, value in comparison.get("kwh_differences", {}).items()
            ]
            st.dataframe(pd.DataFrame(difference_rows), hide_index=True, width="stretch")
            if note := comparison.get("comparison_note"):
                st.caption(note)
            st.write(
                f"Nhiệt độ trung bình: {_format_value(comparison.get('temperature_difference_c'))} °C · "
                f"Sản lượng: {_format_value(comparison.get('production_difference_t'))} tấn · "
                "Hạng mục có chênh lệch điện tăng lớn nhất "
                f"{CATEGORY_LABELS.get(comparison.get('main_driver'), comparison.get('main_driver') or '—')}"
            )
            if st.button("Hỏi AI về so sánh này", key=f"ask_compare_{compare_response['run_id']}"):
                _handoff_to_chat(
                    store,
                    f"So sánh {month_a} với {month_b}",
                    [int(compare_response["run_id"])],
                    f"So sánh mức tiêu thụ của tháng {month_b} với {month_a}, nêu độ phủ và giới hạn diễn giải.",
                )


def _show_seu_enpi(store: dict[str, Any]) -> None:
    """Hiển thị Pareto SEU và kết quả EnPI."""
    if not _show_mapping_guide(store):
        return

    df = store["df"]
    kwh_cols = [column for column in df.columns if str(column).startswith("kwh_")]
    if not kwh_cols:
        st.info("Mapping chưa có cột điện chuẩn bắt đầu bằng `kwh_`.")
        return

    default_threshold = float(store["config"].get("seu_threshold", 0.8))
    threshold = st.slider(
        "Ngưỡng SEU (GIẢ ĐỊNH - cần kiểm chứng)",
        min_value=0.0,
        max_value=1.0,
        value=float(store.get("seu_threshold_value", default_threshold)),
        step=0.05,
        key=f"seu_threshold_{store.get('decision_scope_id', 'dataset')}",
    )
    previous_threshold = store.get("seu_threshold_value")
    if previous_threshold is not None and float(previous_threshold) != float(threshold):
        store.pop("report_export", None)
    store["seu_threshold_value"] = float(threshold)
    data_key = id(df)
    seu_response = _session_cached(
        store,
        ("seu_analysis", data_key, tuple(kwh_cols), float(threshold)),
        lambda: run_tool("seu_analysis", {"threshold": float(threshold)}, store),
    )
    if "run_id" not in seu_response:
        st.error(seu_response.get("error", "Không thể tính Pareto SEU."))
        return
    pareto = seu_response.get("result")
    if isinstance(pareto, dict) and "error" in pareto:
        st.error(pareto["error"])
        return
    store.setdefault("analysis_results", {})["seu"] = seu_response
    if pareto.empty:
        st.info("Cần số đo theo phụ tải để phân tích Pareto SEU.")
    else:
        figure = _session_cached(
            store,
            ("seu_figure", data_key, tuple(kwh_cols), float(threshold)),
            lambda: _build_seu_figure(pareto, float(threshold)),
        )
        st.plotly_chart(figure, width="stretch")
        pareto_display = pareto.rename(
            columns={"kwh": "kWh", "share": "Tỷ trọng", "cum": "Tích lũy", "is_seu": "SEU"}
        ).copy()
        pareto_display.index = [CATEGORY_LABELS.get(item, item) for item in pareto.index]
        st.dataframe(pareto_display, width="stretch")
        st.caption(
            f"run_id {seu_response['run_id']} · Tỷ trọng = kWh hạng mục / tổng kWh; "
            "SEU gồm hạng mục khi tỷ trọng tích lũy trước nó còn dưới ngưỡng."
        )
        if st.button("Hỏi AI về SEU", key=f"ask_seu_{seu_response['run_id']}"):
            _handoff_to_chat(
                store,
                "Kết quả Pareto SEU",
                [int(seu_response["run_id"])],
                "Giải thích các hạng mục SEU, tỷ trọng và ngưỡng đã dùng.",
            )

    st.subheader("EnPI")
    driver_labels = {"production_t": "Sản lượng", "cdd": "CDD"}
    present_drivers = tuple(driver for driver in driver_labels if driver in df)
    available_drivers = tuple(driver for driver in present_drivers if df[driver].nunique() > 1) or present_drivers
    if not available_drivers:
        st.info("Ánh xạ sản lượng hoặc nhiệt độ để chọn biến hồi quy EnPI.")
        return
    driver_options = [available_drivers]
    if len(available_drivers) == 2:
        driver_options.extend((driver,) for driver in available_drivers)
    previous_selection = tuple(store.get("enpi_drivers", available_drivers))
    selection = st.selectbox(
        "Biến hồi quy EnPI",
        driver_options,
        index=driver_options.index(previous_selection) if previous_selection in driver_options else 0,
        format_func=lambda option: " + ".join(driver_labels[driver] for driver in option),
        key=f"enpi_drivers_{store.get('decision_scope_id', 'dataset')}",
        disabled=len(driver_options) == 1,
    )
    if selection != previous_selection:
        store.pop("report_export", None)
        store.get("analysis_results", {}).pop("enpi", None)
    store["enpi_drivers"] = list(selection)

    try:
        enpi_response = _session_cached(
            store,
            ("enpi_analysis", data_key, selection),
            lambda: run_tool(
                "enpi",
                {"total_col": "kwh_total", "drivers": list(selection)},
                store,
            ),
        )
    except (KeyError, ValueError) as exc:
        st.info(f"Chưa đủ dữ liệu để tính EnPI: {exc}")
        return
    if "run_id" not in enpi_response:
        st.error(enpi_response.get("error", "Không thể tính EnPI."))
        return
    enpi_result = enpi_response.get("result", {})
    if isinstance(enpi_result, dict) and "error" in enpi_result:
        st.info(f"Chưa đủ dữ liệu để tính EnPI: {enpi_result['error']}")
        return
    store.setdefault("analysis_results", {})["enpi"] = enpi_response

    st.caption(f"Nguồn EnPI · run_id {enpi_response['run_id']}")
    coefficients = enpi_result["coefficients"]
    metric_col, fit_col = st.columns(2)
    with metric_col:
        if enpi_result.get("kwh_per_ton") is None:
            st.info("Suất tiêu hao kWh/tấn cần điện năng và sản lượng ghép cặp với tổng sản lượng dương.")
        else:
            st.metric("kWh/tấn tổng thể", _format_value(enpi_result["kwh_per_ton"]))
            if enpi_result.get("intensity_rows") is not None:
                st.caption(f"Dòng ghép được điện năng với sản lượng · {enpi_result['intensity_rows']:,}")
    if coefficients:
        with fit_col:
            st.metric("R² trong mẫu", _format_value(enpi_result.get("r2"), 4))
            if enpi_result.get("regression_rows") is not None:
                st.caption(f"Dòng dùng để khớp mô hình · {enpi_result['regression_rows']:,}")
        with st.expander("Mô hình hồi quy và hệ số"):
            terms = [
                f"{coefficient} × {driver_labels[driver]}"
                for coefficient, driver in zip(("β₁", "β₂"), selection)
            ]
            st.caption("kWh = " + " + ".join(["β₀", *terms]))
            if "cdd" in selection:
                st.caption(
                    f"CDD = max(nhiệt độ − {store['config'].get('cdd_base_temp_c', 24)} °C, 0)."
                )
            st.dataframe(
                pd.DataFrame(
                    [{"Thành phần": name, "Hệ số": value} for name, value in coefficients.items()]
                ),
                hide_index=True,
                width="stretch",
            )
    if note := enpi_result.get("regression_note"):
        st.info(note)
    monthly = enpi_result["monthly_kwh_per_ton"]
    if not monthly.empty:
        st.caption("kWh/tấn theo tháng")
        st.dataframe(
            monthly.rename("kWh/tấn").rename_axis("Tháng").reset_index(),
            hide_index=True,
            width="stretch",
        )
    ask_run_ids = [int(seu_response["run_id"]), int(enpi_response["run_id"])]
    if st.button("Hỏi AI về kết quả SEU & EnPI", key=f"ask_seu_enpi_{data_key}"):
        _handoff_to_chat(
            store,
            "Kết quả SEU và EnPI",
            ask_run_ids,
            "Giải thích kết quả SEU và EnPI, nêu rõ công thức và giới hạn dữ liệu.",
        )


def _format_value(value: Any, decimals: int = 2) -> str:
    """Định dạng chỉ số hoặc hiện dấu gạch khi chưa có giá trị."""
    if value is None or pd.isna(value):
        return "—"
    return f"{float(value):,.{decimals}f}"


def _show_baseline_result(store: dict[str, Any], response: dict[str, Any]) -> None:
    """Hiển thị phần đối chiếu và chẩn đoán mô hình đã tính ở máy chủ."""
    result = response["result"]
    summary = result["summary"]
    model = result["model"]
    st.caption(f"Nguồn đường cơ sở · run_id {response['run_id']}")
    kwh_metrics = (
        ("Thực đo (kWh)", summary["actual_kwh"]),
        ("Cơ sở điều chỉnh (kWh)", summary["expected_kwh"]),
        ("Tiết kiệm ước tính (kWh)", summary["savings_kwh"]),
    )
    for column, (label, value) in zip(st.columns(3), kwh_metrics):
        column.metric(label, _format_value(value), border=True)
    if summary["savings_pct"] is not None:
        st.caption(
            f"Tỷ lệ tiết kiệm ước tính {_format_value(summary['savings_pct'])}% so với "
            "đường cơ sở điều chỉnh. Giá trị âm là mức sử dụng điện tăng."
        )
    for warning in result["warnings"]:
        st.warning(warning)
    for assumption in result["assumptions"]:
        st.caption(assumption)

    coverage = []
    for key, label in (("baseline", "Cơ sở"), ("reporting", "Báo cáo")):
        period = result[key]
        coverage.append(
            {
                "Kỳ": label,
                "Bắt đầu": period["start_date"],
                "Kết thúc": period["end_date"],
                "Ngày theo lịch": period["expected_rows"],
                "Ngày ghi nhận": period["observed_rows"],
                "Ngày dùng tính": period["eligible_rows"],
                "Độ phủ": f"{_format_value(period['coverage_pct'], 1)}%",
            }
        )
    st.dataframe(pd.DataFrame(coverage), hide_index=True, width="stretch")

    monthly = result["monthly"]
    if any(month["eligible_rows"] for month in monthly):
        figure = go.Figure()
        for key, label, color in (
            ("actual_kwh", "Thực đo", "#138676"),
            ("expected_kwh", "Cơ sở điều chỉnh", "#5590b3"),
        ):
            figure.add_bar(
                x=[month["month"] for month in monthly],
                y=[month[key] for month in monthly],
                name=label,
                marker_color=color,
                hovertemplate="%{x} · %{y:,.2f} kWh<extra>" + label + "</extra>",
            )
        figure.update_layout(
            barmode="group",
            height=285,
            margin=dict(l=10, r=10, t=15, b=10),
            paper_bgcolor="#ffffff",
            plot_bgcolor="#ffffff",
            yaxis_title="Điện năng (kWh)",
            legend=dict(orientation="h", y=1.12),
        )
        figure.update_xaxes(type="category", title="Tháng báo cáo")
        figure.update_yaxes(gridcolor="#e8efed")
        st.plotly_chart(figure, width="stretch")

    with st.container(border=True):
        st.markdown("**Chẩn đoán độ phù hợp mô hình**")
        indicators = (
            ("R²", model["r2"], ""),
            ("R² hiệu chỉnh", model["adjusted_r2"], ""),
            ("CV(RMSE)", model["cvrmse_pct"], "%"),
            ("NMBE", model["nmbe_pct"], "%"),
        )
        st.dataframe(
            pd.DataFrame(
                {
                    "Chỉ số": label,
                    "Giá trị": _format_value(value, 4 if not unit else 2) + (unit if value is not None else ""),
                }
                for label, value, unit in indicators
            ),
            hide_index=True,
            width="stretch",
        )
        st.caption(
            "Chỉ số độ phù hợp tính trong kỳ cơ sở. NMBE gần 0 với mô hình có hệ số chặn "
            "phản ánh độ lệch trung bình trong mẫu."
        )
        labels = {"intercept": "Hệ số chặn", **BASELINE_DRIVER_LABELS}
        ranges = model["driver_ranges"]
        st.dataframe(
            pd.DataFrame(
                {
                    "Thành phần": labels.get(name, name),
                    "Hệ số": value,
                    "Nhỏ nhất kỳ cơ sở": ranges.get(name, {}).get("min"),
                    "Lớn nhất kỳ cơ sở": ranges.get(name, {}).get("max"),
                }
                for name, value in model["coefficients"].items()
            ),
            hide_index=True,
            width="stretch",
        )

    if st.button("Hỏi AI về đường cơ sở", key=f"ask_baseline_{response['run_id']}"):
        _handoff_to_chat(
            store,
            "Đường cơ sở và kỳ báo cáo",
            [response["run_id"]],
            "Giải thích mức tiết kiệm ước tính, độ phù hợp mô hình và những ngày đủ dữ liệu.",
        )


def _show_energy_baseline(store: dict[str, Any]) -> None:
    """Chọn hai kỳ có thứ tự thời gian và đối chiếu mức điện chuẩn hóa."""
    if not store.get("mapping_confirmed"):
        return

    with st.expander("Đường cơ sở năng lượng (EnB) và tiết kiệm ước tính"):
        df = store.get("measured_df", store["df"])
        drivers = [
            driver for driver in BASELINE_DRIVER_LABELS
            if driver in df and df[driver].nunique() > 1
        ]
        if not drivers:
            st.info("Cần biến điều chỉnh có thay đổi theo ngày, chẳng hạn sản lượng, CDD hoặc giờ vận hành.")
            return
        first = df["timestamp"].min().date()
        last = df["timestamp"].max().date()
        if pd.isna(first) or first == last:
            st.info("Cần dữ liệu nhiều ngày để chọn kỳ cơ sở và kỳ báo cáo.")
            return

        st.caption(
            "Chọn kỳ đại diện trước thay đổi và kỳ báo cáo sau thay đổi. Phép tính dùng số đo "
            "theo ngày và đối chiếu điện thực đo với giá trị kỳ cơ sở tại điều kiện kỳ báo cáo."
        )
        scope = store["run_scope_id"]
        filters = store.setdefault("baseline_filters", {})
        kwh_cols = [column for column in df if column.startswith("kwh_")]
        mapped_meters = submeter_columns(kwh_cols)
        uses_total_meter = total_meter_columns(df, kwh_cols) == [TOTAL_METER]
        meter_labels = {
            "kwh_total": "Công tơ tổng" if uses_total_meter else "Tổng các phụ tải đã ánh xạ",
            **{meter: CATEGORY_LABELS.get(meter, meter) for meter in mapped_meters},
        }
        boundary_column, driver_column = st.columns(2)
        with boundary_column:
            energy_col = st.selectbox(
                "Phạm vi điện năng",
                tuple(meter_labels),
                index=list(meter_labels).index(filters.get("energy_col", "kwh_total")),
                format_func=meter_labels.__getitem__,
                key=f"baseline_energy_col_{scope}",
            )
        preferred = [driver for driver in ("production_t", "cdd") if driver in drivers]
        with driver_column:
            selection = st.multiselect(
                "Biến điều chỉnh đường cơ sở",
                drivers,
                default=filters.get("drivers", preferred or drivers[:1]),
                format_func=BASELINE_DRIVER_LABELS.__getitem__,
                key=f"baseline_drivers_{scope}",
            )
        if energy_col == "kwh_total":
            if uses_total_meter:
                st.caption("Đường cơ sở dùng số đo công tơ tổng tại các ngày đủ biến đã chọn.")
            else:
                st.caption("Mỗi ngày dùng tính tổng cần đủ số đo các phụ tải đã ánh xạ.")

        reporting_days = max(1, (last - first).days // 3)
        report_start = last - timedelta(days=reporting_days - 1)
        baseline_end = report_start - timedelta(days=1)
        baseline_col, reporting_col = st.columns(2)
        with baseline_col:
            baseline = st.date_input(
                "Kỳ cơ sở",
                value=filters.get("baseline", (first, baseline_end)),
                min_value=first,
                max_value=last,
                key=f"baseline_dates_{scope}",
            )
        with reporting_col:
            reporting = st.date_input(
                "Kỳ báo cáo",
                value=filters.get("reporting", (report_start, last)),
                min_value=first,
                max_value=last,
                key=f"reporting_dates_{scope}",
            )

        filters.update(energy_col=energy_col, drivers=selection)
        complete_ranges = len(baseline) == len(reporting) == 2
        args = None
        if complete_ranges:
            filters.update(baseline=baseline, reporting=reporting)
            args = {
                "baseline_start": baseline[0].isoformat(),
                "baseline_end": baseline[1].isoformat(),
                "reporting_start": reporting[0].isoformat(),
                "reporting_end": reporting[1].isoformat(),
                "drivers": list(selection),
                "energy_col": energy_col,
            }
        analysis_results = store.setdefault("analysis_results", {})
        if store.get("baseline_args") != args:
            if analysis_results.pop("energy_baseline", None) is not None:
                store.pop("report_export", None)
            store.pop("baseline_args", None)
        if args is None:
            st.info("Chọn ngày bắt đầu và kết thúc cho cả hai kỳ.")
            return
        if baseline[1] >= reporting[0]:
            st.warning("Kỳ cơ sở phải kết thúc trước ngày bắt đầu kỳ báo cáo.")
            return

        if not selection:
            st.info("Chọn ít nhất một biến điều chỉnh đường cơ sở.")
            return

        if st.button("Tính đường cơ sở", key=f"calculate_baseline_{scope}", type="primary"):
            cache_key = ("energy_baseline", energy_col, *selection, *baseline, *reporting)
            with st.spinner("Đang điều chỉnh đường cơ sở…"):
                response = _session_cached(
                    store, cache_key, lambda: run_tool("energy_baseline", args, store)
                )
            if error := response.get("error") or response.get("result", {}).get("error"):
                st.error(error)
                return
            analysis_results["energy_baseline"] = response
            store["baseline_args"] = args
            store.pop("report_export", None)

        if response := analysis_results.get("energy_baseline"):
            _show_baseline_result(store, response)


def _show_opportunities(store: dict[str, Any]) -> None:
    """Hiển thị cơ hội, nguồn tính và lưu lựa chọn người dùng."""
    if not _show_mapping_guide(store):
        return

    response = store.get("opportunities_tool_response")
    if response is None:
        response = run_tool("list_opportunities", {}, store)
        store["opportunities_tool_response"] = response
    if "run_id" not in response:
        st.error(response.get("error", "Không thể tính danh sách cơ hội."))
        return
    if isinstance(response.get("result"), dict) and "error" in response["result"]:
        st.error(response["result"]["error"])
        return

    opportunities = response.get("result", [])
    store.setdefault("analysis_results", {})["opportunities"] = response
    st.caption(
        f"Cơ hội từ config.yaml · nguồn run_id {response['run_id']} · "
        "tỷ lệ tiết kiệm, giá điện và vốn đầu tư là giả định."
    )
    if not opportunities:
        st.info("Không có cơ hội nào trong config.yaml.")
        return

    decision_scope = store.get("decision_scope_id") or _decision_scope_id(store)
    decisions = db.latest_decisions(decision_scope)
    status_labels = {"Approve": "Đã duyệt", "Reject": "Đã từ chối"}
    st.dataframe(
        pd.DataFrame(
            [
                {
                    "Cơ hội": item["name"],
                    "kWh/năm": item.get("saved_kwh"),
                    "VNĐ/năm": item.get("saved_vnd"),
                    "tCO₂/năm": item.get("co2_tonnes"),
                    "ROI (%)": item.get("roi_pct"),
                    "Hoàn vốn (tháng)": item.get("payback_months"),
                    "Quyết định": status_labels.get(decisions.get(str(item["name"])), "Chờ duyệt"),
                }
                for item in opportunities
            ]
        ),
        column_config={
            "kWh/năm": st.column_config.NumberColumn(format="%.2f"),
            "VNĐ/năm": st.column_config.NumberColumn(format="%.0f"),
            "tCO₂/năm": st.column_config.NumberColumn(format="%.2f"),
            "ROI (%)": st.column_config.NumberColumn(format="%.2f"),
            "Hoàn vốn (tháng)": st.column_config.NumberColumn(format="%.2f"),
        },
        hide_index=True,
        width="stretch",
    )
    if store["config"].get("emission_factor_kg_per_kwh") is None:
        st.caption("Cột tCO₂ để trống vì chưa cấu hình hệ số phát thải.")

    for index, item in enumerate(opportunities):
        name = str(item["name"])
        status = decisions.get(name)
        with st.expander(f"{name} · {status_labels.get(status, 'Chờ duyệt')}"):
            applies_to = str(item.get("applies_to", ""))
            st.caption(
                f"Căn cứ {CATEGORY_LABELS.get(applies_to, applies_to)} · "
                f"{_format_value(item.get('base_kwh_annual'))} kWh/năm"
            )
            st.write(item.get("assumption", "Giả định cần kiểm chứng."))
            approve_column, reject_column, ask_column = st.columns(3)
            if approve_column.button(
                "Duyệt",
                key=f"approve_opportunity_{index}",
                disabled=status == "Approve" or item.get("saved_kwh") is None,
            ):
                db.save_decision(name, "Approve", decision_scope)
                store.pop("report_export", None)
                st.rerun()
            if reject_column.button(
                "Từ chối", key=f"reject_opportunity_{index}", disabled=status == "Reject"
            ):
                db.save_decision(name, "Reject", decision_scope)
                store.pop("report_export", None)
                st.rerun()
            if ask_column.button("Hỏi AI về cơ hội", key=f"ask_opportunity_{index}"):
                _handoff_to_chat(
                    store,
                    f"Cơ hội: {name}",
                    [int(response["run_id"])],
                    f"Giải thích cơ hội '{name}', bằng chứng từ mức tiêu thụ và các giả định cần kiểm chứng.",
                )


def _show_run_sources(run_ids: list[int], scope_id: str) -> None:
    """Hiển thị tham số và kết quả đã lưu cho các run_id."""
    with st.expander("Nguồn tính toán"):
        if not run_ids:
            st.caption("Câu trả lời này chưa gọi công cụ tính toán.")
            return
        runs = db.get_runs(run_ids, scope_id=scope_id)
        for run_id in run_ids:
            run = runs.get(int(run_id))
            if run is None:
                st.warning(f"Phiên hiện tại không có nguồn cho run_id {run_id}.")
                continue
            st.markdown(f"**run_id {run_id} · {run['tool']}**")
            result = run["result"]
            if run["tool"] == "dashboard_data" and isinstance(result, dict):
                result = result.get("summary", result)
            st.json({"Tham số": run["args"], "Kết quả": result}, expanded=False)


def _show_answer_evidence(message: dict[str, Any], scope_id: str) -> None:
    """Hiện kết quả đối chiếu số liệu và nguồn trong phạm vi phiên."""
    verification = message.get("verification")
    if isinstance(verification, dict) and verification.get("verified") is False:
        st.warning(
            "Một số số liệu hoặc run_id trong câu trả lời chưa khớp khi đối chiếu tự động. "
            "Mở nguồn tính toán để kiểm tra."
        )
    _show_run_sources(message.get("run_ids", []), scope_id)


@st.fragment
def _show_chat(store: dict[str, Any]) -> None:
    """Hiển thị hội thoại, câu hỏi gợi ý và nguồn tính toán."""
    chat_history: list[dict[str, Any]] = store.setdefault("chat_history", [])
    api_ready = bool(os.getenv("GEMINI_API_KEY"))
    data_ready = bool(store.get("mapping_confirmed"))
    scope_id = store.get("run_scope_id", "")

    with st.container(key="ve_assistant_panel", border=True):
        status_class = "" if api_ready else " offline"
        status_label = "Đã đặt API key" if api_ready else "Cần cấu hình Gemini"
        st.html(
            f'<div class="ve-chat-title"><strong>Hội thoại</strong>'
            f'<span class="ve-status{status_class}">{status_label}</span></div>'
        )
        if not api_ready:
            st.info("Điền GEMINI_API_KEY trong tệp .env rồi khởi chạy lại ứng dụng.")
        elif not data_ready:
            st.info("Có thể hỏi kiến thức chung. Hãy xác nhận dữ liệu để hỏi về số liệu nhà máy.")
        if store.get("pending_question") and not api_ready:
            st.caption(f"Câu hỏi đang chờ cấu hình API: {store['pending_question']}")

        selected_question: str | None = None
        if api_ready and not chat_history:
            questions = (
                (
                    ("Chất lượng dữ liệu", "Tóm tắt chất lượng và độ phủ số đo năng lượng."),
                    ("Hạng mục SEU", "Các hạng mục nào đóng góp nhiều nhất vào tiêu thụ điện?"),
                    ("Thời gian hoàn vốn", "Cơ hội nào có thời gian hoàn vốn dự kiến ngắn nhất?"),
                )
                if data_ready else (
                    ("EnPI là gì?", "EnPI là gì và đọc chỉ số kWh/tấn như thế nào?"),
                    ("Đường cơ sở là gì?", "Đường cơ sở năng lượng dùng để làm gì?"),
                )
            )
            for index, (column, (label, question_text)) in enumerate(zip(st.columns(len(questions)), questions)):
                if column.button(
                    label,
                    key=f"sample_question_{index}",
                    width="stretch",
                ):
                    selected_question = question_text

        for message in chat_history:
            with st.chat_message(message["role"]):
                st.markdown(message["content"])
                if message["role"] == "assistant":
                    _show_answer_evidence(message, scope_id)

        typed_question = st.chat_input(
            "Nhập câu hỏi về năng lượng", disabled=not api_ready
        )
        pending_question = store.get("pending_question")
        question = pending_question or selected_question or typed_question
        if question and api_ready:
            history = [
                {"role": message["role"], "content": message["content"]}
                for message in chat_history
            ]
            with st.spinner("Đang phân tích dữ liệu..."):
                response = ask(question, store, history)
            store.pop("pending_question", None)
            store.pop("selected_context", None)
            chat_history.append({"role": "user", "content": question})
            assistant_message = {
                "role": "assistant",
                "content": response["answer"],
                "run_ids": response.get("run_ids", []),
                "verification": response.get("verification"),
            }
            chat_history.append(assistant_message)
            with st.chat_message("user"):
                st.markdown(question)
            with st.chat_message("assistant"):
                st.markdown(response["answer"])
                _show_answer_evidence(assistant_message, scope_id)


def _register_source(
    filename: str, content: bytes, store: dict[str, Any], *, upload_id: str | None = None
) -> None:
    """Lưu tệp mới một lần và lấy tên các bảng Excel."""
    if upload_id is not None and upload_id == store.get("upload_file_id"):
        return

    fingerprint = hashlib.sha256(content).hexdigest()
    suffix = Path(filename).suffix.lower()
    same_source = (
        fingerprint == store.get("upload_fingerprint")
        and suffix == Path(store.get("filename", "")).suffix.lower()
    )
    if not same_source:
        _invalidate_dataset_state(store)
    store.update(
        filename=filename,
        upload_size_bytes=len(content),
        upload_fingerprint=fingerprint,
        upload_file_id=upload_id,
    )
    if same_source:
        return

    if suffix == ".xlsx":
        try:
            with pd.ExcelFile(BytesIO(content)) as workbook:
                store["upload_sheet_names"] = workbook.sheet_names
            store["workbook_content"] = content
        except Exception as exc:
            store["upload_error"] = str(exc)
    else:
        _load_upload_draft(store, content)


def _load_upload_draft(store: dict[str, Any], content: bytes) -> None:
    """Đọc bảng đang chọn và tạo bản nháp ánh xạ cục bộ."""
    try:
        raw_df = _read_upload_bytes(store["filename"], content, store.get("upload_sheet", 0))
    except Exception as exc:
        store["upload_error"] = str(exc)
        return
    suggested = suggest_mapping(raw_df, use_ai=False)
    store.update(raw_df=raw_df, mapping_seed=suggested, mapping=suggested.copy())


def _show_data_page(store: dict[str, Any]) -> None:
    """Hiển thị bước tải file, ánh xạ cột và kiểm tra dữ liệu."""
    _page_heading(
        "Nạp và ánh xạ dữ liệu",
        "Nhập số liệu tiêu thụ theo kỳ đo, xác nhận đơn vị và kiểm tra chất lượng trước khi phân tích.",
        entry=True,
    )

    with st.container(border=True):
        if store.get("upload_fingerprint"):
            upload_column, sample_column = st.container(), None
        else:
            upload_column, sample_column = st.columns([1.6, 1], gap="medium")
        upload_widget_revision = int(st.session_state.setdefault("upload_widget_revision", 0))
        upload_key = f"energy_data_upload_{upload_widget_revision}"
        uploaded_file = upload_column.file_uploader(
            "Tệp số liệu CSV hoặc XLSX",
            type=["csv", "xlsx"],
            key=upload_key,
            on_change=_on_upload_change,
            args=(upload_key,),
            help="CSV UTF-8/UTF-16 hoặc Excel XLSX.",
        )
        if uploaded_file is not None:
            _register_source(
                uploaded_file.name, uploaded_file.getvalue(), store,
                upload_id=getattr(uploaded_file, "file_id", None),
            )
        if sample_column is not None and not store.get("upload_fingerprint"):
            with sample_column:
                st.markdown("**Dữ liệu mẫu**")
                sample_name = st.selectbox("Loại dữ liệu mẫu", tuple(SAMPLE_FILES), key="sample_variant")
                st.caption("Số liệu giả lập theo ngày về vận hành, điện, nhiệt, chi phí và phát thải.")
                if st.button("Dùng dữ liệu mẫu", key="load_sample_dataset", icon=":material/science:"):
                    path = Path(__file__).with_name("data") / SAMPLE_FILES[sample_name]
                    try:
                        _register_source(path.name, path.read_bytes(), store)
                    except OSError as exc:
                        st.error(f"Không đọc được dữ liệu mẫu: {exc}")
                    else:
                        st.rerun()

        if sheet_names := store.get("upload_sheet_names"):
            current_sheet = store.get("upload_sheet")
            selected_sheet = st.selectbox(
                "Trang tính Excel",
                options=sheet_names,
                index=sheet_names.index(current_sheet) if current_sheet in sheet_names else 0,
                key=f"upload_sheet_{store['upload_fingerprint']}",
            )
            if selected_sheet != current_sheet:
                _invalidate_sheet_state(store)
                store["upload_sheet"] = selected_sheet
                _load_upload_draft(store, store["workbook_content"])

        if store.get("upload_fingerprint"):
            st.button(
                "Xóa bộ dữ liệu",
                key="clear_energy_dataset",
                on_click=_clear_dataset_state,
            )
        if upload_error := store.get("upload_error"):
            st.error(f"Không đọc được file: {upload_error}")

    raw_df = store.get("raw_df")
    has_data = isinstance(raw_df, pd.DataFrame)
    current_step = 3 if store.get("mapping_confirmed") else 2 if has_data else 1
    _render_data_steps(current_step)
    if not has_data:
        return

    file_size_kib = float(store.get("upload_size_bytes", 0)) / 1024
    sheet_label = f" · Trang tính {store['upload_sheet']}" if store.get("upload_sheet") else ""
    st.caption(
        f"Tệp {store.get('filename', 'đã tải lên')}{sheet_label} · {file_size_kib:,.1f} KiB · "
        f"{len(raw_df):,} dòng · {len(raw_df.columns):,} cột · "
        f"{int(raw_df.isna().sum().sum()):,} ô trống"
    )

    mapping_column, guide_column = st.columns([1.55, 1], gap="medium")
    with mapping_column:
        st.markdown("**Ánh xạ các cột dữ liệu**")
        if st.button(
            "Đề xuất ánh xạ bằng Gemini",
            key=f"resuggest_{store['upload_fingerprint']}",
            disabled=not os.getenv("GEMINI_API_KEY"),
            help="Chỉ gửi tên cột cho Gemini. Gợi ý ban đầu dùng từ khóa trên máy này.",
        ):
            with st.spinner("Đang đề xuất ánh xạ..."):
                store["mapping"] = suggest_mapping(raw_df)
            store["mapping_seed"] = store["mapping"].copy()
            store["mapping_revision"] = int(store.get("mapping_revision", 0)) + 1
            st.info("Đã cập nhật đề xuất. Kiểm tra tên cột và đơn vị trước khi xác nhận.")
        editor_key = (
            f"mapping_editor_{store['upload_fingerprint']}_"
            f"{store.get('mapping_revision', 0)}"
        )
        # Streamlit xóa widget khi rời trang. Khởi tạo lại từ bản nháp đã lưu.
        if editor_key not in st.session_state:
            store["mapping_seed"] = store.get("mapping", {}).copy()
        suggested_mapping = store.get("mapping_seed", store.get("mapping", {}))
        editor_df = pd.DataFrame(
            {
                "Cột nguồn": list(raw_df.columns),
                "Tên cột chuẩn": [
                    suggested_mapping.get(str(column), "unmapped")
                    for column in raw_df.columns
                ],
            }
        )
        edited_df = st.data_editor(
            editor_df,
            column_config={
                "Tên cột chuẩn": st.column_config.SelectboxColumn(
                    "Tên cột chuẩn",
                    options=[*CANONICAL_COLUMNS, "unmapped"],
                    required=True,
                )
            },
            disabled=["Cột nguồn"],
            hide_index=True,
            width="stretch",
            key=editor_key,
        )
        current_mapping = {
            str(row["Cột nguồn"]): str(row["Tên cột chuẩn"])
            for _, row in edited_df.iterrows()
        }

        unit_warnings = _session_cached(
            store,
            ("unit_warnings", id(raw_df), tuple(sorted(current_mapping.items()))),
            lambda: detect_unit_issues(raw_df, current_mapping),
        )
        unit_factors: dict[str, float] = {}
        saved_factors = store.get("draft_unit_factors", {})
        for warning in unit_warnings:
            st.warning(f"{warning['column']}: {warning['warning']}")
            unit_key = (
                f"unit_{store['upload_fingerprint']}_{store.get('upload_sheet')}_"
                f"{warning['column']}"
            )
            confirmed_mwh = st.checkbox(
                f"Xác nhận {warning['column']} là MWh và đổi sang kWh (×1.000)",
                key=unit_key,
                value=unit_key not in st.session_state and warning["column"] in saved_factors,
            )
            if confirmed_mwh:
                unit_factors[warning["column"]] = 1000.0

        mapping_changed = (
            store.get("mapping_confirmed")
            and (
                current_mapping != store.get("confirmed_mapping")
                or unit_factors != store.get("confirmed_unit_factors")
            )
        )
        if mapping_changed:
            _invalidate_analysis_state(store)
            st.info("Ánh xạ hoặc đơn vị đã đổi. Xác nhận lại để cập nhật phân tích.")
        store["mapping"] = current_mapping
        store["draft_unit_factors"] = unit_factors

        if st.button(
            "Xác nhận mapping",
            type="primary",
            key=f"confirm_{store['upload_fingerprint']}",
            width="stretch",
            disabled=bool(store.get("mapping_confirmed")),
        ):
            try:
                mapped_df = apply_mapping(raw_df, current_mapping, unit_factors)
                validation_errors = validate_mapped_data(mapped_df)
                if validation_errors:
                    raise ValueError(" ".join(validation_errors))
                quality_store = {
                    "df": mapped_df,
                    "config": store["config"],
                    "run_scope_id": store["run_scope_id"],
                }
                quality_response = run_tool("data_quality", {}, quality_store)
                if "run_id" not in quality_response:
                    raise ValueError(quality_response.get("error", "Không thể làm sạch dữ liệu."))
                if isinstance(quality_response.get("result"), dict) and "error" in quality_response["result"]:
                    raise ValueError(quality_response["result"]["error"])
                cleaned_df = quality_store["clean_df"]
                validation_errors = validate_mapped_data(cleaned_df)
                if validation_errors:
                    raise ValueError(" ".join(validation_errors))
                store.update(
                    df=cleaned_df,
                    clean_df=cleaned_df,
                    measured_df=mapped_df,
                    quality_report=quality_response["result"],
                    quality_run_id=quality_response["run_id"],
                    confirmed_mapping=current_mapping.copy(),
                    confirmed_unit_factors=unit_factors.copy(),
                    mapping_confirmed=True,
                    analysis_results={"quality": quality_response},
                )
                store["decision_scope_id"] = _decision_scope_id(store)
                st.success("Đã xác nhận mapping và làm sạch dữ liệu.")
            except (ValueError, KeyError) as exc:
                st.error(f"Không thể áp dụng mapping: {exc}")

        if store.get("mapping_confirmed") and store.get("quality_report"):
            st.markdown("**Báo cáo chất lượng dữ liệu**")
            report_df = pd.DataFrame.from_dict(store["quality_report"], orient="index")
            report_df.index.name = "Cột điện"
            report_df = report_df.rename(
                columns={
                    "removed": "Giá trị bị loại",
                    "interpolated": "Giá trị nội suy",
                    "missing": "Còn thiếu",
                }
            )
            st.caption(f"Nguồn kiểm tra chất lượng · run_id {store['quality_run_id']}")
            st.dataframe(report_df, width="stretch")
            _show_interval_quality(store["clean_df"].attrs.get("measurement_interval_quality", {}))
            if st.button("Mở Dashboard", key=f"open_dashboard_{store['upload_fingerprint']}"):
                st.session_state["active_page"] = "Dashboard"
                st.rerun()
        elif store.get("mapping_confirmed"):
            st.info("Không có cột điện cần kiểm tra chất lượng.")

        if store.get("mapping_confirmed") and isinstance(store.get("df"), pd.DataFrame):
            mapping_issues = store.get("measured_df", store["df"]).attrs.get("mapping_issues", {})
            if mapping_issues:
                st.warning("Có giá trị cần kiểm tra sau ánh xạ. Xem số lượng theo cột ở bảng bên dưới.")
                st.dataframe(
                    pd.DataFrame(mapping_issues.items(), columns=["Cột chuẩn", "Số giá trị"]),
                    hide_index=True,
                    width="stretch",
                )
            with st.expander("Xem trước dữ liệu sau chuẩn hóa"):
                normalized_df = store["df"]
                st.caption(
                    f"{len(normalized_df):,} dòng · {len(normalized_df.columns):,} cột chuẩn · "
                    f"{int(normalized_df.isna().sum().sum()):,} ô trống"
                )
                st.dataframe(normalized_df.head(10), hide_index=True, width="stretch")

    with guide_column:
        with st.container(border=True):
            st.markdown(
                """
                <div class="ve-hint-card">
                  <strong>⚡ Gợi ý dữ liệu</strong>
                  <p>✓ Thời gian (datetime)</p>
                  <p>✓ Điện năng (kWh)</p>
                  <p>✓ Sản lượng (nếu có)</p>
                  <p>✓ Nhiệt độ hoặc biến liên quan</p>
                </div>
                """,
                unsafe_allow_html=True,
            )
            st.caption("Dữ liệu xem trước")
            st.dataframe(raw_df.head(6), hide_index=True, width="stretch")
            st.info("Kiểm tra đơn vị và mapping trước khi xác nhận để cập nhật phân tích.")


def _show_report_export(store: dict[str, Any]) -> None:
    """Tạo và tải cùng một hồ sơ kiểm toán ở các định dạng hỗ trợ."""
    if not _show_mapping_guide(store):
        return

    with st.container(border=True):
        st.subheader("Hồ sơ kiểm toán")
        st.caption(
            "Báo cáo dùng dữ liệu đã xác nhận, kết quả phân tích và các quyết định của phiên."
        )
        baseline = store.get("analysis_results", {}).get("energy_baseline")
        if baseline and "error" not in baseline.get("result", {}):
            st.caption(f"Có kèm đường cơ sở năng lượng · run_id {baseline['run_id']}")

        if st.button("Xuất báo cáo", type="primary", key="export_energy_report"):
            try:
                with st.spinner("Đang tạo báo cáo..."):
                    report_data = build_report_data(store)
                    interpretation = generate_interpretation(report_data)
                    store["report_export"] = {
                        "markdown": render_markdown(report_data, interpretation),
                        "docx": render_docx(report_data, interpretation),
                        "json": render_report_json(report_data),
                        "run_ids": report_data["run_ids"],
                    }
            except (ValueError, KeyError) as exc:
                st.error(f"Không thể tạo báo cáo: {exc}")

        if report_export := store.get("report_export"):
            st.success("Hồ sơ đã sẵn sàng để tải xuống.")
            st.caption(f"Nguồn tính toán · {', '.join(map(str, report_export['run_ids']))}")
            formats = (
                ("docx", "Tải Word", "bao_cao_kiem_toan_nang_luong.docx",
                 "application/vnd.openxmlformats-officedocument.wordprocessingml.document"),
                ("markdown", "Tải Markdown", "bao_cao_kiem_toan_nang_luong.md", "text/markdown"),
                ("json", "Tải hồ sơ JSON", "ho_so_kiem_toan_nang_luong.json", "application/json"),
            )
            for column, (name, label, filename, mime) in zip(st.columns(3), formats):
                column.download_button(
                    label,
                    data=report_export[name],
                    file_name=filename,
                    mime=mime,
                    width="stretch",
                    on_click="ignore",
                    key=f"download_energy_report_{name}",
                )


def _render_data_steps(current_step: int) -> None:
    """Đánh dấu bước đang thực hiện trong quy trình chuẩn bị dữ liệu."""
    steps = []
    for index, name in enumerate(("Nạp dữ liệu", "Ánh xạ cột", "Xác nhận và kiểm tra"), start=1):
        state = "active" if index == current_step else "done" if index < current_step else ""
        current = ' aria-current="step"' if index == current_step else ""
        steps.append(
            f'<span class="ve-step {state}"{current}>'
            f'<b class="ve-step-number">{index}</b>{name}</span>'
        )
    st.html('<div class="ve-stepper">' + "".join(steps) + "</div>")


def main() -> None:
    """Khởi chạy các trang trong không gian kiểm toán năng lượng."""
    st.set_page_config(
        page_title="Virtual Energy Engineer",
        page_icon=":material/electric_bolt:",
        layout="wide",
        initial_sidebar_state="auto",
    )
    _apply_styles()

    if "store" not in st.session_state:
        st.session_state["store"] = {"config": _load_config()}
    store: dict[str, Any] = st.session_state["store"]
    if "config" not in store:
        store["config"] = _load_config()
    if "session_scope_id" not in store:
        store["session_scope_id"] = uuid.uuid4().hex
    if "run_scope_id" not in store:
        store["run_scope_id"] = uuid.uuid4().hex

    current_page = _render_navigation()
    with st.container(key="ve_main_layout"):
        if current_page == "Dữ liệu":
            _show_data_page(store)
        elif current_page == "Dashboard":
            _page_heading("Dashboard năng lượng", "Theo dõi điện năng, nhiệt độ và sản lượng trong kỳ đã chọn.")
            show_hourly_weather(store["config"].get("weather"))
            _show_dashboard(store)
        elif current_page == "SEU & EnPI":
            _page_heading("Hiệu suất năng lượng", "Xác định hạng mục tiêu thụ đáng kể, theo dõi EnPI và lập đường cơ sở.")
            _show_energy_baseline(store)
            _show_seu_enpi(store)
        elif current_page == "Cơ hội":
            _page_heading("Cơ hội tiết kiệm", "Đối chiếu giả định, so sánh mức tiết kiệm và ghi nhận quyết định.")
            _show_opportunities(store)
            if store.get("mapping_confirmed"):
                with st.expander("Xuất hồ sơ kiểm toán"):
                    _show_report_export(store)
        elif current_page == "Báo cáo":
            _page_heading("Báo cáo năng lượng", "Xuất hồ sơ Word, Markdown hoặc JSON kèm nguồn tính toán.")
            _show_report_export(store)
        elif current_page == "Hỏi đáp":
            _page_heading("Trợ lý năng lượng", "Hỏi về kết quả phân tích và mở nguồn tính toán ngay trong câu trả lời.")
            _show_chat(store)
    _render_dataset_status(store)


if __name__ == "__main__":
    main()
