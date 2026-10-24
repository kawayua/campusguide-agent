"""W6-A：claim 不得超出 quote 的提示词契约。"""

from __future__ import annotations

from campusguide.agents.advisor import PROMPT_VERSION, SYSTEM_PROMPT, build_messages
from tests.w2_helpers import make_article, make_evidence


def test_w6a_prompt_requires_claims_to_stay_inside_quote() -> None:
    assert PROMPT_VERSION.startswith("w6a")
    for phrase in ("条件 + 后果", "不得补充", "限定词", "逐条自检", "不要展开"):
        assert phrase in SYSTEM_PROMPT

    messages = build_messages("问题", [make_evidence(make_article())], None)
    assert "claim 不得超出 quote" in messages[1]["content"]
