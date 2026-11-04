"""Coverage regression tests for context frames and explicit fact targets.

Each case runs the same deterministic checker twice: once with the pessimistic
``generation_missing`` model verdict and once with the optimistic ``answered``
verdict. The checker must recover the former and validate the latter only when
the final claim actually contains the asked facts.
"""

from __future__ import annotations

import json
from datetime import date

import pytest

from campusguide.agents.coverage import CoverageChecker
from campusguide.agents.plan import QuestionUnit
from campusguide.agents.providers import ChatResponse
from campusguide.domain.models import Citation, Claim


class _Provider:
    def __init__(self, payload: dict) -> None:
        self.payload = payload

    def complete(
        self,
        messages: list[dict[str, str]],
        *,
        temperature: float = 0.2,
        max_tokens: int = 1200,
    ) -> ChatResponse:
        return ChatResponse(
            content=json.dumps(self.payload, ensure_ascii=False),
            model="test",
        )


def _citation(title: str, article_no: str = "第一条") -> Citation:
    return Citation(
        article_id=f"{title}#{article_no}",
        quote="原文章节",
        quote_start=0,
        quote_end=4,
        doc_title=title,
        article_no=article_no,
        effective_from=date(2026, 1, 1),
        source_url="https://example.edu.cn/notice",
    )


def _claim(text: str, title: str) -> Claim:
    return Claim(claim_id="c1", text=text, citations=[_citation(title)])


def _unit(
    question: str,
    *,
    aspects: list[str] | None = None,
    audience: list[str] | None = None,
) -> QuestionUnit:
    span = question.rstrip("？?。 ")
    return QuestionUnit(
        unit_id="u1",
        original_span=span,
        audience_scope=audience or [],
        requested_aspects=aspects or [span],
    )


def _run(
    question: str,
    claim_text: str,
    title: str,
    *,
    status: str,
    missing: list[str],
    aspects: list[str] | None = None,
    audience: list[str] | None = None,
):
    unit = _unit(question, aspects=aspects, audience=audience)
    provider = _Provider(
        {
            "units": [
                {
                    "unit_id": "u1",
                    "status": status,
                    "claim_ids": ["c1"] if status == "answered" else [],
                    "missing_items": missing,
                }
            ],
            "unanswered_questions": [],
        }
    )
    audit = CoverageChecker(provider).check(
        question,
        [unit],
        [_claim(claim_text, title)],
        [],
        [],
    )
    assert audit.unavailable is False
    return audit, audit.units[0]


FOCUS_CASES = [
    (
        "VD06",
        "本科考试中，学生考前应提前多久到场，迟到多久不能参加，开考后多久内不能离场？",
        "学生应在考前10分钟进入考场，迟到30分钟者不得参加该课程考试并作缺考论，开考后30分钟内不得离开考场。",
        "华南农业大学本科学生考场工作细则",
        ["学生考前提前多久"],
        ["本科考试中，学生考前应提前多久到场，迟到多久不能参加，开考后多久内不能离场"],
    ),
    (
        "VD08",
        "平时考试发现作弊后，相关试卷和登记表应在几日内报送哪里处理？",
        "平时的考试或考查，应在3日内将违纪作弊学生试卷、《考场情况记录表》《违规情况登记表》和《违规情况登记告知书》报送本科生院进行处理。",
        "华南农业大学本科学生考场工作细则",
        ["应在几日内报送"],
        ["平时考试发现作弊后，相关试卷和登记表应在几日内报送哪里处理"],
    ),
    (
        "VH02",
        "我还是本科在校生，能办理学历证明吗，可以出具哪一种证明？",
        "本科在校生未毕业学生不办理学历证明，只出具在读证明。",
        "中英文成绩单、学历学位证明及其他证明",
        ["我还是本科在校生", "可以出具种证明"],
        ["我还是本科在校生，能办理学历证明吗，可以出具哪一种证明"],
    ),
    (
        "VH03",
        "办理成绩单等证明时，在校生前5份收费吗，累计超过5份每份多少钱？",
        "办理出国成绩单及学历学位证明时，在校生5份内免费办理，累计超出5份的，每份收取5元。",
        "关于办理出国成绩单及学历学位证明的说明",
        ["收费吗"],
        ["费用"],
    ),
    (
        "VH04",
        "我想打印本科成绩单，学校哪些地点可以24小时自助打印？",
        "学校24小时自助打印点包括：行政楼一楼大厅、三角市紫荆e站24小时自助服务大厅。",
        "本科生自助打印服务指南",
        ["我想打印本科成绩单"],
        ["我想打印本科成绩单，学校哪些地点可以24小时自助打印"],
    ),
    (
        "VD06-duration-owned-capability",
        "考生迟到多久不能参加考试？",
        "考生迟到30分钟不得参加考试。",
        "华南农业大学本科学生考场工作细则",
        [],
        ["考生迟到多久不能参加考试"],
    ),
    (
        "VD08-recipient-agency",
        "相关试卷和登记表应在几日内报送哪里处理？",
        "相关试卷和登记表应在3日内报送本科生院处理。",
        "华南农业大学本科学生考场工作细则",
        [],
        ["相关试卷和登记表应在几日内报送哪里处理"],
    ),
    (
        "VH03-tier-amount",
        "在校生成绩单前3份收费吗，累计超过3份每份多少钱？",
        "办理成绩单时，在校生3份内免费办理，累计超过3份的，每份收取7元。",
        "关于办理出国成绩单及学历学位证明的说明",
        [],
        ["在校生成绩单前3份收费吗，累计超过3份每份多少钱"],
    ),
]


