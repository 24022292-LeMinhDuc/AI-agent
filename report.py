"""Tổng hợp kết quả công cụ và xuất báo cáo kiểm toán."""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from html import escape
from io import BytesIO
from typing import Any

import pandas as pd
from docx import Document
from docx.oxml import OxmlElement
from google.genai import types

import db
from agent import run_tool
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
        raise ValueError("Không tìm thấy dữ liệu đã ánh xạ trong store.")

    config = store.get("config", {})
    threshold = float(store.get("seu_threshold_value", config.get("seu_threshold", 0.8)))
    default_drivers = ["production_t", "cdd"]
    available_drivers = [column for column in default_drivers if column in df.columns]
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
        assumptions.append("Hệ số phát thải chưa cấu hình nên không tính CO₂.")

    report_data = {
        "metadata": {
            "schema_version": 1,
            "generated_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "source_file": store.get("filename"),
            "source_sheet": store.get("upload_sheet"),
            "date_start": scope.get("date_start"),
            "date_end": scope.get("date_end"),
            "rows": scope.get("rows"),
            "valid_timestamp_rows": scope.get("valid_timestamp_rows"),
            "total_kwh": scope.get("total_kwh"),
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
    if baseline_run := runs.get("energy_baseline"):
        report_data["energy_baseline"] = {
            "run_id": baseline_run["run_id"],
            "args": baseline_run["args"],
            "result": baseline_run["result"],
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
    except Exception:
        pass
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


def _baseline_report_blocks(baseline_run: dict[str, Any]) -> list[_ReportBlock]:
    """Trình bày đường cơ sở từ đúng kết quả đã lưu."""
    args = baseline_run["args"]
    result = baseline_run["result"]
    blocks: list[_ReportBlock] = [f"Lần tính đường cơ sở {baseline_run['run_id']}"]
    if "error" in result:
        blocks.append(result["error"])
        return blocks

    driver_labels = {"production_t": "Sản lượng (tấn)", "cdd": "CDD"}
    scope = ", ".join(result.get("meter_columns", [args["energy_col"]]))
    drivers = ", ".join(driver_labels[driver] for driver in args["drivers"])
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
    model = result["model"]
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
    blocks.extend(result.get("assumptions", []))
    blocks.extend(result.get("warnings", []))
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
