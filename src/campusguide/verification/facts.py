"""W3 Fact 层：抽取、规范化与 quote 覆盖。

只抽取有业务含义的数字事实：百分比、日期、年份、期限、金额/学分/学时。
规则宁可漏检不可误杀；无法可靠规范化的中文数字记录为 unparsed，不参与拦截。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date

from campusguide.domain.enums import FactKind
from campusguide.domain.models import Fact
from campusguide.domain.text import normalize_text
from campusguide.retrieval.source_spans import (
    StructuredTableRow,
    structured_cell_canonical,
    structured_table_row,
)
from campusguide.verification.quotes import locate_quote

_CN_DIGITS = {
    "〇": 0,
    "零": 0,
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
_CN_UNITS = {"十": 10, "百": 100, "千": 1000}
_CN_BIG_UNITS = {"万": 10000, "亿": 100000000}
_CN_NUMERAL_CHARS = "〇零一二三四五六七八九十百千万亿两廿卅卌"
_NUM_TOKEN = rf"(?:\d+(?:\.\d+)?|[{_CN_NUMERAL_CHARS}]+)"
_NUM_BOUNDARY = rf"(?<![{_CN_NUMERAL_CHARS}点.])"
_DATE_SEPARATOR = r"(?:-|—|–|~|至|到)"


def chinese_to_int(text: str) -> int | None:
    """把常见中文数字转成整数；无法可靠解析时返回 None。"""
    text = text.strip()
    if not text:
        return None
    if all(char in _CN_DIGITS for char in text):
        return int("".join(str(_CN_DIGITS[char]) for char in text))

    total = 0
    section = 0
    number = 0
    for char in text:
        if char in _CN_DIGITS:
            number = _CN_DIGITS[char]
        elif char in _CN_UNITS:
            unit = _CN_UNITS[char]
            if number == 0:
                number = 1
            section += number * unit
            number = 0
        elif char in _CN_BIG_UNITS:
            section += number
            if section == 0:
                section = 1
            total += section * _CN_BIG_UNITS[char]
            section = 0
            number = 0
        else:
            return None
    return total + section + number


def _parse_number(token: str) -> float | None:
    if re.fullmatch(r"\d+(?:\.\d+)?", token):
        return float(token)
    value = chinese_to_int(token)
    return float(value) if value is not None else None


def _format_number(value: float) -> str:
    if float(value).is_integer():
        return str(int(value))
    return f"{value:.4f}".rstrip("0").rstrip(".")


@dataclass(frozen=True)
class _UnitSpec:
    pattern: str
    suffix: str
    kind: FactKind
    multiplier: float = 1.0


_UNIT_SPECS = (
    _UnitSpec(r"亿元(?!半|余|多)", "元", FactKind.AMOUNT, 100_000_000),
    _UnitSpec(r"万元(?!半|余|多)", "元", FactKind.AMOUNT, 10_000),
    _UnitSpec(r"元(?!半|余|多)", "元", FactKind.AMOUNT),
    _UnitSpec(r"个?学分(?!半|余|多)", "学分", FactKind.NUMBER),
    _UnitSpec(r"个?学时(?!半|余|多)", "学时", FactKind.NUMBER),
    _UnitSpec(r"个?工作日(?!半|余|多)", "工作日", FactKind.DURATION),
    _UnitSpec(r"个?自然日(?!半|余|多)", "自然日", FactKind.DURATION),
    _UnitSpec(r"个?学期(?!半|余|多)", "学期", FactKind.DURATION),
    _UnitSpec(r"个?学年(?!半|余|多)", "学年", FactKind.DURATION),
    _UnitSpec(r"个?月(?!半|余|多)", "月", FactKind.DURATION),
    _UnitSpec(r"个?星期(?!半|余|多)", "星期", FactKind.DURATION),
    _UnitSpec(r"个?周(?!半|余|多)", "周", FactKind.DURATION),
    _UnitSpec(r"年(?!级|代|版|半|余|多)", "年", FactKind.DURATION),
    _UnitSpec(r"天(?!半|余|多)", "天", FactKind.DURATION),
    _UnitSpec(r"小时(?!半|余|多)", "小时", FactKind.DURATION),
)
_ALL_UNIT_PATTERN = "|".join(f"(?:{spec.pattern})" for spec in _UNIT_SPECS)
_AMOUNT_UNIT_PATTERN = "|".join(
    f"(?:{spec.pattern})" for spec in _UNIT_SPECS if spec.kind in {FactKind.AMOUNT, FactKind.NUMBER}
)
_DURATION_UNITS = tuple(spec.pattern for spec in _UNIT_SPECS if spec.kind is FactKind.DURATION)
_DURATION_UNIT_PATTERN = "|".join(f"(?:{pattern})" for pattern in _DURATION_UNITS)

_RANGE_RE = re.compile(
    rf"{_NUM_BOUNDARY}(?P<a>{_NUM_TOKEN})\s*{_DATE_SEPARATOR}\s*"
    rf"(?P<b>{_NUM_TOKEN})\s*(?P<unit>{_ALL_UNIT_PATTERN})"
)
_PERCENT_RANGE_RE = re.compile(
    rf"{_NUM_BOUNDARY}(?P<a>{_NUM_TOKEN})\s*{_DATE_SEPARATOR}\s*"
    rf"(?P<b>{_NUM_TOKEN})\s*%"
)
_PERCENT_RE = re.compile(r"(?<!\d)(?P<num>\d+(?:\.\d+)?)\s*%")
_CN_PERCENT_RE = re.compile(r"百分之(?P<num>[〇零一二三四五六七八九十百千万亿两0-9.]+)(?!点)")
_AMOUNT_RE = re.compile(rf"{_NUM_BOUNDARY}(?P<num>{_NUM_TOKEN})\s*(?P<unit>{_AMOUNT_UNIT_PATTERN})")
_DURATION_RE = re.compile(
    rf"{_NUM_BOUNDARY}(?P<num>{_NUM_TOKEN})\s*(?P<unit>{_DURATION_UNIT_PATTERN})"
)
_UNPARSED_RE = re.compile(rf"(?P<num>[{_CN_NUMERAL_CHARS}]+)\s*(?P<unit>{_ALL_UNIT_PATTERN})")
_CN_DECIMAL_RE = re.compile(
    rf"[{_CN_NUMERAL_CHARS}]+点[{_CN_NUMERAL_CHARS}]+(?:{_ALL_UNIT_PATTERN})?"
)

_DATE_PATTERNS = (
    re.compile(r"(?<!\d)(\d{4})-(\d{1,2})-(\d{1,2})(?!\d)"),
    re.compile(r"(?<!\d)(\d{4})/(\d{1,2})/(\d{1,2})(?!\d)"),
    re.compile(r"(?<!\d)(\d{4})年(\d{1,2})月(\d{1,2})[日号]"),
    re.compile(
        r"([〇零一二三四五六七八九]{4})年"
        r"([一二三四五六七八九十]{1,3})月"
        r"([一二三四五六七八九十]{1,3})[日号]"
    ),
)
_DATE_LIKE_PATTERNS = (
    re.compile(r"(?<!\d)\d{4}\s*[-/年]\s*\d{1,2}\s*[-/月]\s*(?:\d{1,2}\s*[日号]?)?"),
    re.compile(
        r"[〇零一二三四五六七八九]{4}年[一二三四五六七八九十]{1,3}月"
        r"(?:[一二三四五六七八九十]{1,3}[日号])?"
    ),
)
_ARABIC_YEAR_RE = re.compile(r"(?<!\d)((?:19|20)\d{2})年(?!版)")
_CN_YEAR_RE = re.compile(r"([〇零一二三四五六七八九]{4})年(?!版)")
_VERSION_LABEL_RE = re.compile(r"(?:(?:19|20)\d{2}\s*年?\s*版|v\d{4})", re.IGNORECASE)
_ACADEMIC_YEAR_LABEL_RE = re.compile(
    r"(?<!\d)(?P<start>(?:19|20)\d{2})\s*"
    r"(?:-+|—+|–+|－+|−+|~+|～+|〜+|至|到)\s*"
    r"(?P<end>(?:19|20)\d{2})\s*学年"
)
_ACADEMIC_YEAR_FACT_RE = re.compile(r"(?P<year>(?:19|20)\d{2})学年")


@dataclass
class FactExtraction:
    facts: list[Fact] = field(default_factory=list)
    unparsed: list[str] = field(default_factory=list)


@dataclass
class FactCoverage:
    claim_facts: list[Fact] = field(default_factory=list)
    covered: list[Fact] = field(default_factory=list)
    uncovered: list[Fact] = field(default_factory=list)
    unparsed: list[str] = field(default_factory=list)
    quote_unparsed: list[str] = field(default_factory=list)


def _overlaps(spans: list[tuple[int, int]], start: int, end: int) -> bool:
    return any(start < span_end and end > span_start for span_start, span_end in spans)


def _unit_spec(unit: str) -> _UnitSpec | None:
    for spec in _UNIT_SPECS:
        if re.fullmatch(spec.pattern, unit):
            return spec
    return None


def _fact_for(spec: _UnitSpec, value: float, *, raw: str) -> Fact:
    canonical = f"{_format_number(value * spec.multiplier)}{spec.suffix}"
    return Fact(kind=spec.kind, raw=raw, canonical=canonical)


def _percent_fact(value: float, raw: str) -> Fact:
    return Fact(kind=FactKind.PERCENT, raw=raw, canonical=f"{_format_number(value)}%")


def _valid_date(year: int, month: int, day: int) -> bool:
    if not 1900 <= year <= 2100:
        return False
    try:
        date(year, month, day)
    except ValueError:
        return False
    return True


def _parse_date_match(match: re.Match[str]) -> tuple[int, int, int] | None:
    year_text, month_text, day_text = match.group(1), match.group(2), match.group(3)
    year = chinese_to_int(year_text) if not year_text.isdigit() else int(year_text)
    month = chinese_to_int(month_text) if not month_text.isdigit() else int(month_text)
    day = chinese_to_int(day_text) if not day_text.isdigit() else int(day_text)
    if year is None or month is None or day is None:
        return None
    if not _valid_date(year, month, day):
        return None
    return year, month, day


def extract_facts_detailed(text: str) -> FactExtraction:
    normalized = normalize_text(text)
    extraction = FactExtraction()
    seen: set[str] = set()

    date_spans: list[tuple[int, int]] = []
    date_like_spans: list[tuple[int, int]] = []
    version_spans: list[tuple[int, int]] = []
    year_spans: list[tuple[int, int]] = []
    range_spans: list[tuple[int, int]] = []
    blocked_single_spans: list[tuple[int, int]] = []

    def add(fact: Fact) -> None:
        if fact.canonical in seen:
            return
        seen.add(fact.canonical)
        extraction.facts.append(fact)

    for match in _VERSION_LABEL_RE.finditer(normalized):
        version_spans.append((match.start(), match.end()))

    for pattern in _DATE_PATTERNS:
        for match in pattern.finditer(normalized):
            parsed = _parse_date_match(match)
            if parsed is None:
                continue
            year, month, day = parsed
            canonical = f"{year:04d}-{month:02d}-{day:02d}"
            raw = match.group(0)
            add(Fact(kind=FactKind.DATE, raw=raw, canonical=canonical))
            add(Fact(kind=FactKind.DATE, raw=raw, canonical=f"{year:04d}"))
            date_spans.append((match.start(), match.end()))

    for pattern in _DATE_LIKE_PATTERNS:
        for match in pattern.finditer(normalized):
            date_like_spans.append((match.start(), match.end()))

    for match in _ARABIC_YEAR_RE.finditer(normalized):
        if _overlaps(date_spans, match.start(), match.end()):
            continue
        if _overlaps(version_spans, match.start(), match.end()):
            continue
        year = int(match.group(1))
        add(Fact(kind=FactKind.DATE, raw=match.group(0), canonical=f"{year:04d}"))
        year_spans.append((match.start(), match.end()))

    for match in _CN_YEAR_RE.finditer(normalized):
        if _overlaps(date_spans, match.start(), match.end()):
            continue
        if _overlaps(version_spans, match.start(), match.end()):
            continue
        year = chinese_to_int(match.group(1))
        if year is None or not 1900 <= year <= 2100:
            continue
        add(Fact(kind=FactKind.DATE, raw=match.group(0), canonical=f"{year:04d}"))
        year_spans.append((match.start(), match.end()))

    for match in _PERCENT_RANGE_RE.finditer(normalized):
        span = (match.start(), match.end())
        if _overlaps(date_spans + date_like_spans, *span):
            continue
        first = _parse_number(match.group("a"))
        second = _parse_number(match.group("b"))
        if first is None or second is None:
            continue
        raw = match.group(0)
        add(_percent_fact(first, raw))
        add(_percent_fact(second, raw))
        range_spans.append(span)

    for match in _RANGE_RE.finditer(normalized):
        span = (match.start(), match.end())
        if _overlaps(date_spans + date_like_spans + version_spans, *span):
            continue
        spec = _unit_spec(match.group("unit"))
        first = _parse_number(match.group("a"))
        second = _parse_number(match.group("b"))
        if spec is None or first is None or second is None:
            continue
        raw = match.group(0)
        add(_fact_for(spec, first, raw=raw))
        add(_fact_for(spec, second, raw=raw))
        range_spans.append(span)

    for match in _PERCENT_RE.finditer(normalized):
        span = (match.start(), match.end())
        if _overlaps(range_spans, *span):
            continue
        value = _parse_number(match.group("num"))
        if value is None:
            continue
        add(_percent_fact(value, match.group(0)))

    for match in _CN_PERCENT_RE.finditer(normalized):
        span = (match.start(), match.end())
        if _overlaps(range_spans, *span):
            continue
        value = _parse_number(match.group("num"))
        if value is None:
            continue
        add(_percent_fact(value, match.group(0)))

    blocked_single_spans = date_spans + date_like_spans + version_spans + year_spans + range_spans

    for match in _AMOUNT_RE.finditer(normalized):
        span = (match.start(), match.end())
        if _overlaps(blocked_single_spans, *span):
            continue
        spec = _unit_spec(match.group("unit"))
        value = _parse_number(match.group("num"))
        if spec is None or value is None:
            continue
        add(_fact_for(spec, value, raw=match.group(0)))

    for match in _DURATION_RE.finditer(normalized):
        span = (match.start(), match.end())
        if _overlaps(blocked_single_spans, *span):
            continue
        spec = _unit_spec(match.group("unit"))
        value = _parse_number(match.group("num"))
        if spec is None or value is None:
            continue
        add(_fact_for(spec, value, raw=match.group(0)))

    for match in _UNPARSED_RE.finditer(normalized):
        if _parse_number(match.group("num")) is not None:
            continue
        token = match.group(0)
        if token not in extraction.unparsed:
            extraction.unparsed.append(token)

    for match in _CN_DECIMAL_RE.finditer(normalized):
        token = match.group(0)
        if token not in extraction.unparsed:
            extraction.unparsed.append(token)

    return extraction


def extract_facts(text: str) -> list[Fact]:
    return extract_facts_detailed(text).facts


def check_fact_coverage(claim_text: str, quote_texts: list[str]) -> FactCoverage:
    claim_extraction = extract_facts_detailed(claim_text)
    quote_canonicals: set[str] = set()
    quote_unparsed: list[str] = []
    for quote in quote_texts:
        quote_extraction = extract_facts_detailed(quote)
        quote_canonicals.update(fact.canonical for fact in quote_extraction.facts)
        quote_unparsed.extend(quote_extraction.unparsed)

    covered = [fact for fact in claim_extraction.facts if fact.canonical in quote_canonicals]
    uncovered = [fact for fact in claim_extraction.facts if fact.canonical not in quote_canonicals]
    return FactCoverage(
        claim_facts=claim_extraction.facts,
        covered=covered,
        uncovered=uncovered,
        unparsed=claim_extraction.unparsed,
        quote_unparsed=list(dict.fromkeys(quote_unparsed)),
    )


def facts_not_covered(claim_text: str, quote_texts: list[str]) -> list[Fact]:
    return check_fact_coverage(claim_text, quote_texts).uncovered


_STRUCTURED_HOURS_CONTEXT = (
    (re.compile(r"周学时|每(?:周|星期)学时|学时\s*/\s*周"), "weekly_hours"),
    (re.compile(r"总学时|学时总数|学时合计|总课时"), "total_hours"),
    (re.compile(r"理论|讲授"), "theory_hours"),
    (re.compile(r"实验|上机"), "experiment_hours"),
    (re.compile(r"实习|实践|实训"), "practice_hours"),
)
_AGGREGATE_CREDIT_RE = re.compile(r"总学分|毕业学分|最低学分|修满|合计|共计|学分要求")
_SENTENCE_BREAK = "。；;\n"
_CLAUSE_BREAK = "，,、。；;：:\n"
_STRUCTURED_ROW_KINDS = frozenset({"table_row", "matrix_row"})


def _before_context(text: str, position: int, size: int = 18) -> str:
    window = text[max(0, position - size) : position]
    cut = max((window.rfind(char) for char in "，,。；;：:\n"), default=-1)
    return window[cut + 1 :]


def _hours_field(window: str) -> str:
    best_start = -1
    best_field = "total_hours"
    for pattern, field_name in _STRUCTURED_HOURS_CONTEXT:
        for match in pattern.finditer(window):
            if match.start() >= best_start:
                best_start = match.start()
                best_field = field_name
    return best_field


def _fact_field(fact: Fact, claim_text: str, position: int) -> str | None:
    """Claim-side semantic expectation for one fact occurrence."""
    window = _before_context(claim_text, position)
    if fact.kind is FactKind.NUMBER and fact.canonical.endswith("学分"):
        if _AGGREGATE_CREDIT_RE.search(window):
            return None
        return "credits"
    if fact.kind is FactKind.NUMBER and fact.canonical.endswith("学时"):
        return _hours_field(window)
    if fact.kind is FactKind.AMOUNT and fact.canonical.endswith("元"):
        return "amount"
    if fact.kind is FactKind.DURATION:
        if fact.canonical.endswith("周"):
            return "weeks"
        # “第8学期”/“修读学期为8” is a curriculum slot, not a duration.
        if fact.canonical.endswith("学期") and re.search(
            r"第\s*$|修读学期|开课学期", window
        ):
            return "semester"
    return None


def _fact_field_occurrences(fact: Fact, claim_text: str) -> list[tuple[int, str | None]]:
    if not fact.raw:
        return []
    return [
        (match.start(), _fact_field(fact, claim_text, match.start()))
        for match in re.finditer(re.escape(fact.raw), claim_text)
    ]


def _structured_fact_candidate(fact: Fact) -> bool:
    if fact.kind is FactKind.NUMBER:
        return fact.canonical.endswith(("学分", "学时"))
    if fact.kind is FactKind.AMOUNT:
        return fact.canonical.endswith("元")
    if fact.kind is FactKind.DURATION:
        return fact.canonical.endswith(("周", "学期"))
    return False


def _sentence_bounds(text: str, position: int) -> tuple[int, int]:
    start = 0
    for index in range(position - 1, -1, -1):
        if text[index] in _SENTENCE_BREAK:
            start = index + 1
            break
    end = len(text)
    for index in range(position, len(text)):
        if text[index] in _SENTENCE_BREAK:
            end = index
            break
    return start, end


def _clause_bounds(text: str, position: int) -> tuple[int, int]:
    start = 0
    for index in range(position - 1, -1, -1):
        if text[index] in _CLAUSE_BREAK:
            start = index + 1
            break
    end = len(text)
    for index in range(position, len(text)):
        if text[index] in _CLAUSE_BREAK:
            end = index
            break
    return start, end


def _entity_word_char(char: str) -> bool:
    return (char.isascii() and char.isalnum()) or "\u2160" <= char <= "\u217f"


def _entity_boundary_ok(text: str, start: int, end: int, entity: str) -> bool:
    if entity.isdigit() and (
        (start > 0 and text[start - 1].isdigit())
        or (end < len(text) and text[end].isdigit())
    ):
        return False
    if (
        entity
        and _entity_word_char(entity[-1])
        and end < len(text)
        and _entity_word_char(text[end])
    ):
        return False
    return not (
        entity
        and _entity_word_char(entity[0])
        and start > 0
        and _entity_word_char(text[start - 1])
    )


def _entity_occurrences(
    text: str, start: int, end: int, rows: list[StructuredTableRow]
) -> list[tuple[int, int, str, str]]:
    matches: list[tuple[int, int, str, str]] = []
    for row in rows:
        for entity in row.entities:
            if len(entity) < 2:
                continue
            cursor = start
            while True:
                index = text.find(entity, cursor, end)
                if index < 0:
                    break
                if _entity_boundary_ok(text, index, index + len(entity), entity):
                    matches.append((index, index + len(entity), row.row_id, entity))
                cursor = index + max(1, len(entity))
    kept: list[tuple[int, int, str, str]] = []
    for match in matches:
        covered_by_longer = any(
            other is not match
            and other[0] <= match[0]
            and other[1] >= match[1]
            and (other[1] - other[0]) > (match[1] - match[0])
            for other in matches
        )
        if not covered_by_longer:
            kept.append(match)
    return kept


def _bound_row_ids(
    rows: list[StructuredTableRow], claim_text: str, position: int
) -> set[str]:
    """Bind the row whose full entity set (e.g. category + grade) best matches this clause."""
    clause_start, clause_end = _clause_bounds(claim_text, position)
    matches = _entity_occurrences(claim_text, clause_start, clause_end, rows)
    if not matches:
        sentence_start, sentence_end = _sentence_bounds(claim_text, position)
        matches = _entity_occurrences(claim_text, sentence_start, sentence_end, rows)
    if not matches:
        matches = _entity_occurrences(claim_text, 0, len(claim_text), rows)
        if len({row_id for _, _, row_id, _ in matches}) != 1:
            return set()
    if not matches:
        return set()
    matched: dict[str, set[str]] = {}
    distances: dict[str, int] = {}
    for start, end, row_id, entity in matches:
        matched.setdefault(row_id, set()).add(entity)
        distance = min(abs(start - position), abs(end - position))
        distances[row_id] = min(distances.get(row_id, distance), distance)
    # A row naming more of its own entities (category + grade) outranks a row
    # matching only a shared label such as "一等奖".
    kept = [
        row_id
        for row_id, entities in matched.items()
        if not any(
            other_id != row_id and other_entities > entities
            for other_id, other_entities in matched.items()
        )
    ]
    if not kept:
        return set()
    if len(kept) > 1 and len({frozenset(matched[row_id]) for row_id in kept}) == 1:
        return set()
    nearest = min(distances[row_id] for row_id in kept)
    winners = {row_id for row_id in kept if distances[row_id] == nearest}
    return winners if len(winners) == 1 else set()


@dataclass
class _StructuredQuote:
    row: StructuredTableRow
    spans: list[tuple[int, int]]


def _structured_quote_rows(quotes: list[str], articles: list) -> list[_StructuredQuote]:
    if not quotes or not articles:
        return []
    merged: dict[tuple, _StructuredQuote] = {}
    bodies: dict[tuple[str, str], set[str]] = {}

    def add(row: StructuredTableRow, spans: list[tuple[int, int]], article) -> None:
        if not spans:
            return
        identity = (row.table_id, row.row_id)
        bodies.setdefault(identity, set()).add(article.text)
        key = (
            article.doc_id,
            article.version_id,
            article.article_id,
            article.part_no,
            article.text,
            *identity,
        )
        item = merged.get(key)
        if item is None:
            item = _StructuredQuote(row=row, spans=[])
            merged[key] = item
        for span in spans:
            if span not in item.spans:
                item.spans.append(span)

    if len(quotes) == len(articles):
        for quote, article in zip(quotes, articles, strict=True):
            row = structured_table_row(article)
            span = locate_quote(article.text, quote) if row is not None and quote else None
            if row is not None and row.reliable and span is not None:
                add(row, [(span.start, span.end)], article)
    else:
        for article in articles:
            row = structured_table_row(article)
            if row is None or not row.reliable:
                continue
            spans = [
                (span.start, span.end)
                for quote in quotes
                if quote and (span := locate_quote(article.text, quote)) is not None
            ]
            add(row, spans, article)
    # Same row id from different bodies is ambiguous; never merge or use either.
    return [
        item
        for item in merged.values()
        if len(bodies[(item.row.table_id, item.row.row_id)]) == 1
    ]


def _cell_quoted(row: StructuredTableRow, index: int, spans: list[tuple[int, int]]) -> bool:
    if index >= len(row.cell_spans):
        return False
    start, end = row.cell_spans[index]
    return any(start >= span_start and end <= span_end for span_start, span_end in spans)


def _structured_support(
    fact: Fact,
    occurrences: list[tuple[int, str | None]],
    claim_text: str,
    quote_rows: list[_StructuredQuote],
) -> bool:
    rows = [item.row for item in quote_rows]
    for position, expected_field in occurrences:
        if expected_field is None:
            return False
        bound = _bound_row_ids(rows, claim_text, position)
        if not bound:
            return False
        supported = False
        for item in quote_rows:
            row = item.row
            if row.is_matrix:
                continue
            column_index = next(
                (
                    index
                    for index, column in enumerate(row.columns)
                    if column.field == expected_field
                ),
                None,
            )
            if column_index is None:
                continue
            canonical = structured_cell_canonical(
                row.values[column_index], expected_field
            )
            if canonical != fact.canonical:
                continue
            if not _cell_quoted(row, column_index, item.spans):
                continue
            if row.row_id in bound:
                supported = True
                break
        if not supported:
            return False
    return True


def _structured_numeric_claim(quote_rows: list[_StructuredQuote], fact: Fact) -> bool:
    """Whether any quoted data cell literally contains this number+unit.

    This includes cells whose column has no recognized field (for example a
    category cell that also mentions "8学分"); that literal must not bypass the
    fact's own column semantics.
    """
    canonical = normalize_text(fact.canonical)
    if not canonical:
        return False
    for item in quote_rows:
        row = item.row
        values = row.raw_values or row.values
        for index, value in enumerate(values):
            if not _cell_quoted(row, index, item.spans):
                continue
            cell_text = normalize_text(value)
            for match in re.finditer(re.escape(canonical), cell_text):
                before = cell_text[match.start() - 1] if match.start() > 0 else ""
                if not before.isdigit():
                    return True
    return False


def _quote_is_structured(quote: str, articles: list) -> bool:
    for article in articles:
        row = structured_table_row(article)
        if row is None or not row.reliable:
            continue
        span = locate_quote(article.text, quote)
        if span is None:
            continue
        if any(start < span.end and end > span.start for start, end in row.cell_spans):
            return True
    return False


def _apply_structured_fact_support(
    result: FactCoverage, text: str, quotes: list[str], articles: list
) -> None:
    quote_rows = _structured_quote_rows(quotes, articles)
    if not quote_rows:
        return
    candidates = [fact for fact in result.claim_facts if _structured_fact_candidate(fact)]
    if not candidates:
        return
    non_structured_canonicals = {
        fact.canonical
        for quote in quotes
        if quote and not _quote_is_structured(quote, articles)
        for fact in extract_facts_detailed(quote).facts
    }
    for fact in candidates:
        occurrences = _fact_field_occurrences(fact, text)
        if not occurrences:
            continue
        supported = _structured_support(fact, occurrences, text, quote_rows)
        if fact in result.uncovered and supported:
            result.uncovered.remove(fact)
            result.covered.append(fact)
            continue
        if fact not in result.covered or supported:
            continue
        expected_fields = {
            expected_field for _, expected_field in occurrences if expected_field
        }
        if not expected_fields or fact.canonical in non_structured_canonicals:
            continue
        # Base coverage came from a structured cell literal, but the field/row
        # semantics do not match; a number in another column/row cannot prove it.
        if _structured_numeric_claim(quote_rows, fact):
            result.covered.remove(fact)
            result.uncovered.append(fact)


def _academic_year_labels(text: str) -> list[tuple[int, int, str, str]]:
    """Complete consecutive academic-year labels in normalized text.

    Returns ``(label_start, label_end, first_year, second_year)``. Incomplete or
    inverted ranges are excluded, so they keep the normal duration gate.
    """
    labels: list[tuple[int, int, str, str]] = []
    for match in _ACADEMIC_YEAR_LABEL_RE.finditer(text):
        first, second = match.group("start"), match.group("end")
        if int(second) != int(first) + 1:
            continue
        labels.append((match.start(), match.end(), first, second))
    return labels


def _academic_year_title_pairs(quotes: list[str], articles: list) -> set[tuple[str, str]]:
    """Ranges named in full by one source title that actually backs a quote."""
    pairs: set[tuple[str, str]] = set()
    for article in articles:
        if not article.doc_title:
            continue
        if not any(quote and locate_quote(article.text, quote) for quote in quotes):
            continue
        for _, _, first, second in _academic_year_labels(normalize_text(article.doc_title)):
            pairs.add((first, second))
    return pairs


def _academic_year_scope_support(
    fact: Fact,
    normalized_text: str,
    labels: list[tuple[int, int, str, str]],
    title_pairs: set[tuple[str, str]],
) -> bool:
    """A label year is scope only if every year occurrence is part of a supported label."""
    if fact.kind is not FactKind.DURATION:
        return False
    fact_match = _ACADEMIC_YEAR_FACT_RE.fullmatch(fact.canonical)
    if fact_match is None:
        return False
    year = fact_match.group("year")
    occurrences = list(re.finditer(rf"(?<!\d){re.escape(year)}(?!\d)", normalized_text))
    relevant = [
        match
        for match in occurrences
        if normalized_text.startswith("学年", match.end())
        or any(start <= match.start() and match.end() <= end for start, end, _, _ in labels)
    ]
    if not relevant:
        return False
    return all(
        any(
            start <= match.start()
            and match.end() <= end
            and year in (first, second)
            and (first, second) in title_pairs
            for start, end, first, second in labels
        )
        for match in relevant
    )


def check_evidence_fact_coverage(text: str, quotes: list[str], articles: list) -> FactCoverage:
    """Permit explicit intake years, notice labels and same-source academic years.

    Intake years are trusted scope metadata. Academic-year range labels are source
    scope, not durations, but only when one cited source title names the complete
    consecutive range. Bare years, ordinary dates, deadlines, amounts and single
    ``X学年`` durations still require body support.
    """
    result = check_fact_coverage(text, quotes)
    supported_years = {
        str(year)
        for article in articles
        if article.audience_detail
        for year in article.audience_detail.intake_years
    }
    contextual = []
    for fact in result.uncovered:
        if fact.kind is not FactKind.DATE or not re.fullmatch(r"20\d{2}", fact.canonical):
            continue
        occurrences = list(re.finditer(re.escape(fact.canonical) + r"年", text))
        if not occurrences:
            continue

        def supported_scope(match, year=fact.canonical):
            suffix = text[match.end() :]
            if suffix.startswith("入学") and year in supported_years:
                return True
            # An explicitly named notice's annual label is source scope, not a
            # publication date, event date or deadline. Require a literal title match.
            label = re.split(r"[，,。；;：:]", suffix)[0]
            if not re.search(r"奖学金|助学金|招生|推免|培养方案|选拔|评审", label):
                return False
            return any(
                (year + "年" + label[:size]) in article.doc_title
                for size in range(min(len(label), 16), 5, -1)
                for article in articles
            )

        if all(supported_scope(m) for m in occurrences):
            contextual.append(fact)
    # A numeric table cell supports credits only together with its explicitly
    # quoted header in the same document/version/table, and an identified row.
    if len(quotes) == len(articles):
        headers = {
            (a.doc_id, a.version_id, a.article_no)
            for q, a in zip(quotes, articles, strict=True)
            # 结构化行只走逐列/逐行语义校验，不能被旧扁平表规则绕过。
            if a.block_kind not in _STRUCTURED_ROW_KINDS
            and re.search(r"课程代码\s*[|｜]\s*课程名称\s*[|｜]\s*学分", q)
        }
        table_values = set()
        for quote, article in zip(quotes, articles, strict=True):
            if (article.doc_id, article.version_id, article.article_no) not in headers:
                continue
            for row in re.finditer(
                r"(?<!\d)\d{6}\s*[|｜]\s*([^|｜\n]+)[^|｜]{0,180}[|｜]\s*(\d+(?:\.\d+)?)\s*[|｜]\s*\d+(?:\.\d+)?\s*[|｜]",
                quote,
            ):
                course = row[1].strip()
                if course in text:
                    table_values.add(f"{_format_number(float(row[2]))}学分")
        contextual.extend(
            fact
            for fact in result.uncovered
            if fact.kind is FactKind.NUMBER and fact.canonical in table_values
        )
    normalized_text = normalize_text(text)
    academic_labels = _academic_year_labels(normalized_text)
    title_pairs = (
        _academic_year_title_pairs(quotes, articles) if academic_labels else set()
    )
    if title_pairs:
        contextual.extend(
            fact
            for fact in result.uncovered
            if _academic_year_scope_support(
                fact, normalized_text, academic_labels, title_pairs
            )
        )
    result.uncovered = [fact for fact in result.uncovered if fact not in contextual]
    result.covered.extend(contextual)
    _apply_structured_fact_support(result, text, quotes, articles)
    return result
