"""W6-A2：quote 连续性与范围/数字覆盖的契约测试。"""

from __future__ import annotations

import json

from campusguide.agents.advisor import (
    PROMPT_VERSION,
    SYSTEM_PROMPT,
    Advisor,
    ClaimDraft,
    build_messages,
    parse_claims_json,
)
from campusguide.agents.providers import FakeChatProvider
from campusguide.agents.quote_repair import find_quote_problems
from campusguide.domain.models import Evidence
from campusguide.verification.claims import ClaimValidator
from tests.w2_helpers import make_article, make_evidence

ARTICLE_ID = "disciplinary-rules@v2017#第八条"
DISCIPLINE_TEXT = (
    "学生有下列情形之一，给予开除学籍处分：\n"
    "（一）违反宪法，反对四项基本原则的；\n"
    "（二）违反国家法律，情节严重的；\n"
    "（三）考试作弊的。"
)
ATTENDANCE_TEXT = "一学期内累计旷课30-39学时的，给予严重警告处分。"


def _evidence(text: str = DISCIPLINE_TEXT) -> list[Evidence]:
    return [make_evidence(make_article(article_id=ARTICLE_ID, text=text))]


def _claims(*claims: tuple[str, str, str]) -> str:
    payload = {
        "claims": [
            {
                "claim_id": claim_id,
                "text": text,
                "kind": "other",
                "citations": [{"article_id": ARTICLE_ID, "quote": quote}],
            }
            for claim_id, text, quote in claims
        ]
    }
    return json.dumps(payload, ensure_ascii=False)


def _draft(claim_id: str, text: str, quote: str) -> ClaimDraft:
    return ClaimDraft.model_validate(
        {
            "claim_id": claim_id,
            "text": text,
            "kind": "other",
            "citations": [{"article_id": ARTICLE_ID, "quote": quote}],
        }
    )


def test_detects_chapeau_plus_non_adjacent_item() -> None:
    quote = "学生有下列情形之一，给予开除学籍处分：（二）违反国家法律，情节严重的；"
    problem = find_quote_problems(
        [_draft("c1", "违反国家法律，情节严重的，给予开除学籍处分。", quote)], _evidence()
    )
    assert problem
    assert any("连续子串" in reason for reason in problem[0].reasons)


def test_detects_number_not_in_quote() -> None:
    quote = "给予严重警告处分。"
    problem = find_quote_problems(
        [_draft("c1", "一学期内累计旷课30-39学时的，给予严重警告处分。", quote)],
        _evidence(ATTENDANCE_TEXT),
    )
    assert problem
    assert any("数字/期限" in reason for reason in problem[0].reasons)


def test_detects_scope_missing_but_number_present() -> None:
    quote = "累计旷课30-39学时的，给予严重警告处分。"
    problem = find_quote_problems(
        [_draft("c1", "一学期内累计旷课30-39学时的，给予严重警告处分。", quote)],
        _evidence(ATTENDANCE_TEXT),
    )
    assert problem
    assert any("1学期" in reason for reason in problem[0].reasons)


def test_repair_uses_contiguous_full_quote_and_verifier_accepts() -> None:
    bad_quote = "学生有下列情形之一，给予开除学籍处分：（二）违反国家法律，情节严重的；"
    good_quote = (
        "学生有下列情形之一，给予开除学籍处分：\n"
        "（一）违反宪法，反对四项基本原则的；\n"
        "（二）违反国家法律，情节严重的；"
    )
    provider = FakeChatProvider(
        [
            _claims(("c1", "违反国家法律，情节严重的，给予开除学籍处分。", bad_quote)),
            _claims(("c1", "违反国家法律，情节严重的，给予开除学籍处分。", good_quote)),
        ]
    )
    result = Advisor(provider).generate("哪些情形会被开除学籍？", _evidence())

    assert result.quote_repair_attempted is True
    assert result.quote_repaired is True
    assert result.quote_repair_remaining == []
    assert len(provider.calls) == 2
    assert provider.calls[1][-1]["content"].find("连续子串") >= 0

    check = ClaimValidator().check(result.drafts, _evidence())
    assert len(check.claims) == 1
    assert check.claims[0].citations[0].quote == good_quote


def test_repair_restores_missing_scoped_number() -> None:
    bad_quote = "累计旷课30-39学时的，给予严重警告处分。"
    good_quote = ATTENDANCE_TEXT
    provider = FakeChatProvider(
        [
            _claims(("c1", "一学期内累计旷课30-39学时的，给予严重警告处分。", bad_quote)),
            _claims(("c1", "一学期内累计旷课30-39学时的，给予严重警告处分。", good_quote)),
        ]
    )
    result = Advisor(provider).generate("旷课多少学时会被处分？", _evidence(ATTENDANCE_TEXT))
    assert result.quote_repaired is True
    assert result.quote_repair_remaining == []
    check = ClaimValidator().check(result.drafts, _evidence(ATTENDANCE_TEXT))
    assert len(check.claims) == 1


