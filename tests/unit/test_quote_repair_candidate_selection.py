"""ND19：quote repair 必须推荐同文档/版本内业务对象匹配的精确原文窗口。

这些测试只使用合成片段，不读取评测集；重点是：
- 裸 ``6`` 仍不是 ``6学分``；
- 两字偶然实体（``实践``）不能冒充业务对象（``专业实践``）；
- 数字锚点前只有“计”时，实体可以来自同一句；
- 候选仍是从登记窗口原样取回，不拼接、不推断。
"""

from __future__ import annotations

import json

from campusguide.agents.advisor import Advisor, ClaimDraft
from campusguide.agents.providers import FakeChatProvider
from campusguide.agents.quote_repair import (
    MAX_ADDITIONAL_CLAIM_CANDIDATES,
    SpanRegistry,
    build_repair_messages,
    find_quote_problems,
)
from campusguide.domain.models import Evidence
from campusguide.verification.claims import ClaimValidator
from campusguide.verification.facts import check_evidence_fact_coverage
from tests.w2_helpers import make_article, make_evidence

DOC_ID = "graduate-handbook"
VERSION = "v2026"
OTHER_VERSION = "v2025"

FLAT_TEXT = "三、培养环节及时间安排\n专业实践    第五学期    6"
FLAT_ID = f"{DOC_ID}@{VERSION}#培养环节表"

WRONG_TEXT = "社会主义理论与实践 6学分。"
WRONG_ID = f"{DOC_ID}@{VERSION}#社会主义理论与实践"

UNRELATED_TEXT = "专业实践在第五学期结束前完成，其他事项计6学分。"
UNRELATED_ID = f"{DOC_ID}@{VERSION}#其他事项"

LONG_TEXT = (
    "第二章 培养方案\n"
    "培养环节：9学分，其中专业实践6学分，第五学期完成，其他3学分。"
)
LONG_ID = f"{DOC_ID}@{VERSION}#培养方案"

SHORT_TEXT = "培养环节：专业实践    第五学期    6 学分。"
SHORT_ID = f"{DOC_ID}@{VERSION}#培养环节行"

OTHER_VERSION_TEXT = "培养环节：专业实践    第五学期    6 学分。"
OTHER_VERSION_ID = f"{DOC_ID}@{OTHER_VERSION}#培养环节行"

CLAIM_FULL = "工业工程与管理2025—2026级全日制和非全日制专业实践计6学分。"
CLAIM_TIMING = "专业实践在第五学期结束前完成，计6学分。"
QUESTION = "工业工程与管理2025—2026级专业实践计多少学分？"

TRAINING_DOC = "training-handbook"
TRAINING_VERSION = "v2027"
TRAINING_FLAT_TEXT = "2.科研训练 | 第三学期 | 4 |"
TRAINING_FLAT_ID = f"{TRAINING_DOC}@{TRAINING_VERSION}#section3"
TRAINING_CORRECT_TEXT = "培养环节：9学分，其中科研训练4学分，其他3学分。"
TRAINING_CORRECT_ID = f"{TRAINING_DOC}@{TRAINING_VERSION}#table.row2"
TRAINING_WRONG_TEXT = "其中其他课程需完成4学分。"
TRAINING_WRONG_ID = f"{TRAINING_DOC}@{TRAINING_VERSION}#other"
TRAINING_CLAIM = "参与科研训练需完成4学分。"
TRAINING_SEPARATED_CLAIM = "参与科研训练，需完成4学分。"


def _article(article_id: str, text: str, *, version_id: str = VERSION):
    return make_article(
        article_id=article_id,
        doc_id=DOC_ID,
        version_id=version_id,
        doc_title="工业工程与管理专业学位研究生培养方案",
        article_no=article_id.rsplit("#", 1)[-1],
        text=text,
    )


def _build_evidence(rows: list[tuple[str, str, str]]) -> list[Evidence]:
    return [
        make_evidence(
            _article(article_id, text, version_id=version),
            rank=index,
        )
        for index, (article_id, text, version) in enumerate(rows, 1)
    ]


def _base_evidence() -> list[Evidence]:
    return _build_evidence(
        [
            (FLAT_ID, FLAT_TEXT, VERSION),
            (WRONG_ID, WRONG_TEXT, VERSION),
            (UNRELATED_ID, UNRELATED_TEXT, VERSION),
            (LONG_ID, LONG_TEXT, VERSION),
            (SHORT_ID, SHORT_TEXT, VERSION),
            (OTHER_VERSION_ID, OTHER_VERSION_TEXT, OTHER_VERSION),
        ]
    )


def _draft(text: str = CLAIM_FULL) -> ClaimDraft:
    return ClaimDraft.model_validate(
        {
            "claim_id": "c1",
            "text": text,
            "kind": "other",
            "citations": [{"article_id": FLAT_ID, "quote": FLAT_TEXT}],
        }
    )


