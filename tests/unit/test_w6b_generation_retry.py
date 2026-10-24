"""P1：JSON 空返回重试与空 claims 重试。"""

from __future__ import annotations

import json

from campusguide.agents.advisor import Advisor
from campusguide.agents.providers import ChatProviderError, FakeChatProvider
from tests.w2_helpers import make_article, make_evidence

TEXT = "申请奖学金应当学习成绩优异，品行端正。"
ARTICLE_ID = "demo-policy@v2021#第一条"
GOOD_CLAIMS = json.dumps(
    {
        "claims": [
            {
                "claim_id": "c1",
                "text": "申请奖学金应当学习成绩优异。",
                "kind": "eligibility",
                "citations": [
                    {
                        "article_id": ARTICLE_ID,
                        "quote": "申请奖学金应当学习成绩优异",
                    }
                ],
            }
        ]
    },
    ensure_ascii=False,
)


def _evidence():
    return [make_evidence(make_article(article_id=ARTICLE_ID, text=TEXT))]


def test_multi_question_first_call_uses_larger_budget() -> None:
    provider = FakeChatProvider([GOOD_CLAIMS])
    Advisor(provider, max_tokens=3000).generate("条件是什么？程序是什么？", _evidence())
    assert provider.options[0]["max_tokens"] == 6000


def test_single_question_keeps_configured_budget() -> None:
    provider = FakeChatProvider([GOOD_CLAIMS])
    Advisor(provider, max_tokens=3000).generate("奖学金条件是什么？", _evidence())
    assert provider.options[0]["max_tokens"] == 3000


def test_first_provider_error_is_retried_once() -> None:
    provider = FakeChatProvider([ChatProviderError("endpoint down"), GOOD_CLAIMS])
    result = Advisor(provider, max_tokens=3000).generate("奖学金条件", _evidence())
    assert result.provider_retried is True
    assert len(provider.calls) == 2
    assert len(result.drafts) == 1


def test_empty_json_repair_is_retried_once() -> None:
    provider = FakeChatProvider(["not json", "", GOOD_CLAIMS])
    result = Advisor(provider).generate("奖学金条件", _evidence())
    assert result.json_empty_retried is True
    assert len(result.drafts) == 1
    assert len(provider.calls) == 3
    # 第三次是“从证据重新生成”的空上下文重试，不再带上一次坏输出。
    assert all(message["role"] != "assistant" for message in provider.calls[2])


def test_first_truncated_output_repairs_with_larger_budget() -> None:
    provider = FakeChatProvider(
        [
            '{"claims": [{"claim_id": "c1", "text": "被截断的答案',
            GOOD_CLAIMS,
        ]
    )
    result = Advisor(provider, max_tokens=3000).generate("奖学金条件", _evidence())
    assert result.json_truncation_retried is True
    assert len(provider.calls) == 2
    assert provider.options[1]["max_tokens"] == 6000
    assert len(result.drafts) == 1


def test_truncated_json_is_retried_with_larger_budget() -> None:
    provider = FakeChatProvider(
        [
            "not json",
            '{"claims": [{"claim_id": "c1", "text": "被截断的答案',
            GOOD_CLAIMS,
        ]
    )
    result = Advisor(provider, max_tokens=3000).generate("奖学金条件", _evidence())
    assert result.json_truncation_retried is True
    assert len(result.drafts) == 1
    assert len(provider.calls) == 3
    assert provider.options[2]["max_tokens"] == 6000
    assert all(message["role"] != "assistant" for message in provider.calls[2])


def test_non_empty_invalid_json_is_not_retried_twice() -> None:
    provider = FakeChatProvider(["not json", "still not json"])
    try:
        Advisor(provider).generate("奖学金条件", _evidence())
    except Exception as exc:
        assert "JSON 修复失败" in str(exc)
    else:
        raise AssertionError("应当抛出 JSON 修复失败")
    assert len(provider.calls) == 2


def test_empty_claims_retry_can_recover() -> None:
    provider = FakeChatProvider(['{"claims": []}', GOOD_CLAIMS])
    result = Advisor(provider).generate("奖学金条件", _evidence())
    assert result.empty_claims_retried is True
    assert len(result.drafts) == 1
    assert len(provider.calls) == 2


def test_empty_claims_retry_stays_empty() -> None:
    provider = FakeChatProvider(['{"claims": []}', '{"claims": []}'])
    result = Advisor(provider).generate("奖学金条件", _evidence())
    assert result.empty_claims_retried is True
    assert result.drafts == []
    assert len(provider.calls) == 2


def test_empty_claims_retry_provider_error_keeps_empty() -> None:
    provider = FakeChatProvider(['{"claims": []}', ChatProviderError("endpoint down")])
    result = Advisor(provider).generate("奖学金条件", _evidence())
    assert result.empty_claims_retried is True
    assert result.drafts == []
    assert result.empty_claims_error
