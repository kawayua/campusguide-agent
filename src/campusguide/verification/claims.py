"""claim / citation 确定性校验。

W2 只校验：article_id 来自本轮 Evidence、quote 是原文连续片段、字段补全。
数字/日期覆盖与语义蕴含留到 W3/W6。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from campusguide.agents.advisor import ClaimDraft, normalize_kind
from campusguide.domain.models import Citation, Claim, Evidence
from campusguide.domain.text import normalize_text

MIN_QUOTE_CHARS = 6
MAX_CLAIMS = 5


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


def _drop(
    result: ValidationResult,
    *,
    claim_id: str,
    reason: str,
    detail: str = "",
) -> None:
    result.dropped.append({"claim_id": claim_id, "reason": reason, "detail": detail})


class ClaimValidator:
    def __init__(self, *, min_quote_chars: int = MIN_QUOTE_CHARS, max_claims: int = MAX_CLAIMS):
        self._min_quote_chars = min_quote_chars
        self._max_claims = max_claims

    def validate(self, drafts: list[ClaimDraft], evidence: list[Evidence]) -> ValidationResult:
        result = ValidationResult()
        by_article: dict[str, list[Evidence]] = {}
        for item in evidence:
            by_article.setdefault(item.article.article_id, []).append(item)

        if len(drafts) > self._max_claims:
            for index, extra in enumerate(drafts[self._max_claims :], start=self._max_claims + 1):
                _drop(
                    result,
                    claim_id=extra.claim_id or f"c{index}",
                    reason="TOO_MANY_CLAIMS",
                    detail=f"W2 最多保留 {self._max_claims} 条 claim",
                )
            drafts = drafts[: self._max_claims]

        reserved_ids = {draft.claim_id for draft in drafts if draft.claim_id}
        used_ids: set[str] = set()
        next_index = 1
        for draft in drafts:
            claim_id = draft.claim_id
            if not claim_id:
                while f"c{next_index}" in reserved_ids or f"c{next_index}" in used_ids:
                    next_index += 1
                claim_id = f"c{next_index}"
                next_index += 1
            elif claim_id in used_ids:
                _drop(result, claim_id=claim_id, reason="DUPLICATE_CLAIM_ID")
                continue
            used_ids.add(claim_id)

            text = draft.text.strip()
            if not text:
                _drop(result, claim_id=claim_id, reason="EMPTY_CLAIM_TEXT")
                continue

            citations: list[Citation] = []
            seen_spans: set[tuple[str, int, int]] = set()
            for citation_draft in draft.citations:
                quoted = citation_draft.quote.strip()
                normalized_quote = normalize_text(quoted)
                if len(normalized_quote) < self._min_quote_chars:
                    result.dropped_citations.append(
                        {
                            "claim_id": claim_id,
                            "article_id": citation_draft.article_id,
                            "reason": "QUOTE_TOO_SHORT",
                        }
                    )
                    continue
                if "…" in quoted or "..." in quoted:
                    result.dropped_citations.append(
                        {
                            "claim_id": claim_id,
                            "article_id": citation_draft.article_id,
                            "reason": "QUOTE_ELLIPSIS",
                        }
                    )
                    continue

                candidates = by_article.get(citation_draft.article_id)
                if not candidates:
                    result.dropped_citations.append(
                        {
                            "claim_id": claim_id,
                            "article_id": citation_draft.article_id,
                            "reason": "CITATION_NOT_IN_EVIDENCE",
                        }
                    )
                    continue

                located: tuple[Evidence, int] | None = None
                for item in candidates:
                    position = item.article.text_normalized.find(normalized_quote)
                    if position >= 0:
                        located = (item, position)
                        break
                if located is None:
                    result.dropped_citations.append(
                        {
                            "claim_id": claim_id,
                            "article_id": citation_draft.article_id,
                            "reason": "QUOTE_NOT_LOCATABLE",
                        }
                    )
                    continue

                item, position = located
                span = (item.article.article_id, position, position + len(normalized_quote))
                if span in seen_spans:
                    continue
                seen_spans.add(span)
                article = item.article
                citations.append(
                    Citation(
                        article_id=article.article_id,
                        quote=quoted,
                        quote_start=position,
                        quote_end=position + len(normalized_quote),
                        doc_title=article.doc_title,
                        article_no=article.article_no,
                        part_no=article.part_no,
                        block_type=article.block_type,
                        effective_from=article.effective_from,
                        date_confidence=article.date_confidence,
                        source_url=article.source_url,
                    )
                )

            if not citations:
                _drop(result, claim_id=claim_id, reason="NO_VALID_CITATION")
                continue

            result.claims.append(
                Claim(
                    claim_id=claim_id,
                    text=text,
                    kind=normalize_kind(draft.kind),
                    citations=citations,
                )
            )
        return result
