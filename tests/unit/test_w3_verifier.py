"""W3 Verifier：claim/citation 硬校验、丢弃与最终决策。"""

from __future__ import annotations

import json

from campusguide.agents.advisor import CitationDraft, ClaimDraft
from campusguide.domain.enums import (
    IssueScope,
    IssueSeverity,
    OutcomeCode,
    VerificationDecision,
)
from campusguide.domain.models import VerificationIssue
from campusguide.verification.verifier import Verifier
from tests.w2_helpers import make_article, make_evidence

ARTICLE_ID = "demo-policy@v2021#第一条"


def _draft(
    claim_id: str,
    text: str,
    quotes: list[str],
    *,
    article_id: str = ARTICLE_ID,
) -> ClaimDraft:
    return ClaimDraft(
        claim_id=claim_id,
        text=text,
        kind="eligibility",
        citations=[CitationDraft(article_id=article_id, quote=quote) for quote in quotes],
    )


def _evidence(text: str = "申请奖学金应当学习成绩优异，品行端正。奖励20000元，连续2年。"):
    return [make_evidence(make_article(article_id=ARTICLE_ID, text=text))]


def _codes(issues) -> set[str]:
    return {issue.code for issue in issues}


def test_valid_and_invalid_citation_keeps_claim_with_warning() -> None:
    draft = _draft(
        "c1",
        "申请奖学金需要学习成绩优异。",
        ["学习成绩优异，品行端正", "原文中没有这句话"],
    )
    report = Verifier().verify([draft], _evidence())

    assert report.result.decision is VerificationDecision.PASS
    assert [claim.claim_id for claim in report.result.claims] == ["c1"]
    assert len(report.result.claims[0].citations) == 1
    warning = next(issue for issue in report.result.issues if issue.code == "QUOTE_MISMATCH")
    assert warning.scope is IssueScope.CITATION
    assert warning.severity is IssueSeverity.WARNING


def test_all_citations_invalid_drops_claim_and_abstains() -> None:
    draft = _draft("c1", "申请奖学金需要学习成绩优异。", ["原文中没有这句话"])
    report = Verifier().verify([draft], _evidence())

    assert report.result.decision is VerificationDecision.ABSTAIN
    assert report.result.reason_code is OutcomeCode.UNVERIFIABLE
    assert report.result.claims == []
    assert report.result.dropped_claim_ids == ["c1"]
    assert "NO_VALID_CITATION" in _codes(report.result.issues)
    assert "QUOTE_MISMATCH" in _codes(report.result.issues)


def test_fact_uncovered_drops_claim() -> None:
    article = make_article(article_id=ARTICLE_ID, text="奖励10000元。")
    draft = _draft("c1", "奖励20000元。", ["奖励10000元"])
    report = Verifier().verify([draft], [make_evidence(article)])

    assert report.result.decision is VerificationDecision.ABSTAIN
    issue = next(issue for issue in report.result.issues if issue.code == "NUMBER_NOT_SUPPORTED")
    assert issue.scope is IssueScope.CLAIM
    assert issue.severity is IssueSeverity.BLOCKING
    assert issue.facts == ["20000元"]
    assert report.result.dropped_claim_ids == ["c1"]


def test_equivalent_fact_forms_pass() -> None:
    article = make_article(
        article_id=ARTICLE_ID,
        text="奖励20000元，连续2年，2025年9月1日生效。",
    )
    draft = _draft(
        "c1",
        "奖励两万元，连续两年，2025-09-01 生效。",
        ["奖励20000元，连续2年，2025年9月1日生效"],
    )
    report = Verifier().verify([draft], [make_evidence(article)])

    assert report.result.decision is VerificationDecision.PASS
    assert report.result.issues == []


def test_fact_can_be_split_across_citations() -> None:
    article = make_article(
        article_id=ARTICLE_ID,
        text="奖励20000元，并且连续2年。",
    )
    draft = _draft("c1", "奖励两万元，连续两年。", ["奖励20000元", "并且连续2年"])
    report = Verifier().verify([draft], [make_evidence(article)])

    assert report.result.decision is VerificationDecision.PASS
    assert len(report.result.claims[0].citations) == 2


