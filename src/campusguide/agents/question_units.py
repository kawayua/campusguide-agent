"""Grounded question decomposition; no corpus answers or case identifiers."""

from __future__ import annotations

import re

from campusguide.agents.plan import QuestionUnit
from campusguide.retrieval.profile_expand import ProfileLexicon

_CERTIFICATE_AWARD_VERB = r"(?:发|颁发|发放|授予|领取|获得|拿到|取得|拿|给)"
# 明/件/券/据/词/人/物/言 are compounds headed by 证 (证明、证件、证券、证据、
# 证词、证人、证物、证言), not certificate nouns. A bare “发什么证” or
# “颁发哪种证书” still matches because the lookahead only rejects those tails.
_CERTIFICATE_NOUN = r"证(?!明|件|券|据|词|人|物|言)"
_CERTIFICATE_AWARD_ASK = re.compile(
    _CERTIFICATE_AWARD_VERB
    + r"[^。；，,？?]{0,6}(?:什么|哪种|哪些|啥|何种)"
    + _CERTIFICATE_NOUN
    + r"|"
    + _CERTIFICATE_AWARD_VERB
    + r"[^。；，,？?]{0,6}"
    + _CERTIFICATE_NOUN
    + r"[^。；，,？?]{0,4}(?:吗|么|嘛|呢|否)"
    + r"|(?:是否|有没有|会不会|能否|能不能|可不可以)"
    + r"[^。；，,？?]{0,6}"
    + _CERTIFICATE_AWARD_VERB
    + r"[^。；，,？?]{0,6}"
    + _CERTIFICATE_NOUN
)

_REGISTRY_PLATFORM = r"(?:学信网|学信档案)"
_REGISTRY_ACTION = r"(?:备注|标注|备案|登记|注册|显示|体现|注明|记录)"
_REGISTRY_UNCERTAIN = (
    r"(?:不详|未知|未说明|未明确|不明确|不确定|待确认|无法确认|未能确认"
    r"|另行通知|未公布|未提供|暂无信息)"
)
# A platform registration/annotation statement only answers the asked aspect
# when it is definite. The aspect name ("学信网备案") deliberately differs from
# the common body wording "学信网备注" so this check is not bypassed by the
# literal aspect-name fallback in coverage._aspect_supported.
_REGISTRY_CLAIM_PATTERN = (
    _REGISTRY_PLATFORM
    + r"[^。；]{0,16}"
    + _REGISTRY_ACTION
    + r"(?![^。；]{0,8}"
    + _REGISTRY_UNCERTAIN
    + r")"
)

# Each aspect is independently checked after claim verification.
ASPECTS: tuple[tuple[str, str], ...] = (
    ("核心课程", r"核心课"),
    ("实践课程", r"实践课程|实践环节|集中实践|实践教学(?!基地|中心)|实践要求"),
    ("实践基地", r"实践(?:教学)?基地|工程实践(?:教育)?中心"),
    ("选修课程", r"选修|科目"),
    ("毕业设计", r"毕业设计|毕业论文"),
    ("培养目标", r"培养目标"),
    ("课程设置", r"课程设置|课程安排|课程体系"),
    ("毕业要求", r"毕业要求"),
    ("毕业学分", r"毕业.{0,8}学分|总学分|毕业学分"),
    ("学制", r"学制"),
    ("开课学期", r"春季|秋季|夏季|冬季|开课学期|第[一二三四五六七八九十\d]+学期|上学期|下学期"),
    ("授予学位", r"授予.{0,6}学位|什么学位"),
    ("研究方向", r"研究方向"),
    ("申请条件", r"条件|资格|适用|哪些学生|哪些作品|申报范围"),
    ("办理程序", r"如何申请|如何办理|怎样申请|怎样办理|办理程序|什么程序|申请流程|怎么申请"),
    ("提交材料", r"材料|提交|交什么"),
    ("截止时间", r"截止"),
    ("时间安排", r"时间安排|日程安排|工作日程"),
    (
        "公示方式",
        r"公示.*方式|方式.*公示"
        r"|(?:官网|网站|网页|公众号)[^。；]{0,24}(?:发布|公布|公示)"
        r"|(?:附件|名单)[^。；]{0,24}(?:发布|公布|公示)"
        r"|(?:发布|公布)[^。；]{0,24}(?:名单|结果|附件)",
    ),
    ("成绩记载", r"成绩.*记载|成绩.*登记"),
    ("证书", r"证书|结业证|" + _CERTIFICATE_AWARD_ASK.pattern),
    ("学信网备案", _REGISTRY_CLAIM_PATTERN),
    ("定义区别", r"区别|定义"),
    ("负责人", r"谁|负责人|主任|院长|书记"),
    ("师资归属", r"哪个系|哪.*教师|老师吗|老师嘛"),
    ("发布日期", r"哪天发布|何时发布|什么时候发布|发布于|哪天|发布日期"),
)