def _catalog(text: str, evidence: list[Evidence]):
    drafts = [_draft(text)]
    problems = find_quote_problems(drafts, evidence)
    assert problems
    registry = SpanRegistry.build(evidence)
    candidates = registry.candidates_for_claims(drafts, problems)
    return registry, drafts, problems, candidates["c1"]


def _training_article(article_id: str, text: str):
    return make_article(
        article_id=article_id,
        doc_id=TRAINING_DOC,
        version_id=TRAINING_VERSION,
        doc_title="培养方案",
        article_no="条款",
        text=text,
    )


def _training_evidence() -> list[Evidence]:
    return [
        make_evidence(_training_article(TRAINING_FLAT_ID, TRAINING_FLAT_TEXT), rank=1),
        make_evidence(_training_article(TRAINING_CORRECT_ID, TRAINING_CORRECT_TEXT), rank=2),
        make_evidence(_training_article(TRAINING_WRONG_ID, TRAINING_WRONG_TEXT), rank=3),
    ]


def _training_draft(text: str) -> ClaimDraft:
    return ClaimDraft.model_validate(
        {
            "claim_id": "c1",
            "text": text,
            "kind": "other",
            "citations": [
                {"article_id": TRAINING_FLAT_ID, "quote": TRAINING_FLAT_TEXT}
            ],
        }
    )


def _training_views(text: str):
    evidence = _training_evidence()
    drafts = [_training_draft(text)]
    problems = find_quote_problems(drafts, evidence)
    assert problems
    registry = SpanRegistry.build(evidence)
    candidates = registry.candidates_for_claims(drafts, problems)["c1"]
    legacy = registry.ids_for_claims(drafts, problems)["c1"]
    return candidates, legacy


def test_candidate_selection_prefers_precise_same_family_literal() -> None:
    evidence = _base_evidence()
    _, _, _, candidates = _catalog(CLAIM_FULL, evidence)

    assert candidates.missing_facts == ["6学分"]
    assert candidates.original_span_ids == ["source_1_body"]
    recommended = candidates.recommended_span_ids
    assert recommended[0] == "source_5_body"
    assert "source_4_body" in recommended
    # 偶然共享“实践”两字的窗口和无关小句都不能被推荐。
    assert "source_2_body" not in recommended
    assert "source_3_body" not in recommended
    # 其他版本的相同文字不属于本 claim 的来源家族。
    assert "source_6_body" not in recommended
    for excluded in ("source_2_body", "source_3_body", "source_6_body"):
        assert excluded not in candidates.span_ids
    assert "source_1_body" in candidates.span_ids


def test_entity_elsewhere_in_sentence_does_not_borrow_other_clause_fact() -> None:
    evidence = _base_evidence()
    _, _, _, candidates = _catalog(CLAIM_TIMING, evidence)

    # “第五学期”已由原始引用覆盖；缺的只有 6 学分，且数字前只有“计”。
    assert candidates.missing_facts == ["6学分"]
    recommended = candidates.recommended_span_ids
    assert recommended[:2] == ["source_5_body", "source_4_body"]
    assert "source_2_body" not in recommended
    # 同一句里的“专业实践”仍能作为实体锚点。
    by_id = {item.span_id: item for item in candidates.recommended}
    assert by_id["source_5_body"].supported_facts == ("6学分",)
    assert by_id["source_4_body"].supported_facts == ("6学分",)
    # 该窗口虽然有字面 6 学分，但最近的实体是“其他事项”，不能借给专业实践。
    assert "source_3_body" not in recommended


def test_flat_bare_value_still_fails_deterministic_coverage() -> None:
    evidence = _base_evidence()
    coverage = check_evidence_fact_coverage(
        CLAIM_FULL, [FLAT_TEXT], [evidence[0].article]
    )
    assert [fact.canonical for fact in coverage.uncovered] == ["6学分"]

    check = ClaimValidator().check([_draft(CLAIM_FULL)], evidence)
    assert check.claims == []
    assert any(item["reason"] == "NUMBER_NOT_SUPPORTED" for item in check.dropped)


def test_no_eligible_candidate_fails_closed() -> None:
    evidence = _build_evidence(
        [
            (FLAT_ID, FLAT_TEXT, VERSION),
            (WRONG_ID, WRONG_TEXT, VERSION),
        ]
    )
    registry, drafts, problems, candidates = _catalog(CLAIM_FULL, evidence)

    assert candidates.missing_facts == ["6学分"]
    assert candidates.recommended == []
    assert candidates.span_ids == ["source_1_body"]

    messages = build_repair_messages(
        [
            {"role": "system", "content": "BASE-SYSTEM"},
            {"role": "user", "content": f"问题：{QUESTION}"},
        ],
        "{}",
        drafts,
        problems,
        spans=registry.spans,
        candidates_by_claim={"c1": candidates},
    )
    content = messages[-1]["content"]
    assert "没有找到与 claim 业务对象一致" in content
    assert "请删除该 claim" in content
    assert "不得把裸数字当作带单位事实" in content
    assert "[上一轮引用]" in content
    assert "source_1_body" in content[content.index("[上一轮引用]") :]


