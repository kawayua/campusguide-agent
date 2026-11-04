"""Regressions for numeric identity, explicit semester choice and bound echoes."""

from __future__ import annotations

import json
from datetime import date
from types import SimpleNamespace

from campusguide.agents.coverage import (
    CoverageChecker,
    _clause_is_property_echo,
    _is_topic_part,
    _Property,
    _property_supported,
    _question_clauses,
    _requested_properties,
)
from campusguide.agents.plan import QuestionUnit
from campusguide.agents.question_units import requested_aspects
from campusguide.domain.models import Citation, Claim

_COURSE_QUESTION = (
    "示例技术2024—2025级课程12345678901234高级检索是几学分、"
    "春季还是秋季开，必修还是选修？"
)
_COURSE_CLAIM = (
    "示例技术2024—2025级课程12345678901234高级检索是3学分、"
    "秋季开课，属于选修。"
)


def _claim(text: str) -> Claim:
    return Claim(
        claim_id="c1",
        text=text,
        citations=[
            Citation(
                article_id="a1",
                quote=text,
                quote_start=0,
                quote_end=len(text),
                doc_title="学生手册",
                article_no="第一条",
                effective_from=date(2025, 9, 1),
                source_url="https://example.edu/rule",
            )
        ],
    )


def _unit(question: str, aspects: list[str] | None = None) -> QuestionUnit:
    return QuestionUnit(
        unit_id="u1",
        original_span=question,
        requested_aspects=aspects or [question.strip("？?。 ")],
    )


class _AnsweredProvider:
    def complete(self, messages, temperature=0, max_tokens=2400):
        payload = {
            "units": [
                {
                    "unit_id": "u1",
                    "status": "answered",
                    "claim_ids": ["c1"],
                    "missing_items": [],
                }
            ],
            "unanswered_questions": [],
        }
        return SimpleNamespace(
            content=json.dumps(payload, ensure_ascii=False),
            prompt_tokens=0,
            completion_tokens=0,
            latency_ms=0,
        )


def _audit(question: str, claim_text: str, aspects: list[str] | None = None):
    checker = CoverageChecker(_AnsweredProvider())
    return checker.check(question, [_unit(question, aspects)], [_claim(claim_text)], [], [])


def test_course_code_is_the_numeric_identity_not_the_whole_prefix() -> None:
    audit = _audit(_COURSE_QUESTION, _COURSE_CLAIM)
    assert audit.units[0]["status"] == "answered"

    numeric = [
        prop for prop in _requested_properties(_COURSE_QUESTION) if prop.kind == "numeric"
    ]
    assert numeric
    assert "12345678901234" in numeric[0].required
    assert not any("2024" in term for term in numeric[0].required)


def test_another_course_code_cannot_answer_with_same_credits_and_year() -> None:
    wrong = _COURSE_CLAIM.replace("12345678901234", "98765432109876")
    audit = _audit(_COURSE_QUESTION, wrong)
    assert audit.units[0]["status"] != "answered"


def test_course_code_binding_does_not_require_the_title_to_repeat() -> None:
    short = "课程12345678901234为3学分、秋季开课，属于选修。"
    audit = _audit(_COURSE_QUESTION, short)
    assert audit.units[0]["status"] == "answered"


def test_unknown_program_keeps_atomic_entity_anchor_across_year() -> None:
    question = "示例技术2024—2025级高级检索是几学分？"
    assert _audit(question, "示例技术2024—2025级高级检索是3学分。").units[0][
        "status"
    ] == "answered"
    wrong = "其他技术2024—2025级高级检索是3学分。"
    assert _audit(question, wrong).units[0]["status"] != "answered"


def test_explicit_semester_choice_adds_asked_aspect() -> None:
    aspects = requested_aspects(_COURSE_QUESTION)
    assert "开课学期" in aspects
    assert "选修课程" in aspects
    assert "开课学期" not in requested_aspects("秋季课程的学分是多少？")


def test_semester_property_requires_a_term_and_course_binding() -> None:
    semester = [
        prop for prop in _requested_properties(_COURSE_QUESTION) if prop.kind == "semester"
    ]
    assert semester
    assert "12345678901234" in semester[0].required
    assert _property_supported(semester[0], _COURSE_CLAIM)

    missing_term = "示例技术2024—2025级课程12345678901234高级检索是3学分，属于选修。"
    assert not _property_supported(semester[0], missing_term)
    wrong_course = _COURSE_CLAIM.replace("12345678901234", "98765432109876")
    assert not _property_supported(semester[0], wrong_course)


