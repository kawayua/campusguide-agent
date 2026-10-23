"""W3 核对契约：VerificationIssue / VerificationResult。

设计见 docs/superpowers/specs/2026-10-23-w3-citation-verification-abstention-design.md 第 3 节。
REVISE/CLARIFY 保留在枚举中给 W6 重写与 W5 追问，W3 不产生这两个值。
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, Field, model_validator

from campusguide.domain.enums import OutcomeCode, VerificationDecision
from campusguide.domain.models import Claim


class IssueSeverity(StrEnum):
    BLOCKING = "blocking"  # claim 不能进入答案
    WARNING = "warning"  # 保留 claim，但提示


class VerificationIssue(BaseModel):
    code: str
    severity: IssueSeverity
    claim_id: str | None = None
    article_id: str | None = None
    part_no: int | None = None
    detail: str = ""
    facts: list[str] = Field(default_factory=list)


class VerificationResult(BaseModel):
    decision: VerificationDecision
    claims: list[Claim] = Field(default_factory=list)  # 只含可渲染 claim
    issues: list[VerificationIssue] = Field(default_factory=list)
    reason_code: OutcomeCode | None = None
    dropped_claim_ids: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _decision_consistent_with_issues(self) -> VerificationResult:
        blocking = [issue for issue in self.issues if issue.severity is IssueSeverity.BLOCKING]
        if self.decision is VerificationDecision.PASS:
            if blocking:
                raise ValueError("PASS 不能包含 blocking issue")
        elif self.decision is VerificationDecision.PARTIAL:
            if not self.claims or not self.dropped_claim_ids:
                raise ValueError("PARTIAL 必须至少保留一条 claim 且至少丢弃一条 claim")
        elif self.decision is VerificationDecision.ABSTAIN:
            if self.claims:
                raise ValueError("ABSTAIN 不能携带可渲染 claim")
            if self.reason_code is None:
                raise ValueError("ABSTAIN 必须带 reason_code")
        return self

    @property
    def partial(self) -> bool:
        return self.decision is VerificationDecision.PARTIAL
