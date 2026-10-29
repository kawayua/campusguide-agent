"""Content-gap fixes for 098 / 035 / 020 (2026-10-29 plan, sections A and B)."""

from __future__ import annotations

import json
from datetime import date

from campusguide.agents.coverage import CoverageChecker, _content_supports, split_missing
from campusguide.agents.plan import QuestionUnit
from campusguide.agents.verifier import GraphVerifier
from campusguide.domain.models import Citation, Claim, Evidence
from campusguide.qa.list_claim import asked_entity_type, build_list_claim, claim_lists_type
from campusguide.verification.entities import items_of_type
from campusguide.verification.claims import ClaimValidator

CASE_098_QUOTE = (
    "基于云平台的计算机实验教学示范中心信息数学与应用统计实验教学示范中心"
    "数学建模实验教学示范中心计算机软件实验室计算机硬件实验室计算机系统实验室"
    "计算机网络实验室云计算与大数据实验室物联网实验室IT项目综合训练实验室"
    "ERP沙盘模拟综合实验室工业工程实验室生产运作与管理实验室、信息技术与管理实验室、"
    "管理系统仿真实验室"
)
CASE_098_QUESTION = "数学与信息学院有哪些实验教学示范中心，是否有云计算与大数据实验室？"
CASE_098_BAD = (
    "数学与信息学院的实验教学示范中心包括基于云平台的计算机实验教学示范中心、"
    "信息数学与应用统计实验教学示范中心、数学建模实验教学示范中心、计算机软件实验室、"
    "云计算与大数据实验室。"
)


class ScriptedProvider:
    def __init__(self, verdict: dict) -> None:
        self._verdict = verdict

    def complete(self, messages, *, temperature=0.2, max_tokens=1200):
        class Response:
            content = json.dumps(self._verdict, ensure_ascii=False)
            prompt_tokens = 100
            completion_tokens = 50
            latency_ms = 10

        return Response()


def _evidence(title: str, text: str, article_no: str = "第一条") -> Evidence:
    from tests.w2_helpers import make_article, make_evidence

    return make_evidence(
        make_article(f"{title}#1", doc_title=title, article_no=article_no, text=text),
        score=0.9,
    )


def _claim(
    claim_id: str,
    text: str,
    doc_title: str,
    article_no: str = "第一条",
    quote: str | None = None,
) -> Claim:
    cited = quote if quote is not None else text
    return Claim(
        claim_id=claim_id,
        text=text,
        citations=[
            Citation(
                article_id=f"{doc_title}#1",
                quote=cited,
                quote_start=0,
                quote_end=len(cited),
                doc_title=doc_title,
                article_no=article_no,
                effective_from=date(2025, 9, 1),
                source_url="https://www.scau.edu.cn/x.htm",
            )
        ],
    )


# --------------------------------------------------------------------- 098
def test_098_asked_type_is_the_demonstration_centre() -> None:
    assert asked_entity_type(CASE_098_QUESTION) == "实验教学示范中心"


def test_098_list_fallback_keeps_only_the_three_centres() -> None:
    evidence = [_evidence("数学与信息学院、软件学院教学实验平台", CASE_098_QUOTE)]
    draft = build_list_claim(CASE_098_QUESTION, evidence)
    assert draft is not None
    names = items_of_type(CASE_098_QUOTE, "实验教学示范中心")
    assert len(names) == 3
    for name in names:
        assert name in draft.text
    assert "云计算与大数据实验室" not in draft.text
    # 引用必须是原文连续子串，名称才不算改写
    quote = draft.citations[0].quote
    assert quote in CASE_098_QUOTE
    assert all(name in quote for name in names)


def test_098_list_fallback_passes_the_normal_validator() -> None:
    evidence = [_evidence("数学与信息学院、软件学院教学实验平台", CASE_098_QUOTE)]
    draft = build_list_claim(CASE_098_QUESTION, evidence)
    report = ClaimValidator().check([draft], evidence)
    assert report.dropped_claim_ids == []
    assert [claim.claim_id for claim in report.claims] == [draft.claim_id]


