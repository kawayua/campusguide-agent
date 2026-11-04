"""Scoped bounds and explicit quantitative asks (synthetic only)."""

from __future__ import annotations

import json
from datetime import date

from campusguide.agents.coverage import (
    CoverageAudit,
    CoverageChecker,
    _property_supported,
    _requested_properties,
)
from campusguide.agents.plan import QuestionUnit
from campusguide.agents.providers import FakeChatProvider
from campusguide.domain.models import Citation, Claim

SUBJECT = "示例专业"
TITLE = "示例专业管理办法"


def _claim(claim_id: str, text: str, *, title: str = TITLE) -> Claim:
    return Claim(
        claim_id=claim_id,
        text=text,
        citations=[
            Citation(
                article_id=f"{title}#1",
                quote=text,
                quote_start=0,
                quote_end=len(text),
                doc_title=title,
                article_no="第一条",
                effective_from=date(2026, 1, 1),
                source_url="https://example.edu.cn/x",
            )
        ],
    )


def _unit(
    question: str,
    aspects: list[str],
    *,
    subject: str = SUBJECT,
) -> QuestionUnit:
    return QuestionUnit(
        unit_id="u1",
        original_span=question,
        subject=subject,
        objects=[],
        requested_aspects=aspects,
    )


def _audit(
    unit: QuestionUnit,
    claims: list[Claim],
    *,
    status: str,
    claim_ids: list[str] | None = None,
) -> CoverageAudit:
    verdict = {
        "units": [
            {
                "unit_id": unit.unit_id,
                "status": status,
                "claim_ids": (
                    [claim.claim_id for claim in claims]
                    if claim_ids is None
                    else claim_ids
                ),
                "missing_items": [],
            }
        ],
        "unanswered_questions": [],
    }
    provider = FakeChatProvider([json.dumps(verdict, ensure_ascii=False)])
    return CoverageChecker(provider).check(
        unit.original_span, [unit], claims, [], []
    )


# ------------------------------------------------- ratio baseline and grade
_RATIO_QUESTION = (
    "各专业接收学生人数，相对一年级学生人数的原则下限和上限分别是多少？"
)
_RATIO_CLAIM = (
    "各专业接收其他专业转入的学生人数原则上不少于本专业一年级学生人数的7%，"
    "最多不超过17%。"
)


def _ratio_bounds():
    return [
        prop
        for prop in _requested_properties(_RATIO_QUESTION)
        if prop.kind == "bound"
    ]


def test_relative_grade_ratio_bounds_are_scoped_and_answered() -> None:
    bounds = _ratio_bounds()
    assert sorted(prop.direction for prop in bounds) == ["max", "min"]
    assert all(prop.measure == "比例" for prop in bounds)
    assert all(_property_supported(prop, _RATIO_CLAIM) for prop in bounds)


def test_ratio_grade_is_not_a_time_unit_and_other_grades_do_not_answer() -> None:
    wrong_grade = (
        "各专业接收其他专业转入的学生人数原则上不少于本专业二年级学生人数的7%，"
        "最多不超过17%。"
    )
    assert not any(
        _property_supported(prop, wrong_grade) for prop in _ratio_bounds()
    )


def test_ratio_grades_do_not_mix_across_clauses() -> None:
    mixed = (
        "各专业接收一年级学生人数的7%为下限，"
        "二年级接收人数最多不超过17%。"
    )
    bounds = {prop.direction: prop for prop in _ratio_bounds()}
    assert _property_supported(bounds["min"], mixed)
    assert not _property_supported(bounds["max"], mixed)


# ------------------------------------------- three scoped course-bound limits
_COURSE_QUESTION = (
    "本科生一学期含重修一般至少和至多选多少学分？修读辅修专业的上限呢？"
)
_COURSE_CLAIM = (
    "本科学生每学期所选课程（含重修）总学分一般最低不得少于11学分，"
    "上限为29学分；修读辅修专业的学生，每学期修读总学分上限为41学分。"
)


def _course_bounds():
    return [
        prop
        for prop in _requested_properties(_COURSE_QUESTION)
        if prop.kind == "bound"
    ]


def test_course_bounds_cover_ordinary_min_max_and_minor_max() -> None:
    bounds = _course_bounds()
    assert len(bounds) == 3
    assert all(prop.measure == "学分" for prop in bounds)
    assert sorted(prop.direction for prop in bounds) == ["max", "max", "min"]
    ordinary_max = [
        prop
        for prop in bounds
        if prop.direction == "max" and "辅修" not in prop.core
    ]
    minor_max = [prop for prop in bounds if "辅修" in prop.core]
    assert len(ordinary_max) == 1 and len(minor_max) == 1
    assert all(_property_supported(prop, _COURSE_CLAIM) for prop in bounds)


