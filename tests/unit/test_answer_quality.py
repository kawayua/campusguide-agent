"""Observable answer-quality regressions, including scope and completeness negatives."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import httpx
import pytest

from campusguide.agents.advisor import Advisor, build_evidence_block
from campusguide.agents.coverage import CoverageChecker
from campusguide.agents.graph import GraphNodes, route_after_coverage
from campusguide.agents.plan import Plan, QueryPlanItem, QuestionUnit
from campusguide.agents.planner import Planner, PlannerContext
from campusguide.agents.providers import ChatProviderError, FakeChatProvider, OpenAICompatibleChat
from campusguide.agents.question_units import decompose_question
from campusguide.agents.retriever import RetrievalBundle
from campusguide.config import load_organization
from campusguide.domain.audience import AudienceDetail
from campusguide.domain.enums import Audience, BlockType
from campusguide.domain.models import PolicyFilters
from campusguide.retrieval.budget import evidence_cost
from campusguide.retrieval.corpus_views import CorpusViews, roster_names
from campusguide.retrieval.hybrid import HybridRetriever, _Candidate
from campusguide.retrieval.profile_expand import ProfileLexicon
from campusguide.verification.semantic import SemanticVerifier
from tests.unit.test_w6_semantic import labels_json, make_claim
from tests.w2_helpers import make_article, make_evidence

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def lexicon():
    return ProfileLexicon.from_organization(load_organization(ROOT / "configs/organization.yaml"))


class EmptyCatalog:
    def list_flows(self, **kwargs):
        return []


def planned(text, lexicon, **kwargs):
    return Planner(EmptyCatalog(), llm_enabled=False, profile_lexicon=lexicon).plan(
        PlannerContext(message=text, as_of=date(2026, 10, 25), **kwargs)
    )


def test_context_modifier_is_not_a_standalone_question(lexicon):
    plan = planned("对于2024年入学的计算机科学与技术学生，人工智能选修组有哪些科目？", lexicon)
    assert len(plan.queries) == 1
    assert "计算机科学与技术" in plan.queries[0].text
    assert plan.queries[0].filters["profile"] == {
        "intake_year": 2024,
        "majors": ["computer-science"],
    }
    assert {u.cohort for u in plan.question_units} == {2024}
    assert {u.subject for u in plan.question_units} == {"计算机科学与技术"}


def test_versions_remain_separate_in_queries_and_units(lexicon):
    plan = planned("计算机科学与技术2022版和2024版培养方案的毕业总学分有何不同？", lexicon)
    assert len(plan.queries) == 2
    assert {q.filters["profile"]["intake_year"] for q in plan.queries} == {2022, 2024}
    assert {u.cohort for u in plan.question_units} == {2022, 2024}


def test_parallel_major_aspects_are_not_dropped(lexicon):
    units = decompose_question("2024级统计学和信息与计算科学的核心课程与学制是什么？", lexicon)
    assert {(u.subject, u.requested_aspects[0]) for u in units} == {
        (major, aspect) for major in ["统计学", "信息与计算科学"] for aspect in ["核心课程", "学制"]
    }


def test_graduate_work_is_an_object_not_user_identity(lexicon):
    plan = planned("学院的研究生工作由谁负责？", lexicon)
    assert not plan.needs_clarification
    assert all("audience" not in q.filters for q in plan.queries)


def test_unknown_role_clarifies_but_explicit_comparison_does_not(lexicon):
    assert planned("不及格后补考和重修成绩如何记载？", lexicon).needs_clarification
    plan = planned("本科生和研究生补考和重修成绩如何记载？", lexicon)
    assert not plan.needs_clarification


def test_unknown_entity_followup_preserves_original_question(lexicon):
    first = planned("两个学院有哪些本科专业？", lexicon)
    assert first.clarification.reason == "ENTITY_UNKNOWN"
    second = planned(
        "农学院和数学与信息学院",
        lexicon,
        pending_clarification=first.clarification.model_dump(),
        pending_question="两个学院有哪些本科专业？",
    )
    assert not second.needs_clarification
    assert all(word in second.queries[0].text for word in ["农学院", "数学与信息学院", "本科专业"])


def test_audience_followup_restores_generation_question(lexicon):
    context = PlannerContext(
        message="本科生",
        as_of=date(2026, 10, 25),
        pending_question="不及格后补考和重修成绩如何记载？",
        pending_clarification={"reason": "AUDIENCE_UNKNOWN"},
    )
    plan = Planner(EmptyCatalog(), llm_enabled=False, profile_lexicon=lexicon).plan(context)
    assert "补考" in context.message
    assert plan.queries[0].filters["audience"] == [Audience.UNDERGRADUATE.value]


def test_roster_normalization_does_not_concatenate_complete_names():
    names = roster_names("教授 张三 梁 云 李四 王小五")
    assert "梁云" in names
    assert "张三梁云" not in names
    assert "三梁" not in names
    assert "李四王" not in names


def test_roster_lookup_uses_metadata_college_and_original_body(lexicon):
    article = make_article(
        doc_title="计算机科学与工程系教师名单",
        article_no="计算机科学与工程系教师",
        text="教授 张三 梁 云 李四 王小五",
    )
    article.audience_detail = AudienceDetail(colleges=["info"])
    view = CorpusViews([article], lexicon)
    result = view.search("梁云是数学与信息学院的老师吗，属于哪个系？")
    assert (article.article_id, 1) in result.exact_keys
    assert result.articles[0].text == article.text
    assert "数学与信息学院" in build_evidence_block([make_evidence(article)])
    wrong = view.search("张三梁是数学与信息学院的老师吗？")
    assert not any(w.startswith("ROSTER_LOOKUP") for w in wrong.warnings)


def news_article(day, idx, title="某学院近一年学术动态"):
    return make_article(
        article_id=f"list@v2026#段落{idx}",
        doc_id="list",
        doc_title=title,
        article_no=f"段落{idx}",
        article_index=idx,
        text=f"{day}｜不同的学术报告{idx}｜https://example.edu.cn/{idx}",
    )


def test_latest_date_scans_full_collection_not_input_order(lexicon):
    rows = [
        news_article("2025-12-31", 2),
        news_article("2026-08-10", 27),
        news_article("2026-01-01", 3),
    ]
    result = CorpusViews(rows, lexicon).search("最近收录的一条学术报告消息是什么？")
    assert result.collection_query
    assert [a.text[:10] for a in result.articles] == ["2026-08-10"]
    assert any("scanned=3:matched=1" in w for w in result.warnings)


def test_monthly_news_complete_collection_excludes_neighbor_months(lexicon):
    rows = [
        news_article("2026-10-10", 2, "某学院新闻"),
        news_article("2026-09-10", 3, "某学院新闻"),
        news_article("2026-10-14", 4, "某学院新闻"),
    ]
    result = CorpusViews(rows, lexicon).search("2026年10月发布的新闻有哪些？")
    assert [a.text[:10] for a in result.articles] == ["2026-10-14", "2026-10-10"]
    assert rows[0].published_at == date(
        2021, 8, 30
    )  # Aggregate metadata never determines entry date.


def test_empty_month_does_not_return_different_month(lexicon):
    result = CorpusViews([news_article("2026-08-10", 2)], lexicon).search(
        "2026年7月学术动态有哪些？"
    )
    assert result.collection_query
    assert not result.articles
    assert any("matched=0" in w for w in result.warnings)


def test_derived_candidates_obey_all_filters():
    article = make_article(block_type=BlockType.SECTION)
    retriever = object.__new__(HybridRetriever)
    candidates = {(article.article_id, 1): _Candidate(article=article)}
    for filters in [
        PolicyFilters(block_types=[BlockType.ARTICLE]),
        PolicyFilters(block_types=[BlockType.SECTION], doc_ids=["other"]),
        PolicyFilters(block_types=[BlockType.SECTION], flow_ids=["other"]),
        PolicyFilters(block_types=[BlockType.SECTION], audience=[Audience.GRADUATE]),
    ]:
        kept, _ = retriever._post_filter(candidates, filters)
        assert not kept


def test_shared_budget_counts_heading_and_envelope():
    a = make_article(text="一" * 1000)
    assert evidence_cost(a) > 500
    b = a.model_copy(update={"doc_title": "二" * 400})
    assert evidence_cost(b) - evidence_cost(a) > 180


def coverage_response(status="answered", ids=None, missing=None):
    return json.dumps(
        {
            "units": [
                {
                    "unit_id": "u1",
                    "status": status,
                    "claim_ids": ids or [],
                    "missing_items": missing or [],
                }
            ],
            "unanswered_questions": [],
        },
        ensure_ascii=False,
    )


def test_nonempty_evidence_cannot_count_as_answered():
    checker = CoverageChecker(FakeChatProvider([coverage_response()]))
    audit = checker.check(
        "核心课程有哪些？",
        [QuestionUnit(unit_id="u1", original_span="核心课程有哪些")],
        [],
        [make_evidence(make_article())],
        [],
    )
    assert not audit.unavailable
    assert audit.units[0]["status"] == "generation_missing"
    assert audit.missing


def test_coverage_rejects_unknown_unit_or_claim():
    for content in [
        coverage_response(ids=["invented"]),
        coverage_response().replace('"u1"', '"u2"'),
    ]:
        checker = CoverageChecker(FakeChatProvider([content]))
        audit = checker.check(
            "问题", [QuestionUnit(unit_id="u1", original_span="问题")], [], [], []
        )
        assert audit.unavailable
        assert audit.units[0]["status"] == "verification_failed"


def test_missing_list_items_cannot_be_marked_complete():
    claim = make_claim()
    checker = CoverageChecker(
        FakeChatProvider([coverage_response(ids=[claim.claim_id], missing=["第二项课程"])])
    )
    audit = checker.check(
        "核心课程有哪些",
        [QuestionUnit(unit_id="u1", original_span="核心课程有哪些")],
        [claim],
        [],
        [],
    )
    assert audit.units[0]["status"] == "generation_missing"
    assert "第二项课程" in audit.missing


def test_semantic_checks_every_claim_after_five():
    provider = FakeChatProvider(
        [
            labels_json(*[(f"c{i}", "supported") for i in range(5)]),
            labels_json(("c5", "contradicted")),
        ]
    )
    report = SemanticVerifier(provider).check([make_claim(f"c{i}") for i in range(6)], [])
    assert len(provider.calls) == 2
    assert report.checked == 6
    assert report.contradicted_ids == ["c5"]


def test_one_supplement_round_preserves_verified_claims():
    class Retriever:
        calls = 0

        def retrieve(self, *args, **kwargs):
            self.calls += 1
            return RetrievalBundle()

    nodes = object.__new__(GraphNodes)
    nodes._retriever = Retriever()
    nodes._today = date(2026, 10, 25)
    unit = QuestionUnit(
        unit_id="u1", original_span="核心课程与实践有哪些", requested_aspects=["实践课程"]
    )
    claim = make_claim()
    state = {
        "plan": Plan(question_units=[unit], queries=[QueryPlanItem(query_id="q1", text="课程")]),
        "unit_coverage": [{"unit_id": "u1", "status": "generation_missing"}],
        "claims": [claim],
        "evidence": [],
        "question": unit.original_span,
    }
    update = nodes.supplement(state)
    assert update["repair_verified_claims"] == [claim]
    assert update["coverage_repair_count"] == 1
    # R4：允许第二轮按单元补查，第三轮必须停止。
    second = nodes.supplement({**state, **update})
    assert second["coverage_repair_count"] == 2
    assert second["repair_verified_claims"] == [claim]
    assert nodes._retriever.calls == 2
    third = nodes.supplement({**state, **update, **second})
    assert nodes._retriever.calls == 2
    assert not third["coverage_repair_pending"]
    assert route_after_coverage({"coverage_repair_pending": True}) == "supplement"


def test_billing_error_is_not_retried_by_provider_or_advisor():
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(402, json={"error": "insufficient balance"})

    provider = OpenAICompatibleChat(
        base_url="https://example.invalid/v1",
        api_key="private",
        model="test",
        transport=httpx.MockTransport(handler),
    )
    with pytest.raises(ChatProviderError) as error:
        Advisor(provider).generate("问题", [])
    assert error.value.status_code == 402
    assert not error.value.retryable
    assert len(calls) == 1


def test_quote_pointer_copies_original_without_reformatting():
    from campusguide.agents.advisor import parse_claims_json
    from campusguide.retrieval.source_spans import source_spans

    article = make_article(
        text="人工智能方向 | 603917 | 数据分析与数据挖掘\nDataMining | 2 | 32 | 32\n613068 | 人工智能\nAI | 2 | 32 | 32"
    )
    evidence = [make_evidence(article)]
    spans = source_spans(evidence)
    row = spans["source_1_row_1"]
    assert row.quote == article.text[row.start : row.end]
    assert "613068" not in row.quote
    payload = {
        "claims": [
            {
                "text": "数据分析与数据挖掘可选修。",
                "citations": [{"article_id": article.article_id, "quote_id": row.span_id}],
            }
        ]
    }
    draft = parse_claims_json(json.dumps(payload, ensure_ascii=False), evidence).claims[0]
    assert draft.citations[0].quote == row.quote


def test_quote_pointer_cannot_reference_other_evidence_or_fabricate_body():
    from campusguide.agents.advisor import AdvisorError, parse_claims_json

    evidence = [make_evidence(make_article())]
    for citation in [
        {"quote_id": "source_2_body"},
        {"quote_id": "source_1_body", "article_id": "another"},
        {"quote_id": "source_1_body", "quote": "篡改正文内容"},
    ]:
        with pytest.raises(AdvisorError):
            parse_claims_json(json.dumps({"claims": [{"citations": [citation]}]}), evidence)


def test_table_module_boundary_does_not_inherit_next_module():
    from campusguide.retrieval.source_spans import source_spans

    article = make_article(
        text="甲方向 | 603917 | 甲课程名称\nA | 2 | 32 | 32\n乙选修模块 | 613068 | 乙课程名称\nB | 3 | 48 | 48"
    )
    rows = [r for r in source_spans([make_evidence(article)]).values() if "_row_" in r.span_id]
    assert rows[0].module == "甲方向"
    assert "乙选修模块" not in rows[0].quote
    assert "甲课程名称" not in rows[1].quote


def test_intake_metadata_cannot_support_deadline_or_wrong_cohort():
    from campusguide.verification.facts import check_evidence_fact_coverage

    article = make_article(text="本专业核心课程包括程序设计和数据结构。")
    article.audience_detail = AudienceDetail(intake_years=[2024])
    assert not check_evidence_fact_coverage(
        "2024年入学学生核心课包括程序设计。", [article.text], [article]
    ).uncovered
    for claim in ["截止时间为2024年。", "2023年入学的学生应学程序设计。", "学制为2024年。"]:
        assert check_evidence_fact_coverage(claim, [article.text], [article]).uncovered


def test_table_credit_requires_same_source_header_and_identified_row():
    from campusguide.verification.facts import check_evidence_fact_coverage

    header = "课程代码 | 课程名称 | 学分 | 学时 | 修读学期"
    row = "603917 | 数据分析与数据挖掘\nDataMining | 2 | 32 | 32"
    a = make_article(text=header)
    b = make_article(text=row, part_no=2)
    text = "数据分析与数据挖掘为2学分。"
    assert not check_evidence_fact_coverage(text, [header, row], [a, b]).uncovered
    assert check_evidence_fact_coverage(text, [row], [b]).uncovered
    other = a.model_copy(update={"version_id": "v2022"})
    assert check_evidence_fact_coverage(text, [header, row], [other, b]).uncovered
    assert check_evidence_fact_coverage("完全不同的课程为2学分。", [header, row], [a, b]).uncovered
    assert check_evidence_fact_coverage(
        "数据分析与数据挖掘为3学分。", [header, row], [a, b]
    ).uncovered


def test_source_view_invalidates_after_source_change(tmp_path, lexicon):
    path = tmp_path / "articles.jsonl"
    first = news_article("2026-01-01", 2)
    path.write_text(first.model_dump_json() + "\n", encoding="utf8")
    view = CorpusViews.load(path, lexicon)
    old_hash = view.source_hash
    newer = news_article("2026-08-10", 3)
    path.write_text(
        first.model_dump_json() + "\n" + newer.model_dump_json() + "\n", encoding="utf8"
    )
    result = view.search("最近收录的学术报告是哪条？")
    assert result.articles[0].text.startswith("2026-08-10")
    assert view.source_hash != old_hash


def test_implicit_shared_year_suffix_does_not_become_single_year(lexicon):
    from campusguide.agents.question_units import cohort_years, query_profile

    question = "2025、2026版计算机科学与技术研究生培养方案的实践要求"
    assert cohort_years(question) == [2025, 2026]
    assert "intake_year" not in query_profile(question, lexicon).get("profile", {})
    assert {u.cohort for u in decompose_question(question, lexicon)} == {2025, 2026}


def test_curriculum_core_and_practice_each_keep_direct_section():
    from campusguide.agents.retriever import GraphRetriever
    from campusguide.agents.retriever import _Candidate as Candidate

    core = make_evidence(
        make_article(
            article_id="plan#core", article_no="专业核心课程", text="核心课程包括甲课程与乙课程。"
        ),
        score=0.65,
    )
    practice = make_evidence(
        make_article(
            article_id="plan#practice",
            article_no="培养计划进程表",
            text="集中实践包括毕业实习和课程设计。",
        ),
        score=0.61,
    )
    irrelevant = make_evidence(
        make_article(
            article_id="plan#matrix", article_no="培养目标实现矩阵", text="相关矩阵信息。"
        ),
        score=0.99,
    )
    for hit in [core, practice, irrelevant]:
        hit.matched_queries = ["q1"]
    candidates = {
        hit.key: Candidate(evidence=hit, queries={"q1"}) for hit in [core, practice, irrelevant]
    }
    plan = Plan(
        question_units=[
            QuestionUnit(unit_id="u1", original_span="核心与实践", requested_aspects=["核心课程"]),
            QuestionUnit(unit_id="u2", original_span="核心与实践", requested_aspects=["实践课程"]),
        ]
    )
    retriever = GraphRetriever(None, max_evidence=3, max_per_doc=1)
    selected = retriever._select(
        candidates, {"q1": {hit.key: hit.score for hit in [core, practice, irrelevant]}}, plan=plan
    )
    assert core.key in {e.key for e in selected}
    assert practice.key in {e.key for e in selected}


def test_course_coverage_catches_omitted_named_course_even_if_model_says_answered():
    from campusguide.agents.advisor import CitationDraft, ClaimDraft
    from campusguide.verification.claims import ClaimValidator

    article = make_article(article_no="专业核心课程", text="甲课程、乙课程、丙课程。")
    evidence = [make_evidence(article)]
    claim = (
        ClaimValidator()
        .check(
            [
                ClaimDraft(
                    text="核心课程包括甲课程和乙课程。",
                    citations=[CitationDraft(article_id=article.article_id, quote=article.text)],
                )
            ],
            evidence,
        )
        .claims[0]
    )
    checker = CoverageChecker(FakeChatProvider([coverage_response(ids=[claim.claim_id])]))
    unit = QuestionUnit(
        unit_id="u1", original_span="核心课程有哪些？", requested_aspects=["核心课程"]
    )
    audit = checker.check(unit.original_span, [unit], [claim], evidence, [])
    assert audit.units[0]["status"] == "generation_missing"
    assert "丙课程" in audit.missing


def test_named_notice_year_is_scope_but_publication_date_is_not():
    from campusguide.verification.facts import check_evidence_fact_coverage

    article = make_article(
        doc_title="关于2026年本科生国家奖学金评审工作的通知", text="申请材料为审批表和成绩证明。"
    )
    assert not check_evidence_fact_coverage(
        "2026年本科生国家奖学金需要审批表。", [article.text], [article]
    ).uncovered
    for text in ["该通知发布于2026年。", "2026年截止申请。", "可获得2026元。"]:
        assert check_evidence_fact_coverage(text, [article.text], [article]).uncovered


def test_comparison_queries_do_not_readd_entire_object_list(lexicon):
    plan = planned("考试迟到、携带手机或者替考，分别涉及哪些考场规定和纪律处分？", lexicon)
    assert len(plan.queries) == 3
    assert "手机" not in plan.queries[0].text
    assert "迟到" not in plan.queries[2].text


def test_rerank_facets_preserve_subject_and_have_bound():
    from campusguide.retrieval.query_views import rerank_query_views

    views = rerank_query_views("微专业有什么区别，报名条件和完成后取得的证书是什么？")
    assert len(views) == 3
    assert views[0].startswith("微专业")
    assert "微专业 申请条件" in views
    assert "微专业 定义" in views
    assert rerank_query_views("奖学金金额是多少？") == ["奖学金金额是多少？"]


def test_latest_claim_requires_collection_scan_proof():
    from campusguide.agents.advisor import CitationDraft, ClaimDraft
    from campusguide.agents.verifier import GraphVerifier
    from campusguide.domain.enums import GraphDecision

    article = make_article(text="2026-08-10｜学术报告：测试报告｜https://example.edu.cn/a")
    evidence = [make_evidence(article)]
    draft = ClaimDraft(
        claim_id="c1",
        text="最新收录的学术报告是2026-08-10的测试报告。",
        citations=[CitationDraft(article_id=article.article_id, quote=article.text)],
    )
    for warnings, expected in [
        ([], GraphDecision.ABSTAIN),
        (["COLLECTION_SCAN:list:scanned=50:matched=1"], GraphDecision.PASS),
    ]:
        semantic = SemanticVerifier(FakeChatProvider([labels_json(("c1", "supported"))]))
        report = GraphVerifier(semantic_verifier=semantic, max_revise=0).verify(
            [draft], evidence, question="最新学术报告是什么", retrieval_warnings=warnings
        )
        assert report.decision is expected
