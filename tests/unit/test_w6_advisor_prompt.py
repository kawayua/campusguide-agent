"""W6-A：claim 不得超出 quote 的提示词契约。"""

from __future__ import annotations

from campusguide.agents.advisor import PROMPT_VERSION, SYSTEM_PROMPT, build_messages
from campusguide.domain.enums import BlockType
from tests.w2_helpers import make_article, make_evidence


def test_w6a_prompt_requires_claims_to_stay_inside_quote() -> None:
    assert PROMPT_VERSION.startswith("w6a")
    for phrase in ("条件 + 后果", "不得补充", "限定词", "逐条自检", "不要展开"):
        assert phrase in SYSTEM_PROMPT

    messages = build_messages("问题", [make_evidence(make_article())], None)
    assert "claim 不得超出 quote" in messages[1]["content"]


def test_multi_question_prompt_lists_subquestions_and_enumeration() -> None:
    question = (
        "本科毕业生在什么条件下可被授予学士学位？"
        "学位申请人在攻读学位过程中出现哪些情形时学校不授予或撤销学位？"
    )
    messages = build_messages(question, [make_evidence(make_article())], None)
    content = messages[1]["content"]
    assert "本题包含 2 个子问题" in content
    assert "每个子问题至少要有 1 条 claim" in content
    assert "不得只复述引导句" in content


def test_single_question_prompt_has_no_subquestion_block() -> None:
    messages = build_messages("奖学金申请条件是什么？", [make_evidence(make_article())], None)
    content = messages[1]["content"]
    assert "本题包含" not in content
    assert "不得只复述引导句" not in content


def test_job_intent_adds_multi_aspect_section_instruction() -> None:
    section = make_article(
        article_id="plan@v2026#一、人才培养目标",
        article_no="一、人才培养目标",
        block_type=BlockType.SECTION,
        text="博士生在理论研究或技术研究中取得创新性成果。可在高等院校从事教学工作。",
    )
    messages = build_messages(
        "2026 版培养方案：博士生在研究成果和毕业后可从事的工作方面有何规定？",
        [make_evidence(section)],
        None,
    )
    assert "优先整体引用该 section" in messages[1]["content"]
