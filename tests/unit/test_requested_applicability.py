from __future__ import annotations

import json
from datetime import date
from pathlib import Path

from campusguide.agents.advisor import Advisor, CitationDraft, ClaimDraft
from campusguide.agents.graph import GraphNodes, GraphRunner, _checklist_scope_supported
from campusguide.agents.planner import Planner
from campusguide.agents.providers import FakeChatProvider
from campusguide.agents.retriever import GraphRetriever
from campusguide.agents.verifier import GraphVerifier
from campusguide.config import load_handoff_config
from campusguide.domain.audience import AudienceDetail
from campusguide.domain.enums import (
    ArticleStatus,
    Audience,
    BlockType,
    DocType,
    GraphDecision,
    IssueScope,
    OutcomeCode,
)
from campusguide.domain.models import (
    Article,
    ArticlePublic,
    ChecklistItem,
    Citation,
    Claim,
    Evidence,
)
from campusguide.domain.profiles import UserProfile
from campusguide.mcp.client import SearchResponse
from campusguide.mcp.schemas import GetArticleData, SearchArticlesData, SearchHit
from campusguide.verification.request_scope import (
    claim_scope_issues,
    filter_evidence_for_request,
    missing_future_targets,
    parse_requested_scope,
    source_applicability,
)
from campusguide.verification.scope import apply_scope
from campusguide.verification.semantic import SemanticVerifier
from tests.w6_helpers import catalog, claims_json, supported_json

ROOT = Path(__file__).resolve().parents[2]
AS_OF = date(2026, 11, 1)


def make_article(
    article_id: str,
    title: str,
    text: str,
    *,
    published_at: date = date(2026, 1, 10),
    effective_from: date = date(2026, 1, 1),
    metadata_source_status: str = "not_checked",
    audience_detail: AudienceDetail | None = None,
    topic_tags: list[str] | None = None,
) -> Article:
    return Article(
        article_id=article_id,
        doc_id=article_id.split("#", 1)[0],
        version_id="v2026",
        doc_title=title,
        doc_type=DocType.NOTICE,
        article_no="第一条",
        article_index=1,
        part_no=1,
        block_type=BlockType.ARTICLE,
        text=text,
        text_normalized=text,
        audience=[Audience.PUBLIC],
        audience_detail=audience_detail,
        status=ArticleStatus.ACTIVE,
        effective_from=effective_from,
        published_at=published_at,
        issuing_department="财务处",
        source_url="https://example.edu/notice",
        source_sha256="0" * 64,
        metadata_source_status=metadata_source_status,
        topic_tags=topic_tags or [],
    )


def current_fee_article() -> Article:
    return make_article(
        "fee-current#第一条",
        "华南农业大学出国成绩单认证收费标准",
        "出国成绩单认证费为每份15元。",
        topic_tags=["出国成绩单认证费"],
    )


def future_fee_article() -> Article:
    return make_article(
        "fee-2028#第一条",
        "华南农业大学2028学年度出国成绩单认证费标准",
        "2028学年度出国成绩单认证费为每份20元。",
        topic_tags=["出国成绩单认证费"],
    )


def annual_fee_article(
    year: int,
    *,
    fee: int = 15,
    include_year_in_body: bool = True,
) -> Article:
    body = (
        f"{year}年度出国成绩单认证费为每份{fee}元。"
        if include_year_in_body
        else f"出国成绩单认证费为每份{fee}元。"
    )
    return make_article(
        f"fee-{year}#第一条",
        f"华南农业大学{year}年度出国成绩单认证收费标准",
        body,
        published_at=date(year, 1, 10),
        effective_from=date(year, 1, 1),
        topic_tags=["出国成绩单认证费"],
    )


def academic_fee_article(start_year: int, end_year: int) -> Article:
    label = f"{start_year}-{str(end_year)[-2:]}学年"
    return make_article(
        f"fee-{start_year}-{end_year}#第一条",
        f"华南农业大学{label}出国成绩单认证费标准",
        f"{label}出国成绩单认证费为每份15元。",
        published_at=date(start_year, 10, 10),
        effective_from=date(start_year, 9, 1),
        topic_tags=["出国成绩单认证费"],
    )


