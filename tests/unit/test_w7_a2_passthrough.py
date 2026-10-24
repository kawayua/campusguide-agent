"""W7 A2：eval-only pass-through verifier。"""

from __future__ import annotations

from campusguide.agents.advisor import CitationDraft, ClaimDraft
from campusguide.domain.enums import OutcomeCode, VerificationDecision
from campusguide.verification.passthrough import PassThroughVerifier
from tests.w2_helpers import make_article, make_evidence

ARTICLE_ID = "demo-policy@v2021#第一条"
TEXT = "申请奖学金应当学习成绩优异，品行端正。"


def _evidence():
    return [make_evidence(make_article(article_id=ARTICLE_ID, text=TEXT))]


def _draft(quote: str, *, text: str = "申请奖学金应当学习成绩优异。") -> ClaimDraft:
    return ClaimDraft(
        claim_id="c1",
        text=text,
        kind="eligibility",
        citations=[CitationDraft(article_id=ARTICLE_ID, quote=quote)],
    )


def test_passthrough_keeps_claim_with_mismatched_quote() -> None:
    result = PassThroughVerifier().verify([_draft("quote 不在原文中")], _evidence())
    assert result.result.decision is VerificationDecision.PASS
    assert len(result.result.claims) == 1
    assert result.result.claims[0].citations[0].quote == "quote 不在原文中"
    assert result.check is None


def test_passthrough_without_drafts_abstains() -> None:
    result = PassThroughVerifier().verify([], _evidence())
    assert result.result.decision is VerificationDecision.ABSTAIN
    assert result.result.reason_code is OutcomeCode.NO_VALID_CLAIM


def test_passthrough_without_valid_citation_abstains() -> None:
    draft = ClaimDraft(claim_id="c1", text="无引用", kind="other", citations=[])
    result = PassThroughVerifier().verify([draft], _evidence())
    assert result.result.decision is VerificationDecision.ABSTAIN
    assert result.result.reason_code is OutcomeCode.UNVERIFIABLE
    assert result.result.dropped_claim_ids == ["c1"]
