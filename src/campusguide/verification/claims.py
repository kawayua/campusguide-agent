"""claim / citation 确定性校验。

W2 只校验：article_id 来自本轮 Evidence、quote 是原文连续片段、字段补全。
W3 起 ``Citation.quote_start/quote_end`` 是 ``article.text`` 的原文偏移；
fact 覆盖、隐私与最终决策分别在 facts/scope/verifier 层完成。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from campusguide.agents.advisor import ClaimDraft, normalize_kind
from campusguide.domain.enums import IssueScope, IssueSeverity
from campusguide.domain.models import Citation, Claim, Evidence, VerificationIssue
from campusguide.domain.text import normalize_text
from campusguide.verification.facts import check_fact_coverage
from campusguide.verification.privacy import privacy_issue, scan_privacy
from campusguide.verification.quotes import locate_quote

MIN_QUOTE_CHARS = 6
MAX_CLAIMS = 5

_LEGACY_CITATION_REASONS = {
    "QUOTE_TOO_SHORT": "QUOTE_TOO_SHORT",
    "QUOTE_ELLIPSIS": "QUOTE_ELLIPSIS",
    "CITATION_NOT_IN_EVIDENCE": "CITATION_NOT_IN_EVIDENCE",
    "QUOTE_MISMATCH": "QUOTE_NOT_LOCATABLE",
}


@dataclass
class ValidationResult:
    claims: list[Claim] = field(default_factory=list)
    dropped: list[dict[str, str]] = field(default_factory=list)
    dropped_citations: list[dict[str, str]] = field(default_factory=list)

    @property
    def claims_total(self) -> int:
        return len(self.claims) + len(
            {item["claim_id"] for item in self.dropped if item.get("claim_id")}
        )


@dataclass
class ClaimCheck:
    """claim 级核对的完整结果，供 Verifier、trace 与评测复用。"""

    claims: list[Claim] = field(default_factory=list)
    issues: list[VerificationIssue] = field(default_factory=list)
    dropped_claim_ids: list[str] = field(default_factory=list)
    dropped: list[dict[str, str]] = field(default_factory=list)
    dropped_citations: list[dict[str, str]] = field(default_factory=list)
    records: list[dict] = field(default_factory=list)


def _claim_issue(
    code: str,
    claim_id: str,
    *,
    detail: str = "",
    severity: IssueSeverity = IssueSeverity.BLOCKING,
    facts: list[str] | None = None,
) -> VerificationIssue:
    return VerificationIssue(
        code=code,
        scope=IssueScope.CLAIM,
        severity=severity,
        claim_id=claim_id,
        detail=detail,
        facts=facts or [],
    )


def _citation_issue(
    code: str,
    *,
    claim_id: str,
    article_id: str,
    detail: str = "",
) -> VerificationIssue:
    return VerificationIssue(
        code=code,
        scope=IssueScope.CITATION,
        severity=IssueSeverity.WARNING,
        claim_id=claim_id,
        article_id=article_id or None,
        detail=detail,
    )


def _assign_ids(drafts: list[ClaimDraft]) -> list[str]:
    """为每个 draft 生成稳定且互不冲突的内部 claim_id。"""
    reserved = {draft.claim_id for draft in drafts if draft.claim_id}
    ids: list[str] = []
    used: set[str] = set()
    next_index = 1
    for draft in drafts:
        base = draft.claim_id
        if not base:
            while f"c{next_index}" in reserved or f"c{next_index}" in used:
                next_index += 1
            base = f"c{next_index}"
            next_index += 1
        candidate = base
        suffix = 2
        while candidate in used:
            candidate = f"{base}~{suffix}"
            suffix += 1
        used.add(candidate)
        ids.append(candidate)
    return ids


class ClaimValidator:
    def __init__(self, *, min_quote_chars: int = MIN_QUOTE_CHARS, max_claims: int = MAX_CLAIMS):
        self._min_quote_chars = min_quote_chars
        self._max_claims = max_claims

    def validate(self, drafts: list[ClaimDraft], evidence: list[Evidence]) -> ValidationResult:
        check = self.check(drafts, evidence)
        return ValidationResult(
            claims=check.claims,
            dropped=check.dropped,
            dropped_citations=check.dropped_citations,
        )

    def check(self, drafts: list[ClaimDraft], evidence: list[Evidence]) -> ClaimCheck:
        check = ClaimCheck()
        by_article: dict[str, list[Evidence]] = {}
        for item in evidence:
            by_article.setdefault(item.article.article_id, []).append(item)

        ids = _assign_ids(drafts)
        reserved_ids = {draft.claim_id for draft in drafts if draft.claim_id}
        used_ids: set[str] = set()
        next_index = 1

        for index, draft in enumerate(drafts):
            claim_id = draft.claim_id
            if not claim_id:
                while f"c{next_index}" in reserved_ids or f"c{next_index}" in used_ids:
                    next_index += 1
                claim_id = f"c{next_index}"
                next_index += 1
            internal_id = ids[index]
            record: dict = {
                "claim_id": internal_id,
                "text": draft.text.strip(),
                "kind": normalize_kind(draft.kind).value,
                "kept": False,
                "draft_citations": [
                    {"article_id": citation.article_id, "quote": citation.quote}
                    for citation in draft.citations
                ],
                "citations": [],
                "issues": [],
                "dropped_reason": None,
            }
            check.records.append(record)

            if index >= self._max_claims:
                issue = _claim_issue(
                    "TOO_MANY_CLAIMS",
                    internal_id,
                    detail=f"W3 最多保留 {self._max_claims} 条 claim",
                )
                self._drop(check, record, issue, claim_id)
                continue
            if internal_id != claim_id and claim_id in used_ids:
                issue = _claim_issue("DUPLICATE_CLAIM_ID", internal_id)
                self._drop(check, record, issue, claim_id)
                continue
            used_ids.add(claim_id)

            text = draft.text.strip()
            if not text:
                issue = _claim_issue("EMPTY_CLAIM_TEXT", internal_id)
                self._drop(check, record, issue, claim_id)
                continue

            citations = self._locate_citations(
                draft,
                claim_id=internal_id,
                by_article=by_article,
                record=record,
                check=check,
            )
            if not citations:
                issue = _claim_issue(
                    "NO_VALID_CITATION",
                    internal_id,
                    detail="claim 没有可映射回原文的 citation",
                )
                self._drop(check, record, issue, claim_id)
                continue

            record["citations"] = [
                {
                    "article_id": citation.article_id,
                    "part_no": citation.part_no,
                    "quote": citation.quote,
                    "quote_start": citation.quote_start,
                    "quote_end": citation.quote_end,
                }
                for citation in citations
            ]

            privacy_categories: set[str] = set(scan_privacy(text))
            for citation in citations:
                privacy_categories.update(scan_privacy(citation.quote))
            record["privacy"] = sorted(privacy_categories)
            if privacy_categories:
                self._drop(
                    check,
                    record,
                    privacy_issue(internal_id, sorted(privacy_categories)),
                    claim_id,
                )
                continue

            coverage = check_fact_coverage(text, [citation.quote for citation in citations])
            record["fact_coverage"] = {
                "claim_facts": [fact.canonical for fact in coverage.claim_facts],
                "uncovered": [fact.canonical for fact in coverage.uncovered],
                "unparsed": coverage.unparsed,
                "quote_unparsed": coverage.quote_unparsed,
            }
            if coverage.uncovered:
                issue = _claim_issue(
                    "NUMBER_NOT_SUPPORTED",
                    internal_id,
                    detail="claim 中的业务事实未被引用 quote 覆盖",
                    facts=[fact.canonical for fact in coverage.uncovered],
                )
                self._drop(check, record, issue, claim_id)
                continue

            record["kept"] = True
            check.claims.append(
                Claim(
                    claim_id=internal_id,
                    text=text,
                    kind=normalize_kind(draft.kind),
                    citations=citations,
                )
            )
        return check

    def _locate_citations(
        self,
        draft: ClaimDraft,
        *,
        claim_id: str,
        by_article: dict[str, list[Evidence]],
        record: dict,
        check: ClaimCheck,
    ) -> list[Citation]:
        citations: list[Citation] = []
        seen_spans: set[tuple[str, int, int, int]] = set()
        for citation_draft in draft.citations:
            quoted = citation_draft.quote.strip()
            normalized_quote = normalize_text(quoted)
            if len(normalized_quote) < self._min_quote_chars:
                issue = _citation_issue(
                    "QUOTE_TOO_SHORT",
                    claim_id=claim_id,
                    article_id=citation_draft.article_id,
                    detail=f"quote 归一化后不足 {self._min_quote_chars} 字",
                )
                self._add_citation_issue(check, record, issue, citation_draft.article_id)
                continue
            if "…" in quoted or "..." in quoted:
                issue = _citation_issue(
                    "QUOTE_ELLIPSIS",
                    claim_id=claim_id,
                    article_id=citation_draft.article_id,
                    detail="quote 含省略号",
                )
                self._add_citation_issue(check, record, issue, citation_draft.article_id)
                continue

            candidates = by_article.get(citation_draft.article_id)
            if not candidates:
                issue = _citation_issue(
                    "CITATION_NOT_IN_EVIDENCE",
                    claim_id=claim_id,
                    article_id=citation_draft.article_id,
                    detail="citation.article_id 不在本轮 Evidence",
                )
                self._add_citation_issue(check, record, issue, citation_draft.article_id)
                continue

            located: tuple[Evidence, int, int] | None = None
            for item in candidates:
                span = locate_quote(item.article.text, quoted)
                if span is not None:
                    located = (item, span.start, span.end)
                    break
            if located is None:
                issue = _citation_issue(
                    "QUOTE_MISMATCH",
                    claim_id=claim_id,
                    article_id=citation_draft.article_id,
                    detail="quote 无法映射回被引 part 的 article.text",
                )
                self._add_citation_issue(check, record, issue, citation_draft.article_id)
                continue

            item, start, end = located
            article = item.article
            key = (article.article_id, article.part_no, start, end)
            if key in seen_spans:
                continue
            seen_spans.add(key)
            citations.append(
                Citation(
                    article_id=article.article_id,
                    quote=article.text[start:end],
                    quote_start=start,
                    quote_end=end,
                    doc_title=article.doc_title,
                    article_no=article.article_no,
                    part_no=article.part_no,
                    block_type=article.block_type,
                    effective_from=article.effective_from,
                    date_confidence=article.date_confidence,
                    source_url=article.source_url,
                )
            )
        return citations

    @staticmethod
    def _add_citation_issue(
        check: ClaimCheck,
        record: dict,
        issue: VerificationIssue,
        article_id: str,
    ) -> None:
        check.issues.append(issue)
        record["issues"].append(issue)
        check.dropped_citations.append(
            {
                "claim_id": issue.claim_id or "",
                "article_id": article_id,
                "reason": _LEGACY_CITATION_REASONS.get(issue.code, issue.code),
            }
        )

    @staticmethod
    def _drop(
        check: ClaimCheck,
        record: dict,
        issue: VerificationIssue,
        legacy_claim_id: str,
    ) -> None:
        check.issues.append(issue)
        record["issues"].append(issue)
        record["dropped_reason"] = issue.code
        if record["claim_id"] not in check.dropped_claim_ids:
            check.dropped_claim_ids.append(record["claim_id"])
        check.dropped.append(
            {
                "claim_id": legacy_claim_id,
                "reason": issue.code,
                "detail": issue.detail,
            }
        )