def multi_supported_json(*claim_ids: str) -> str:
    return json.dumps(
        {
            "labels": [
                {"claim_id": claim_id, "label": "supported", "detail": "ok"}
                for claim_id in claim_ids
            ]
        },
        ensure_ascii=False,
    )


class StaticSearchClient:
    def __init__(self, articles: list[Article]) -> None:
        self._articles = {item.article_id: item for item in articles}

    def warmup(self) -> None:
        return None

    def search_articles(self, request, *, timeout=None) -> SearchResponse:
        hits = [
            SearchHit(
                article_id=item.article_id,
                part_no=item.part_no,
                block_type=item.block_type.value,
                article=ArticlePublic.from_article(item),
                rerank_score=0.9,
                rank=index + 1,
                retrieval_query=request.query,
            )
            for index, item in enumerate(self._articles.values())
        ]
        return SearchResponse(
            data=SearchArticlesData(
                query=request.query,
                queries=[request.query],
                articles=hits,
                index_version="test-index",
            )
        )

    def get_article(self, article_id: str, part_no: int | None = None) -> GetArticleData:
        item = self._articles[article_id]
        if part_no is not None and item.part_no != part_no:
            raise KeyError(article_id)
        return GetArticleData(article=ArticlePublic.from_article(item))


def make_runner(
    articles: list[Article],
    *,
    advisor_response: str,
    semantic_response: str | None = None,
) -> GraphRunner:
    flow_catalog = catalog()
    advisor = Advisor(
        FakeChatProvider([advisor_response]),
        max_tokens=3000,
        enable_quote_repair=False,
    )
    semantic = SemanticVerifier(
        FakeChatProvider([semantic_response or supported_json(claim_id="c1")])
    )
    nodes = GraphNodes(
        planner=Planner(flow_catalog, llm_enabled=False),
        retriever=GraphRetriever(
            StaticSearchClient(articles),
            expected_index_version="test-index",
        ),
        advisor=advisor,
        verifier=GraphVerifier(semantic_verifier=semantic, max_revise=0),
        catalog=flow_catalog,
        handoff_config=load_handoff_config(ROOT / "configs" / "handoff.yaml"),
        max_revise=0,
    )
    return GraphRunner(nodes)


def make_draft(
    article: Article,
    *,
    text: str,
    quote: str | None = None,
    claim_id: str = "c1",
) -> ClaimDraft:
    return ClaimDraft(
        claim_id=claim_id,
        text=text,
        kind="eligibility",
        citations=[
            CitationDraft(
                article_id=article.article_id,
                quote=quote or article.text,
            )
        ],
    )


def make_citation(article: Article, *, quote: str | None = None) -> Citation:
    return Citation(
        article_id=article.article_id,
        quote=quote or article.text,
        quote_start=0,
        quote_end=len(quote or article.text),
        doc_title=article.doc_title,
        article_no=article.article_no,
        part_no=article.part_no,
        effective_from=article.effective_from,
        source_url=article.source_url,
    )


def test_parse_explicit_forms_and_ignore_cohorts_versions_durations_codes() -> None:
    assert parse_requested_scope("2028年出国成绩单认证费", AS_OF).future_targets[0].label() == (
        "2028年"
    )
    dated = parse_requested_scope("2028年3月出国成绩单认证费", AS_OF).future_targets[0]
    assert dated.label() == "2028年"
    academic = parse_requested_scope("2028学年度出国成绩单认证费", AS_OF).future_targets[0]
    assert (academic.start_year, academic.end_year) == (2028, 2029)
    range_target = parse_requested_scope("2028-2029学年收费标准", AS_OF).future_targets[0]
    assert (range_target.start_year, range_target.end_year) == (2028, 2029)
    assert parse_requested_scope("明年学费标准", AS_OF).future_targets[0].start_year == 2027
    assert (
        parse_requested_scope("下一学年学费标准", AS_OF).future_targets[0].label()
        == "2027-2028学年"
    )
    assert parse_requested_scope("2026年学费标准", AS_OF).future_targets == []

    for text in (
        "2028级本科生培养方案",
        "2028届毕业生就业材料",
        "2024版培养方案",
        "学制4年",
        "课程代码2028是什么",
        "2028年入学",
    ):
        assert parse_requested_scope(text, AS_OF).targets == [], text
    for text in (
        "2028年招生",
        "2028年招生报名费是多少？",
        "2028年推免报名费是多少？",
    ):
        assert parse_requested_scope(text, AS_OF).future_targets[0].label() == "2028年", text


