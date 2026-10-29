"""Regressions from the 100-case live failure audit, independent of local corpus files."""

from __future__ import annotations

import json
import threading
from contextlib import closing
from datetime import date
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest
from qdrant_client import QdrantClient

from campusguide.agents.graph import _default_block_types
from campusguide.agents.planner import Planner, PlannerContext
from campusguide.agents.retriever import GraphRetriever
from campusguide.domain.audience import AudienceDetail
from campusguide.domain.enums import Audience, BlockType, Intent, OutcomeCode
from campusguide.domain.models import PolicyFilters
from campusguide.mcp.schemas import ErrorCode
from campusguide.observability.errors import safe_error_summary
from campusguide.qa.guard import check_guard
from campusguide.qa.search_clients import LocalHybridSearchClient
from campusguide.retrieval.connections import create_qdrant_client
from campusguide.retrieval.service import RetrievalError
from campusguide.verification.scope import apply_scope
from tests.unit.test_w4_hybrid import _build, _retriever
from tests.w2_helpers import make_article
from tests.w6_helpers import FakeSearchClient, catalog, make_runner

AS_OF = date(2026, 10, 27)


def plan(question: str):
    return Planner(catalog(), llm_enabled=False).plan(PlannerContext(message=question, as_of=AS_OF))


def test_local_database_works_with_an_unavailable_inherited_proxy(monkeypatch):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            payload = (
                {"version": "1.19.1", "title": "qdrant"}
                if self.path == "/"
                else {"result": {"collections": []}, "status": "ok", "time": 0}
            )
            data = json.dumps(payload).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    for key in ["HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"]:
        monkeypatch.setenv(key, "http://127.0.0.1:1")
    monkeypatch.setenv("NO_PROXY", "")
    monkeypatch.setenv("QDRANT_URL", f"http://127.0.0.1:{server.server_port}")
    monkeypatch.delenv("QDRANT_LOCATION", raising=False)
    monkeypatch.delenv("QDRANT_TRUST_ENV", raising=False)
    try:
        client = create_qdrant_client()
        assert client.get_collections().collections == []
        client.close()
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=2)


@pytest.mark.parametrize("audience", [Audience.UNDERGRADUATE, Audience.GRADUATE])
def test_public_information_survives_retrieval_and_post_scope(audience):
    public = make_article(
        "news@v2021#第一条", doc_id="news", text="学院公开学术新闻。", audience=[Audience.PUBLIC]
    )
    ug = make_article(
        "ug@v2021#第一条",
        doc_id="ug",
        text="本科生奖学金申请条件。",
        audience=[Audience.UNDERGRADUATE],
    )
    grad = make_article(
        "grad@v2021#第一条",
        doc_id="grad",
        text="研究生奖学金申请条件。",
        audience=[Audience.GRADUATE],
    )
    with closing(QdrantClient(location=":memory:")) as client:
        encoder = _build(client, [public, ug, grad])
        response = _retriever(client, encoder).search_articles(
            "学院公开学术新闻与奖学金", filters=PolicyFilters(audience=[audience], as_of=AS_OF)
        )
        scoped = apply_scope(response.evidence, audience=audience, as_of=AS_OF)
        ids = {e.article.doc_id for e in scoped.evidence}
        assert "news" in ids
        assert ("grad" if audience is Audience.UNDERGRADUATE else "ug") not in ids


def test_public_information_still_obeys_college_constraints():
    from campusguide.domain.profiles import UserProfile

    first = make_article("info@v2021#第一条", doc_id="info", audience=[Audience.PUBLIC]).model_copy(
        update={"audience_detail": AudienceDetail(colleges=["info"])}
    )
    other = make_article("agri@v2021#第一条", doc_id="agri", audience=[Audience.PUBLIC]).model_copy(
        update={"audience_detail": AudienceDetail(colleges=["agri"])}
    )
    with closing(QdrantClient(location=":memory:")) as client:
        encoder = _build(client, [first, other])
        response = _retriever(client, encoder).search_articles(
            "学院介绍",
            filters=PolicyFilters(
                audience=[Audience.UNDERGRADUATE],
                profile=UserProfile(colleges=["info"]),
                as_of=AS_OF,
            ),
        )
        assert {e.article.doc_id for e in response.evidence} == {"info"}


def test_shared_section_id_preserves_each_part():
    rows = [
        make_article(
            "curriculum@v2021#课程",
            doc_id="curriculum",
            part_no=i,
            block_type=BlockType.SECTION,
            text=f"人工智能选修课程分段{i}：深度学习与计算机视觉。",
        )
        for i in [1, 2]
    ]
    with closing(QdrantClient(location=":memory:")) as client:
        encoder = _build(client, rows)
        response = _retriever(client, encoder).search_articles(
            "人工智能选修课程",
            filters=PolicyFilters(block_types=[BlockType.ARTICLE, BlockType.SECTION], as_of=AS_OF),
        )
        assert {e.article.part_no for e in response.evidence} == {1, 2}


def test_two_active_cohorts_are_retrieved_separately_and_both_kept():
    question = "计算机专业2022版和2024版培养方案的毕业学分有什么不同？"
    planned = plan(question)
    rows = [
        make_article(
            f"cs@v{year}#毕业",
            doc_id="cs",
            version_id=f"v{year}",
            effective_from=date(year, 8, 1),
            block_type=BlockType.SECTION,
            text=f"{year}级计算机培养方案，毕业需{credit}学分。",
        ).model_copy(update={"audience_detail": AudienceDetail(intake_years=[year])})
        for year, credit in [(2022, 160), (2024, 159)]
    ]
    with closing(QdrantClient(location=":memory:")) as client:
        encoder = _build(client, rows)
        backend = LocalHybridSearchClient(_retriever(client, encoder))
        bundle = GraphRetriever(backend).retrieve(
            planned, PolicyFilters(block_types=[BlockType.SECTION], as_of=AS_OF)
        )
        assert {e.article.version_id for e in bundle.evidence} == {"v2022", "v2024"}
        assert all(bundle.coverage.values())


