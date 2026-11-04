"""Semantic sub-question coverage: amounts, member roles, actions, document scope."""

from __future__ import annotations

import json
from datetime import date

from campusguide.agents.coverage import CoverageChecker
from campusguide.agents.plan import QuestionUnit
from campusguide.agents.providers import FakeChatProvider
from campusguide.domain.enums import BlockType
from campusguide.domain.models import Article, CellProvenance, Citation, Claim
from campusguide.retrieval.profile_expand import ProfileLexicon
from tests.w2_helpers import make_article, make_evidence


def _verdict(status: str, claim_ids: list[str] | None = None) -> str:
    return json.dumps(
        {
            "units": [
                {
                    "unit_id": "u1",
                    "status": status,
                    "claim_ids": claim_ids or [],
                    "missing_items": [],
                }
            ],
            "unanswered_questions": [],
        },
        ensure_ascii=False,
    )


def _unit(
    span: str,
    *,
    subject: str = "",
    objects: list[str] | None = None,
    version: str | None = None,
    aspects: list[str] | None = None,
) -> QuestionUnit:
    return QuestionUnit(
        unit_id="u1",
        original_span=span,
        subject=subject,
        objects=[subject] if objects is None and subject else list(objects or []),
        version=version,
        requested_aspects=aspects if aspects is not None else [span.strip("？?。 ")],
    )


def _claim(text: str, *, title: str, no: str = "", claim_id: str = "c1") -> Claim:
    citation = Citation(
        article_id=f"{title}#{no or '正文'}",
        part_no=1,
        quote=text,
        quote_start=0,
        quote_end=len(text),
        doc_title=title,
        article_no=no,
        effective_from=date(2024, 1, 1),
        source_url="https://example.edu.cn/synthetic",
    )
    return Claim(claim_id=claim_id, text=text, citations=[citation])


def _audit(
    unit: QuestionUnit,
    claim: Claim,
    *,
    evidence: list | None = None,
    status: str = "generation_missing",
    lexicon: ProfileLexicon | None = None,
):
    ids = [claim.claim_id] if status == "answered" else []
    checker = CoverageChecker(
        FakeChatProvider([_verdict(status, ids)]), profile_lexicon=lexicon
    )
    return checker.check(
        unit.original_span, [unit], [claim], list(evidence or []), []
    )


# ---------- A: per-person-per-year standards -------------------------------

_AWARD_QUESTION = "某项年度学业奖励，博士生和硕士生的每人每年奖励标准各是多少？"


def test_yearly_standard_answers_both_degree_levels() -> None:
    unit = _unit(_AWARD_QUESTION)
    claim = _claim(
        "博士研究生一等奖9000元/人·年，硕士研究生一等奖6000元/人·年。",
        title="年度学业奖励评定办法",
        no="奖励标准",
    )
    audit = _audit(unit, claim)
    assert audit.units[0]["status"] == "answered"
    assert audit.units[0]["claim_ids"] == ["c1"]
    assert audit.missing == []


def test_monthly_standard_does_not_answer_yearly_amount() -> None:
    unit = _unit(_AWARD_QUESTION)
    claim = _claim(
        "博士研究生一等奖9000元/人·月，硕士研究生一等奖600元/人·月。",
        title="年度学业奖励评定办法",
        no="奖励标准",
    )
    audit = _audit(unit, claim, status="answered")
    assert audit.units[0]["status"] == "generation_missing"


def test_one_degree_level_missing_keeps_gap() -> None:
    unit = _unit(_AWARD_QUESTION)
    claim = _claim(
        "博士研究生一等奖9000元/人·年。",
        title="年度学业奖励评定办法",
        no="奖励标准",
    )
    audit = _audit(unit, claim, status="answered")
    assert audit.units[0]["status"] == "generation_missing"
    assert any("硕士" in item for item in audit.missing)


