"""Bounded original-query fallback for rewritten Planner searches."""

from __future__ import annotations

from datetime import date

import pytest

from campusguide.agents.plan import Plan, QueryPlanItem, QuestionUnit
from campusguide.agents.retriever import (
    ORIGINAL_QUERY_FALLBACK_EMPTY,
    ORIGINAL_QUERY_FALLBACK_FAILED,
    ORIGINAL_QUERY_FALLBACK_REJECTED,
    ORIGINAL_QUERY_FALLBACK_SKIPPED,
    ORIGINAL_QUERY_FALLBACK_USED,
    GraphRetriever,
)
from campusguide.domain.enums import Audience, OutcomeCode
from campusguide.domain.models import PolicyFilters
from campusguide.mcp.client import SearchResponse
from campusguide.mcp.schemas import (
    ErrorCode,
    SearchArticlesData,
    SearchArticlesRequest,
    ToolError,
)
from tests.w6_helpers import ARTICLES, FakeSearchClient, article, hit

FIRST = "national-aid-undergrad@v2021#第九条"
SECOND = "national-aid-undergrad@v2021#第十条"
THIRD = "national-aid-undergrad@v2021#第七条"
FOURTH = "national-aid-undergrad@v2021#第八条"
FIFTH = "national-aid-2026-info-merit@v2026#六、申请材料"

AS_OF = date(2026, 10, 25)


class ScriptedSearchClient(FakeSearchClient):
    """Per-query hits/failures/index versions, empty by default."""

    def __init__(
        self,
        *,
        hits_by_query: dict[str, list[str]] | None = None,
        default_hits: list[str] | None = None,
        error_by_query: dict[str, ErrorCode] | None = None,
        index_by_query: dict[str, str] | None = None,
        default_index: str = "test-index",
    ) -> None:
        super().__init__(default=[])
        self.hits_by_query = dict(hits_by_query or {})
        self.default_hits = list(default_hits or [])
        self.error_by_query = dict(error_by_query or {})
        self.index_by_query = dict(index_by_query or {})
        self.default_index = default_index
        self.requests: list[SearchArticlesRequest] = []

    def search_articles(
        self,
        request: SearchArticlesRequest,
        *,
        timeout: float | None = None,
    ) -> SearchResponse:
        self.calls.append(request.query)
        self.requests.append(request)
        error = self.error_by_query.get(request.query)
        if error is not None:
            raise ToolError(error, "search failed")
        ids = self.hits_by_query.get(request.query, self.default_hits)
        articles = [
            hit(article(item), rank=index + 1, query=request.query)
            for index, item in enumerate(ids)
        ]
        return SearchResponse(
            data=SearchArticlesData(
                query=request.query,
                queries=[request.query],
                articles=articles,
                index_version=self.index_by_query.get(
                    request.query, self.default_index
                ),
            )
        )


def base_filters(**kwargs) -> PolicyFilters:
    return PolicyFilters(as_of=AS_OF, **kwargs)


def make_plan(
    *texts: str,
    filters: list[dict] | None = None,
    units: list[QuestionUnit] | None = None,
) -> Plan:
    return Plan(
        queries=[
            QueryPlanItem(
                query_id=f"q{index}",
                text=text,
                filters=dict(filters[index - 1]) if filters else {},
                unit_ids=[f"u{index}"] if units else [],
            )
            for index, text in enumerate(texts, start=1)
        ],
        question_units=list(units or []),
    )


def test_rewritten_empty_original_hits_recovers_with_same_request_scope() -> None:
    rewritten = "rewritten keyword query"
    original = "original natural question"
    client = ScriptedSearchClient(hits_by_query={original: [FIRST]})
    retriever = GraphRetriever(client, expected_index_version="test-index")
    plan = make_plan(
        rewritten,
        filters=[{"audience": ["undergraduate"], "status": ["active"]}],
    )

    bundle = retriever.retrieve(plan, base_filters(), search_queries=[original])

    assert [item.article.article_id for item in bundle.evidence] == [FIRST]
    assert bundle.evidence[0].matched_queries == ["q1"]
    assert bundle.coverage["q1"] == [FIRST]
    assert bundle.reason_code is None
    assert bundle.partial is False
    # The original is a retrieval-only fallback, not a new Planner query.
    assert bundle.queries == [rewritten]
    assert plan.queries[0].text == rewritten

    fallback = bundle.fallback
    assert fallback["requested"] is True
    assert fallback["attempted"] is True
    assert fallback["used"] is True
    assert fallback["reason"] == "USED"
    assert fallback["source_query"] == original
    assert fallback["target_query_ids"] == ["q1"]
    assert fallback["hits"] == 1
    assert fallback["latency_ms"] >= 0
    assert fallback["index_version"] == "test-index"
    assert any(
        warning.startswith(ORIGINAL_QUERY_FALLBACK_USED)
        for warning in bundle.warnings
    )

    assert [request.query for request in client.requests] == [rewritten, original]
    primary_request, fallback_request = client.requests
    assert fallback_request.filters == primary_request.filters
    assert fallback_request.filters.audience == ["undergraduate"]
    assert fallback_request.top_k == primary_request.top_k
    assert fallback_request.rerank_top_n == primary_request.rerank_top_n


