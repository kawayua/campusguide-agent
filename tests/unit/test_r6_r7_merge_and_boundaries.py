"""R6/A1/A6 merge behaviour, using the review's real claims and counterexamples."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from campusguide.agents.graph import boundary_notes
from campusguide.domain.enums import OutcomeCode
from campusguide.domain.models import Citation, Claim
from campusguide.qa.merge import clean_claim_text, merge_claims

ROOT = Path(__file__).resolve().parents[2]


def _citation(doc_title: str, article_no: str = "第一条") -> Citation:
    return Citation(
        article_id=f"{doc_title}#{article_no}",
        quote="引用原文片段足够长以便通过校验。",
        quote_start=0,
        quote_end=18,
        doc_title=doc_title,
        article_no=article_no,
        effective_from=date(2025, 9, 1),
        source_url="https://www.scau.edu.cn/example.htm",
    )


def _claim(claim_id: str, text: str, *doc_titles: str) -> Claim:
    return Claim(
        claim_id=claim_id,
        text=text,
        citations=[_citation(title) for title in doc_titles] or [_citation("某办法")],
    )


def _texts(claims: list[Claim]) -> list[str]:
    return [claim.text for claim in claims]


def _kinds(result) -> set[str]:
    return {note.kind for note in result.notes}


PLAN_2022 = "2022级计算机科学与技术专业毕业总学分为163学分。"
PLAN_2024 = "2024级计算机科学与技术专业毕业总学分为159.5学分。"

# 真实补答修复样本：两条已核实结论只是措辞、子集或补充关系，不能报“冲突”。
COURSE_ARRANGEMENT = (
    "本科新生入学后，第一学期的课程由学院和教务处根据各专业的教学计划直接安排，不需要选课。"
)
COURSE_ARRANGEMENT_SELF = (
    "本科新生入学后，第一学期的课程由学院和教务处根据各专业的教学计划直接安排，"
    "不需要自己选课。"
)
TRANSFER_MATERIALS_BASE = (
    "本科转学学生的相关手续和证明材料应一式4份，除学校留存外，"
    "同时报拟转入和转出学校所在地省级教育行政部门备案"
)
TRANSFER_MATERIALS = TRANSFER_MATERIALS_BASE + "。"
TRANSFER_FORM = (
    "学生填写《广东省普通高校转学备案表（本专科生）》（以下简称《备案表》，一式4份），"
    "本科生院将《备案表》送分管校领导审批。"
)
TRANSFER_MATERIALS_SUPPLEMENT = (
    TRANSFER_MATERIALS_BASE
    + "；《广东省普通高校转学备案表（本专科生）》也为一式4份。"
)
STUDY_YEARS_FULL = (
    "学生在校学习的最长年限（含休学和保留学籍），四年制专业为6年，休学创业的为8年；"
    "五年制专业为7年，休学创业的为9年。"
)
STUDY_YEARS_STARTUP = (
    "学生在校学习的最长年限（含休学和保留学籍），四年制专业休学创业的最长年限为8年；"
    "五年制专业休学创业的最长年限为9年。"
)
WITHDRAWAL_PARENTHETICAL = (
    "学生休学一般以1年为期，累计休学年限不得超过2年（休学创业的不超过4年）。"
)
WITHDRAWAL_PLAIN = "学生休学一般以1年为期，累计休学年限不得超过2年，休学创业的不超过4年。"
APPEAL_DEADLINE = (
    "学生对退学处理有异议的，向学校学生申诉处理委员会提出书面申诉的期限为"
    "退学决定书送达之日起10日内；学校学生申诉处理委员会在接到书面申诉之日起15日内"
    "作出复查结论并告知申诉人；学生对复查决定有异议的，向广东省教育厅提出书面申诉的"
    "期限为学校复查决定书送达之日起15日内。"
)
APPEAL_DEADLINE_REWORDED = (
    "学生对退学处理有异议的，向学校学生申诉处理委员会提出书面申诉的期限为"
    "退学决定书送达之日起10日内；学校学生申诉处理委员会在接到书面申诉之日起15日内"
    "作出复查结论并告知申诉人；学生对复查决定有异议的，在学校复查决定书送达之日起"
    "15日内，可以向广东省教育厅提出书面申诉。"
)


def test_cross_version_facts_are_both_kept() -> None:
    result = merge_claims(
        [
            _claim("c1", PLAN_2022, "计算机科学与技术专业人才培养方案"),
            _claim("c2", PLAN_2024, "计算机科学与技术专业人才培养方案"),
        ]
    )
    assert _texts(result.claims) == [PLAN_2022, PLAN_2024]
    assert result.dropped_claim_ids == []


def test_cross_version_result_is_order_independent() -> None:
    forward = merge_claims([_claim("c1", PLAN_2022), _claim("c2", PLAN_2024)])
    backward = merge_claims([_claim("c1", PLAN_2024), _claim("c2", PLAN_2022)])
    assert set(_texts(forward.claims)) == set(_texts(backward.claims))
    assert _kinds(forward) == _kinds(backward) == set()


def test_different_class_types_are_not_merged() -> None:
    result = merge_claims(
        [
            _claim("c1", "创新班毕业总学分为160学分。", "计算机科学与技术专业人才培养方案"),
            _claim("c2", "普通班毕业总学分为163学分。", "计算机科学与技术专业人才培养方案"),
        ]
    )
    assert len(result.claims) == 2


def test_true_conflict_keeps_both_statements_with_a_verification_note() -> None:
    result = merge_claims(
        [
            _claim("c1", "国家奖学金奖励标准为每人每年8000元。", "华南农业大学国家奖学金评审办法"),
            _claim("c2", "国家奖学金奖励标准为每人每年6000元。", "农学院国家奖学金评审细则"),
        ]
    )
    assert len(result.claims) == 2
    assert "conflict" in _kinds(result)
    assert "核实" in next(n.detail for n in result.notes if n.kind == "conflict")


def test_conflict_is_not_decided_by_citation_count_or_order() -> None:
    one = _claim("c1", "国家奖学金奖励标准为每人每年8000元。", "办法甲")
    two = _claim(
        "c2",
        "国家奖学金奖励标准为每人每年6000元。",
        "办法乙",
        "办法乙",
    )
    forward = merge_claims([one, two])
    backward = merge_claims([two, one])
    assert len(forward.claims) == len(backward.claims) == 2
    assert set(_texts(forward.claims)) == set(_texts(backward.claims))


def test_duplicate_claims_collapse_without_changing_facts() -> None:
    result = merge_claims(
        [
            _claim("c1", "国家奖学金奖励标准为每人每年8000元，按学年一次发放。"),
            _claim("c2", "国家奖学金奖励标准为每人每年8000元，按学年一次发放。"),
        ]
    )
    assert len(result.claims) == 1
    assert result.dropped_claim_ids == ["c2"]
    assert "duplicate" in _kinds(result)


def test_similar_wording_with_different_numbers_is_not_a_duplicate() -> None:
    result = merge_claims(
        [
            _claim("c1", "计算机科学与技术专业毕业总学分为163学分。", "培养方案"),
            _claim("c2", "计算机科学与技术专业毕业总学分为159.5学分。", "培养方案"),
        ]
    )
    assert len(result.claims) == 2


def test_table_fragment_lines_are_removed() -> None:
    text = "| 课程代码 | 学分 |\n|---|---|\n610001 | 5 |\n学生需修满学分。"
    assert clean_claim_text(text) == "学生需修满学分。"


def test_stale_missing_is_retracted_in_either_order() -> None:
    negative = _claim("c1", "微专业结业证书规则未收录。")
    positive = _claim("c2", "微专业结业证书规则为修满规定学分后颁发证书。")
    for pair in ([negative, positive], [positive, negative]):
        result = merge_claims(pair)
        assert _texts(result.claims) == ["微专业结业证书规则为修满规定学分后颁发证书。"]
        assert "stale_missing" in _kinds(result)


def test_missing_statement_survives_when_nothing_covers_it() -> None:
    result = merge_claims([_claim("c1", "微专业结业证书规则未收录。")])
    assert _texts(result.claims) == ["微专业结业证书规则未收录。"]


def test_info_notes_do_not_become_substantive() -> None:
    result = merge_claims(
        [
            _claim("c1", "国家奖学金奖励标准为每人每年8000元，按学年一次发放。"),
            _claim("c2", "国家奖学金奖励标准为每人每年8000元，按学年一次发放。"),
        ]
    )
    assert result.substantive_notes == []
    assert result.info_notes


def test_boundary_notes_separate_corpus_gap_from_scope_filtering() -> None:
    gap = boundary_notes({}, OutcomeCode.NO_EVIDENCE)
    assert "可能" in gap[0] and "未命中" in gap[0]
    scope = boundary_notes(
        {"retrieval_warnings": ["AUDIENCE_MISMATCH: some-article"]}, OutcomeCode.NO_EVIDENCE
    )
    assert "被剔除" in scope[0] and "不等于学校没有规定" in scope[0]
    assert boundary_notes({}, OutcomeCode.UNVERIFIABLE) == []


# ---------------------------------------------- review repair pairs (2026-11-01)


def test_equivalent_course_arrangement_variant_is_not_a_conflict() -> None:
    result = merge_claims(
        [
            _claim("c1", COURSE_ARRANGEMENT, "华南农业大学本科生选课管理规定"),
            _claim("supplement_1", COURSE_ARRANGEMENT_SELF, "新生选课问答"),
        ]
    )
    assert result.substantive_notes == []
    assert len(result.claims) == 1
    # 等价表述合并后两条 citation 都必须保留。
    assert len(result.claims[0].citations) == 2
    assert "不需要" in result.claims[0].text


def test_transfer_material_superset_is_not_a_conflict() -> None:
    result = merge_claims(
        [
            _claim("c1", TRANSFER_MATERIALS, "华南农业大学本科学生转学工作实施细则"),
            _claim("c2", TRANSFER_FORM, "华南农业大学本科学生转学工作实施细则"),
            _claim(
                "supplement_1",
                TRANSFER_MATERIALS_SUPPLEMENT,
                "华南农业大学本科学生转学工作实施细则",
                "转学备案表填写说明",
            ),
        ]
    )
    assert result.substantive_notes == []
    assert set(_texts(result.claims)) == {
        TRANSFER_MATERIALS,
        TRANSFER_FORM,
        TRANSFER_MATERIALS_SUPPLEMENT,
    }
    supplemented = next(
        claim for claim in result.claims if claim.text == TRANSFER_MATERIALS_SUPPLEMENT
    )
    assert len(supplemented.citations) == 2
    assert all("4份" in text for text in _texts(result.claims))


def test_startup_study_year_subset_is_not_a_conflict() -> None:
    result = merge_claims(
        [
            _claim("c1", STUDY_YEARS_FULL, "华南农业大学本科学生学籍管理实施细则"),
            _claim("supplement_1", STUDY_YEARS_STARTUP, "华南农业大学本科学生学籍管理实施细则"),
        ]
    )
    assert result.substantive_notes == []
    assert set(_texts(result.claims)) == {STUDY_YEARS_FULL, STUDY_YEARS_STARTUP}
    # 子集陈述不能覆盖完整分支，普通学制年限必须仍在结果里。
    assert "6年" in STUDY_YEARS_FULL
    assert any("6年" in text for text in _texts(result.claims))


def test_withdrawal_parenthetical_is_a_safe_equivalent() -> None:
    result = merge_claims(
        [
            _claim("c1", WITHDRAWAL_PARENTHETICAL, "华南农业大学本科学生学籍管理实施细则"),
            _claim("supplement_1", WITHDRAWAL_PLAIN, "华南农业大学本科学生学籍管理实施细则"),
        ]
    )
    assert result.substantive_notes == []
    assert len(result.claims) == 1
    assert result.claims[0].citations


def test_appeal_deadline_reword_keeps_both_without_warning() -> None:
    result = merge_claims(
        [
            _claim("c1", APPEAL_DEADLINE, "华南农业大学本科学生学籍管理实施细则"),
            _claim(
                "supplement_1",
                APPEAL_DEADLINE_REWORDED,
                "华南农业大学本科学生学籍管理实施细则",
            ),
        ]
    )
    assert result.substantive_notes == []
    assert set(_texts(result.claims)) == {APPEAL_DEADLINE, APPEAL_DEADLINE_REWORDED}
    assert all("10日" in text and "15日" in text for text in _texts(result.claims))


def test_complementary_material_items_are_not_a_conflict() -> None:
    result = merge_claims(
        [
            _claim("c1", "国家奖学金申请材料包括申请表和成绩单。"),
            _claim("c2", "国家奖学金申请材料包括个人事迹材料。"),
        ]
    )
    assert result.substantive_notes == []
    assert len(result.claims) == 2


def test_swapped_role_values_are_a_real_conflict() -> None:
    result = merge_claims(
        [
            _claim(
                "c1",
                "本科生转专业申请材料由学院审核后报学校教务处审批，审核结果在学院网站公示。",
            ),
            _claim(
                "c2",
                "本科生转专业申请材料由学校教务处审核后报学院审批，审核结果在学院网站公示。",
            ),
        ]
    )
    assert len(result.claims) == 2
    detail = next(note.detail for note in result.notes if note.kind == "conflict")
    assert "经办主体/角色" in detail
    assert "核实" in detail


@pytest.mark.parametrize("second_amount", ["8000", "6000"])
def test_different_conditions_are_not_a_conflict(second_amount: str) -> None:
    result = merge_claims(
        [
            _claim(
                "c1",
                "国家奖学金奖励标准为每人每年8000元，定向培养的学生可申请。",
            ),
            _claim(
                "c2",
                f"国家奖学金奖励标准为每人每年{second_amount}元，非定向培养的学生可申请。",
            ),
        ]
    )
    # 条件绑定不同（定向/非定向）时，即便数值不同也不能报同一事项冲突。
    assert result.substantive_notes == []
    assert len(result.claims) == 2


def test_polarity_conflict_is_a_real_conflict() -> None:
    result = merge_claims(
        [
            _claim("c1", "本科生必须参加培养方案规定的课程考核。"),
            _claim("c2", "本科生不需要参加培养方案规定的课程考核。"),
        ]
    )
    assert len(result.claims) == 2
    detail = next(note.detail for note in result.notes if note.kind == "conflict")
    assert "义务方向相反" in detail


def test_separate_cohorts_are_kept_apart_without_a_conflict() -> None:
    result = merge_claims(
        [
            _claim("c1", "2022级计算机科学与技术专业毕业总学分为163学分。", "培养方案"),
            _claim("c2", "2024级计算机科学与技术专业毕业总学分为163学分。", "培养方案"),
        ]
    )
    assert len(result.claims) == 2
    assert result.substantive_notes == []


# ---------------------------------------------- explicit actor binding (2026-11-02)


def test_same_leave_flow_with_different_approver_is_a_real_conflict() -> None:
    result = merge_claims(
        [
            _claim("c1", "学生请假由学院审批。"),
            _claim("c2", "学生请假由教务处审批。"),
        ]
    )
    assert len(result.claims) == 2
    detail = next(note.detail for note in result.notes if note.kind == "conflict")
    assert "经办主体/角色" in detail
    assert "核实" in detail


def test_swapped_actual_roles_on_the_same_leave_flow_are_a_real_conflict() -> None:
    result = merge_claims(
        [
            _claim("c1", "学生请假由辅导员审批，并报学院备案。"),
            _claim("c2", "学生请假由学院审批，并报辅导员备案。"),
        ]
    )
    assert len(result.claims) == 2
    detail = next(note.detail for note in result.notes if note.kind == "conflict")
    assert "经办主体/角色" in detail


@pytest.mark.parametrize(
    ("left", "right"),
    [
        ("学生请假由学院审批。", "奖助学金申请由教务处审批。"),
        ("学生请假3天以内由辅导员审批。", "学生请假15天以上由学院审批。"),
        ("学生请假由学院审批。", "学生请假由学院办公室审批。"),
        ("学生请假由华南农业大学教务处审批。", "学生请假由教务处审批。"),
    ],
)
def test_distinct_actor_bindings_do_not_raise_a_conflict(left: str, right: str) -> None:
    result = merge_claims([_claim("c1", left), _claim("c2", right)])
    assert result.substantive_notes == []
    assert len(result.claims) == 2