def test_title_amount_cannot_fill_the_claim_body() -> None:
    unit = _unit(_AWARD_QUESTION)
    claim = _claim(
        "博士研究生和硕士研究生一等奖均已评定。",
        title="年度学业奖励评定办法（9000元、6000元/人·年）",
        no="奖励标准",
    )
    audit = _audit(unit, claim, status="answered")
    assert audit.units[0]["status"] == "generation_missing"
    assert audit.missing


# ---------- B: role-bound reliable member lists ----------------------------

_MEMBER_QUESTION = "乙学院委员会的联络委员是谁？"
_MEMBER_TITLE = "乙学院委员会联络委员名单"
_HEADERS = ("分工", "姓名", "所在单位", "备注")
_PATHS = (("分工",), ("姓名",), ("所在单位",), ("备注",))


def _cell(table_id: str, row_index: int, col: int, text: str) -> CellProvenance:
    return CellProvenance(
        cell_id=f"{table_id}-{row_index}-{col}",
        row=row_index,
        col=col,
        text=text,
        origin_row=row_index,
        origin_col=col,
        covered=False,
        text_source="native",
    )


def _member_row(
    row_index: int,
    role: str,
    name: str,
    *,
    title: str,
    table_id: str = "t1",
) -> Article:
    text = (
        "表头："
        + " | ".join(_HEADERS)
        + "\n行："
        + " | ".join([role, name, "乙学院", ""])
    )
    cells = [
        _cell(table_id, row_index, 0, role),
        _cell(table_id, row_index, 1, name),
        _cell(table_id, row_index, 2, "乙学院"),
        _cell(table_id, row_index, 3, ""),
    ]
    article = make_article(
        f"award@v2024#{table_id}.row{row_index}",
        doc_id="award",
        version_id="v2024",
        doc_title=title,
        article_no=f"表格1第{row_index + 1}行",
        block_type=BlockType.SECTION,
        text=text,
    )
    return article.model_copy(
        update={
            "block_kind": "table_row",
            "parent_article_id": f"award@v2024#{table_id}",
            "parent_table_id": table_id,
            "table_id": table_id,
            "row_id": f"{table_id}-r{row_index}",
            "row_index": row_index,
            "table_headers": list(_HEADERS),
            "header_paths": [list(path) for path in _PATHS],
            "cell_provenance": cells,
        }
    )


def _member_evidence(title: str = _MEMBER_TITLE) -> list:
    rows = [
        _member_row(1, "联络委员", "赵甲", title=title),
        _member_row(2, "联络委员", "钱乙", title=title),
        _member_row(3, "联络委员", "孙丙", title=title),
    ]
    return [make_evidence(article, rank=index) for index, article in enumerate(rows, 1)]


def test_role_member_claim_with_all_reliable_names_is_answered() -> None:
    unit = _unit(_MEMBER_QUESTION, subject="乙学院", aspects=["负责人"])
    claim = _claim(
        "乙学院委员会联络委员包括赵甲、钱乙和孙丙。",
        title=_MEMBER_TITLE,
        no="成员名单",
    )
    audit = _audit(unit, claim, evidence=_member_evidence())
    assert audit.units[0]["status"] == "answered"
    assert audit.units[0]["claim_ids"] == ["c1"]


def test_missing_member_stays_missing() -> None:
    unit = _unit(_MEMBER_QUESTION, subject="乙学院", aspects=["负责人"])
    claim = _claim(
        "乙学院委员会联络委员包括赵甲和钱乙。",
        title=_MEMBER_TITLE,
        no="成员名单",
    )
    audit = _audit(unit, claim, evidence=_member_evidence(), status="answered")
    assert audit.units[0]["status"] == "generation_missing"
    assert any("孙丙" in item for item in audit.missing)


