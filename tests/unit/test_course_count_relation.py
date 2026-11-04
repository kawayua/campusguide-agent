"""Regression coverage for split course-enrollment threshold/stop relations."""

from __future__ import annotations

import json
from datetime import date

from campusguide.agents.coverage import CoverageChecker
from campusguide.agents.plan import QuestionUnit
from campusguide.agents.providers import FakeChatProvider
from campusguide.domain.models import Citation, Claim

_QUESTION = "选课人数不足多少人的课程原则上停开，如果仍要开课需谁批准？"
_TITLE = "华南农业大学本科生选课管理规定"
_NUMBER = 20
_FULL = (
    f"每门课程修读人数原则上不得少于{_NUMBER}人。"
    f"当选课人数少于{_NUMBER}人时，该课程停开。"
    f"如选课人数少于{_NUMBER}人而又需要开课的，须报教务处批准。"
)


def _unit(question: str = _QUESTION) -> QuestionUnit:
    return QuestionUnit(
        unit_id="u1",
        original_span=question,
        subject="",
        objects=[],
        requested_aspects=[question],
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
                article_no="第十三条",
                effective_from=date(2016, 11, 11),
                source_url="https://example.edu.cn/course-selection",
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


def test_split_condition_effect_answers_enrollment_threshold() -> None:
    unit = _unit()
    claim = _claim(_FULL)
    optimistic = _audit(unit, claim)
    pessimistic = _audit(
        unit,
        claim,
        status="generation_missing",
        claim_ids=[],
        missing_items=["多少人"],
    )

    for audit in (optimistic, pessimistic):
        assert audit.units[0]["status"] == "answered", audit.missing
        assert audit.units[0]["claim_ids"] == ["c1"]
        assert audit.units[0]["missing_items"] == []


def test_insufficient_alias_binds_split_threshold() -> None:
    unit = _unit()
    claim = _claim(
        f"每门课程修读人数原则上不得少于{_NUMBER}人。"
        f"当选课人数不足{_NUMBER}人时，该课程停开。"
        f"如选课人数少于{_NUMBER}人而又需要开课的，须报教务处批准。"
    )

    audit = _audit(unit, claim)

    assert audit.units[0]["status"] == "answered", audit.missing
    assert audit.units[0]["claim_ids"] == ["c1"]
    assert audit.units[0]["missing_items"] == []


def test_approval_subclause_remains_required() -> None:
    unit = _unit()
    claim = _claim(
        f"每门课程修读人数原则上不得少于{_NUMBER}人。"
        f"当选课人数少于{_NUMBER}人时，该课程停开。"
    )

    audit = _audit(unit, claim)

    assert audit.units[0]["status"] == "generation_missing"
    assert any("批准" in item for item in audit.units[0]["missing_items"])


def test_missing_number_or_context_only_stays_missing() -> None:
    unit = _unit()
    context_only = _claim(
        f"每门课程修读人数原则上不得少于{_NUMBER}人。"
        f"如选课人数少于{_NUMBER}人而又需要开课的，须报教务处批准。"
    )
    missing_number = _claim(
        "当选课人数不足时，该课程停开。"
        "如选课人数不足而又需要开课的，须报教务处批准。"
    )
    missing_number_with_other_value = _claim(
        f"每门课程修读人数原则上不得少于{_NUMBER}人。"
        "当选课人数不足时，该课程停开。"
        "如选课人数不足而又需要开课的，须报教务处批准。"
    )

    for claim in (
        context_only,
        missing_number,
        missing_number_with_other_value,
    ):
        audit = _audit(unit, claim)
        assert audit.units[0]["status"] == "generation_missing"
        assert "多少人" in " ".join(audit.units[0]["missing_items"])


def test_unrelated_person_count_or_fee_cannot_supply_threshold() -> None:
    unit = _unit()
    unrelated_counts = (
        _claim(
            f"导师人数少于{_NUMBER}人，该课程停开。"
            "如仍要开课的，须报教务处批准。"
        ),
        _claim(
            f"课程教师人数少于{_NUMBER}人，该课程停开。"
            "如仍要开课的，须报教务处批准。"
        ),
        _claim(
            f"选课费用为{_NUMBER}元。"
            "如仍要开课的，须报教务处批准。"
        ),
    )

    for claim in unrelated_counts:
        audit = _audit(unit, claim)
        assert audit.units[0]["status"] == "generation_missing"
        assert "多少人" in " ".join(audit.units[0]["missing_items"])


def test_nonadjacent_or_negated_stop_does_not_bind() -> None:
    unit = _unit()
    nonadjacent = _claim(
        f"当选课人数少于{_NUMBER}人时。该课程停开。"
        "如仍要开课的，须报教务处批准。"
    )
    negated = _claim(
        f"当选课人数少于{_NUMBER}人时，该课程不会停开。"
        "如仍要开课的，须报教务处批准。"
    )
    other_course = _claim(
        f"当选课人数少于{_NUMBER}人时，另一门课程停开。"
        "如仍要开课的，须报教务处批准。"
    )
    wrong_direction = _claim(
        f"当选课人数不少于{_NUMBER}人时，该课程停开。"
        "如仍要开课的，须报教务处批准。"
    )

    for claim in (nonadjacent, negated, other_course, wrong_direction):
        audit = _audit(unit, claim)
        assert audit.units[0]["status"] == "generation_missing"
        assert "多少人" in " ".join(audit.units[0]["missing_items"])