# A property ask (“什么时候完成/是否需要缴费”) is not the same as mentioning the
# object. Decompose and coverage share these predicates so an aspect is added
# only when the question actually asks that property.
PAYMENT_ASK = re.compile(
    r"(?:是否|需不需要|要不要|用不用|能不能|可不可以|能否|会不会|必须|需要|需|须)"
    r"[^。；，,？?]{0,8}(?:缴费|交费|收费|缴纳|交纳|付费|交钱|收钱)"
    r"|(?:缴费|交费|收费|缴纳|付费)[^。；，,？?]{0,4}(?:吗|么|否|不)"
)
DEGREE_ASK = re.compile(
    r"(?:是否|能否|能不能|可不可以|会不会|有没有|需不需要)"
    r"[^。；，,？?]{0,8}(?:授予|获得|取得|拿到|颁发)[^。；，,？?]{0,4}学位"
    r"|(?:授予|获得|取得|颁发)[^。；，,？?]{0,4}学位[^。；，,？?]{0,4}(?:吗|么|否|不)"
)
SUBMISSION_ASK = re.compile(
    r"(?:是否|需不需要|要不要|用不用|必须|需要|需|须|应)"
    r"[^。；，,？?]{0,10}(?:提交|递交|上交|报送|交材料|交论文|交报告)"
    r"|(?:提交|递交|上交|报送)[^。；，,？?]{0,6}(?:吗|么|否|不)"
)
TIMING_ASK = re.compile(
    r"(?:什么|何时|啥)(?:时候)?"
    r"(?:开始|结束|完成|提交|递交|进行|安排|办理|申请|报名|开课|上课|考试|答辩|毕业"
    r"|评选|评审|举行|举办|召开|报到|入学|选课)"
    r"|(?:开始|结束|完成|提交|递交|进行|安排|办理|申请|报名|答辩|考试|举行|举办|召开)"
    r"(?:在|于)?(?:什么|哪一?)(?:时候|时间|学期)"
    r"|(?:完成|提交|答辩|考试|申请|报名)时间"
    r"|第几学期|时间安排|日程安排|工作日程"
)
DURATION_ASK = re.compile(
    r"多久|多长时间|多少时间|多少年|多少个月|多少天|几年|几个月|几天"
    r"|(?:学制|修业年限|基本修业年限|培养年限|学习年限|任期|服役期)"
    r"[^。；，,？?]{0,6}(?:多长|多久|几年|多少年|多少个月|多长时间)"
)
# Only an explicit semester choice is a separate asked aspect; a background
# mention such as “秋季课程的学分” stays ordinary scope.
SEMESTER_ASK = re.compile(
    r"(?:春季?|秋季?)[^。；，,？?]{0,4}(?:还是|或|或者)[^。；，,？?]{0,4}(?:春季?|秋季?)"
    r"|(?:哪个|哪一个|哪一)学期(?:开|开课|上课|开设|开始|授课)?"
    r"|(?:开课|上课|开设|授课)学期(?:是|为|在)?(?:什么|哪|几)"
)
BOUND_MAX_ASK = re.compile(r"最多|至多|最高|上限|不超过|不得超过|不大于|封顶")
BOUND_MIN_ASK = re.compile(r"最少|至少|最低|下限|不少于|不得低于|不低于")
SELECTION_NEG_ASK = re.compile(r"未选修|没选修|没有选修|未选|没选|没有选|不选|未修读|没有修读")
SELECTION_WORD = re.compile(r"选修|选课|必修")

_MEMBER_ASK = re.compile(r"名单|人选|成员|人员|哪些人|哪位|哪一位|是谁|为谁")
_WHO_ROLE = re.compile(r"谁(?:负责|担任|主管|分管|管理|领导|是|来)|(?:是|为)谁")
_ROLE_TERM = re.compile(
    r"负责人|主任|院长|书记|领导|导师|老师|责任人|委员(?!会)|成员|人员"
    r"|联络员|秘书|组长|队长|主席"
)