def test_parse_normalizes_fullwidth_business_years() -> None:
    academic = parse_requested_scope("２０２８学年度成绩单认证费是多少？", AS_OF)
    assert academic.future_targets[0].label() == "2028-2029学年"
    calendar = parse_requested_scope("２０２８年出国成绩单认证费", AS_OF)
    assert calendar.future_targets[0].label() == "2028年"


def test_parse_ignores_document_locators_but_keeps_explicit_targets() -> None:
    for text in (
        "2024年修订的办法现在出国成绩单认证费是多少？",
        "2024年印发的收费办法现在还有效吗？",
        "根据2024年修订的通知，现在的认证费是多少？",
    ):
        assert parse_requested_scope(text, AS_OF).targets == [], text

    current = parse_requested_scope("2026年度出国成绩单认证费是多少元", AS_OF)
    assert current.targets[0].label() == "2026年"
    academic = parse_requested_scope("2026-27学年出国成绩单认证费是多少元", AS_OF)
    assert (academic.targets[0].start_year, academic.targets[0].end_year) == (2026, 2027)
    assert academic.targets[0].label() == "2026-2027学年"

    for text in (
        "2024版培养方案",
        "2024级本科生培养方案",
        "2027届毕业生就业材料",
        "学制4年",
    ):
        assert parse_requested_scope(text, AS_OF).targets == [], text


def test_source_applicability_requires_exact_explicit_scope_and_business() -> None:
    question = "2028-2029学年出国成绩单认证费是多少元"
    target = parse_requested_scope(question, AS_OF).future_targets[0]
    broad = make_article(
        "fee-broad#第一条",
        "2027-2029学年出国成绩单认证费标准",
        "2027-2029学年出国成绩单认证费为每份20元。",
    )
    exact = make_article(
        "fee-exact#第一条",
        "2028-2029学年出国成绩单认证费标准",
        "2028-2029学年出国成绩单认证费为每份20元。",
        published_at=date(2026, 1, 10),
    )
    assert source_applicability(broad, target, question=question).status == "mismatch"
    assert source_applicability(exact, target, question=question).status == "matched"


def test_source_applicability_distinguishes_current_timeless_past_and_explicit_years() -> None:
    current_question = "2026年度出国成绩单认证费是多少元"
    past_question = "2025年度出国成绩单认证费是多少元"
    current_target = parse_requested_scope(current_question, AS_OF).targets[0]
    past_target = parse_requested_scope(past_question, AS_OF).targets[0]
    annual_2024 = annual_fee_article(2024, include_year_in_body=False)
    annual_2025 = annual_fee_article(2025)
    annual_2026 = annual_fee_article(2026)

    assert (
        source_applicability(annual_2024, current_target, question=current_question).status
        == "mismatch"
    )
    assert (
        source_applicability(annual_2024, past_target, question=past_question).status
        == "mismatch"
    )
    assert (
        source_applicability(annual_2026, current_target, question=current_question).status
        == "matched"
    )
    assert (
        source_applicability(annual_2025, past_target, question=past_question).status
        == "matched"
    )
    assert (
        source_applicability(current_fee_article(), current_target, question=current_question).status
        == "unknown"
    )
    assert (
        source_applicability(current_fee_article(), past_target, question=past_question).status
        == "unknown"
    )


def test_source_applicability_academic_range_requires_exact_years() -> None:
    question = "2026-27学年出国成绩单认证费是多少元"
    target = parse_requested_scope(question, AS_OF).targets[0]
    previous = academic_fee_article(2025, 2026)
    current = academic_fee_article(2026, 2027)
    assert source_applicability(previous, target, question=question).status == "mismatch"
    assert source_applicability(current, target, question=question).status == "matched"


