from __future__ import annotations

import pytest

from campusguide.agents.coverage_actions import requirement_answered

APPEAL_ANSWER = (
    "收到退学决定书7日内向学校学生申诉处理委员会书面申诉；"
    "委员会接到申诉11日内作出复查；"
    "复查决定书送达13日内可向省教育厅申诉。"
)


@pytest.mark.parametrize(
    ("requirement", "answer", "expected"),
    [
        ("如果仍要开课需谁批准", "人数不足而仍需开课，须报教研室批准。", True),
        ("仍需开课批准机关", "开课须报学校批准。", True),
        ("如果仍要开课需谁批准", "开课须批准。", False),
        ("如果仍要开课需谁批准", "开课由教务处备案。", False),
        ("如果仍要开课需谁批准", "开课未经教务处批准。", False),
        ("如果仍要开课需谁批准", "开课无须报学校批准。", False),
        ("请假2天以内由谁审批", "请假2天以内由辅导员审批。", True),
        ("请假5至12天由谁审批", "请假5-12天由学院审批。", True),
        ("请假5至12天由谁审批", "请假2天以内由辅导员审批。", False),
        ("请假2天以内由谁审批", "请假5至12天由学院审批。", False),
        ("请假5至12天由谁审批", "请假2天以内由辅导员审批；5至12天按流程办理。", False),
        ("请假3天由谁审批", "请假3天以内由辅导员审批。", True),
        ("请假3天由谁审批", "请假15天以上由学院审批。", False),
        ("请假4至14天由谁审批", "请假4天由辅导员审批。", False),
        ("请假4至14天由谁审批", "请假4至14天由学院审批。", True),
        ("请假15天以上由谁审批", "请假30天以内由学院审批。", False),
        ("请假15天以上由谁审批", "请假15天以上由学院审批。", True),
        ("请假3天由谁审批", "请假3个工作日以内由辅导员审批。", False),
        ("请假3个工作日由谁审批", "请假3天以内由辅导员审批。", False),
        ("请假3天由谁审批", "困难补助3天以内由财务处批准。", False),
        (
            "仍需开课由谁批准",
            "停开课程由教务处通知改选；困难补助由财务处批准。",
            False,
        ),
        ("仍需开课由谁批准", "开课由教务处批准；困难补助由财务处批准。", True),
        ("仍需开课由谁批准", "停开课程由教务处批准。", False),
        ("仍需开课由谁批准", "开课时间另行通知，困难补助由财务处批准。", False),
        ("仍需开课由谁批准", "开课由教务处批准尚不明确。", False),
        ("请假2天以内由谁审批", "困难补助2天以内由财务处批准。", False),
        ("奖助申请由谁批准", "奖助申请由学生资助中心批准。", True),
        ("奖助申请由谁批准", "困难补助由学生资助中心批准。", False),
        ("请假由谁审批", "请假由辅导员审批。", True),
        ("请假由谁审批", "请假须报学院审批。", True),
        ("请假由谁审批", "开课由教务处批准。", False),
        ("转专业由谁批准", "转专业由教务处批准。", True),
        ("转专业由谁批准", "困难补助由教务处批准。", False),
    ],
)
def test_authority_requires_bound_range_actor_and_approval(
    requirement: str,
    answer: str,
    expected: bool,
) -> None:
    assert requirement_answered(requirement, answer) is expected