def test_minor_limit_cannot_fill_the_ordinary_maximum() -> None:
    bounds = _course_bounds()
    ordinary_max = next(
        prop
        for prop in bounds
        if prop.direction == "max" and "辅修" not in prop.core
    )
    minor_max = next(prop for prop in bounds if "辅修" in prop.core)

    minor_only = (
        "修读辅修专业的学生，每学期修读总学分上限为41学分。"
    )
    assert _property_supported(minor_max, minor_only)
    assert not _property_supported(ordinary_max, minor_only)

    ordinary_min_only = (
        "本科学生每学期所选课程（含重修）总学分一般最低不得少于11学分；"
        "修读辅修专业的学生，每学期修读总学分上限为41学分。"
    )
    assert not _property_supported(ordinary_max, ordinary_min_only)
    assert _property_supported(minor_max, ordinary_min_only)


def test_ordinary_minimum_still_requires_the_ordinary_scope() -> None:
    bounds = _course_bounds()
    ordinary_min = next(
        prop
        for prop in bounds
        if prop.direction == "min" and "辅修" not in prop.core
    )
    assert _property_supported(
        ordinary_min,
        "本科学生每学期所选课程（含重修）总学分一般最低不得少于11学分。",
    )
    assert not _property_supported(
        ordinary_min,
        "修读辅修专业的学生，每学期修读总学分上限为41学分。",
    )


# ------------------------------------ explicit quantities independent of aspect
_QUANTITY_QUESTION = f"{SUBJECT}专业实践何时完成，计多少学分？"


def _quantity_property():
    properties = _requested_properties(_QUANTITY_QUESTION, (SUBJECT,))
    numeric = [prop for prop in properties if prop.kind == "numeric"]
    assert len(numeric) == 1
    return numeric[0]


def test_explicit_quantity_property_is_created_next_to_timing() -> None:
    properties = _requested_properties(_QUANTITY_QUESTION, (SUBJECT,))
    assert "timing" in {prop.kind for prop in properties}
    numeric = _quantity_property()
    assert numeric.measure == "学分"
    assert "专业实践" in numeric.core


def test_quantity_accepts_the_credit_in_the_final_claim_body() -> None:
    numeric = _quantity_property()
    assert _property_supported(
        numeric, f"{SUBJECT}专业实践在第五学期结束前完成，计6学分。"
    )
    assert not _property_supported(
        numeric, f"{SUBJECT}专业实践在第五学期结束前完成。"
    )
    assert not _property_supported(
        numeric, f"{SUBJECT}理论课程共计24学分。"
    )


def test_timing_aspect_cannot_hide_a_missing_credit_quantity() -> None:
    unit = _unit(_QUANTITY_QUESTION, ["时间安排"])
    timing_only = _claim(
        "c1", f"{SUBJECT}专业实践在第五学期结束前完成。"
    )
    audit = _audit(
        unit, [timing_only], status="answered", claim_ids=["c1"]
    )
    assert audit.units[0]["status"] == "generation_missing"
    assert any("学分" in item for item in audit.missing)

    complete = _claim(
        "c1", f"{SUBJECT}专业实践在第五学期结束前完成，计6学分。"
    )
    recovered = _audit(unit, [complete], status="generation_missing")
    assert recovered.units[0]["status"] == "answered"
    assert recovered.units[0]["claim_ids"] == ["c1"]
    assert recovered.missing == []


def test_citation_title_cannot_supply_the_missing_quantity() -> None:
    unit = _unit(_QUANTITY_QUESTION, ["时间安排"])
    claim = _claim(
        "c1",
        f"{SUBJECT}专业实践已完成。",
        title=f"{SUBJECT}专业实践计6学分说明",
    )
    audit = _audit(unit, [claim], status="answered", claim_ids=["c1"])
    assert audit.units[0]["status"] == "generation_missing"
    assert any("学分" in item for item in audit.missing)


# ------------------------------------------------------ other explicit quantities
def test_copy_count_accepts_chinese_and_financial_numerals() -> None:
    question = f"{SUBJECT}转学证明材料一式几份？"
    numeric = [
        prop
        for prop in _requested_properties(question, (SUBJECT,))
        if prop.kind == "numeric"
    ]
    assert len(numeric) == 1 and numeric[0].measure == "份"
    assert _property_supported(numeric[0], "转学证明材料一式肆份。")
    assert _property_supported(numeric[0], "转学证明材料一式四份。")
    assert not _property_supported(numeric[0], "转学申请表一式肆份。")


def test_people_count_accepts_ren_and_ming_but_not_another_person() -> None:
    question = f"{SUBJECT}接收学生人数多少？"
    numeric = [
        prop
        for prop in _requested_properties(question, (SUBJECT,))
        if prop.kind == "numeric"
    ]
    assert len(numeric) == 1 and numeric[0].measure == "人"
    assert _property_supported(numeric[0], f"{SUBJECT}接收学生120人。")
    assert _property_supported(numeric[0], f"{SUBJECT}接收学生120名。")
    assert not _property_supported(numeric[0], f"{SUBJECT}接收教师120人。")


