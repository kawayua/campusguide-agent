from __future__ import annotations

from datetime import date

from campusguide.agents.plan import Plan, QueryPlanItem
from campusguide.agents.retriever import (
    FLOW_CITATION_UNAVAILABLE,
    PARTIAL_RETRIEVAL,
    GraphRetriever,
)
from campusguide.domain.enums import ArticleStatus, Audience, OutcomeCode
from campusguide.domain.models import ArticlePublic, PolicyFilters
from campusguide.mcp.client import SearchResponse
from campusguide.mcp.schemas import (
    GetArticleData,
    SearchArticlesData,
)
from tests.w6_helpers import ARTICLES, FakeSearchClient, article, hit


def make_plan(*queries: str) -> Plan:
    return Plan(
        queries=[
            QueryPlanItem(query_id=f"q{index}", text=text)
            for index, text in enumerate(queries, start=1)
        ]
    )


def base_filters(**kwargs) -> PolicyFilters:
    return PolicyFilters(as_of=date(2026, 10, 25), **kwargs)


def test_multi_query_merges_and_records_coverage() -> None:
    first = article("national-aid-undergrad@v2021#第九条")
    second = article("national-aid-undergrad@v2021#第十条")
    client = FakeSearchClient(
        by_query={
            "国家奖学金条件": [first.article_id, second.article_id],
            "国家励志奖学金条件": [second.article_id],
        }
    )
    retriever = GraphRetriever(client, expected_index_version="test-index")
    bundle = retriever.retrieve(make_plan("国家奖学金条件", "国家励志奖学金条件"), base_filters())
    assert bundle.reason_code is None
    ids = [item.article.article_id for item in bundle.evidence]
    assert ids.count(second.article_id) == 1
    assert set(bundle.coverage) == {"q1", "q2"}
    assert second.article_id in bundle.coverage["q1"]
    assert bundle.coverage["q2"] == [second.article_id]
    merged = next(item for item in bundle.evidence if item.article.article_id == second.article_id)
    assert set(merged.matched_queries) == {"q1", "q2"}


def test_each_query_keeps_a_hit_even_with_small_global_cap() -> None:
    first = article("national-aid-undergrad@v2021#第九条")
    second = article("national-aid-undergrad@v2021#第十条")
    client = FakeSearchClient(
        by_query={
            "q1 text": [first.article_id],
            "q2 text": [second.article_id],
        }
    )
    retriever = GraphRetriever(
        client,
        max_evidence=1,
        expected_index_version="test-index",
    )
    bundle = retriever.retrieve(make_plan("q1 text", "q2 text"), base_filters())
    assert {item.article.article_id for item in bundle.evidence} == {
        first.article_id,
        second.article_id,
    }


def test_per_doc_cap_limits_long_documents() -> None:
    ids = [
        "national-aid-undergrad@v2021#第九条",
        "national-aid-undergrad@v2021#第十条",
        "national-aid-undergrad@v2021#第七条",
        "national-aid-undergrad@v2021#第八条",
    ]
    client = FakeSearchClient(default=ids)
    retriever = GraphRetriever(
        client,
        max_evidence=8,
        max_per_doc=3,
        expected_index_version="test-index",
    )
    bundle = retriever.retrieve(make_plan("国奖条件"), base_filters())
    assert (
        len([item for item in bundle.evidence if item.article.doc_id == "national-aid-undergrad"])
        <= 3
    )


def test_flow_citation_hydration_marks_source_and_score_zero() -> None:
    citation = "national-aid-2026-info-merit@v2026#六、申请材料"
    client = FakeSearchClient()
    retriever = GraphRetriever(client, expected_index_version="test-index")
    bundle = retriever.retrieve(
        make_plan("国奖材料"),
        base_filters(),
        flow_citations=[citation],
    )
    flow_evidence = [item for item in bundle.evidence if item.source == "flow"]
    assert flow_evidence
    assert flow_evidence[0].article.article_id == citation
    assert flow_evidence[0].score == 0.0
    assert bundle.flowed is True


