"""Regression coverage for condition-prefix answering and ratio asks (ND08)."""

from __future__ import annotations

import json
from datetime import date

from campusguide.agents.coverage import CoverageChecker, _question_clauses
from campusguide.agents.plan import QuestionUnit
from campusguide.agents.providers import FakeChatProvider
from campusguide.domain.models import Citation, Claim

_QUESTION = "实行学分制的本科生，修满总学分多少比例后不允许转专业？"
_TITLE = "华南农业大学本科学生转专业实施办法"
_COMPLETE = (
    "实行学分制且其学分已修满教学计划规定总学分的三分之二及以上的学生"
    "不允许转专业。"
)


def _unit(span: str, *, version: str | None = None) -> QuestionUnit:
    return QuestionUnit(
        unit_id="u1",
        original_span=span,
        subject="",
        objects=[],
        version=version,
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


def test_condition_prefix_is_scope_not_a_separately_asked_fact() -> None:
    clauses, topics = _question_clauses(_QUESTION)

    assert topics == ["实行学分制的本科生"]
    assert "实行学分制的本科生" not in clauses
    assert any("比例" in clause and "转专业" in clause for clause in clauses)


def test_conjunction_inside_condition_is_preserved_as_scope() -> None:
    question = "实行学分制且修满教学计划总学分的本科生，多少比例后不允许转专业？"
    clauses, topics = _question_clauses(question)

    assert topics == ["实行学分制且修满教学计划总学分的本科生"]
    assert "且" in topics[0]
    assert any("比例" in clause and "转专业" in clause for clause in clauses)


def test_complete_verified_fact_is_answered_with_optimistic_and_pessimistic_audits() -> None:
    unit = _unit(_QUESTION)
    claim = _claim(_COMPLETE)

    optimistic = _audit(unit, claim)
    pessimistic = _audit(
        unit,
        claim,
        status="generation_missing",
        claim_ids=[],
        missing_items=["实行学分制的本科生"],
    )

    for audit in (optimistic, pessimistic):
        assert audit.units[0]["status"] == "answered", audit.missing
        assert audit.units[0]["claim_ids"] == ["c1"]
        assert audit.units[0]["missing_items"] == []


def test_context_only_and_missing_threshold_claims_remain_missing() -> None:
    unit = _unit(_QUESTION)
    context_only = _claim("实行学分制的本科生不允许转专业。")
    missing_threshold = _claim(
        "实行学分制且其学分已修满教学计划规定总学分的学生不允许转专业。"
    )

    for claim in (context_only, missing_threshold):
        audit = _audit(unit, claim)
        assert audit.units[0]["status"] == "generation_missing"
        assert "比例" in " ".join(audit.missing)


def test_wrong_business_or_population_is_not_recovered() -> None:
    unit = _unit(_QUESTION)
    wrong_business = _claim(
        "实行学分制且其学分已修满教学计划规定总学分的三分之二及以上的学生"
        "不允许休学。"
    )
    wrong_population = _claim(
        "实行学分制且其学分已修满教学计划规定总学分的三分之二及以上的研究生"
        "不允许转专业。"
    )
    wrong_year = _claim(
        "2024级实行学分制且其学分已修满教学计划规定总学分的三分之二及以上的"
        "学生不允许转专业。"
    )

    for unit_case, claim in (
        (unit, wrong_business),
        (unit, wrong_population),
        (_unit(_QUESTION, version="2025级"), wrong_year),
    ):
        audit = _audit(unit_case, claim)
        assert audit.units[0]["status"] == "generation_missing"


def test_explicit_definition_ask_remains_unanswered_by_a_threshold_fact() -> None:
    for question in (
        "学分制本科生是什么意思？",
        "实行学分制的本科生适用哪些学生？",
    ):
        audit = _audit(_unit(question), _claim(_COMPLETE))
        assert audit.units[0]["status"] == "generation_missing"
        assert audit.missing
