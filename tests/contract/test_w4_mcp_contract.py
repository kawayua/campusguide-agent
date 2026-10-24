from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from fastmcp import Client

from campusguide.config import load_organization
from campusguide.domain.models import Article, Evidence
from campusguide.mcp.policy_meta_server import (
    JsonlMetaBackend,
    PolicyMetaService,
    build_meta_server,
)
from campusguide.mcp.policy_search_server import (
    PolicySearchService,
    SearchRegistries,
    build_search_server,
)
from campusguide.retrieval.hybrid import SearchResult, VersionConflictError
from campusguide.retrieval.rerank import RerankUnavailable
from campusguide.retrieval.service import RetrievalError
from tests.w2_helpers import make_article

ARTICLE_ID = "demo-policy@v2021#第一条"
ARTICLE_TEXT = "申请奖学金应当学习成绩优异，品行端正。"


def _evidence() -> Evidence:
    return Evidence(
        article=make_article(article_id=ARTICLE_ID, text=ARTICLE_TEXT),
        dense_score=0.82,
        sparse_score=12.4,
        rrf_score=0.031,
        rerank_score=0.91,
        matched_spans=[(0, 4)],
        retrieval_query="奖学金",
        rank=1,
    )


class FakeBackend:
    index_version = "articles_v1@test"

    def __init__(self, *, error: Exception | None = None, empty: bool = False) -> None:
        self.error = error
        self.empty = empty
        self.calls = 0
        self.last_filters = None

    def warmup(self) -> None:
        return None

    def search_articles(self, query, *, filters, top_k, rerank_top_n) -> SearchResult:
        self.calls += 1
        self.last_filters = filters
        if self.error is not None:
            raise self.error
        return SearchResult(
            evidence=[] if self.empty else [_evidence()],
            queries=[query],
            candidates=2,
            index_version=self.index_version,
            applied_filters=["as_of=2026-10-24"],
            warnings=(
                ["AUDIENCE_UNDETERMINED: 未指定受众，未按角色做硬过滤"]
                if not filters.audience
                else []
            ),
            timing={"dense_ms": 1.0, "sparse_ms": 1.0, "rerank_ms": 2.0},
        )

    def get_article(self, article_id: str, part_no: int | None = None) -> Article | None:
        if self.error is not None:
            raise self.error
        if article_id == ARTICLE_ID and part_no in (None, 1):
            return make_article(article_id=ARTICLE_ID, text=ARTICLE_TEXT)
        return None


def _search_service(backend: FakeBackend) -> PolicySearchService:
    return PolicySearchService(
        backend,
        registries=SearchRegistries(
            doc_ids={"demo-policy"},
            flow_ids={"scholarship-application"},
            organization=load_organization("configs/organization.yaml"),
        ),
    )


def _call(server, tool: str, arguments: dict):
    async def run():
        async with Client(server) as client:
            return await client.call_tool(tool, arguments, raise_on_error=False)

    result = asyncio.run(run())
    payload = result.structured_content
    if isinstance(payload, dict) and set(payload) == {"result"}:
        payload = payload["result"]
    assert isinstance(payload, dict), "tool 必须返回 JSON 对象信封"
    return result, payload


def test_search_tools_are_read_only_and_schema_is_explicit() -> None:
    server = build_search_server(_search_service(FakeBackend()))

    async def run():
        async with Client(server) as client:
            return await client.list_tools()

    tools = asyncio.run(run())
    names = {tool.name for tool in tools}
    assert names == {"search_articles", "get_article"}
    schema = next(tool for tool in tools if tool.name == "search_articles").input_schema
    assert schema["required"] == ["request"]
    assert schema["properties"]["request"]["required"] == ["query"]
    assert "filter" not in " ".join(names).lower()


def test_search_articles_normal_and_no_text_raw_ref() -> None:
    result, payload = _call(
        build_search_server(_search_service(FakeBackend())),
        "search_articles",
        {"request": {"query": "国家奖学金申请条件", "top_k": 10, "rerank_top_n": 5}},
    )
    assert not result.is_error
    assert payload["ok"] is True
    assert payload["schema_version"] == "1.0"
    data = payload["data"]
    assert data["index_version"] == "articles_v1@test"
    assert data["articles"][0]["article_id"] == ARTICLE_ID
    assert data["articles"][0]["dense_score"] == 0.82
    assert data["articles"][0]["sparse_score"] == 12.4
    assert data["articles"][0]["rrf_score"] == 0.031
    assert data["articles"][0]["rerank_score"] == 0.91
    assert data["articles"][0]["part_no"] == 1
    assert data["articles"][0]["block_type"] == "article"
    assert "text_raw_ref" not in json.dumps(data, ensure_ascii=False)
    assert data["timing"]["rerank_ms"] == 2.0


def test_search_articles_empty_result_is_success() -> None:
    _, payload = _call(
        build_search_server(_search_service(FakeBackend(empty=True))),
        "search_articles",
        {"request": {"query": "火星殖民"}},
    )
    assert payload["ok"] is True
    assert payload["data"]["articles"] == []


