"""W3 Fact 层：中文数字、日期、期限、金额与 quote 覆盖。"""

from __future__ import annotations

import pytest

from campusguide.domain.enums import FactKind
from campusguide.verification.facts import (
    check_fact_coverage,
    chinese_to_int,
    extract_facts,
    extract_facts_detailed,
    facts_not_covered,
)


def _canonicals(text: str) -> set[str]:
    return {fact.canonical for fact in extract_facts(text)}


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("一", 1),
        ("两", 2),
        ("十", 10),
        ("十二", 12),
        ("二十", 20),
        ("一百二十三", 123),
        ("两千零二十五", 2025),
        ("二〇二五", 2025),
        ("十万", 100000),
        ("一百万", 1000000),
        ("一亿", 100000000),
    ],
)
def test_chinese_to_int(raw: str, expected: int) -> None:
    assert chinese_to_int(raw) == expected


def test_chinese_to_int_refuses_unknown_characters() -> None:
    assert chinese_to_int("廿五") is None
    assert chinese_to_int("") is None


@pytest.mark.parametrize(
    "raw",
    ["2025年9月1日", "2025-09-01", "2025/09/01", "二〇二五年九月一日"],
)
def test_date_forms_share_canonical(raw: str) -> None:
    facts = extract_facts(raw)
    assert "2025-09-01" in {fact.canonical for fact in facts}
    assert "2025" in {fact.canonical for fact in facts}
    assert all(fact.kind is FactKind.DATE for fact in facts)


def test_year_and_version_labels() -> None:
    assert "2025" in _canonicals("2025年")
    assert _canonicals("2025年版") == set()
    assert _canonicals("v2025") == set()
    assert _canonicals("2025 年版") == set()


def test_percent_forms_share_canonical() -> None:
    assert "10%" in _canonicals("10%")
    assert "10%" in _canonicals("百分之十")
    assert "30%" in _canonicals("前30%")
    assert "10.5%" in _canonicals("10.5%")


@pytest.mark.parametrize(
    ("raw", "canonical"),
    [
        ("连续2年", "2年"),
        ("连续两年", "2年"),
        ("10个工作日", "10工作日"),
        ("十个工作日", "10工作日"),
        ("3个月", "3月"),
        ("三个月", "3月"),
        ("3-4个学期", "3学期"),
        ("3-4个学期", "4学期"),
        ("10—28学分", "10学分"),
        ("10—28学分", "28学分"),
        ("10至28学分", "10学分"),
        ("10到28学分", "28学分"),
    ],
)
def test_duration_and_ranges(raw: str, canonical: str) -> None:
    assert canonical in _canonicals(raw)


@pytest.mark.parametrize(
    ("raw", "canonical"),
    [
        ("奖励2万元", "20000元"),
        ("奖励两万元", "20000元"),
        ("奖励十万元", "100000元"),
        ("奖励100000元", "100000元"),
        ("8000元", "8000元"),
        ("10学分", "10学分"),
        ("十个学分", "10学分"),
        ("10学时", "10学时"),
    ],
)
def test_amount_forms(raw: str, canonical: str) -> None:
    assert canonical in _canonicals(raw)


def test_ignores_non_business_numbers_and_labels() -> None:
    for raw in ("第10条", "第二章", "第X部分", "（一）", "10", "2025版", "v2025"):
        assert _canonicals(raw) == set(), raw


def test_full_date_does_not_emit_month_or_day_durations() -> None:
    canonicals = _canonicals("2025年9月1日")
    assert "9月" not in canonicals
    assert "1月" not in canonicals
    assert "1日" not in canonicals


def test_partial_year_month_does_not_emit_month_duration() -> None:
    canonicals = _canonicals("2025年9月")
    assert "2025" in canonicals
    assert "9月" not in canonicals


def test_facts_are_deduplicated() -> None:
    facts = extract_facts("10学分、10学分、10学分")
    assert [fact.canonical for fact in facts].count("10学分") == 1


@pytest.mark.parametrize(
    ("claim", "quotes"),
    [
        ("连续两年", ["连续2年"]),
        ("奖励两万元", ["奖励20000元"]),
        ("2025年9月1日", ["二〇二五年九月一日"]),
        ("10个工作日", ["十个工作日"]),
        ("2025年9月1日", ["2025-09-01"]),
        ("2025年9月1日", ["2025/09/01"]),
        ("十万元", ["100000元"]),
        ("30%", ["前百分之三十"]),
        ("10学分", ["10—28学分"]),
        ("2025年", ["2025年9月1日"]),
        ("奖励两万元", ["奖励标准", "两万元"]),
    ],
)
def test_fact_coverage_accepts_equivalent_forms(claim: str, quotes: list[str]) -> None:
    coverage = check_fact_coverage(claim, quotes)
    assert coverage.uncovered == [], coverage
    assert facts_not_covered(claim, quotes) == []


def test_fact_coverage_rejects_missing_or_different_values() -> None:
    coverage = check_fact_coverage("奖励20000元", ["奖励10000元"])
    assert [fact.canonical for fact in coverage.uncovered] == ["20000元"]

    coverage = check_fact_coverage("2025年9月2日", ["2025年9月1日"])
    assert [fact.canonical for fact in coverage.uncovered] == ["2025-09-02"]

    coverage = check_fact_coverage("10学分", ["10学时"])
    assert [fact.canonical for fact in coverage.uncovered] == ["10学分"]


def test_claim_without_facts_is_always_covered() -> None:
    coverage = check_fact_coverage("申请奖学金需要成绩优异。", ["学习成绩优异"])
    assert coverage.claim_facts == []
    assert coverage.uncovered == []


def test_unparseable_chinese_number_is_recorded_not_guessed() -> None:
    extraction = extract_facts_detailed("连续廿五年")
    assert extraction.facts == []
    assert extraction.unparsed == ["廿五年"]

    coverage = check_fact_coverage("连续廿五年", ["连续25年"])
    assert coverage.uncovered == []
    assert coverage.unparsed == ["廿五年"]
