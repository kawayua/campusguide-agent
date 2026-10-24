from __future__ import annotations

import json
from datetime import date
from pathlib import Path

from campusguide.agents.advisor import Advisor
from campusguide.agents.providers import FakeChatProvider
from campusguide.domain.enums import Audience, OutcomeCode
from campusguide.domain.models import ArticlePublic
from campusguide.mcp.client import SearchResponse
from campusguide.mcp.schemas import (
    ErrorCode,
    SearchArticlesData,
    SearchHit,
    ToolError,
)
from campusguide.observability.trace import TraceWriter
from campusguide.qa.service import QAService, QASettings
from tests.w2_helpers import make_article

ARTICLE_ID = "demo-policy@v2021#第一条"
ARTICLE_TEXT = "申请奖学金应当学习成绩优异，品行端正。"


def _claims_json(quote: str = "学习成绩优异") -> str:
    return json.dumps(
        {
            "claims": [
                {
                    "claim_id": "c1",
                    "text": "申请奖学金需要学习成绩优异。",
                    "kind": "eligibility",
                    "citations": [{"article_id": ARTICLE_ID, "quote": quote}],
                }
            ]
        },
        ensure_ascii=False,
    )


def _hit() -> SearchHit:
    article = make_article(article_id=ARTICLE_ID, text=ARTICLE_TEXT)
    return SearchHit(
        article_id=ARTICLE_ID,
        part_no=1,
        block_type="article",
        article=ArticlePublic.from_article(article),
        dense_score=0.82,
        sparse_score=12.4,
        rrf_score=0.031,
        rerank_score=0.91,
        matched_spans=[(0, 4)],
        retrieval_query="奖学金",
        rank=1,
    )


def _response(
    hits: list[SearchHit] | None = None,
    *,
    index_version: str = "articles_v1@w4-hybrid-test",
) -> SearchResponse:
    return SearchResponse(
        data=SearchArticlesData(
            query="国家奖学金申请条件是什么？",
            queries=["国家奖学金申请条件是什么？"],
            articles=hits if hits is not None else [_hit()],
            index_version=index_version,
            candidates=2,
            timing={"dense_ms": 1.0, "sparse_ms": 1.0, "rerank_ms": 2.0},
            warnings=["PROFILE_UNDETERMINED: 示例 warning"],
        ),
        server="policy-search",
        tool="search_articles",
        latency_ms=25,
        retries=1,
    )


class FakeSearchClient:
    def __init__(self, handler) -> None:
        self._handler = handler
        self.requests = []

    def search_articles(self, request, *, timeout=None) -> SearchResponse:
        self.requests.append(request)
        result = self._handler(request)
        if isinstance(result, Exception):
            raise result
        return result

    def get_article(self, article_id, part_no=None):
        raise NotImplementedError


class FakeMetaClient:
    def __init__(self, *, error: Exception | None = None) -> None:
        self.error = error
        self.calls: list[str] = []

    def get_document_versions(self, doc_id: str) -> dict:
        self.calls.append(doc_id)
        if self.error is not None:
            raise self.error
        return {
            "doc_id": doc_id,
            "versions": [
                {
                    "version_id": "v2021",
                    "status": "active",
                    "effective_from": "2021-09-01",
                    "effective_to": None,
                    "date_confidence": "high",
                }
            ],
        }


def _service(
    client: FakeSearchClient,
    *,
    trace_dir: Path,
    expected_index_version: str = "",
    meta_client=None,
) -> QAService:
    settings = QASettings(
        min_dense_score=0.0,
        expected_index_version=expected_index_version,
    )
    return QAService(
        search_client=client,
        meta_client=meta_client,
        advisor=Advisor(FakeChatProvider([_claims_json()])),
        settings=settings,
        trace_writer=TraceWriter(trace_dir),
        today=date(2026, 10, 24),
    )


def _last_trace(directory: Path) -> dict:
    trace_file = next(directory.glob("*.jsonl"))
    return json.loads(trace_file.read_text("utf-8").splitlines()[-1])


def test_answers_with_mcp_scores_and_trace(tmp_path: Path) -> None:
    client = FakeSearchClient(lambda request: _response())
    service = _service(
        client,
        trace_dir=tmp_path / "traces",
        expected_index_version="articles_v1@w4-hybrid-test",
    )
    result = service.ask("国家奖学金申请条件是什么？", audience=Audience.UNDERGRADUATE)

    assert result.outcome is OutcomeCode.ANSWERED
    assert result.index_version == "articles_v1@w4-hybrid-test"
    assert result.mcp["server"] == "policy-search"
    assert result.mcp["retries"] == 1
    assert result.retrieval_timing["rerank_ms"] == 2.0
    assert result.retrieval_warnings

    trace = _last_trace(tmp_path / "traces")
    assert trace["index_version"] == "articles_v1@w4-hybrid-test"
    assert trace["mcp"]["tool"] == "search_articles"
    assert trace["retrieval"][0]["dense_score"] == 0.82
    assert trace["retrieval"][0]["sparse_score"] == 12.4
    assert trace["retrieval"][0]["rrf_score"] == 0.031
    assert trace["retrieval"][0]["rerank_score"] == 0.91
    assert trace["retrieval_timing"]["rerank_ms"] == 2.0


