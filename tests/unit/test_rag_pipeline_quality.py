"""Regression checks for ranking scale and object/section coverage."""

from __future__ import annotations

import json
import math
import sys
from types import SimpleNamespace

import pytest

from campusguide.agents.question_units import decompose_question
from campusguide.agents.retriever import _filters_for_query
from campusguide.domain.models import Citation, Claim, Evidence, PolicyFilters
from campusguide.domain.profiles import UserProfile
from campusguide.retrieval.corpus_views import CorpusViews
from campusguide.retrieval.profile_expand import ProfileLexicon
from campusguide.retrieval.rerank import BgeReranker
from campusguide.verification.semantic import _item_payload
from tests.w2_helpers import make_article


def test_cross_encoder_scores_are_normalized_once(monkeypatch):
    class Identity:
        def __call__(self, value):
            return value

    def cross_encoder(*args, **kwargs):
        activation = kwargs.get("activation_fn", lambda x: 1 / (1 + math.exp(-x)))
        return SimpleNamespace(predict=lambda *a, **k: [activation(3), activation(-5)])

    monkeypatch.setitem(
        sys.modules, "torch", SimpleNamespace(nn=SimpleNamespace(Identity=Identity))
    )
    monkeypatch.setitem(
        sys.modules, "sentence_transformers", SimpleNamespace(CrossEncoder=cross_encoder)
    )
    scores = BgeReranker(device="cpu").score("question", ["relevant", "unrelated"])
    assert scores == pytest.approx([0.952574, 0.006693], abs=1e-6)


def _plan(heading, text, part=1, title="软件工程专业培养方案（2024版）", doc="se"):
    return make_article(
        f"{doc}@v2024#{heading}",
        doc_id=doc,
        version_id="v2024",
        doc_title=title,
        article_no=heading,
        text=text,
        part_no=part,
    )


def test_core_section_survives_many_high_overlap_timetable_fragments():
    core = _plan("六、专业核心课程", "操作系统、数据库系统、计算机网络。")
    tables = [
        _plan("十、培养计划进程表", "专业技能实践 实验教学 软件工程实践课程" * 15, n)
        for n in range(1, 25)
    ]
    views = CorpusViews([*tables, core], ProfileLexicon({}, {"se": ("软件工程",)}))
    selected = views.search("2024级软件工程专业的核心课程和实践课程", limit=8)
    assert core in selected.articles
    assert any(a.article_no == "六、专业核心课程" for a in selected.articles[:2])


def test_comparison_reserves_both_plan_documents():
    regular = _plan(
        "二、培养目标", "普通班培养目标", title="农学专业培养方案（2024版）", doc="regular"
    )
    special = [
        _plan(
            "十、培养计划进程表",
            "农学丁颖创新班课程安排 实验教学" * 20,
            n,
            title="农学丁颖创新班培养方案（2024版）",
            doc="special",
        )
        for n in range(1, 25)
    ]
    views = CorpusViews(
        [*special, regular], ProfileLexicon({}, {"agro": ("农学",)}, {"dy": ("丁颖创新班",)})
    )
    selected = views.search("2024级农学丁颖创新班和普通农学专业培养目标和课程安排区别", limit=6)
    assert {a.doc_id for a in selected.articles} == {"regular", "special"}


def test_practice_bases_do_not_require_curriculum_courses():
    units = decompose_question("学院有哪些校外实践教学基地，哪个是国家级工程实践中心？")
    assert any("实践基地" in u.requested_aspects for u in units)
    assert all("实践课程" not in u.requested_aspects for u in units)
    assert any(
        "实践课程" in u.requested_aspects for u in decompose_question("专业实践教学有哪些课程？")
    )


def test_named_major_does_not_inherit_speakers_other_college():
    base = PolicyFilters(profile=UserProfile(colleges=["info"], majors=["se"], intake_year=2024))
    result = _filters_for_query(base, {"profile": {"majors": ["agro"]}})
    assert result.profile.majors == ["agro"]
    assert result.profile.colleges == []
    assert result.profile.intake_year == 2024
    assert base.profile.colleges == ["info"]


