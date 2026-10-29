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


def check_evidence_fact_coverage(text: str, quotes: list[str], articles: list) -> FactCoverage:
    """Permit only explicit intake-year metadata, never publication/deadline facts.

    Intake years are trusted scope metadata. A bare year or year in any other role
    still requires body support. Amounts, durations, courses and roles are unchanged.
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
            if re.search(r"课程代码\s*[|｜]\s*课程名称\s*[|｜]\s*学分", q)
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
    result.uncovered = [fact for fact in result.uncovered if fact not in contextual]
    result.covered.extend(contextual)
    return result
