"""claim 级事实抽取与覆盖检查（H4）。

只抽取带单位、百分号、日期结构或明确期限的数字；无法规范化的中文数字
不强行猜测。整体策略遵循 W3 设计 5.4：宁可漏检，不可误杀。
"""

from __future__ import annotations

import re
from enum import StrEnum

from pydantic import BaseModel

from campusguide.domain.text import normalize_text


class FactKind(StrEnum):
    NUMBER = "number"
    PERCENT = "percent"
    DATE = "date"
    DURATION = "duration"
    AMOUNT = "amount"


class Fact(BaseModel):
    kind: FactKind
    raw: str
    canonical: str


_CN_DIGITS = {
    "零": 0,
    "〇": 0,
    "一": 1,
    "二": 2,
    "两": 2,
    "三": 3,
    "四": 4,
    "五": 5,
    "六": 6,
    "七": 7,
    "八": 8,
    "九": 9,
}


def chinese_to_int(text: str) -> int | None:
    """把常见中文数字转为 int；不支持或含未知字符时返回 None。"""
    if not text:
        return None
    if (
        all(ch in _CN_DIGITS for ch in text)
        and len(text) > 1
        and not any(ch in "十百千万" for ch in text)
    ):
        # 年份式连写：二〇二五 -> 2025
        return int("".join(str(_CN_DIGITS[ch]) for ch in text))
    total = 0
    section = 0
    number = 0
    for ch in text:
        if ch in _CN_DIGITS:
            number = _CN_DIGITS[ch]
        elif ch in {"十", "百", "千"}:
            unit = {"十": 10, "百": 100, "千": 1000}[ch]
            section += (number or 1) * unit
            number = 0
        elif ch == "万":
            section += number
            total += (section or 1) * 10000
            section = 0
            number = 0
        else:
            return None
    return total + section + number


def int_to_chinese(value: int) -> str | None:
    """把 1..9999 的整数转为中文数字；超出范围返回 None。"""
    if not 1 <= value <= 9999:
        return None
    digit = "零一二三四五六七八九"
    units = ["", "十", "百", "千"]
    text = str(value)
    parts: list[str] = []
    zero_pending = False
    for index, ch in enumerate(text):
        d = int(ch)
        unit = units[len(text) - 1 - index]
        if d == 0:
            zero_pending = bool(parts)
            continue
        if zero_pending:
            parts.append("零")
            zero_pending = False
        if d == 1 and unit == "十" and not parts:
            parts.append("十")
        else:
            parts.append(digit[d] + unit)
    return "".join(parts) or None


# 抽取顺序即优先级：日期 -> 百分比 -> 区间 -> 金额 -> 年份 -> 期限；
# 后续 pattern 跳过与更早命中重叠的 span。
_DATE_RE = re.compile(r"(?P<y>\d{4})\s*[-/年.]\s*(?P<m>\d{1,2})\s*[-/月.]\s*(?P<d>\d{1,2})\s*日?")
# 中文日期：二〇二五年九月一日（canonical 仍为 YYYY-MM-DD）
_DATE_CN_RE = re.compile(
    r"(?P<y>[〇零一二三四五六七八九]{4})年\s*"
    r"(?P<m>[一二三四五六七八九十]{1,2})月\s*"
    r"(?P<d>[一二三四五六七八九十]{1,3})日"
)
_PERCENT_NUM_RE = re.compile(r"(?P<n>\d+(?:\.\d+)?)\s*%")
_PERCENT_CN_RE = re.compile(r"百分之(?P<cn>[零〇一二两三四五六七八九十百点]+)")
_RANGE_RE = re.compile(
    r"(?P<a>\d+(?:\.\d+)?)\s*[-—–~～至到]{1,2}\s*(?P<b>\d+(?:\.\d+)?)\s*"
    r"(?P<unit>万元|元|学分|学时|小时|分钟|个工作日|个学年|个学期|学年|学期|年|个月|月|周|星期|天|日|人|次|门|篇|字)"
)
_AMOUNT_RE = re.compile(
    r"(?P<n>\d+(?:\.\d+)?)\s*"
    r"(?P<unit>万元|元|学分|学时|小时|分钟|人|次|门|篇|字)"
)
_AMOUNT_CN_RE = re.compile(
    r"(?P<cn>[一二两三四五六七八九十百千]{1,6})\s*"
    r"(?P<unit>万元|元|学分|学时|小时|分钟|人|次|门|篇|字)"
)
_YEAR_RE = re.compile(r"(?P<y>\d{4})\s*年(?![版份度])")
_DURATION_RE = re.compile(
    r"(?:连续|满|不少于|超过|至少)?\s*"
    r"(?<!\d)(?P<n>\d{1,3}|[一二两三四五六七八九十]{1,4})\s*个?\s*"
    r"(?P<unit>个工作日|工作日|自然日|学年|学期|年|个月|月|周|星期|天|日)"
)

