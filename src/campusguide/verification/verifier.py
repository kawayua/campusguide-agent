"""W3 确定性 Verifier：范围检查（H5/H6）+ claim 级硬校验（H4/H10）。

H1/H3/H7 由 W2 的 ClaimValidator 提供（citation ID、quote span、citation 必填），
本模块调用它并追加 fact coverage、输出隐私与 issue 记录，不复制其逻辑。
语义蕴含与重写循环不在 W3 范围（W6）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

from campusguide.agents.advisor import ClaimDraft
from campusguide.domain.enums import ArticleStatus, Audience, OutcomeCode, VerificationDecision
from campusguide.domain.models import Evidence
from campusguide.ingest.redact import redact_text
from campusguide.verification.claims import ClaimValidator, ValidationResult
from campusguide.verification.facts import extract_facts, uncovered_facts
from campusguide.verification.issues import IssueSeverity, VerificationIssue, VerificationResult

# W2 dropped 记录 -> issue code；code 语义与 7 节硬校验矩阵一致
_DROP_ISSUE_CODES = {
    "QUOTE_TOO_SHORT": "QUOTE_TOO_SHORT",
    "QUOTE_ELLIPSIS": "QUOTE_ELLIPSIS",
    "QUOTE_MISMATCH": "QUOTE_MISMATCH",
    "CITATION_NOT_IN_EVIDENCE": "CITATION_NOT_IN_EVIDENCE",
    "NO_VALID_CITATION": "MISSING_CITATION",
    "EMPTY_CLAIM_TEXT": "EMPTY_CLAIM_TEXT",
    "DUPLICATE_CLAIM_ID": "DUPLICATE_CLAIM_ID",
    "TOO_MANY_CLAIMS": "TOO_MANY_CLAIMS",
}


@dataclass
class ScopeResult:
    """Evidence 范围检查结果：保留的 evidence + issue + 拒答原因。"""

    evidence: list[Evidence] = field(default_factory=list)
    issues: list[VerificationIssue] = field(default_factory=list)
    abstain_reason: OutcomeCode | None = None

    @property
    def abstain(self) -> bool:
        return self.abstain_reason is not None


class DeterministicVerifier:
    def __init__(
        self,
        *,
        validator: ClaimValidator | None = None,
    ) -> None:
        self._validator = validator or ClaimValidator()

    # ---------- H5/H6：Evidence 范围检查 ----------

    def check_scope(
        self,
        evidence: list[Evidence],
        audience: Audience | None,
        as_of: date,
    ) -> ScopeResult:
        issues: list[VerificationIssue] = []
        if not evidence:
            return ScopeResult(evidence=[], issues=issues)

        if all(item.article.status is ArticleStatus.REPEALED for item in evidence):
            return ScopeResult(
                evidence=[],
                issues=issues,
                abstain_reason=OutcomeCode.ONLY_REPEALED,
            )

        in_window: list[Evidence] = []
        for item in evidence:
            article = item.article
            effective_to = article.effective_to
            if (
                article.status is ArticleStatus.ACTIVE
                and article.effective_from <= as_of
                and (effective_to is None or effective_to >= as_of)
            ):
                in_window.append(item)
                continue
            issues.append(
                VerificationIssue(
                    code="VERSION_MISMATCH",
                    severity=IssueSeverity.BLOCKING,
                    article_id=article.article_id,
                    part_no=article.part_no,
                    detail=(
                        f"status={article.status.value} "
                        f"effective={article.effective_from.isoformat()}.."
                        f"{effective_to.isoformat() if effective_to else 'None'} as_of={as_of.isoformat()}"
                    ),
                )
            )
        if not in_window:
            return ScopeResult(
                evidence=[], issues=issues, abstain_reason=OutcomeCode.VERSION_MISMATCH
            )

        versions_by_doc: dict[str, list[Evidence]] = {}
        for item in in_window:
            versions_by_doc.setdefault(item.article.doc_id, []).append(item)
        conflicts: set[str] = set()
        multi_version_docs: set[str] = set()
        for doc_id, items in versions_by_doc.items():
            versions = {item.article.version_id for item in items}
            if len(versions) <= 1:
                continue
            # 同一 doc 多版本并存：只有生效日相同（平局）才算无法裁决的冲突。
            # 不同年份的培养方案等"届别版本"按 W3 口径保留，W4 接版本解析器。
            effective_dates = {item.article.effective_from for item in items}
            if len(effective_dates) == 1:
                conflicts.add(doc_id)
            else:
                multi_version_docs.add(doc_id)
        if conflicts:
            issues.append(
                VerificationIssue(
                    code="VERSION_CONFLICT",
                    severity=IssueSeverity.BLOCKING,
                    detail="同一 doc_id 存在生效日相同的多个现行版本："
                    + ", ".join(sorted(conflicts)),
                )
            )
            return ScopeResult(
                evidence=[], issues=issues, abstain_reason=OutcomeCode.VERSION_CONFLICT
            )
        if multi_version_docs:
            issues.append(
                VerificationIssue(
                    code="MIXED_VERSIONS",
                    severity=IssueSeverity.WARNING,
                    detail=(
                        "同一 doc_id 并存多个届别版本，按现行证据作答；完整解析留 W4："
                        + ", ".join(sorted(multi_version_docs))
                    ),
                )
            )

        if audience is None or audience == Audience.ALL:
            issues.append(
                VerificationIssue(
                    code="AUDIENCE_UNDETERMINED",
                    severity=IssueSeverity.WARNING,
                    detail="未指定受众，引用卡片展示原文 audience",
                )
            )
            return ScopeResult(evidence=in_window, issues=issues)

        kept: list[Evidence] = []
        for item in in_window:
            article = item.article
            if audience in article.audience or Audience.ALL in article.audience:
                kept.append(item)
                continue
            issues.append(
                VerificationIssue(
                    code="AUDIENCE_MISMATCH",
                    severity=IssueSeverity.BLOCKING,
                    article_id=article.article_id,
                    part_no=article.part_no,
                    detail=f"audience={[a.value for a in article.audience]} 请求={audience.value}",
                )
            )
        if not kept:
            return ScopeResult(evidence=[], issues=issues, abstain_reason=OutcomeCode.UNVERIFIABLE)
        return ScopeResult(evidence=kept, issues=issues)

    # ---------- H4/H10 + issue 汇总 ----------

    def verify(
        self,
        drafts: list[ClaimDraft],
        evidence: list[Evidence],
        audience: Audience | None,
        as_of: date,
    ) -> VerificationResult:
        validation: ValidationResult = self._validator.validate(drafts, evidence)
        issues: list[VerificationIssue] = []
        dropped_ids: list[str] = []

        for record in validation.dropped:
            claim_id = record.get("claim_id")
            code = _DROP_ISSUE_CODES.get(record.get("reason", ""), record.get("reason", "UNKNOWN"))
            # 超出 5 条上限是裁剪，不是"未通过核对"：仍不渲染，但记 warning，
            # 不触发 PARTIAL_ANSWER 的省略提示。
            severity = (
                IssueSeverity.WARNING if code == "TOO_MANY_CLAIMS" else IssueSeverity.BLOCKING
            )
            if severity is IssueSeverity.BLOCKING and claim_id and claim_id not in dropped_ids:
                dropped_ids.append(claim_id)
            issues.append(
                VerificationIssue(
                    code=code,
                    severity=severity,
                    claim_id=claim_id,
                    detail=record.get("detail", ""),
                )
            )
        for record in validation.dropped_citations:
            claim_id = record.get("claim_id")
            code = _DROP_ISSUE_CODES.get(record.get("reason", ""), record.get("reason", "UNKNOWN"))
            issues.append(
                VerificationIssue(
                    code=code,
                    severity=IssueSeverity.BLOCKING,
                    claim_id=claim_id,
                    article_id=record.get("article_id"),
                    detail="citation 被丢弃",
                )
            )

        kept_claims = []
        for claim in validation.claims:
            claim_issues = self._check_claim(claim.text, claim.claim_id, claim.citations)
            blocking = [item for item in claim_issues if item.severity is IssueSeverity.BLOCKING]
            issues.extend(claim_issues)
            if blocking:
                dropped_ids.append(claim.claim_id)
            else:
                kept_claims.append(claim)

        if kept_claims and dropped_ids:
            decision = VerificationDecision.PARTIAL
            reason_code = OutcomeCode.PARTIAL_ANSWER
        elif kept_claims:
            decision = VerificationDecision.PASS
            reason_code = None
        else:
            decision = VerificationDecision.ABSTAIN
            # 模型零 claims 归 NO_VALID_CLAIM；有 claims 但全部失败归 UNVERIFIABLE
            reason_code = OutcomeCode.NO_VALID_CLAIM if not drafts else OutcomeCode.UNVERIFIABLE

        return VerificationResult(
            decision=decision,
            claims=kept_claims,
            issues=issues,
            reason_code=reason_code,
            dropped_claim_ids=dropped_ids,
        )

    def _check_claim(self, text: str, claim_id: str, citations) -> list[VerificationIssue]:
        issues: list[VerificationIssue] = []

        # H4：fact coverage
        facts = extract_facts(text)
        if facts:
            quotes = [citation.quote for citation in citations]
            uncovered = uncovered_facts(facts, quotes)
            if uncovered:
                issues.append(
                    VerificationIssue(
                        code="NUMBER_NOT_SUPPORTED",
                        severity=IssueSeverity.BLOCKING,
                        claim_id=claim_id,
                        detail="数字/日期/期限未出现在本轮引用 quote 中",
                        facts=[fact.canonical for fact in uncovered],
                    )
                )

        # H10：输出隐私（设计 6.4 只拦手机号/身份证/学号/个人邮箱；
        # 部门公开座机、@scau.edu.cn 官方邮箱、公开条款里的联系人姓名放行）
        privacy_keys: list[str] = []
        scan_targets = [text, *(citation.quote for citation in citations)]
        for target in scan_targets:
            counts = redact_text(target).counts
            privacy_keys.extend(
                key
                for key in counts
                if key in {"id_card", "mobile", "student_id", "personal_email"}
            )
        if privacy_keys:
            issues.append(
                VerificationIssue(
                    code="PRIVACY_VIOLATION",
                    severity=IssueSeverity.BLOCKING,
                    claim_id=claim_id,
                    detail="输出含手机号/身份证/学号/个人邮箱/联系人姓名",
                    facts=sorted(set(privacy_keys)),
                )
            )
        return issues