def test_generic_or_wrong_role_does_not_answer_role_question() -> None:
    unit = _unit(_MEMBER_QUESTION, subject="乙学院", aspects=["负责人"])
    evidence = _member_evidence()
    for text in (
        "乙学院委员会负责人包括赵甲、钱乙和孙丙。",
        "乙学院委员会的负责人很多，包括赵甲、钱乙和孙丙。",
    ):
        claim = _claim(text, title=_MEMBER_TITLE, no="成员名单")
        audit = _audit(unit, claim, evidence=evidence, status="answered")
        assert audit.units[0]["status"] == "generation_missing", text


def test_other_college_member_list_does_not_answer() -> None:
    unit = _unit(_MEMBER_QUESTION, subject="乙学院", aspects=["负责人"])
    title = "丙学院委员会联络委员名单"
    claim = _claim(
        "丙学院委员会联络委员包括赵甲、钱乙和孙丙。",
        title=title,
        no="成员名单",
    )
    audit = _audit(
        unit,
        claim,
        evidence=_member_evidence(title),
        status="answered",
    )
    assert audit.units[0]["status"] == "generation_missing"


def test_course_list_question_keeps_its_content_binding() -> None:
    question = "专业甲核心课程有哪些？"
    unit = _unit(question, subject="专业甲", aspects=["核心课程"])
    article = make_article(
        "plan@v2024#核心课程",
        doc_title="专业甲培养方案",
        article_no="专业核心课程",
        text="专业甲核心课程包括课程甲、课程乙、课程丙。",
    )
    claim = _claim(
        "专业甲核心课程包括课程甲、课程乙、课程丙。",
        title="专业甲培养方案",
        no="专业核心课程",
    )
    audit = _audit(unit, claim, evidence=[make_evidence(article)])
    assert audit.units[0]["status"] == "answered"
    assert audit.units[0]["claim_ids"] == ["c1"]


# ---------- C: action dates without repeating the property word ------------

_DATE_QUESTION = "组级审核和院级审核公示分别安排在哪些日期？"
_DATE_TITLE = "年度评议工作安排"


def test_action_dates_without_literal_date_word_are_answered() -> None:
    unit = _unit(_DATE_QUESTION)
    claim = _claim(
        "组级审核为4月5日-6日，院级审核公示为4月7日-9日。",
        title=_DATE_TITLE,
        no="评议安排",
    )
    audit = _audit(unit, claim)
    assert audit.units[0]["status"] == "answered"
    assert audit.units[0]["claim_ids"] == ["c1"]


def test_publicity_date_cannot_borrow_another_action_date() -> None:
    unit = _unit(_DATE_QUESTION)
    claim = _claim(
        "组级审核为4月5日-6日。",
        title=_DATE_TITLE,
        no="评议安排",
    )
    audit = _audit(unit, claim, status="answered")
    assert audit.units[0]["status"] == "generation_missing"
    assert any("院级审核公示" in item for item in audit.missing)


def test_title_dates_cannot_replace_action_dates() -> None:
    unit = _unit(_DATE_QUESTION)
    claim = _claim(
        "组级审核和院级审核公示均已完成。",
        title="年度评议工作安排（4月5日-6日、4月7日-9日）",
        no="评议安排",
    )
    audit = _audit(unit, claim, status="answered")
    assert audit.units[0]["status"] == "generation_missing"
    assert audit.missing


# ---------- D: colon document context vs a real question -------------------

_DOC_QUESTION = "查询甲学部2019年博士生招生细则：网上报名起止日期和报名费分别是什么？"


def test_document_colon_context_need_not_be_repeated() -> None:
    unit = _unit(_DOC_QUESTION, subject="甲学部")
    claim = _claim(
        "网上报名时间为2019年9月2日至9月6日，报名费为120元。",
        title="甲学部2019年博士生招生细则",
        no="报名说明",
    )
    audit = _audit(unit, claim)
    assert audit.units[0]["status"] == "answered"
    assert audit.units[0]["claim_ids"] == ["c1"]


