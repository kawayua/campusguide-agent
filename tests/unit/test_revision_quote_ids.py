"""W6-B：revise / quote repair 的 quote-ID-only 严格契约。

严格模式只接受 ``citation {quote_id}``，由服务器按登记窗口取回原文；
通用生成解析保持 raw quote 兼容，但引用窗口ID仍必须与本轮证据一致。
"""

from __future__ import annotations

import json
from datetime import date

import pytest

from campusguide.agents.advisor import (
    Advisor,
    AdvisorError,
    ClaimDraft,
    build_revision_prompt,
    merge_revised_drafts,
    parse_claims_json,
)
from campusguide.agents.graph import GraphNodes
from campusguide.agents.providers import FakeChatProvider
from campusguide.agents.quote_repair import SpanRegistry, is_scoped_span_id
from campusguide.agents.verifier import GraphVerifier
from campusguide.domain.enums import GraphDecision
from campusguide.domain.models import Citation, Claim
from campusguide.verification.claims import ClaimValidator
from tests.w2_helpers import make_article, make_evidence

ARTICLE_ID = "demo-policy@v2021#第一条"
TEXT = "申请奖学金应当学习成绩优异，品行端正。"
CLAIM_TEXT = "申请奖学金应当学习成绩优异。"
SCOPED_QUOTE = "学习成绩优异"


def _evidence(text: str = TEXT, **kwargs):
    return [make_evidence(make_article(article_id=ARTICLE_ID, text=text, **kwargs))]


def _claim_payload(
    citations: list[dict],
    *,
    claim_id: str = "c1",
    text: str = CLAIM_TEXT,
) -> dict:
    return {
        "claims": [
            {
                "claim_id": claim_id,
                "text": text,
                "kind": "eligibility",
                "citations": citations,
            }
        ]
    }


def _json(payload: dict) -> str:
    return json.dumps(payload, ensure_ascii=False)


def _raw_citation(quote: str, article_id: str = ARTICLE_ID) -> dict:
    return {"article_id": article_id, "quote": quote}


def _id_citation(quote_id: str) -> dict:
    return {"quote_id": quote_id}


def _strict_parse(content: str, evidence, *, allowed: set[str] | None = None):
    return parse_claims_json(
        content,
        evidence,
        strict_quote_ids=True,
        allowed_claim_ids={"c1"} if allowed is None else allowed,
    )


def _previous(claim_id: str, citation: dict, *, text: str = CLAIM_TEXT) -> ClaimDraft:
    return ClaimDraft.model_validate(
        _claim_payload([citation], claim_id=claim_id, text=text)["claims"][0]
    )


def test_strict_parse_hydrates_registered_source_window() -> None:
    evidence = _evidence()
    draft = _strict_parse(
        _json(_claim_payload([_id_citation("source_1_body")])), evidence
    ).claims[0]

    assert draft.citations[0].quote_id == "source_1_body"
    assert draft.citations[0].article_id == ARTICLE_ID
    assert draft.citations[0].quote == TEXT


@pytest.mark.parametrize(
    ("citation", "message"),
    [
        (_raw_citation(SCOPED_QUOTE), "只允许 citation.quote_id"),
        ({"quote_id": "source_1_body", "quote": TEXT}, "只允许 citation.quote_id"),
        (
            {"quote_id": "source_1_body", "article_id": ARTICLE_ID},
            "只允许 citation.quote_id",
        ),
        ({"quote": SCOPED_QUOTE}, "只允许 citation.quote_id"),
        (_id_citation("source_9_body"), "不在本轮证据"),
        (_id_citation(""), "要求 citation.quote_id"),
    ],
)
def test_strict_parse_rejects_raw_dual_unknown_or_empty_ids(
    citation: dict, message: str
) -> None:
    with pytest.raises(AdvisorError) as exc:
        _strict_parse(_json(_claim_payload([citation])), _evidence())
    assert message in str(exc.value)


