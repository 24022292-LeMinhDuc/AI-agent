"""Tổng hợp kết quả công cụ và xuất báo cáo kiểm toán."""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from html import escape
from io import BytesIO
from typing import Any

import pandas as pd
from docx import Document
from docx.oxml import OxmlElement
from google.genai import types

import db
from agent import run_tool
from energy_baseline import BASELINE_DRIVER_LABELS
from gemini_service import create_client, generate_content

REPORT_TITLE = "Báo cáo kiểm toán năng lượng"
FIXED_INTERPRETATION = (
    "Báo cáo tổng hợp các kết quả tính toán tất định từ dữ liệu đã xác nhận. "
    "Các hạng mục SEU được xếp theo mức tiêu thụ. Kế hoạch hành động ghi nhận các cơ hội đã được duyệt. "
    "Các ước tính cần được xem cùng giả định và giới hạn dữ liệu."
)


def _report_tool_run(
    name: str,
    args: dict[str, Any],
    store: dict[str, Any],
    previous_run: dict[str, Any] | None,
) -> dict[str, Any]:
    """Dùng kết quả đã ghi vết nếu công cụ và tham số khớp."""
    if previous_run and previous_run["tool"] == name and previous_run["args"] == args:
        result = previous_run["result"]
        if not isinstance(result, dict) or "error" not in result:
            return previous_run

    response = run_tool(name, args, store)
    if "run_id" not in response:
        raise ValueError(response.get("error", f"Không chạy được công cụ {name}."))
    run = db.get_run(response["run_id"], scope_id=store.get("run_scope_id", ""))
    if run is None:
        raise ValueError(f"Không đọc được nguồn của lần chạy {response['run_id']}.")
    return run


