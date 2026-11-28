"""Kiểm chứng các con số trong câu trả lời bằng kết quả của những run được phép dùng."""

from __future__ import annotations

import re
from collections.abc import Iterator
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from math import isfinite
from typing import Any

# Giữ các cách trích dẫn cũ của CLI và lịch sử trò chuyện.
_CITATION = r"(?:\[[ \t]*(?:run_id[ \t]*[:=#]?[ \t]*)?\d{1,19}[ \t]*\]|(?<!\w)run_id[ \t]*[:=#][ \t]*\d{1,19}(?!\d))"
_CITATIONS = re.compile(rf"{_CITATION}(?:[ \t,]*{_CITATION})*", re.IGNORECASE)
_NUMBER = re.compile(
    r"(?<!\w)[+−-]?(?:(?:\d{1,3}(?:[ \u00a0\u202f]\d{3})+(?!\d)|\d+)"
    r"(?:[.,]\d+)*|[.,]\d+)(?:[eE][+-]?\d+)?"
)
_UNIT = re.compile(
    r"\s*(?:(?P<scale>nghìn|ngàn|triệu|tỷ|tỉ)\s+)?"
    r"(?P<unit>%|(?:vn[đd]|đồng|₫|đ|t\s*co[2₂]e|tấn\s+co[2₂]e|kgco[2₂]e|mwh|kwh|kw|°c)"
    r"(?:\s*/\s*(?:kwh|tấn|m[²2]))?(?!\w))",
    re.IGNORECASE,
)
_SCALES = {"nghìn": 3, "ngàn": 3, "triệu": 6, "tỷ": 9, "tỉ": 9}
_DATE = re.compile(r"\d{4}-\d{2}(?:-\d{2})?(?:[T ]\d{2}:\d{2}[:.\d+Z-]*)?")
_REFERENCE_LABEL = re.compile(
    r"\b(?:scope\s*[123]|ISO\s+\d{4,5}(?::\d{4})?|ASHRAE\s+(?:Guideline\s+)?14(?:-\d{4})?)\b", re.IGNORECASE
)
_METER = re.compile(
    r"\b(?:(?P<chiller>chiller)|(?P<lighting>lighting|chiếu sáng)|"
    r"(?P<compressed_air>compressed air|khí nén)|(?P<production>production|điện sản xuất)|"
    r"(?P<total>total|tổng điện|điện tổng|tổng tiêu thụ)|(?P<other>điện phụ trợ khác|kwh other))\b",
    re.IGNORECASE,
)
_METRIC = re.compile(
    r"\b(?:(?P<savings>tiết kiệm|savings?|saved)|(?P<actual>thực đo|thực tế|đo được|measured|actual)"
    r"|(?P<baseline>đường cơ sở|cơ sở chuẩn hóa|tham chiếu|expected|baseline)"
    r"|(?P<roi>ROI)|(?P<payback>hoàn vốn|payback))\b", re.IGNORECASE
)
_FIELD_METRICS = {
    "actual_kwh": "actual", "total_kwh": "actual", "metered_total_kwh": "actual",
    "energy_kwh_by_column": "actual", "expected_kwh": "baseline", "base_kwh_annual": "baseline",
    "saved_kwh": "savings", "savings_kwh": "savings", "annual_kwh_saved": "savings",
    "saved_vnd": "savings", "annual_cost_saved_vnd": "savings", "shift_savings_vnd": "savings",
    "savings_pct": "savings", "saving_pct": "saving_assumption", "annual_co2_saved_t": "savings",
    "co2_tonnes": "savings", "roi_pct": "roi", "payback_months": "payback",
}


def _citation_ids(text: str) -> list[int]:
    return [int(value) for value in re.findall(r"\d+", text)]


def extract_run_ids(answer: str) -> list[int]:
    ids = (run_id for group in _CITATIONS.finditer(answer) for run_id in _citation_ids(group[0]))
    return list(dict.fromkeys(ids))