def test_strict_parse_rejects_duplicate_quote_id_within_claim() -> None:
    payload = _claim_payload(
        [_id_citation("source_1_body"), _id_citation("source_1_body")]
    )
    with pytest.raises(AdvisorError) as exc:
        _strict_parse(_json(payload), _evidence())
    assert "重复引用窗口ID" in str(exc.value)


def test_strict_parse_limits_claims_to_failed_ids() -> None:
    unknown = _claim_payload([_id_citation("source_1_body")], claim_id="c2")
    with pytest.raises(AdvisorError) as exc:
        _strict_parse(_json(unknown), _evidence())
    assert "未失败的 claim_id" in str(exc.value)

    missing = _claim_payload([_id_citation("source_1_body")])
    del missing["claims"][0]["claim_id"]
    with pytest.raises(AdvisorError) as exc:
        _strict_parse(_json(missing), _evidence())
    assert "要求每个 claim 提供 claim_id" in str(exc.value)

    duplicate = {
        "claims": [
            _claim_payload([_id_citation("source_1_body")])["claims"][0],
            _claim_payload([_id_citation("source_1_body")])["claims"][0],
        ]
    }
    with pytest.raises(AdvisorError) as exc:
        _strict_parse(_json(duplicate), _evidence())
    assert "重复 claim_id" in str(exc.value)

    no_citation = _claim_payload([], claim_id="c1")
    with pytest.raises(AdvisorError) as exc:
        _strict_parse(_json(no_citation), _evidence())
    assert "缺少 citation.quote_id" in str(exc.value)


def test_revise_prompt_registers_scoped_span_from_source_slice() -> None:
    evidence = _evidence()
    previous = _previous("c1", _raw_citation(SCOPED_QUOTE))
    prompt = build_revision_prompt(
        "申请奖学金需要什么条件？",
        evidence,
        failed_claim_ids=["c1"],
        issues=[],
        previous_claims=[previous],
        audience=None,
    )

    scoped = [span_id for span_id in prompt.registry.spans if is_scoped_span_id(span_id)]
    assert scoped == ["source_1_prev_1"]
    span = prompt.registry.spans[scoped[0]]
    start = TEXT.index(SCOPED_QUOTE)
    assert span.start == start
    assert span.end == start + len(SCOPED_QUOTE)
    assert span.quote == TEXT[span.start : span.end]
    assert span.article_id == ARTICLE_ID
    assert span.part_no == 1

    user = prompt.messages[-1]["content"]
    assert "source_1_prev_1" in user
    assert "part_no=1" in user
    assert f"原文偏移={span.start}-{span.end}" in user
    assert json.dumps(span.quote, ensure_ascii=False) in user


def test_revise_prompt_uses_existing_window_for_exact_previous_quote() -> None:
    evidence = _evidence()
    previous = _previous("c1", _raw_citation(TEXT))
    prompt = build_revision_prompt(
        "申请奖学金需要什么条件？",
        evidence,
        failed_claim_ids=["c1"],
        issues=[],
        previous_claims=[previous],
        audience=None,
    )

    assert not any(is_scoped_span_id(span_id) for span_id in prompt.registry.spans)
    assert '"quote_ids": ["source_1_body"]' in prompt.messages[-1]["content"]


def test_revise_prompt_marks_unmappable_old_quote() -> None:
    evidence = _evidence()
    previous = _previous("c1", _raw_citation("不在原文中的连续片段"))
    prompt = build_revision_prompt(
        "申请奖学金需要什么条件？",
        evidence,
        failed_claim_ids=["c1"],
        issues=[],
        previous_claims=[previous],
        audience=None,
    )
    user = prompt.messages[-1]["content"]
    assert "旧引用无法映射到当前证据，请改用可用窗口ID" in user
    assert "source_1_body" in user
    assert not any(is_scoped_span_id(span_id) for span_id in prompt.registry.spans)


