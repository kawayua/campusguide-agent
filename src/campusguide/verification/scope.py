"""H5 受众 / H6 版本 scope 过滤。

所有 scope 问题都是 ``EVIDENCE + WARNING``：单条 evidence 被剔除，但仍有
有效 evidence 时继续作答；全部被剔除才进入拒答原因码。
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date

from campusguide.domain.enums import (
    ArticleStatus,
    Audience,
    IssueScope,
    IssueSeverity,
    OutcomeCode,
)
from campusguide.domain.audience_match import audience_matches
from campusguide.domain.models import Evidence, VerificationIssue


@dataclass
class ScopeResult:
    evidence: list[Evidence] = field(default_factory=list)
    issues: list[VerificationIssue] = field(default_factory=list)
    reason_code: OutcomeCode | None = None


def _evidence_issue(code: str, evidence: Evidence, detail: str) -> VerificationIssue:
    article = evidence.article
    return VerificationIssue(
        code=code,
        scope=IssueScope.EVIDENCE,
        severity=IssueSeverity.WARNING,
        article_id=article.article_id,
        part_no=article.part_no,
        detail=detail,
    )


def _version_valid(
    evidence: list[Evidence], as_of: date
) -> tuple[list[Evidence], list[VerificationIssue]]:
    kept: list[Evidence] = []
    issues: list[VerificationIssue] = []
    for item in evidence:
        article = item.article
        if article.status is not ArticleStatus.ACTIVE:
            issues.append(
                _evidence_issue(
                    "VERSION_MISMATCH",
                    item,
                    f"版本状态不是 active：{article.status.value}",
                )
            )
            continue
        if article.effective_from > as_of:
            issues.append(_evidence_issue("VERSION_MISMATCH", item, "尚未生效"))
            continue
        if article.effective_to is not None and article.effective_to < as_of:
            issues.append(_evidence_issue("VERSION_MISMATCH", item, "已过有效期"))
            continue
        kept.append(item)
    return kept, issues


def _drop_conflicts(
    evidence: list[Evidence],
) -> tuple[list[Evidence], list[VerificationIssue]]:
    by_doc: dict[str, list[Evidence]] = defaultdict(list)
    for item in evidence:
        by_doc[item.article.doc_id].append(item)

    conflicted_docs: set[str] = set()
    for doc_id, items in by_doc.items():
        versions_by_date: dict[date, set[str]] = defaultdict(set)
        for item in items:
            versions_by_date[item.article.effective_from].add(item.article.version_id)
        if any(len(versions) > 1 for versions in versions_by_date.values()):
            conflicted_docs.add(doc_id)

    if not conflicted_docs:
        return list(evidence), []

    kept: list[Evidence] = []
    issues: list[VerificationIssue] = []
    for item in evidence:
        if item.article.doc_id in conflicted_docs:
            issues.append(
                _evidence_issue(
                    "VERSION_CONFLICT",
                    item,
                    "同一文档的多个 active 版本共享生效日，无法确定适用版本",
                )
            )
        else:
            kept.append(item)
    return kept, issues


def _mixed_version_issues(evidence: list[Evidence]) -> list[VerificationIssue]:
    by_doc: dict[str, list[Evidence]] = defaultdict(list)
    for item in evidence:
        by_doc[item.article.doc_id].append(item)

    issues: list[VerificationIssue] = []
    for items in by_doc.values():
        effective_dates = {item.article.effective_from for item in items}
        if len(effective_dates) <= 1:
            continue
        versions = sorted({item.article.version_id for item in items})
        issues.append(
            _evidence_issue(
                "MIXED_VERSIONS",
                items[0],
                "同一文档存在多个生效日版本：" + "、".join(versions),
            )
        )
    return issues


def _filter_audience(
    evidence: list[Evidence],
    audience: Audience | None,
) -> tuple[list[Evidence], list[VerificationIssue], bool]:
    effective = None if audience in (None, Audience.ALL) else audience
    if effective is None:
        return (
            list(evidence),
            [
                VerificationIssue(
                    code="AUDIENCE_UNDETERMINED",
                    scope=IssueScope.EVIDENCE,
                    severity=IssueSeverity.WARNING,
                    detail="未指定受众，不做硬过滤",
                )
            ],
            False,
        )

    kept: list[Evidence] = []
    issues: list[VerificationIssue] = []
    for item in evidence:
        audiences = set(item.article.audience)
        if audience_matches(audiences, effective):
            kept.append(item)
        else:
            issues.append(
                _evidence_issue(
                    "AUDIENCE_MISMATCH",
                    item,
                    f"evidence 受众不包含 {effective.value}",
                )
            )
    return kept, issues, not kept


def apply_scope(
    evidence: list[Evidence],
    *,
    audience: Audience | None = None,
    as_of: date | None = None,
) -> ScopeResult:
    """按版本 → 冲突 → 受众顺序过滤 evidence，并给出全部剔除时的原因码。"""
    if not evidence:
        return ScopeResult(reason_code=OutcomeCode.NO_EVIDENCE)

    as_of = as_of or date.today()
    version_valid, issues = _version_valid(evidence, as_of)
    if not version_valid:
        return ScopeResult(issues=issues, reason_code=OutcomeCode.VERSION_MISMATCH)

    version_valid, conflict_issues = _drop_conflicts(version_valid)
    issues.extend(conflict_issues)
    if not version_valid:
        return ScopeResult(issues=issues, reason_code=OutcomeCode.VERSION_CONFLICT)

    issues.extend(_mixed_version_issues(version_valid))

    audience_valid, audience_issues, all_filtered = _filter_audience(version_valid, audience)
    issues.extend(audience_issues)
    if all_filtered:
        return ScopeResult(issues=issues, reason_code=OutcomeCode.UNVERIFIABLE)

    return ScopeResult(evidence=audience_valid, issues=issues)