def test_unrelated_deadline_and_publication_year_do_not_lend_fee_scope() -> None:
    question = "2028年出国成绩单认证费是多少元"
    target = parse_requested_scope(question, AS_OF).future_targets[0]
    unrelated = make_article(
        "award-2028#第一条",
        "2028年国家奖学金评审通知",
        "申请截止时间为2028年3月1日。",
        published_at=date(2028, 1, 2),
    )
    no_scope = make_article(
        "award-plain#第一条",
        "国家奖学金评审通知",
        "申请截止时间为2028年3月1日。",
        published_at=date(2028, 1, 2),
    )
    published_only = make_article(
        "fee-published#第一条",
        "出国成绩单认证费标准",
        "本标准于2028年3月1日发布，具体收费另行通知。",
        published_at=date(2028, 1, 2),
    )
    assert source_applicability(unrelated, target, question=question).status != "matched"
    assert source_applicability(no_scope, target, question=question).status == "unknown"
    assert source_applicability(published_only, target, question=question).status == "unknown"


def test_same_article_other_business_year_does_not_lend_fee_scope() -> None:
    question = "2028年出国成绩单认证费是多少元"
    target = parse_requested_scope(question, AS_OF).future_targets[0]
    article = make_article(
        "fee-and-award#第一条",
        "出国成绩单认证费及奖学金事项",
        "2028年度奖学金标准为3000元。出国成绩单认证费为每份15元。",
    )
    assert source_applicability(article, target, question=question).status != "matched"


def test_body_header_binds_business_and_history_does_not_reject_future_proof() -> None:
    question = "2028学年度出国成绩单认证费是多少元"
    target = parse_requested_scope(question, AS_OF).future_targets[0]
    header = make_article(
        "fee-header#第一条",
        "收费事项说明",
        "2028学年度收费标准：出国成绩单认证费为每份20元。",
    )
    history = make_article(
        "fee-history#第一条",
        "华南农业大学2028学年度出国成绩单认证费标准",
        "2025年出国成绩单认证费为每份10元。2028学年度出国成绩单认证费为每份20元。",
    )
    assert source_applicability(header, target, question=question).status == "matched"
    assert source_applicability(history, target, question=question).status == "matched"


def test_evidence_filter_clears_current_only_future_answer() -> None:
    question = "2028学年度学校出国成绩单认证费是每份多少元"
    scope = parse_requested_scope(question, AS_OF)
    result = filter_evidence_for_request(
        [Evidence(article=current_fee_article(), rerank_score=1.0)],
        scope,
    )
    assert result.evidence == []
    assert result.reason_code is OutcomeCode.VERSION_MISMATCH

    mixed = parse_requested_scope("2026年和2028学年度出国成绩单认证费各是多少元", AS_OF)
    kept = filter_evidence_for_request(
        [
            Evidence(article=current_fee_article(), rerank_score=1.0),
            Evidence(article=future_fee_article(), rerank_score=0.9),
        ],
        mixed,
    )
    assert {item.article.article_id for item in kept.evidence} == {
        "fee-current#第一条",
        "fee-2028#第一条",
    }


def test_evidence_filter_rejects_explicit_wrong_year_for_current_and_past() -> None:
    annual_2024 = annual_fee_article(2024, include_year_in_body=False)
    for question in (
        "2026年度出国成绩单认证费是多少元",
        "2025年度出国成绩单认证费是多少元",
    ):
        scope = parse_requested_scope(question, AS_OF)
        result = filter_evidence_for_request(
            [Evidence(article=annual_2024, rerank_score=1.0)],
            scope,
        )
        assert result.evidence == [], question
        assert result.reason_code is OutcomeCode.VERSION_MISMATCH, question


def test_evidence_filter_allows_timeless_current_but_not_past_borrowing() -> None:
    current_scope = parse_requested_scope("2026年度出国成绩单认证费是多少元", AS_OF)
    past_scope = parse_requested_scope("2025年度出国成绩单认证费是多少元", AS_OF)
    generic = Evidence(article=current_fee_article(), rerank_score=1.0)

    kept = filter_evidence_for_request([generic], current_scope)
    assert kept.evidence == [generic]

    dropped = filter_evidence_for_request([generic], past_scope)
    assert dropped.evidence == []
    assert dropped.reason_code is OutcomeCode.VERSION_MISMATCH


