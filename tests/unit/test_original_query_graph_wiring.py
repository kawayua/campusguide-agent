"""Graph retriever passes the original question only to the initial fallback."""

from __future__ import annotations

from datetime import date

from campusguide.agents.graph import GraphNodes
from campusguide.agents.plan import Plan, QueryPlanItem
from campusguide.agents.retriever import GraphRetriever
from campusguide.domain.enums import BlockType
from tests.w6_helpers import FakeSearchClient

ORIGINAL = "国家助学金第九条讲什么？"
REWRITTEN = "国家助学金 第九条 规定"
ARTICLE_ID = "national-aid-undergrad@v2021#第九条"


def _nodes(client: FakeSearchClient) -> GraphNodes:
    return GraphNodes(
        planner=object(),
        retriever=GraphRetriever(client, expected_index_version="test-index"),
        advisor=object(),
        verifier=object(),
        catalog=object(),
    )


def _state() -> dict:
    return {
        "question": ORIGINAL,
        "plan": Plan(queries=[QueryPlanItem(query_id="q1", text=REWRITTEN)]),
        "trace": [],
        "as_of": date(2026, 10, 25),
        "block_types": [BlockType.ARTICLE],
    }


def test_rewritten_empty_falls_back_to_original_question() -> None:
    client = FakeSearchClient(by_query={ORIGINAL: [ARTICLE_ID]}, default=[])
    nodes = _nodes(client)

    update = nodes.retriever(_state())

    assert [item.article.article_id for item in update["evidence"]] == [ARTICLE_ID]
    assert client.calls == [REWRITTEN, ORIGINAL]
    fallback = update["trace"][-1]["fallback"]
    assert fallback["used"] is True
    assert fallback["source_query"] == ORIGINAL
    assert any(
        warning.startswith("ORIGINAL_QUERY_FALLBACK_USED")
        for warning in update["retrieval_warnings"]
    )


def test_primary_hit_does_not_search_original_question() -> None:
    client = FakeSearchClient(by_query={REWRITTEN: [ARTICLE_ID]}, default=[])
    nodes = _nodes(client)

    update = nodes.retriever(_state())

    assert [item.article.article_id for item in update["evidence"]] == [ARTICLE_ID]
    assert client.calls == [REWRITTEN]
    assert update["trace"][-1]["fallback"] == {}
