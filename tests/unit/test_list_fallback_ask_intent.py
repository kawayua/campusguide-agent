"""The list fallback must require an enumeration intent bound to the asked type."""

from __future__ import annotations

import pytest

from campusguide.agents.verifier import GraphVerifier
from campusguide.qa.list_claim import asked_entity_type, build_list_claim
from campusguide.verification.entities import items_of_type
from tests.w2_helpers import make_article, make_evidence

# The extractor turns these regulation fragments into pseudo laboratory names,
# so only the intent gate can stop a fabricated list.
SAFETY_TEXT = (
    "各实验室安全责任人负责实验室安全管理，实验室安全工作领导小组负责实验室安全检查。"
)
LAB_TEXT = "广东省农业生态重点实验室、广东省畜禽养殖重点实验室、广东省动物疫病重点实验室。"
CENTRE_TEXT = (
    "计算机实验教学示范中心、信息数学与应用统计实验教学示范中心、数学建模实验教学示范中心。"
)
PLAIN_LAB_TEXT = "计算机软件实验室、计算机网络实验室、云计算与大数据实验室。"
RESEARCH_TEXT = "广东省农业工程研究中心、广东省农产品加工研究中心。"


def _evidence(
    text: str,
    title: str = "某学院教学科研平台",
    article_id: str = "platform#第一条",
):
    return make_evidence(make_article(article_id, doc_title=title, text=text))


NEGATIVE_QUESTIONS = [
    "谁负责实验室安全？",
    "哪些领导负责实验室安全？",
    "实验室安全有哪些规定？",
    "实验室安全管理有哪些职责？",
    "有实验室吗？",
    "实验室管理委员会成员有哪些？",
    "农学院负责本科教学、研究生工作和实验室安全的领导分别是谁？",
]


@pytest.mark.parametrize("question", NEGATIVE_QUESTIONS)
def test_non_enumeration_questions_do_not_fabricate_lab_names(question):
    # The extractor does return pseudo labs here; only the intent gate can stop them.
    assert items_of_type(SAFETY_TEXT, "实验室")
    assert asked_entity_type(question) == ""
    assert build_list_claim(question, [_evidence(SAFETY_TEXT)]) is None


PSEUDOLIST_QUESTIONS = [
    "哪些领导负责实验室安全？",
    "实验室安全有哪些规定？",
    "实验室安全管理的流程是什么？",
    "实验室管理委员会成员有哪些？",
]


@pytest.mark.parametrize("question", PSEUDOLIST_QUESTIONS)
def test_list_requirements_do_not_turn_mentions_into_pseudo_lists(question):
    evidence = [_evidence(SAFETY_TEXT, "实验室安全管理办法", "safety#第一条")]
    assert GraphVerifier._list_requirements(question, [], evidence) == []


def test_list_requirements_keep_a_genuine_centre_list():
    question = "学院有哪些实验教学示范中心？"
    requirements = GraphVerifier._list_requirements(question, [], [_evidence(CENTRE_TEXT)])
    assert len(requirements) == 1
    requirement = requirements[0]
    assert requirement["kind"] == "category"
    assert requirement["type_word"] == "实验教学示范中心"
    assert requirement["required_items"] == items_of_type(CENTRE_TEXT, "实验教学示范中心")


@pytest.mark.parametrize(
    ("question", "text", "type_word"),
    [
        ("学院有哪些实验教学示范中心？", CENTRE_TEXT, "实验教学示范中心"),
        ("学院的重点实验室有哪些？", LAB_TEXT, "重点实验室"),
        ("有哪些省部级重点实验室？", LAB_TEXT, "重点实验室"),
        ("实验室名称有哪些？", PLAIN_LAB_TEXT, "实验室"),
        ("学院有哪些研究中心？", RESEARCH_TEXT, "研究中心"),
    ],
)
def test_genuine_list_questions_keep_literal_cited_names(question, text, type_word):
    assert asked_entity_type(question) == type_word
    draft = build_list_claim(question, [_evidence(text)])
    assert draft is not None
    names = items_of_type(text, type_word)
    assert names
    for name in names:
        assert name in draft.text
    quote = draft.citations[0].quote
    assert quote in text
    assert all(name in quote for name in names)


def test_spacing_and_unrelated_institute_do_not_hide_list_intent():
    draft = build_list_claim("动物科学学院  有哪些  省部级重点实验室 ？", [_evidence(LAB_TEXT)])
    assert draft is not None
    assert "广东省农业生态重点实验室" in draft.text


def test_mixed_leadership_and_real_list_keeps_only_the_listed_type():
    question = "农学院负责实验室安全的领导是谁，学院有哪些重点实验室？"
    evidence = [
        _evidence(SAFETY_TEXT, "实验室安全管理办法", "safety#第一条"),
        _evidence(LAB_TEXT, "动物科学学院科研平台", "labs#第一条"),
    ]
    draft = build_list_claim(question, evidence)
    assert draft is not None
    assert "广东省农业生态重点实验室" in draft.text
    assert "安全责任人" not in draft.text