def test_failed_repair_is_dropped_by_verifier_without_third_call() -> None:
    bad_quote = "学生有下列情形之一，给予开除学籍处分：（二）违反国家法律，情节严重的；"
    provider = FakeChatProvider(
        [
            _claims(("c1", "违反国家法律，情节严重的，给予开除学籍处分。", bad_quote)),
            _claims(("c1", "违反国家法律，情节严重的，给予开除学籍处分。", bad_quote)),
        ]
    )
    result = Advisor(provider).generate("哪些情形会被开除学籍？", _evidence())
    assert len(provider.calls) == 2
    assert result.quote_repair_remaining == ["c1"]
    check = ClaimValidator().check(result.drafts, _evidence())
    assert check.claims == []
    assert "c1" in check.dropped_claim_ids


def test_repair_can_delete_unrepairable_claim() -> None:
    bad_quote = "学生有下列情形之一，给予开除学籍处分：（二）违反国家法律，情节严重的；"
    provider = FakeChatProvider(
        [
            _claims(("c1", "违反国家法律，情节严重的，给予开除学籍处分。", bad_quote)),
            '{"claims": []}',
        ]
    )
    result = Advisor(provider).generate("哪些情形会被开除学籍？", _evidence())
    assert result.quote_repaired is True
    assert result.drafts == []


def test_invalid_repair_json_keeps_original_draft_for_verifier() -> None:
    bad_quote = "学生有下列情形之一，给予开除学籍处分：（二）违反国家法律，情节严重的；"
    provider = FakeChatProvider(
        [
            _claims(("c1", "违反国家法律，情节严重的，给予开除学籍处分。", bad_quote)),
            "not json",
        ]
    )
    result = Advisor(provider).generate("哪些情形会被开除学籍？", _evidence())
    assert result.quote_repair_error
    assert len(result.drafts) == 1
    check = ClaimValidator().check(result.drafts, _evidence())
    assert check.claims == []


def test_json_repair_then_quote_repair_order() -> None:
    bad_quote = "学生有下列情形之一，给予开除学籍处分：（二）违反国家法律，情节严重的；"
    good_quote = DISCIPLINE_TEXT[0:60]
    provider = FakeChatProvider(
        [
            "not json",
            _claims(("c1", "违反国家法律，情节严重的，给予开除学籍处分。", bad_quote)),
            _claims(("c1", "违反国家法律，情节严重的，给予开除学籍处分。", good_quote)),
        ]
    )
    result = Advisor(provider).generate("哪些情形会被开除学籍？", _evidence())
    assert result.repaired is True
    assert result.quote_repaired is True
    assert len(provider.calls) == 3


def test_valid_quote_does_not_trigger_repair() -> None:
    provider = FakeChatProvider(
        [_claims(("c1", "违反国家法律，情节严重的，给予开除学籍处分。", DISCIPLINE_TEXT))]
    )
    result = Advisor(provider).generate("哪些情形会被开除学籍？", _evidence())
    assert result.quote_repair_attempted is False
    assert len(provider.calls) == 1


def test_prompt_version_and_contract_phrases() -> None:
    # w6a2.1 保留 w6a2 契约短语，另加多问/列举覆盖约束。
    assert PROMPT_VERSION == "w6a2.1-2026-10-24"
    for phrase in ("连续子串", "跳过中间", "一学期内", "一个条件、一个后果或一个数量"):
        assert phrase in SYSTEM_PROMPT
    messages = build_messages("问题", _evidence(), None)
    assert "连续子串" in messages[1]["content"]
    assert "非相邻分项" in messages[1]["content"]


def test_repair_messages_include_problem_claim_and_reason() -> None:
    from campusguide.agents.quote_repair import build_repair_messages

    drafts = parse_claims_json(
        _claims(
            (
                "c1",
                "违反国家法律，情节严重的，给予开除学籍处分。",
                "学生有下列情形之一，给予开除学籍处分：（二）违反国家法律，情节严重的；",
            )
        )
    ).claims
    problems = find_quote_problems(drafts, _evidence())
    messages = build_repair_messages(
        [{"role": "system", "content": "s"}, {"role": "user", "content": "u"}],
        "{}",
        drafts,
        problems,
    )
    content = messages[-1]["content"]
    assert "c1" in content
    assert "连续子串" in content
    assert "跳过中间" in content