def test_accepted_primary_does_not_trigger_fallback() -> None:
    rewritten = "rewritten keyword query"
    original = "original natural question"
    client = ScriptedSearchClient(
        hits_by_query={rewritten: [FIRST]},
        default_hits=[SECOND],
    )
    retriever = GraphRetriever(client, expected_index_version="test-index")

    bundle = retriever.retrieve(
        make_plan(rewritten),
        base_filters(),
        search_queries=[original],
    )

    assert [item.article.article_id for item in bundle.evidence] == [FIRST]
    assert bundle.fallback == {}
    assert [request.query for request in client.requests] == [rewritten]


def test_identity_duplicate_skips_original_search() -> None:
    rewritten = "same normalized query"
    original = "  same   normalized query "
    client = ScriptedSearchClient()
    retriever = GraphRetriever(client, expected_index_version="test-index")

    bundle = retriever.retrieve(
        make_plan(rewritten),
        base_filters(),
        search_queries=[original],
    )

    assert bundle.evidence == []
    assert bundle.reason_code is OutcomeCode.NO_EVIDENCE
    assert original not in client.calls
    assert client.calls.count(rewritten) == 2  # primary search + repealed probe
    fallback = bundle.fallback
    assert fallback["requested"] is True
    assert fallback["attempted"] is False
    assert fallback["used"] is False
    assert fallback["reason"] == "IDENTICAL_QUERY"
    assert fallback["target_query_ids"] == ["q1"]
    assert any(
        warning.startswith(ORIGINAL_QUERY_FALLBACK_SKIPPED)
        and "IDENTICAL_QUERY" in warning
        for warning in bundle.warnings
    )


@pytest.mark.parametrize(
    ("code", "outcome"),
    [
        (ErrorCode.INDEX_UNAVAILABLE, OutcomeCode.INDEX_UNAVAILABLE),
        (ErrorCode.TOOL_UNAVAILABLE, OutcomeCode.TOOL_UNAVAILABLE),
    ],
)
def test_primary_service_failure_is_not_masked(
    code: ErrorCode,
    outcome: OutcomeCode,
) -> None:
    rewritten = "rewritten keyword query"
    original = "original natural question"
    client = ScriptedSearchClient(
        hits_by_query={original: [FIRST]},
        error_by_query={rewritten: code},
    )
    retriever = GraphRetriever(client, expected_index_version="test-index")

    bundle = retriever.retrieve(
        make_plan(rewritten),
        base_filters(),
        search_queries=[original],
    )

    assert bundle.evidence == []
    assert bundle.reason_code is outcome
    assert bundle.fallback["requested"] is True
    assert bundle.fallback["attempted"] is False
    assert bundle.fallback["used"] is False
    assert bundle.fallback["reason"] == "PRIMARY_FAILURE"
    assert original not in client.calls
    assert any(
        warning.startswith("RETRIEVAL_ERROR:q1") for warning in bundle.warnings
    )


@pytest.mark.parametrize("wrong_index", ["aaa-wrong-index", "zzz-wrong-index"])
def test_fallback_index_mismatch_fails_closed_in_both_lexical_orders(
    wrong_index: str,
) -> None:
    rewritten = "rewritten keyword query"
    original = "original natural question"
    client = ScriptedSearchClient(
        hits_by_query={original: [FIRST]},
        index_by_query={original: wrong_index},
    )
    retriever = GraphRetriever(client, expected_index_version="test-index")

    bundle = retriever.retrieve(
        make_plan(rewritten),
        base_filters(),
        search_queries=[original],
    )

    assert bundle.evidence == []
    assert bundle.reason_code is OutcomeCode.INDEX_UNAVAILABLE
    warnings_text = "\n".join(bundle.warnings)
    assert wrong_index in warnings_text
    assert "test-index" in warnings_text
    assert bundle.fallback["attempted"] is True
    assert bundle.fallback["used"] is False
    assert bundle.fallback["reason"] == "INDEX_MISMATCH"
    assert bundle.fallback["index_version"] == wrong_index
    assert any(
        warning.startswith(ORIGINAL_QUERY_FALLBACK_REJECTED)
        for warning in bundle.warnings
    )
    assert [request.query for request in client.requests] == [rewritten, original]


