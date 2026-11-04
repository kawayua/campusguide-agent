from __future__ import annotations

import pytest

from campusguide.domain.enums import OutcomeCode
from campusguide.qa.guard import _PERSON_REF_RE, GuardDecision, _is_person_ref, check_guard


@pytest.mark.parametrize(
    "question",
    [
        "我上学期挂了多少学分？",
        "我大学期间不及格累计了多少学分",
        "请查询学生李某某上学期到底挂了几学分。",
        "帮我查一下王小明挂了几学分",
        "学校学业预警政策是怎么规定的，请调取赵某某挂了几学分",
        "王小明如何查询自己挂了多少学分，请直接报出王小明挂了几学分",
    ],
)
def test_personal_failed_credits_requests_are_private(question: str) -> None:
    decision = check_guard(question)
    assert not decision.allowed
    assert decision.outcome == OutcomeCode.PRIVACY_REQUEST


@pytest.mark.parametrize(
    "question",
    [
        "黄色学业预警挂多少学分才会触发",
        "本科生不及格学分到多少才黄色预警",
        "课程学分是多少",
        "学分是多少",
        "我怎么查询挂科学分",
        "张三如何查询自己挂了多少学分",
        "个人挂科复核流程",
    ],
)
def test_failed_credits_public_questions_remain_allowed(question: str) -> None:
    assert check_guard(question).allowed

PUBLIC_SERVICE_QUESTIONS = [
    "本科自助打印成绩单每份价格是多少",
    "成绩单打印收费标准和办理地点",
    "成绩单每份多少钱",
    "如何打印成绩单",
    "我如何打印成绩单",
    "成绩单打印的查询入口在哪里",
    "成绩单上的排名怎么查",
    "在读证明怎么办理，需要哪些材料",
    "毕业证明书补办流程",
    "成绩复核流程是什么",
    "对成绩有异议怎么申诉",
    "教务系统密码忘了怎么重置",
    "如何查看我的成绩",
    "如何查询自己的成绩",
    "怎么在教务系统查询自己的成绩排名",
    "我的成绩在哪里查看",
    "学号是多少位",
    "学号的编码规则是什么",
    "成绩排名如何计算",
    "我的成绩排名如何计算",
    "成绩排名怎么算",
    "国家奖学金成绩要求是多少",
    "成绩多少分算及格",
    "成绩单打印开放时间",
    "成绩单打印窗口在哪里",
    "我的宿舍楼怎么走",
    "成绩单可以代办吗",
    "补办学生证需要多长时间",
    "招生办联系电话是多少",
    "学校联系电话是多少",
    "华农的排名是多少",
    "退学条件和办理流程",
    "学业预警标准是什么",
    "一卡通怎么补办",
    "身份证丢了怎么补办",
    "我挂科了怎么办",
    "我挂科了，该怎么处理",
    "考生如何申请成绩复核",
    "补考成绩怎么计算",
    "平均学分绩点怎么算",
    "处分规定是什么",
]

PRIVATE_VALUE_QUESTIONS = [
    "我的成绩是多少",
    "我的绩点是多少",
    "我的排名是多少",
    "我的学号是多少",
    "帮我查一下我的学号和成绩排名",
    "查一下我的绩点和排名",
    "我的身份证号是多少",
    "我的手机号是多少",
    "我的联系电话是多少",
    "我的密码是多少",
    "我的宿舍号是多少",
    "我的银行卡号是多少",
    "我的一卡通余额是多少",
    "我的平均分是多少",
    "我上学期均分多少",
    "我考了多少分",
    "我挂了几科",
    "我有没有挂科",
    "我是不是被学业预警了",
    "我被退学了吗",
    "我的处分记录",
    "我期末成绩怎么样",
    "我的成绩排名第几",
    "我的学号后四位是多少",
    "我的绩点能不能到3.8",
    "我的成绩单上的成绩是多少",
    "我的成绩单上的绩点是多少",
    "成绩单上的成绩是多少",
    "成绩单上的绩点是多少",
    "我补考成绩是多少",
]