# ---------------------------------------------------------- workday day counts
def test_generic_day_question_accepts_workdays() -> None:
    duration = [
        prop
        for prop in _requested_properties("示例事项复核一般需要几天？")
        if prop.kind == "duration"
    ]
    assert len(duration) == 1
    assert _property_supported(duration[0], "示例事项复核一般3个工作日。")


def test_explicit_workday_question_rejects_calendar_days() -> None:
    duration = [
        prop
        for prop in _requested_properties("示例事项复核一般需要几个工作日？")
        if prop.kind == "duration"
    ]
    assert len(duration) == 1
    assert _property_supported(duration[0], "示例事项复核一般3个工作日。")
    assert not _property_supported(duration[0], "示例事项复核一般3个自然日。")


# ------------------------------------------- effect scaffolding and level scope
def test_credit_warning_does_not_require_an_unstated_level() -> None:
    question = "本科课程累计不合格多少学分会收到红色预警？"
    numeric = [
        prop for prop in _requested_properties(question) if prop.kind == "numeric"
    ]
    assert len(numeric) == 1 and numeric[0].measure == "学分"
    assert _property_supported(
        numeric[0],
        "学生所修课程考核不合格累计达到或超过25学分的，给予红色预警。",
    )
    assert not _property_supported(
        numeric[0],
        "学生所修课程考核不合格累计达到或超过25学分的，给予橙色预警。",
    )
    assert not _property_supported(
        numeric[0],
        "研究生所修课程考核不合格累计达到或超过25学分的，给予红色预警。",
    )


def test_stop_open_people_count_keeps_its_object_and_action() -> None:
    question = "选课人数不足多少人的课程原则上停开，如果仍要开课需谁批准？"
    numeric = [
        prop for prop in _requested_properties(question) if prop.kind == "numeric"
    ]
    assert len(numeric) == 1 and numeric[0].measure == "人"
    assert "人数" in numeric[0].core and "停开" in numeric[0].core
    assert _property_supported(
        numeric[0],
        "每门课程修读人数原则上不得少于15人，当选课人数少于15人时该课程停开；"
        "如选课人数少于15人而又需要开课的，须报教务处批准。",
    )
    assert not _property_supported(
        numeric[0], "每门课程修读人数原则上不得少于15人。"
    )


# ------------------------------------- coordinated counts stay field-local
_MICRO_QUESTION = "微专业通常包含多少门课、总共多少学分、培养周期几个学期？"
_MICRO_CLAIM = (
    "微专业一般由5～6门课程组成，总学分10-12学分，培养周期为3-4个学期。"
)


def _micro_counts(question: str = _MICRO_QUESTION, protected: tuple[str, ...] = ()):
    return {
        prop.measure: prop
        for prop in _requested_properties(question, protected)
        if prop.kind == "numeric"
    }


def test_coordinated_counts_bind_each_field_to_the_shared_program() -> None:
    counts = _micro_counts()
    assert len(counts) == 3
    assert sorted(counts) == ["学分", "学期", "门"]
    assert all(_property_supported(prop, _MICRO_CLAIM) for prop in counts.values())

    assert not _property_supported(
        counts["门"],
        "微专业一般由5～6个学分组成，总学分10-12学分，培养周期为3-4个学期。",
    )
    assert not _property_supported(
        counts["学分"],
        "微专业一般由5～6门课程组成，培养周期为3-4个学期。",
    )
    assert not _property_supported(
        counts["学期"],
        "微专业一般由5～6门课程组成，总学分10-12学分，培养周期为3-4个学年。",
    )
    assert not _property_supported(
        counts["学分"],
        "其他专业一般由5～6门课程组成，总学分10-12学分，培养周期为3-4个学期。",
    )


def test_coordinated_counts_keep_a_protected_scope_atomic() -> None:
    name = "微电子与固体电子学"
    question = f"{name}通常包含多少门课、总共多少学分、培养周期几个学期？"
    claim = f"{name}一般由5～6门课程组成，总学分10-12学分，培养周期为3-4个学期。"
    counts = [
        prop
        for prop in _requested_properties(question, (name,))
        if prop.kind == "numeric"
    ]
    assert len(counts) == 3
    assert all(_property_supported(prop, claim) for prop in counts)
    # Identity may be represented by the unit gate or atomic property anchors;
    # test the public audit behavior instead of the private core string shape.
    unit = _unit(question, [question], subject=name)
    complete = _audit(unit, [_claim("c1", claim)], status="answered")
    assert not complete.missing
    wrong_scope = _audit(
        unit,
        [_claim("c2", claim.replace(name, "另一专业"))],
        status="answered",
    )
    assert wrong_scope.missing