@pytest.mark.parametrize(
    "question",
    [
        "农学院院长是谁？",
        "梁云属于哪个系？",
        "最近有哪些学术讲座？",
        "申请国家奖学金的条件？",
    ],
)
def test_both_block_types_can_enter_recall(question):
    assert set(_default_block_types(question)) == {BlockType.ARTICLE, BlockType.SECTION}


@pytest.mark.parametrize(
    "question",
    [
        "农学院负责本科教学、研究生工作和实验室安全的领导分别是谁？",
        "本科生和研究生分别如何办理在读证明，本科生成绩单收费规定是什么？",
        "农学院关于袁隆平追梦奖学金拟推荐人选的公示，何时发布？",
        "怎么申请袁隆平追梦奖学金？",
    ],
)
def test_public_fact_or_unregistered_award_is_not_a_national_award_flow(question):
    result = plan(question)
    assert result.intent is Intent.POLICY_QA
    assert result.flow_id is None
    assert result.needs_clarification is False
    assert result.queries


def test_two_award_publicities_are_two_queries_not_one_enum():
    result = plan(
        "农学院2026—2027学年国家助学金和2025—2026学年国家励志奖学金分别发布了哪些评选公示？"
    )
    assert result.flow_id is None
    assert len(result.queries) == 2
    assert "国家助学金" in result.queries[0].text and "2026—2027" in result.queries[0].text
    assert "国家励志奖学金" in result.queries[1].text and "2025—2026" in result.queries[1].text
    assert "国家励志奖学金" not in result.queries[0].text


@pytest.mark.parametrize(
    "question",
    [
        "农学院2026年研究生奖学金评选办法和补充说明，对科研成果加分如何规定？",
        "2021年本科生学籍管理实施细则有哪些要求？",
    ],
)
def test_year_in_an_active_document_title_does_not_imply_repealed(question):
    assert check_guard(question).allowed
    assert plan(question).outcome is None


@pytest.mark.parametrize(
    ("question", "outcome"),
    [
        ("2020 年版本的国家奖学金办法", OutcomeCode.HISTORICAL_NOT_SUPPORTED),
        ("帮我查一下我的成绩和排名", OutcomeCode.PRIVACY_REQUEST),
        ("隔壁学校的奖学金政策", OutcomeCode.OUT_OF_SCOPE),
    ],
)
def test_protective_guards_still_work(question, outcome):
    assert check_guard(question).outcome is outcome


def test_failed_facts_do_not_become_answered_because_the_flow_is_closed():
    question = "国家奖学金怎么申请，金额是多少"
    queries = {item.text for item in plan(question).queries}
    client = FakeSearchClient(fail_queries=queries, fail_code=ErrorCode.INDEX_UNAVAILABLE)
    result = make_runner(search_client=client).invoke("mixed-failure", question, as_of=AS_OF)
    assert result["outcome"]["code"] == "PARTIAL_ANSWER"
    assert result["checklist"]
    assert "金额是多少" in result["final_answer"]
    assert any(
        "RETRIEVAL_ERROR:" in w
        for t in result["trace"]
        if t["node"] == "retriever"
        for w in t["warnings"]
    )


def test_database_health_failure_is_detected_during_warmup(monkeypatch):
    with closing(QdrantClient(location=":memory:")) as client:
        encoder = _build(client, [make_article()])
        retriever = _retriever(client, encoder)

        def fail(*args, **kwargs):
            raise TimeoutError("database down")

        monkeypatch.setattr(client, "get_collection", fail)
        with pytest.raises(RetrievalError, match="连接检查失败"):
            retriever.warmup()


def test_error_summary_preserves_location_without_credentials():
    result = safe_error_summary(
        RuntimeError(
            "connect https://user:pass@example.test:6333/collections?api_key=secret api_key=topsecret"
        )
    )
    assert "example.test:6333/collections" in result
    assert "pass" not in result and "secret" not in result
    assert "user:" not in result
    assert len(result) <= 241


def test_model_reported_omission_stays_partial_despite_nonempty_retrieval():
    from tests.w6_helpers import article, claims_json, supported_json

    item = article("national-aid-undergrad@v2021#第九条")
    payload = json.loads(claims_json(item.article_id, item.text[:70]))
    payload["unanswered_questions"] = ["奖励标准是多少"]
    runner = make_runner(
        advisor_responses=[json.dumps(payload, ensure_ascii=False)],
        semantic_responses=[supported_json()],
    )
    result = runner.invoke(
        "omission", "申请国家奖学金需要满足哪些条件，奖励标准是多少？", as_of=AS_OF
    )
    assert all(result["coverage"].values())
    assert result["outcome"]["code"] == "PARTIAL_ANSWER"
    assert "奖励标准是多少" in result["final_answer"]
    assert "尚缺" in result["final_answer"]
    assert "尚未完整答出" in result["final_answer"]


def test_roster_layout_normalization_only_changes_ranking_passage():
    from campusguide.retrieval.hybrid import _passage

    item = make_article(text="教授 李 明 王芳\n副教授 赵强")
    passage = _passage(item)
    assert "李明" in passage
    assert item.text == "教授 李 明 王芳\n副教授 赵强"


def test_comma_separated_faculty_questions_are_visible_to_generation():
    from campusguide.agents.advisor import build_messages

    content = build_messages("李明是学院的老师吗，属于哪个系？", [], None)[1]["content"]
    assert "本题包含 2 个子问题" in content
    assert "子问题2：属于哪个系" in content