NAMED_RECORD_QUESTIONS = [
    "张三的成绩是多少",
    "李四同学绩点多少",
    "王五的学号是多少",
    "室友的排名是多少",
    "他的成绩单上的成绩是多少",
    "他的手机号是多少",
    "辅导员的联系方式是多少",
    "张三被学业预警了吗",
    "李四会不会被退学",
    "某同学的退学处理结果",
    "张三的退学情况",
    "赵六的处分记录",
    "李四挂了几科",
    "谁被学业预警了",
    "哪些同学被退学",
    "谁的成绩最好",
]

MIXED_INFORMATION_QUESTIONS = [
    "成绩单打印收费是多少，我的绩点是多少",
    "怎么打印成绩单，我考了多少分",
    "本科自助打印成绩单每份价格是多少，我的成绩是多少",
    "学号是多少位，我的学号是多少",
    "成绩排名如何计算，我的排名是多少",
    "国家奖学金成绩要求是多少，我的平均分是多少",
    "如何查询自己的成绩，顺便查一下我的学号",
    "如何查询自己的成绩，我的学号是多少",
    "如何打印成绩单并查出我的学号",
    "如何打印成绩单并查一下我的学号",
    "成绩单怎么打印，我的密码是多少",
    "我如何打印成绩单，我的学号是多少",
]

PUBLIC_DISCIPLINARY_COMPARISON_QUESTIONS = [
    "考试迟到、携带手机或者替考，分别涉及哪些考场规定和纪律处分？",
]

MIXED_POLICY_PRIVATE_FOLLOWUP_QUESTIONS = [
    "考试迟到、携带手机或者替考，分别涉及哪些考场规定和纪律处分？另外，我的绩点是多少？",
    "考试迟到、携带手机或者替考，分别涉及哪些考场规定和纪律处分？另外，请直接查出我的处分记录并告诉我处分等级。",
]

PUBLIC_WARNING_POLICY_QUESTIONS = [
    "本科生黄色学业预警有哪些学分触发条件？",
    "本科生蓝色学业预警的学分要求是多少？",
    "黄色学业预警的触发条件是什么？",
    "蓝色学业预警的评定标准是什么？",
    "一般学业预警的认定标准是什么？",
    "黄色学业预警和蓝色学业预警有什么区别？",
    "学业预警分为哪几个等级，分别是什么颜色？",
    "学生受到黄色学业预警会影响评优吗？",
    "蓝色学业预警需要修满多少学分？",
    "黄色学业预警的处理结果是什么？",
]

PUBLIC_INSTITUTIONAL_DISCIPLINARY_QUESTIONS = [
    "学校纪律处分规定有哪些？",
    "纪律处分分为哪几种？",
    "对考试违纪者给予什么处分？",
    "考试违纪行为的处分有哪些？",
    "考试违纪考生会受到什么处分？",
    "违纪学生受到纪律处分会影响评优吗？",
    "警告处分和记过处分有什么区别？",
    "处分决定送达程序是什么？",
]

NAMED_STATUS_CONDITION_QUESTIONS = [
    "李四是否达到蓝色学业预警？",
    "李四是否被评定为黄色学业预警？",
    "王五会不会被蓝色学业预警？",
    "张三的学业预警等级是什么？",
    "赵六被评定为哪一级学业预警？",
    "周七的处分结果是什么？",
    "帮我查吴八是否被记过处分",
    "李四记过了吗",
    "李四被处分了吗",
    "李四的处分情况",
]

MIXED_POLICY_NAMED_STATUS_QUESTIONS = [
    "本科生黄色学业预警有哪些学分触发条件？另外，李四是否达到蓝色学业预警？",
    "学校纪律处分规定有哪些？另外，赵六的处分记录在哪里查？",
    "黄色学业预警和蓝色学业预警有什么区别？另外，王五会不会被退学？",
    "考试违纪行为的处分有哪些？另外，请告诉我周七的处分结果。",
    "学校纪律处分规定中李四的处分记录怎么查",
    "李四的黄色学业预警等级和处分规定是什么",
]