def build_report_data(store: dict[str, Any]) -> dict[str, Any]:
    """Gom dữ liệu chất lượng, SEU, EnPI và cơ hội từ các lần chạy công cụ."""
    if not store.get("mapping_confirmed"):
        raise ValueError("Cần xác nhận mapping trước khi tạo báo cáo.")
    df = store.get("clean_df", store.get("df"))
    if not isinstance(df, pd.DataFrame):
        raise TypeError("Không tìm thấy dữ liệu đã ánh xạ trong store.")

    config = store.get("config", {})
    threshold = float(store.get("seu_threshold_value", config.get("seu_threshold", 0.8)))
    default_drivers = ["production_t", "cdd"]
    present_drivers = [column for column in default_drivers if column in df]
    available_drivers = [column for column in present_drivers if df[column].nunique() > 1] or present_drivers
    drivers = store.get("enpi_drivers") or available_drivers or default_drivers
    analysis_results = store.setdefault("analysis_results", {})
    specs = {
        "quality": ("data_quality", {}),
        "scope": ("dataset_summary", {}),
        "seu": ("seu_analysis", {"threshold": threshold}),
        "enpi": ("enpi", {"total_col": "kwh_total", "drivers": drivers}),
        "opportunities": ("list_opportunities", {}),
    }
    baseline_args = store.get("baseline_args")
    if baseline_args and analysis_results.get("energy_baseline"):
        specs["energy_baseline"] = ("energy_baseline", baseline_args)
    for optional_tool in ("energy_balance", "tariff_power_quality"):
        if cached := analysis_results.get(optional_tool):
            cached_args = cached.get("args") if isinstance(cached, dict) else None
            specs[optional_tool] = (
                optional_tool,
                cached_args if isinstance(cached_args, dict) else {},
            )
    previous_ids = {key: (analysis_results.get(key) or {}).get("run_id") for key in specs}
    previous_ids["quality"] = previous_ids["quality"] or store.get("quality_run_id")
    previous_ids["opportunities"] = previous_ids["opportunities"] or (
        store.get("opportunities_tool_response") or {}
    ).get("run_id")
    previous_runs = db.get_runs(
        (run_id for run_id in previous_ids.values() if run_id is not None),
        scope_id=store.get("run_scope_id", ""),
    )
    runs: dict[str, dict[str, Any]] = {}
    for key, (tool, args) in specs.items():
        run = _report_tool_run(tool, args, store, previous_runs.get(previous_ids[key]))
        runs[key] = run
        if key in ("quality", "scope") and "error" in run["result"]:
            raise ValueError(run["result"]["error"])
        # Kết quả trong bộ nhớ có thể giữ kiểu pandas cho giao diện.
        if previous_ids[key] != run["run_id"]:
            analysis_results[key] = {"run_id": run["run_id"], "result": run["result"]}

    quality_run = runs["quality"]
    store["quality_report"] = quality_run["result"]
    store["quality_run_id"] = quality_run["run_id"]
    if isinstance(store.get("clean_df"), pd.DataFrame):
        store["df"] = store["clean_df"]
    scope = runs["scope"]["result"]
    seu_result = runs["seu"]["result"]
    seu_items = [
        {
            "item": item,
            **{column: seu_result[column][item] for column in ("kwh", "share", "cum", "is_seu")},
        }
        for item in seu_result.get("kwh", {})
    ]
    enpi_result = runs["enpi"]["result"]
    opportunity_result = runs["opportunities"]["result"]
    opportunities = opportunity_result if isinstance(opportunity_result, list) else []
    decisions = db.latest_decisions(store.get("decision_scope_id", ""))

    approved: list[dict[str, Any]] = []
    waiting: list[dict[str, str]] = []
    rejected: list[dict[str, str]] = []
    for item in opportunities:
        name = str(item.get("name", ""))
        status = decisions.get(name, "")
        if status.casefold() == "approve":
            approved.append({**item, "status": "Approve"})
        elif status.casefold() == "reject":
            rejected.append({"name": name, "status": "Reject"})
        else:
            waiting.append({"name": name, "status": "chờ duyệt"})

    tool_runs = [
        {field: run[field] for field in ("run_id", "ts", "tool", "args", "result")}
        for run in runs.values()
    ]

    assumptions = [
        str(item["assumption"])
        for item in opportunities
        if item.get("assumption")
    ]
    assumptions.extend(
        [
            "Kết quả dựa trên giai đoạn dữ liệu lịch sử đã tải lên.",
            "Các giá trị tiết kiệm, phát thải, ROI và hoàn vốn là ước tính theo giả định của từng cơ hội.",
        ]
    )
    if config.get("emission_factor_kg_per_kwh") is None:
        assumptions.append("Các cơ hội tiết kiệm cần hệ số phát thải cấu hình để ước tính CO₂e tránh phát thải.")

    report_data = {
        "metadata": {
            "schema_version": 1,
            "generated_at_utc": datetime.now(UTC).isoformat(timespec="seconds"),
            "source_file": store.get("filename"),
            "source_sha256": store.get("upload_fingerprint"),
            "source_sheet": store.get("upload_sheet"),
            "date_start": scope.get("date_start"),
            "date_end": scope.get("date_end"),
            "rows": scope.get("rows"),
            "valid_timestamp_rows": scope.get("valid_timestamp_rows"),
            "total_kwh": scope.get("total_kwh"),
            "total_source": scope.get("total_source"),
            "energy_reconciliation": scope.get("energy_reconciliation"),
            "interval_quality": scope.get("interval_quality", {}),
            "dataset_run_id": runs["scope"]["run_id"],
            "column_mapping": store.get("confirmed_mapping", {}),
            "unit_conversion_factors": store.get("confirmed_unit_factors", {}),
        },
        "executive_summary": {
            "data_quality": quality_run["result"],
            "quality_run_id": quality_run["run_id"],
        },
        "seu_analysis": {
            "items": seu_items,
            "threshold": threshold,
            "run_id": runs["seu"]["run_id"],
            "error": seu_result.get("error"),
        },
        "enpi_analysis": {
            "result": enpi_result,
            "run_id": runs["enpi"]["run_id"],
            "drivers": runs["enpi"]["args"]["drivers"],
            "error": enpi_result.get("error"),
        },
        "action_plan": {
            "approved": approved,
            "waiting_for_approval": waiting,
            "rejected": rejected,
            "error": opportunity_result.get("error") if isinstance(opportunity_result, dict) else None,
        },
        "assumptions_and_limitations": assumptions,
        "run_ids": [run["run_id"] for run in tool_runs],
        "tool_runs": tool_runs,
    }
    if audit := scope.get("reported_audit"):
        report_data["reported_audit"] = audit
    if baseline_run := runs.get("energy_baseline"):
        report_data["energy_baseline"] = {
            "run_id": baseline_run["run_id"],
            "args": baseline_run["args"],
            "result": baseline_run["result"],
        }
    for optional_tool in ("energy_balance", "tariff_power_quality"):
        if optional_run := runs.get(optional_tool):
            report_data[optional_tool] = {
                "run_id": optional_run["run_id"],
                "args": optional_run["args"],
                "result": optional_run["result"],
            }
    return db._json_safe(report_data)


