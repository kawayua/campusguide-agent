"""Production wiring for structured action/result asks in clause coverage."""

from __future__ import annotations

import json
from datetime import date
from types import SimpleNamespace

from campusguide.agents.coverage import (
    CoverageChecker,
    _property_supported,
    _question_clauses,
    _requested_properties,
)
from campusguide.agents.plan import QuestionUnit
from campusguide.domain.models import Citation, Claim

AUTHORITY_QUESTION = (
    "本科生请假3天、4至14天和15天以上分别由谁审批"
)
SELECTION_QUESTION = (
    "已选课却不参加学习考核，以及没选课自行听课再考核，成绩分别怎么处理？"
)
APPEAL_QUESTION = (
    "本科生不服退学处理，向校内委员会申诉、校内复查、再向省教育厅申诉的期限各是多少？"
)
PROGRAM_QUESTION = (
    "主修毕业或结业离校时，微专业学习还能继续吗？没修满微专业学分能得到什么？"
)
COUNT_QUESTION = "甲材料及乙证明材料各需几份？"


class _PessimisticProvider:
    """Model says every unit is missing so only deterministic coverage can recover it."""

    def complete(self, messages, temperature=0, max_tokens=2400):
        payload = {
            "units": [
                {
                    "unit_id": "u1",
                    "status": "generation_missing",
                    "claim_ids": [],
                    "missing_items": [],
                }
            ],
            "unanswered_questions": [],
        }
        return SimpleNamespace(
            content=json.dumps(payload, ensure_ascii=False),
            prompt_tokens=0,
            completion_tokens=0,
            latency_ms=0,
        )


def _claim(text: str, claim_id: str, doc_title: str = "学生手册") -> Claim:
    return Claim(
        claim_id=claim_id,
        text=text,
        citations=[
            Citation(
                article_id=f"a-{claim_id}",
                quote=text,
                quote_start=0,
                quote_end=len(text),
                doc_title=doc_title,
                article_no="第一条",
                effective_from=date(2025, 9, 1),
                source_url="https://example.edu/rule",
            )
        ],
    )


def _unit(question: str, **overrides) -> QuestionUnit:
    data = {
        "unit_id": "u1",
        "original_span": question,
        "requested_aspects": [question.strip("？?。 ")],
    }
    data.update(overrides)
    return QuestionUnit(**data)


def _audit(
    question: str,
    claims: list[str],
    *,
    unit: QuestionUnit | None = None,
    profile_lexicon=None,
    doc_title: str = "学生手册",
):
    checker = CoverageChecker(
        _PessimisticProvider(), profile_lexicon=profile_lexicon
    )
    return checker.check(
        question,
        [unit or _unit(question)],
        [_claim(text, f"c{index}", doc_title=doc_title) for index, text in enumerate(claims, 1)],
        [],
        [],
    )


def test_question_clauses_keep_threshold_branches_and_shared_agency() -> None:
    clauses, topics = _question_clauses(AUTHORITY_QUESTION, ())
    assert not topics
    assert len(clauses) == 3
    assert all("由谁审批" in clause for clause in clauses)
    assert all("本科生请假" in clause for clause in clauses)


def test_question_clauses_attach_deadline_to_each_appeal_stage() -> None:
    clauses, topics = _question_clauses(APPEAL_QUESTION, ())
    assert topics == ["本科生不服退学处理"]
    assert len(clauses) == 3
    assert all("期限" in clause for clause in clauses)
    assert any("校内委员会" in clause for clause in clauses)
    assert any("省教育厅" in clause for clause in clauses)


def test_question_clauses_do_not_merge_two_program_questions() -> None:
    clauses, _ = _question_clauses(PROGRAM_QUESTION, ())
    assert len(clauses) == 2
    assert any("还能继续" in clause for clause in clauses)
    assert any("没修满" in clause for clause in clauses)
    assert not any(clause.strip() == "主修毕业或结业离校时" for clause in clauses)


def test_question_clauses_keep_both_selection_states() -> None:
    clauses, topics = _question_clauses(SELECTION_QUESTION, ())
    assert not topics
    assert len(clauses) == 2
    assert any("已选课" in clause for clause in clauses)
    assert any("没选课" in clause for clause in clauses)
    assert not any(clause.strip() == "成绩分别怎么处理" for clause in clauses)


def test_authority_thresholds_recover_from_pessimistic_model() -> None:
    audit = _audit(
        AUTHORITY_QUESTION,
        [
            "本科生请假3天以内由辅导员审批。",
            "本科生请假4至14天由学院审批。",
            "本科生请假15天以上由学校审批。",
        ],
    )
    assert audit.units[0]["status"] == "answered"
    assert audit.units[0]["claim_ids"]


def test_authority_wrong_and_partial_branches_do_not_borrow() -> None:
    wrong = _audit(
        AUTHORITY_QUESTION,
        [
            "本科生请假3天以内由辅导员审批。",
            "本科生请假30天以内由学院审批。",
        ],
    )
    assert wrong.units[0]["status"] != "answered"

    partial = _audit(
        AUTHORITY_QUESTION,
        [
            "请假4至14天由学院审批。",
            "请假15天以上由学校审批。",
        ],
    )
    assert partial.units[0]["status"] != "answered"


def test_authority_point_request_requires_the_matching_unit() -> None:
    audit = _audit(
        AUTHORITY_QUESTION,
        [
            "本科生请假3个工作日以内由辅导员审批。",
            "本科生请假4至14天由学院审批。",
            "本科生请假15天以上由学校审批。",
        ],
    )
    assert audit.units[0]["status"] != "answered"