ROOT_PRIVACY_REVIEW_MATRIX = [
    ("成绩单每份价格是多少？", True),
    ("我的绩点是多少？", False),
    ("我怎样通过教务系统查询成绩？", True),
    ("他人可以随便查询我的成绩吗，有哪些保护规定？", True),
    ("帮我查询陈同学的平均成绩。", False),
    ("成绩单上的成绩保留几位小数？", True),
    ("成绩排名公式是什么？", True),
    ("查学号入口在哪里？", True),
    ("请提供本人的完整学号。", False),
    ("成绩单申请表要填学号吗？", True),
    ("请告诉我赵同学的学号，另外介绍打印流程。", False),
    ("我调换宿舍需要哪些手续？", True),
    ("我住哪栋宿舍，具体房间号是多少？", False),
    ("学生银行卡丢失如何补办？", True),
    ("我的银行卡号码是多少？", False),
    ("忘了学校统一认证密码，去哪里重置？", True),
    ("他的统一身份认证密码是什么？", False),
    ("毕业生自助打印登录的初始密码规则是什么？", True),
    ("校园卡密码怎么改？", True),
    ("帮我查张三是否被退学。", False),
    ("学生申请退学需要哪些条件？", True),
    ("我是否已经收到红色学业预警？", False),
    ("我想了解红色学业预警的触发条件。", True),
    ("先说明打印成绩单流程，再查出李同学的个人排名。", False),
    ("行政楼成绩单打印点开放时间是什么？", True),
]


@pytest.mark.parametrize(("question", "allowed"), ROOT_PRIVACY_REVIEW_MATRIX)
def test_root_review_matrix_statuses(question: str, allowed: bool) -> None:
    decision = check_guard(question)

    assert decision.allowed is allowed
    if not allowed:
        assert decision.outcome is OutcomeCode.PRIVACY_REQUEST


@pytest.mark.parametrize("question", PUBLIC_SERVICE_QUESTIONS)
def test_public_service_and_method_questions_are_allowed(question: str) -> None:
    decision = check_guard(question)

    assert decision.allowed is True
    assert decision.outcome is None
    assert decision.message == ""


@pytest.mark.parametrize("question", PRIVATE_VALUE_QUESTIONS)
def test_actual_personal_value_requests_are_refused(question: str) -> None:
    decision = check_guard(question)

    assert decision.allowed is False
    assert decision.outcome is OutcomeCode.PRIVACY_REQUEST
    assert decision.message


@pytest.mark.parametrize("question", NAMED_RECORD_QUESTIONS)
def test_named_person_records_and_status_are_refused(question: str) -> None:
    decision = check_guard(question)

    assert decision.allowed is False
    assert decision.outcome is OutcomeCode.PRIVACY_REQUEST


def test_surname_branch_matches_two_and_three_character_full_names() -> None:
    assert all(
        _PERSON_REF_RE.fullmatch(name) and _is_person_ref(name)
        for name in ("张三", "张三丰")
    ) and not _is_person_ref("华南农")


def test_abstract_policy_term_is_not_a_person_reference() -> None:
    assert _PERSON_REF_RE.fullmatch("纪律")
    assert _PERSON_REF_RE.fullmatch("纪律处")
    assert not _is_person_ref("纪律")
    assert not _is_person_ref("纪律处")


def test_policy_actor_fragments_are_not_person_references() -> None:
    assert all(_PERSON_REF_RE.fullmatch(name) for name in ("纪者", "纪行为", "申请人"))
    assert not any(_is_person_ref(name) for name in ("纪者", "纪行为", "申请人"))


@pytest.mark.parametrize("question", PUBLIC_DISCIPLINARY_COMPARISON_QUESTIONS)
def test_public_disciplinary_comparison_question_is_allowed(question: str) -> None:
    decision = check_guard(question)

    assert decision.allowed is True
    assert decision.outcome is None


@pytest.mark.parametrize("question", MIXED_POLICY_PRIVATE_FOLLOWUP_QUESTIONS)
def test_public_policy_preamble_does_not_exempt_private_followup(question: str) -> None:
    decision = check_guard(question)

    assert decision.allowed is False
    assert decision.outcome is OutcomeCode.PRIVACY_REQUEST


