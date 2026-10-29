"""W6 图 Verifier：W3 硬校验 + 语义核对 + H9 流程事实 + 回退决策。

第一层沿用 ``ClaimValidator``（H1/H3/H4/H5/H6/H7/H10）；第二层只对硬校验
通过的 claim 做 claim-quote 语义核对。图决策为 pass/revise/clarify/abstain，
最终仍输出 W3 ``VerificationResult``（PASS/PARTIAL/ABSTAIN）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from campusguide.agents.advisor import ClaimDraft
from campusguide.agents.plan import Clarification
from campusguide.qa.list_claim import (
    asked_entity_type,
    build_list_claim,
    claim_lists_type,
    claim_lists_type_cleanly,
)
from campusguide.verification.entities import TYPE_WORDS, items_of_type
from campusguide.domain.enums import (
    GraphDecision,
    IssueScope,
    IssueSeverity,
    OutcomeCode,
)
from campusguide.domain.models import (
    ChecklistItem,
    Claim,
    Evidence,
    VerificationIssue,
    VerificationResult,
)
from campusguide.verification.claims import ClaimValidator
from campusguide.verification.conditions import check_conditions
from campusguide.verification.semantic import (
    SemanticLabel,
    SemanticReport,
    SemanticVerifier,
)
from campusguide.verification.verifier import VerifyReport, decide

_DATE_RE = re.compile(r"(?<!\d)(20\d{2})[-年/](\d{1,2})[-月/](\d{1,2})日?(?!\d)")
_DEPT_RE = re.compile(
    r"[\u4e00-\u9fa5]{2,18}(?:学院|学生工作部|资助中心|本科生院|研究生院|"
    r"教务处|学生处|学籍科|培养科|学生工作办公室|办公室)"
)


@dataclass
class GraphVerifyReport:
    decision: GraphDecision
    hard: VerifyReport
    verification: VerificationResult | None = None
    semantic: SemanticReport | None = None
    semantic_unchecked: bool = False
    failed_claim_ids: list[str] = field(default_factory=list)
    revise_issues: list[dict] = field(default_factory=list)
    # 方案 A2：改写时必须逐项覆盖的类型清单（来自问题与被引原文）。
    revise_requirements: list[dict] = field(default_factory=list)
    issues: list[VerificationIssue] = field(default_factory=list)
    reason_code: OutcomeCode | None = None
    detail: str = ""


def _issue(
    code: str,
    claim_id: str | None,
    *,
    detail: str = "",
    scope: IssueScope = IssueScope.CLAIM,
    severity: IssueSeverity = IssueSeverity.BLOCKING,
) -> VerificationIssue:
    return VerificationIssue(
        code=code,
        scope=scope,
        severity=severity,
        claim_id=claim_id,
        detail=detail,
    )


def _norm_date(match: re.Match[str]) -> str:
    return f"{int(match.group(1)):04d}-{int(match.group(2)):02d}-{int(match.group(3)):02d}"


def _dates_in(text: str) -> set[str]:
    return {_norm_date(match) for match in _DATE_RE.finditer(text)}


class GraphVerifier:
    def __init__(
        self,
        validator: ClaimValidator | None = None,
        semantic_verifier: SemanticVerifier | None = None,
        *,
        max_revise: int = 2,
        semantic_mode: str = "strict",
    ) -> None:
        self._validator = validator or ClaimValidator()
        self._semantic = semantic_verifier
        self._max_revise = max(0, max_revise)
        self._semantic_mode = semantic_mode

    def verify(
        self,
        drafts: list[ClaimDraft],
        evidence: list[Evidence],
        *,
        scope_issues: list[VerificationIssue] | None = None,
        checklist: list[ChecklistItem] | None = None,
        revision_count: int = 0,
        clarify_count: int = 0,
        max_clarify: int = 3,
        clarification: Clarification | None = None,
        previously_verified: list[Claim] | None = None,
        question: str = "",
        retrieval_warnings: list[str] | None = None,
    ) -> GraphVerifyReport:
        # 方案 A4：问题问某类实体清单、而草稿只列了别的类型时，用原文名称
        # 补一条逐字引用型结论；它仍要过下面的全部核对。
        drafts = self._with_list_fallback(question, drafts, evidence)
        hard = self._validator.check(drafts, evidence)
        issues = [*(scope_issues or []), *hard.issues]
        if clarification is not None and clarify_count < max_clarify:
            return GraphVerifyReport(
                decision=GraphDecision.CLARIFY,
                hard=hard,
                issues=issues,
                reason_code=OutcomeCode.UNVERIFIABLE,
                detail=clarification.question,
            )

        kept = list(hard.claims)
        hard_dropped_ids = list(hard.dropped_claim_ids)
        # max_claims 是设计上限，不属于可修复的 quote/数字/引用错误；
        # 对超限 claim 发起 revise 只会浪费一次生成调用。
        too_many_ids = {
            record["claim_id"]
            for record in hard.records
            if record.get("dropped_reason") == "TOO_MANY_CLAIMS"
        }
        failed_ids = [claim_id for claim_id in hard_dropped_ids if claim_id not in too_many_ids]
        failed_issues: list[VerificationIssue] = [
            issue
            for issue in hard.issues
            if issue.scope is IssueScope.CLAIM
            and issue.severity is IssueSeverity.BLOCKING
            and issue.claim_id in failed_ids
        ]
        semantic_bad: set[str] = set()

        # R1：条件断言确定性核对（阈值方向、义务方向、角色、实体类别）。
        # 语义模型可能放过“30分钟后”写成“30分钟内”，这里独立拦截；
        # 例外遗漏只作提示，不丢弃结论。
        condition_issues, advisory_issues = self._condition_issues(kept)
        if advisory_issues:
            issues.extend(advisory_issues)
        if condition_issues:
            issues.extend(condition_issues)
            for issue in condition_issues:
                if issue.claim_id and issue.claim_id not in failed_ids:
                    failed_ids.append(issue.claim_id)
                failed_issues.append(issue)

        # H9：流程事实与 checklist 不一致即 blocking。
        h9_issues = self._flow_fact_issues(kept, checklist or [])
        collection_scanned = any(
            w.startswith("COLLECTION_SCAN:") for w in (retrieval_warnings or [])
        )
        if question and not collection_scanned and re.search(r"最近|最新", question):
            h9_issues.extend(
                _issue(
                    "TEMPORAL_COLLECTION_UNVERIFIED",
                    claim.claim_id,
                    detail="未完成已收录消息集合的日期排序，不能断言最新",
                )
                for claim in kept
                if re.search(r"最新|最近", claim.text)
            )
        if h9_issues:
            issues.extend(h9_issues)
            for issue in h9_issues:
                if issue.claim_id and issue.claim_id not in failed_ids:
                    failed_ids.append(issue.claim_id)
                failed_issues.append(issue)

        semantic: SemanticReport | None = None
        semantic_unchecked = False
        if self._semantic is None:
            semantic_unchecked = True
        else:
            previous = {claim.claim_id: claim for claim in (previously_verified or [])}
            reused = [claim for claim in kept if previous.get(claim.claim_id) == claim]
            reused_ids = {claim.claim_id for claim in reused}
            semantic = self._semantic.check(
                [claim for claim in kept if claim.claim_id not in reused_ids], evidence
            )
            if not semantic.unavailable:
                for claim in reused:
                    semantic.labels[claim.claim_id] = SemanticLabel.SUPPORTED
                    semantic.details[claim.claim_id] = (
                        "同一轮补答中保留的已核对结论；正文、引用和元数据均未改变"
                    )
            if semantic.unavailable:
                if self._semantic_mode == "strict":
                    issue = _issue(
                        "SEMANTIC_UNAVAILABLE",
                        None,
                        detail=semantic.error or "语义模型不可用",
                        scope=IssueScope.EVIDENCE,
                        severity=IssueSeverity.WARNING,
                    )
                    return GraphVerifyReport(
                        decision=GraphDecision.ABSTAIN,
                        hard=hard,
                        semantic=semantic,
                        issues=[*issues, issue],
                        reason_code=OutcomeCode.UNVERIFIABLE,
                        detail="语义核对不可用，按 strict 策略拒答",
                    )
                semantic_unchecked = True
            else:
                semantic_issues, semantic_failed = self._semantic_issues(semantic)
                issues.extend(semantic_issues)
                failed_issues.extend(semantic_issues)
                for claim_id in semantic_failed:
                    semantic_bad.add(claim_id)
                    if claim_id not in failed_ids:
                        failed_ids.append(claim_id)

        list_requirements = self._list_requirements(question, kept, evidence)
        if hard.dropped_claim_ids and not kept and revision_count < self._max_revise and failed_ids:
            # 硬校验全部失败：没有可语义核对的 claim。
            return GraphVerifyReport(
                decision=GraphDecision.REVISE,
                hard=hard,
                semantic=semantic,
                semantic_unchecked=semantic_unchecked,
                failed_claim_ids=failed_ids,
                revise_issues=[issue.model_dump(mode="json") for issue in failed_issues],
                revise_requirements=list_requirements,
                issues=issues,
                detail="硬校验失败，进入 revise",
            )
        if failed_ids and revision_count < self._max_revise:
            return GraphVerifyReport(
                decision=GraphDecision.REVISE,
                hard=hard,
                semantic=semantic,
                semantic_unchecked=semantic_unchecked,
                failed_claim_ids=failed_ids,
                revise_issues=[issue.model_dump(mode="json") for issue in failed_issues],
                revise_requirements=list_requirements,
                issues=issues,
                detail="存在可修复失败项，进入 revise",
            )

        # 修订超限或没有失败项：丢弃仍失败的 claim。
        contradicted = (
            set(semantic.contradicted_ids) if semantic and not semantic.unavailable else set()
        )
        if contradicted & {claim.claim_id for claim in kept}:
            return GraphVerifyReport(
                decision=GraphDecision.ABSTAIN,
                hard=hard,
                semantic=semantic,
                semantic_unchecked=semantic_unchecked,
                failed_claim_ids=sorted(contradicted),
                issues=issues,
                reason_code=OutcomeCode.UNVERIFIABLE,
                detail="语义核对仍为 contradicted，整题拒答",
            )

        bad_ids = set(hard_dropped_ids) | set(failed_ids) | semantic_bad
        final_claims = [claim for claim in kept if claim.claim_id not in bad_ids]
        final_issues = [issue for issue in issues]
        if semantic and semantic_unchecked:
            final_issues.append(
                _issue(
                    "SEMANTIC_UNCHECKED",
                    None,
                    detail="本次未完成语义核对，仅硬校验通过",
                    scope=IssueScope.EVIDENCE,
                    severity=IssueSeverity.WARNING,
                )
            )
        if not final_claims:
            return GraphVerifyReport(
                decision=GraphDecision.ABSTAIN,
                hard=hard,
                semantic=semantic,
                semantic_unchecked=semantic_unchecked,
                failed_claim_ids=sorted(bad_ids),
                issues=final_issues,
                reason_code=(
                    OutcomeCode.NO_VALID_CLAIM if not drafts else OutcomeCode.UNVERIFIABLE
                ),
                detail="没有通过核对的 claim",
            )
        result = decide(
            final_claims,
            final_issues,
            sorted(bad_ids),
            drafts_total=len(drafts),
        )
        return GraphVerifyReport(
            decision=GraphDecision.PASS,
            hard=hard,
            semantic=semantic,
            semantic_unchecked=semantic_unchecked,
            failed_claim_ids=sorted(bad_ids),
            issues=final_issues,
            verification=result,
            reason_code=result.reason_code,
        )

    # ---------------------------------------------------------- 方案 A4 / A2
    @staticmethod
    def _with_list_fallback(
        question: str,
        drafts: list[ClaimDraft],
        evidence: list,
    ) -> list[ClaimDraft]:
        """问题问某类清单、草稿却一项未列时，补一条逐字引用型结论。"""
        type_word = asked_entity_type(question)
        if not type_word or claim_lists_type_cleanly(drafts, type_word):
            return drafts
        claim = build_list_claim(question, evidence)
        if claim is None:
            return drafts
        if claim.claim_id in {draft.claim_id for draft in drafts}:
            return drafts
        return [*drafts, claim]

    @staticmethod
    def _list_requirements(question: str, claims: list, evidence: list) -> list[dict]:
        """改写要求：必须逐项列出的名称，以及不得混入的类型。"""
        type_word = asked_entity_type(question)
        if not type_word:
            return []
        if claim_lists_type_cleanly(claims, type_word):
            return []
        required: list[str] = []
        others: list[str] = []
        for item in evidence:
            text = item.article.text or ""
            names = items_of_type(text, type_word)
            if not names:
                continue
            required = names[:8]
            others = [
                word
                for word in TYPE_WORDS
                if word != type_word and word in text and type_word not in word
            ][:3]
            break
        if not required:
            return []
        return [
            {
                "kind": "category",
                "type_word": type_word,
                "required_items": required,
                "forbidden_types": others,
            }
        ]

    @staticmethod
    def _condition_issues(
        claims: list[Claim],
    ) -> tuple[list[VerificationIssue], list[VerificationIssue]]:
        """Deterministic condition conflicts that a language model may miss.

        Returns ``(blocking, advisory)``; a dropped exception is advisory because
        the claim is still factually supported.
        """
        blocking: list[VerificationIssue] = []
        advisory: list[VerificationIssue] = []
        for claim in claims:
            quotes = [citation.quote for citation in claim.citations if citation.quote]
            for finding in check_conditions(claim.text, quotes):
                if not finding.blocking:
                    advisory.append(
                        _issue(
                            "EXCEPTION_DROPPED",
                            claim.claim_id,
                            detail=finding.detail,
                            severity=IssueSeverity.WARNING,
                        )
                    )
                    continue
                blocking.append(
                    _issue(
                        "CONDITION_CONTRADICTED",
                        claim.claim_id,
                        detail=f"{finding.kind}: {finding.detail}",
                    )
                )
        return blocking, advisory

    @staticmethod
    def _semantic_issues(
        semantic: SemanticReport,
    ) -> tuple[list[VerificationIssue], list[str]]:
        issues: list[VerificationIssue] = []
        failed: list[str] = []
        for claim_id, label in semantic.labels.items():
            if label is SemanticLabel.SUPPORTED:
                continue
            failed.append(claim_id)
            issues.append(
                _issue(
                    f"SEMANTIC_{label.value.upper()}",
                    claim_id,
                    detail=semantic.details.get(claim_id, ""),
                )
            )
        return issues, failed

    @staticmethod
    def _flow_fact_issues(
        claims: list[Claim],
        checklist: list[ChecklistItem],
    ) -> list[VerificationIssue]:
        if not checklist:
            return []
        allowed_dates: set[str] = set()
        allowed_materials: set[str] = set()
        allowed_departments: set[str] = set()
        for item in checklist:
            if item.deadline:
                allowed_dates.update(_dates_in(item.deadline))
            if item.deadline_note:
                allowed_dates.update(_dates_in(item.deadline_note))
            if item.status_note:
                allowed_dates.update(_dates_in(item.status_note))
            allowed_materials.update(item.materials)
            if item.department:
                allowed_departments.update(
                    part.strip() for part in re.split(r"[、/,，]", item.department) if part.strip()
                )
        issues: list[VerificationIssue] = []
        for claim in claims:
            if claim.kind.value not in {"step", "material", "deadline", "department"}:
                continue
            if claim.kind.value in {"step", "deadline"}:
                dates = _dates_in(claim.text)
                if dates and not dates <= allowed_dates:
                    issues.append(
                        _issue(
                            "FLOW_FACT_MISMATCH",
                            claim.claim_id,
                            detail="claim 中的截止日期不在流程 checklist 中："
                            + "、".join(sorted(dates - allowed_dates)),
                        )
                    )
            if claim.kind.value in {"step", "department"}:
                for match in _DEPT_RE.finditer(claim.text):
                    mention = match.group(0)
                    if any(
                        part in mention or mention in part
                        for part in allowed_departments
                        if len(part) >= 4
                    ):
                        continue
                    if mention in {"学院", "学校"}:
                        continue
                    issues.append(
                        _issue(
                            "FLOW_FACT_MISMATCH",
                            claim.claim_id,
                            detail=f"claim 中的部门不在流程 checklist 中：{mention}",
                        )
                    )
                    break
            if (
                claim.kind.value == "material"
                and allowed_materials
                and not any(
                    material in claim.text or claim.text in material or material[:4] in claim.text
                    for material in allowed_materials
                )
            ):
                issues.append(
                    _issue(
                        "FLOW_FACT_MISMATCH",
                        claim.claim_id,
                        detail="claim 中的材料不在流程 checklist 中",
                    )
                )
        return issues


__all__ = ["GraphVerifier", "GraphVerifyReport"]
