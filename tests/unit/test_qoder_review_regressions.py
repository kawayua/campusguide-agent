"""Review regressions: comparison facts stay attached to their own source scope."""
from campusguide.agents.coverage import _claim_supports_unit
from campusguide.agents.plan import QuestionUnit
from campusguide.retrieval.source_spans import source_spans
from tests.unit.test_r4_coverage_binding import _claim
from tests.w2_helpers import make_article, make_evidence


def _version_unit(year):
    return QuestionUnit(
        unit_id=f"v{year}", original_span="比较两个版本的培养目标",
        subject="农学", cohort=year, version=str(year), requested_aspects=["培养目标"],
    )


def test_gap_version_does_not_inherit_other_versions_answer():
    article = make_article(
        "plan@v2024#目标", doc_title="农学2024版培养方案", article_no="培养目标",
        text="2024版农学培养目标为培养农业科研人才；2022版未提供完整对比。",
    )
    claim = _claim(article, article.text)
    assert _claim_supports_unit(_version_unit(2024), claim)
    assert not _claim_supports_unit(_version_unit(2022), claim)


def test_extra_citation_does_not_turn_single_version_fact_into_comparison():
    a24 = make_article(
        "plan@v2024#目标", doc_title="农学2024版培养方案", article_no="培养目标",
        text="2024版农学培养目标为培养农业科研人才。",
    )
    a22 = make_article(
        "plan@v2022#目标", doc_title="农学2022版培养方案", article_no="培养目标",
        text="2022版农学培养目标为培养农业技术人才。",
    )
    claim = _claim(a24, a24.text)
    claim.citations.extend(_claim(a22, a22.text).citations)
    assert _claim_supports_unit(_version_unit(2024), claim)
    assert not _claim_supports_unit(_version_unit(2022), claim)


def test_missing_class_scope_does_not_inherit_other_class_answer():
    terms = {"dy": ("农学丁颖创新班", "丁颖创新班"), "plain": ("农学普通班", "普通农学")}
    article = make_article(
        "plan@v2024#目标", doc_title="农学丁颖创新班培养方案", article_no="培养目标",
        text="农学丁颖创新班培养目标为培养农业科研人才；农学普通班未提供完整对比。",
    )
    claim = _claim(article, article.text)
    plain_id = next(k for k, aliases in terms.items() if "农学普通班" in aliases)
    unit = QuestionUnit(
        unit_id="plain", original_span="比较丁颖创新班与农学普通班培养目标",
        subject="农学", class_type=plain_id, requested_aspects=["培养目标"],
    )
    assert not _claim_supports_unit(unit, claim, class_terms=terms)


def test_multiline_numeric_cells_align_after_optional_category_column():
    header = make_article(
        "plan@v2024#进程表", part_no=1, article_no="进程表",
        text="课程类别 | 课程代码 | 课程名称 | 学分 | 总学时 | 理论学时 | 实验学时 | 周数 | 修读学期 | 开课单位",
    )
    row = make_article(
        "plan@v2024#进程表", part_no=3, article_no="进程表",
        text="617017 | 毕业论文/设计(工学)\nGraduation Thesis/Design | 6 | 210 | 0 | 0 | 14周 | 8 | 软件学院",
    )
    span = next(s for s in source_spans([make_evidence(header), make_evidence(row)]).values() if "_row_" in s.span_id)
    assert span.credits == "6"
    assert span.hours == "210"
    assert span.semester == "8"
    assert span.weeks in {"14", "14周"}
    assert span.quote == row.text
    assert span.quote == row.text[span.start:span.end]




def test_each_version_must_answer_its_own_aspect_in_mixed_claim():
    article = make_article(
        "plan@v2024#比较", doc_title="农学培养方案对比", article_no="培养目标和总学分",
        text="2024版农学培养目标为培养农业科研人才；2022版农学总学分为150学分。",
    )
    claim = _claim(article, article.text)
    assert _claim_supports_unit(_version_unit(2024), claim)
    assert not _claim_supports_unit(_version_unit(2022), claim)


def test_each_class_must_answer_its_own_aspect_in_mixed_claim():
    terms = {"dy": ("农学丁颖创新班", "丁颖创新班"), "plain": ("农学普通班", "普通农学")}
    article = make_article(
        "plan@v2024#比较", doc_title="农学班型比较", article_no="培养目标和总学分",
        text="农学丁颖创新班培养目标为培养农业科研人才；农学普通班总学分为150学分。",
    )
    claim = _claim(article, article.text)
    unit = QuestionUnit(
        unit_id="plain", original_span="比较两班型培养目标和学分", subject="农学",
        class_type="plain", requested_aspects=["培养目标"],
    )
    assert not _claim_supports_unit(unit, claim, class_terms=terms)


def test_supplement_keeps_hydrated_flow_sources_for_existing_checklist():
    from campusguide.agents.graph import _merge_supplement_evidence

    one = make_evidence(make_article("flow@v2024#第一条", doc_id="flow"))
    two = make_evidence(make_article("flow@v2024#第二条", doc_id="flow"))
    one.source = two.source = "flow"
    kept, warnings = _merge_supplement_evidence(
        [one, two], [], max_evidence=1, token_budget=10000, cited_keys=set(),
    )
    assert {item.key for item in kept} == {one.key, two.key}
    assert any("PROTECTED_OVER_COUNT" in warning for warning in warnings)


def test_structural_provenance_survives_merge_with_a_higher_score_query():
    from campusguide.agents.plan import Plan, QueryPlanItem
    from campusguide.agents.retriever import GraphRetriever
    from campusguide.domain.models import ArticlePublic, PolicyFilters
    from campusguide.mcp.client import SearchResponse
    from campusguide.mcp.schemas import SearchArticlesData, SearchHit

    article = make_article("roster@v2024#成员", version_id="v2024", text="张三为成员。")
    for structured_first in (True, False):
        class SearchClient:
            def search_articles(self, request, structured_first=structured_first):
                structured = (request.query == "姓名") == structured_first
                hit = SearchHit(
                    article_id=article.article_id, part_no=article.part_no,
                    block_type=article.block_type.value,
                    article=ArticlePublic.from_article(article),
                    rerank_score=0.1 if structured else 0.9,
                    structured=structured, structured_rank=0 if structured else None,
                )
                return SearchResponse(
                    data=SearchArticlesData(query=request.query, articles=[hit]),
                    server="test", latency_ms=0,
                )

        bundle = GraphRetriever(SearchClient(), enable_repealed_probe=False).retrieve(
            Plan(queries=[
                QueryPlanItem(query_id="q1", text="姓名"),
                QueryPlanItem(query_id="q2", text="成员归属"),
            ]),
            PolicyFilters(),
        )
        assert len(bundle.evidence) == 1
        item = bundle.evidence[0]
        assert item.structured
        assert item.structured_rank == 0
        assert item.rerank_score == 0.9
        assert set(item.matched_queries) == {"q1", "q2"}
