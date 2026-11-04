from __future__ import annotations

from campusguide.agents.coverage import (
    _duration_claim_segments,
    _property_supported,
    _requested_properties,
)

_DEADLINE_QUESTION = "名单书面告知开学几周内处理，汇总表又几周内备案"
_DEADLINE_CLAIM = (
    "每学期开学2周内统计名单、书面告知家长；"
    "每学期开学3周内汇总表报学籍科备案"
)

_DURATION_QUESTION = "本科生休学一般多久一期，累计最多多久，休学创业最多多久"
_DURATION_CLAIM = "学生休学一般以3年为期，累计不超过5年（休学创业不超过7年）"

_ACTUAL_SHAPED_QUESTION = (
    "示例微专业评优预警名单和书面告知应在开学几周内处理，"
    "评优预警汇总表又应在几周内备案？"
)
_ACTUAL_SHAPED_CLAIM = (
    "每学期开学4周内，示例学院分类统计符合条件的示例微专业评优预警名单，"
    "核对无误后书面告知本人；"
    "每学期开学6周内，示例学院将评优预警汇总表报学生科备案。"
)


def _durations(question: str):
    return [
        prop
        for prop in _requested_properties(question)
        if prop.kind == "duration"
    ]


def _support(question: str, claim: str) -> list[bool]:
    return [_property_supported(prop, claim) for prop in _durations(question)]


def test_shared_event_anchor_keeps_both_business_bindings() -> None:
    props = _durations(_DEADLINE_QUESTION)

    assert [prop.core for prop in props] == ["开学", "汇总表"]
    assert [prop.business for prop in props] == [("名单", "告知"), ("汇总表",)]
    assert [prop.action for prop in props] == ["处理", "备案"]
    assert all(_property_supported(prop, _DEADLINE_CLAIM) for prop in props)


def test_actual_shaped_question_binds_anchor_and_business_separately() -> None:
    props = _durations(_ACTUAL_SHAPED_QUESTION)

    assert [prop.core for prop in props] == ["开学", "评优预警汇总表"]
    assert [prop.business for prop in props] == [("名单", "告知"), ("汇总表",)]
    assert [prop.action for prop in props] == ["处理", "备案"]
    assert all(_property_supported(prop, _ACTUAL_SHAPED_CLAIM) for prop in props)


def test_actual_shaped_swapped_objects_and_changed_action_are_negatives() -> None:
    first, second = _durations(_ACTUAL_SHAPED_QUESTION)
    swapped = (
        "每学期开学4周内，示例学院分类统计评优预警汇总表，核对无误后书面告知本人；"
        "每学期开学6周内，示例学院将评优预警名单报学生科备案。"
    )

    assert not _property_supported(first, swapped)
    assert not _property_supported(second, swapped)
    assert _property_supported(first, _ACTUAL_SHAPED_CLAIM)
    assert not _property_supported(second, _ACTUAL_SHAPED_CLAIM.replace("备案", "公布"))


def test_later_duration_branch_does_not_borrow_the_previous_object() -> None:
    first, second = _durations(_ACTUAL_SHAPED_QUESTION)
    claim = (
        "每学期开学4周内，示例学院分类统计评优预警名单并书面告知本人；"
        "每学期开学6周内，报学生科备案。"
    )

    assert _property_supported(first, claim)
    assert not _property_supported(second, claim)


def test_changed_filing_action_stays_missing() -> None:
    first, second = _durations(_DEADLINE_QUESTION)
    changed = _DEADLINE_CLAIM.replace("备案", "公布")

    assert _property_supported(first, changed)
    assert not _property_supported(second, changed)


def test_processing_accepts_verified_equivalent_verbs() -> None:
    first, second = _durations(_DEADLINE_QUESTION)

    for action in ("统计名单", "书面告知名单", "给予名单书面预警"):
        claim = (
            f"每学期开学2周内{action}；"
            "每学期开学3周内汇总表报学籍科备案"
        )
        assert _property_supported(first, claim)
        assert _property_supported(second, claim)


def test_same_deadline_cannot_lend_the_other_object() -> None:
    swapped = (
        "每学期开学2周内统计汇总表、书面告知家长；"
        "每学期开学3周内名单报学籍科备案"
    )

    assert _support(_DEADLINE_QUESTION, swapped) == [False, False]


def test_generic_student_prefix_and_parenthetical_startup_branch() -> None:
    assert _support(_DURATION_QUESTION, _DURATION_CLAIM) == [True, True, True]


def test_missing_term_branch_is_a_gap() -> None:
    claim = "累计不超过5年（休学创业不超过7年）"

    assert _support(_DURATION_QUESTION, claim) == [False, True, True]


def test_missing_cumulative_branch_is_a_gap() -> None:
    claim = "学生休学一般以3年为期，休学创业不超过7年"

    assert _support(_DURATION_QUESTION, claim) == [True, False, True]


def test_missing_startup_branch_is_a_gap() -> None:
    claim = "学生休学一般以3年为期，累计不超过5年"

    assert _support(_DURATION_QUESTION, claim) == [True, True, False]


def test_branch_values_do_not_cross_fill() -> None:
    only_cumulative = "学生休学一般以3年为期，累计不超过5年"
    only_startup = "学生休学一般以3年为期，休学创业不超过7年"

    assert not _support(_DURATION_QUESTION, only_cumulative)[2]
    assert not _support(_DURATION_QUESTION, only_startup)[1]


def test_parenthetical_split_requires_an_independent_branch() -> None:
    assert _duration_claim_segments("累计不超过5年（休学创业不超过7年）") == [
        "累计不超过5年",
        "休学创业不超过7年",
    ]
    assert _duration_claim_segments("累计不超过5年（含3天）") == [
        "累计不超过5年（含3天）"
    ]
    assert _duration_claim_segments("累计不超过5年（3天）") == [
        "累计不超过5年（3天）"
    ]