def generate_interpretation(report_data: dict[str, Any]) -> str:
    """Viết diễn giải định tính từ đúng dữ liệu báo cáo."""
    if not os.getenv("GEMINI_API_KEY"):
        return FIXED_INTERPRETATION

    seu = report_data.get("seu_analysis", {})
    enpi = report_data.get("enpi_analysis", {}).get("result", {})
    action_plan = report_data.get("action_plan", {})
    facts = {
        "run_ids": report_data.get("run_ids", []),
        "seu_available": bool(seu.get("items")),
        "enpi_intensity_available": enpi.get("kwh_per_ton") is not None,
        "enpi_regression_available": enpi.get("r2") is not None,
        "approved_opportunities": bool(action_plan.get("approved")),
        "pending_opportunities": bool(action_plan.get("waiting_for_approval")),
    }
    try:
        response = generate_content(
            create_client(),
            contents=(
                "Chỉ dựa trên JSON được cung cấp. Không tính, suy đoán, khuyến nghị thêm, "
                "hoặc viết số liệu/chữ số. Các con số sẽ được trình bày riêng từ dữ liệu công cụ.\n\n"
                "Viết một đoạn tiếng Việt ngắn để diễn giải dữ liệu sau, không đưa số liệu:\n"
                + json.dumps(facts, ensure_ascii=False, allow_nan=False)
            ),
            config=types.GenerateContentConfig(
                system_instruction=(
                    "Bạn viết diễn giải định tính cho báo cáo kiểm toán năng lượng. "
                    "Chỉ dựa trên dữ liệu JSON người dùng cung cấp."
                ),
                max_output_tokens=600,
            ),
        )
        interpretation = (response.text or "").strip()
        if interpretation and not any(character.isdigit() for character in interpretation):
            return interpretation
    except (RuntimeError, ValueError):
        return FIXED_INTERPRETATION
    return FIXED_INTERPRETATION


@dataclass
class _Table:
    headers: list[str]
    rows: list[list[str]]


_ReportBlock = str | list[str] | _Table


def _format_number(value: Any, decimals: int = 2) -> str:
    """Định dạng giá trị đo và giá trị thiếu."""
    if value is None:
        return "—"
    return f"{float(value):,.{decimals}f}"


def _format_percent(value: float | None) -> str:
    """Hiển thị tỷ trọng từ tỷ lệ đã tính."""
    return "—" if value is None else f"{value:.2%}"


def audit_summary_rows(audit: dict[str, Any]) -> list[tuple[str, float, int]]:
    """Tạo các dòng có đơn vị và số bản ghi dùng tính cho bảng kiểm toán."""
    labels = {
        "operating_hours": "Giờ vận hành (h)",
        "peak_demand_kw": "Công suất cực đại (kW)",
        "power_factor_mean": "Hệ số công suất trung bình",
        "steam_t": "Sản lượng hơi (tấn)",
        "energy_cost_vnd": "Chi phí năng lượng từ tệp (VNĐ)",
        "scope1_tco2e": "Scope 1 từ tệp (tCO₂e)",
        "scope2_tco2e": "Scope 2 từ tệp (tCO₂e)",
        "total_emissions_tco2e": "Tổng phát thải từ tệp (tCO₂e)",
    }
    rows = [
        (label, audit[field]["value"], audit[field]["rows"])
        for field, label in labels.items()
        if field in audit and audit[field].get("value") is not None
    ]
    for fuel, item in audit.get("boiler_fuel_kg_by_type", {}).items():
        if item.get("value") is not None:
            rows.append((f"Nhiên liệu lò hơi {fuel} (kg)", item["value"], item["rows"]))
    if eui := audit.get("building_eui"):
        rows.extend((
            ("Diện tích sàn (m²)", eui["floor_area_m2"], eui["paired_rows"]),
            ("Cường độ điện tòa nhà trong kỳ (kWh/m²)", eui["kwh_per_m2"], eui["paired_rows"]),
        ))
    if chiller := audit.get("chiller"):
        rows.extend((
            ("Tải lạnh chiller ghép với số điện (RTh)", chiller["cooling_load_rth"], chiller["paired_rows"]),
            ("Suất điện chiller (kW/RT)", chiller["kw_per_rt"], chiller["paired_rows"]),
            ("COP chiller", chiller["cop"], chiller["paired_rows"]),
        ))
    return rows