@pytest.mark.parametrize(
    ("requirement", "answer", "expected"),
    [
        (
            "没选课自行听课再考核，成绩怎么处理",
            "未办理选课手续自行听课，不得参加考核；即使参加也不予登记成绩。",
            True,
        ),
        ("没选课自行听课再考核，成绩怎么处理", "已选课程不参加考核，成绩记0分。", False),
        (
            "选了课但不参加考核，成绩怎么处理",
            "已选课程不参加考核的，成绩按0分记。",
            True,
        ),
        (
            "选了课但不参加考核，成绩怎么处理",
            "未办理选课手续自行听课的，不予登记成绩。",
            False,
        ),
        ("没选课自行听课再考核，成绩怎么处理", "未办理选课手续自行听课。", False),
        (
            "没选课自行听课再考核，成绩怎么处理",
            "未办理选课手续自行听课的学生，不得擅自离校，考核由学校安排。",
            False,
        ),
        ("没选课能否参加考核", "未办理选课手续自行听课的，不得参加考核。", True),
        ("没选课能否参加考核", "未办理选课手续自行听课的，不得擅自离校。", False),
        ("没选课能否参加考核", "未办理选课手续自行听课的，可以参加考核。", True),
        (
            "没选课自行听课再考核，成绩怎么处理",
            "未办理选课手续自行听课的，考核成绩尚不明确。",
            False,
        ),
        ("选了课但不参加考核，成绩怎么处理", "已选课程不参加考核，考核由学校安排。", False),
    ],
)
def test_selection_state_and_grade_result_stay_on_the_same_branch(
    requirement: str,
    answer: str,
    expected: bool,
) -> None:
    assert requirement_answered(requirement, answer) is expected


@pytest.mark.parametrize(
    ("requirement", "answer", "expected"),
    [
        (
            "主修毕业或结业离校时，微专业学习还能继续吗",
            "主修毕业或结业离校时，微专业学习同时终止。",
            True,
        ),
        ("主修毕业或结业离校时，微专业学习还能继续吗", "另一个项目学习同时终止。", False),
        ("主修毕业或结业离校时，微专业学习还能继续吗", "辅修专业学习同时终止。", False),
        ("主修毕业或结业离校时，微专业学习还能继续吗", "微专业学习仍可继续。", True),
        ("没修满微专业学分能得到什么", "学校只提供修读成绩单。", True),
        ("没修满微专业学分能得到什么", "微专业合格后发结业证书。", False),
        ("没修满微专业学分能得到什么", "微专业未修满学分的，只提供成绩单。", True),
        ("没修满微专业学分能得到什么", "微专业未修满学分。", False),
        ("没修满微专业学分能得到什么", "其他项目未修满学分的，只提供成绩单。", False),
        ("国际交流项目学习还能继续吗", "国际交流项目学习终止。", None),
        (
            "主修毕业离校时微专业学习还能继续吗",
            "主修专业学业结束，微专业课程实行学分制。",
            False,
        ),
        (
            "主修毕业离校时微专业学习还能继续吗",
            "学生主修专业学业结束，无论毕业或者结业离校，微专业学习同时终止。",
            True,
        ),
        ("微专业学习还能继续吗", "微专业可继续修读。", True),
        ("微专业学习还能继续吗", "微专业能否继续学习尚不明确。", False),
        ("微专业学习还能继续吗", "微专业学习不能继续。", True),
        ("微专业学习还能继续吗", "微专业学习终止。", True),
        ("主修毕业离校时微专业学习还能继续吗", "主修专业学业结束。", False),
        ("主修毕业离校时微专业学习还能继续吗", "微专业课程实行学分制。", False),
        (
            "主修毕业离校时微专业学习还能继续吗",
            "主修专业学业结束，辅修学习同时终止。",
            False,
        ),
        (
            "主修毕业离校时微专业学习还能继续吗",
            "微专业课程实行学分制，其学习同时终止。",
            True,
        ),
        ("没修满微专业学分能得到什么", "学生未修满微专业学分。", False),
        ("没修满微专业学分能得到什么", "微专业未修满学分的，只有学分证明。", False),
        ("没修满微专业学分能得到什么", "微专业未修满学分的，学校提供学分证明。", True),
        (
            "没修满微专业学分能得到什么",
            "未修满微专业学分能否取得证明尚不明确。",
            False,
        ),
    ],
)
def test_program_subject_and_condition_are_not_interchangeable(
    requirement: str,
    answer: str,
    expected: bool | None,
) -> None:
    assert requirement_answered(requirement, answer) is expected