def test_document_question_still_needs_the_fee() -> None:
    unit = _unit(_DOC_QUESTION, subject="甲学部")
    claim = _claim(
        "网上报名时间为2019年9月2日至9月6日。",
        title="甲学部2019年博士生招生细则",
        no="报名说明",
    )
    audit = _audit(unit, claim, status="answered")
    assert audit.units[0]["status"] == "generation_missing"
    assert any("报名费" in item for item in audit.missing)


def test_document_question_still_needs_the_date() -> None:
    unit = _unit(_DOC_QUESTION, subject="甲学部")
    claim = _claim(
        "报名费为120元。",
        title="甲学部2019年博士生招生细则",
        no="报名说明",
    )
    audit = _audit(unit, claim, status="answered")
    assert audit.units[0]["status"] == "generation_missing"
    assert any("日期" in item for item in audit.missing)


def test_specific_colon_left_is_required_content() -> None:
    question = "报名费：博士生和硕士生分别是多少？"
    unit = _unit(question)
    full = _claim(
        "博士生报名费为120元，硕士生报名费为80元。",
        title="招生收费说明",
        no="收费标准",
    )
    assert _audit(unit, full).units[0]["status"] == "answered"

    partial = _claim(
        "博士生报名费为120元，硕士生学费为80元。",
        title="招生收费说明",
        no="收费标准",
    )
    audit = _audit(unit, partial, status="answered")
    assert audit.units[0]["status"] == "generation_missing"
    assert any("硕士生报名费" in item for item in audit.missing)


# ---------- E: registered institution aliases ------------------------------

_ALIAS_QUESTION = "乙技院2019年度综合评议，学生自评与班级评议分别安排在哪些日期？"
_ALIAS_TITLE = "乙技术学院2019年度综合评议工作方案"


def _alias_lexicon() -> ProfileLexicon:
    return ProfileLexicon({"dept-a": ("乙技术学院", "乙技院")}, {})


def _alias_claim() -> Claim:
    return _claim(
        "学生自评安排在2019年5月6日，班级评议安排在2019年5月8日。",
        title=_ALIAS_TITLE,
        no="二、评议安排",
    )


def test_registered_college_alias_links_question_and_title() -> None:
    unit = _unit(_ALIAS_QUESTION, subject="乙技院", version="2019")
    audit = _audit(unit, _alias_claim(), lexicon=_alias_lexicon())
    assert audit.units[0]["status"] == "answered"
    assert audit.units[0]["claim_ids"] == ["c1"]


def test_college_alias_does_not_answer_another_college() -> None:
    question = "丙学院2019年度综合评议，学生自评与班级评议分别安排在哪些日期？"
    unit = _unit(question, subject="丙学院", version="2019")
    audit = _audit(unit, _alias_claim(), status="answered", lexicon=_alias_lexicon())
    assert audit.units[0]["status"] == "generation_missing"


def test_alias_title_cannot_supply_the_body_dates() -> None:
    unit = _unit(_ALIAS_QUESTION, subject="乙技院", version="2019")
    claim = _claim(
        "学生自评和班级评议均已完成。",
        title="乙技术学院2019年度综合评议工作方案（学生自评5月6日，班级评议5月8日）",
        no="二、评议安排",
    )
    audit = _audit(unit, claim, status="answered", lexicon=_alias_lexicon())
    assert audit.units[0]["status"] == "generation_missing"
    assert audit.missing


def test_without_lexicon_alias_keeps_literal_scope() -> None:
    unit = _unit(_ALIAS_QUESTION, subject="乙技院", version="2019")
    audit = _audit(unit, _alias_claim(), status="answered")
    assert audit.units[0]["status"] == "generation_missing"


# ---------- F: 每生每年 / 每生每月 share only the 人/生 unit ---------------