def _baseline_report_blocks(baseline_run: dict[str, Any]) -> list[_ReportBlock]:
    """Trình bày đường cơ sở từ đúng kết quả đã lưu."""
    args = baseline_run["args"]
    result = baseline_run["result"]
    blocks: list[_ReportBlock] = [f"Lần tính đường cơ sở {baseline_run['run_id']}"]
    if "error" in result:
        blocks.append(result["error"])
        return blocks

    scope = ", ".join(result.get("meter_columns", [args["energy_col"]]))
    model = result["model"]
    drivers = ", ".join(
        "Nhiệt độ ngoài trời" if driver == "temp_c" else BASELINE_DRIVER_LABELS.get(driver, driver)
        for driver in model.get("drivers", args["drivers"])
    )
    blocks.append(f"Cột điện đo · {scope}. Biến hồi quy · {drivers}.")
    coverage_rows = [
        [
            label, period["start_date"], period["end_date"],
            _format_number(period["expected_rows"], 0),
            _format_number(period["eligible_rows"], 0),
            _format_number(period["coverage_pct"]),
        ]
        for label, period in (("Cơ sở", result["baseline"]), ("Báo cáo", result["reporting"]))
    ]
    blocks.append(_Table(
        ["Kỳ", "Bắt đầu", "Kết thúc", "Số ngày", "Ngày dùng", "Độ phủ (%)"], coverage_rows
    ))
    summary = result["summary"]
    blocks.append(_Table(
        ["Chỉ số", "Giá trị"],
        [
            ["Điện đo trên các ngày so sánh (kWh)", _format_number(summary["actual_kwh"])],
            ["Điện cơ sở đã chuẩn hóa (kWh)", _format_number(summary["expected_kwh"])],
            ["Chênh lệch ước tính (kWh)", _format_number(summary["savings_kwh"])],
            ["Chênh lệch ước tính (%)", _format_number(summary["savings_pct"])],
            ["R² trong mẫu", _format_number(model["r2"], 4)],
            ["R² hiệu chỉnh", _format_number(model["adjusted_r2"], 4)],
            ["CV(RMSE) (%)", _format_number(model["cvrmse_pct"])],
            ["NMBE (%)", _format_number(model["nmbe_pct"])],
        ],
    ))
    if check := result.get("ashrae14_check"):
        uncertainty = result["uncertainty"]
        status = "Đạt" if check["passed"] else "Không đạt"
        granularity = "Ngày" if check["granularity"] == "daily" else "Tháng"
        blocks.append(_Table(
            ["Đánh giá M&V", "Giá trị"],
            [
                ["Mô hình đã chọn", model["method"]],
                ["Ngưỡng ASHRAE 14 của dự án", f"{status} · {granularity}"],
                ["CV(RMSE) dùng kiểm tra (%)", _format_number(check["cvrmse_pct"])],
                ["NMBE dùng kiểm tra (%)", _format_number(check["nmbe_pct"])],
                ["Điều chỉnh phi thường lệ (kWh)", _format_number(result["nra_total_kwh"])],
                ["FSU theo ngày ở 90% (%)", _format_number(uncertainty["fsu_90_pct"])],
                ["FSU theo ngày ở 95% (%)", _format_number(uncertainty["fsu_95_pct"])],
            ],
        ))
        if adjustments := result.get("non_routine_adjustments"):
            blocks.append(_Table(
                ["Từ ngày", "Đến ngày", "kWh/ngày", "Ngày áp dụng", "Lý do"],
                [
                    [entry["start_date"], entry["end_date"], _format_number(entry["delta_kwh_per_day"]),
                     str(entry["applied_days"]), entry["reason"]]
                    for entry in adjustments
                ],
            ))
    blocks.extend(result.get("assumptions", []))
    blocks.extend(result.get("warnings", []))
    return blocks