@pytest.mark.parametrize(
    "case_id,question,claim_text,title,optimistic_missing,pessimistic_missing",
    FOCUS_CASES,
)
@pytest.mark.parametrize("verdict", ["pessimistic", "optimistic"])
def test_focus_cases_answer_under_both_verdicts(
    case_id: str,
    question: str,
    claim_text: str,
    title: str,
    optimistic_missing: list[str],
    pessimistic_missing: list[str],
    verdict: str,
) -> None:
    if verdict == "pessimistic":
        audit, row = _run(
            question,
            claim_text,
            title,
            status="generation_missing",
            missing=pessimistic_missing,
        )
    else:
        audit, row = _run(
            question,
            claim_text,
            title,
            status="answered",
            missing=optimistic_missing,
        )
    assert case_id
    assert row["status"] == "answered", (case_id, verdict, row, audit.missing)
    assert row["claim_ids"] == ["c1"]
    assert row["missing_items"] == []
    assert audit.missing == []


PARTIAL_CASES = [
    (
        "VD06-partial",
        "本科考试中，学生考前应提前多久到场，迟到多久不能参加，开考后多久内不能离场？",
        "学生应在考前10分钟进入考场。",
        "华南农业大学本科学生考场工作细则",
        None,
        ["本科"],
    ),
    (
        "VD08-missing-object",
        "平时考试发现作弊后，相关试卷和登记表应在几日内报送哪里处理？",
        "平时的考试或考查，应在3日内将相关材料报送本科生院进行处理。",
        "华南农业大学本科学生考场工作细则",
        None,
        ["本科"],
    ),
    (
        "VH02-no-concrete-type",
        "我还是本科在校生，能办理学历证明吗，可以出具哪一种证明？",
        "本科在校生未毕业学生不办理学历证明。",
        "中英文成绩单、学历学位证明及其他证明",
        None,
        ["本科"],
    ),
    (
        "VH03-wrong-tier",
        "办理成绩单等证明时，在校生前5份收费吗，累计超过5份每份多少钱？",
        "办理出国成绩单及学历学位证明时，在校生5份内免费办理，每份收取5元。",
        "关于办理出国成绩单及学历学位证明的说明",
        ["费用"],
        [],
    ),
    (
        "VH03-wrong-object",
        "在校生成绩单前5份收费吗，累计超过5份每份多少钱？",
        "住宿费在校生5份内免费办理，累计超过5份每份收取5元。",
        "关于办理出国成绩单及学历学位证明的说明",
        ["费用"],
        [],
    ),
    (
        "VH04-generic-place",
        "我想打印本科成绩单，学校哪些地点可以24小时自助打印？",
        "学校设有24小时自助打印点，提供自助打印服务。",
        "本科生自助打印服务指南",
        None,
        ["本科"],
    ),
    (
        "VD08-missing-recipient",
        "相关试卷和登记表应在几日内报送哪里处理？",
        "相关试卷和登记表应在3日内报送处理。",
        "华南农业大学本科学生考场工作细则",
        None,
        None,
    ),
    (
        "VD08-missing-form",
        "相关试卷和登记表应在几日内报送哪里处理？",
        "相关试卷应在3日内报送本科生院处理。",
        "华南农业大学本科学生考场工作细则",
        None,
        None,
    ),
    (
        "VD08-missing-duration",
        "相关试卷和登记表应在几日内报送哪里处理？",
        "相关试卷和登记表应报送本科生院处理。",
        "华南农业大学本科学生考场工作细则",
        None,
        None,
    ),
    (
        "VH03-wrong-population",
        "在校生成绩单前3份收费吗，累计超过3份每份多少钱？",
        "毕业生成绩单3份内免费办理，累计超过3份的，每份收取7元。",
        "关于办理出国成绩单及学历学位证明的说明",
        None,
        None,
    ),
    (
        "VH03-wrong-tier",
        "在校生成绩单前3份收费吗，累计超过3份每份多少钱？",
        "在校生成绩单2份内免费办理，累计超过2份的，每份收取7元。",
        "关于办理出国成绩单及学历学位证明的说明",
        None,
        None,
    ),
    (
        "VH03-missing-excess",
        "在校生成绩单前3份收费吗，累计超过3份每份多少钱？",
        "在校生成绩单3份内免费办理，每份收取7元。",
        "关于办理出国成绩单及学历学位证明的说明",
        None,
        None,
    ),
]