def _audit_many(
    unit: QuestionUnit,
    claims: list[Claim],
    *,
    status: str = "generation_missing",
    ids: list[str] | None = None,
    evidence: list | None = None,
):
    if ids is None:
        ids = [claim.claim_id for claim in claims] if status == "answered" else []
    verdict = {
        "units": [
            {
                "unit_id": unit.unit_id,
                "status": status,
                "claim_ids": ids,
                "missing_items": [],
            }
        ],
        "unanswered_questions": [],
    }
    checker = CoverageChecker(FakeChatProvider([json.dumps(verdict, ensure_ascii=False)]))
    return checker.check(
        unit.original_span, [unit], claims, list(evidence or []), []
    )


def test_per_student_yearly_wording_answers_two_degree_levels() -> None:
    unit = _unit(_AWARD_QUESTION)
    claim = _claim(
        "博士研究生一等奖每生每年9000元，硕士研究生一等奖每生每年6000元。",
        title="年度学业奖励评定办法",
        no="奖励标准",
    )
    audit = _audit(unit, claim)
    assert audit.units[0]["status"] == "answered"
    assert audit.units[0]["claim_ids"] == ["c1"]
    assert audit.missing == []


def test_per_student_monthly_wording_does_not_answer_yearly_amount() -> None:
    unit = _unit(_AWARD_QUESTION)
    claim = _claim(
        "博士研究生一等奖每生每月900元，硕士研究生一等奖每生每月600元。",
        title="年度学业奖励评定办法",
        no="奖励标准",
    )
    audit = _audit(unit, claim, status="answered")
    assert audit.units[0]["status"] == "generation_missing"
    assert any("每人每年" in item for item in audit.missing)


def test_per_student_question_wording_accepts_standard_claim() -> None:
    question = "某项年度学业奖励，博士生和硕士生的每生每年奖励标准各是多少？"
    unit = _unit(question)
    claim = _claim(
        "博士研究生一等奖每人每年9000元，硕士研究生一等奖每人每年6000元。",
        title="年度学业奖励评定办法",
        no="奖励标准",
    )
    audit = _audit(unit, claim)
    assert audit.units[0]["status"] == "answered"
    assert audit.units[0]["claim_ids"] == ["c1"]


# ---------- G: instructions and exclusions never hide the real role ---------

_ROLE_INSTRUCTION_QUESTION = (
    "乙学院党委联络委员都有谁？请完整列出，不要把其他委员混进来。"
)
_ROLE_PARALLEL_QUESTION = (
    "乙学院党委联络委员和宣传委员是谁？请完整列出，不要把其他委员混进来。"
)
_ROLE_TITLE = "乙学院委员会职务分工名单"


def _role_evidence() -> list:
    rows = [
        _member_row(1, "联络委员", "赵甲", title=_ROLE_TITLE),
        _member_row(2, "联络委员", "钱乙", title=_ROLE_TITLE),
        _member_row(3, "宣传委员", "周丁", title=_ROLE_TITLE),
        _member_row(4, "宣传委员", "吴戊", title=_ROLE_TITLE),
    ]
    return [make_evidence(article, rank=index) for index, article in enumerate(rows, 1)]


def test_instruction_and_exclusion_still_answer_requested_role() -> None:
    unit = _unit(_ROLE_INSTRUCTION_QUESTION, subject="乙学院", aspects=["负责人"])
    claim = _claim(
        "乙学院党委联络委员包括赵甲、钱乙和孙丙。",
        title=_MEMBER_TITLE,
        no="成员名单",
    )
    audit = _audit(unit, claim, evidence=_member_evidence())
    assert audit.units[0]["status"] == "answered"
    assert audit.units[0]["claim_ids"] == ["c1"]
    assert audit.missing == []


def test_exclusion_question_rejects_names_under_another_role() -> None:
    unit = _unit(_ROLE_INSTRUCTION_QUESTION, subject="乙学院", aspects=["负责人"])
    claim = _claim(
        "乙学院党委宣传委员包括赵甲、钱乙和孙丙。",
        title=_MEMBER_TITLE,
        no="成员名单",
    )
    audit = _audit(unit, claim, evidence=_member_evidence(), status="answered")
    assert audit.units[0]["status"] == "generation_missing"