def test_omitted_semester_cannot_pass_the_unit() -> None:
    missing_term = "示例技术2024—2025级课程12345678901234高级检索是3学分，属于选修。"
    audit = _audit(
        _COURSE_QUESTION, missing_term, aspects=["开课学期", "选修课程"]
    )
    assert audit.units[0]["status"] == "generation_missing"
    assert audit.units[0]["missing_items"]


def test_pure_bound_ask_echo_is_dropped_before_core_matching() -> None:
    bound = _Property(
        kind="bound",
        cue="至多门",
        core="普通本科生每学期选课门数",
        direction="max",
        measure="门",
        ask="至多",
    )
    assert _clause_is_property_echo("至多选", [bound])
    assert _clause_is_property_echo("至多选修", [bound])
    assert not _clause_is_property_echo("至多选第2门", [bound])
    assert not _clause_is_property_echo("至多选，另需缴费", [bound])


def test_a_missing_bound_still_cannot_use_another_scope_value() -> None:
    missing = _Property(
        kind="bound",
        cue="至多门",
        core="转专业学生每学期选课门数",
        direction="max",
        measure="门",
        ask="至多",
    )
    text = "普通本科生每学期至多选11门课程，辅修专业至多选41门课程。"
    assert not _property_supported(missing, text)


def test_minimum_day_bound_replaces_duplicate_duration_check() -> None:
    question = "某申请经校级会议通过后的网上公示至少几天？"
    claim = "某申请经校级会议审议通过后，网上公示时间不少于7个工作日。"
    properties = _requested_properties(question)
    assert not any(prop.kind == "duration" for prop in properties)
    bounds = [prop for prop in properties if prop.kind == "bound"]
    assert len(bounds) == 1
    assert bounds[0].direction == "min"
    assert bounds[0].measure == "天"
    assert _property_supported(bounds[0], claim)


def test_minimum_bound_rejects_wrong_action_and_wrong_unit() -> None:
    question = "某申请经校级会议通过后的网上公示至少几天？"
    bounds = [
        prop for prop in _requested_properties(question) if prop.kind == "bound"
    ]
    assert len(bounds) == 1
    wrong_action = (
        "某申请经校级会议审议通过后，助学金名单报送时间不少于7个工作日。"
    )
    assert not _property_supported(bounds[0], wrong_action)

    workday_question = "某申请经校级会议通过后的网上公示至少几个工作日？"
    workday_bounds = [
        prop
        for prop in _requested_properties(workday_question)
        if prop.kind == "bound"
    ]
    assert len(workday_bounds) == 1
    assert workday_bounds[0].measure == "工作日"
    natural_day_claim = (
        "某申请经校级会议审议通过后，网上公示时间不少于7个自然日。"
    )
    assert not _property_supported(workday_bounds[0], natural_day_claim)


def test_unscoped_duration_question_is_not_dropped() -> None:
    question = "某申请经校级会议通过后的网上公示至少几天，入学多久才能转专业？"
    durations = [
        prop for prop in _requested_properties(question) if prop.kind == "duration"
    ]
    assert any("入学" in prop.core for prop in durations)


def test_explicit_week_ask_is_never_a_comma_topic() -> None:
    question = (
        "学业预警名单和书面告知应在开学几周内处理，"
        "预警汇总表又应在几周内备案"
    )
    assert not _is_topic_part("学业预警名单和书面告知应在开学几周内处理")
    assert _is_topic_part("2025级计算机学院")
    clauses, topics = _question_clauses(question, ())
    assert not topics
    assert any("预警汇总表" in clause for clause in clauses)


def test_full_checker_accepts_comma_delayed_deadline_answer() -> None:
    question = (
        "学业预警名单和书面告知应在开学几周内处理，"
        "预警汇总表又应在几周内备案"
    )
    answer = (
        "学业预警名单和书面告知应在开学2周内处理，"
        "预警汇总表应在开学3周内备案。"
    )
    audit = _audit(question, answer)
    assert audit.units[0]["status"] == "answered"


def test_full_checker_flags_one_missing_deadline_branch() -> None:
    question = (
        "学业预警名单和书面告知应在开学几周内处理，"
        "预警汇总表又应在几周内备案"
    )
    partial = "学业预警名单和书面告知应在开学2周内处理。"
    audit = _audit(question, partial)
    assert audit.units[0]["status"] != "answered"
    assert audit.missing or audit.units[0]["missing_items"]