def _readings(raw: str, unit: str) -> Iterator[Decimal]:
    """Đọc cả quy ước 1.234,56 và 1,234.56, giữ nguyên độ chính xác được viết."""
    raw = "".join(raw.split()).replace("−", "-")
    mantissa, _, exponent = raw.lower().partition("e")
    # Với kWh và tiền, một dấu phẩy kèm đúng ba chữ số là dấu nhóm nghìn.
    if unit in {"kwh", "vnd", "vnd/kwh"} and re.fullmatch(r"[+-]?[1-9]\d{0,2},\d{3}", mantissa):
        mantissa = mantissa.replace(",", "")
    for decimal_mark, grouping in ((".", ","), (",", ".")):
        integer, mark, fraction = mantissa.partition(decimal_mark)
        if mark and not fraction.isdigit():
            continue
        if grouping in integer and not re.fullmatch(
            rf"[+-]?[1-9]\d{{0,2}}(?:{re.escape(grouping)}\d{{3}})+", integer
        ):
            continue
        normalized = integer.replace(grouping, "") + ("." + fraction if mark else "")
        try:
            number = Decimal(normalized + ("e" + exponent if exponent else ""))
            as_float = float(number)
            if isfinite(as_float) and (as_float or number.is_zero()):
                yield number
        except InvalidOperation:
            continue
        if not mark and grouping not in integer:
            break


def _unit_name(label: str) -> str:
    label = "".join(label.lower().split()).replace("₂", "2").replace("²", "2")
    head, slash, denominator = label.partition("/")
    aliases = {"vnđ": "vnd", "đồng": "vnd", "đ": "vnd", "₫": "vnd", "tấnco2e": "tco2e"}
    return aliases.get(head, head) + slash + denominator


def _unit_after(text: str, position: int) -> tuple[str, int, int]:
    match = _UNIT.match(text, position)
    if not match:
        return "", 0, position
    return _unit_name(match["unit"]), _SCALES.get((match["scale"] or "").lower(), 0), match.end()


def _field_unit(path: tuple[str, ...]) -> str:
    keys = " ".join(path).lower()
    words = set(keys.replace("_", " ").split())
    if words & {"rows", "count", "days", "months", "missing", "removed", "interpolated", "observations"}:
        return "count"
    if words & {"share", "cum", "ratio", "fraction"} or "confidence_level" in path:
        return "fraction"
    if words & {"pct", "percent", "percentage"}:
        return "%"
    for token, unit in (("vnd_per_kwh", "vnd/kwh"), ("kwh_per_ton", "kwh/tấn"),
                        ("kwh_per_m2", "kwh/m2"), ("kgco2e_per_kwh", "kgco2e/kwh"),
                        ("co2_tonnes", "tco2e")):
        if token in keys:
            return unit
    if words & {"temp", "temperature"}:
        return "°c"
    if words & {"tons", "area", "people", "hours", "vif", "cop"}:
        return "other"
    for unit in ("tco2e", "vnd", "kwh", "mwh", "kw"):
        if unit in words:
            return unit
    return ""


@dataclass(frozen=True)
class _Fact:
    value: Decimal
    unit: str
    meter: str | None
    metric: str | None = None


def _source_facts(value: Any, path: tuple[str, ...] = (), explicit_unit: str = "") -> Iterator[_Fact]:
    if isinstance(value, dict):
        meter = value.get("applies_to")
        if isinstance(meter, str):
            path = (*path, meter)
        for key, item in value.items():
            key = str(key)
            if _DATE.fullmatch(key):
                yield from _source_facts(key)
            label = value.get("unit") if key == "value" else None
            yield from _source_facts(item, (*path, key), _unit_name(label) if isinstance(label, str) else "")
    elif isinstance(value, (tuple, list)):
        for item in value:
            yield from _source_facts(item, path)
    elif type(value) in (float, int):
        number = Decimal(str(value))
        if number.is_finite():
            meter = _METER.search(" ".join(path).replace("_", " "))
            metric = next((_FIELD_METRICS[key] for key in reversed(path) if key in _FIELD_METRICS), None)
            yield _Fact(number, explicit_unit or _field_unit(path), meter.lastgroup if meter else None, metric)
    elif isinstance(value, str):
        if _DATE.fullmatch(value):
            for component in re.findall(r"\d+", value):
                yield _Fact(Decimal(component), "date", None)
        else:
            for match in _NUMBER.finditer(value):
                label, scale, _ = _unit_after(value, match.end())
                if label or match[0] == value.strip():
                    label = label or explicit_unit or _field_unit(path)
                    for reading in _readings(match[0], label):
                        yield _Fact(reading * 10**scale if scale else reading, label, None)