def test_profile_is_forwarded_to_mcp_request(tmp_path: Path) -> None:
    from campusguide.domain.profiles import UserProfile

    client = FakeSearchClient(lambda request: _response())
    service = _service(client, trace_dir=tmp_path)
    profile = UserProfile(colleges=["agri"], majors=["agronomy"], intake_year=2024)
    service.ask("培养方案", profile=profile)
    request = client.requests[0]
    assert request.filters.profile is not None
    assert request.filters.profile.colleges == ["agri"]
    assert request.filters.profile.intake_year == 2024


def test_version_metadata_comes_from_policy_meta(tmp_path: Path) -> None:
    meta = FakeMetaClient()
    client = FakeSearchClient(lambda request: _response())
    service = _service(client, trace_dir=tmp_path, meta_client=meta)
    result = service.ask("国家奖学金申请条件是什么？", audience=Audience.UNDERGRADUATE)
    assert meta.calls == ["demo-policy"]
    assert result.version_metadata["demo-policy"]["version_count"] == 1
    assert result.version_metadata["demo-policy"]["versions"][0]["version_id"] == "v2021"
    trace = _last_trace(tmp_path)
    assert trace["version_metadata"]["demo-policy"]["version_count"] == 1


def test_meta_failure_does_not_block_answer(tmp_path: Path) -> None:
    meta = FakeMetaClient(error=ToolError(ErrorCode.INDEX_UNAVAILABLE, "meta down"))
    client = FakeSearchClient(lambda request: _response())
    service = _service(client, trace_dir=tmp_path, meta_client=meta)
    result = service.ask("国家奖学金申请条件是什么？", audience=Audience.UNDERGRADUATE)
    assert result.outcome is OutcomeCode.ANSWERED
    assert result.version_metadata["demo-policy"]["error_code"] == "INDEX_UNAVAILABLE"


def test_index_version_mismatch_refuses(tmp_path: Path) -> None:
    client = FakeSearchClient(lambda request: _response(index_version="server@other"))
    service = _service(
        client,
        trace_dir=tmp_path,
        expected_index_version="local@expected",
    )
    result = service.ask("国家奖学金申请条件是什么？", audience=Audience.UNDERGRADUATE)
    assert result.outcome is OutcomeCode.INDEX_UNAVAILABLE


def test_tool_unavailable_refuses(tmp_path: Path) -> None:
    client = FakeSearchClient(lambda request: ToolError(ErrorCode.TOOL_UNAVAILABLE, "server down"))
    service = _service(client, trace_dir=tmp_path)
    result = service.ask("国家奖学金申请条件是什么？", audience=Audience.UNDERGRADUATE)
    assert result.outcome is OutcomeCode.TOOL_UNAVAILABLE
    assert "检索工具不可用" in result.message
    trace = _last_trace(tmp_path)
    assert trace["mcp"]["error_code"] == "TOOL_UNAVAILABLE"


def test_rerank_unavailable_maps_to_tool_unavailable(tmp_path: Path) -> None:
    client = FakeSearchClient(
        lambda request: ToolError(ErrorCode.RERANK_UNAVAILABLE, "reranker down")
    )
    service = _service(client, trace_dir=tmp_path)
    result = service.ask("国家奖学金申请条件是什么？", audience=Audience.UNDERGRADUATE)
    assert result.outcome is OutcomeCode.TOOL_UNAVAILABLE


def test_conflict_maps_to_version_conflict(tmp_path: Path) -> None:
    client = FakeSearchClient(lambda request: ToolError(ErrorCode.CONFLICT, "conflict"))
    service = _service(client, trace_dir=tmp_path)
    result = service.ask("国家奖学金申请条件是什么？", audience=Audience.UNDERGRADUATE)
    assert result.outcome is OutcomeCode.VERSION_CONFLICT


def test_empty_search_hits_repealed_probe(tmp_path: Path) -> None:
    def handler(request):
        if request.filters.status == ["repealed"]:
            return _response()
        return _response(hits=[])

    client = FakeSearchClient(handler)
    service = _service(client, trace_dir=tmp_path)
    result = service.ask("国家奖学金申请条件是什么？", audience=Audience.UNDERGRADUATE)
    assert result.outcome is OutcomeCode.ONLY_REPEALED
    assert result.probe["hit"] is True
    assert len(client.requests) == 2
    assert client.requests[1].filters.ignore_effective_dates is True
