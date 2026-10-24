"""W3 v2 核对决策：claim 级校验 + PASS / PARTIAL / ABSTAIN。

决策算法固定（v2 第 4.2 节）：
- 零 draft → ABSTAIN(NO_VALID_CLAIM)；
- 有 draft 但全部被丢弃 → ABSTAIN(UNVERIFIABLE)；
- 部分丢弃 → PARTIAL(PARTIAL_ANSWER)；
- 无丢弃 → PASS。

决策层不调用 LLM，也不修改 W2 的检索/生成主路径。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from campusguide.agents.advisor import ClaimDraft
from campusguide.domain.enums import (
    IssueScope,
    IssueSeverity,
    OutcomeCode,
    VerificationDecision,
)
from campusguide.domain.models import Claim, Evidence, VerificationIssue, VerificationResult
from campusguide.verification.claims import ClaimCheck, ClaimValidator


def _blocking_claim_ids(issues: list[VerificationIssue]) -> set[str]:
    blocking: set[str] = set()
    for issue in issues:
        if issue.scope is not IssueScope.CLAIM or issue.severity is not IssueSeverity.BLOCKING:
            continue
        if not issue.claim_id:
            raise ValueError("CLAIM + BLOCKING issue 必须带 claim_id")
        blocking.add(issue.claim_id)
    return blocking


def decide(
    claims: list[Claim],
    issues: list[VerificationIssue],
    dropped_claim_ids: list[str],
    *,
    drafts_total: int,
) -> VerificationResult:
    """按固定顺序生成本轮核对结果，并守住 blocking/PASS 不变量。"""
    blocking_ids = _blocking_claim_ids(issues)
    dropped_ids = list(dict.fromkeys(dropped_claim_ids))
    if not blocking_ids <= set(dropped_ids):
        raise ValueError("CLAIM + BLOCKING issue 必须出现在 dropped_claim_ids 中")
    if blocking_ids & {claim.claim_id for claim in claims}:
        raise ValueError("被 CLAIM + BLOCKING 标记的 claim 不得继续渲染")

    if drafts_total == 0:
        return VerificationResult(
            decision=VerificationDecision.ABSTAIN,
            claims=[],
            issues=list(issues),
            dropped_claim_ids=[],
            reason_code=OutcomeCode.NO_VALID_CLAIM,
        )
    if not claims:
        return VerificationResult(
            decision=VerificationDecision.ABSTAIN,
            claims=[],
            issues=list(issues),
            dropped_claim_ids=dropped_ids,
            reason_code=OutcomeCode.UNVERIFIABLE,
        )
    if dropped_ids:
        return VerificationResult(
            decision=VerificationDecision.PARTIAL,
            claims=list(claims),
            issues=list(issues),
            dropped_claim_ids=dropped_ids,
            reason_code=OutcomeCode.PARTIAL_ANSWER,
        )
    return VerificationResult(
        decision=VerificationDecision.PASS,
        claims=list(claims),
        issues=list(issues),
        dropped_claim_ids=[],
        reason_code=None,
    )


@dataclass
class VerifyReport:
    result: VerificationResult
    records: list[dict] = field(default_factory=list)
    check: ClaimCheck | None = None


class Verifier:
    """把 claim 级校验与决策算法组合成一次可审计的核对。"""

    def __init__(self, validator: ClaimValidator | None = None) -> None:
        self._validator = validator or ClaimValidator()

    @property
    def validator(self) -> ClaimValidator:
        return self._validator

    def verify(
        self,
        drafts: list[ClaimDraft],
        evidence: list[Evidence],
        *,
        scope_issues: list[VerificationIssue] | None = None,
    ) -> VerifyReport:
        check = self._validator.check(drafts, evidence)
        issues = [*(scope_issues or []), *check.issues]
        result = decide(
            check.claims,
            issues,
            check.dropped_claim_ids,
            drafts_total=len(drafts),
        )
        return VerifyReport(result=result, records=check.records, check=check)