@pytest.mark.parametrize(
    "case_id,question,claim_text,title,aspects,audience", PARTIAL_CASES
)
@pytest.mark.parametrize("verdict", ["pessimistic", "optimistic"])
def test_partial_or_wrong_fact_is_not_answered(
    case_id: str,
    question: str,
    claim_text: str,
    title: str,
    aspects: list[str] | None,
    audience: list[str] | None,
    verdict: str,
) -> None:
    if verdict == "pessimistic":
        audit, row = _run(
            question,
            claim_text,
            title,
            status="generation_missing",
            missing=[question],
            aspects=aspects,
            audience=audience,
        )
    else:
        audit, row = _run(
            question,
            claim_text,
            title,
            status="answered",
            missing=[],
            aspects=aspects,
            audience=audience,
        )
    assert case_id
    assert row["status"] != "answered", (case_id, verdict, row, audit.missing)
    assert row["missing_items"] or audit.missing


SYNTHETIC_POSITIVE_CASES = [
    (
        "duration-arrive",
        "考生考前多长时间到达考场？",
        "考生应在考前15分钟进入考场。",
        "华南农业大学本科学生考场工作细则",
        None,
        ["本科"],
    ),
    (
        "duration-return-days",
        "材料提交后，应在几日内报送学院？",
        "材料应在3日内报送学院。",
        "本科生学籍管理规定",
        None,
        [],
    ),
    (
        "fee-tier-paraphrase",
        "在校生成绩单前5份收费吗，累计超过5份每份多少钱？",
        "办理成绩单证明时，在校生成绩单5份内免费办理，累计超出5份的，每份收取5元。",
        "关于办理出国成绩单及学历学位证明的说明",
        ["费用"],
        [],
    ),
]


@pytest.mark.parametrize(
    "case_id,question,claim_text,title,aspects,audience", SYNTHETIC_POSITIVE_CASES
)
def test_synthetic_paraphrases_are_answered(
    case_id: str,
    question: str,
    claim_text: str,
    title: str,
    aspects: list[str] | None,
    audience: list[str] | None,
) -> None:
    audit, row = _run(
        question,
        claim_text,
        title,
        status="generation_missing",
        missing=[question],
        aspects=aspects,
        audience=audience,
    )
    assert case_id
    assert row["status"] == "answered", (case_id, row, audit.missing)
    assert audit.missing == []


SYNTHETIC_NEGATIVE_CASES = [
    (
        "duration-wrong-action",
        "材料提交后，应在几日内报送学院？",
        "材料应在3日内审核完毕。",
        "本科生学籍管理规定",
        None,
        [],
    ),
    (
        "duration-wrong-object",
        "材料提交后，应在几日内报送学院？",
        "学院应在3日内完成审核。",
        "本科生学籍管理规定",
        None,
        [],
    ),
    (
        "context-wrong-audience",
        "我是本科在校生，可以出具哪一种证明？",
        "研究生可出具在读证明，不办理学历证明。",
        "中英文成绩单、学历学位证明及其他证明",
        None,
        ["本科"],
    ),
    (
        "target-generic-type",
        "可以出具哪一种证明？",
        "学校可以出具证明。",
        "中英文成绩单、学历学位证明及其他证明",
        None,
        [],
    ),
]


@pytest.mark.parametrize(
    "case_id,question,claim_text,title,aspects,audience", SYNTHETIC_NEGATIVE_CASES
)
def test_synthetic_negative_pairs_remain_missing(
    case_id: str,
    question: str,
    claim_text: str,
    title: str,
    aspects: list[str] | None,
    audience: list[str] | None,
) -> None:
    audit, row = _run(
        question,
        claim_text,
        title,
        status="answered",
        missing=[],
        aspects=aspects,
        audience=audience,
    )
    assert case_id
    assert row["status"] != "answered", (case_id, row, audit.missing)
    assert row["missing_items"] or audit.missing