def _energy_balance_report_blocks(balance_run: dict[str, Any]) -> list[_ReportBlock]:
    """Tạo các khối tóm tắt cân bằng năng lượng GJ/TOE và hiệu suất thiết bị."""
    result = balance_run.get("result", {})
    blocks: list[_ReportBlock] = [f"Lần tính cân bằng năng lượng {balance_run.get('run_id', '—')}"]
    if not result.get("available"):
        blocks.append(result.get("reason") or "Chưa đủ dữ liệu cân bằng năng lượng.")
        return blocks

    summary = [
        (f"Năng lượng đầu vào {_format_number(result.get('total_input_gj'))} GJ "
         f"({_format_number(result.get('total_input_toe'), 3)} TOE)"),
        (f"Hiệu suất lò hơi {_format_number(result.get('boiler_efficiency_pct'))}% "
         f"({result.get('boiler_paired_rows', 0)} bản ghi ghép cặp)"),
        f"Chiller {_format_number(result.get('chiller_kw_per_rt'), 3)} kW/RT · "
        f"COP {_format_number(result.get('chiller_cop'))}"
        + (f" ({result['chiller_rating']})" if result.get("chiller_rating") else ""),
    ]
    blocks.append(summary)
    if rows := result.get("seu_breakdown"):
        blocks.append(
            _Table(
                ["Hạng mục", "GJ", "TOE", "Tỷ trọng (%)", "SEU"],
                [
                    [
                        str(row.get("use", "—")),
                        _format_number(row.get("gj")),
                        _format_number(row.get("toe"), 3),
                        _format_number(row.get("share_pct")),
                        "Có" if row.get("is_seu") else "Không",
                    ]
                    for row in rows
                ],
            )
        )
    outputs = [
        [label, _format_number(item.get("gj")), _format_number(item.get("toe"), 3)]
        for key, label in (("steam", "Hơi"), ("cooling", "Lạnh"))
        if (item := (result.get("outputs") or {}).get(key)) and item.get("rows")
    ]
    if outputs:
        blocks.append(_Table(["Đầu ra hữu ích", "GJ", "TOE"], outputs))
    for warning in result.get("warnings", []):
        blocks.append(f"Lưu ý: {warning}")
    return blocks


def _tariff_report_blocks(tariff_run: dict[str, Any]) -> list[_ReportBlock]:
    """Tạo các khối tóm tắt chi phí TOU, hệ số phụ tải và chất lượng điện."""
    result = tariff_run.get("result", {})
    blocks: list[_ReportBlock] = [f"Lần tính biểu giá & chất lượng điện {tariff_run.get('run_id', '—')}"]
    if not result.get("available"):
        blocks.append(result.get("reason") or "Chưa đủ dữ liệu biểu giá và chất lượng điện.")
        return blocks

    period_labels = {"peak": "Cao điểm", "normal": "Bình thường", "off_peak": "Thấp điểm"}
    summary = [
        (f"Chi phí TOU ước tính {_format_number(result.get('estimated_cost_vnd'), 0)} VNĐ "
         f"(cao điểm {_format_number(result.get('peak_share_pct'))}%)"),
        f"Tiết kiệm ước tính khi dịch tải {_format_number(result.get('shift_savings_vnd'), 0)} VNĐ",
        (f"Công suất đỉnh {_format_number(result.get('peak_demand_kw'))} kW · "
         f"Hệ số phụ tải {_format_number(result.get('load_factor'), 3)} · "
         f"cos φ trung bình {_format_number(result.get('mean_power_factor'), 3)}"),
    ]
    if result.get("pf_rows"):
        summary.append(
            f"cos φ dưới ngưỡng tại {result.get('low_pf_records', 0)} bản ghi · "
            f"Tiền phản kháng ước tính {_format_number(result.get('reactive_penalty_vnd'), 0)} VNĐ · "
            f"Dung lượng bù sơ bộ {_format_number(result.get('compensation_kvar'))} kVAr"
        )
    blocks.append(summary)
    if tou_rows := result.get("tou"):
        blocks.append(
            _Table(
                ["Khung giờ", "Bản ghi", "kWh", "VNĐ/kWh", "Chi phí ước tính (VNĐ)"],
                [
                    [
                        period_labels.get(row.get("period", ""), str(row.get("period", "—"))),
                        str(row.get("rows", 0)),
                        _format_number(row.get("kwh")),
                        _format_number(row.get("rate_vnd_per_kwh"), 0),
                        _format_number(row.get("cost_vnd"), 0),
                    ]
                    for row in tou_rows
                ],
            )
        )
    for warning in result.get("warnings", []):
        blocks.append(f"Lưu ý: {warning}")
    return blocks