def _role_question(text: str) -> bool:
    if not (_WHO_ROLE.search(text) or _MEMBER_ASK.search(text)):
        return False
    return bool(
        _ROLE_TERM.search(text)
        or re.search(r"谁(?:负责|担任|主管|分管|管理|领导)", text)
    )


def _material_question(text: str) -> bool:
    asks = (
        re.search(
            r"(?:哪些|什么|哪几|几份|是否需要|需不需要|要不要|需要|需|应|须|必须)"
            r"[^。；，,？?]{0,10}材料",
            text,
        )
        or re.search(
            r"材料[^。；，,？?]{0,8}(?:有哪些|包括哪些|包括什么|清单|目录|是什么|吗|呢)",
            text,
        )
        or re.search(
            r"(?:提交|递交|上交|准备|提供|携带|补交)[^。；，,？?]{0,8}"
            r"(?:哪些|什么|材料清单|哪些材料|什么材料)",
            text,
        )
        or SUBMISSION_ASK.search(text)
    )
    if not asks:
        return False
    # 材料科学与工程等专业名只是对象，不是在问提交材料。
    return not (re.search(r"材料(?:科学|成型|物理|化学|类|专业)", text) and not re.search(r"提交|申请|材料清单|哪些材料|什么材料|需要[^。；，,？?]{0,6}材料", text))


def _certificate_question(text: str) -> bool:
    award_ask = bool(_CERTIFICATE_AWARD_ASK.search(text))
    if "证书" not in text and "结业证" not in text and not award_ask:
        return False
    if re.search(
        r"(?:提交|递交|上交|携带|复印件|材料|加分)[^。；，,？?]{0,8}证书", text
    ) and not (
        award_ask
        or re.search(
            r"(?:颁发|发放|授予|领取|获得|拿到|取得)[^。；，,？?]{0,8}证书", text
        )
    ):
        return False
    if award_ask:
        return True
    return bool(
        re.search(
            r"(?:颁发|发放|授予|领取|获得|拿到|取得|有没有|是否有|是否发|什么|哪些|哪种)"
            r"[^。；，,？?]{0,8}证书",
            text,
        )
        or re.search(
            r"证书[^。；，,？?]{0,8}(?:是|由|怎么|如何|何时|多久|发放|领取|颁发|授予"
            r"|吗|呢|什么|情况)",
            text,
        )
        or "结业证" in text
    )


_REGISTRY_ASK = re.compile(
    _REGISTRY_PLATFORM
    + r"[^。；，,？?]{0,16}"
    + r"(?:是否|会不会|会否|有没有|有无|能否|能不能|可不可以|可否)"
    + r"[^。；，,？?]{0,6}"
    + _REGISTRY_ACTION
    + r"|"
    + _REGISTRY_PLATFORM
    + r"[^。；，,？?]{0,16}"
    + _REGISTRY_ACTION
    + r"[^。；，,？?]{0,2}(?:吗|么|嘛|呢|否)"
    + r"|"
    + _REGISTRY_PLATFORM
    + r"[^。；，,？?]{0,16}"
    + _REGISTRY_ACTION
    + r"(?:了)?(?:信息|内容|情况|结果)?(?:是)?"
    + r"(?:什么|哪些|哪种|何种|如何|怎么|怎样)"
)


def _registry_question(text: str) -> bool:
    """Only an explicit platform annotation/registry ask, not a background mention."""
    return bool(_REGISTRY_ASK.search(text))


_CREDIT_RATIO_ASK = re.compile(
    # “总学分的多少比例”“总学分占多少百分比”“总学分的80%”：总学分是分母。
    r"总学分[^。；，,？?]{0,12}(?:比例|百分比|占比|百分之|[%％])"
)
_CREDIT_ELIGIBILITY_SCOPE = re.compile(
    r"转专业|转出|转入|准入|门槛|不允许|不得|才能|方可|才可以"
)


