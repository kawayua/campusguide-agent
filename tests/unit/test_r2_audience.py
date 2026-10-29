"""R2 guard cases: asker identity must not be inferred from the topic role.

Report cases 028/043/059/060/096 failed because "研究生" appeared in the question
and the planner hard-filtered retrieval to graduate-tagged documents, dropping
admissions, supervisor-selection and degree documents. 007 has the opposite
requirement: the graduate role *is* the rule subject, and a master-specific
document must still satisfy it.
"""

from __future__ import annotations

from datetime import date

import pytest

from campusguide.agents.planner import Planner, PlannerContext, _audience_from_text
from campusguide.domain.audience_match import audience_matches, expand_audience
from campusguide.domain.enums import Audience
from campusguide.verification.scope import apply_scope
from campusguide.domain.models import Evidence
from tests.w6_helpers import article

OBJECT_QUESTIONS = {
    28: "农学院2026年硕士研究生复试包括哪些环节？",
    43: "农学院博士生导师和硕士生导师的岗位资格选聘条件是什么？",
    59: "研究生申请学位论文答辩和硕士、博士学位，需要满足哪些基本条件和程序要求？",
    60: "申请2027年推荐免试研究生需要提交哪些材料？",
    96: "农学院2026年申请考核制博士招生的条件是什么？",
}


@pytest.mark.parametrize("number,question", sorted(OBJECT_QUESTIONS.items()))
def test_topic_role_is_not_the_asker(number: int, question: str) -> None:
    assert _audience_from_text(question) is None, f"case {number}"


@pytest.mark.parametrize(
    "question,expected",
    [
        ("本科生如何申请勤工助学岗位？", Audience.UNDERGRADUATE),
        ("研究生课程考核不合格后如何处理？", Audience.GRADUATE),
        ("我是一名研究生，怎么申请国家奖学金？", Audience.GRADUATE),
        ("本科生和研究生补考成绩如何记载？", None),
        ("学院的研究生工作由谁负责？", None),
    ],
)
def test_asker_identity_only_when_the_role_is_personal(
    question: str, expected: Audience | None
) -> None:
    assert _audience_from_text(question) is expected


def test_master_document_satisfies_graduate_question() -> None:
    assert audience_matches({Audience.GRADUATE_MASTER}, Audience.GRADUATE)
    assert audience_matches({Audience.GRADUATE}, Audience.GRADUATE_MASTER)
    assert not audience_matches({Audience.UNDERGRADUATE}, Audience.GRADUATE)
    assert audience_matches({Audience.ALL}, Audience.GRADUATE)
    assert Audience.GRADUATE_MASTER in expand_audience([Audience.GRADUATE])


def test_scope_keeps_master_document_for_graduate_scope() -> None:
    article_obj = article("national-aid-undergrad@v2021#第九条")
    article_obj = article_obj.model_copy(update={"audience": [Audience.GRADUATE_MASTER]})
    evidence = [Evidence(article=article_obj)]
    result = apply_scope(evidence, audience=Audience.GRADUATE, as_of=date(2026, 10, 25))
    assert [item.article.article_id for item in result.evidence] == [article_obj.article_id]
    assert not [i for i in result.issues if i.code == "AUDIENCE_MISMATCH"]


class EmptyCatalog:
    def list_flows(self, **_kwargs):  # noqa: ANN003
        return []


def test_admissions_question_keeps_retrieval_unfiltered() -> None:
    plan = Planner(EmptyCatalog(), llm_enabled=False).plan(
        PlannerContext(message=OBJECT_QUESTIONS[28], as_of=date(2026, 10, 25))
    )
    assert all("audience" not in query.filters for query in plan.queries)