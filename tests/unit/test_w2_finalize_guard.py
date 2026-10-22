from __future__ import annotations

from campusguide.agents.advisor import CitationDraft, ClaimDraft
from campusguide.domain.enums import BlockType, OutcomeCode
from campusguide.qa.finalize import DISCLAIMER, LOW_DATE_NOTICE, finalize
from campusguide.qa.guard import check_guard
from campusguide.verification.claims import ClaimValidator
from tests.w2_helpers import make_article, make_evidence


def _claim(text: str, article, quote: str, block_type: BlockType = BlockType.ARTICLE):
    draft = ClaimDraft(
        claim_id="c1",
        text=text,
        citations=[CitationDraft(article_id=article.article_id, quote=quote)],
    )
    return ClaimValidator().validate([draft], [make_evidence(article)]).claims[0]


def test_finalize_renders_claims_and_citation_cards() -> None:
    article = make_article(date_confidence="low")
    claim = _claim("申请奖学金需要成绩优异。", article, "学习成绩优异")
    result = finalize([claim])

    assert "结论要点：" in result.answer_markdown
    assert "申请奖学金需要成绩优异。" in result.answer_markdown
    assert "[演示办法 第一条]" in result.answer_markdown
    assert "> 学习成绩优异" in result.answer_markdown
    assert LOW_DATE_NOTICE in result.answer_markdown
    assert DISCLAIMER in result.answer_markdown
    assert len(result.citations) == 1


def test_finalize_section_label_does_not_use_article_wording() -> None:
    article = make_article(
        article_id="plan@v2024#二、培养目标",
        doc_title="培养方案",
        article_no="二、培养目标",
        block_type=BlockType.SECTION,
        text="本专业培养具有社会责任感的数学人才。",
    )
    claim = _claim("培养目标是培养数学人才。", article, "具有社会责任感的数学人才")
    result = finalize([claim])

    assert "章节/段落" in result.answer_markdown
    assert "第 二、培养目标" not in result.answer_markdown


def test_guard_rejects_empty_history_privacy_and_out_of_scope() -> None:
    assert check_guard("").outcome is OutcomeCode.INVALID_REQUEST
    assert check_guard("   ").outcome is OutcomeCode.INVALID_REQUEST
    assert check_guard("问" * 301).outcome is OutcomeCode.INVALID_REQUEST
    assert (
        check_guard("2020 年版本的国家奖学金办法").outcome is OutcomeCode.HISTORICAL_NOT_SUPPORTED
    )
    assert check_guard("去年的转专业政策").outcome is OutcomeCode.HISTORICAL_NOT_SUPPORTED
    assert check_guard("帮我查一下我的学号和成绩排名").outcome is OutcomeCode.PRIVACY_REQUEST
    assert check_guard("隔壁学校的奖学金政策").outcome is OutcomeCode.OUT_OF_SCOPE


def test_guard_allows_normal_scau_question() -> None:
    assert check_guard("国家奖学金申请条件是什么？").allowed
    assert check_guard("隔壁学校与华南农业大学相比如何").allowed