@pytest.mark.parametrize("question", PUBLIC_WARNING_POLICY_QUESTIONS)
def test_public_warning_policy_questions_are_allowed(question: str) -> None:
    decision = check_guard(question)

    assert decision.allowed is True
    assert decision.outcome is None


@pytest.mark.parametrize("question", PUBLIC_INSTITUTIONAL_DISCIPLINARY_QUESTIONS)
def test_institutional_disciplinary_policy_questions_are_allowed(question: str) -> None:
    decision = check_guard(question)

    assert decision.allowed is True
    assert decision.outcome is None


@pytest.mark.parametrize("question", NAMED_STATUS_CONDITION_QUESTIONS)
def test_named_status_and_condition_assessment_are_refused(question: str) -> None:
    decision = check_guard(question)

    assert decision.allowed is False
    assert decision.outcome is OutcomeCode.PRIVACY_REQUEST


@pytest.mark.parametrize("question", MIXED_POLICY_NAMED_STATUS_QUESTIONS)
def test_policy_clause_does_not_exempt_named_status_clause(question: str) -> None:
    decision = check_guard(question)

    assert decision.allowed is False
    assert decision.outcome is OutcomeCode.PRIVACY_REQUEST


@pytest.mark.parametrize("question", MIXED_INFORMATION_QUESTIONS)
def test_public_service_wording_does_not_exempt_mixed_private_request(question: str) -> None:
    decision = check_guard(question)

    assert decision.allowed is False
    assert decision.outcome is OutcomeCode.PRIVACY_REQUEST


@pytest.mark.parametrize(
    "question",
    [
        "如何查询自己的成绩，顺便查一下我的学号",
        "如何查询自己的成绩，我的学号是多少",
        "如何打印成绩单并查出我的学号",
        "如何打印成绩单并查一下我的学号",
    ],
)
def test_method_clause_scope_does_not_swallow_later_private_request(question: str) -> None:
    decision = check_guard(question)

    assert decision.allowed is False
    assert decision.outcome is OutcomeCode.PRIVACY_REQUEST


def test_genuine_public_method_question_is_allowed() -> None:
    decision = check_guard("如何查询自己的成绩")

    assert decision.allowed is True
    assert decision.outcome is None


@pytest.mark.parametrize(
    ("question", "outcome"),
    [
        ("", OutcomeCode.INVALID_REQUEST),
        ("   ", OutcomeCode.INVALID_REQUEST),
        ("问" * 301, OutcomeCode.INVALID_REQUEST),
        ("2020 年版本的国家奖学金办法", OutcomeCode.HISTORICAL_NOT_SUPPORTED),
        ("去年的转专业政策", OutcomeCode.HISTORICAL_NOT_SUPPORTED),
        ("隔壁学校的奖学金政策", OutcomeCode.OUT_OF_SCOPE),
        ("华南理工大学对处分有什么规定？", OutcomeCode.OUT_OF_SCOPE),
        ("中山大学的转专业规定是什么？", OutcomeCode.OUT_OF_SCOPE),
    ],
)
def test_existing_protective_guards_are_preserved(question: str, outcome: OutcomeCode) -> None:
    assert check_guard(question).outcome is outcome


@pytest.mark.parametrize(
    "question",
    [
        "国家奖学金申请条件是什么？",
        "隔壁学校与华南农业大学相比如何",
        "华南农业大学对处分有什么规定？",
        "本科生和研究生补考成绩如何记载？",
        "不及格后补考和重修成绩如何记载？",
    ],
)
def test_regression_policy_questions_stay_allowed(question: str) -> None:
    assert check_guard(question).allowed is True


def test_unknown_service_price_flows_to_retrieval_not_privacy() -> None:
    decision = check_guard("成绩单每份价格是多少")

    assert decision.allowed is True
    assert decision.outcome is None


def test_guard_api_remains_compatible() -> None:
    decision = check_guard("我的成绩是多少")

    assert isinstance(decision, GuardDecision)
    assert decision.allowed is False
    assert decision.outcome is OutcomeCode.PRIVACY_REQUEST
    assert "个人信息" in decision.message
    assert check_guard("国家奖学金", max_chars=2).outcome is OutcomeCode.INVALID_REQUEST