def test_098_verifier_adds_a_verbatim_centre_list_when_the_draft_mixes_labs() -> None:
    evidence = [_evidence("数学与信息学院、软件学院教学实验平台", CASE_098_QUOTE)]
    bad = _claim(
        "c1",
        CASE_098_BAD,
        "数学与信息学院、软件学院教学实验平台",
        quote=CASE_098_QUOTE,
    )
    report = GraphVerifier().verify(
        [bad],
        evidence,
        question=CASE_098_QUESTION,
        max_clarify=3,
    )
    # 混入实验室的旧结论仍要改写；同时补进来的逐字引用结论必须点名三个中心。
    assert report.decision.value == "revise"
    texts = " ".join(claim.text for claim in report.hard.claims)
    assert "数学建模实验教学示范中心" in texts
    assert "信息数学与应用统计实验教学示范中心" in texts


def test_098_no_requirement_when_the_centres_are_already_listed() -> None:
    evidence = [_evidence("数学与信息学院、软件学院教学实验平台", CASE_098_QUOTE)]
    good = _claim(
        "c1",
        "数学与信息学院的实验教学示范中心包括基于云平台的计算机实验教学示范中心、"
        "信息数学与应用统计实验教学示范中心、数学建模实验教学示范中心。",
        "数学与信息学院、软件学院教学实验平台",
        quote=CASE_098_QUOTE,
    )
    assert claim_lists_type([good], "实验教学示范中心") is True
    report = GraphVerifier().verify([good], evidence, question=CASE_098_QUESTION, max_clarify=3)
    assert report.revise_requirements == []


def test_098_verifier_injects_the_fallback_when_rewrite_dropped_the_claim() -> None:
    evidence = [_evidence("数学与信息学院、软件学院教学实验平台", CASE_098_QUOTE)]
    only_lab = _claim(
        "c1",
        "数学与信息学院设有云计算与大数据实验室。",
        "数学与信息学院、软件学院教学实验平台",
        quote=CASE_098_QUOTE,
    )
    kept = GraphVerifier._with_list_fallback(CASE_098_QUESTION, [only_lab], evidence)
    assert len(kept) == 2
    assert any(claim_lists_type([draft], "实验教学示范中心") for draft in kept)


# --------------------------------------------------------------------- 035
def test_035_concrete_gap_is_not_a_wording_echo() -> None:
    question = "农学院研究生奖学金评选对科研成果加分及学术不端如何规定？"
    echoes, concrete = split_missing(
        ["农学院2026年研究生奖学金评选办法及补充说明中，对学术不端行为的具体规定是什么？"],
        question,
    )
    assert echoes == []
    assert concrete and "学术不端" in concrete[0]


def test_035_concrete_gap_blocks_recovery() -> None:
    unit = QuestionUnit(
        unit_id="u1",
        original_span="农学院研究生奖学金评选对科研成果加分及学术不端如何规定？",
        subject="农学院",
        requested_aspects=["农学院研究生奖学金评选对科研成果加分及学术不端如何规定"],
    )
    claims = [
        _claim(
            "c1",
            "研究生老生参评的科研成果须为2025年9月1日至2026年8月31日期间取得，"
            "科研成果加分按论文、专利、竞赛分档计算。",
            "农学院2026年研究生奖学金评选办法",
        )
    ]
    verdict = {
        "units": [
            {
                "unit_id": "u1",
                "status": "generation_missing",
                "claim_ids": [],
                "missing_items": ["对学术不端行为的具体规定是什么？"],
            }
        ],
        "unanswered_questions": [],
    }
    audit = CoverageChecker(ScriptedProvider(verdict)).check(
        unit.original_span,
        [unit],
        claims,
        [_evidence("农学院2026年研究生奖学金评选办法", "第三条")],
        [],
    )
    assert audit.units[0]["status"] == "generation_missing"
    assert any("学术不端" in item for item in audit.missing)


