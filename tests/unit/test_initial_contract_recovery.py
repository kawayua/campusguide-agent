"""初始生成引用契约失败后的有界恢复测试。"""

from __future__ import annotations

import json

import pytest

from campusguide.agents.advisor import (
    Advisor,
    AdvisorError,
    ClaimDraft,
    merge_revised_drafts,
)
from campusguide.agents.providers import FakeChatProvider
from tests.w2_helpers import make_article, make_evidence

QUESTION = "申请奖学金需要什么条件？"
ARTICLE_ID = "demo-policy@v2021#第一条"
TEXT = "申请奖学金应当学习成绩优异，品行端正。"
BAD_QUOTE = "这是被篡改的引用"


def _evidence():
    return [make_evidence(make_article(article_id=ARTICLE_ID, text=TEXT))]


def _claims(citation: dict[str, str]) -> str:
    return json.dumps(
        {
            "claims": [
                {
                    "claim_id": "c1",
                    "text": "申请奖学金应当学习成绩优异。",
                    "kind": "eligibility",
                    "citations": [citation],
                }
            ]
        },
        ensure_ascii=False,
    )


def _id_only_claims(quote_id: str = "source_1_body") -> str:
    return _claims({"quote_id": quote_id})


def _user_texts(messages: list[dict[str, str]]) -> list[str]:
    return [message["content"] for message in messages if message["role"] == "user"]


def _catalog_section(prompt: str) -> str:
    assert "可用窗口ID" in prompt
    return prompt.split("可用窗口ID", 1)[1]


def test_conflicting_quote_retries_clean_and_resolves_server_quote() -> None:
    provider = FakeChatProvider(
        [
            _claims({"quote_id": "source_1_body", "quote": BAD_QUOTE}),
            _id_only_claims(),
        ]
    )
    result = Advisor(provider).generate(QUESTION, _evidence())

    assert len(provider.calls) == 2
    retry_messages = provider.calls[1]
    assert all(message["role"] != "assistant" for message in retry_messages)
    prompts = _user_texts(retry_messages)
    assert any(QUESTION in text for text in prompts)
    assert any(TEXT in text for text in prompts)
    prompt = prompts[-1]
    assert "quote与登记窗口不一致" in prompt
    assert '{"quote_id":"<本轮证据中已存在的窗口ID>"}' in prompt
    assert "不得再输出 article_id、quote 或复制窗口正文等其他字段" in prompt
    assert "本轮证据窗口清单保持不变（与首次生成相同）" in prompt
    assert "source_1_body" in _catalog_section(prompt)
    assert all(BAD_QUOTE not in message["content"] for message in retry_messages)

    citation = result.drafts[0].citations[0]
    assert citation.article_id == ARTICLE_ID
    assert citation.quote == TEXT
    assert citation.quote_id == "source_1_body"
    assert citation.trusted_part_no == 1
    assert citation.trusted_start == 0
    assert citation.trusted_end == len(TEXT)
    assert result.quote_repair_attempted is False


def test_unknown_id_retry_offers_only_known_catalog_ids() -> None:
    provider = FakeChatProvider(
        [
            _claims({"quote_id": "source_99_body"}),
            _id_only_claims(),
        ]
    )
    result = Advisor(provider).generate(QUESTION, _evidence())

    assert len(provider.calls) == 2
    retry_messages = provider.calls[1]
    assert all(message["role"] != "assistant" for message in retry_messages)
    prompt = _user_texts(retry_messages)[-1]
    assert "引用窗口ID不在本轮证据中：source_99_body" in prompt
    catalog = _catalog_section(prompt)
    assert "source_1_body" in catalog
    assert "source_99_body" not in catalog
    assert result.drafts[0].citations[0].quote == TEXT


def test_second_conflicting_response_fails_closed_without_third_call() -> None:
    provider = FakeChatProvider(
        [
            _claims({"quote_id": "source_1_body", "quote": BAD_QUOTE}),
            _claims({"quote_id": "source_1_body", "quote": "另一个不匹配的正文"}),
        ]
    )
    with pytest.raises(AdvisorError) as exc_info:
        Advisor(provider).generate(QUESTION, _evidence())

    assert "JSON 修复失败" in str(exc_info.value)
    assert "quote与登记窗口不一致" in str(exc_info.value)
    assert len(provider.calls) == 2


def test_empty_second_response_gets_clean_id_only_third_retry() -> None:
    provider = FakeChatProvider(
        [
            _claims({"quote_id": "source_1_body", "quote": BAD_QUOTE}),
            "",
            _id_only_claims(),
        ]
    )
    result = Advisor(provider).generate(QUESTION, _evidence())

    assert len(provider.calls) == 3
    retry_messages = provider.calls[2]
    assert all(message["role"] != "assistant" for message in retry_messages)
    prompt = _user_texts(retry_messages)[-1]
    assert '{"quote_id":"<本轮证据中已存在的窗口ID>"}' in prompt
    assert "source_1_body" in _catalog_section(prompt)
    assert result.json_empty_retried is True
    assert result.json_truncation_retried is False
    assert result.drafts[0].citations[0].quote == TEXT


def test_revision_contract_failure_keeps_non_failed_claims_protected() -> None:
    previous = [
        ClaimDraft.model_validate(
            {
                "claim_id": "c1",
                "text": "申请奖学金应当学习成绩优异。",
                "kind": "eligibility",
                "citations": [{"article_id": ARTICLE_ID, "quote": TEXT}],
            }
        ),
        ClaimDraft.model_validate(
            {
                "claim_id": "c2",
                "text": "申请奖学金应当品行端正。",
                "kind": "eligibility",
                "citations": [{"article_id": ARTICLE_ID, "quote": TEXT}],
            }
        ),
    ]
    provider = FakeChatProvider(
        [
            _claims({"quote_id": "source_1_body", "quote": BAD_QUOTE}).replace(
                '"claim_id": "c1"', '"claim_id": "c2"'
            ),
            _id_only_claims(),
        ]
    )
    result = Advisor(provider).revise(
        QUESTION,
        _evidence(),
        failed_claim_ids=["c2"],
        issues=[{"reason": "unsupported"}],
        previous_claims=previous,
    )

    assert len(provider.calls) == 2
    assert result.drafts == []
    assert "严格修订契约失败" in result.revision_error
    merged = merge_revised_drafts(previous, result.drafts, ["c2"])
    assert [draft.claim_id for draft in merged] == ["c1", "c2"]
    assert merged[0] is previous[0]
    assert merged[1] is previous[1]


def test_legacy_raw_quote_initial_generation_still_parses() -> None:
    provider = FakeChatProvider(
        [_claims({"article_id": ARTICLE_ID, "quote": TEXT})]
    )
    result = Advisor(provider).generate(QUESTION, _evidence())

    assert len(provider.calls) == 1
    citation = result.drafts[0].citations[0]
    assert citation.article_id == ARTICLE_ID
    assert citation.quote == TEXT
    assert citation.quote_id == ""
    assert result.quote_repair_attempted is False