def test_revise_accepts_server_registered_scoped_span() -> None:
    evidence = _evidence()
    previous = _previous("c1", _raw_citation(SCOPED_QUOTE))
    provider = FakeChatProvider(
        [_json(_claim_payload([_id_citation("source_1_prev_1")]))]
    )

    result = Advisor(provider).revise(
        "申请奖学金需要什么条件？",
        evidence,
        failed_claim_ids=["c1"],
        issues=[],
        previous_claims=[previous],
        audience=None,
    )

    assert result.revision_error == ""
    assert len(provider.calls) == 1
    citation = result.drafts[0].citations[0]
    assert citation.quote_id == "source_1_prev_1"
    assert citation.quote == SCOPED_QUOTE
    assert citation.article_id == ARTICLE_ID


def test_scoped_spans_preserve_part_identity_and_reject_cross_part_quote() -> None:
    part1_text = "第一片段：申请奖学金应当学习成绩优异。"
    part2_text = "第二片段：奖学金金额为8000元。"
    evidence = [
        make_evidence(make_article(article_id=ARTICLE_ID, part_no=1, text=part1_text)),
        make_evidence(make_article(article_id=ARTICLE_ID, part_no=2, text=part2_text)),
    ]
    registry = SpanRegistry.build(evidence)

    part1_id = registry.ensure_quote_span(ARTICLE_ID, "申请奖学金应当学习成绩优异")
    part2_id = registry.ensure_quote_span(ARTICLE_ID, "奖学金金额为8000元")
    assert part1_id and part2_id and part1_id != part2_id
    assert is_scoped_span_id(part1_id) and is_scoped_span_id(part2_id)

    span1 = registry.spans[part1_id]
    span2 = registry.spans[part2_id]
    assert (span1.part_no, span2.part_no) == (1, 2)
    assert span1.quote == part1_text[span1.start : span1.end]
    assert span2.quote == part2_text[span2.start : span2.end]
    assert span2.quote not in part1_text

    # 跨两个独立原始片段的拼接文本没有连续原文，不能登记成窗口。
    combined = "学习成绩优异。第二片段：奖学金金额"
    assert registry.ensure_quote_span(ARTICLE_ID, combined) is None


def test_revise_cross_part_mixing_fails_with_contract_error() -> None:
    part1_text = "第一片段：申请奖学金应当学习成绩优异。"
    part2_text = "第二片段：奖学金金额为8000元。"
    evidence = [
        make_evidence(make_article(article_id=ARTICLE_ID, part_no=1, text=part1_text)),
        make_evidence(make_article(article_id=ARTICLE_ID, part_no=2, text=part2_text)),
    ]
    previous = _previous("c1", _raw_citation(SCOPED_QUOTE))
    mixed = _json(
        _claim_payload([_raw_citation("学习成绩优异。第二片段：奖学金金额")])
    )
    provider = FakeChatProvider([mixed, mixed])

    result = Advisor(provider).revise(
        "申请奖学金需要什么条件？",
        evidence,
        failed_claim_ids=["c1"],
        issues=[],
        previous_claims=[previous],
        audience=None,
    )

    assert result.drafts == []
    assert "严格修订契约失败" in result.revision_error
    assert len(provider.calls) == 2
    retry_feedback = provider.calls[1][-1]["content"]
    assert "严格引用契约" in retry_feedback
    assert "可用 quote_id" in retry_feedback