def test_need_verb_keeps_entity_head_and_offers_candidate() -> None:
    candidates, legacy = _training_views(TRAINING_CLAIM)

    recommended = candidates.recommended_span_ids
    assert "source_2_body" in recommended
    assert "source_3_body" not in recommended
    assert legacy == ["source_1_body", "source_2_body"]


def test_need_verb_after_punctuation_still_offers_candidate() -> None:
    candidates, legacy = _training_views(TRAINING_SEPARATED_CLAIM)

    assert "source_2_body" in candidates.recommended_span_ids
    assert "source_3_body" not in candidates.recommended_span_ids
    assert legacy == ["source_1_body", "source_2_body"]


def test_legacy_ids_for_claims_keep_originals_first() -> None:
    evidence = _base_evidence()
    drafts = [_draft(CLAIM_FULL)]
    problems = find_quote_problems(drafts, evidence)
    assert problems
    registry = SpanRegistry.build(evidence)
    candidates = registry.candidates_for_claims(drafts, problems)["c1"]
    legacy = registry.ids_for_claims(drafts, problems)["c1"]

    # 新视图仍推荐优先；旧兼容视图保留 original-first。
    assert candidates.span_ids[:2] == ["source_5_body", "source_4_body"]
    assert legacy == ["source_1_body", "source_5_body", "source_4_body"]


def test_repair_prompt_shows_identity_literal_and_support() -> None:
    evidence = _base_evidence()
    registry, drafts, problems, candidates = _catalog(CLAIM_FULL, evidence)
    messages = build_repair_messages(
        [
            {"role": "system", "content": "BASE-SYSTEM"},
            {"role": "user", "content": f"问题：{QUESTION}"},
        ],
        "{}",
        drafts,
        problems,
        spans=registry.spans,
        candidates_by_claim={"c1": candidates},
    )
    content = messages[-1]["content"]

    assert "缺少的类型化事实：6学分" in content
    assert f"article_id={SHORT_ID}" in content
    assert "支持=6学分" in content
    assert f"原文={json.dumps(SHORT_TEXT, ensure_ascii=False)}" in content
    assert content.index("[推荐]") < content.index("[上一轮引用]")
    assert content.index(SHORT_ID) < content.index("[上一轮引用]")
    assert "source_1_body" in content[content.index("[上一轮引用]") :]
    # 原始问题和系统提示仍保留在修复对话前缀中。
    assert messages[:2] == [
        {"role": "system", "content": "BASE-SYSTEM"},
        {"role": "user", "content": f"问题：{QUESTION}"},
    ]


def test_advisor_repair_uses_recommended_window_without_extra_retry() -> None:
    evidence = _base_evidence()
    provider = FakeChatProvider(
        [
            json.dumps(
                {
                    "claims": [
                        {
                            "claim_id": "c1",
                            "text": CLAIM_FULL,
                            "kind": "other",
                            "citations": [{"article_id": FLAT_ID, "quote": FLAT_TEXT}],
                        }
                    ]
                },
                ensure_ascii=False,
            ),
            json.dumps(
                {
                    "claims": [
                        {
                            "claim_id": "c1",
                            "text": CLAIM_FULL,
                            "kind": "other",
                            "citations": [{"quote_id": "source_5_body"}],
                        }
                    ]
                },
                ensure_ascii=False,
            ),
        ]
    )

    result = Advisor(provider).generate(QUESTION, evidence)

    assert len(provider.calls) == 2
    repair_content = provider.calls[1][-1]["content"]
    assert "source_5_body" in repair_content
    assert "支持=6学分" in repair_content
    assert "原文=" in repair_content
    assert "推荐窗口只是已有原文候选" in repair_content
    assert QUESTION in provider.calls[1][1]["content"]
    assert result.quote_repair_attempted is True
    assert result.quote_repair_remaining == []

    check = ClaimValidator().check(result.drafts, evidence)
    assert len(check.claims) == 1
    citation = check.claims[0].citations[0]
    assert citation.article_id == SHORT_ID
    assert citation.quote == SHORT_TEXT


def test_recommended_candidates_are_bounded() -> None:
    evidence = [
        make_evidence(_article(FLAT_ID, FLAT_TEXT), rank=1),
        *[
            make_evidence(
                _article(
                    f"{DOC_ID}@{VERSION}#专业实践{i}",
                    "培养环节：专业实践    第五学期    6 学分。",
                ),
                rank=i + 1,
            )
            for i in range(10)
        ],
    ]
    _, _, _, candidates = _catalog(CLAIM_FULL, evidence)

    assert len(candidates.recommended) == MAX_ADDITIONAL_CLAIM_CANDIDATES
    assert all(
        item.span_id != "source_1_body" for item in candidates.recommended
    )