def test_version_mode_and_status_semantics_are_forwarded() -> None:
    backend = FakeBackend()
    server = build_search_server(_search_service(backend))
    _call(
        server,
        "search_articles",
        {
            "request": {
                "query": "奖学金",
                "filters": {"version_mode": "historical", "as_of": "2023-10-24"},
            }
        },
    )
    assert backend.last_filters.version_mode.value == "historical"
    assert backend.last_filters.status is None

    _call(
        server,
        "search_articles",
        {
            "request": {
                "query": "奖学金",
                "filters": {
                    "version_mode": "include_repealed",
                    "as_of": "2023-10-24",
                },
            }
        },
    )
    assert backend.last_filters.version_mode.value == "include_repealed"
    assert backend.last_filters.status is None

    _call(
        server,
        "search_articles",
        {
            "request": {
                "query": "奖学金",
                "filters": {
                    "status": ["repealed"],
                    "ignore_effective_dates": True,
                },
            }
        },
    )
    assert [status.value for status in backend.last_filters.status] == ["repealed"]
    assert backend.last_filters.ignore_effective_dates is True


def test_audience_all_is_treated_as_unspecified() -> None:
    backend = FakeBackend()
    server = build_search_server(_search_service(backend))
    _, payload = _call(
        server,
        "search_articles",
        {"request": {"query": "奖学金", "filters": {"audience": ["all"]}}},
    )
    assert payload["ok"] is True
    assert backend.last_filters.audience is None
    assert any("AUDIENCE_UNDETERMINED" in warning for warning in payload["warnings"])


@pytest.mark.parametrize(
    ("payload_request", "code"),
    [
        ({"query": "奖学金", "top_k": 0}, "INVALID_ARGUMENT"),
        ({"query": "奖学金", "rerank_top_n": 21}, "INVALID_ARGUMENT"),
        ({"query": "奖学金", "filters": {"audience": ["martian"]}}, "INVALID_FILTER"),
        ({"query": "奖学金", "filters": {"status": ["obsolete"]}}, "INVALID_FILTER"),
        ({"query": "奖学金", "filters": {"version_mode": "forever"}}, "INVALID_FILTER"),
        ({"query": "奖学金", "filters": {"block_types": ["paragraph"]}}, "INVALID_FILTER"),
        ({"query": "奖学金", "filters": {"doc_ids": ["nope"]}}, "INVALID_FILTER"),
        ({"query": "奖学金", "filters": {"flow_ids": ["nope"]}}, "INVALID_FILTER"),
        (
            {
                "query": "奖学金",
                "filters": {"profile": {"colleges": ["unknown-college"]}},
            },
            "INVALID_FILTER",
        ),
        (
            {"query": "奖学金", "filters": {"as_of": "2026/10/24"}},
            "INVALID_ARGUMENT",
        ),
    ],
)
def test_search_articles_invalid_parameters(payload_request: dict, code: str) -> None:
    _, payload = _call(
        build_search_server(_search_service(FakeBackend())),
        "search_articles",
        {"request": payload_request},
    )
    assert payload["ok"] is False
    assert payload["schema_version"] == "1.0"
    assert payload["error"]["code"] == code
    assert payload["error"]["retryable"] is False


def test_get_article_normal_and_missing() -> None:
    server = build_search_server(_search_service(FakeBackend()))
    result, payload = _call(server, "get_article", {"article_id": ARTICLE_ID, "part_no": 1})
    assert not result.is_error
    assert payload["ok"] is True
    assert payload["data"]["article"]["article_id"] == ARTICLE_ID
    assert "text_raw_ref" not in json.dumps(payload, ensure_ascii=False)
    assert payload["data"]["metadata"]["doc_id"] == "demo-policy"

    _, missing = _call(server, "get_article", {"article_id": "missing@v1#第一条"})
    assert missing["ok"] is False
    assert missing["error"]["code"] == "NOT_FOUND"


@pytest.mark.parametrize(
    ("error", "code", "retryable"),
    [
        (RetrievalError("qdrant down"), "INDEX_UNAVAILABLE", False),
        (RerankUnavailable("reranker down"), "RERANK_UNAVAILABLE", True),
        (VersionConflictError("same effective date"), "CONFLICT", False),
    ],
)
def test_search_backend_degradation_maps_to_error_envelope(
    error: Exception,
    code: str,
    retryable: bool,
) -> None:
    _, payload = _call(
        build_search_server(_search_service(FakeBackend(error=error))),
        "search_articles",
        {"request": {"query": "奖学金"}},
    )
    assert payload["ok"] is False
    assert payload["error"]["code"] == code
    assert payload["error"]["retryable"] is retryable


