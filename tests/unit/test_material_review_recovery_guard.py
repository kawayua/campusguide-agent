"""Guard: purpose-only identity checks are not submitted-material answers."""

from __future__ import annotations

import json

from campusguide.agents.coverage import CoverageChecker
from campusguide.agents.plan import QuestionUnit
from campusguide.agents.providers import FakeChatProvider
from campusguide.domain.models import Citation, Claim
from tests.w2_helpers import make_article, make_evidence

QUESTION = "申请时须提交哪些材料？"


def _unit() -> QuestionUnit:
    return QuestionUnit(
        unit_id="u-material",
        original_span=QUESTION,
        requested_aspects=["提交材料"],
        answer_shape="list",
    )


def _model_answer(claim_id: str) -> str:
    return json.dumps(
        {
            "units": [
                {
                    "unit_id": "u-material",
                    "status": "answered",
                    "claim_ids": [claim_id],
                    "missing_items": [],
                }
            ],
            "unanswered_questions": [],
        },
        ensure_ascii=False,
    )


def _check(answer: str):
    article = make_article(text=answer)
    citation = Citation(
        article_id=article.article_id,
        quote=answer,
        quote_start=0,
        quote_end=len(answer),
        doc_title=article.doc_title,
        article_no=article.article_no,
        part_no=article.part_no,
        block_type=article.block_type,
        effective_from=article.effective_from,
        date_confidence=article.date_confidence,
        source_url=article.source_url,
    )
    claim = Claim(claim_id="claim-material", text=answer, citations=[citation])
    checker = CoverageChecker(FakeChatProvider([_model_answer(claim.claim_id)]))
    return checker.check(
        QUESTION, [_unit()], [claim], [make_evidence(article)], []
    )


def test_purpose_only_material_statement_stays_generation_missing() -> None:
    audit = _check("身份证和成绩单仅用于身份验证。")

    assert audit.unavailable is False
    assert audit.units[0]["status"] == "generation_missing"
    assert audit.units[0]["claim_ids"] == []


def test_explicit_submission_with_identity_purpose_stays_answered() -> None:
    audit = _check("申请人须提交身份证复印件和成绩单用于身份验证。")

    assert audit.unavailable is False
    assert audit.units[0]["status"] == "answered"
    assert audit.units[0]["claim_ids"] == ["claim-material"]