def test_flow_citation_unavailable_is_warning_not_fabrication() -> None:
    client = FakeSearchClient()
    retriever = GraphRetriever(client, expected_index_version="test-index")
    bundle = retriever.retrieve(
        make_plan("国奖材料"),
        base_filters(),
        flow_citations=["not-a-real-article#第一条"],
    )
    assert any(item.startswith(FLOW_CITATION_UNAVAILABLE) for item in bundle.warnings)
    assert all(item.article.article_id != "not-a-real-article#第一条" for item in bundle.evidence)


def test_partial_query_failure_keeps_other_query() -> None:
    client = FakeSearchClient(
        by_query={"国奖条件": ["national-aid-undergrad@v2021#第九条"]},
        fail_queries={"励志条件"},
    )
    retriever = GraphRetriever(client, expected_index_version="test-index")
    bundle = retriever.retrieve(make_plan("国奖条件", "励志条件"), base_filters())
    assert bundle.evidence
    assert bundle.partial is True
    assert PARTIAL_RETRIEVAL in bundle.warnings
    assert bundle.coverage.get("q2") == []


def test_all_queries_fail_returns_tool_unavailable() -> None:
    client = FakeSearchClient(fail_queries={"q1", "q2"})
    retriever = GraphRetriever(client, expected_index_version="test-index")
    bundle = retriever.retrieve(make_plan("q1", "q2"), base_filters())
    assert bundle.evidence == []
    assert bundle.reason_code is OutcomeCode.TOOL_UNAVAILABLE


def test_index_version_mismatch_abstains() -> None:
    client = FakeSearchClient(index_version="other-index")
    retriever = GraphRetriever(client, expected_index_version="test-index")
    bundle = retriever.retrieve(make_plan("国奖条件"), base_filters())
    assert bundle.reason_code is OutcomeCode.INDEX_UNAVAILABLE
    assert bundle.evidence == []


def test_empty_active_hits_repealed_probe() -> None:
    class ProbeClient(FakeSearchClient):
        def search_articles(self, request, *, timeout=None):
            if request.filters.status == [ArticleStatus.REPEALED.value]:
                return SearchResponse(
                    data=SearchArticlesData(
                        query=request.query,
                        queries=[request.query],
                        articles=[hit(article("national-aid-undergrad@v2021#第九条"))],
                        index_version="test-index",
                    )
                )
            return SearchResponse(
                data=SearchArticlesData(
                    query=request.query,
                    queries=[request.query],
                    articles=[],
                    index_version="test-index",
                )
            )

    retriever = GraphRetriever(ProbeClient(), expected_index_version="test-index")
    bundle = retriever.retrieve(make_plan("旧国奖条件"), base_filters())
    assert bundle.reason_code is OutcomeCode.ONLY_REPEALED
    assert bundle.probe["hit"] is True


def test_scope_filters_audience_mismatch() -> None:
    graduate_article = next(
        item for item in ARTICLES.values() if Audience.GRADUATE in item.audience
    )
    client = FakeSearchClient(default=[graduate_article.article_id])
    retriever = GraphRetriever(client, expected_index_version="test-index")
    bundle = retriever.retrieve(
        make_plan("研究生培养方案"),
        base_filters(audience=[Audience.UNDERGRADUATE]),
    )
    assert bundle.evidence == []
    assert bundle.reason_code is OutcomeCode.UNVERIFIABLE
    assert any(issue.code == "AUDIENCE_MISMATCH" for issue in bundle.issues)


def test_query_filters_are_forwarded_to_search() -> None:
    seen = []

    class CaptureClient(FakeSearchClient):
        def search_articles(self, request, *, timeout=None):
            seen.append(request.filters)
            return super().search_articles(request, timeout=timeout)

    plan = Plan(
        queries=[
            QueryPlanItem(
                query_id="q0",
                text="国奖条件",
                filters={"audience": ["undergraduate"], "status": ["active"]},
            )
        ]
    )
    retriever = GraphRetriever(CaptureClient(), expected_index_version="test-index")
    retriever.retrieve(plan, base_filters())
    assert seen[0].audience == ["undergraduate"]
    assert seen[0].status == ["active"]


def test_get_article_receives_public_article() -> None:
    item = article("national-aid-undergrad@v2021#第九条")
    payload = GetArticleData(article=ArticlePublic.from_article(item))
    assert payload.article.article_id == item.article_id
    assert isinstance(payload.article.text, str)
