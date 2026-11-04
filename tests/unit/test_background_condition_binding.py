"""Regression coverage for background-condition predicate binding."""

from __future__ import annotations

import json
from datetime import date

from campusguide.agents.coverage import CoverageChecker
from campusguide.agents.plan import QuestionUnit
from campusguide.agents.providers import FakeChatProvider
from campusguide.domain.models import Citation, Claim

_TITLE = "华南农业大学本科学生转专业实施办法"
_Q_SYSTEM = "实行学分制的本科生，修满总学分多少比例后不允许转专业？"
_Q_REGION = "来自港澳地区的本科生，修满总学分多少比例后不允许转专业？"
_Q_CONJUNCTION = (
    "实行学分制且修满教学计划总学分的本科生，多少比例后不允许转专业？"
)
_Q_UNKNOWN = (
    "学籍状态为特定类别的本科生，修满总学分多少比例后不允许转专业？"
)
_COMPLETE = (
    "实行学分制且其学分已修满教学计划规定总学分的三分之二及以上的学生"
    "不允许转专业。"
)
_REGION_COMPLETE = (
    "港澳地区本科学生其学分已修满教学计划规定总学分的三分之二及以上后"
    "不允许转专业。"
)
_WRONG_SYSTEM = (
    "实行学年制且学分已修满总学分三分之二及以上的学生不允许转专业。"
)
_WRONG_REGION = (
    "内地本科学生的学分修满总学分三分之二及以上后不允许转专业。"
)
_NO_RATIO = (
    "实行学分制且其学分已修满教学计划规定总学分的本科生不允许转专业。"
)


def _unit(span: str) -> QuestionUnit:
    return QuestionUnit(
        unit_id="u1",
        original_span=span,
        subject="",
        objects=[],
        requested_aspects=[span],
    )


def _claim(text: str, *, title: str = _TITLE) -> Claim:
    return Claim(
        claim_id="c1",
        text=text,
        citations=[
            Citation(
                article_id="a1",
                quote=text,
                quote_start=0,
                quote_end=len(text),
                doc_title=title,
                article_no="第五条",
                effective_from=date(2026, 1, 1),
                source_url="https://example.edu.cn/policy",
            )
        ],
    )


def _audit(
    unit: QuestionUnit,
    claim: Claim,
    *,
    status: str = "answered",
    claim_ids: list[str] | None = None,
    missing_items: list[str] | None = None,
):
    verdict = {
        "units": [
            {
                "unit_id": unit.unit_id,
                "status": status,
                "claim_ids": (
                    [claim.claim_id] if claim_ids is None else claim_ids
                ),
                "missing_items": missing_items or [],
            }
        ],
        "unanswered_questions": [],
    }
    provider = FakeChatProvider([json.dumps(verdict, ensure_ascii=False)])
    return CoverageChecker(provider).check(
        unit.original_span, [unit], [claim], [], []
    )


def _assert_answered(unit: QuestionUnit, claim: Claim) -> None:
    topic = unit.original_span.split("，", 1)[0]
    optimistic = _audit(unit, claim)
    pessimistic = _audit(
        unit,
        claim,
        status="generation_missing",
        claim_ids=[],
        missing_items=[topic],
    )

    for audit in (optimistic, pessimistic):
        assert audit.units[0]["status"] == "answered", audit.missing
        assert audit.units[0]["claim_ids"] == ["c1"]
        assert audit.units[0]["missing_items"] == []


def _assert_rejected(unit: QuestionUnit, claim: Claim) -> None:
    topic = unit.original_span.split("，", 1)[0]
    optimistic = _audit(unit, claim)
    pessimistic = _audit(
        unit,
        claim,
        status="generation_missing",
        claim_ids=[],
        missing_items=[topic],
    )

    for audit in (optimistic, pessimistic):
        assert audit.units[0]["status"] == "generation_missing"
        assert audit.units[0]["claim_ids"] == []


def test_wrong_study_system_or_region_is_rejected() -> None:
    for question, body in (
        (_Q_SYSTEM, _WRONG_SYSTEM),
        (_Q_REGION, _WRONG_REGION),
    ):
        _assert_rejected(_unit(question), _claim(body))


def test_existing_complete_undergrad_source_still_passes() -> None:
    _assert_answered(_unit(_Q_SYSTEM), _claim(_COMPLETE))


def test_correct_region_and_conjunctive_conditions_pass() -> None:
    for question, body in (
        (_Q_REGION, _REGION_COMPLETE),
        (_Q_CONJUNCTION, _COMPLETE),
    ):
        _assert_answered(_unit(question), _claim(body))


def test_conjunction_missing_one_condition_remains_missing() -> None:
    missing_system = (
        "其学分已修满教学计划规定总学分的三分之二及以上的本科生"
        "不允许转专业。"
    )
    missing_threshold = (
        "实行学分制且其学分已修满总学分的三分之二及以上的本科生"
        "不允许转专业。"
    )

    for body in (missing_system, missing_threshold):
        _assert_rejected(_unit(_Q_CONJUNCTION), _claim(body))


def test_unknown_condition_cannot_use_generic_undergrad_threshold() -> None:
    _assert_rejected(_unit(_Q_UNKNOWN), _claim(_COMPLETE))


def test_title_only_or_opposed_body_cannot_promote_condition() -> None:
    title_echo = "华南农业大学学分制本科生转专业实施办法"
    missing_condition = (
        "其学分已修满教学计划规定总学分的三分之二及以上的本科生"
        "不允许转专业。"
    )

    for body in (missing_condition, _WRONG_SYSTEM):
        _assert_rejected(
            _unit(_Q_SYSTEM),
            _claim(body, title=title_echo),
        )


def test_original_complete_claim_does_not_answer_missing_ratio() -> None:
    audit = _audit(_unit(_Q_SYSTEM), _claim(_NO_RATIO))

    assert audit.units[0]["status"] == "generation_missing"
    assert "比例" in " ".join(audit.missing)