def _write_meta(tmp_path: Path, *, missing: bool = False) -> Path:
    path = tmp_path / "documents.jsonl"
    if missing:
        return path
    rows = [
        {
            "doc_id": "demo-policy",
            "version_id": "v2021",
            "title": "演示办法",
            "doc_type": "regulation",
            "authority_level": "binding",
            "issuer_level": "university",
            "issuer_college_id": None,
            "parent_doc_id": None,
            "relation": "standalone",
            "issuing_department": "本科生院",
            "source_url": "https://example.edu.cn/policy",
            "published_at": "2021-08-30",
            "effective_from": "2021-09-01",
            "effective_to": None,
            "status": "active",
            "source_sha256": "a" * 64,
            "fetched_at": "2026-10-24T00:00:00Z",
            "parser_version": "test",
            "audience": ["undergraduate"],
            "audience_detail": None,
            "date_confidence": "high",
            "notes": None,
        },
        {
            "doc_id": "demo-policy",
            "version_id": "v2026",
            "title": "演示办法",
            "doc_type": "regulation",
            "authority_level": "binding",
            "issuer_level": "university",
            "issuer_college_id": None,
            "parent_doc_id": None,
            "relation": "standalone",
            "issuing_department": "本科生院",
            "source_url": "https://example.edu.cn/policy",
            "published_at": "2026-08-30",
            "effective_from": "2026-09-01",
            "effective_to": None,
            "status": "active",
            "source_sha256": "b" * 64,
            "fetched_at": "2026-10-24T00:00:00Z",
            "parser_version": "test",
            "audience": ["undergraduate"],
            "audience_detail": None,
            "date_confidence": "high",
            "notes": None,
        },
    ]
    path.write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
        encoding="utf-8",
    )
    return path


def _meta_server(tmp_path: Path, *, missing: bool = False):
    return build_meta_server(
        PolicyMetaService(JsonlMetaBackend(_write_meta(tmp_path, missing=missing)))
    )


def test_meta_normal_tools_and_no_flow_calendar() -> None:
    import tempfile

    with tempfile.TemporaryDirectory() as directory:
        server = _meta_server(Path(directory))

        async def run():
            async with Client(server) as client:
                return await client.list_tools()

        names = {tool.name for tool in asyncio.run(run())}
        assert names == {
            "get_document_versions",
            "get_document",
            "resolve_version",
            "list_documents",
        }
        assert not names & {"get_flow", "list_flows", "get_calendar"}

        _, versions = _call(server, "get_document_versions", {"doc_id": "demo-policy"})
        assert versions["ok"] is True
        assert [item["version_id"] for item in versions["data"]["versions"]] == [
            "v2021",
            "v2026",
        ]
        first = versions["data"]["versions"][0]
        assert first["date_confidence"] == "high"
        assert first["source_url"].startswith("https://")
        assert first["effective_from"] == "2021-09-01"
        assert first["status"] == "active"
        assert "source_sha256" not in json.dumps(first, ensure_ascii=False)

        _, document = _call(server, "get_document", {"doc_id": "demo-policy"})
        assert document["data"]["version_count"] == 2
        assert document["data"]["latest_version_id"] == "v2026"

        _, resolved = _call(
            server,
            "resolve_version",
            {"doc_id": "demo-policy", "as_of": "2026-10-24", "mode": "current"},
        )
        assert resolved["data"]["selected_version_id"] == "v2026"

        _, historical = _call(
            server,
            "resolve_version",
            {"doc_id": "demo-policy", "as_of": "2023-10-24", "mode": "historical"},
        )
        assert historical["data"]["selected_version_id"] == "v2021"

        _, listed = _call(server, "list_documents", {"query": "演示"})
        assert listed["data"]["count"] == 1
        assert "text" not in json.dumps(listed["data"], ensure_ascii=False)


def test_meta_empty_not_found_and_invalid() -> None:
    import tempfile

    with tempfile.TemporaryDirectory() as directory:
        server = _meta_server(Path(directory))
        _, empty = _call(server, "list_documents", {"query": "不存在"})
        assert empty["ok"] is True and empty["data"]["count"] == 0

        _, missing = _call(server, "get_document_versions", {"doc_id": "nope"})
        assert missing["error"]["code"] == "NOT_FOUND"

        _, invalid = _call(server, "list_documents", {"audience": "martian"})
        assert invalid["error"]["code"] == "INVALID_FILTER"

        _, bad_mode = _call(server, "resolve_version", {"doc_id": "demo-policy", "mode": "x"})
        assert bad_mode["error"]["code"] == "INVALID_FILTER"

        _, bad_date = _call(
            server,
            "resolve_version",
            {"doc_id": "demo-policy", "as_of": "2026/10/24"},
        )
        assert bad_date["error"]["code"] == "INVALID_ARGUMENT"


def test_meta_missing_file_is_index_unavailable() -> None:
    import tempfile

    with tempfile.TemporaryDirectory() as directory:
        server = _meta_server(Path(directory), missing=True)
        _, payload = _call(server, "get_document_versions", {"doc_id": "demo-policy"})
        assert payload["ok"] is False
        assert payload["error"]["code"] == "INDEX_UNAVAILABLE"