def test_revise_retry_failure_preserves_passed_claims() -> None:
    evidence = _evidence()
    passed = _previous("c1", _raw_citation("申请奖学金应当学习成绩优异"))
    failed = _previous(
        "c2",
        {"article_id": ARTICLE_ID, "quote": TEXT, "quote_id": "source_1_body"},
        text="奖学金金额为8000元。",
    )
    raw_reply = _json(
        _claim_payload(
            [_raw_citation(SCOPED_QUOTE)],
            claim_id="c2",
            text="奖学金金额为8000元。",
        )
    )
    provider = FakeChatProvider([raw_reply, raw_reply])

    result = Advisor(provider).revise(
        "奖学金金额是多少？",
        evidence,
        failed_claim_ids=["c2"],
        issues=[],
        previous_claims=[passed, failed],
        audience=None,
    )

    # 两次契约失败只返回空修订和可观察错误，不抛 AdvisorError。
    assert result.drafts == []
    assert result.revision_error
    assert "严格修订契约失败" in result.revision_error
    assert len(result.responses) == 2
    assert len(provider.calls) == 2
    retry_feedback = provider.calls[1][-1]["content"]
    assert "严格引用契约" in retry_feedback
    assert "不是合法 JSON" not in retry_feedback

    merged = merge_revised_drafts([passed, failed], result.drafts, ["c2"])
    assert [draft.claim_id for draft in merged] == ["c1", "c2"]

    check = ClaimValidator().check(merged, evidence)
    assert [claim.claim_id for claim in check.claims] == ["c1"]
    assert [item["claim_id"] for item in check.dropped] == ["c2"]
    assert check.dropped[0]["reason"] == "NUMBER_NOT_SUPPORTED"


def test_revise_rejects_raw_reply_even_when_previous_failed_draft_is_legacy() -> None:
    evidence = _evidence()
    previous = _previous("c1", _raw_citation(SCOPED_QUOTE))
    raw_reply = _json(_claim_payload([_raw_citation(SCOPED_QUOTE)]))
    provider = FakeChatProvider([raw_reply, raw_reply])

    result = Advisor(provider).revise(
        "申请奖学金需要什么条件？",
        evidence,
        failed_claim_ids=["c1"],
        issues=[],
        previous_claims=[previous],
        audience=None,
    )

    # 旧引用已在 prompt 前登记为窗口ID，但新输出仍只能选择 quote_id。
    assert "source_1_prev_1" in provider.calls[0][-1]["content"]
    assert result.drafts == []
    assert "严格修订契约失败" in result.revision_error
    assert "只允许 citation.quote_id" in result.revision_error
    assert len(provider.calls) == 2


def test_revise_double_contract_failure_keeps_unaffected_claims() -> None:
    evidence = _evidence()
    passed = _previous("c1", _raw_citation(TEXT), text=CLAIM_TEXT)
    failed = _previous("c2", _raw_citation(SCOPED_QUOTE), text="奖学金金额为8000元。")
    raw_reply = _json(
        _claim_payload(
            [_raw_citation(SCOPED_QUOTE)],
            claim_id="c2",
            text="奖学金金额为8000元。",
        )
    )
    provider = FakeChatProvider([raw_reply, raw_reply])

    result = Advisor(provider).revise(
        "奖学金金额是多少？",
        evidence,
        failed_claim_ids=["c2"],
        issues=[],
        previous_claims=[passed, failed],
        audience=None,
    )

    assert result.drafts == []
    assert result.revision_error
    merged = merge_revised_drafts([passed, failed], result.drafts, ["c2"])
    assert merged == [passed, failed]

    check = ClaimValidator().check(merged, evidence)
    assert [claim.claim_id for claim in check.claims] == ["c1"]
    assert [item["claim_id"] for item in check.dropped] == ["c2"]
    assert check.dropped[0]["reason"] == "NUMBER_NOT_SUPPORTED"


def test_selected_source_2_id_keeps_part_identity_and_exact_offsets() -> None:
    text = "申请奖学金应当学习成绩优异。"
    evidence = [
        make_evidence(make_article(article_id=ARTICLE_ID, part_no=1, text=text)),
        make_evidence(make_article(article_id=ARTICLE_ID, part_no=2, text=text)),
    ]
    draft = _strict_parse(
        _json(_claim_payload([_id_citation("source_2_body")])), evidence
    ).claims[0]
    citation = draft.citations[0]
    assert citation.quote_id == "source_2_body"
    assert citation.trusted_part_no == 2
    assert (citation.trusted_start, citation.trusted_end) == (0, len(text))

    check = ClaimValidator().check([draft], evidence)
    assert len(check.claims) == 1
    verified = check.claims[0].citations[0]
    assert verified.part_no == 2
    assert (verified.quote_start, verified.quote_end) == (0, len(text))
    assert verified.quote == text