def _graduation_credit_question(text: str) -> bool:
    if re.search(r"毕业.{0,8}学分|毕业学分", text):
        return True
    if "总学分" not in text:
        return False
    # “修满总学分的多少比例才允许转专业”问的是资格比例，总学分只是分母。
    if _CREDIT_RATIO_ASK.search(text):
        return False
    if _CREDIT_ELIGIBILITY_SCOPE.search(text) and re.search(
        r"总学分[^。；，,？?]{0,12}(?:最低|不低于|不少于|达到|修满)", text
    ):
        return False
    # 奖助评选条件里的“总学分”是资格门槛，不是培养方案毕业学分。
    return not bool(
        re.search(r"申请|评选|评定|评审|条件|资格|奖助|奖学金|助学金|评优|推免", text)
    )


# “哪些学生组织/社团/机构”是在枚举组织，不是在问学生的申请资格。
# 干部/党员/团员/代表 are student person roles, not organizational compound heads.
_STUDENT_OBJECT_TAIL = re.compile(
    r"[^。；，,？?]{0,2}"
    r"(?:组织|社团|机构|协会|学会|部门|团队|班级|学院|专业|课程|活动|项目|竞赛"
    r"|宿舍|会|群|号)"
)
_STUDENT_PERSON_ROLE = re.compile(r"学生(?:干部|党员|团员|代表)")
_STUDENT_CAN_APPLY = re.compile(
    r"(?:可以|能否|能不能|可不可以|是否可以|有资格|具备资格)"
    r"[^。；，,？?]{0,8}(?:申请|申报|评选|评定|报考|参赛|获得)"
)


def _eligibility_question(text: str) -> bool:
    if re.search(
        r"申请条件|申报条件|评选条件|资格要求|申报范围|适用(?:范围|对象)|哪些作品",
        text,
    ):
        return True
    if _STUDENT_PERSON_ROLE.search(text) and _STUDENT_CAN_APPLY.search(text):
        return True
    if any(
        not _STUDENT_OBJECT_TAIL.match(text, match.end())
        for match in re.finditer(r"哪些学生", text)
    ):
        return True
    if re.search(r"符合条件", text) and not re.search(
        r"什么条件|哪些条件|条件(?:是|有)什么|需要[^。；，,？?]{0,6}条件", text
    ):
        # “符合条件的学生如何申请”里的条件只是范围前缀。
        return False
    return bool(
        re.search(
            r"(?:申请|申报|评选|评定|认定|报考|参赛|评优|资助|推免)"
            r"[^。；，,？?]{0,12}(?:条件|资格|要求|范围)",
            text,
        )
        or re.search(
            r"(?:条件|资格)[^。；，,？?]{0,6}(?:是什么|有哪些|哪些|如何|怎么|要求|吗|呢)",
            text,
        )
        or re.search(r"(?:什么|哪些)[^。；，,？?]{0,8}(?:条件|资格)", text)
        or re.search(r"(?:需要|需|要|符合)[^。；，,？?]{0,10}(?:条件|资格)", text)
    )


def _graduation_design_question(text: str) -> bool:
    if not re.search(r"毕业设计|毕业论文", text):
        return False
    if re.search(
        r"要求|学分|学时|周数|多少周|几周|字数|选题|开题|查重|流程|规定|内容|撰写|评阅|成绩"
        r"|包括(?:哪些|什么)|哪些(?:内容|要求|环节)|什么(?:内容|要求)",
        text,
    ):
        return True
    # 只问时间/时长/是否提交时，毕业论文只是对象，不是单独的要求清单。
    if TIMING_ASK.search(text) or DURATION_ASK.search(text) or SUBMISSION_ASK.search(text):
        return False
    return bool(re.search(r"安排|答辩|提交", text))


_SELECTION_LIST_ASK = re.compile(
    r"选修(?:课程|课|组|模块|方向)?[^。；，,？?]{0,16}"
    r"(?:有哪些|包括哪些|包含哪些|包括什么|有什么|哪些|哪几门|几门|多少门"
    r"|学分|学时|课程设置|要求|科目)"
    r"|(?:哪些|什么|哪几门)科目|科目[^。；，,？?]{0,8}(?:有哪些|哪些|包括)"
)
_SELECTION_STATE_ASK = re.compile(
    r"未选修|没选修|没有选修|未选|没选|不选|未修读"
    r"|已(?:经)?(?:选修|选课|选|修)|选修(?:了|过)"
    r"|(?:选修|必修)[^。；，,？?]{0,6}(?:和|与|或|还是|分别)"
    r"[^。；，,？?]{0,6}(?:选修|必修)"
)


