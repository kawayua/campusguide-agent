from __future__ import annotations

from campusguide.agents.advisor import CitationDraft, ClaimDraft
from campusguide.domain.enums import BlockType
from campusguide.verification.claims import ClaimValidator
from tests.w2_helpers import make_article, make_evidence


def _draft(
    quote: str,
    article_id: str = "demo-policy@v2021#第一条",
    claim_id: str = "c1",
) -> ClaimDraft:
    return ClaimDraft(
        claim_id=claim_id,
        text="申请奖学金需要成绩优异。",
        kind="eligibility",
        citations=[CitationDraft(article_id=article_id, quote=quote)],
    )


def test_valid_quote_is_located_and_fields_filled() -> None:
    article = make_article(date_confidence="low")
    evidence = [make_evidence(article)]
    result = ClaimValidator().validate([_draft("学习成绩优异，品行端正")], evidence)

    assert len(result.claims) == 1
    citation = result.claims[0].citations[0]
    assert citation.article_id == article.article_id
    assert citation.part_no == article.part_no
    assert citation.block_type is BlockType.ARTICLE
    assert citation.date_confidence == "low"
    assert citation.quote_match == "exact"
    assert citation.quote_start == article.text.find(citation.quote)
    assert citation.quote_end > citation.quote_start
    assert result.dropped == []
    assert result.dropped_citations == []


def test_quote_can_span_whitespace_differences() -> None:
    article = make_article(text="申请条件：\n（一）成绩优异；\n（二）品行端正。")
    result = ClaimValidator().validate(
        [_draft("（一）成绩优异；（二）品行端正")], [make_evidence(article)]
    )
    assert len(result.claims) == 1


def test_quote_span_maps_back_to_original_text() -> None:
    article = make_article(text="申请条件：\n（一）成绩优异；\n（二）品行端正。")
    result = ClaimValidator().validate(
        [_draft("（一）成绩优异；（二）品行端正")], [make_evidence(article)]
    )
    citation = result.claims[0].citations[0]
    assert citation.quote_match == "normalized"
    assert article.text[citation.quote_start : citation.quote_end] == (
        "（一）成绩优异；\n（二）品行端正"
    )


def test_citation_not_in_evidence_is_dropped() -> None:
    article = make_article()
    result = ClaimValidator().validate(
        [_draft("学习成绩优异，品行端正", article_id="ghost@v1#第一条")],
        [make_evidence(article)],
    )
    assert result.claims == []
    assert result.dropped[0]["reason"] == "NO_VALID_CITATION"
    assert result.dropped_citations[0]["reason"] == "CITATION_NOT_IN_EVIDENCE"


def test_quote_not_locatable_and_ellipsis_are_rejected() -> None:
    article = make_article()
    result = ClaimValidator().validate(
        [
            _draft("这句话不在原文中", claim_id="c1"),
            _draft("学习成绩优异…品行端正", claim_id="c2"),
        ],
        [make_evidence(article)],
    )
    assert result.claims == []
    reasons = {item["reason"] for item in result.dropped_citations}
    assert "QUOTE_MISMATCH" in reasons
    assert "QUOTE_ELLIPSIS" in reasons


def test_short_quote_is_rejected() -> None:
    article = make_article()
    result = ClaimValidator().validate([_draft("成绩")], [make_evidence(article)])
    assert result.claims == []
    assert result.dropped_citations[0]["reason"] == "QUOTE_TOO_SHORT"


def test_multi_part_article_uses_the_part_where_quote_lives() -> None:
    part1 = make_article(
        article_id="multi@v1#第九条",
        part_no=1,
        text="国家奖学金申请条件：二年级及以上。",
    )
    part2 = make_article(
        article_id="multi@v1#第九条",
        part_no=2,
        text="成绩排名位于前百分之十，可以申请。",
    )
    result = ClaimValidator().validate(
        [_draft("成绩排名位于前百分之十", article_id="multi@v1#第九条")],
        [make_evidence(part1, 0.9, 1), make_evidence(part2, 0.8, 2)],
    )
    assert len(result.claims) == 1
    assert result.claims[0].citations[0].part_no == 2


def test_duplicate_claim_id_and_too_many_claims() -> None:
    article = make_article()
    drafts = [
        _draft("学习成绩优异，品行端正"),
        _draft("学习成绩优异，品行端正"),
        _draft("学习成绩优异，品行端正"),
        _draft("学习成绩优异，品行端正"),
        _draft("学习成绩优异，品行端正"),
        _draft("学习成绩优异，品行端正"),
    ]
    result = ClaimValidator().validate(drafts, [make_evidence(article)])
    reasons = {item["reason"] for item in result.dropped}
    assert "DUPLICATE_CLAIM_ID" in reasons
    assert "TOO_MANY_CLAIMS" in reasons
    assert len(result.claims) <= 5