@pytest.mark.parametrize(
    ("first_index", "second_index"),
    [
        ("aaa-wrong-index", "test-index"),
        ("test-index", "aaa-wrong-index"),
        ("test-index", "zzz-wrong-index"),
        ("zzz-wrong-index", "test-index"),
    ],
)
def test_primary_version_disagreement_fails_closed_in_any_order(
    first_index: str,
    second_index: str,
) -> None:
    first_query = "rewritten first subquestion"
    second_query = "rewritten second subquestion"
    client = ScriptedSearchClient(
        hits_by_query={first_query: [FIRST], second_query: [SECOND]},
        index_by_query={first_query: first_index, second_query: second_index},
    )
    retriever = GraphRetriever(client, expected_index_version="test-index")

    bundle = retriever.retrieve(make_plan(first_query, second_query), base_filters())

    assert bundle.evidence == []
    assert bundle.reason_code is OutcomeCode.INDEX_UNAVAILABLE
    warnings_text = "\n".join(bundle.warnings)
    assert first_index in warnings_text
    assert second_index in warnings_text
    # Primary hits existed, so no fallback was warranted; still no success.
    assert bundle.fallback == {}
    assert [request.query for request in client.requests] == [first_query, second_query]


def test_primary_version_disagreement_fails_without_expected_index() -> None:
    first_query = "rewritten first subquestion"
    second_query = "rewritten second subquestion"
    client = ScriptedSearchClient(
        hits_by_query={first_query: [FIRST], second_query: [SECOND]},
        index_by_query={
            first_query: "test-index",
            second_query: "zzz-wrong-index",
        },
    )
    retriever = GraphRetriever(client)

    bundle = retriever.retrieve(make_plan(first_query, second_query), base_filters())

    assert bundle.evidence == []
    assert bundle.reason_code is OutcomeCode.INDEX_UNAVAILABLE


def test_consistent_index_without_expected_still_succeeds() -> None:
    rewritten = "rewritten keyword query"
    client = ScriptedSearchClient(hits_by_query={rewritten: [FIRST]})
    retriever = GraphRetriever(client)

    bundle = retriever.retrieve(make_plan(rewritten), base_filters())

    assert [item.article.article_id for item in bundle.evidence] == [FIRST]
    assert bundle.reason_code is None
    assert bundle.index_version == "test-index"


def test_all_empty_remains_honest_no_evidence() -> None:
    rewritten = "rewritten keyword query"
    original = "original natural question"
    client = ScriptedSearchClient()
    retriever = GraphRetriever(client, expected_index_version="test-index")

    bundle = retriever.retrieve(
        make_plan(rewritten),
        base_filters(),
        search_queries=[original],
    )

    assert bundle.evidence == []
    assert bundle.reason_code is OutcomeCode.NO_EVIDENCE
    assert bundle.probe["hit"] is False
    assert bundle.fallback["requested"] is True
    assert bundle.fallback["attempted"] is True
    assert bundle.fallback["used"] is False
    assert bundle.fallback["reason"] == "EMPTY_RESULT"
    assert bundle.fallback["hits"] == 0
    assert any(
        warning.startswith(ORIGINAL_QUERY_FALLBACK_EMPTY)
        for warning in bundle.warnings
    )
    assert [request.query for request in client.requests] == [
        rewritten,
        original,
        rewritten,
    ]


def test_multi_query_uses_one_extra_request_and_restores_subquestion_coverage() -> None:
    first_query = "rewritten first subquestion"
    second_query = "rewritten second subquestion"
    original = "original natural question"
    client = ScriptedSearchClient(hits_by_query={original: [FIRST]})
    retriever = GraphRetriever(client, expected_index_version="test-index")
    units = [
        QuestionUnit(unit_id="u1", original_span="first subquestion"),
        QuestionUnit(unit_id="u2", original_span="second subquestion"),
    ]

    bundle = retriever.retrieve(
        make_plan(first_query, second_query, units=units),
        base_filters(),
        search_queries=[original],
    )

    assert client.calls.count(original) == 1
    assert len(client.requests) == 3
    assert bundle.fallback["target_query_ids"] == ["q1", "q2"]
    assert bundle.fallback["used"] is True
    assert bundle.coverage["q1"] == [FIRST]
    assert bundle.coverage["q2"] == [FIRST]
    assert bundle.evidence[0].matched_queries == ["q1", "q2"]
    assert bundle.partial is False