def requested_aspects(text: str) -> list[str]:
    """Only aspects the question actually asks.

    Mentioning a director, a course name or “总学分” inside an eligibility
    threshold is context, not a separate requested aspect.
    """
    aspects: list[str] = []
    for name, pattern in ASPECTS:
        if name == "负责人" and not _role_question(text):
            continue
        if name == "选修课程" and not (
            _SELECTION_LIST_ASK.search(text) or _SELECTION_STATE_ASK.search(text)
        ):
            continue
        if name == "毕业学分" and not _graduation_credit_question(text):
            continue
        if name == "申请条件" and not _eligibility_question(text):
            continue
        if name == "提交材料" and not _material_question(text):
            continue
        if name == "证书" and not _certificate_question(text):
            continue
        if name == "学信网备案" and not _registry_question(text):
            continue
        if name == "毕业设计" and not _graduation_design_question(text):
            continue
        if name == "开课学期" and not SEMESTER_ASK.search(text):
            continue
        if name == "发布日期" and not re.search(
            r"(?:发布|公布|公示|印发|发文)[^。；，,？?]{0,10}"
            r"(?:日期|哪天|何时|什么时候|时间)",
            text,
        ) and not re.search(
            r"(?:哪天|何时|什么时候|发布日期)[^。；，,？?]{0,10}"
            r"(?:发布|公布|公示|印发|发文)",
            text,
        ):
            continue
        if re.search(pattern, text):
            aspects.append(name)
    # 学生干部/党员等个人身份 + 明确“可以申请”就是资格问句，不再要求
    # 句中出现“条件/哪些学生”；“哪些学生组织”类枚举仍被排除。
    if (
        "申请条件" not in aspects
        and _STUDENT_PERSON_ROLE.search(text)
        and _STUDENT_CAN_APPLY.search(text)
        and not any(
            _STUDENT_OBJECT_TAIL.match(text, match.end())
            for match in re.finditer(r"哪些学生", text)
        )
    ):
        aspects.append("申请条件")
    if (
        PAYMENT_ASK.search(text)
        or re.search(
            r"(?:缴费|交费|收费|缴纳)[^。；，,？?]{0,6}(?:吗|么|否)", text
        )
    ) and "费用" not in aspects:
        aspects.append("费用")
    if DEGREE_ASK.search(text) and "授予学位" not in aspects:
        aspects.append("授予学位")
    if TIMING_ASK.search(text) and "时间安排" not in aspects:
        aspects.append("时间安排")
    if "核心课程" in aspects and "课程设置" in aspects:
        aspects.remove("课程设置")
    # 培养方案比较题不需要额外的“定义区别”单元（报告 A4 / 041）。
    if "定义区别" in aspects and re.search(r"培养目标|课程|学分|学制", text):
        aspects.remove("定义区别")
    return aspects


def mentioned_terms(text: str, registry: dict[str, tuple[str, ...]]) -> list[tuple[str, str]]:
    """Longest reliable registered names; short major aliases never match inside words."""
    found: list[tuple[int, int, str, str]] = []
    for key, terms in registry.items():
        for term in terms:
            if len(term) < 2 or (len(term) == 2 and term != terms[0]):
                continue
            for match in re.finditer(re.escape(term), text):
                if term == "农学" and text[match.end() :].startswith("院"):
                    continue
                found.append((match.start(), match.end(), key, term))
    chosen: list[tuple[int, int, str, str]] = []
    for item in sorted(found, key=lambda x: (-(x[1] - x[0]), x[0])):
        if not any(item[0] < other[1] and other[0] < item[1] for other in chosen):
            chosen.append(item)
    return list(dict.fromkeys((key, name) for _, _, key, name in sorted(chosen)))


def cohort_years(text: str) -> list[int]:
    """Include shared suffixes such as 2025、2026版 and 2025—2026级."""
    years: list[int] = []
    for match in re.finditer(r"20\d{2}(?:\s*[、和与及—–-]\s*20\d{2})*\s*(?:年入学|级|版)", text):
        years.extend(int(y) for y in re.findall(r"20\d{2}", match.group()))
    return list(dict.fromkeys(years))


_RANGE_TOKEN = re.compile(r"20\d{2}\s*[—–-]\s*20\d{2}\s*(?:级|版)?")
_YEAR_TOKEN = re.compile(r"20\d{2}\s*(?:级|版)")