def _report_sections(
    report_data: dict[str, Any], interpretation: str
) -> list[tuple[str, list[_ReportBlock]]]:
    """Tạo các mục và bảng dùng chung cho bản Markdown và Word."""
    metadata = report_data.get("metadata", {})
    scope_rows = [
        ["Thời điểm tạo (UTC)", metadata.get("generated_at_utc") or "—"],
        ["Tệp nguồn", metadata.get("source_file") or "—"],
        ["Từ ngày", metadata.get("date_start") or "—"],
        ["Đến ngày", metadata.get("date_end") or "—"],
        ["Số dòng dữ liệu", _format_number(metadata.get("rows"), 0)],
        ["Dòng có thời gian hợp lệ", _format_number(metadata.get("valid_timestamp_rows"), 0)],
        ["Tổng điện năng sau xử lý (kWh)", _format_number(metadata.get("total_kwh"))],
    ]
    if metadata.get("source_sheet"):
        scope_rows.insert(2, ["Trang tính", metadata["source_sheet"]])
    if metadata.get("source_sha256"):
        scope_rows.append(["SHA-256 tệp nguồn", metadata["source_sha256"]])
    if metadata.get("total_source") == "metered_total":
        scope_rows.append(["Nguồn điện tổng", "Công tơ tổng trong tệp"])
    elif metadata.get("total_source") == "submeter_sum":
        scope_rows.append(["Nguồn điện tổng", "Tổng các phụ tải có số đo"])
    if reconciliation := metadata.get("energy_reconciliation"):
        scope_rows.extend((
            ["Dòng đối chiếu có đủ số liệu", str(reconciliation["paired_rows"])],
            ["Điện tổng trừ tổng phụ tải trên các dòng đối chiếu (kWh)",
             _format_number(reconciliation["residual_kwh"])],
        ))
    interval_quality = metadata.get("interval_quality", {})
    if interval_quality.get("duplicate_rows_removed"):
        scope_rows.append(["Dòng lặp đã bỏ", str(interval_quality["duplicate_rows_removed"])])
    if interval_quality.get("conflicting_timestamp_rows"):
        scope_rows.append(["Dòng có thời điểm mâu thuẫn", str(interval_quality["conflicting_timestamp_rows"])])

    summary = report_data["executive_summary"]
    quality_rows = [
        [
            column,
            str(values.get("removed", 0)),
            str(values.get("interpolated", 0)),
            str(values.get("missing", 0)),
        ]
        for column, values in summary.get("data_quality", {}).items()
    ]
    summary_blocks: list[_ReportBlock] = [
        interpretation,
        f"Lần kiểm tra chất lượng dữ liệu {summary.get('quality_run_id', '—')}",
        _Table(["Cột điện", "Giá trị bị loại", "Giá trị nội suy", "Còn thiếu"], quality_rows),
    ]

    seu = report_data["seu_analysis"]
    seu_blocks: list[_ReportBlock] = [
        f"Lần tính SEU {seu.get('run_id', '—')} · Ngưỡng tích lũy {_format_percent(seu.get('threshold'))}",
        "Tỷ trọng dùng tổng điện của các phụ tải có số đo làm mẫu số.",
    ]
    if seu.get("error"):
        seu_blocks.append(seu["error"])
    else:
        seu_rows = [
            [
                item["item"],
                _format_number(item["kwh"]),
                _format_percent(item["share"]),
                _format_percent(item["cum"]),
                "Có" if item["is_seu"] else "Không",
            ]
            for item in seu.get("items", [])
        ]
        seu_blocks.append(_Table(["Hạng mục", "kWh", "Tỷ trọng", "Tích lũy", "SEU"], seu_rows))

    enpi_section = report_data["enpi_analysis"]
    enpi = enpi_section.get("result", {})
    enpi_blocks: list[_ReportBlock] = [f"Lần tính EnPI {enpi_section.get('run_id', '—')}"]
    if drivers := enpi_section.get("drivers"):
        labels = {"production_t": "Sản lượng (tấn)", "cdd": "CDD"}
        enpi_blocks.append("Biến hồi quy · " + ", ".join(labels.get(driver, driver) for driver in drivers))
    if enpi.get("error"):
        enpi_blocks.append(enpi["error"])
    else:
        metrics = [
            f"Suất tiêu hao tổng thể (kWh/tấn) {_format_number(enpi.get('kwh_per_ton'))}",
            f"R² {_format_number(enpi.get('r2'), 4)}",
        ]
        if enpi.get("intensity_rows") is not None:
            metrics.append(f"Số dòng tính suất tiêu hao {enpi['intensity_rows']}")
        if enpi.get("regression_rows") is not None:
            metrics.append(f"Số dòng đủ dữ liệu hồi quy {enpi['regression_rows']}")
        enpi_blocks.append(metrics)
        if enpi.get("regression_note"):
            enpi_blocks.append(enpi["regression_note"])
        coefficients = enpi.get("coefficients", {})
        if coefficients:
            enpi_blocks.append(
                _Table(
                    ["Hệ số hồi quy", "Giá trị"],
                    [[name, _format_number(value)] for name, value in coefficients.items()],
                )
            )
        enpi_blocks.append(
            _Table(
                ["Tháng", "kWh/tấn"],
                [
                    [month, _format_number(value)]
                    for month, value in enpi.get("monthly_kwh_per_ton", {}).items()
                ],
            )
        )

    action_plan = report_data["action_plan"]
    action_blocks: list[_ReportBlock] = []
    if action_plan.get("error"):
        action_blocks.append(action_plan["error"])
    else:
        approved_rows = [
            [
                item["name"],
                _format_number(item.get("saved_kwh")),
                _format_number(item.get("saved_vnd"), 0),
                _format_number(item.get("co2_tonnes")),
                _format_number(item.get("roi_pct")),
                _format_number(item.get("payback_months")),
            ]
            for item in action_plan.get("approved", [])
        ]
        action_blocks.extend(
            [
                "Cơ hội đã duyệt · Các giá trị trong bảng là ước tính theo giả định.",
                _Table(
                    ["Cơ hội", "kWh/năm", "VNĐ/năm", "tCO₂/năm", "ROI (%)", "Hoàn vốn (tháng)"],
                    approved_rows,
                ),
            ]
        )
        review_rows = [
            [item["name"], status]
            for key, status in (("waiting_for_approval", "Chờ duyệt"), ("rejected", "Đã từ chối"))
            for item in action_plan.get(key, [])
        ]
        if review_rows:
            action_blocks.append(_Table(["Cơ hội", "Trạng thái"], review_rows))

    run_rows = [
        [str(run["run_id"]), run["tool"], run.get("ts", "—")]
        for run in report_data.get("tool_runs", [])
    ]
    if not run_rows:
        run_rows = [[str(run_id), "—", "—"] for run_id in report_data["run_ids"]]
    sections = [
        ("Phạm vi phân tích", [_Table(["Thông tin", "Giá trị"], scope_rows)]),
        ("Tóm tắt điều hành", summary_blocks),
        ("Phân tích SEU", seu_blocks),
        ("Chỉ số hiệu suất năng lượng (EnPI)", enpi_blocks),
        ("Kế hoạch hành động", action_blocks),
        ("Giả định và hạn chế", [report_data["assumptions_and_limitations"]]),
        ("Run IDs", [_Table(["run_id", "Công cụ", "Thời điểm (UTC)"], run_rows)]),
    ]
    if baseline_run := report_data.get("energy_baseline"):
        sections.insert(4, (
            "Đường cơ sở năng lượng và chênh lệch ước tính",
            _baseline_report_blocks(baseline_run),
        ))
    extra_sections: list[tuple[str, list[_ReportBlock]]] = []
    if balance_run := report_data.get("energy_balance"):
        extra_sections.append(("Cân bằng năng lượng (GJ/TOE) và hiệu suất", _energy_balance_report_blocks(balance_run)))
    if tariff_run := report_data.get("tariff_power_quality"):
        extra_sections.append(("Biểu giá TOU và chất lượng điện", _tariff_report_blocks(tariff_run)))
    for offset, section in enumerate(extra_sections):
        sections.insert(4 + offset, section)
    audit_rows = audit_summary_rows(report_data.get("reported_audit", {}))
    if audit_rows:
        sections.insert(2, ("Vận hành, nhiệt và phát thải", [
            "Chi phí và phát thải tổng hợp từ các giá trị đã khai báo trong tệp.",
            _Table(["Chỉ số", "Giá trị", "Dòng dùng tính"], [
                [name, _format_number(value, 3), str(count)] for name, value, count in audit_rows
            ]),
        ]))
    return sections