@pytest.mark.parametrize(
    ("requirement", "answer", "expected"),
    [
        ("向校内委员会申诉的期限", APPEAL_ANSWER, True),
        ("校内复查期限", APPEAL_ANSWER, True),
        ("再向省教育厅申诉期限", APPEAL_ANSWER, True),
        (
            "校内复查期限",
            "收到退学决定书7日内向学校学生申诉处理委员会书面申诉；"
            "复查决定书送达13日内可向省教育厅申诉。",
            False,
        ),
        (
            "向校内委员会申诉的期限",
            "委员会接到申诉11日内作出复查；"
            "复查决定书送达13日内可向省教育厅申诉。",
            False,
        ),
        (
            "再向省教育厅申诉期限",
            "收到退学决定书7日内向学校学生申诉处理委员会书面申诉；"
            "委员会接到申诉11日内作出复查。",
            False,
        ),
        (
            "校内复查期限",
            "学校学生申诉处理委员会应在接到申诉后11日内作出复查决定。",
            True,
        ),
        (
            "向校内委员会申诉的期限",
            "未向学校学生申诉处理委员会提出申诉。",
            False,
        ),
        (
            "向校内委员会申诉的期限",
            "7日内；向学校学生申诉处理委员会提出申诉。",
            False,
        ),
        (
            "向校内委员会申诉的期限",
            "7日内向学校学生申诉处理委员会提出申诉。",
            True,
        ),
        ("校内复查期限", "11日内；学校学生申诉处理委员会作出复查。", False),
        ("校内复查期限", "11日内学校学生申诉处理委员会作出复查。", True),
        (
            "向校内委员会申诉的期限",
            "7日内向学校提出申诉尚不明确。",
            False,
        ),
    ],
)
def test_appeal_deadlines_are_bound_to_stage_and_action(
    requirement: str,
    answer: str,
    expected: bool,
) -> None:
    assert requirement_answered(requirement, answer) is expected


@pytest.mark.parametrize(
    "requirement",
    [
        "学校地址在哪里",
        "奖学金怎么申请",
        "微专业怎么报名",
        "微专业继续教育怎么报名",
        "今天天气怎么样",
        "开课须批准",
    ],
)
def test_unrecognized_requirements_defer_to_legacy(requirement: str) -> None:
    assert requirement_answered(requirement, "任何回答") is None


def test_overlapping_shapes_defer_to_legacy() -> None:
    requirement = "没选课参加考核由谁审批，成绩怎么处理"
    answer = "未办理选课手续自行听课，不予登记成绩。"
    assert requirement_answered(requirement, answer) is None


def test_recognized_request_with_empty_answer_is_false() -> None:
    assert requirement_answered("请假2天以内由谁审批", "") is False


BOTH_SELECTION_STATES_ANSWER = (
    "已选课程不参加考核的，成绩记0分；"
    "未办理选课手续自行听课的，不予登记成绩。"
)

BACHELOR_LEAVE_ANSWER = (
    "本科生请假3天以内由辅导员审批；"
    "4至14天由所在学院分管学生工作的院领导审批，报学院备案；"
    "请假15天以上（含15天），由党委学生工作部审批，同时报本科生院备案。"
)


@pytest.mark.parametrize(
    ("requirement", "answer", "expected"),
    [
        ("选了课但不参加考核，成绩怎么处理", BOTH_SELECTION_STATES_ANSWER, True),
        ("没选课自行听课再考核，成绩怎么处理", BOTH_SELECTION_STATES_ANSWER, True),
        (
            "选了课但不参加考核，成绩怎么处理",
            "已选课程不参加考核的；未办理选课手续自行听课的，不予登记成绩。",
            False,
        ),
        (
            "没选课自行听课再考核，成绩怎么处理",
            "已选课程不参加考核的，成绩记0分；未办理选课手续自行听课的。",
            False,
        ),
        (
            "选了课能否参加考核",
            "已选课程可以参加考核；未办理选课手续自行听课的，不得参加考核。",
            True,
        ),
        (
            "没选课能否参加考核",
            "已选课程可以参加考核；未办理选课手续自行听课的，不得参加考核。",
            True,
        ),
    ],
)
def test_selection_answers_with_both_states_are_matched_per_branch(
    requirement: str,
    answer: str,
    expected: bool,
) -> None:
    assert requirement_answered(requirement, answer) is expected