_DURATION_UNIT_CANONICAL = {
    "个工作日": "工作日",
    "工作日": "工作日",
    "个学年": "学年",
    "个学期": "学期",
    "自然日": "天",
    "个月": "月",
}


class _Span(Fact):
    start: int
    end: int


def _mk(kind: FactKind, raw: str, canonical: str, start: int, end: int) -> _Span:
    return _Span(kind=kind, raw=raw, canonical=canonical, start=start, end=end)


def _extract(text: str, pattern: re.Pattern[str], build) -> list[_Span]:
    spans: list[_Span] = []
    for match in pattern.finditer(text):
        fact = build(match)
        if fact is not None:
            spans.append(fact)
    return spans


def extract_facts(text: str) -> list[Fact]:
    """从 claim 文本抽取业务事实；裸数字、条款号、版本标签忽略。"""
    if not text:
        return []
    consumed: list[tuple[int, int]] = []
    spans: list[_Span] = []

    def take(new_spans: list[_Span]) -> None:
        for span in new_spans:
            if any(span.start < end and start < span.end for start, end in consumed):
                continue
            consumed.append((span.start, span.end))
            spans.append(span)

    take(
        _extract(
            text,
            _DATE_RE,
            lambda m: _mk(
                FactKind.DATE,
                m.group(0),
                f"{int(m.group('y')):04d}-{int(m.group('m')):02d}-{int(m.group('d')):02d}",
                m.start(),
                m.end(),
            ),
        )
    )
    take(_extract(text, _DATE_CN_RE, _cn_date_fact))
    take(
        _extract(
            text,
            _PERCENT_NUM_RE,
            lambda m: _mk(
                FactKind.PERCENT,
                m.group(0),
                f"{m.group('n')}%",
                m.start(),
                m.end(),
            ),
        )
    )
    take(
        _extract(
            text,
            _PERCENT_CN_RE,
            lambda m: _cn_fact(
                FactKind.PERCENT, m.group("cn"), "%", m.start(), m.end(), m.group(0)
            ),
        )
    )
    # 区间：展开为两个端点 fact，raw 保留完整区间写法；两端共享同一 span
    for match in _RANGE_RE.finditer(text):
        unit = match.group("unit")
        canonical_unit = _DURATION_UNIT_CANONICAL.get(unit, unit)
        raw = match.group(0)
        if any(match.start() < end and start < match.end() for start, end in consumed):
            continue
        consumed.append((match.start(), match.end()))
        spans.append(
            _mk(
                FactKind.AMOUNT,
                raw,
                f"{match.group('a')}{canonical_unit}",
                match.start(),
                match.end(),
            )
        )
        spans.append(
            _mk(
                FactKind.AMOUNT,
                raw,
                f"{match.group('b')}{canonical_unit}",
                match.start(),
                match.end(),
            )
        )
    take(
        _extract(
            text,
            _AMOUNT_RE,
            lambda m: _mk(
                FactKind.AMOUNT, m.group(0), f"{m.group('n')}{m.group('unit')}", m.start(), m.end()
            ),
        )
    )
    take(
        _extract(
            text,
            _AMOUNT_CN_RE,
            lambda m: _cn_fact(
                FactKind.AMOUNT, m.group("cn"), m.group("unit"), m.start(), m.end(), m.group(0)
            ),
        )
    )
    take(
        _extract(
            text,
            _YEAR_RE,
            lambda m: _mk(FactKind.NUMBER, m.group(0), m.group("y"), m.start(), m.end()),
        )
    )
    take(
        _extract(
            text,
            _DURATION_RE,
            lambda m: _duration_fact(m),
        )
    )

    spans.sort(key=lambda item: (item.start, item.end))
    facts: list[Fact] = []
    seen: set[tuple[str, str, str]] = set()
    for span in spans:
        key = (span.kind.value, span.raw, span.canonical)
        if key in seen:
            continue
        seen.add(key)
        facts.append(Fact(kind=span.kind, raw=span.raw, canonical=span.canonical))
    return facts


def _cn_date_fact(match: re.Match[str]) -> _Span | None:
    year = chinese_to_int(match.group("y"))
    month = chinese_to_int(match.group("m"))
    day = chinese_to_int(match.group("d"))
    if year is None or month is None or day is None:
        return None
    if not (1 <= month <= 12 and 1 <= day <= 31):
        return None
    return _mk(
        FactKind.DATE,
        match.group(0),
        f"{year:04d}-{month:02d}-{day:02d}",
        match.start(),
        match.end(),
    )


