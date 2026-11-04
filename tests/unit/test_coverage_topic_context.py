"""Covered raw values and comma-prefix topic context (synthetic only)."""

from __future__ import annotations

import json
from datetime import date

from campusguide.agents.coverage import (
    CoverageChecker,
    _claim_supports_unit,
    _question_clauses,
    _topic_covered,
)
from campusguide.agents.plan import QuestionUnit
from campusguide.agents.providers import FakeChatProvider
from campusguide.domain.enums import BlockType
from campusguide.domain.models import Article, CellProvenance, Citation, Claim
from campusguide.retrieval.source_spans import structured_table_row
from tests.w2_helpers import make_article

_COURSE_PATHS: tuple[tuple[str, ...], ...] = (
    ("课程代码",),
    ("课程名称",),
    ("学分",),
    ("学时", "总数"),
    ("学时", "理论"),
    ("学时", "实验"),
    ("学时", "实习"),
    ("修读学期",),
    ("备注甲",),
    ("备注乙",),
)


def _course_article(covered_value: str) -> Article:
    values = ["123456", "测试课程", "3", "48", "32", "0", "16", "1", "真实说明", covered_value]
    text = "表头：" + " | ".join(" > ".join(path) for path in _COURSE_PATHS)
    text += "\n行：" + " | ".join(values)
    cells = [
        CellProvenance(
            cell_id=f"t1#r1c{index}",
            row=1,
            col=index,
            text=value,
            origin_row=1,
            origin_col=index,
            colspan=2 if index == 8 else 1,
            text_source="native",
        )
        for index, value in enumerate(values[:9])
    ]
    cells.append(
        CellProvenance(
            cell_id="t1#r1c9",
            row=1,
            col=9,
            text="",
            origin_row=1,
            origin_col=8,
            covered=True,
        )
    )
    article = make_article(
        "plan@v1#t1.row1",
        doc_id="plan",
        version_id="v1",
        doc_title="测试培养方案",
        article_no="表格1第2行",
        block_type=BlockType.SECTION,
        text=text,
    )
    return article.model_copy(
        update={
            "block_kind": "table_row",
            "table_id": "t1",
            "parent_table_id": "t1",
            "parent_article_id": "plan@v1#t1",
            "row_id": "t1-r1",
            "row_index": 1,
            "table_headers": [" > ".join(path) for path in _COURSE_PATHS],
            "header_paths": [list(path) for path in _COURSE_PATHS],
            "cell_provenance": cells,
        }
    )


def _member_article(role_value: str, source_ids: list[str]) -> Article:
    paths = (("类别",), ("姓名",), ("备注",))
    values = [role_value, "钱二", ""]
    text = "表头：" + " | ".join(" > ".join(path) for path in paths)
    text += "\n行：" + " | ".join(values)
    cells = [
        CellProvenance(
            cell_id="t1#r2c0",
            row=2,
            col=0,
            text="",
            origin_row=1,
            origin_col=0,
            covered=True,
            text_source="native",
        ),
        CellProvenance(
            cell_id="t1#r2c1",
            row=2,
            col=1,
            text="钱二",
            origin_row=2,
            origin_col=1,
            text_source="native",
        ),
        CellProvenance(
            cell_id="t1#r2c2",
            row=2,
            col=2,
            text="",
            origin_row=2,
            origin_col=2,
            text_source="native",
        ),
    ]
    article = make_article(
        "award@v1#t1.row2",
        doc_id="award",
        version_id="v1",
        doc_title="甲学院评优办法",
        article_no="表格1第3行",
        block_type=BlockType.SECTION,
        text=text,
    )
    return article.model_copy(
        update={
            "block_kind": "table_row",
            "table_id": "t1",
            "parent_table_id": "t1",
            "parent_article_id": "award@v1#t1",
            "row_id": "t1-r2",
            "row_index": 2,
            "table_headers": [" > ".join(path) for path in paths],
            "header_paths": [list(path) for path in paths],
            "cell_provenance": cells,
            "source_cell_ids": source_ids,
        }
    )


def test_horizontal_covered_empty_placeholder_stays_reliable() -> None:
    row = structured_table_row(_course_article(""))
    assert row is not None and row.reliable, row.problems if row else ()
    assert row.value("credits") == "3"
    assert row.value("total_hours") == "48"


def test_horizontal_covered_raw_value_is_rejected() -> None:
    for value in ("999", "新值"):
        row = structured_table_row(_course_article(value))
        assert row is not None and not row.reliable
        assert any(problem.startswith("COVERED_CELL_HAS_VALUE") for problem in row.problems)