def test_tampered_trusted_offsets_or_part_fail_validation() -> None:
    text = "申请奖学金应当学习成绩优异。"
    evidence = [
        make_evidence(make_article(article_id=ARTICLE_ID, part_no=1, text=text)),
        make_evidence(make_article(article_id=ARTICLE_ID, part_no=2, text=text)),
    ]
    draft = _strict_parse(
        _json(_claim_payload([_id_citation("source_2_body")])), evidence
    ).claims[0]

    tampered_offsets = draft.model_copy(deep=True)
    tampered_offsets.citations[0].trusted_start = 1
    offset_check = ClaimValidator().check([tampered_offsets], evidence)
    assert offset_check.claims == []
    assert any(issue.code == "QUOTE_MISMATCH" for issue in offset_check.issues)

    tampered_part = draft.model_copy(deep=True)
    tampered_part.citations[0].trusted_part_no = 3
    part_check = ClaimValidator().check([tampered_part], evidence)
    assert part_check.claims == []
    assert any(issue.code == "CITATION_NOT_IN_EVIDENCE" for issue in part_check.issues)


def test_validated_citation_uses_quote_check_and_trusted_fields_stay_strict() -> None:
    evidence = _evidence()
    verified = Claim(
        claim_id="c1",
        text=CLAIM_TEXT,
        citations=[
            Citation(
                article_id=ARTICLE_ID,
                quote=SCOPED_QUOTE,
                quote_start=0,
                quote_end=len(SCOPED_QUOTE),
                doc_title="演示办法",
                article_no="第一条",
                effective_from=date(2021, 9, 1),
                source_url="https://example.edu.cn/policy",
            )
        ],
    )
    check = ClaimValidator().check([verified], evidence)
    assert [claim.claim_id for claim in check.claims] == ["c1"]
    start = TEXT.index(SCOPED_QUOTE)
    verified_citation = check.claims[0].citations[0]
    assert (verified_citation.quote_start, verified_citation.quote_end) == (
        start,
        start + len(SCOPED_QUOTE),
    )

    mismatched = verified.model_copy(deep=True)
    mismatched.citations[0].quote = "原文中没有的连续片段"
    mismatch_check = ClaimValidator().check([mismatched], evidence)
    assert mismatch_check.claims == []
    assert any(issue.code == "QUOTE_MISMATCH" for issue in mismatch_check.issues)

    trusted = _strict_parse(
        _json(_claim_payload([_id_citation("source_1_body")])), evidence
    ).claims[0]
    trusted.citations[0].trusted_start = 1
    trusted_check = ClaimValidator().check([trusted], evidence)
    assert trusted_check.claims == []
    assert any(issue.code == "QUOTE_MISMATCH" for issue in trusted_check.issues)