def attached_span(text: str, obj: str) -> tuple[int | None, str | None]:
    """Version attached to one object: a single year or an explicit range.

    “2025—2026级工业工程与管理” is one range, not two cohorts (报告 A4).
    """
    for match in re.finditer(re.escape(obj), text):
        head = text[: match.start()]
        ranges = list(_RANGE_TOKEN.finditer(head))
        years = list(_YEAR_TOKEN.finditer(head))
        if ranges and (not years or ranges[-1].end() >= years[-1].start()):
            start, end = re.findall(r"20\d{2}", ranges[-1].group())
            return None, f"{start}-{end}"
        if years:
            token = years[-1]
            before = head[token.start() - 1 : token.start()]
            if before in ("、", "和", "与", "及"):
                # 列举式“2025、2026版”：保留多年，不压成单一年份。
                return None, None
            year = int(re.findall(r"20\d{2}", token.group())[0])
            return year, str(year)
    return None, None


def shared_version_range(text: str) -> str | None:
    """A question-level version range such as “2025—2026级培养方案”.

    Even without a recognized subject, the range is one shared version, not two
    unrelated cohort requirements. Listed years (“2025、2026版”) stay separate.
    """
    ranges = list(_RANGE_TOKEN.finditer(text))
    if len(ranges) != 1:
        return None
    match = ranges[0]
    for year in re.finditer(r"20\d{2}", text):
        if not (match.start() <= year.start() < match.end()):
            return None
    start, end = re.findall(r"20\d{2}", match.group())
    return f"{start}-{end}"


GRADUATE_LEVEL = re.compile(r"研究生|硕士|博士|学硕|专硕|非全日制|MBA|MPA")
UNDERGRADUATE_LEVEL = re.compile(r"本科|学士|大[一二三四五]|专升本")


def mentioned_level(text: str) -> str | None:
    """培养层次：本科 / 研究生；两者同时出现表示比较，不做层次限制。"""
    graduate = bool(GRADUATE_LEVEL.search(text))
    undergraduate = bool(UNDERGRADUATE_LEVEL.search(text))
    if graduate and not undergraduate:
        return "graduate"
    if undergraduate and not graduate:
        return "undergraduate"
    return None


def mentioned_class_types(
    text: str, lexicon: ProfileLexicon | None = None
) -> list[tuple[str, str]]:
    """班型（创新班、实验班、普通班等）；同一专业不同班型培养方案不通用。"""
    # 旧测试/旧调用方构造的 lexicon 可能没有班型词表；缺省按空处理，
    # 不能因此抛 AttributeError 放弃班型拆分。
    class_terms = getattr(lexicon, "class_terms", None) if lexicon is not None else None
    if not class_terms:
        return []
    return mentioned_terms(text, class_terms)


def class_level(class_id: str, lexicon: ProfileLexicon | None) -> str | None:
    if lexicon is None:
        return None
    return lexicon.class_levels.get(class_id)