def test_vertical_inherited_label_is_context_not_a_value() -> None:
    row = structured_table_row(_member_article("审查委员", []))
    assert row is not None and row.reliable, row.problems if row else ()
    assert row.values[0] == ""
    assert row.raw_values[0] == ""


def test_vertical_inherited_label_needs_a_traceable_origin() -> None:
    traced = structured_table_row(_member_article("审查委员", ["t1#r1c0"]))
    assert traced is not None and traced.reliable, traced.problems if traced else ()
    untraced = structured_table_row(_member_article("审查委员", ["t1#r9c9"]))
    assert untraced is not None and not untraced.reliable
    assert any(problem.startswith("COVERED_CELL_HAS_VALUE") for problem in untraced.problems)


def test_vertical_covered_numeric_value_is_rejected() -> None:
    row = structured_table_row(_member_article("1234", []))
    assert row is not None and not row.reliable
    assert any(problem.startswith("COVERED_CELL_HAS_VALUE") for problem in row.problems)


_TOPIC_QUESTION = "乙学院2026年度综合评议，学生自评与班级评议分别安排在哪些日期？"
_TOPIC_TITLE = "乙学院2026年度综合评议工作方案"


def _citation(title: str) -> Citation:
    quote = "学生自评与班级评议的时间安排见正文。"
    return Citation(
        article_id=f"{title}#二、评议安排",
        quote=quote,
        quote_start=0,
        quote_end=len(quote),
        doc_title=title,
        article_no="二、评议安排",
        effective_from=date(2026, 1, 1),
        source_url="https://example.edu.cn/x",
    )


def _claim(text: str, title: str = _TOPIC_TITLE) -> Claim:
    return Claim(claim_id="c1", text=text, citations=[_citation(title)])


def _unit(question: str, *, subject: str = "乙学院") -> QuestionUnit:
    return QuestionUnit(
        unit_id="u1",
        original_span=question,
        subject=subject,
        requested_aspects=[question],
    )


def _audit(unit: QuestionUnit, claim: Claim, *, status: str):
    verdict = {
        "units": [
            {
                "unit_id": unit.unit_id,
                "status": status,
                "claim_ids": [claim.claim_id] if status == "answered" else [],
                "missing_items": [],
            }
        ],
        "unanswered_questions": [],
    }
    provider = FakeChatProvider([json.dumps(verdict, ensure_ascii=False)])
    return CoverageChecker(provider).check(unit.original_span, [unit], [claim], [], [])


def test_topic_prefix_with_two_stage_answer_is_answered() -> None:
    unit = _unit(_TOPIC_QUESTION)
    claim = _claim("学生自评安排在2026年9月1日，班级评议安排在2026年9月3日。")
    assert _claim_supports_unit(unit, claim)
    audit = _audit(unit, claim, status="generation_missing")
    assert audit.units[0]["status"] == "answered"
    assert audit.units[0]["claim_ids"] == ["c1"]
    assert audit.missing == []


def test_topic_prefix_does_not_become_a_missing_aspect() -> None:
    clauses, topics = _question_clauses(_TOPIC_QUESTION)
    assert topics == ["乙学院2026年度综合评议"]
    assert clauses == ["学生自评日期", "班级评议日期"]


def test_missing_one_stage_stays_missing() -> None:
    unit = _unit(_TOPIC_QUESTION)
    claim = _claim("学生自评安排在2026年9月1日。")
    audit = _audit(unit, claim, status="answered")
    assert audit.units[0]["status"] == "generation_missing"
    assert any("班级评议" in item for item in audit.missing)
    assert all("综合评议" not in item for item in audit.missing)


def test_topic_wrong_version_is_not_answered() -> None:
    unit = _unit(_TOPIC_QUESTION)
    wrong = _claim(
        "学生自评安排在2025年9月1日，班级评议安排在2025年9月3日。",
        title="乙学院2025年度综合评议工作方案",
    )
    assert not _claim_supports_unit(unit, wrong)
    mixed = _claim(
        "学生自评安排在2025年9月1日，班级评议安排在2025年9月3日。",
        title=_TOPIC_TITLE,
    )
    assert not _claim_supports_unit(unit, mixed)
    assert _audit(unit, wrong, status="answered").units[0]["status"] == "generation_missing"


def test_topic_wrong_subject_is_not_answered() -> None:
    unit = _unit(_TOPIC_QUESTION, subject="")
    claim = _claim(
        "学生自评安排在2026年9月1日，班级评议安排在2026年9月3日。",
        title="甲学院2026年度综合评议工作方案",
    )
    assert not _claim_supports_unit(unit, claim)