def _document_text(value: str) -> str:
    """Làm phẳng dòng và bỏ ký tự điều khiển không hợp lệ trong Word."""
    xml_text = re.sub(r"[\x00-\x08\x0B\x0C\x0E-\x1F\uD800-\uDFFF\uFFFE\uFFFF]", "", value)
    return " ".join(xml_text.split())


def _markdown_text(value: str) -> str:
    """Giữ nội dung văn bản an toàn trong dòng và ô bảng Markdown."""
    return escape(_document_text(value), quote=False).replace("\\", "\\\\").replace("|", "\\|")


def _markdown_table(table: _Table) -> str:
    if not table.rows:
        return "Chưa có dữ liệu."

    rows = [table.headers, ["---"] * len(table.headers), *table.rows]
    return "\n".join("| " + " | ".join(map(_markdown_text, row)) + " |" for row in rows)


def render_markdown(report_data: dict[str, Any], interpretation: str) -> str:
    """Kết xuất báo cáo Markdown từ dữ liệu tổng hợp."""
    blocks = [f"# {REPORT_TITLE}"]
    for heading, section in _report_sections(report_data, interpretation):
        blocks.append(f"## {heading}")
        for block in section:
            if isinstance(block, _Table):
                blocks.append(_markdown_table(block))
            elif isinstance(block, list):
                blocks.append("\n".join(f"- {_markdown_text(line)}" for line in block))
            else:
                blocks.append(_markdown_text(block))
    return "\n\n".join(blocks) + "\n"