def test_parallel_roles_need_both_roles_in_the_claim() -> None:
    unit = _unit(_ROLE_PARALLEL_QUESTION, subject="乙学院", aspects=["负责人"])
    claim = _claim(
        "乙学院党委联络委员包括赵甲、钱乙，宣传委员包括周丁、吴戊。",
        title=_ROLE_TITLE,
        no="成员名单",
    )
    audit = _audit(unit, claim, evidence=_role_evidence())
    assert audit.units[0]["status"] == "answered"
    assert audit.units[0]["claim_ids"] == ["c1"]


def test_parallel_roles_reject_single_role_claim() -> None:
    unit = _unit(_ROLE_PARALLEL_QUESTION, subject="乙学院", aspects=["负责人"])
    claim = _claim(
        "乙学院党委联络委员包括赵甲、钱乙、周丁、吴戊。",
        title=_ROLE_TITLE,
        no="成员名单",
    )
    audit = _audit(unit, claim, evidence=_role_evidence(), status="answered")
    assert audit.units[0]["status"] == "generation_missing"
    assert audit.units[0]["claim_ids"] == []


def test_parallel_roles_still_require_every_reliable_name() -> None:
    unit = _unit(_ROLE_PARALLEL_QUESTION, subject="乙学院", aspects=["负责人"])
    claim = _claim(
        "乙学院党委联络委员包括赵甲、钱乙，宣传委员包括周丁。",
        title=_ROLE_TITLE,
        no="成员名单",
    )
    audit = _audit(unit, claim, evidence=_role_evidence(), status="answered")
    assert audit.units[0]["status"] == "generation_missing"
    assert any("吴戊" in item for item in audit.missing)


_ROLE_FALLBACK_QUESTION = "乙学院评优办法：联络委员都有谁，宣传委员是谁？"


def _role_fallback_unit() -> QuestionUnit:
    return _unit(_ROLE_FALLBACK_QUESTION, subject="乙学院")


def _contact_claim() -> Claim:
    return _claim(
        "乙学院党委联络委员是赵甲、钱乙。",
        title="乙学院评优办法",
        claim_id="c1",
    )


def _publicity_claim() -> Claim:
    return _claim(
        "乙学院党委宣传委员是周丁、吴戊。",
        title="乙学院评优办法",
        claim_id="c2",
    )


def test_fallback_role_question_keeps_both_roles_independent() -> None:
    unit = _role_fallback_unit()
    audit = _audit_many(unit, [_contact_claim(), _publicity_claim()])
    assert audit.units[0]["status"] == "answered"
    assert audit.units[0]["claim_ids"] == ["c1", "c2"]
    assert audit.missing == []


def test_fallback_role_question_rejects_a_single_role_answer() -> None:
    unit = _role_fallback_unit()
    audit = _audit_many(
        unit, [_publicity_claim()], status="answered", ids=["c2"]
    )
    assert audit.units[0]["status"] == "generation_missing"
    assert audit.units[0]["claim_ids"] == []
    assert any("联络委员" in item for item in audit.missing)


# ---------- H: multi-claim collective binding on fallback questions --------

_UNION_QUESTION = "乙学院招生细则：网上报名起止日期和报名费分别是什么？"


def _union_unit(*, version: str | None = None) -> QuestionUnit:
    return _unit(_UNION_QUESTION, subject="乙学院", version=version)


def _union_date_claim() -> Claim:
    return _claim(
        "网上报名时间为2024年9月1日至9月10日。",
        title="乙学院招生细则",
        no="报名说明",
        claim_id="c1",
    )


def _union_fee_claim() -> Claim:
    return _claim(
        "报名费为200元。",
        title="乙学院招生细则",
        no="报名说明",
        claim_id="c2",
    )


def test_union_of_date_and_fee_claims_recovers_answered_unit() -> None:
    unit = _union_unit()
    audit = _audit_many(unit, [_union_date_claim(), _union_fee_claim()])
    assert audit.units[0]["status"] == "answered"
    assert audit.units[0]["claim_ids"] == ["c1", "c2"]
    assert audit.missing == []


