"""R3 guard cases: level, class type, version and section aware retrieval.

Report cases 005/030/031/041/061/064/071/088/092. The two named prohibitions are
"不得用本科表回答研究生实践" and "不得用分流补充方案替代创新班完整方案".
"""

from __future__ import annotations

from pathlib import Path

import pytest

from campusguide.agents.question_units import (
    mentioned_class_types,
    mentioned_level,
)
from campusguide.config import load_organization
from campusguide.retrieval.corpus_views import CorpusViews, is_supplement, level_conflict
from campusguide.retrieval.profile_expand import ProfileLexicon
from tests.w2_helpers import make_article

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(scope="module")
def lexicon() -> ProfileLexicon:
    return ProfileLexicon.from_organization(load_organization(ROOT / "configs/organization.yaml"))


def _plan(
    doc_id: str,
    title: str,
    *,
    version: str = "v2024",
    chapter: tuple[str, ...] = ("一、培养目标",),
    text: str = "本专业培养方案包括培养目标、课程设置与毕业要求。",
    part_no: int = 2,
):
    return make_article(
        f"{doc_id}@{version}#{chapter[-1] if chapter else '正文'}",
        doc_id=doc_id,
        version_id=version,
        doc_title=title,
        article_no=chapter[-1] if chapter else "正文",
        part_no=part_no,
        text=text,
    )


UNDER_AGRO = _plan("agro-plan-2024", "农学专业本科培养方案（2024版）", version="v2024")
GRADUATE_PRACTICE = _plan(
    "grad-practice-2024",
    "研究生实践环节培养方案（2024版）",
    version="v2024g",
    text="研究生实践环节包括专业实践与学术活动，计6学分。",
)
INNOVATION_PLAN = _plan(
    "agro-innovation-2024",
    "农学丁颖创新班培养方案（2024版）",
    version="v2024i",
    chapter=("农学丁颖创新班", "课程设置"),
    text="创新班课程设置包括通识课程、核心课程与个性化课程。",
)
SUPPLEMENT = _plan(
    "agro-stream-2024",
    "农学专业分流补充方案",
    version="v2024s",
    text="分流补充方案对创新班课程设置作出调整。",
)


def _views(*articles) -> CorpusViews:
    return CorpusViews(list(articles), ProfileLexicon({}, {}))


def test_level_words_map_to_one_layer(lexicon: ProfileLexicon) -> None:
    assert mentioned_level("研究生的实践环节有哪些") == "graduate"
    assert mentioned_level("本科生实践课程有哪些") == "undergraduate"
    assert mentioned_level("本科生和研究生实践环节有何不同") is None
    assert mentioned_level("实践环节有哪些") is None


def test_class_types_come_from_the_registry(lexicon: ProfileLexicon) -> None:
    assert [name for _, name in mentioned_class_types("农学丁颖创新班课程设置", lexicon)] == [
        "丁颖创新班"
    ]
    assert mentioned_class_types("农学专业课程设置", lexicon) == []


def test_level_conflict_and_supplement_helpers() -> None:
    assert level_conflict("研究生实践环节培养方案", "undergraduate")
    assert level_conflict("农学专业本科培养方案", "graduate")
    assert not level_conflict("农学专业培养方案", "undergraduate")
    assert is_supplement("农学专业分流补充方案")
    assert not is_supplement("农学丁颖创新班培养方案")


def test_graduate_practice_question_does_not_return_undergraduate_plan(lexicon) -> None:
    views = CorpusViews([UNDER_AGRO, GRADUATE_PRACTICE], lexicon)
    result = views.search("研究生实践环节有哪些学分要求")
    ids = {article.doc_id for article in result.articles}
    assert "grad-practice-2024" in ids
    assert "agro-plan-2024" not in ids


def test_undergraduate_question_does_not_return_graduate_plan(lexicon) -> None:
    views = CorpusViews([UNDER_AGRO, GRADUATE_PRACTICE], lexicon)
    ids = {article.doc_id for article in views.search("本科农学专业培养方案课程设置").articles}
    assert "agro-plan-2024" in ids
    assert "grad-practice-2024" not in ids


def test_innovation_class_question_prefers_the_full_plan(lexicon) -> None:
    views = CorpusViews([SUPPLEMENT, INNOVATION_PLAN], lexicon)
    result = views.search("农学丁颖创新班培养方案的课程设置")
    assert result.articles[0].doc_id == "agro-innovation-2024"
    assert "agro-stream-2024" not in {article.doc_id for article in result.articles[:1]}


def test_generic_question_still_ranks_but_supplement_is_penalised(lexicon) -> None:
    views = CorpusViews([SUPPLEMENT, INNOVATION_PLAN], lexicon)
    ranked = [article.doc_id for article in views.search("农学专业培养方案课程设置").articles]
    assert ranked.index("agro-innovation-2024") < ranked.index("agro-stream-2024")


def test_version_specific_plan_is_selected(lexicon) -> None:
    plan_2022 = _plan("agro-plan-2022", "农学专业培养方案（2022版）", version="v2022")
    plan_2024 = _plan("agro-plan-2024", "农学专业培养方案（2024版）", version="v2024")
    views = CorpusViews([plan_2022, plan_2024], lexicon)
    ids = {article.doc_id for article in views.search("2024级农学专业培养方案课程设置").articles}
    assert ids == {"agro-plan-2024"}