"""W3 fact 抽取、规范化与覆盖检查测试。"""

from __future__ import annotations

import pytest

from campusguide.verification.facts import (
    FactKind,
    chinese_to_int,
    extract_facts,
    int_to_chinese,
    uncovered_facts,
)


def _canonicals(text: str) -> list[str]:
    return [fact.canonical for fact in extract_facts(text)]


# ---------- 中文数字工具 ----------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("十", 10),
        ("十五", 15),
        ("二十", 20),
        ("二十五", 25),
        ("两", 2),
        ("一百", 100),
        ("一百零五", 105),
        ("八千", 8000),
        ("二〇二五", 2025),
        ("三", 3),
    ],
)
def test_chinese_to_int(text: str, expected: int) -> None:
    assert chinese_to_int(text) == expected


@pytest.mark.parametrize("text", ["", "abc", "第十", "3"])
def test_chinese_to_int_rejects_invalid(text: str) -> None:
    assert chinese_to_int(text) is None


@pytest.mark.parametrize(
    ("value", "expected"),
    [(10, "十"), (25, "二十五"), (105, "一百零五"), (8000, "八千"), (2, "二")],
)
def test_int_to_chinese(value: int, expected: str) -> None:
    assert int_to_chinese(value) == expected


# ---------- 抽取范围 ----------


def test_percent_variants() -> None:
    assert _canonicals("成绩排名前10%") == ["10%"]
    assert _canonicals("排名前百分之三十") == ["30%"]


def test_dates() -> None:
    assert _canonicals("2025年9月1日起施行") == ["2025-09-01"]
    assert _canonicals("截止2025-09-01") == ["2025-09-01"]


def test_year_ignores_version_labels() -> None:
    assert _canonicals("按2025年版执行") == []
    assert _canonicals("自2025年起") == ["2025"]


def test_durations() -> None:
    assert _canonicals("连续两年考核合格") == ["2年"]
    assert _canonicals("10个工作日内答复") == ["10工作日"]
    assert _canonicals("原则上3个月内完成") == ["3月"]


def test_amounts_and_credits() -> None:
    assert _canonicals("奖励8000元") == ["8000元"]
    assert _canonicals("修满10学分") == ["10学分"]
    assert _canonicals("完成48学时") == ["48学时"]
    assert _canonicals("三万元以内") == ["30000元"]


def test_range_expands_to_endpoints() -> None:
    assert _canonicals("选修10—28学分") == ["10学分", "28学分"]
    assert _canonicals("10至28学分") == ["10学分", "28学分"]


def test_range_with_quantifier_expands_both_endpoints() -> None:
    assert _canonicals("培养周期为3-4个学期") == ["3学期", "4学期"]
    assert uncovered_facts(extract_facts("培养周期为3-4个学期"), ["3～4个学期"]) == []


def test_structural_numbers_are_ignored() -> None:
    assert _canonicals("依据第九条、第（一）项和2025年版") == []


def test_bare_numbers_are_ignored() -> None:
    assert _canonicals("参见 123 页说明") == []


# ---------- 覆盖检查 ----------


def test_coverage_cross_forms() -> None:
    facts = extract_facts("排名前百分之三十，奖励8000元")
    assert uncovered_facts(facts, ["排名前30%，奖励8000元"]) == []
    # 捌仟无法规范化且 canonical 不匹配 -> 8000元 未覆盖
    facts = extract_facts("奖励8000元")
    assert [f.canonical for f in uncovered_facts(facts, ["奖励一万元"])] == ["8000元"]


def test_coverage_accepts_chinese_numeral_in_quote() -> None:
    facts = extract_facts("奖励8000元")
    assert uncovered_facts(facts, ["奖励八千元"]) == []


def test_coverage_accepts_quantifier_form() -> None:
    facts = extract_facts("10个工作日内")
    assert uncovered_facts(facts, ["十个工作日内答复"]) == []


def test_coverage_range_endpoints() -> None:
    facts = extract_facts("选修10至28学分")
    assert uncovered_facts(facts, ["选修10—28学分"]) == []


def test_coverage_multiple_quotes_share() -> None:
    facts = extract_facts("奖励8000元，覆盖10学分")
    assert uncovered_facts(
        facts,
        ["奖励8000元"],
    ) == [facts[1]]
    assert uncovered_facts(facts, ["奖励8000元", "覆盖10学分"]) == []


def test_coverage_percent_chinese_in_quote() -> None:
    facts = extract_facts("排名前30%")
    assert uncovered_facts(facts, ["排名位于前百分之三十"]) == []


def test_coverage_date_cross_format() -> None:
    facts = extract_facts("2025年9月1日起")
    assert uncovered_facts(facts, ["2025-09-01 实施"]) == []
    assert uncovered_facts(facts, ["2025/9/1 实施"]) == []
    assert uncovered_facts(facts, ["2025年09月01日 实施"]) == []
    assert uncovered_facts(facts, ["2025年9月1日 实施"]) == []


def test_uncovered_fact_reports_canonical() -> None:
    facts = extract_facts("奖励8000元")
    uncovered = uncovered_facts(facts, ["奖励金额另有规定"])
    assert [f.canonical for f in uncovered] == ["8000元"]
    assert uncovered[0].kind is FactKind.AMOUNT