def _cn_fact(kind: FactKind, cn: str, unit: str, start: int, end: int, raw: str) -> _Span | None:
    value = chinese_to_int(cn)
    if value is None:
        return None
    canonical = f"{value * 10000}元" if unit == "万元" else f"{value}{unit}"
    return _mk(kind, raw, canonical, start, end)


def _duration_fact(match: re.Match[str]) -> _Span | None:
    raw_number = match.group("n")
    unit = match.group("unit")
    value = int(raw_number) if raw_number.isdigit() else chinese_to_int(raw_number)
    if value is None:
        return None
    canonical_unit = _DURATION_UNIT_CANONICAL.get(unit, unit)
    return _mk(
        FactKind.DURATION,
        match.group(0).strip(),
        f"{value}{canonical_unit}",
        match.start(),
        match.end(),
    )


def _fact_patterns(fact: Fact) -> list[re.Pattern[str]]:
    """fact 的可接受原文形态；覆盖检查按归一化 quote 匹配。"""
    patterns: list[str] = []
    for candidate in (fact.canonical, normalize_text(fact.raw)):
        if candidate:
            patterns.append(re.escape(candidate))
    numeric = re.fullmatch(r"(\d+)(.*)", fact.canonical)
    if numeric:
        number, unit = numeric.groups()
        if int(number) <= 9999:
            chinese = int_to_chinese(int(number))
            if chinese:
                patterns.append(re.escape(f"{chinese}{unit}"))
                if unit not in {"%", "年"}:
                    patterns.append(re.escape(f"{chinese}个{unit}"))
        if int(number) == 2:
            # “两”是 2 的常用变体：两年 / 两万元 / 两个学期
            patterns.append(re.escape(f"两{unit}"))
            if unit not in {"%", "年"}:
                patterns.append(re.escape(f"两个{unit}"))
        if unit not in {"%", "年"}:
            # 引文常带量词：10个工作日
            patterns.append(re.escape(f"{number}个{unit}"))
        # 区间端点：10—28学分 / 10至28学分 / 3～4个学期 中任一端都算覆盖
        connector = r"\s*[-—–~～至到]{1,2}\s*"
        unit_pattern = r"\s*个?\s*" + re.escape(unit)
        patterns.append(re.escape(number) + connector + r"\d+(?:\.\d+)?" + unit_pattern)
        patterns.append(r"\d+(?:\.\d+)?" + connector + re.escape(number) + unit_pattern)
    if fact.kind is FactKind.PERCENT:
        percent = re.fullmatch(r"(\d+(?:\.\d+)?)%", fact.canonical)
        if percent and float(percent.group(1)).is_integer():
            chinese = int_to_chinese(int(float(percent.group(1))))
            if chinese:
                patterns.append(re.escape(f"百分之{chinese}"))
    if fact.kind is FactKind.DATE:
        shifted = re.fullmatch(r"(\d{4})-(\d{2})-(\d{2})", fact.canonical)
        if shifted:
            year, month, day = shifted.groups()
            patterns.append(re.escape(f"{year}年{int(month)}月{int(day)}日"))
            patterns.append(re.escape(f"{year}年{month}月{day}日"))
            patterns.append(re.escape(f"{year}/{int(month)}/{int(day)}"))
            patterns.append(re.escape(f"{year}/{month}/{day}"))
            patterns.append(re.escape(f"{year}.{int(month)}.{int(day)}"))
            # 中文日期：二〇二五年九月一日 / 2025年九月一日
            chinese_year = "".join("〇一二三四五六七八九"[int(digit)] for digit in year)
            chinese_month = int_to_chinese(int(month))
            chinese_day = int_to_chinese(int(day))
            if chinese_month and chinese_day:
                patterns.append(re.escape(f"{chinese_year}年{chinese_month}月{chinese_day}日"))
                patterns.append(re.escape(f"{year}年{chinese_month}月{chinese_day}日"))
    return [re.compile(pattern) for pattern in dict.fromkeys(patterns)]


def uncovered_facts(facts: list[Fact], quotes: list[str]) -> list[Fact]:
    """返回未被任一 quote 覆盖的 fact；fact 可跨多个 quote 覆盖。"""
    normalized_quotes = [normalize_text(quote) for quote in quotes if quote]
    uncovered: list[Fact] = []
    for fact in facts:
        patterns = _fact_patterns(fact)
        if not patterns:
            # 无法生成可靠匹配形态时按"未覆盖"处理，避免误放行
            uncovered.append(fact)
            continue
        if not any(pattern.search(quote) for pattern in patterns for quote in normalized_quotes):
            uncovered.append(fact)
    return uncovered