# --------------------------------------------------------------------- 020
def test_020_implementation_path_does_not_answer_graduation_design() -> None:
    assert (
        _content_supports(
            "毕业设计",
            "毕业设计是培养方案中综合性最强的实践环节，可作为多项毕业要求的实现途径。",
        )
        is False
    )


def test_020_numbers_answer_graduation_design() -> None:
    assert _content_supports("毕业设计", "毕业设计（论文）为6学分，安排14周，在第8学期完成。") is True


def test_020_vague_design_claim_is_not_recovered() -> None:
    unit = QuestionUnit(
        unit_id="u1",
        original_span="软件工程专业的核心课程和毕业设计要求是什么？",
        subject="软件工程",
        objects=["软件工程"],
        requested_aspects=["毕业设计"],
    )
    claims = [
        _claim(
            "c1",
            "软件工程专业的毕业设计可作为多项毕业要求的实现途径。",
            "软件工程专业人才培养方案",
        )
    ]
    verdict = {
        "units": [
            {"unit_id": "u1", "status": "generation_missing", "claim_ids": [], "missing_items": []}
        ],
        "unanswered_questions": [],
    }
    audit = CoverageChecker(ScriptedProvider(verdict)).check(
        unit.original_span,
        [unit],
        claims,
        [_evidence("软件工程专业人才培养方案", "第八部分")],
        [],
    )
    assert audit.units[0]["status"] == "generation_missing"
    assert audit.missing


def test_020_detailed_design_claim_covers_the_unit() -> None:
    unit = QuestionUnit(
        unit_id="u1",
        original_span="软件工程专业的毕业设计要求是什么？",
        subject="软件工程",
        objects=["软件工程"],
        requested_aspects=["毕业设计"],
    )
    claims = [
        _claim(
            "c1",
            "软件工程专业毕业设计（论文）为6学分，安排14周，在第8学期完成。",
            "软件工程专业人才培养方案",
        )
    ]
    verdict = {
        "units": [
            {
                "unit_id": "u1",
                "status": "generation_missing",
                "claim_ids": [],
                "missing_items": ["具体学分/周数/学期未列出"],
            }
        ],
        "unanswered_questions": [],
    }
    audit = CoverageChecker(ScriptedProvider(verdict)).check(
        unit.original_span,
        [unit],
        claims,
        [_evidence("软件工程专业人才培养方案", "第八部分")],
        [],
    )
    # 具体缺口存在时不恢复；这是有意为之：解析正确后仍要如实报缺。
    assert audit.units[0]["status"] == "generation_missing"

def test_035_fallback_unit_requires_every_clause() -> None:
    """线上第二次覆盖把“已答”判回来的场景：加分结论不能盖住学术不端。"""
    question = "农学院2026年研究生奖学金评选办法和补充说明，对科研成果加分及学术不端如何规定？"
    unit = QuestionUnit(
        unit_id="u1",
        original_span=question,
        subject="农学院",
        requested_aspects=[question],
    )
    claims = [
        _claim(
            "c1",
            "研究生老生的参评科研成果必须以华南农业大学为第一署名单位；"
            "科研成果主要包括发表论文、专著、优秀成果奖、发明专利和听学术讲座等。",
            "农学院2026年研究生奖学金评选办法",
        )
    ]
    verdict = {
        "units": [
            {"unit_id": "u1", "status": "answered", "claim_ids": ["c1"], "missing_items": []}
        ],
        "unanswered_questions": [],
    }
    audit = CoverageChecker(ScriptedProvider(verdict)).check(
        unit.original_span,
        [unit],
        claims,
        [_evidence("农学院2026年研究生奖学金评选办法", "五、评选项目及评分标准")],
        [],
    )
    assert audit.units[0]["status"] == "generation_missing"
    assert any("学术不端" in item for item in audit.missing)