def decompose_question(text: str, lexicon: ProfileLexicon | None = None) -> list[QuestionUnit]:
    lexicon = lexicon or ProfileLexicon({}, {})
    majors = mentioned_terms(text, lexicon.major_terms)
    colleges = mentioned_terms(text, lexicon.college_terms)
    curriculum = bool(re.search(r"培养|课程|学分|学制|学位|选修|科目|毕业要求", text))
    objects = [name for _, name in (majors if curriculum and majors else colleges)]
    concepts = list(
        dict.fromkeys(
            re.findall(r"微专业|辅修学士学位|辅修专业|国家励志奖学金|国家奖学金|国家助学金", text)
        )
    )
    if len(concepts) > 1:
        objects = concepts
    years = cohort_years(text)
    # 班型（创新班/普通班等）是对象的一部分：同一专业不同班型的培养方案不通用，
    # 比较题必须拆成独立单元，否则一方的结论会被当成另一方的答案。
    class_slots: list[str | None] = [
        class_id for class_id, _ in mentioned_class_types(text, lexicon)
    ] or [None]
    aspects = requested_aspects(text)
    if not aspects:
        aspects = [text.strip("？?。 ")]
    shape = "fact"
    if re.search(r"哪些|分别|分别是|有什么", text):
        shape = "list"
    if re.search(r"区别|差异|不同|变化", text):
        shape = "comparison"
    if re.search(r"多少个|多少位|共.*多少", text):
        shape = "count"
    if re.search(r"最近|最新", text):
        shape = "latest"
    if re.search(r"流程|办理|程序", text):
        shape = "procedure"
    # Build object x version x aspect, with one shared original source span.
    # Versions with explicitly attached different objects are not cross-multiplied.
    subjects: list[tuple[str, int | None, str | None]] = []
    range_version = shared_version_range(text)
    for obj in objects or [""]:
        obj_years = years
        version: str | None = None
        attached: str | None = None
        if obj:
            year, attached = attached_span(text, obj)
            if attached is not None:
                obj_years = [year] if year is not None else [None]
                version = attached
        if version is None and range_version is not None and attached is None:
            # “2025—2026级” is one shared plan version even without a subject.
            obj_years = [None]
            version = range_version
        if version is None and len(objects) > 1 and len(years) > 1:
            local = re.search(r"(20\d{2})\s*(?:版|级).*?" + re.escape(obj), text)
            if local and not any(other in local.group() for other in objects if other != obj):
                obj_years = [int(local.group(1))]
        subjects.extend((obj, year, version) for year in obj_years or [None])
    units: list[QuestionUnit] = []
    audience = [word for word in ("本科", "研究生") if word in text]
    level = mentioned_level(text)
    for obj, year, version in subjects:
        for class_id in class_slots:
            for aspect in aspects:
                units.append(
                    QuestionUnit(
                        unit_id=f"u{len(units) + 1}",
                        original_span=text,
                        subject=obj,
                        objects=[obj] if obj else [],
                        audience_scope=audience or ([level] if level else []),
                        cohort=year,
                        version=version or (str(year) if year else None),
                        class_type=class_id,
                        requested_aspects=[aspect],
                        answer_shape=shape,
                    )
                )
    # No silent deletion: excessive complexity is explicitly retained as a missing slot.
    if len(units) > 12:
        units = [
            *units[:11],
            QuestionUnit(
                unit_id="u12",
                original_span=text,
                requested_aspects=["超出本轮预算的其余对象/方面"],
                required_slots=["请拆分问题"],
                ambiguity="UNIT_BUDGET_EXCEEDED",
            ),
        ]
    return units


_GRADUATING_RE = re.compile(r"(20\d{2})\s*(?:届|年(?:应届)?毕业)")
_ADMISSION_RE = re.compile(
    r"(20\d{2})\s*年?\s*(?:本科|硕士|博士|研究生|推免)?\s*(?:招生|招考|推免)"
)
_ACADEMIC_YEAR_RE = re.compile(r"(20\d{2})\s*[-—–至]\s*(20\d{2})\s*学年")


def _single_year(values: set[int]) -> int | None:
    return next(iter(values)) if len(values) == 1 else None


def query_profile(text: str, lexicon: ProfileLexicon) -> dict:
    """从问题中抽取用户画像。

    “2027届”是毕业届别，“2026年博士招生”是招生年度，“2025-2026学年”是
    学年，三者都不能复用 intake_year（入学年），否则跨年度文件会被错误过滤。
    """
    profile: dict = {}
    graduating = {int(year) for year in _GRADUATING_RE.findall(text)}
    graduating_year = _single_year(graduating)
    if graduating_year is not None:
        profile["graduating_year"] = graduating_year
    admission = {int(year) for year in _ADMISSION_RE.findall(text)}
    admission_year = _single_year(admission)
    if admission_year is not None:
        profile["admission_year"] = admission_year
    academic = _ACADEMIC_YEAR_RE.search(text)
    if academic:
        profile["academic_year"] = f"{academic.group(1)}-{academic.group(2)}"

    curriculum = bool(re.search(r"培养|课程|学分|专业|选修|毕业|学制|科目", text))
    if curriculum and "graduating_year" not in profile and "admission_year" not in profile:
        years = set(cohort_years(text))
        if len(years) == 1:
            profile["intake_year"] = int(next(iter(years)))
    if curriculum:
        majors = mentioned_terms(text, lexicon.major_terms)
        if len(majors) == 1:
            profile["majors"] = [majors[0][0]]
        class_types = mentioned_class_types(text, lexicon)
        if len(class_types) == 1:
            profile["class_types"] = [class_types[0][0]]
    return {"profile": profile} if profile else {}
