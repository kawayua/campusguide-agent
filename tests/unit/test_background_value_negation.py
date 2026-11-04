"""Regression coverage for condition-value polarity under generic labels."""

from __future__ import annotations

import json
from datetime import date

from campusguide.agents.coverage import CoverageChecker
from campusguide.agents.plan import QuestionUnit
from campusguide.agents.providers import FakeChatProvider
from campusguide.domain.models import Citation, Claim

_TITLE = "华南农业大学本科学生转专业实施办法"
_Q_POSITIVE = "学籍状态为甲类的本科生，修满总学分多少比例后不允许转专业？"
_Q_NEGATIVE = "学籍状态为非甲类的本科生，修满总学分多少比例后不允许转专业？"
_Q_UNPROVEN = "学籍状态为乙类的本科生，修满总学分多少比例后不允许转专业？"
_POSITIVE_BODY = (
    "学籍状态为甲类的本科生修满总学分三分之二及以上后不允许转专业。"
)
_NEGATIVE_BODY = (
    "学籍状态为非甲类的本科生修满总学分三分之二及以上后不允许转专业。"
)
_NOT_BODIES = (
    "学籍状态不是甲类的本科生修满总学分三分之二及以上后不允许转专业。",
    "学籍状态为不是甲类的本科生修满总学分三分之二及以上后不允许转专业。",
)


def _unit(span: str) -> QuestionUnit:
    return QuestionUnit(
        unit_id="u1",
        original_span=span,
        subject="",
        objects=[],
        requested_aspects=[span],
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
                doc_title=_TITLE,
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


def test_positive_arbitrary_category_with_positive_body_passes() -> None:
    _assert_answered(_unit(_Q_POSITIVE), _claim(_POSITIVE_BODY))


def test_positive_condition_rejects_non_category_body() -> None:
    _assert_rejected(_unit(_Q_POSITIVE), _claim(_NEGATIVE_BODY))


def test_positive_condition_rejects_bu_shi_category_body() -> None:
    for body in _NOT_BODIES:
        _assert_rejected(_unit(_Q_POSITIVE), _claim(body))


def test_explicit_negative_condition_with_matching_body_passes() -> None:
    _assert_answered(_unit(_Q_NEGATIVE), _claim(_NEGATIVE_BODY))


def test_negative_condition_with_positive_body_is_missing() -> None:
    _assert_rejected(_unit(_Q_NEGATIVE), _claim(_POSITIVE_BODY))


def test_unproven_other_category_condition_is_missing() -> None:
    _assert_rejected(_unit(_Q_UNPROVEN), _claim(_POSITIVE_BODY))