def render_docx(report_data: dict[str, Any], interpretation: str) -> bytes:
    """Kết xuất tài liệu Word với tiêu đề bảng lặp qua các trang."""
    document = Document()
    document.core_properties.title = REPORT_TITLE
    document.core_properties.language = "vi-VN"
    generated_at_utc = report_data.get("metadata", {}).get("generated_at_utc")
    if generated_at_utc:
        generated_at = datetime.fromisoformat(generated_at_utc)
        document.core_properties.created = generated_at
        document.core_properties.modified = generated_at
    document.add_heading(REPORT_TITLE, 0)

    for heading, section in _report_sections(report_data, interpretation):
        document.add_heading(heading, 1)
        for block in section:
            if isinstance(block, _Table):
                if not block.rows:
                    document.add_paragraph("Chưa có dữ liệu.")
                    continue
                table = document.add_table(rows=1, cols=len(block.headers))
                table.style = "Light Shading Accent 1"
                table.rows[0]._tr.get_or_add_trPr().append(OxmlElement("w:tblHeader"))
                for cell, header in zip(table.rows[0].cells, block.headers):
                    cell.text = header
                for row in block.rows:
                    for cell, text in zip(table.add_row().cells, row):
                        cell.text = _document_text(text)
            elif isinstance(block, list):
                for line in block:
                    document.add_paragraph(_document_text(line), style="List Bullet")
            else:
                document.add_paragraph(_document_text(block))

    buffer = BytesIO()
    document.save(buffer)
    return buffer.getvalue()


def render_report_json(report_data: dict[str, Any]) -> str:
    """Xuất dữ liệu báo cáo và các bản ghi đối chiếu theo JSON."""
    return json.dumps(report_data, ensure_ascii=False, allow_nan=False, indent=2) + "\n"