def test_manager_full_graph_rejects_current_source_for_future_question() -> None:
    advisor = claims_json(
        "fee-current#第一条",
        "出国成绩单认证费为每份15元。",
        text="出国成绩单认证费为每份15元。",
    )
    runner = make_runner([current_fee_article()], advisor_response=advisor)
    result = runner.invoke(
        "scope-current-only",
        "2028学年度学校出国成绩单认证费是每份多少元",
        as_of=AS_OF,
    )
    assert result["outcome"]["code"] == "VERSION_MISMATCH"
    assert "2028-2029学年" in result["final_answer"]
    assert "未能确认" in result["final_answer"]
    assert "advisor" not in [item["node"] for item in result["trace"]]


def test_full_graph_accepts_explicit_future_source_with_current_publication() -> None:
    advisor = claims_json(
        "fee-2028#第一条",
        "2028学年度出国成绩单认证费为每份20元。",
        text="2028学年度出国成绩单认证费为每份20元。",
    )
    runner = make_runner([future_fee_article()], advisor_response=advisor)
    result = runner.invoke(
        "scope-future-match",
        "2028学年度学校出国成绩单认证费是每份多少元",
        as_of=AS_OF,
    )
    assert result["outcome"]["code"] == "ANSWERED"
    assert result["claims"]


def test_full_graph_model_omits_year_but_cites_wrong_current_source() -> None:
    advisor = claims_json(
        "fee-current#第一条",
        "出国成绩单认证费为每份15元。",
        text="出国成绩单认证费为每份15元。",
    )
    runner = make_runner(
        [current_fee_article(), future_fee_article()],
        advisor_response=advisor,
    )
    result = runner.invoke(
        "scope-model-omits",
        "2026年和2028学年度出国成绩单认证费各是多少元",
        as_of=AS_OF,
    )
    assert result["outcome"]["code"] == "VERSION_MISMATCH"
    assert "2028-2029学年" in result["final_answer"]


def test_full_graph_blocks_2024_annual_source_for_2026_request_with_omitted_year_claim() -> None:
    source = annual_fee_article(2024, include_year_in_body=False)
    advisor = claims_json(
        source.article_id,
        source.text,
        text="出国成绩单认证费为每份15元。",
    )
    runner = make_runner([source], advisor_response=advisor)
    result = runner.invoke(
        "scope-current-wrong-year",
        "2026年度出国成绩单认证费是多少元？",
        as_of=AS_OF,
    )
    assert result["outcome"]["code"] == "VERSION_MISMATCH"
    assert "2026年" in result["final_answer"]
    assert "未能确认" in result["final_answer"]
    assert "advisor" not in [item["node"] for item in result["trace"]]


def test_full_graph_blocks_2024_annual_source_for_2025_request() -> None:
    source = annual_fee_article(2024, include_year_in_body=False)
    advisor = claims_json(
        source.article_id,
        source.text,
        text="出国成绩单认证费为每份15元。",
    )
    runner = make_runner([source], advisor_response=advisor)
    result = runner.invoke(
        "scope-past-wrong-year",
        "2025年度出国成绩单认证费是多少元？",
        as_of=AS_OF,
    )
    assert result["outcome"]["code"] == "VERSION_MISMATCH"
    assert "2025年" in result["final_answer"]
    assert "advisor" not in [item["node"] for item in result["trace"]]


def test_full_graph_accepts_matching_2026_annual_source_with_omitted_year_claim() -> None:
    source = annual_fee_article(2026, include_year_in_body=False)
    advisor = claims_json(
        source.article_id,
        source.text,
        text="出国成绩单认证费为每份15元。",
    )
    runner = make_runner([source], advisor_response=advisor)
    result = runner.invoke(
        "scope-current-match",
        "2026年度出国成绩单认证费是多少元？",
        as_of=AS_OF,
    )
    assert result["outcome"]["code"] == "ANSWERED"


def test_full_graph_accepts_generic_current_timeless_policy() -> None:
    source = current_fee_article()
    advisor = claims_json(
        source.article_id,
        source.text,
        text="出国成绩单认证费为每份15元。",
    )
    runner = make_runner([source], advisor_response=advisor)
    result = runner.invoke(
        "scope-current-timeless",
        "2026年度出国成绩单认证费是多少元？",
        as_of=AS_OF,
    )
    assert result["outcome"]["code"] == "ANSWERED"


