"""A2 eval-only pass-through verifier。

跳过 W3 的 quote/fact/scope 核对，把生成层的 draft 直接转成可渲染 claim。
仅用于隔离 Verifier 贡献；不进入生产装配。没有合法 citation 的 draft 会被
结构性丢弃（claim 契约要求至少一个 citation），但这不算 W3 核验。
"""

from __future__ import annotations

from campusguide.agents.advisor import ClaimDraft, normalize_kind
from campusguide.domain.enums import OutcomeCode, VerificationDecision
from campusguide.domain.models import (
    Citation,
    Claim,
    Evidence,
    VerificationIssue,
    VerificationResult,
)
from campusguide.verification.quotes import locate_quote
from campusguide.verification.verifier import VerifyReport


class PassThroughVerifier:
    def verify(
        self,
        drafts: list[ClaimDraft],
        evidence: list[Evidence],
        *,
        scope_issues: list[VerificationIssue] | None = None,
    ) -> VerifyReport:
        evidence_by_key = {
            (item.article.article_id, item.article.part_no): item.article for item in evidence
        }
        claims: list[Claim] = []
        dropped: list[str] = []
        records: list[dict] = []
        used_ids: set[str] = set()
        for index, draft in enumerate(drafts, start=1):
            claim_id = draft.claim_id or f"c{index}"
            while claim_id in used_ids:
                claim_id = f"{claim_id}~2"
            used_ids.add(claim_id)
            citations: list[Citation] = []
            for citation in draft.citations:
                article = evidence_by_key.get((citation.article_id, 1))
                if article is None:
                    article = next(
                        (
                            item.article
                            for item in evidence
                            if item.article.article_id == citation.article_id
                        ),
                        None,
                    )
                if article is None or not citation.quote.strip():
                    continue
                span = locate_quote(article.text, citation.quote)
                start, end = (span.start, span.end) if span else (0, len(citation.quote))
                citations.append(
                    Citation(
                        article_id=article.article_id,
                        quote=citation.quote,
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
            record = {
                "claim_id": claim_id,
                "text": draft.text,
                "kind": draft.kind,
                "kept": bool(citations),
                "draft_citations": [
                    {"article_id": item.article_id, "quote": item.quote} for item in draft.citations
                ],
                "citations": [
                    {"article_id": item.article_id, "quote": item.quote} for item in citations
                ],
                "issues": [],
                "fact_coverage": {},
            }
            if not citations:
                record["dropped_reason"] = "NO_CITATION"
                dropped.append(claim_id)
                records.append(record)
                continue
            records.append(record)
            claims.append(
                Claim(
                    claim_id=claim_id,
                    text=draft.text,
                    kind=normalize_kind(draft.kind),
                    citations=citations,
                )
            )

        issues = list(scope_issues or [])
        if not drafts:
            result = VerificationResult(
                decision=VerificationDecision.ABSTAIN,
                claims=[],
                issues=issues,
                dropped_claim_ids=[],
                reason_code=OutcomeCode.NO_VALID_CLAIM,
            )
        elif not claims:
            result = VerificationResult(
                decision=VerificationDecision.ABSTAIN,
                claims=[],
                issues=issues,
                dropped_claim_ids=dropped,
                reason_code=OutcomeCode.UNVERIFIABLE,
            )
        elif dropped:
            result = VerificationResult(
                decision=VerificationDecision.PARTIAL,
                claims=claims,
                issues=issues,
                dropped_claim_ids=dropped,
                reason_code=OutcomeCode.PARTIAL_ANSWER,
            )
        else:
            result = VerificationResult(
                decision=VerificationDecision.PASS,
                claims=claims,
                issues=issues,
                dropped_claim_ids=[],
                reason_code=None,
            )
        return VerifyReport(result=result, records=records, check=None)
