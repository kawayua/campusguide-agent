"""Same-doc/version quote candidate scope for numeric fact repair."""

from __future__ import annotations

import json

from campusguide.agents.advisor import Advisor, ClaimDraft
from campusguide.agents.providers import FakeChatProvider
from campusguide.agents.quote_repair import (
    MAX_ADDITIONAL_CLAIM_CANDIDATES,
    SpanRegistry,
    find_quote_problems,
)
from campusguide.domain.models import Article, Evidence
from campusguide.retrieval.source_spans import source_spans
from campusguide.verification.claims import ClaimValidator
from tests.w2_helpers import make_article, make_evidence

DOC_ID = "demo-grad-program"
VERSION_ID = "v2099-2100"
WRONG_ARTICLE_ID = f"{DOC_ID}@{VERSION_ID}#section3"
CORRECT_ARTICLE_ID = f"{DOC_ID}@{VERSION_ID}#table.row2"
WRONG_TEXT = "三、培养环节及时间安排\n2.科研训练 | 第三学期 | 第三学期 | 4 |"
CORRECT_TEXT = "培养环节：9学分，其中科研训练4学分，其他3学分。"
CLAIM_TEXT = "参与科研训练需完成4学分。"


def _article(
    article_id: str,
    text: str,
    *,
    doc_id: str = DOC_ID,
    version_id: str = VERSION_ID,
    article_no: str = "条款",
    article_index: int = 1,
) -> Article:
    return make_article(
        article_id=article_id,
        doc_id=doc_id,
        version_id=version_id,
        doc_title="培养方案",
        article_no=article_no,
        article_index=article_index,
        text=text,
    )


def _evidence(*articles: Article) -> list[Evidence]:
    return [
        make_evidence(item, rank=index)
        for index, item in enumerate(articles, 1)
    ]


def _draft(
    *,
    article_id: str = WRONG_ARTICLE_ID,
    quote: str = WRONG_TEXT,
    text: str = CLAIM_TEXT,
) -> ClaimDraft:
    return ClaimDraft.model_validate(
        {
            "claim_id": "c1",
            "text": text,
            "kind": "other",
            "citations": [{"article_id": article_id, "quote": quote}],
        }
    )


def _catalog(
    evidence: list[Evidence], draft: ClaimDraft
) -> tuple[SpanRegistry, list[str], list]:
    problems = find_quote_problems([draft], evidence)
    assert problems
    registry = SpanRegistry.build(evidence)
    catalog = registry.ids_for_claims([draft], problems)
    return registry, catalog[problems[0].claim_id], problems


def _claims_json(*, quote_id: str = "", article_id: str = "", quote: str = "") -> str:
    citation = (
        {"quote_id": quote_id}
        if quote_id
        else {"article_id": article_id, "quote": quote}
    )
    return json.dumps(
        {
            "claims": [
                {
                    "claim_id": "c1",
                    "text": CLAIM_TEXT,
                    "kind": "other",
                    "citations": [citation],
                }
            ]
        },
        ensure_ascii=False,
    )


def test_same_doc_typed_entity_candidate_is_offered_after_originals() -> None:
    evidence = _evidence(
        _article(WRONG_ARTICLE_ID, WRONG_TEXT),
        _article(CORRECT_ARTICLE_ID, CORRECT_TEXT, article_no="table.row2"),
    )
    registry, ids, _ = _catalog(evidence, _draft())

    assert ids == ["source_1_body", "source_2_body"]
    span = registry.spans["source_2_body"]
    assert span.article_id == CORRECT_ARTICLE_ID
    assert span.quote == CORRECT_TEXT


def test_offered_ids_are_exact_existing_source_spans() -> None:
    evidence = _evidence(
        _article(WRONG_ARTICLE_ID, WRONG_TEXT),
        _article(CORRECT_ARTICLE_ID, CORRECT_TEXT, article_no="table.row2"),
    )
    expected = source_spans(evidence)
    registry, ids, _ = _catalog(evidence, _draft())

    for span_id in ids:
        assert span_id in expected
        assert registry.spans[span_id] == expected[span_id]