def _matches(raw: str, unit: str, scale: int, meter: str | None, metric: str | None,
             facts: set[_Fact]) -> bool:
    readings = tuple(_readings(raw, unit))
    for fact in facts:
        if metric and fact.metric != metric:
            continue
        if unit == "kwh" and meter and fact.meter and fact.meter != meter:
            continue
        if unit and fact.unit and unit != fact.unit and not (unit == "%" and fact.unit == "fraction"):
            continue
        stored = fact.value / 10**scale if scale else fact.value
        if unit == "%" and fact.unit == "fraction":
            candidates = (stored * 100,)
        elif unit == "%" and not fact.unit and abs(stored) <= 1:
            candidates = (stored, stored * 100)
        else:
            candidates = (stored,)
        for printed in readings:
            for candidate in candidates:
                try:
                    if candidate == printed or candidate.quantize(printed, rounding=ROUND_HALF_UP) == printed:
                        return True
                except InvalidOperation:
                    continue
    return False


def _claim_groups(answer: str) -> Iterator[tuple[str, list[int]]]:
    """Gắn các số trong cùng mệnh đề với trích dẫn ngay sau hoặc ở đầu mệnh đề."""
    answer = _REFERENCE_LABEL.sub(" ", answer.replace("**", "").replace("`", ""))
    answer = re.sub(r"(?m)^[ \t]*\d+[.)](?=[ \t])", "", answer)
    # Cho phép đặt trích dẫn ngay sau dấu chấm kết câu.
    answer = re.sub(rf"([.!?])\s+(?={_CITATION})", " ", answer, flags=re.IGNORECASE)
    for sentence in re.split(r"[\n;!?]+|\.(?!\d)(?=\s|$)", answer):
        start = 0
        prefix_ids = []
        for group in _CITATIONS.finditer(sentence):
            ids = _citation_ids(group[0])
            preceding = sentence[start:group.start()]
            yield preceding, ids
            prefix_ids = [] if _NUMBER.search(preceding) else ids
            start = group.end()
        yield sentence[start:], prefix_ids


def verify_results(answer: str, results: dict[int, Any]) -> dict[str, Any]:
    """Đối chiếu từng số với các run được trích dẫn tại câu đó."""
    run_ids = extract_run_ids(answer)
    sources = {
        run_id: set(_source_facts(results[run_id])) for run_id in run_ids
        if run_id in results and not (isinstance(results[run_id], dict) and "error" in results[run_id])
    }
    missing_ids = [run_id for run_id in run_ids if run_id not in sources]
    checked = []
    missing_citations = []
    issues = []
    for claim, ids in _claim_groups(answer):
        facts = set().union(*(sources[run_id] for run_id in ids if run_id in sources))
        previous_end = 0
        for match in _NUMBER.finditer(claim):
            label, scale, end = _unit_after(claim, match.end())
            meters = list(_METER.finditer(claim, 0, match.start())) if label == "kwh" else []
            context = claim[previous_end:match.start()]
            labels = list(_METRIC.finditer(context))
            after_unit = _METRIC.match(claim[end:].lstrip()) if label else None
            metric = after_unit.lastgroup if after_unit else (labels[-1].lastgroup if labels else None)
            if metric == "savings" and label == "%" and re.search(r"giả định|assumed", context, re.IGNORECASE):
                metric = "saving_assumption"
            matched = _matches(match[0], label, scale, meters[-1].lastgroup if meters else None, metric, facts)
            previous_end = end
            checked.append({"text": match[0], "matched": matched})
            if not matched:
                issues.append({"quantity": claim[match.start():end], "run_ids": ids})
            if not ids:
                missing_citations.append(match[0])

    unmatched = [item for item in checked if not item["matched"]]
    return {
        "verified": bool(run_ids) and not missing_ids and not unmatched,
        "run_ids": run_ids, "missing_run_ids": missing_ids,
        "numbers_checked": checked, "unmatched_numbers": unmatched,
        "missing_citations": missing_citations, "issues": issues,
    }