@pytest.mark.parametrize(
    ("requirement", "answer", "expected"),
    [
        (
            "没修满微专业学分能得到什么",
            "主修毕业或结业离校时，微专业学习同时终止。",
            False,
        ),
        ("没修满微专业学分能得到什么", "微专业学习同时终止。", False),
        ("没修满微专业学分能得到什么", "学校只提供修读成绩单。", True),
        ("没修满微专业学分能得到什么", "微专业未修满学分的，只提供成绩单。", True),
        (
            "没修满微专业学分能得到什么",
            "微专业修满学分后颁发结业证书。",
            False,
        ),
        ("没修满微专业学分能得到什么", "微专业未修满学分的，按结业处理。", True),
        (
            "没修满微专业学分能得到什么",
            "微专业未修满学分的，不予发放结业证书。",
            True,
        ),
        ("没修满微专业学分能得到什么", "其他项目未修满学分的，按结业处理。", False),
        ("没修满微专业学分能得到什么", "主修未修满学分的，按结业处理。", False),
    ],
)
def test_incomplete_program_outcome_requires_its_own_result(
    requirement: str,
    answer: str,
    expected: bool,
) -> None:
    assert requirement_answered(requirement, answer) is expected


@pytest.mark.parametrize(
    ("requirement", "answer", "expected"),
    [
        ("本科生请假3天由谁审批", BACHELOR_LEAVE_ANSWER, True),
        ("本科生请假4至14天由谁审批", BACHELOR_LEAVE_ANSWER, True),
        ("本科生请假15天以上由谁审批", BACHELOR_LEAVE_ANSWER, True),
        (
            "本科生请假4至14天由谁审批",
            "研究生请假4至14天由学院审批。",
            False,
        ),
        (
            "研究生请假4至14天由谁审批",
            "本科生请假4至14天由学院审批。",
            False,
        ),
        ("本科生请假4至14天由谁审批", "请假4至14天由学院审批。", True),
        (
            "本科生请假15天以上由谁审批",
            "本科生请假3天以内由辅导员审批；"
            "研究生请假4至14天由学院审批；"
            "请假15天以上由学校审批。",
            False,
        ),
        (
            "研究生请假15天以上由谁审批",
            "本科生请假3天以内由辅导员审批；"
            "研究生请假4至14天由学院审批；"
            "请假15天以上由学校审批。",
            True,
        ),
        (
            "请假15天以上由谁审批",
            "请假3天以内由辅导员审批；困难补助15天以上由财务处批准。",
            False,
        ),
        ("请假3天由谁审批", "转专业3天以内由教务处批准。", False),
        (
            "全日制本科生请假3天由谁审批",
            "研究生请假3天以内由辅导员审批。",
            False,
        ),
        ("全日制本科生请假3天由谁审批", "请假3天以内由辅导员审批。", True),
        ("本科生请假15天以上由谁审批", "本科生请假2周以上由学院审批。", False),
        ("本科生请假4至14天由谁审批", "本科生请假4至17天由学院审批。", True),
        (
            "本科生请假15天以上由谁审批",
            "请假15天以上（含15天）由党委学生工作部审批。",
            True,
        ),
    ],
)
def test_authority_context_carries_business_and_population_per_series(
    requirement: str,
    answer: str,
    expected: bool,
) -> None:
    assert requirement_answered(requirement, answer) is expected


@pytest.mark.parametrize(
    ("requirement", "answer", "expected"),
    [
        (
            "如果仍要开课需谁批准",
            "每门课程修读人数原则上不得少于20人，当选课人数少于20人时该课程停开；"
            "如选课人数少于20人而又需要开课的，须报研究生院批准。",
            True,
        ),
        (
            "如果仍要开课需谁批准",
            "每门课程修读人数原则上不得少于20人，当选课人数少于20人时该课程停开；"
            "如选课人数少于20人而又需要开课的，转专业须报研究生院批准。",
            False,
        ),
    ],
)
def test_authority_carries_action_from_noisy_conditional_part(
    requirement: str,
    answer: str,
    expected: bool,
) -> None:
    assert requirement_answered(requirement, answer) is expected