def test_semantic_comparison_keeps_each_quotes_source_identity():
    articles = [
        _plan("培养目标", "培养基础研究能力", doc="old"),
        _plan("课程结构", "实践课程计20学分", doc="new"),
    ]
    citations = [
        Citation(
            article_id=a.article_id,
            part_no=a.part_no,
            quote=a.text,
            quote_start=0,
            quote_end=len(a.text),
            effective_from=a.effective_from,
            doc_title=a.doc_title,
            article_no=a.article_no,
            source_url=a.source_url,
        )
        for a in articles
    ]
    claim = Claim(claim_id="c1", text="两版方案培养目标和课程结构不同", citations=citations)
    payload = _item_payload(claim, [Evidence(article=a) for a in articles])
    assert [s["article_id"] for s in payload["sources"]] == [a.article_id for a in articles]
    assert payload["sources"][1]["article_no"] == "课程结构"
    assert "quote" not in payload
    assert "基础研究能力" in json.dumps(payload, ensure_ascii=False)


def test_class_registry_keeps_distinct_curricula_separate():
    from pathlib import Path

    from campusguide.agents.question_units import mentioned_class_types
    from campusguide.config import load_organization

    lexicon = ProfileLexicon.from_organization(
        load_organization(Path(__file__).resolve().parents[2] / "configs/organization.yaml")
    )
    assert mentioned_class_types("新农学创新班培养目标", lexicon) == [
        ("new-agronomy-innovation", "新农学创新班")
    ]
    assert mentioned_class_types("丁颖创新班培养目标", lexicon) == [
        ("dingying-innovation", "丁颖创新班")
    ]
    assert mentioned_class_types("创新班培养目标", lexicon) == []


def test_table_row_brings_original_header_from_preceding_fragment():
    header = _plan("十、培养计划进程表", "课程代码 | 课程名称 | 学分 | 学时 | 修读学期", 1)
    row = _plan("十、培养计划进程表", "617017 | 毕业论文/设计 | 6 | 210 | 8", 3)
    core = _plan("六、专业核心课程", "操作系统、数据库系统。")
    views = CorpusViews([header, row, core], ProfileLexicon({}, {"se": ("软件工程",)}))
    selected = views.search("2024级软件工程核心课程和毕业设计要求", limit=8)
    assert row in selected.articles
    assert header in selected.articles
    assert header.text == "课程代码 | 课程名称 | 学分 | 学时 | 修读学期"
    assert any(w.startswith("TABLE_HEADER_CONTEXT:") for w in selected.warnings)


def _claim(text, claim_id="c1"):
    a = _plan("课程设置", "软件工程课程包括操作系统、数据库系统。")
    citation = Citation(
        article_id=a.article_id,
        part_no=1,
        quote=a.text,
        quote_start=0,
        quote_end=len(a.text),
        effective_from=a.effective_from,
        doc_title=a.doc_title,
        article_no=a.article_no,
        source_url=a.source_url,
    )
    return Claim(claim_id=claim_id, text=text, citations=[citation])


def test_boundary_sentence_is_not_an_answered_comparison():
    from campusguide.agents.coverage import _claim_supports_unit
    from campusguide.agents.plan import QuestionUnit

    unit = QuestionUnit(
        unit_id="u1",
        original_span="软件工程课程设置有何不同",
        subject="软件工程",
        requested_aspects=["课程设置"],
    )
    claim = _claim("软件工程课程设置仅包含操作系统、数据库系统等，未提供另一个版本的完整对比细节。")
    assert not _claim_supports_unit(unit, claim)
    claim = _claim("软件工程课程包括操作系统、数据库系统，未提供完整的两边课程设置全貌对比。")
    assert not _claim_supports_unit(unit, claim)


def test_preserved_claim_with_verified_prefix_is_merged_once():
    from campusguide.qa.merge import merge_claims

    text = "2024级软件工程核心课程包括操作系统、数据库系统。"
    result = merge_claims([_claim(text), _claim("已核实：" + text, "c2")])
    assert len(result.claims) == 1
    assert result.claims[0].text == text


def test_header_expansion_does_not_leave_dangling_protected_keys():
    header = _plan("十、培养计划进程表", "课程代码 | 课程名称 | 学分 | 学时 | 修读学期", 1)
    rows = [
        _plan("十、培养计划进程表", f"{610000 + n} | 毕业论文/设计实践 | 6 | 210 | 8", n + 1)
        for n in range(1, 20)
    ]
    views = CorpusViews([header, *rows], ProfileLexicon({}, {"se": ("软件工程",)}))
    selected = views.search("2024级软件工程毕业设计实践课程", limit=3)
    assert len(selected.articles) <= 3
    assert selected.exact_keys <= {(a.article_id, a.part_no) for a in selected.articles}