def test_advisor_trace_records_revision_error_and_keeps_passed_drafts() -> None:
    evidence = _evidence()
    passed = _previous("c1", _raw_citation(TEXT), text=CLAIM_TEXT)
    failed = _previous("c2", _raw_citation(SCOPED_QUOTE), text="奖学金金额为8000元。")
    raw_reply = _json(
        _claim_payload(
            [_raw_citation(SCOPED_QUOTE)],
            claim_id="c2",
            text="奖学金金额为8000元。",
        )
    )
    nodes = GraphNodes(
        planner=None,
        retriever=None,
        advisor=Advisor(FakeChatProvider([raw_reply, raw_reply])),
        verifier=GraphVerifier(),
        catalog=None,
        max_revise=2,
    )
    state = {
        "question": "奖学金金额是多少？",
        "evidence": evidence,
        "audience": None,
        "revision_count": 1,
        "failed_claim_ids": ["c2"],
        "drafts": [passed, failed],
        "revise_issues": [],
        "revise_requirements": [],
        "trace": [],
    }

    update = nodes.advisor(state)

    # 契约连续失败后耗尽本轮修订预算，已通过项原样保留。
    assert update["drafts"] == [passed, failed]
    assert update["revision_count"] == 2
    trace = update["trace"][-1]
    assert trace["mode"] == "revise"
    assert "严格修订契约失败" in trace["revision_error"]

    # Verifier 不再发起改写，只丢弃仍失败的 claim。
    verdict = nodes.verifier({**state, **update})
    assert verdict["decision"] is GraphDecision.PASS
    assert [claim.claim_id for claim in verdict["claims"]] == ["c1"]
    assert verdict["failed_claim_ids"] == ["c2"]


def test_generation_legacy_accepts_raw_but_quote_id_must_match() -> None:
    evidence = _evidence()

    raw = parse_claims_json(
        _json(_claim_payload([_raw_citation(SCOPED_QUOTE)])), evidence
    )
    assert raw.claims[0].citations[0].quote == SCOPED_QUOTE
    assert raw.claims[0].citations[0].quote_id == ""

    matching = parse_claims_json(
        _json(
            _claim_payload(
                [{"article_id": ARTICLE_ID, "quote_id": "source_1_body"}]
            )
        ),
        evidence,
    )
    assert matching.claims[0].citations[0].quote == TEXT

    for citation in (
        {"quote_id": "source_1_body", "article_id": "another@v1#x"},
        {"quote_id": "source_1_body", "quote": "篡改正文内容"},
        {"quote_id": "source_9_body"},
    ):
        with pytest.raises(AdvisorError):
            parse_claims_json(_json(_claim_payload([citation])), evidence)


def test_quote_repair_legacy_draft_uses_server_registered_id() -> None:
    evidence = _evidence()
    provider = FakeChatProvider(
        [
            _json(_claim_payload([_raw_citation("原文中没有这句话")])),
            _json(_claim_payload([_id_citation("source_1_body")])),
        ]
    )

    result = Advisor(provider).generate("申请奖学金需要什么条件？", evidence)

    assert result.quote_repair_attempted is True
    assert result.quote_repaired is True
    assert result.quote_repair_remaining == []
    citation = result.drafts[0].citations[0]
    assert citation.quote_id == "source_1_body"
    assert citation.quote == TEXT


def test_quote_repair_id_based_draft_rejects_raw_reply() -> None:
    evidence = _evidence()
    provider = FakeChatProvider(
        [
            _json(
                _claim_payload(
                    [_id_citation("source_1_body")], text="奖学金金额为8000元。"
                )
            ),
            _json(
                _claim_payload(
                    [_raw_citation(TEXT)],
                    text="奖学金金额为8000元。",
                )
            ),
        ]
    )

    result = Advisor(provider).generate("奖学金金额是多少？", evidence)

    assert result.quote_repair_attempted is True
    assert "只允许 citation.quote_id" in result.quote_repair_error
    assert result.quote_repair_remaining == ["c1"]
    citation = result.drafts[0].citations[0]
    assert citation.quote_id == "source_1_body"
    assert citation.quote == TEXT

    check = ClaimValidator().check(result.drafts, evidence)
    assert check.claims == []
    assert "c1" in check.dropped_claim_ids


def test_citation_id_alone_does_not_pass_fact_coverage() -> None:
    evidence = _evidence()
    draft = _strict_parse(
        _json(
            _claim_payload(
                [_id_citation("source_1_body")], text="奖学金金额为8000元。"
            )
        ),
        evidence,
    ).claims[0]

    check = ClaimValidator().check([draft], evidence)

    assert check.claims == []
    assert any(issue.code == "NUMBER_NOT_SUPPORTED" for issue in check.issues)