def test_selection_states_need_their_own_grade_result() -> None:
    audit = _audit(
        SELECTION_QUESTION,
        [
            "已选课程不参加考核的，成绩记0分。",
            "未办理选课手续自行听课的，不予登记成绩。",
        ],
    )
    assert audit.units[0]["status"] == "answered"

    only_selected = _audit(
        SELECTION_QUESTION,
        ["已选课程不参加考核的，成绩记0分。"],
    )
    assert only_selected.units[0]["status"] != "answered"

    unselected_without_grade = _audit(
        SELECTION_QUESTION,
        [
            "已选课程不参加考核的，成绩记0分。",
            "未办理选课手续自行听课的，考核由学校安排。",
        ],
    )
    assert unselected_without_grade.units[0]["status"] != "answered"


def test_legacy_selection_predicate_accepts_negative_handling_enrolment() -> None:
    properties = _requested_properties("没选课自行听课，成绩怎么处理？", ())
    unselected = [prop for prop in properties if prop.polarity == "unselected"]
    assert len(unselected) == 1
    assert _property_supported(
        unselected[0], "未按规定办理选课手续自行听课的，不予登记成绩。"
    )
    assert _property_supported(
        unselected[0], "未办理选修手续自行听课的，不予登记成绩。"
    )
    assert not _property_supported(
        unselected[0], "未完成选修课程学习的，成绩记0分。"
    )
    assert not _property_supported(unselected[0], "未提供选修课程名单。")


def test_appeal_stages_each_need_their_own_deadline() -> None:
    audit = _audit(
        APPEAL_QUESTION,
        [
            "本科学生不服退学处理，收到退学决定书7日内向学校学生申诉处理委员会书面申诉。",
            "委员会接到申诉11日内作出复查。",
            "复查决定书送达13日内可向省教育厅申诉。",
        ],
        doc_title="本科学生不服退学处理的申诉办法",
    )
    assert audit.units[0]["status"] == "answered"

    missing_province = _audit(
        APPEAL_QUESTION,
        [
            "本科学生不服退学处理，收到退学决定书7日内向学校学生申诉处理委员会书面申诉。",
            "委员会接到申诉11日内作出复查。",
        ],
        doc_title="本科学生不服退学处理的申诉办法",
    )
    assert missing_province.units[0]["status"] != "answered"


def test_a_trusted_title_cannot_supply_the_missing_deadline() -> None:
    audit = _audit(
        APPEAL_QUESTION,
        [
            "本科学生不服退学处理，向学校学生申诉处理委员会书面申诉。",
            "委员会接到申诉作出复查。",
            "可向省教育厅申诉。",
        ],
    )
    assert audit.units[0]["status"] != "answered"


def test_program_continuation_and_incomplete_outcome_are_both_required() -> None:
    audit = _audit(
        PROGRAM_QUESTION,
        [
            "主修毕业或结业离校时，微专业学习同时终止。",
            "微专业未修满学分的，只提供修读成绩单。",
        ],
    )
    assert audit.units[0]["status"] == "answered"

    continuation_only = _audit(
        PROGRAM_QUESTION,
        ["主修毕业或结业离校时，微专业学习同时终止。"],
    )
    assert continuation_only.units[0]["status"] != "answered"

    completed_certificate = _audit(
        PROGRAM_QUESTION,
        [
            "主修毕业或结业离校时，微专业学习同时终止。",
            "微专业合格后发结业证书。",
        ],
    )
    assert completed_certificate.units[0]["status"] != "answered"


def test_one_shared_count_ask_splits_into_each_business_head() -> None:
    properties = [
        prop
        for prop in _requested_properties(COUNT_QUESTION, ())
        if prop.kind == "numeric"
    ]
    assert len(properties) == 2
    assert {prop.core for prop in properties} == {"甲材料", "乙证明材料"}

    audit = _audit(
        COUNT_QUESTION,
        ["甲材料一式6份，乙证明材料一式6份。"],
    )
    assert audit.units[0]["status"] == "answered"

    missing = _audit(COUNT_QUESTION, ["甲材料一式6份。"])
    assert missing.units[0]["status"] != "answered"


def test_wrong_version_claim_cannot_support_structured_branches() -> None:
    unit = _unit(AUTHORITY_QUESTION, version="2025级")
    audit = _audit(
        AUTHORITY_QUESTION,
        [
            "本科生请假3天以内由辅导员审批（2024级）。",
            "本科生请假4至14天由学院审批（2024级）。",
            "本科生请假15天以上由学校审批（2024级）。",
        ],
        unit=unit,
    )
    assert audit.units[0]["status"] != "answered"


def test_wrong_class_claim_cannot_support_structured_branches() -> None:
    profile = SimpleNamespace(
        class_terms={"普通班": ("普通班",), "创新班": ("创新班",)},
        college_terms={},
        major_terms={},
    )
    unit = _unit(SELECTION_QUESTION, class_type="普通班")
    audit = _audit(
        SELECTION_QUESTION,
        [
            "创新班：已选课程不参加考核的，成绩记0分。",
            "创新班：未办理选课手续自行听课的，不予登记成绩。",
        ],
        unit=unit,
        profile_lexicon=profile,
    )
    assert audit.units[0]["status"] != "answered"


def test_broad_handbook_title_cannot_lend_withdrawal_scope_to_other_claims() -> None:
    # The follow-up claims lack the withdrawal scope and cite only a broad
    # handbook. A matching first claim cannot authorize unrelated later facts.
    audit = _audit(
        APPEAL_QUESTION,
        [
            "本科学生不服退学处理，收到退学决定书7日内向学校学生申诉处理委员会书面申诉。",
            "委员会接到申诉11日内作出复查。",
            "复查决定书送达13日内可向省教育厅申诉。",
        ],
    )
    assert audit.units[0]["status"] != "answered"