def test_full_graph_rejects_current_undated_price_for_past_request() -> None:
    source = make_article(
        "fee-current-20#第一条",
        "华南农业大学出国成绩单认证收费标准",
        "出国成绩单认证费为每份20元。",
        topic_tags=["出国成绩单认证费"],
    )
    advisor = claims_json(
        source.article_id,
        source.text,
        text="出国成绩单认证费为每份20元。",
    )
    runner = make_runner([source], advisor_response=advisor)
    result = runner.invoke(
        "scope-past-current-price",
        "2025年度出国成绩单认证费是多少元？",
        as_of=AS_OF,
    )
    assert result["outcome"]["code"] == "VERSION_MISMATCH"
    assert "2025年" in result["final_answer"]
    assert "advisor" not in [item["node"] for item in result["trace"]]


def test_full_graph_accepts_explicit_past_source_known_active() -> None:
    source = annual_fee_article(2025)
    advisor = claims_json(
        source.article_id,
        source.text,
        text="2025年度出国成绩单认证费为每份15元。",
    )
    runner = make_runner([source], advisor_response=advisor)
    result = runner.invoke(
        "scope-past-match",
        "2025年度出国成绩单认证费是多少元？",
        as_of=AS_OF,
    )
    assert result["outcome"]["code"] == "ANSWERED"


def test_full_graph_blocks_previous_academic_range_for_current_request() -> None:
    source = academic_fee_article(2025, 2026)
    advisor = claims_json(
        source.article_id,
        source.text,
        text="出国成绩单认证费为每份15元。",
    )
    runner = make_runner([source], advisor_response=advisor)
    result = runner.invoke(
        "scope-academic-current-wrong-range",
        "2026-27学年出国成绩单认证费是多少元？",
        as_of=AS_OF,
    )
    assert result["outcome"]["code"] == "VERSION_MISMATCH"
    assert "2026-2027学年" in result["final_answer"]
    assert "advisor" not in [item["node"] for item in result["trace"]]


def test_full_graph_accepts_two_local_mixed_year_claims() -> None:
    past = annual_fee_article(2025)
    current = annual_fee_article(2026)
    advisor = json.dumps(
        {
            "claims": [
                {
                    "claim_id": "c1",
                    "text": "2025年度出国成绩单认证费为每份15元。",
                    "kind": "eligibility",
                    "citations": [{"article_id": past.article_id, "quote": past.text}],
                },
                {
                    "claim_id": "c2",
                    "text": "2026年度出国成绩单认证费按每份15元收取。",
                    "kind": "eligibility",
                    "citations": [{"article_id": current.article_id, "quote": current.text}],
                },
            ]
        },
        ensure_ascii=False,
    )
    runner = make_runner(
        [past, current],
        advisor_response=advisor,
        semantic_response=multi_supported_json("c1", "c2"),
    )
    result = runner.invoke(
        "scope-mixed-two-claims",
        "2025年度和2026年度出国成绩单认证费各是多少元？",
        as_of=AS_OF,
    )
    assert result["outcome"]["code"] == "ANSWERED"
    assert result["failed_claim_ids"] == []


def test_full_graph_mixed_year_wrong_source_cannot_lend() -> None:
    source = annual_fee_article(2025)
    advisor = claims_json(
        source.article_id,
        source.text,
        text="出国成绩单认证费为每份15元。",
    )
    runner = make_runner([source], advisor_response=advisor)
    result = runner.invoke(
        "scope-mixed-wrong-source",
        "2025年度和2026年度出国成绩单认证费各是多少元？",
        as_of=AS_OF,
    )
    assert result["outcome"]["code"] == "VERSION_MISMATCH"
    assert "2026年" in result["final_answer"]


def test_revision_locator_does_not_reject_current_policy() -> None:
    source = current_fee_article()
    advisor = claims_json(
        source.article_id,
        source.text,
        text="出国成绩单认证费为每份15元。",
    )
    runner = make_runner([source], advisor_response=advisor)
    result = runner.invoke(
        "scope-revision-locator",
        "根据2024年修订的办法，现在出国成绩单认证费是多少元？",
        as_of=AS_OF,
    )
    assert result["outcome"]["code"] == "ANSWERED"