def test_facts_are_not_shared_between_claims() -> None:
    article = make_article(
        article_id=ARTICLE_ID,
        text="奖励20000元。连续2年。学习成绩优异。",
    )
    drafts = [
        _draft("c1", "奖励20000元。", ["奖励20000元"]),
        _draft("c2", "连续两年。", ["学习成绩优异"]),
    ]
    report = Verifier().verify(drafts, [make_evidence(article)])

    assert report.result.decision is VerificationDecision.PARTIAL
    assert [claim.claim_id for claim in report.result.claims] == ["c1"]
    dropped = [issue for issue in report.result.issues if issue.code == "NUMBER_NOT_SUPPORTED"]
    assert dropped[0].claim_id == "c2"
    assert dropped[0].facts == ["2年"]


def test_privacy_violation_drops_claim_without_logging_value() -> None:
    draft = _draft("c1", "联系电话 13800138000。", ["学习成绩优异，品行端正"])
    report = Verifier().verify([draft], _evidence())

    assert report.result.decision is VerificationDecision.ABSTAIN
    issue = next(issue for issue in report.result.issues if issue.code == "PRIVACY_VIOLATION")
    assert issue.scope is IssueScope.CLAIM
    assert issue.severity is IssueSeverity.BLOCKING
    assert issue.facts == ["mobile"]
    payload = json.dumps([item.model_dump(mode="json") for item in report.result.issues])
    assert "13800138000" not in payload


def test_partial_keeps_only_verified_claim() -> None:
    drafts = [
        _draft("c1", "申请奖学金需要学习成绩优异。", ["学习成绩优异，品行端正"]),
        _draft("c2", "另一条结论。", ["原文中没有这句话"]),
    ]
    report = Verifier().verify(drafts, _evidence())

    assert report.result.decision is VerificationDecision.PARTIAL
    assert report.result.reason_code is OutcomeCode.PARTIAL_ANSWER
    assert [claim.claim_id for claim in report.result.claims] == ["c1"]
    assert report.result.dropped_claim_ids == ["c2"]


def test_zero_drafts_is_no_valid_claim() -> None:
    report = Verifier().verify([], _evidence())

    assert report.result.decision is VerificationDecision.ABSTAIN
    assert report.result.reason_code is OutcomeCode.NO_VALID_CLAIM
    assert report.records == []


def test_scope_warnings_are_merged_and_do_not_block_pass() -> None:
    scope_issue = VerificationIssue(
        code="AUDIENCE_UNDETERMINED",
        scope=IssueScope.EVIDENCE,
        severity=IssueSeverity.WARNING,
    )
    draft = _draft("c1", "申请奖学金需要学习成绩优异。", ["学习成绩优异，品行端正"])
    report = Verifier().verify([draft], _evidence(), scope_issues=[scope_issue])

    assert report.result.decision is VerificationDecision.PASS
    assert _codes(report.result.issues) == {"AUDIENCE_UNDETERMINED"}


def test_citation_span_uses_original_text_offset() -> None:
    article = make_article(
        article_id=ARTICLE_ID,
        text="前言\n第一条 奖励20000元。",
    )
    draft = _draft("c1", "奖励20000元。", ["奖励20000元"])
    report = Verifier().verify([draft], [make_evidence(article)])

    citation = report.result.claims[0].citations[0]
    expected_start = article.text.index("奖励20000元")
    assert citation.quote_start == expected_start
    assert citation.quote_end == expected_start + len("奖励20000元")
    assert citation.quote == "奖励20000元"
    assert citation.part_no == 1


def test_records_include_all_drafts_and_drop_status() -> None:
    drafts = [
        _draft("c1", "申请奖学金需要学习成绩优异。", ["学习成绩优异，品行端正"]),
        _draft("c2", "奖励20000元。", ["奖励10000元"]),
    ]
    article = make_article(
        article_id=ARTICLE_ID,
        text="申请奖学金应当学习成绩优异，品行端正。奖励10000元。",
    )
    report = Verifier().verify(drafts, [make_evidence(article)])

    assert [record["claim_id"] for record in report.records] == ["c1", "c2"]
    assert report.records[0]["kept"] is True
    assert report.records[1]["kept"] is False
    assert report.records[1]["dropped_reason"] == "NUMBER_NOT_SUPPORTED"


def test_verifier_does_not_allow_blocking_issue_with_rendered_claim() -> None:
    """即使调用方伪造 issues，decide 也不允许 blocking 与 PASS 并存。"""
    draft = _draft("c1", "申请奖学金需要学习成绩优异。", ["学习成绩优异，品行端正"])
    report = Verifier().verify([draft], _evidence())
    assert report.result.decision is VerificationDecision.PASS
    assert not [
        issue
        for issue in report.result.issues
        if issue.scope is IssueScope.CLAIM and issue.severity is IssueSeverity.BLOCKING
    ]
