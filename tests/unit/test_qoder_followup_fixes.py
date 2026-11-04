"""Review fixes: class/version scoping, real flattened headers, strict supplement budget."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from campusguide.agents.graph import _merge_supplement_evidence
from campusguide.agents.plan import QuestionUnit
from campusguide.agents.retriever import GraphRetriever, _evidence_from_data
from campusguide.config import load_organization
from campusguide.domain.models import ArticlePublic
from campusguide.mcp.schemas import SearchArticlesData, SearchHit
from campusguide.retrieval.profile_expand import ProfileLexicon
from campusguide.retrieval.source_spans import source_spans, table_header_context
from campusguide.verification.coverage_items import required_course_items
from tests.w2_helpers import make_article, make_evidence

ROOT = Path(__file__).resolve().parents[2]
HEADER = "课程代码 | 课程名称 | 学分 | 学时"


def _lexicon() -> ProfileLexicon:
    return ProfileLexicon.from_organization(load_organization(ROOT / "configs/organization.yaml"))


def _practice_article(*, year: int, title: str, code: str, label: str):
    return make_article(
        f"plan@v{year}#课程设置",
        doc_id="plan",
        version_id=f"v{year}",
        doc_title=title,
        article_no="课程设置",
        text=f"{HEADER}\n{code} | {label} | 4 | 8周",
    )


def _practice_unit(*, class_type: str | None) -> QuestionUnit:
    return QuestionUnit(
        unit_id="u1",
        original_span="2024级农学专业实践课程有哪些？",
        subject="农学",
        objects=["农学"],
        cohort=2024,
        version="2024版",
        class_type=class_type,
        requested_aspects=["实践课程"],
    )


def test_required_course_items_filter_other_class_and_version() -> None:
    terms = _lexicon().class_terms
    general = _practice_article(
        year=2024,
        title="2024版农学专业培养方案",
        code="600001",
        label="毕业实习",
    )
    innovation = _practice_article(
        year=2024,
        title="2024版农学丁颖创新班培养方案",
        code="600002",
        label="创新实践",
    )
    old_general = _practice_article(
        year=2022,
        title="2022版农学专业培养方案",
        code="600003",
        label="旧版实习",
    )
    evidence = [
        make_evidence(general),
        make_evidence(innovation, rank=2),
        make_evidence(old_general, rank=3),
    ]

    general_items = required_course_items(
        _practice_unit(class_type="agronomy-general"), evidence, class_terms=terms
    )
    assert general_items == ["毕业实习"]

    innovation_items = required_course_items(
        _practice_unit(class_type="dingying-innovation"), evidence, class_terms=terms
    )
    assert innovation_items == ["创新实践"]


REAL_HEADER = (
    ". | 16 | 0 | 16 | 0 | 6 | 数学与信息学院软件学院/企业基地实验课程合计 | 12 | 384 | 0 | 384 "
    "| 0课程类别 | 课程代码 | 课程名称 | 学分 | 学时 | 修读学期 | 开课单位 | 备注总数 "
    "| 理论 | 实验 | 实习通用技能实践 | 600799 | 军事训练"
)
REAL_ROW = (
    "617017 | 毕业论文/设计(工学)\nGraduation Thesis/Design | 6 | 210 | 0 | 0 | 14周 "
    "| 8 | 数学与信息学院软件学院其他实践教育课程合计 | 25 | 544 | 0 | 0"
)


def test_real_flattened_multilevel_header_keeps_fields_as_gaps() -> None:
    article_id = "info-ug-se-plan@v2024#十、培养计划进程表"
    header = make_article(
        article_id,
        doc_id="info-ug-se-plan",
        version_id="v2024",
        doc_title="2024版软件工程专业培养方案",
        article_no="十、培养计划进程表",
        part_no=32,
        text=REAL_HEADER,
    )
    row = make_article(
        article_id,
        doc_id="info-ug-se-plan",
        version_id="v2024",
        doc_title="2024版软件工程专业培养方案",
        article_no="十、培养计划进程表",
        part_no=34,
        text=REAL_ROW,
    )
    evidence = [make_evidence(header, rank=1), make_evidence(row, rank=2)]
    spans = source_spans(evidence)
    row_span = next(span for span in spans.values() if "_row_" in span.span_id and span.part_no == 34)

    assert row_span.header_part_no == 32
    assert row_span.header_reliable is False
    # A flattened multi-level header cannot be mapped positionally: 210 is the
    # total hours cell but must not be guessed as credits or total hours.
    assert (row_span.credits, row_span.hours, row_span.semester) == ("", "", "")
    assert row_span.weeks == "14"
    assert row_span.headers[:4] == ("课程类别", "课程代码", "课程名称", "学分")
    assert not any(re.fullmatch(r"\d+", column) for column in row_span.headers)
    assert "600799" not in row_span.headers
    assert "军事训练" not in row_span.headers

    context = table_header_context(evidence, row_span.article_id, row_span.part_no, 0)
    assert context is not None
    assert context["reliable"] is False
    assert "600799" not in context["columns"]
    assert "军事训练" not in context["columns"]


def test_supplement_budget_is_hard_for_first_uncited_item() -> None:
    item = make_evidence(make_article("budget@v2024#一", doc_id="budget"))
    kept, warnings = _merge_supplement_evidence(
        [item], [], max_evidence=8, token_budget=1, cited_keys=set()
    )
    assert kept == []
    assert any(
        warning.startswith("SUPPLEMENT_BUDGET_SKIP:") and "reason=token" in warning
        for warning in warnings
    )

    cited, cited_warnings = _merge_supplement_evidence(
        [item], [], max_evidence=8, token_budget=1, cited_keys={item.key}
    )
    assert cited == [item]
    assert any(warning.startswith("SUPPLEMENT_PROTECTED_OVER_TOKEN:") for warning in cited_warnings)

    item.source = "flow"
    flow, flow_warnings = _merge_supplement_evidence(
        [item], [], max_evidence=8, token_budget=1, cited_keys=set()
    )
    assert flow == [item]
    assert any(warning.startswith("SUPPLEMENT_PROTECTED_OVER_TOKEN:") for warning in flow_warnings)


def test_structured_signal_survives_graph_retriever_hydration() -> None:
    article = make_article(
        "plan@v2024#进程表",
        doc_id="plan",
        version_id="v2024",
        article_no="进程表",
        part_no=3,
        text="课程代码 | 课程名称 | 学分 | 学时\n617017 | 毕业论文（设计） | 6 | 210",
    )
    public = ArticlePublic.from_article(article)
    hit = SearchHit(
        article_id=public.article_id,
        part_no=public.part_no,
        block_type=public.block_type.value,
        article=public,
        rerank_score=0.12,
        structured=True,
        structured_rank=2,
    )
    evidence = _evidence_from_data(SearchArticlesData(query="q", articles=[hit]), "q1")[0]
    assert evidence.structured is True
    assert evidence.structured_rank == 2
    assert evidence.rerank_score == pytest.approx(0.12)
    assert evidence.score == pytest.approx(0.12)

    plain = make_evidence(make_article("plain@v2024#一", doc_id="plain"))
    plain.rerank_score = 0.90
    assert GraphRetriever._selection_key(evidence) > GraphRetriever._selection_key(plain)


def test_unit_priority_filters_class_and_explicit_version() -> None:
    lexicon = _lexicon()
    retriever = GraphRetriever(None, profile_lexicon=lexicon)
    innovation = make_evidence(
        make_article(
            "plan@v2024#核心课程",
            doc_id="plan",
            version_id="v2024",
            doc_title="2024版农学丁颖创新班培养方案",
            article_no="核心课程",
        )
    )
    general = make_evidence(
        make_article(
            "plan@v2024#核心课程",
            doc_id="plan",
            version_id="v2024",
            doc_title="2024版农学专业培养方案",
            article_no="核心课程",
        )
    )
    old_innovation = make_evidence(
        make_article(
            "plan@v2022#核心课程",
            doc_id="plan",
            version_id="v2022",
            doc_title="2022版农学丁颖创新班培养方案",
            article_no="核心课程",
        )
    )
    unit = QuestionUnit(
        unit_id="u1",
        original_span="2024级农学丁颖创新班核心课程有哪些？",
        subject="农学",
        objects=["农学"],
        cohort=2024,
        version="2024版",
        class_type="dingying-innovation",
        requested_aspects=["核心课程"],
    )
    assert retriever._unit_priority(unit, innovation) > -1
    assert retriever._unit_priority(unit, general) == -1
    assert retriever._unit_priority(unit, old_innovation) == -1

    # Legacy units without class_type keep the original “普通” heuristic.
    legacy = unit.model_copy(
        update={
            "class_type": None,
            "original_span": "普通农学专业核心课程有哪些？",
        }
    )
    assert retriever._unit_priority(legacy, innovation) == -1
    assert retriever._unit_priority(legacy, general) > -1
