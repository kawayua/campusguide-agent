"""Coverage recovery: real 098/001/039 wording must not be reported as missing."""

from __future__ import annotations

import json
from datetime import date

from campusguide.agents.coverage import CoverageChecker
from campusguide.agents.plan import QuestionUnit
from campusguide.domain.models import Citation, Claim, Evidence


class ScriptedProvider:
    """Returns a fixed coverage verdict, mimicking the live model's junk output."""

    def __init__(self, verdict: dict) -> None:
        self._verdict = verdict

    def complete(self, messages, *, temperature=0.2, max_tokens=1200):
        class Response:
            content = json.dumps(self._verdict, ensure_ascii=False)
            prompt_tokens = 100
            completion_tokens = 50
            latency_ms = 10

        return Response()


def _claim(claim_id: str, text: str, doc_title: str, article_no: str = "第一条") -> Claim:
    return Claim(
        claim_id=claim_id,
        text=text,
        citations=[
            Citation(
                article_id=f"{doc_title}#1",
                quote=text,
                quote_start=0,
                quote_end=len(text),
                doc_title=doc_title,
                article_no=article_no,
                effective_from=date(2025, 9, 1),
                source_url="https://www.scau.edu.cn/x.htm",
            )
        ],
    )


def _evidence(title: str, text: str, article_no: str = "第一条") -> Evidence:
    from tests.w2_helpers import make_article, make_evidence

    return make_evidence(make_article(f"{title}#1", doc_title=title, text=text), score=0.9)


def test_098_question_echo_is_not_a_missing_item() -> None:
    unit = QuestionUnit(
        unit_id="u1",
        original_span="数学与信息学院有哪些实验教学示范中心，是否有云计算与大数据实验室？",
        subject="数学与信息学院",
        objects=["数学与信息学院"],
        requested_aspects=["实验教学示范中心"],
    )
    claims = [
        _claim(
            "c1",
            "数学与信息学院建设有基于云平台的计算机实验教学示范中心、信息数学与应用统计"
            "实验教学示范中心、数学建模实验教学示范中心。",
            "数学与信息学院、软件学院教学实验平台",
        ),
        _claim(
            "c2",
            "数学与信息学院设有云计算与大数据实验室。",
            "数学与信息学院、软件学院教学实验平台",
        ),
    ]
    verdict = {
        "units": [
            {
                "unit_id": "u1",
                "status": "generation_missing",
                "claim_ids": [],
                "missing_items": ["数学与信息学院 数学与信息学院有哪些实验教学示范中心，是否有云计算与大数据实验室"],
            }
        ],
        "unanswered_questions": [],
    }
    audit = CoverageChecker(ScriptedProvider(verdict)).check(
        unit.original_span, [unit], claims, [_evidence("数学与信息学院、软件学院教学实验平台", "示范中心")], []
    )
    assert audit.units[0]["status"] == "answered"
    assert set(audit.units[0]["claim_ids"]) == {"c1"}
    assert audit.missing == []


def test_generic_claim_still_counts_as_missing() -> None:
    unit = QuestionUnit(
        unit_id="u1",
        original_span="两个专业均设置了哪些核心课程？",
        subject="软件工程",
        objects=["软件工程"],
        requested_aspects=["核心课程"],
    )
    claims = [_claim("c1", "两个专业均设置了核心课程。", "培养方案")]
    verdict = {
        "units": [
            {"unit_id": "u1", "status": "generation_missing", "claim_ids": [], "missing_items": []}
        ],
        "unanswered_questions": [],
    }
    audit = CoverageChecker(ScriptedProvider(verdict)).check(
        unit.original_span, [unit], claims, [_evidence("培养方案", "核心课程")], []
    )
    assert audit.units[0]["status"] == "generation_missing"


def test_junk_fragments_are_dropped_from_missing_items() -> None:
    unit = QuestionUnit(
        unit_id="u1",
        original_span="计算机科学与技术专业2022版和2024版的核心课程有哪些变化？",
        subject="计算机科学与技术",
        objects=["计算机科学与技术"],
        requested_aspects=["核心课程"],
    )
    claims = [
        _claim(
            "c1",
            "计算机科学与技术专业2024版核心课程包括：计算机科学概论、数据结构、数据库系统、"
            "面向对象程序设计、计算机网络。",
            "计算机科学与技术专业人才培养方案",
        )
    ]
    verdict = {
        "units": [
            {
                "unit_id": "u1",
                "status": "generation_missing",
                "claim_ids": [],
                "missing_items": ["重要", "）数据结构", "计算机科学与技术"],
            }
        ],
        "unanswered_questions": [],
    }
    audit = CoverageChecker(ScriptedProvider(verdict)).check(
        unit.original_span,
        [unit],
        claims,
        [_evidence("计算机科学与技术专业人才培养方案", "核心课程")],
        [],
    )
    assert audit.units[0]["status"] == "answered"
    assert audit.missing == []


def test_039_definitions_count_as_answered() -> None:
    unit = QuestionUnit(
        unit_id="u1",
        original_span="微专业与辅修专业有什么区别？",
        subject="微专业",
        objects=["微专业", "辅修专业"],
        requested_aspects=["定义区别"],
    )
    claims = [
        _claim(
            "c1",
            "微专业是指为满足学生个性化和多样化发展需求，围绕某一特定领域开设的一组核心课程。"
            "辅修专业是指学有余力的本科生在修读主修专业的基础上，修读与主修专业不同的本科专业课程。",
            "华南农业大学微专业建设管理办法（试行）",
        )
    ]
    verdict = {
        "units": [
            {
                "unit_id": "u1",
                "status": "generation_missing",
                "claim_ids": [],
                "missing_items": ["微专业 定义区别"],
            }
        ],
        "unanswered_questions": [],
    }
    audit = CoverageChecker(ScriptedProvider(verdict)).check(
        unit.original_span,
        [unit],
        claims,
        [_evidence("华南农业大学微专业建设管理办法（试行）", "第二条")],
        [],
    )
    assert audit.units[0]["status"] == "answered"
    assert audit.units[0]["claim_ids"] == ["c1"]


def test_question_aspect_fallback_is_covered_by_a_cited_claim() -> None:
    unit = QuestionUnit(
        unit_id="u1",
        original_span="考试迟到、携带手机或者替考，分别涉及哪些考场规定和纪律处分？",
        subject="",
        requested_aspects=["考试迟到、携带手机或者替考，分别涉及哪些考场规定和纪律处分"],
    )
    claims = [
        _claim(
            "c1",
            "学生迟到30分钟者，不得参加该课程考试并作缺考论；携带手机等电子设备带至考座的，"
            "给予警告处分；代替他人参加考试的，给予开除学籍处分。",
            "华南农业大学本科学生考场工作细则",
        )
    ]
    verdict = {
        "units": [
            {
                "unit_id": "u1",
                "status": "generation_missing",
                "claim_ids": [],
                "missing_items": [
                    "考试迟到、携带手机或者替考，分别涉及哪些考场规定和纪律处分"
                ],
            }
        ],
        "unanswered_questions": [],
    }
    audit = CoverageChecker(ScriptedProvider(verdict)).check(
        unit.original_span,
        [unit],
        claims,
        [_evidence("华南农业大学本科学生考场工作细则", "第二十四条")],
        [],
    )
    assert audit.units[0]["status"] == "answered"
    assert audit.units[0]["claim_ids"] == ["c1"]