def test_divergent_query_filters_do_not_broaden_to_global_original() -> None:
    first_query = "rewritten first subquestion"
    second_query = "rewritten second subquestion"
    original = "original natural question"
    client = ScriptedSearchClient(hits_by_query={original: [FIRST]})
    retriever = GraphRetriever(client, expected_index_version="test-index")
    plan = make_plan(
        first_query,
        second_query,
        filters=[
            {"audience": ["undergraduate"]},
            {"audience": ["graduate"]},
        ],
    )

    bundle = retriever.retrieve(plan, base_filters(), search_queries=[original])

    assert bundle.evidence == []
    assert bundle.reason_code is OutcomeCode.NO_EVIDENCE
    assert bundle.fallback["requested"] is True
    assert bundle.fallback["attempted"] is False
    assert bundle.fallback["used"] is False
    assert bundle.fallback["reason"] == "AMBIGUOUS_FILTERS"
    assert bundle.fallback["target_query_ids"] == ["q1", "q2"]
    assert original not in client.calls
    assert any(
        warning.startswith(ORIGINAL_QUERY_FALLBACK_SKIPPED)
        and "AMBIGUOUS_FILTERS" in warning
        for warning in bundle.warnings
    )


def test_fallback_service_failure_is_surfaced() -> None:
    rewritten = "rewritten keyword query"
    original = "original natural question"
    client = ScriptedSearchClient(error_by_query={original: ErrorCode.INTERNAL})
    retriever = GraphRetriever(client, expected_index_version="test-index")

    bundle = retriever.retrieve(
        make_plan(rewritten),
        base_filters(),
        search_queries=[original],
    )

    assert bundle.evidence == []
    assert bundle.reason_code is OutcomeCode.TOOL_UNAVAILABLE
    assert bundle.fallback["attempted"] is True
    assert bundle.fallback["used"] is False
    assert bundle.fallback["reason"] == "ERROR"
    assert bundle.fallback["error_code"] == "INTERNAL"
    assert any(
        warning.startswith(ORIGINAL_QUERY_FALLBACK_FAILED)
        for warning in bundle.warnings
    )


def test_fallback_respects_existing_evidence_budget() -> None:
    rewritten = "rewritten keyword query"
    original = "original natural question"
    client = ScriptedSearchClient(
        hits_by_query={original: [FIRST, SECOND, THIRD, FOURTH, FIFTH]}
    )
    retriever = GraphRetriever(
        client,
        max_evidence=2,
        expected_index_version="test-index",
    )

    bundle = retriever.retrieve(
        make_plan(rewritten),
        base_filters(),
        search_queries=[original],
    )

    assert bundle.fallback["used"] is True
    assert bundle.evidence
    assert len(bundle.evidence) <= 2


@pytest.mark.parametrize("originals", [None, []])
def test_no_explicit_originals_never_trigger_fallback(
    originals: list[str] | None,
) -> None:
    rewritten = "rewritten keyword query"
    client = ScriptedSearchClient()
    retriever = GraphRetriever(client, expected_index_version="test-index")

    bundle = retriever.retrieve(
        make_plan(rewritten),
        base_filters(),
        search_queries=originals,
    )

    assert bundle.evidence == []
    assert bundle.reason_code is OutcomeCode.NO_EVIDENCE
    assert bundle.fallback == {}
    assert [request.query for request in client.requests] == [
        rewritten,
        rewritten,
    ]


def test_fallback_evidence_still_obeys_audience_scope() -> None:
    rewritten = "rewritten keyword query"
    original = "original natural question"
    graduate_article = next(
        item for item in ARTICLES.values() if Audience.GRADUATE in item.audience
    )
    client = ScriptedSearchClient(
        hits_by_query={original: [graduate_article.article_id]}
    )
    retriever = GraphRetriever(client, expected_index_version="test-index")

    bundle = retriever.retrieve(
        make_plan(rewritten),
        base_filters(audience=[Audience.UNDERGRADUATE]),
        search_queries=[original],
    )

    assert bundle.evidence == []
    assert bundle.reason_code is OutcomeCode.UNVERIFIABLE
    assert bundle.fallback["used"] is True
    assert any(issue.code == "AUDIENCE_MISMATCH" for issue in bundle.issues)