def test_bare_flattened_number_is_not_offered() -> None:
    bare = _article(
        f"{DOC_ID}@{VERSION_ID}#bare",
        "2.科研训练 | 第三学期 | 4 |",
    )
    evidence = _evidence(_article(WRONG_ARTICLE_ID, WRONG_TEXT), bare)
    _, ids, problems = _catalog(evidence, _draft())

    assert ids == ["source_1_body"]
    assert any("4学分" in reason for reason in problems[0].reasons)


def test_candidate_scope_excludes_other_doc_version_and_entity() -> None:
    other_doc = _article(
        f"other-program@{VERSION_ID}#req",
        "其中科研训练4学分。",
        doc_id="other-program",
    )
    other_version = _article(
        f"{DOC_ID}@v2098-2099#req",
        "其中科研训练4学分。",
        version_id="v2098-2099",
    )
    wrong_entity = _article(
        f"{DOC_ID}@{VERSION_ID}#other",
        "其中其他课程需完成4学分。",
    )
    evidence = _evidence(
        _article(WRONG_ARTICLE_ID, WRONG_TEXT),
        other_doc,
        other_version,
        wrong_entity,
    )
    _, ids, _ = _catalog(evidence, _draft())

    assert ids == ["source_1_body"]


def test_additional_candidates_are_capped() -> None:
    candidates = [
        _article(
            f"{DOC_ID}@{VERSION_ID}#c{index}",
            "其中科研训练4学分。",
            article_no=f"c{index}",
            article_index=index,
        )
        for index in range(2, 12)
    ]
    evidence = _evidence(_article(WRONG_ARTICLE_ID, WRONG_TEXT), *candidates)
    _, ids, _ = _catalog(evidence, _draft())

    assert ids[0] == "source_1_body"
    assert len(ids) == 1 + MAX_ADDITIONAL_CLAIM_CANDIDATES
    assert ids[1:] == [
        f"source_{index}_body"
        for index in range(2, 2 + MAX_ADDITIONAL_CLAIM_CANDIDATES)
    ]


def test_no_legitimate_candidate_keeps_bounded_failure() -> None:
    evidence = _evidence(_article(WRONG_ARTICLE_ID, WRONG_TEXT))
    _, ids, problems = _catalog(evidence, _draft())

    assert ids == ["source_1_body"]
    assert any("4学分" in reason for reason in problems[0].reasons)


def test_repair_offers_candidate_without_extra_provider_call() -> None:
    evidence = _evidence(
        _article(WRONG_ARTICLE_ID, WRONG_TEXT),
        _article(CORRECT_ARTICLE_ID, CORRECT_TEXT, article_no="table.row2"),
    )
    provider = FakeChatProvider(
        [
            _claims_json(article_id=WRONG_ARTICLE_ID, quote=WRONG_TEXT),
            _claims_json(quote_id="source_2_body"),
        ]
    )
    result = Advisor(provider).generate("科研训练需要修多少学分？", evidence)

    assert len(provider.calls) == 2
    assert result.quote_repair_attempted is True
    assert result.quote_repaired is True
    assert result.quote_repair_remaining == []

    repair_prompt = provider.calls[1][-1]["content"]
    catalog_section = repair_prompt.split("可用 quote_id", 1)[1]
    assert "source_2_body" in catalog_section

    check = ClaimValidator().check(result.drafts, evidence)
    assert len(check.claims) == 1
    citation = check.claims[0].citations[0]
    assert citation.article_id == CORRECT_ARTICLE_ID
    assert citation.quote == CORRECT_TEXT


def test_pdf_space_between_number_and_unit_keeps_literal_candidate_identity() -> None:
    raw = "培养环节：9 学分，其中科研训练 4 学分，其他3 学分。"
    evidence = _evidence(
        _article(WRONG_ARTICLE_ID, WRONG_TEXT),
        _article(CORRECT_ARTICLE_ID, raw, article_no="table.row2"),
    )
    registry, ids, _ = _catalog(evidence, _draft())
    assert "source_2_body" in ids
    assert registry.spans["source_2_body"].quote == raw
