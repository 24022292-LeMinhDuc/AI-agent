"""Khai báo JSON Schema và function declarations dùng với Gemini."""

from __future__ import annotations

_DATE_PATTERN = r"^[0-9]{4}-(0[1-9]|1[0-2])-(0[1-9]|[12][0-9]|3[01])$"
_METER_PATTERN = r"^kwh_[A-Za-z0-9_]+$"
_DRIVERS_SCHEMA = {
    "type": "array",
    "items": {"type": "string", "enum": ["production_t", "cdd"]},
    "minItems": 1,
    "maxItems": 2,
    "uniqueItems": True,
}

SCHEMAS: dict[str, dict[str, object]] = {
    "dataset_summary": {
        "type": "object",
        "properties": {},
        "required": [],
        "additionalProperties": False,
    },
    "dashboard_data": {
        "type": "object",
        "properties": {
            "level": {"type": "string", "enum": ["day", "week", "month"]},
            "start_date": {"type": "string", "pattern": _DATE_PATTERN},
            "end_date": {"type": "string", "pattern": _DATE_PATTERN},
        },
        "required": ["level", "start_date", "end_date"],
        "additionalProperties": False,
    },
    "data_quality": {
        "type": "object",
        "properties": {},
        "required": [],
        "additionalProperties": False,
    },
    "seu_analysis": {
        "type": "object",
        "properties": {
            "threshold": {"type": "number", "minimum": 0, "maximum": 1},
        },
        "required": ["threshold"],
        "additionalProperties": False,
    },
    "enpi": {
        "type": "object",
        "properties": {
            "total_col": {"type": "string", "pattern": _METER_PATTERN},
            "drivers": _DRIVERS_SCHEMA,
        },
        "required": ["total_col", "drivers"],
        "additionalProperties": False,
    },
    "energy_baseline": {
        "type": "object",
        "properties": {
            "baseline_start": {"type": "string", "pattern": _DATE_PATTERN},
            "baseline_end": {"type": "string", "pattern": _DATE_PATTERN},
            "reporting_start": {"type": "string", "pattern": _DATE_PATTERN},
            "reporting_end": {"type": "string", "pattern": _DATE_PATTERN},
            "energy_col": {"type": "string", "pattern": _METER_PATTERN},
            "drivers": _DRIVERS_SCHEMA,
        },
        "required": [
            "baseline_start", "baseline_end", "reporting_start", "reporting_end", "energy_col", "drivers"
        ],
        "additionalProperties": False,
    },
    "compare_periods": {
        "type": "object",
        "properties": {
            "month_a": {"type": "string", "pattern": r"^[0-9]{4}-(0[1-9]|1[0-2])$"},
            "month_b": {"type": "string", "pattern": r"^[0-9]{4}-(0[1-9]|1[0-2])$"},
        },
        "required": ["month_a", "month_b"],
        "additionalProperties": False,
    },
    "list_opportunities": {
        "type": "object",
        "properties": {},
        "required": [],
        "additionalProperties": False,
    },
}

TOOLS: list[dict[str, object]] = [
    {
        "name": "dataset_summary",
        "description": "Đọc thống kê và metadata của dataset đã xác nhận, không trả toàn bộ dữ liệu.",
        "input_schema": SCHEMAS["dataset_summary"],
    },
    {
        "name": "dashboard_data",
        "description": "Tổng hợp tiêu thụ theo ngày, tuần hoặc tháng trong khoảng ngày được chọn.",
        "input_schema": SCHEMAS["dashboard_data"],
    },
    {
        "name": "data_quality",
        "description": "Kiểm tra và làm sạch các cột điện trong dữ liệu đã nạp.",
        "input_schema": SCHEMAS["data_quality"],
    },
    {
        "name": "seu_analysis",
        "description": "Tính Pareto và xác định các hạng mục sử dụng năng lượng đáng kể.",
        "input_schema": SCHEMAS["seu_analysis"],
    },
    {
        "name": "enpi",
        "description": "Tính kWh/tấn từ các số đo ghép cặp và đánh giá mô hình hồi quy với sản lượng, CDD hoặc cả hai.",
        "input_schema": SCHEMAS["enpi"],
    },
    {
        "name": "energy_baseline",
        "description": (
            "Lập đường cơ sở điện theo ngày và so sánh điện đo kỳ báo cáo với dự đoán đã chuẩn hóa. "
            "Cần ngày bắt đầu/kết thúc hai kỳ, phạm vi cột điện (kwh_total cho tổng) và biến đã chọn. "
            "Chỉ dùng các kỳ đã được người dùng xác định. Chênh lệch là ước tính từ mô hình."
        ),
        "input_schema": SCHEMAS["energy_baseline"],
    },
    {
        "name": "compare_periods",
        "description": "So sánh hai tháng theo điện, nhiệt độ và sản lượng.",
        "input_schema": SCHEMAS["compare_periods"],
    },
    {
        "name": "list_opportunities",
        "description": "Liệt kê các cơ hội mẫu từ cấu hình phía máy chủ.",
        "input_schema": SCHEMAS["list_opportunities"],
    },
]
