"""Follow-up 2026-10-30: supplement rounds must not grow evidence without bound."""

from __future__ import annotations

from datetime import date

from campusguide.agents.graph import GraphNodes, _merge_supplement_evidence
from campusguide.agents.plan import Plan, QueryPlanItem, QuestionUnit
from campusguide.agents.retriever import RetrievalBundle
from campusguide.domain.models import Citation, Claim
from campusguide.retrieval.budget import evidence_cost
from tests.w2_helpers import make_article, make_evidence


def _evidence(doc_id: str, suffix: str, *, text: str = "课程安排。", rank: int = 1):
    article = make_article(
        article_id=f"{doc_id}@v2024#{suffix}",
        doc_id=doc_id,
        doc_title=f"{doc_id}培养方案",
        article_no=suffix,
        text=text,
    )
    return make_evidence(article, rank=rank)


def _claim_for(item) -> Claim:
    article = item.article
    citation = Citation(
        article_id=article.article_id,
        part_no=article.part_no,
        quote=article.text,
        quote_start=0,
        quote_end=len(article.text),
        doc_title=article.doc_title,
        article_no=article.article_no,
        effective_from=article.effective_from,
        source_url=article.source_url,
    )
    return Claim(claim_id="c1", text="既有结论。", citations=[citation])


def test_supplement_keeps_cited_and_one_evidence_per_document() -> None:
    a1 = _evidence("a", "一", rank=1)
    a2 = _evidence("a", "二", rank=2)
    b1 = _evidence("b", "一", rank=3)
    additions = [
        _evidence("a", "三", rank=1),
        _evidence("c", "一", rank=2),
        _evidence("d", "一", rank=3),
        _evidence("d", "二", rank=4),
    ]
    merged, warnings = _merge_supplement_evidence(
        [a1, a2, b1],
        additions,
        max_evidence=4,
        token_budget=100000,
        cited_keys={a1.key},
    )
    assert len(merged) == 4
    assert [item.key for item in merged] == [a1.key, b1.key, additions[1].key, additions[2].key]
    assert {item.article.doc_id for item in merged} == {"a", "b", "c", "d"}
    # 新比较对象 c/d 在重复文档 a 的额外行之前保留。
    assert any(
        warning.startswith(f"SUPPLEMENT_BUDGET_SKIP:{a2.article.article_id}")
        for warning in warnings
    )


def test_supplement_token_budget_is_hard_and_audited() -> None:
    base = _evidence("a", "一", text="一" * 400)
    small = _evidence("b", "一", text="二" * 20)
    large = _evidence("c", "一", text="三" * 20)
    budget = evidence_cost(base.article) + evidence_cost(small.article) + evidence_cost(
        large.article
    ) - 1
    merged, warnings = _merge_supplement_evidence(
        [base],
        [small, large],
        max_evidence=8,
        token_budget=budget,
        cited_keys=set(),
    )
    assert [item.key for item in merged] == [base.key, small.key]
    assert any(
        warning == f"SUPPLEMENT_BUDGET_SKIP:{large.article.article_id}:p1:reason=token"
        for warning in warnings
    )


def test_cited_evidence_survives_even_when_count_is_tight() -> None:
    cited = _evidence("a", "一")
    other = _evidence("b", "一")
    merged, warnings = _merge_supplement_evidence(
        [cited, other],
        [_evidence("c", "一")],
        max_evidence=1,
        token_budget=100000,
        cited_keys={cited.key},
    )
    assert [item.key for item in merged] == [cited.key]
    assert any(w.startswith("SUPPLEMENT_BUDGET_SKIP:") for w in warnings)


class SupplementRetriever:
    def __init__(self, evidence) -> None:
        self.evidence = evidence
        self.calls = 0

    def retrieve(self, *args, **kwargs):
        self.calls += 1
        return RetrievalBundle(evidence=self.evidence, warnings=["REPAIR_RETRIEVAL"])


def test_supplement_node_applies_budget_end_to_end() -> None:
    a1 = _evidence("a", "一", rank=1)
    a2 = _evidence("a", "二", rank=2)
    b1 = _evidence("b", "一", rank=3)
    c1 = _evidence("c", "一", rank=1)
    d1 = _evidence("d", "一", rank=2)
    nodes = object.__new__(GraphNodes)
    nodes._retriever = SupplementRetriever([c1, d1])
    nodes._today = date(2026, 10, 25)
    nodes._max_evidence = 3
    nodes._token_budget = 100000
    unit = QuestionUnit(
        unit_id="u1",
        original_span="两个专业的实践课程有哪些",
        requested_aspects=["实践课程"],
    )
    plan = Plan(
        question_units=[unit],
        queries=[QueryPlanItem(query_id="q1", text="两个专业的实践课程")],
    )
    claim = _claim_for(a1)
    update = nodes.supplement(
        {
            "plan": plan,
            "unit_coverage": [{"unit_id": "u1", "status": "generation_missing"}],
            "claims": [claim],
            "evidence": [a1, a2, b1],
            "question": unit.original_span,
        }
    )
    evidence = update["evidence"]
    assert [item.key for item in evidence] == [a1.key, b1.key, c1.key]
    assert [item.rank for item in evidence] == [1, 2, 3]
    assert update["repair_verified_claims"] == [claim]
    assert any(w.startswith("SUPPLEMENT_BUDGET_SKIP:") for w in update["retrieval_warnings"])
    assert any(w.startswith("SUPPLEMENT_BUDGET_SKIP:") for w in update["trace"][-1]["budget_warnings"])
    assert update["trace"][-1]["evidence_count"] == 3