def test_future_published_source_is_blocked_by_existing_version_filter() -> None:
    future = make_article(
        "fee-upcoming#第一条",
        "2028学年度出国成绩单认证费标准",
        "2028学年度出国成绩单认证费为每份20元。",
        effective_from=date(2028, 1, 1),
        published_at=date(2028, 1, 1),
    )
    runner = make_runner(
        [future],
        advisor_response=claims_json(
            future.article_id,
            future.text,
            text=future.text,
        ),
    )
    result = runner.invoke(
        "scope-upcoming",
        "2028学年度学校出国成绩单认证费是每份多少元",
        as_of=AS_OF,
    )
    assert result["outcome"]["code"] == "VERSION_MISMATCH"
    assert result["evidence"] == []


def test_verifier_rejects_wrong_citation_even_with_correct_source_in_evidence() -> None:
    current = current_fee_article()
    future = future_fee_article()
    verifier = GraphVerifier(semantic_verifier=None, max_revise=0)
    report = verifier.verify(
        [make_draft(current, text="出国成绩单认证费为每份15元。")],
        [Evidence(article=current), Evidence(article=future)],
        question="2028学年度出国成绩单认证费是多少元",
        as_of=AS_OF,
    )
    assert report.decision is GraphDecision.ABSTAIN
    assert report.reason_code is OutcomeCode.VERSION_MISMATCH
    assert any(issue.code == "REQUESTED_SCOPE_MISMATCH" for issue in report.issues)


def test_verifier_rejects_explicit_target_rewrite() -> None:
    current = make_article(
        "fee-2026#第一条",
        "2026年出国成绩单认证费标准",
        "2026年出国成绩单认证费为每份15元。",
    )
    report = GraphVerifier(semantic_verifier=None, max_revise=0).verify(
        [make_draft(current, text="2026年出国成绩单认证费为每份15元。")],
        [Evidence(article=current)],
        question="2028学年度出国成绩单认证费是多少元",
        as_of=AS_OF,
    )
    assert report.reason_code is OutcomeCode.VERSION_MISMATCH
    assert any("2026年" in issue.detail for issue in report.issues)
    assert report.verification is None


def test_claim_scope_issue_uses_cited_source_not_any_matching_evidence() -> None:
    scope = parse_requested_scope("2028学年度出国成绩单认证费是多少元", AS_OF)
    current = current_fee_article()
    claim = Claim(
        claim_id="c1",
        text="出国成绩单认证费为每份15元。",
        citations=[
            Citation(
                article_id=current.article_id,
                quote=current.text,
                quote_start=0,
                quote_end=len(current.text),
                doc_title=current.doc_title,
                article_no=current.article_no,
                effective_from=current.effective_from,
                source_url=current.source_url,
            )
        ],
    )
    issues = claim_scope_issues(
        claim,
        scope,
        [Evidence(article=current), Evidence(article=future_fee_article())],
    )
    assert issues
    assert issues[0].scope is IssueScope.CLAIM
    assert issues[0].severity.value == "blocking"


def test_claim_scope_rejects_current_quote_borrowing_future_year() -> None:
    scope = parse_requested_scope("2028学年度出国成绩单认证费是多少元", AS_OF)
    current = current_fee_article()
    future = future_fee_article()
    claim = Claim(
        claim_id="c1",
        text="出国成绩单认证费为每份15元。",
        citations=[make_citation(current), make_citation(future)],
    )
    issues = claim_scope_issues(
        claim,
        scope,
        [Evidence(article=current), Evidence(article=future)],
    )
    assert issues
    assert issues[0].scope is IssueScope.CLAIM
    assert "2028-2029学年" in issues[0].detail


def test_claim_scope_same_article_other_clause_year_does_not_lend_proof() -> None:
    scope = parse_requested_scope("2028年出国成绩单认证费是多少元", AS_OF)
    article = make_article(
        "fee-and-award#第一条",
        "出国成绩单认证费及奖学金事项",
        "2028年度奖学金标准为3000元。出国成绩单认证费为每份15元。",
    )
    claim = Claim(
        claim_id="c1",
        text="出国成绩单认证费为每份15元。",
        citations=[make_citation(article, quote="出国成绩单认证费为每份15元。")],
    )
    issues = claim_scope_issues(claim, scope, [Evidence(article=article)])
    assert issues
    assert issues[0].scope is IssueScope.CLAIM


def test_claim_scope_accepts_scope_matching_future_fact() -> None:
    scope = parse_requested_scope("2028学年度出国成绩单认证费是多少元", AS_OF)
    future = future_fee_article()
    claim = Claim(
        claim_id="c1",
        text="2028学年度出国成绩单认证费为每份20元。",
        citations=[make_citation(future)],
    )
    assert claim_scope_issues(claim, scope, [Evidence(article=future)]) == []