def test_answered_with_only_one_claim_id_is_corrected_by_union() -> None:
    unit = _union_unit()
    audit = _audit_many(
        unit, [_union_date_claim(), _union_fee_claim()], status="answered", ids=["c1"]
    )
    assert audit.units[0]["status"] == "answered"
    assert audit.units[0]["claim_ids"] == ["c1", "c2"]
    assert audit.missing == []


def test_three_claims_cover_three_sub_questions_collectively() -> None:
    question = "乙学院招生细则：网上报名起止日期、报名费和考试费分别是什么？"
    unit = _unit(question, subject="乙学院")
    exam_fee = _claim(
        "考试费为100元。",
        title="乙学院招生细则",
        no="报名说明",
        claim_id="c3",
    )
    audit = _audit_many(
        unit,
        [_union_date_claim(), _union_fee_claim(), exam_fee],
        status="answered",
        ids=["c1"],
    )
    assert audit.units[0]["status"] == "answered"
    assert audit.units[0]["claim_ids"] == ["c1", "c2", "c3"]
    assert audit.missing == []


def test_unrelated_claim_is_not_attached_to_the_union() -> None:
    unit = _union_unit()
    interview = _claim(
        "考生须参加面试。",
        title="乙学院招生细则",
        no="报名说明",
        claim_id="c3",
    )
    audit = _audit_many(
        unit,
        [_union_date_claim(), _union_fee_claim(), interview],
        status="answered",
        ids=["c1", "c2", "c3"],
    )
    assert audit.units[0]["status"] == "answered"
    assert audit.units[0]["claim_ids"] == ["c1", "c2"]
    assert "c3" not in audit.units[0]["claim_ids"]


def test_unrelated_claim_cannot_substitute_for_the_missing_fee() -> None:
    unit = _union_unit()
    interview = _claim(
        "考生须参加面试。",
        title="乙学院招生细则",
        no="报名说明",
        claim_id="c3",
    )
    audit = _audit_many(
        unit,
        [_union_date_claim(), interview],
        status="answered",
        ids=["c1", "c3"],
    )
    assert audit.units[0]["status"] == "generation_missing"
    assert audit.units[0]["claim_ids"] == []
    assert any("报名费" in item for item in audit.missing)


def test_other_institution_fee_claim_cannot_borrow_the_union() -> None:
    unit = _union_unit()
    wrong_scope = _claim(
        "丙学院招生细则规定报名费为200元。",
        title="丙学院招生细则",
        no="报名说明",
        claim_id="c2",
    )
    audit = _audit_many(
        unit,
        [_union_date_claim(), wrong_scope],
        status="answered",
        ids=["c1", "c2"],
    )
    assert audit.units[0]["status"] == "generation_missing"
    assert audit.units[0]["claim_ids"] == []
    assert any("报名费" in item for item in audit.missing)


def test_wrong_version_fee_claim_cannot_borrow_the_union() -> None:
    unit = _union_unit(version="2024版")
    wrong_version = _claim(
        "乙学院2023版招生细则规定报名费为200元。",
        title="乙学院2023版招生细则",
        no="报名说明",
        claim_id="c2",
    )
    audit = _audit_many(
        unit,
        [_union_date_claim(), wrong_version],
        status="answered",
        ids=["c1", "c2"],
    )
    assert audit.units[0]["status"] == "generation_missing"
    assert audit.units[0]["claim_ids"] == []
    assert any("报名费" in item for item in audit.missing)


def test_one_missing_sub_question_stays_missing() -> None:
    unit = _union_unit()
    audit = _audit_many(
        unit, [_union_date_claim()], status="answered", ids=["c1"]
    )
    assert audit.units[0]["status"] == "generation_missing"
    assert any("报名费" in item for item in audit.missing)
