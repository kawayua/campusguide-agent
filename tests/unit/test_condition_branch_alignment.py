"""Branch-scoped threshold alignment (false-refusal regression).

A multi-branch rule must compare each claimed threshold with the branch that
carries the same actor/department binding. Before this guard, every pair sharing
an anchor and an action was compared, so a correct multi-branch claim was called
contradictory when its own value was compared with the other branch's value.

All fixtures are synthetic; no corpus article, role, ID or question is reused.
"""

from __future__ import annotations

import pytest

from campusguide.verification.conditions import (
    check_conditions,
    compare_thresholds,
    extract_thresholds,
)

SOURCE = "学生请假4天以内由甲部门审批；请假20天以上由丙部门审批。"
APPOSITIVE_SOURCE = "学生请假2天以内由甲部门审批；请假12天以上（含12天），由丙部门审批。"


def _kinds(claim: str, quote: str = SOURCE) -> list[str]:
    return [finding.kind for finding in compare_thresholds(claim, [quote])]


def test_identical_multibranch_source_has_no_findings() -> None:
    assert compare_thresholds(SOURCE, [SOURCE]) == []
    assert check_conditions(SOURCE, [SOURCE]) == []


@pytest.mark.parametrize(
    "claim",
    [
        # Punctuation inserted around the binding.
        "学生请假4天以内，由甲部门审批；请假20天以上，由丙部门审批。",
        # Binding verb paraphrased, same departments and branches.
        "学生请假4天以内由甲部门负责审批；请假20天以上由丙部门负责审批。",
        # Branches restated in the opposite order.
        "请假20天以上由丙部门审批；学生请假4天以内由甲部门审批。",
    ],
)
def test_correct_branches_with_punctuation_or_reordering_are_accepted(
    claim: str,
) -> None:
    assert compare_thresholds(claim, [SOURCE]) == []


def test_swapped_actor_bindings_are_contradicted() -> None:
    claim = "学生请假4天以内由丙部门审批；请假20天以上由甲部门审批。"
    assert _kinds(claim) == ["threshold_value", "threshold_value"]


def test_swapped_branch_values_are_contradicted() -> None:
    quote = "学生请假4天以内由甲部门审批；学生请假20天以内由丙部门审批。"
    claim = "学生请假20天以内由甲部门审批；学生请假4天以内由丙部门审批。"
    assert _kinds(claim, quote) == ["threshold_value", "threshold_value"]


def test_changed_value_in_one_branch_is_contradicted() -> None:
    claim = "学生请假5天以内由甲部门审批；请假20天以上由丙部门审批。"
    assert _kinds(claim) == ["threshold_value"]


def test_single_branch_value_guard_still_fires() -> None:
    quote = "学生请假4天以内由甲部门审批。"
    claim = "学生请假5天以内由甲部门审批。"
    assert _kinds(claim, quote) == ["threshold_value"]


def test_same_value_with_opposite_directions_is_contradicted() -> None:
    quote = "学生请假4天以下由甲部门审批；请假20天以上由丙部门审批。"
    claim = "学生请假4天以上由甲部门审批；请假20天以上由丙部门审批。"
    assert _kinds(claim, quote) == ["threshold_direction"]


def test_exam_after_versus_within_guard_still_fires() -> None:
    quote = (
        "开考30分钟后，监考员禁止迟到的考生入场。"
        "开考后30分钟内，一律不允许考生离开考场。"
    )
    claim = "开考后30分钟内，监考员禁止迟到的考生入场。"
    assert _kinds(claim, quote) == ["threshold_direction"]


def test_irrelevant_branches_cannot_contradict_or_lend_support() -> None:
    # A third department has no source branch: neither leave branch may be used
    # as its aligned counterpart.
    elsewhere = "学生请假4天以内由乙部门审批；请假20天以上由丙部门审批。"
    assert compare_thresholds(elsewhere, [SOURCE]) == []

    # A five-day repair rule must not vouch for a wrong five-day leave value.
    unrelated_source = SOURCE + "宿舍报修5天内由后勤部门处理。"
    claim = "学生请假5天以内由甲部门审批。"
    assert _kinds(claim, unrelated_source) == ["threshold_value"]


def test_parenthetical_inclusive_repeat_keeps_explicit_actor() -> None:
    parsed = [
        (item.anchor, item.value, item.relation, item.binding)
        for item in extract_thresholds(APPOSITIVE_SOURCE)
    ]
    assert parsed == [
        ("学生请假", 2.0, "within", "甲部门"),
        ("请假", 12.0, "at_least", "丙部门"),
    ]
    assert compare_thresholds(APPOSITIVE_SOURCE, [APPOSITIVE_SOURCE]) == []
    assert check_conditions(APPOSITIVE_SOURCE, [APPOSITIVE_SOURCE]) == []

    # A following numeric range is a real branch boundary, not part of the actor.
    ranged = "学生请假12天以上（含12天），由丙部门审批，20至30天由丁部门审批。"
    first = extract_thresholds(ranged)[0]
    assert first.binding == "丙部门"
    assert "丁部门" not in first.context


def test_parenthetical_inclusive_swapped_actors_are_blocking() -> None:
    claim = "学生请假2天以内由丙部门审批；请假12天以上（含12天），由甲部门审批。"
    findings = compare_thresholds(claim, [APPOSITIVE_SOURCE])
    assert [item.kind for item in findings] == ["threshold_value", "threshold_value"]
    assert all(item.blocking for item in findings)


def test_parenthetical_inclusive_swapped_values_are_blocking() -> None:
    quote = "学生请假2天以内由甲部门审批；学生请假12天以内（含12天），由丙部门审批。"
    claim = "学生请假12天以内由甲部门审批；学生请假2天以内（含2天），由丙部门审批。"
    assert compare_thresholds(quote, [quote]) == []
    findings = compare_thresholds(claim, [quote])
    assert [item.kind for item in findings] == ["threshold_value", "threshold_value"]
    assert all(item.blocking for item in findings)


def test_same_actor_thresholds_do_not_cross_contradict() -> None:
    quote = "学生请假3天以内由教务处审批；学生请假10天以上由教务处审批。"
    assert compare_thresholds(quote, [quote]) == []
    assert check_conditions(quote, [quote]) == []
    assert compare_thresholds("学生请假3天以内由教务处审批。", [quote]) == []
    assert compare_thresholds("学生请假10天以上由教务处审批。", [quote]) == []
