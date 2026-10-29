"""R5 aggregate-level guard: a module subtotal must not become the total."""

from __future__ import annotations

from campusguide.verification.aggregates import check_aggregate_level, module_totals, stated_total
from campusguide.verification.conditions import check_conditions

QUOTE = (
    "学生毕业应取得总学分为163学分，其中必修课程83.5学分，选修课程37学分，"
    "实践课程42.5学分。"
)


def test_module_totals_are_read_separately_from_the_total() -> None:
    assert stated_total(QUOTE) == 163.0
    assert module_totals(QUOTE) == {
        "必修课程": 83.5,
        "选修课程": 37.0,
        "实践课程": 42.5,
    }


def test_module_subtotal_as_total_is_flagged() -> None:
    findings = check_aggregate_level("计算机科学与技术专业毕业总学分为37学分。", [QUOTE])
    assert [item.kind for item in findings] == ["aggregate_level"]
    assert "选修课程" in findings[0].detail and "163" in findings[0].detail


def test_correct_total_passes() -> None:
    assert check_aggregate_level("计算机科学与技术专业毕业总学分为163学分。", [QUOTE]) == []


def test_module_statement_is_not_a_total_claim() -> None:
    assert check_aggregate_level("选修课程为37学分。", [QUOTE]) == []


def test_condition_check_surfaces_the_level_error() -> None:
    findings = check_conditions("2022版培养方案毕业总学分为37学分。", [QUOTE])
    assert [finding.kind for finding in findings] == ["aggregate_level"]
    assert findings[0].blocking is True