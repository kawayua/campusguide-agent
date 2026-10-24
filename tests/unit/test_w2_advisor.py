from __future__ import annotations

import pytest

from campusguide.agents.advisor import Advisor, AdvisorError, parse_claims_json
from campusguide.agents.providers import FakeChatProvider
from tests.w2_helpers import make_article, make_evidence


def test_parse_fenced_json() -> None:
    content = """```json
{"claims": [{"claim_id": "c1", "text": "t", "kind": "eligibility",
"citations": [{"article_id": "a", "quote": "q"}]}]}
```"""
    draft = parse_claims_json(content)
    assert draft.claims[0].claim_id == "c1"
    assert draft.claims[0].citations[0].quote == "q"


def test_parse_rejects_missing_claims() -> None:
    with pytest.raises(AdvisorError):
        parse_claims_json('{"answer": "no claims"}')


def test_advisor_repairs_invalid_json_once() -> None:
    provider = FakeChatProvider(
        [
            "not json",
            '{"claims": [{"claim_id": "c1", "text": "t", "kind": "other", '
            '"citations": [{"article_id": "a", "quote": "q"}]}]}',
        ]
    )
    advisor = Advisor(provider, enable_quote_repair=False)
    result = advisor.generate("问题", [make_evidence(make_article())])

    assert result.repaired is True
    assert len(result.drafts) == 1
    assert len(provider.calls) == 2
    assert "无法解析" in provider.calls[1][-1]["content"]


def test_advisor_fails_after_second_invalid_json() -> None:
    provider = FakeChatProvider(["not json", "still not json"])
    advisor = Advisor(provider)
    with pytest.raises(AdvisorError):
        advisor.generate("问题", [make_evidence(make_article())])