def test_topic_detection_is_not_tied_to_one_institution() -> None:
    question = "丙学院2026年度综合评议，学生自评与班级评议分别安排在哪些日期？"
    unit = _unit(question, subject="丙学院")
    claim = _claim(
        "学生自评安排在2026年10月8日，班级评议安排在2026年10月9日。",
        title="丙学院2026年度综合评议工作方案",
    )
    assert _claim_supports_unit(unit, claim)


def test_credits_and_hours_remain_separate_aspects() -> None:
    question = "农学专业毕业学分和学时分别要求多少？"
    clauses, topics = _question_clauses(question)
    assert topics == []
    assert clauses == ["农学专业毕业学分", "学时"]
    unit = _unit(question, subject="农学")
    partial = _claim("农学专业毕业学分为160学分。", title="农学专业人才培养方案")
    assert not _claim_supports_unit(unit, partial)
    audit = _audit(unit, partial, status="answered")
    assert audit.units[0]["status"] == "generation_missing"
    assert any("学时" in item for item in audit.missing)
    full = _claim(
        "农学专业毕业学分为160学分，总学时为2800学时。",
        title="农学专业人才培养方案",
    )
    assert _claim_supports_unit(unit, full)


def test_doctoral_and_master_remain_separate_aspects() -> None:
    question = "博士生和硕士生分别要求什么？"
    clauses, topics = _question_clauses(question)
    assert topics == []
    assert clauses == ["博士生", "硕士生"]
    unit = _unit(question, subject="")
    claim = _claim("博士研究生的研究成果要求如下。", title="研究生管理办法")
    assert not _claim_supports_unit(unit, claim)


def test_topic_title_cannot_supply_the_asked_date() -> None:
    unit = _unit(_TOPIC_QUESTION)
    claim = _claim(
        "学生自评和班级评议均已安排。",
        title="乙学院2026年度综合评议工作方案（时间安排）",
    )
    assert not _claim_supports_unit(unit, claim)
    assert _audit(unit, claim, status="answered").units[0]["status"] == "generation_missing"


def test_two_date_questions_keep_both_branches() -> None:
    question = "学生自评有哪些日期，班级评议有哪些日期？"
    clauses, topics = _question_clauses(question)
    assert topics == []
    assert clauses == ["学生自评日期", "班级评议日期"]
    unit = _unit(question, subject="")
    claim = _claim("学生自评的日期为2026年9月1日。", title="综合评议工作安排")
    assert not _claim_supports_unit(unit, claim)


_DISSENT_TOPIC = "丁学院本科生不服退学处理"
_DISSENT_TITLE = "丁学院本科学生学籍管理实施细则"
_DISSENT_BODY = (
    "学生对退学处理有异议的，向学院学生申诉处理委员会提出书面申诉的期限"
    "为退学决定书送达之日起十个工作日。"
)


def test_dissent_topic_matches_paraphrased_decision_context() -> None:
    claim = _claim(_DISSENT_BODY, title=_DISSENT_TITLE)
    for topic in (
        _DISSENT_TOPIC,
        "丁学院本科生对退学处理不服",
        "丁学院本科生对退学处理有异议",
    ):
        assert _topic_covered(topic, claim), topic


def test_dissent_topic_does_not_borrow_another_decision_object() -> None:
    claim = _claim(
        "学生对休学处理有异议的，向学院学生申诉处理委员会提出书面申诉的期限"
        "为休学决定书送达之日起十个工作日。",
        title=_DISSENT_TITLE,
    )
    assert not _topic_covered(_DISSENT_TOPIC, claim)


def test_dissent_topic_does_not_borrow_another_discipline() -> None:
    claim = _claim(
        "工学专业学生对退学处理有异议的，向学院学生申诉处理委员会提出书面申诉"
        "的期限为退学决定书送达之日起十个工作日。",
        title=_DISSENT_TITLE,
    )
    assert not _topic_covered("丁学院农学专业本科生不服退学处理", claim)


def test_dissent_topic_keeps_institution_and_year_scope() -> None:
    topic = "丁学院2026年本科生不服退学处理"
    right = _claim(_DISSENT_BODY, title="丁学院2026年本科学生学籍管理实施细则")
    wrong_institution = _claim(
        _DISSENT_BODY, title="戊学院2026年本科学生学籍管理实施细则"
    )
    wrong_year = _claim(
        _DISSENT_BODY, title="丁学院2025年本科学生学籍管理实施细则"
    )
    assert _topic_covered(topic, right)
    assert not _topic_covered(topic, wrong_institution)
    assert not _topic_covered(topic, wrong_year)