def test_claim_scope_accepts_unqualified_quote_under_trustworthy_title() -> None:
    scope = parse_requested_scope("2028学年度出国成绩单认证费是多少元", AS_OF)
    future = make_article(
        "fee-title#第一条",
        "华南农业大学2028学年度出国成绩单认证费标准",
        "出国成绩单认证费为每份20元。",
    )
    claim = Claim(
        claim_id="c1",
        text="出国成绩单认证费为每份20元。",
        citations=[make_citation(future)],
    )
    assert claim_scope_issues(claim, scope, [Evidence(article=future)]) == []


def test_claim_scope_accepts_two_local_year_proofs_for_mixed_scope() -> None:
    scope = parse_requested_scope(
        "2025年度和2026年度出国成绩单认证费各是多少元", AS_OF
    )
    past = annual_fee_article(2025)
    current = annual_fee_article(2026)
    evidence = [Evidence(article=past), Evidence(article=current)]
    past_claim = Claim(
        claim_id="c1",
        text="2025年度出国成绩单认证费为每份15元。",
        citations=[make_citation(past)],
    )
    current_claim = Claim(
        claim_id="c2",
        text="2026年度出国成绩单认证费按每份15元收取。",
        citations=[make_citation(current)],
    )
    assert claim_scope_issues(past_claim, scope, evidence) == []
    assert claim_scope_issues(current_claim, scope, evidence) == []


def test_missing_targets_reports_unanswered_mixed_year() -> None:
    scope = parse_requested_scope(
        "2025年度和2026年度出国成绩单认证费各是多少元", AS_OF
    )
    past = annual_fee_article(2025)
    claim = Claim(
        claim_id="c1",
        text="出国成绩单认证费为每份15元。",
        citations=[make_citation(past)],
    )
    evidence = [Evidence(article=past)]
    assert claim_scope_issues(claim, scope, evidence) == []
    missing = missing_future_targets([claim], scope, evidence)
    assert [target.label() for target in missing] == ["2026年"]


def test_future_publication_cannot_prove_price_at_passed_as_of() -> None:
    question = "2028学年度出国成绩单认证费是多少元"
    target = parse_requested_scope(question, AS_OF).future_targets[0]
    future = make_article(
        "fee-upcoming#第一条",
        "2028学年度出国成绩单认证费标准",
        "2028学年度出国成绩单认证费为每份20元。",
        effective_from=date(2026, 1, 1),
        published_at=date(2028, 1, 1),
    )
    assert (
        source_applicability(future, target, question=question, as_of=AS_OF).status
        != "matched"
    )
    assert (
        source_applicability(
            future,
            target,
            question=question,
            as_of=date(2028, 2, 1),
        ).status
        == "matched"
    )


def test_checklist_scope_gate_requires_future_citation() -> None:
    scope = parse_requested_scope("2028学年度出国成绩单认证费是多少元", AS_OF)
    future = future_fee_article()
    current = current_fee_article()
    supported = ChecklistItem(
        step_id="apply",
        title="提交申请",
        status="available",
        description="提交成绩单认证申请。",
        department="财务处",
        citations=[future.article_id],
    )
    unsupported = supported.model_copy(update={"citations": [current.article_id]})
    assert _checklist_scope_supported([supported], [Evidence(article=future)], scope)
    assert not _checklist_scope_supported([unsupported], [Evidence(article=current)], scope)
    assert not _checklist_scope_supported([supported], [Evidence(article=current)], scope)


def test_existing_profile_guard_still_rejects_wrong_class_type() -> None:
    article = make_article(
        "class-scope#第一条",
        "创新班培养方案",
        "创新班执行单独培养方案。",
        audience_detail=AudienceDetail(class_types=["regular"]),
    )
    result = apply_scope(
        [Evidence(article=article)],
        profile=UserProfile(class_types=["innovation"]),
        as_of=AS_OF,
    )
    assert result.evidence == []
    assert result.reason_code is OutcomeCode.UNVERIFIABLE
    assert any(issue.code == "PROFILE_MISMATCH" for issue in result.issues)
