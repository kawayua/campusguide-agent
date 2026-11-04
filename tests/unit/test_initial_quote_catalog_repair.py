"""初始生成：prompt 窗口ID目录与 parser 注册表身份必须一致。"""

from __future__ import annotations

import json

import pytest

from campusguide.agents.advisor import Advisor, AdvisorError
from campusguide.agents.providers import FakeChatProvider
from campusguide.domain.enums import BlockType
from tests.w2_helpers import make_article, make_evidence

GOAL_ID = "plan@v2026#一、人才培养目标"
DISTRACTOR_ID = "plan@v2026#五、研究生科研成果要求"
GOAL_TEXT = "博士生应取得被同行认可的创新性成果。"
DISTRACTOR_TEXT = "博士生需发表至少两篇论文。"
GOAL_QUESTION = "2026 版培养方案：博士生在研究成果和毕业后工作方面有何规定？"


def _claims(quote_id: str, text: str, *, claim_id: str = "c1") -> str:
    return json.dumps(
        {
            "claims": [
                {
                    "claim_id": claim_id,
                    "text": text,
                    "kind": "other",
                    "citations": [{"quote_id": quote_id}],
                }
            ]
        },
        ensure_ascii=False,
    )


def test_filtered_evidence_window_id_keeps_displayed_source_identity() -> None:
    # generation evidence 只保留第二条（培养目标）；prompt 重新编号为 source_1_*，
    # parser 必须使用同一 scope，不能把 source_1_body 解析回原列表第一条。
    evidence = [
        make_evidence(
            make_article(
                article_id=DISTRACTOR_ID,
                article_no="五、研究生科研成果要求",
                part_no=1,
                block_type=BlockType.SECTION,
                text=DISTRACTOR_TEXT,
            )
        ),
        make_evidence(
            make_article(
                article_id=GOAL_ID,
                article_no="一、人才培养目标",
                part_no=2,
                block_type=BlockType.SECTION,
                text=GOAL_TEXT,
            )
        ),
    ]
    provider = FakeChatProvider([_claims("source_1_body", GOAL_TEXT)])

    result = Advisor(provider, enable_quote_repair=False).generate(
        GOAL_QUESTION, evidence
    )

    prompt = provider.calls[0][1]["content"]
    assert GOAL_ID in prompt
    assert DISTRACTOR_ID not in prompt
    assert "source_1_body" in prompt
    assert "[证据2]" not in prompt

    citation = result.drafts[0].citations[0]
    assert citation.quote_id == "source_1_body"
    assert citation.article_id == GOAL_ID
    assert citation.trusted_part_no == 2
    assert (citation.trusted_start, citation.trusted_end) == (0, len(GOAL_TEXT))
    assert citation.quote == GOAL_TEXT


def test_unknown_window_id_repair_prompt_lists_actual_catalog_then_succeeds() -> None:
    first_text = "申请奖学金应当学习成绩优异。"
    second_text = "奖学金金额为8000元。"
    evidence = [
        make_evidence(
            make_article(
                article_id="policy-a@v1#第一条",
                article_no="第一条",
                part_no=1,
                text=first_text,
            )
        ),
        make_evidence(
            make_article(
                article_id="policy-b@v1#第二条",
                article_no="第二条",
                part_no=1,
                text=second_text,
            )
        ),
    ]
    provider = FakeChatProvider(
        [
            _claims("source_9_body", second_text),
            _claims("source_2_body", second_text),
        ]
    )

    result = Advisor(provider, enable_quote_repair=False).generate(
        "奖学金金额是多少？", evidence
    )

    assert result.repaired is True
    assert len(provider.calls) == 2
    repair_prompt = provider.calls[1][-1]["content"]
    assert "可用窗口ID" in repair_prompt
    assert "source_1_body（" in repair_prompt
    assert "source_2_body（" in repair_prompt
    assert "policy-a@v1#第一条" in repair_prompt
    assert "policy-b@v1#第二条" in repair_prompt
    assert "source_9_body（" not in repair_prompt

    citation = result.drafts[0].citations[0]
    assert citation.quote_id == "source_2_body"
    assert citation.article_id == "policy-b@v1#第二条"
    assert citation.trusted_part_no == 1
    assert (citation.trusted_start, citation.trusted_end) == (0, len(second_text))
    assert citation.quote == second_text


def test_repeated_unknown_window_ids_fail_within_bounded_calls() -> None:
    text = "申请奖学金应当学习成绩优异。"
    evidence = [
        make_evidence(
            make_article(article_id="policy-a@v1#第一条", article_no="第一条", text=text)
        )
    ]
    provider = FakeChatProvider(
        [
            _claims("source_9_body", text),
            _claims("source_8_body", text),
        ]
    )

    with pytest.raises(AdvisorError) as exc:
        Advisor(provider, enable_quote_repair=False).generate("奖学金条件", evidence)

    assert "JSON 修复失败" in str(exc.value)
    assert "source_8_body" in str(exc.value)
    assert len(provider.calls) == 2
