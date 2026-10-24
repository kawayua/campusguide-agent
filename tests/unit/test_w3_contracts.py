"""W3 v2 核对契约：Issue scope/severity、Result decision 与不变量。"""

from __future__ import annotations

from datetime import date

import pytest
from pydantic import ValidationError

from campusguide.domain.enums import (
    BlockType,
    IssueScope,
    IssueSeverity,
    OutcomeCode,
    VerificationDecision,
)
from campusguide.domain.models import (
    Citation,
    Claim,
    VerificationIssue,
    VerificationResult,
)
from campusguide.verification.verifier import decide


def _citation(article_id: str = "demo-policy@v2021#第一条") -> Citation:
    return Citation(
        article_id=article_id,
        quote="学习成绩优异",
        quote_start=0,
        quote_end=6,
        doc_title="演示办法",
        article_no="第一条",
        block_type=BlockType.ARTICLE,
        effective_from=date(2021, 9, 1),
        source_url="https://example.edu.cn/policy",
    )


def _claim(claim_id: str = "c1") -> Claim:
    return Claim(claim_id=claim_id, text="申请奖学金需要成绩优异。", citations=[_citation()])


def _blocking_claim_issue(claim_id: str = "c1") -> VerificationIssue:
    return VerificationIssue(
        code="NUMBER_NOT_SUPPORTED",
        scope=IssueScope.CLAIM,
        severity=IssueSeverity.BLOCKING,
        claim_id=claim_id,
        facts=["20000元"],
    )


def test_issue_contract_fields_and_enum_values() -> None:
    issue = VerificationIssue(
        code="QUOTE_MISMATCH",
        scope=IssueScope.CITATION,
        severity=IssueSeverity.WARNING,
        claim_id="c1",
        article_id="demo-policy@v2021#第一条",
        part_no=1,
        detail="quote 无法映射回原文",
    )
    assert issue.scope is IssueScope.CITATION
    assert issue.severity is IssueSeverity.WARNING
    assert issue.part_no == 1
    assert issue.facts == []


def test_pass_cannot_coexist_with_claim_blocking_issue() -> None:
    with pytest.raises(ValidationError, match="blocking"):
        VerificationResult(
            decision=VerificationDecision.PASS,
            claims=[_claim()],
            issues=[_blocking_claim_issue()],
            dropped_claim_ids=[],
        )


def test_pass_allows_scope_warnings() -> None:
    result = VerificationResult(
        decision=VerificationDecision.PASS,
        claims=[_claim()],
        issues=[
            VerificationIssue(
                code="AUDIENCE_UNDETERMINED",
                scope=IssueScope.EVIDENCE,
                severity=IssueSeverity.WARNING,
            )
        ],
        dropped_claim_ids=[],
    )
    assert result.decision is VerificationDecision.PASS
    assert result.reason_code is None


def test_partial_requires_dropped_claim_ids() -> None:
    with pytest.raises(ValidationError, match="dropped_claim_ids"):
        VerificationResult(
            decision=VerificationDecision.PARTIAL,
            claims=[_claim()],
            issues=[_blocking_claim_issue("c2")],
            dropped_claim_ids=[],
        )


def test_partial_requires_reason_and_remaining_claim() -> None:
    with pytest.raises(ValidationError):
        VerificationResult(
            decision=VerificationDecision.PARTIAL,
            claims=[],
            issues=[_blocking_claim_issue("c2")],
            dropped_claim_ids=["c2"],
            reason_code=OutcomeCode.PARTIAL_ANSWER,
        )
    with pytest.raises(ValidationError):
        VerificationResult(
            decision=VerificationDecision.PARTIAL,
            claims=[_claim()],
            issues=[_blocking_claim_issue("c2")],
            dropped_claim_ids=["c2"],
        )


def test_abstain_has_no_claims_and_requires_reason() -> None:
    with pytest.raises(ValidationError):
        VerificationResult(
            decision=VerificationDecision.ABSTAIN,
            claims=[_claim()],
            issues=[],
            dropped_claim_ids=[],
            reason_code=OutcomeCode.UNVERIFIABLE,
        )
    with pytest.raises(ValidationError):
        VerificationResult(
            decision=VerificationDecision.ABSTAIN,
            claims=[],
            issues=[],
            dropped_claim_ids=[],
        )


def test_decide_zero_drafts_is_no_valid_claim() -> None:
    result = decide([], [], [], drafts_total=0)
    assert result.decision is VerificationDecision.ABSTAIN
    assert result.reason_code is OutcomeCode.NO_VALID_CLAIM


def test_decide_all_dropped_is_unverifiable() -> None:
    result = decide([], [_blocking_claim_issue()], ["c1"], drafts_total=1)
    assert result.decision is VerificationDecision.ABSTAIN
    assert result.reason_code is OutcomeCode.UNVERIFIABLE
    assert result.dropped_claim_ids == ["c1"]


def test_decide_some_dropped_is_partial() -> None:
    result = decide([_claim()], [_blocking_claim_issue("c2")], ["c2"], drafts_total=2)
    assert result.decision is VerificationDecision.PARTIAL
    assert result.reason_code is OutcomeCode.PARTIAL_ANSWER
    assert [claim.claim_id for claim in result.claims] == ["c1"]


def test_decide_no_drop_is_pass() -> None:
    result = decide([_claim()], [], [], drafts_total=1)
    assert result.decision is VerificationDecision.PASS
    assert result.reason_code is None


def test_decide_refuses_blocking_issue_without_dropped_id() -> None:
    """blocking claim issue 必须对应 dropped id，否则契约自身应失败关闭。"""
    with pytest.raises(ValueError, match="dropped"):
        decide([_claim()], [_blocking_claim_issue()], [], drafts_total=1)


def test_w3_reason_codes_are_available() -> None:
    expected = {
        "PARTIAL_ANSWER",
        "ONLY_REPEALED",
        "VERSION_CONFLICT",
        "VERSION_MISMATCH",
        "UNVERIFIABLE",
        "NO_VALID_CLAIM",
    }
    assert expected <= {item.name for item in OutcomeCode}
