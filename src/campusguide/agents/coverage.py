"""Independent coverage audit over final verified claims and validated flow facts."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field, replace

from campusguide.agents.advisor import build_evidence_block
from campusguide.agents.coverage_actions import requirement_answered
from campusguide.agents.coverage_context import (
    Population,
    filter_population_branches,
    population_conflicts,
    scan_population,
)
from campusguide.agents.coverage_study_duration import (
    StudyDurationField,
    study_duration_fields,
    study_duration_supported,
)
from campusguide.agents.plan import QuestionUnit
from campusguide.agents.providers import ChatProvider, ChatProviderError
from campusguide.agents.question_units import (
    ASPECTS,
    BOUND_MAX_ASK,
    BOUND_MIN_ASK,
    DEGREE_ASK,
    PAYMENT_ASK,
    SELECTION_NEG_ASK,
    SELECTION_WORD,
    SEMESTER_ASK,
    SUBMISSION_ASK,
    TIMING_ASK,
)
from campusguide.domain.models import ChecklistItem, Claim, Evidence
from campusguide.domain.text import normalize_text
from campusguide.observability.errors import safe_error_summary
from campusguide.retrieval.profile_expand import ProfileLexicon
from campusguide.verification.coverage_items import (
    missing_source_items,
    required_source_items,
)

_ASPECT_PATTERNS = dict(ASPECTS)

COVERAGE_PROMPT_VERSION = "coverage-2026-10-30"
SYSTEM_PROMPT = """你是答案完整性核对器，独立于答案生成器。检查已通过事实核对的最终结论与有效流程清单，是否完整回答原问题。
“没有涉及”“无法回答”“证据不足”等边界句不能算对应事实已答，不得因一条claim很长就忽略其中明确缺口。
不能把检索到证据等同于已经回答，不能相信生成器自报的unanswered_questions或unit_ids。
按照每个对象×版本×班型×方面检查。同一专业不同班型（创新班/普通班）互不覆盖。
列表应覆盖提供原文中用户所问范围的必要项目，比较应涵盖每个对象和方面。
“官网以附件形式发布名单”“在学院网站公布结果”与“公示方式”等价，有这类正文即可算答出公示方式。
宽泛制度题检查核心事项，不要求抄录所有相邻条款；问题只问哪些公示，不额外要求公示起止日。
核实“最新”须有集合扫描的日期选择依据；声明的收录范围不得扩展为真实世界所有活动。
可信同源标题/章节可解释学院、系和文件对象；不能补实质金额/期限/第一第二导师关系。
只把已验证claims或清单中实际提供的内容算作已答。每个answered单元必须列出支持它的claim_id或有效flow:step_id。
若证据足够但答案遗漏：generation_missing；没证据：missing_evidence；未通过引用/语义核对：verification_failed。
不得凭空增设用户没问的强制内容。必要且正确的澄清与有据的边界说明可以单独标出，不以拒答充当回答。
只输出JSON：{"units":[{"unit_id":"u1","status":"answered|missing_evidence|generation_missing|verification_failed|clarification_needed",
"claim_ids":["c1"],"missing_items":["具体遗漏的原问题方面或原文列表项"]}],"unanswered_questions":["原问题仍未覆盖的必要部分"]}。
所有unit必须返回一次，标识只能取输入中的值。没有真实支持标识不得标answered。"""


_LIST_ASPECTS = ("核心课程", "选修课程", "实践课程", "课程设置", "毕业设计")
_NUMBER_ASPECTS = ("毕业学分", "学制", "成绩记载", "截止时间")
_FLOW_ASPECTS = ("办理程序", "提交材料")
_COURSE_NAME = re.compile(
    r"[（(]?[\u4e00-\u9fffA-Za-z]{2,20}(?:课程|设计|论文|实习|实验|训练|实践)[）)]?"
)
_STEP_MARKERS = re.compile(
    r"首先|其次|然后|接着|最后|第一步|第二步|第[一二三四五六七八九十]步|"
    r"提交|携带|填写|登录|办理|领取|审核|公示|报名|申请"
)


_NON_ITEM = re.compile(
    r"设置|包括|开设|共有|均|分别|提供|涵盖|分为|如下|要求|应取得|学分|学时|等$|等等|以及|都有|均有|涉及|包含|相关"
)


def _course_items(text: str) -> list[str]:
    """列举项：课程名通常不以“课程”结尾（如“数据结构”“操作系统”）。

    仅靠 `_COURSE_NAME` 会漏掉大部分真实课程名，导致已答单元被判缺项；
    这里改为按分隔符切分后过滤掉句子骨架，再用数量判断是否是清单。
    """
    items: list[str] = []
    for piece in re.split(r"[、，,；;\n]", text):
        item = piece.strip(" 　.。：:（）()【】[]")
        item = re.sub(r"^[0-9一二三四五六七八九十]+[.、)）]\s*", "", item)
        if not (2 <= len(item) <= 30) or re.search(r"[0-9]", item):
            continue
        if _NON_ITEM.search(item) or re.search(r"是|为|的", item):
            continue
        items.append(item)
    return items


def _has_numbers(text: str) -> bool:
    return bool(re.search(r"\d+(?:\.\d+)?\s*(?:学分|学时|年|个|门|元|%|天|周|小时|分钟)", text))


_LIST_MIN = {
    "核心课程": 2,
    "选修课程": 2,
    "课程设置": 2,
    "实践课程": 1,
}
# These aspects still need concrete content (numbers/steps/list items) even
# when the cited heading carries the aspect name. A mixed-scope heading must
# not let an unrelated clause answer them.
_CONTENT_CHECKED_ASPECTS = (
    set(_LIST_MIN) | set(_NUMBER_ASPECTS) | set(_FLOW_ASPECTS) | {"毕业设计", "定义区别"}
)


_ANS_PROCEDURE_HEAD = re.compile(
    r"(?:办理|申请|申报|审批|操作|工作)?(?:程序|流程|步骤)"
    r"[^。；，,？?]{0,6}(?:包括|如下|分为|分|是|为|[:：])"
)
_ANS_PROCEDURE_STEP = re.compile(
    r"（[一二三四五六七八九十百]+）|\([一二三四五六七八九十百]+\)"
    r"|第[一二三四五六七八九十百]+步|首先|其次|再次|然后|接着|最后"
    r"|[①②③④⑤⑥⑦⑧⑨⑩]|\d+[.、]"
)
_ANS_PROCEDURE_ACTION = re.compile(
    r"提出|提交|递交|上交|报送|填写|填报|签署|审核|审查|审批|登记|发布|公布"
    r"|报名(?!费)|面试|培训|发放|签订|上岗|复核|公示|备案|受理|推荐|领取|办理|申报|申请|通知"
)
_ANS_PROCEDURE_ACTOR = re.compile(
    r"学生|本人|申请人|学院|学校|中心|辅导员|教师|导师|部门|单位|教务处|研究生院|工作人员"
)
# Only ask words are stripped from the question anchor; numbers and negation stay.
_ANS_PROCEDURE_STOP = re.compile(
    r"如何|怎么|怎样|什么|哪些|哪个|是否|可以|能否|需要|应该|应当|分别|具体|相关"
    r"|申请|办理|程序|流程|步骤|材料|时间|限制|要求|规定|办法|本科生|研究生|学生|学校|学院"
    r"|每周|每月|每天|每年|多久|多长时间|以及|或者|还是|并且|岗位|工作|活动"
    r"|的|了|吗|呢|和|与|及|或"
)
_ANS_MATERIAL_ITEM = re.compile(
    r"身份证|学生证|成绩单|申请表|报名表|推荐信|推荐表|个人陈述|个人简历|简历"
    r"|学历证书|学位证书|毕业证书|获奖证书|证明材料|证明信|证件照|照片"
    r"|体检表|政审表|承诺书|委托书|申请书|审批表|登记表|汇总表|档案"
    r"|户口本|银行卡|准考证|录取通知书|论文|作品"
)
_ANS_MATERIAL_PROPERTY = re.compile(
    r"一式[两二三四五六七八九十\d]+份|原件|复印件|扫描件|电子版|纸质版|彩色|黑白"
    r"|加盖[^。；，,？?]{0,6}章|盖章|签字|签名|密封|手写|打印|A4"
)
_ANS_MATERIAL_REVIEW = re.compile(
    r"审核|审查|评审|评分|打分|复核|核查|查验|甄别|考评|考核|评定|初选|复选|遴选|筛选"
    r"|身份验证|身份核验"
)
_ANS_MATERIAL_ROUTE = re.compile(
    r"(?:提交|递交|上交|报送|上传|发送|交)(?:至|到|给)"
    r"|(?:在|于)[^。；，,？?]{0,10}(?:前|之前)(?:提交|递交|报送)?"
    r"|截止(?:时间|日期)|工作日"
)
_ANS_MATERIAL_NONE = re.compile(
    r"(?:无需|无须|不需|不需要|不用|不必)[^。；，,？?]{0,8}"
    r"(?:提交|递交|上交|报送|提供|准备)[^。；，,？?]{0,6}(?:任何)?材料"
    r"|(?:提交|递交|上交|报送|提供|准备)[^。；，,？?]{0,6}材料"
    r"[^。；，,？?]{0,4}(?:无需|无须|不需|不需要|不用|不必)"
    r"|材料[^。；，,？?]{0,6}(?:无需|无须|不需要|不用|不必)(?:再)?(?:提交|递交|提供)"
    r"|(?:没有|无)(?:任何)?(?:需|须)?(?:提交|递交|提供)的?材料"
)
_ANS_MATERIAL_CUE = re.compile(
    r"提交|递交|上交|报送|提供|携带|准备|补交|填写|填报|需|须|应|必须"
    r"|材料[:：]|材料(?:是|为|有)|材料包括|材料如下|材料清单|包括|如下|清单"
)
_ANS_CLAUSE_BOUNDARY = re.compile(r"(?<=[。；;！？!?，,])")
_STEP_ENUM = re.compile(
    r"(?:[（(]\s*[一二三四五六七八九十百]+\s*[）)]"
    r"|第[一二三四五六七八九十百]+步"
    r"|[①②③④⑤⑥⑦⑧⑨⑩]"
    r"|(?<![\d.])\d{1,2}\s*[.、](?!\d))"
)


def _procedure_source_steps(text: str, span: str) -> list[str]:
    """Enumerated step contents from the first hydrated procedure section."""
    for head in _ANS_PROCEDURE_HEAD.finditer(text or ""):
        window = text[head.end() : head.end() + 4000]
        markers = list(_STEP_ENUM.finditer(window))
        if len(markers) < 2 or not _procedure_topic_ok(window, span):
            continue
        cuts = [marker.start() for marker in markers[1:]] + [len(window)]
        steps = [
            re.sub(r"\s+", "", window[marker.end() : cut]).strip(" 　。；;，,、")
            for marker, cut in zip(markers, cuts, strict=True)
        ]
        return [step for step in steps if len(step) >= 2]
    return []


def _procedure_topic_ok(text: str, span: str) -> bool:
    anchors = re.findall(
        r"[\u4e00-\u9fff]{2,}", _ANS_PROCEDURE_STOP.sub(" ", span or "")
    )
    if not anchors:
        return True
    return any(
        fragment[i : i + 2] in text
        for fragment in anchors
        for i in range(len(fragment) - 1)
    )


def _procedure_answer_supported(text: str, span: str) -> bool:
    """Answer-side procedure: concrete actor/action steps, same event as the ask."""
    body = text or ""
    for match in _ANS_PROCEDURE_HEAD.finditer(body):
        start = max(body.rfind(char, 0, match.start()) for char in "。；;！？!?，,") + 1
        window = body[start : match.start() + 500]
        if not _procedure_topic_ok(window, span):
            continue
        if len(_ANS_PROCEDURE_STEP.findall(window)) >= 2 and (
            _ANS_PROCEDURE_ACTION.search(window)
            or _ANS_PROCEDURE_ACTOR.search(window)
        ):
            return True
        actions = set(_ANS_PROCEDURE_ACTION.findall(window))
        if (
            len(actions) >= 2
            and _ANS_PROCEDURE_ACTOR.search(window)
            and re.search(r"[，,；;、]", window)
        ):
            return True
    return False


# Explicit list asks still require names/properties; a route/date alone cannot
# satisfy “哪些材料/材料清单”. “材料提交有哪些要求” is a general requirement ask.
_EXPLICIT_MATERIAL_LIST_ASK = re.compile(
    r"(?:哪些|什么|何种|哪几|几份)[^。；，,？?]{0,8}材料"
    r"|材料[^。；，,？?]{0,4}(?:清单|目录|明细)"
    r"|材料(?:包括|包含)(?:哪些|什么)"
    r"|材料(?:有|是)(?:哪些|什么)"
    r"|(?:提交|递交|上交|准备|提供|携带|补交)"
    r"[^。；，,？?]{0,6}(?:哪些|什么)(?:材料|清单)"
)


def _general_submission_requirement(clause: str, span: str) -> bool:
    return bool(
        span
        and not _EXPLICIT_MATERIAL_LIST_ASK.search(span)
        and _ANS_MATERIAL_ROUTE_BOUND.search(clause)
    )


def _material_answer_supported(text: str, span: str = "") -> bool:
    """Answer-side materials: names/properties/explicit none or a bound general route."""
    for clause in _ANS_CLAUSE_BOUNDARY.split(text or ""):
        clause = clause.strip()
        if not clause:
            continue
        if _ANS_MATERIAL_NONE.search(clause):
            return True
        if _ANS_MATERIAL_REVIEW.search(clause) and not re.search(
            r"(?:需|须|应|必须|请)[^。；，,？?]{0,6}"
            r"(?:提交|递交|上交|报送|提供|携带|准备)"
            r"|(?:提交|递交|上交|报送)[^。；，,？?]{0,6}(?:以下|如下|清单)",
            clause,
        ):
            continue
        items = set(_ANS_MATERIAL_ITEM.findall(clause))
        properties = bool(_ANS_MATERIAL_PROPERTY.search(clause))
        if not items and not properties:
            if _general_submission_requirement(clause, span):
                return True
            continue
        if not items and _ANS_MATERIAL_ROUTE.search(clause):
            # “电子版材料发送至邮箱/5月1日前提交”是提交渠道或时间，不是材料清单。
            continue
        if len(items) >= 2 or properties:
            return True
        if items and _ANS_MATERIAL_CUE.search(clause):
            return True
    return False


def _content_supports(aspect: str, text: str) -> bool:
    """What the claim actually says, not just which keyword it contains (A5)."""
    if aspect == "毕业设计":
        # 020：岗位要求是“6 学分/14 周/第 8 学期”，只写“毕业论文可作为实现途径”
        # 不算答出毕业设计要求。
        return bool(
            re.search(r"\d+(?:\.\d+)?\s*(?:学分|周|学期|字|人)", text)
            or re.search(r"选题|开题|中期检查|答辩|查重|撰写规范|评阅|成绩构成", text)
        )
    if aspect in _LIST_MIN:
        return len(_course_items(text)) >= _LIST_MIN[aspect]
    if aspect in ("截止时间", "发布日期"):
        # “3月1日截止”没有“学分/学时”这类计量单位，旧数字检查会误判。
        return bool(_DATE_TOKEN.search(text))
    if aspect in _NUMBER_ASPECTS:
        return _has_numbers(text)
    if aspect == "提交材料":
        # “根据提交材料审核/初选复选”不是材料清单；保留具名材料与文件属性。
        return _material_answer_supported(text)
    if aspect in _FLOW_ASPECTS:
        return bool(_STEP_MARKERS.search(text))
    if aspect == "定义区别":
        return ("是指" in text or "定义为" in text or "指的是" in text) and len(text) >= 30
    if aspect == "开课学期":
        return bool(_SEMESTER_TOKEN.search(text))
    return True


def _clean_missing(items: list[str], question: str) -> list[str]:
    """Drop wording echoes: the model often repeats the question or a stray fragment."""
    kept: list[str] = []
    squeezed_question = re.sub(r"[\s　、，,。；;：:？?（）()]", "", question or "")
    for item in items:
        text = item.strip()
        squeezed = re.sub(r"[\s　、，,。；;：:？?（）()]", "", text)
        if len(squeezed) < 4:
            continue
        if squeezed and (squeezed == squeezed_question or squeezed in squeezed_question):
            continue
        if text.lstrip().startswith(("）", ")", "、", "，", ",", "：")):
            continue
        kept.append(text)
    return list(dict.fromkeys(kept))


# 具体缺口：带数字/单位或明确的要求类实词。这类缺口说明“确实少答了一块”，
# 不能被反向核对吞掉（035 的学术不端、020 的毕设学分/周数）。
_CONCRETE_GAP = re.compile(
    r"\d|学分|学时|周|学期|年限|元|%|比例|分数线|名额"
    r"|具体|要求|材料|步骤|时间|名单|清单|规定|条件|流程|程序|标准|构成"
)


def split_missing(items: list[str], question: str) -> tuple[list[str], list[str]]:
    """返回 (主题回声, 具体缺口)。回声可以忽略，具体缺口必须保留。"""
    echoes: list[str] = []
    concrete: list[str] = []
    for text in _clean_missing(items, question):
        if _CONCRETE_GAP.search(text):
            concrete.append(text)
        else:
            echoes.append(text)
    return echoes, concrete


_CLAUSE_SPLIT = re.compile(r"[、，,]|以及|或者|及|和|与")
_HEAD_SPLIT = re.compile(r"以及|或者|和|与|及|、")


def _protect_terms(
    text: str, terms: tuple[str, ...] | list[str]
) -> tuple[str, dict[str, str]]:
    """Keep registered names and quoted titles whole while splitting conjunctions."""
    mapping: dict[str, str] = {}
    for index, term in enumerate(
        sorted({term for term in terms if term and len(term) >= 2}, key=len, reverse=True)
    ):
        if term not in text:
            continue
        placeholder = f"\uE000{index}\uE001"
        text = text.replace(term, placeholder)
        mapping[placeholder] = term
    return text, mapping


def _restore_terms(text: str, mapping: dict[str, str]) -> str:
    for placeholder, term in mapping.items():
        text = text.replace(placeholder, term)
    return text


def _split_terms(
    text: str, pattern: re.Pattern[str], protected: tuple[str, ...] = ()
) -> list[str]:
    shadow, mapping = _protect_terms(text, protected)
    return [
        _restore_terms(part, mapping)
        for part in pattern.split(shadow)
        if part.strip()
    ]


_COURSE_CODE = re.compile(
    r"(?:课程|科目)\s*(?:代码|编号|号)?\s*[:：]?\s*[A-Za-z]{0,6}\d{4,}[A-Za-z0-9-]*"
)
_COURSE_TITLE_END = re.compile(
    r"的(?:学分|学时|开课学期|开课|课程性质|性质|考核|成绩|时间|学期|周数|周学时"
    r"|教师|教材|容量|人数|要求)"
    r"|(?:学分|学时|开课学期|课程性质|考核方式|成绩|任课教师|教材|课程容量|选课人数)"
    r"(?=[和与及、，,。；;？?]|$|是|为|多少|什么|怎么|如何)"
)


def _syntactic_course_terms(text: str) -> tuple[str, ...]:
    """Course title anchored by an explicit code, even without quotes.

    “课程12345678信息检索与分析” carries the full title; splitting it on
    “与” would turn half a course name into a mandatory clause.
    """
    terms: list[str] = []
    source = text or ""
    for anchor in _COURSE_CODE.finditer(source):
        match = re.match(
            r"[（(]?([\u4e00-\u9fff][\u4e00-\u9fffA-Za-z0-9（）()·\-]*)",
            source[anchor.end() :],
        )
        if match is None:
            continue
        name = match.group(1)
        cut = _COURSE_TITLE_END.search(name)
        if cut is not None:
            name = name[: cut.start()]
        name = name.strip(" 　的（）()·-")
        if len(name) >= 2:
            terms.append(name)
    return tuple(dict.fromkeys(terms))


_STOP_BIGRAMS = {
    "如何",
    "规定",
    "要求",
    "哪些",
    "什么",
    "是否",
    "分别",
    "怎样",
    "怎么",
    "情况",
    "内容",
    "办法",
    "说明",
    "安排",
    "处理",
    "办理",
    "申请",
    "计算",
}


_META_CLAUSE = re.compile(
    r"分别|各自|涉及|哪些|哪个|哪几|是否|多少|什么|怎样|怎么|如何|何时|哪天|几号|几点"
)
# 比例/百分比/占比是值问法，不能只留下它前面的业务前缀。
_SHARE_ASK = re.compile(r"比例|百分比|占比|%|％")
_DOC_CLAUSE = re.compile(r"管理办法|实施细则|评选办法|补充说明|通知|文件|方案|章程")
_COLON = re.compile(r"[：:]")
_DOC_CONTEXT = re.compile(
    r"查询|关于|根据|依据|按照|详见|参见|细则|办法|规定|通知|方案|简章|章程|公告|指南|意见|文件"
)
_DOC_HEAD = re.compile(r"^(?:华南农业大学|大学|学院|学校)")
# “纪律处分”“规定要求”这类只剩问法成分的碎片，不是需要单独覆盖的方面。
_GENERIC_CLAUSE = re.compile(
    r"^(?:纪律|处分|规定|要求|条件|程序|流程|材料|时间|方式|标准|内容|情况"
    r"|办法|细则|名单|清单|日期|学校|我校|本校|校内|地点|地方|位置|哪里|何处|哪儿"
    r"|又应|应在|上限|下限|最高|最低|最多|最少|至多|至少"
    r"|多长时间|多少时间|多少周|多少年|多久|多长|几周|几日|几天|几|又|应|在|和|与|及|以及|或者)+$"
)
_QUESTION_MARKER = re.compile(
    r"哪些|哪个|哪几|哪一|什么|是否|多少|怎样|怎么|如何|何时|哪天|几号|几点"
    r"|几周|几天|几年|几个月|几份|几名|几位|几个|几门|几次|几项|几学分|几学时"
    r"|多久|多长|谁|列出|吗|呢"
)
# 逗号前的主题只有在带年份/机构/文档等作用域锚点时才不是独立问题。
_SCOPE_ANCHOR = re.compile(
    r"20\d{2}|[\u4e00-\u9fff]{1,8}(?:学院|大学|学校|学部|系|部|处|中心)"
    r"|管理办法|实施细则|评选办法|补充说明|通知|方案|章程|规定|意见|公告|简章|指南"
    r"|奖助|奖励|评优|评议|评审|评选|考核"
)
_DATE_ASK = re.compile(
    r"(?:有|是)?(?:哪些|什么|哪一?个?|哪几)?(?:日期|时间|时候)"
    r"|哪[一几]?天|几号|几点|何时"
)
_DATE_TOKEN = re.compile(
    r"20\d{2}\s*[年./-]\s*\d{1,2}\s*[月./-]\s*\d{1,2}\s*[日号]?"
    r"|20\d{2}\s*年\s*\d{1,2}\s*月"
    r"|\d{1,2}\s*月\s*\d{1,2}\s*[日号]"
    r"|\d{1,2}\s*月|\d{1,2}\s*[日号]|\d{1,2}\s*[:：]\s*\d{2}"
    r"|上午|下午|中午|晚上|周[一二三四五六日天]|星期[一二三四五六日天]"
    r"|学期末|学年末|月底|月末|年底|年初"
)
_ANS_MATERIAL_ROUTE_BOUND = re.compile(
    rf"(?:考生|申请人|申请者|学生|本人|报考者)[^。；，,？?]*(?:{_DATE_TOKEN.pattern})"
    rf"[^。；，,？?]*(?:向|至|到)[^。；，,？?]*(?:研招办|招生办|办公室|教务处"
    rf"|学生处|学院|学校|中心|部门|处|科|室)[^。；，,？?]*"
    rf"(?:提交|递交|上交|报送|提供)[^。；，,？?]*(?:报考|申请|报名|评审|相关)?材料"
)
_DATE_PROPERTY = re.compile(r"(?:起止|日程|安排)")
_DATE_CORE_NOISE = re.compile(r"(?:具体|的|分别|各自|各|是|为|在)")
_AMOUNT_ASK = re.compile(
    r"(?:奖励|奖助|资助|补助|津贴)(?:的)?(?:标准|金额|数额|额度)"
    r"|报名费|考试费|学费|住宿费|费用|金额|数额|额度|价格|收费标准"
    r"|多少(?:钱|元)|几元|单价|每份(?:多少|几元)|标准(?:是|为)?多少"
)
_AMOUNT_PROPERTY = re.compile(
    r"(?:奖励|奖助|资助|补助|津贴)(?:的)?(?:标准|金额|数额|额度)"
    r"|收费标准|费用|金额|数额|额度|价格"
    r"|多少(?:钱|元)|几元|单价|每份(?:多少|几元)|标准(?:是|为)?多少"
)
_AMOUNT_VALUE_ASK = re.compile(
    r"多少(?:钱|元)|几元|单价|每份(?:多少|几元)"
    r"|(?:金额|数额|额度|费用|价格|收费标准|标准)(?:是|为)?多少"
    r"|收费标准"
)
_AMOUNT_CORE_NOISE = re.compile(r"(?:标准|要求|的|各|是|为)")
_AMOUNT_TOKEN = re.compile(r"\d+(?:\.\d+)?\s*(?:万元|元|块|人民币|RMB|￥|¥)")
_AMOUNT_FREE = re.compile(r"免费|免收|不收费|不收取|零元|0\s*元")
_AMOUNT_TIER_WITHIN = re.compile(r"(?:前\s*(\d+)\s*份|(\d+)\s*份(?:以内|之内|内))")
_AMOUNT_TIER_OVER = re.compile(r"(?:超过|超出|多于|高于|以上)\s*(\d+)\s*份")
_AMOUNT_OBJECT = re.compile(
    r"认证费|报名费|考试费|学费|住宿费|培训费|材料费|工本费|打印费"
    r"|成绩单|证明|证书|学历|学位|补助|津贴|奖学金|助学金|奖励|金额|费用"
    r"|[\u4e00-\u9fff]{2,6}费"
)
_PERIOD_MARKERS = ("每人每年", "每人每月", "每生每年", "每生每月")
# “每生每年”与“每人每年”只共享人/生单位；年/月周期仍严格区分。
_PER_STUDENT_PERIOD = re.compile(r"每生每(?=年|月)")
_INSTRUCTION_ONLY = re.compile(
    r"^(?:请|务必|一定|麻烦|需要)?(?:完整|逐一|依次|全部|尽量|只|仅)?"
    r"(?:列出|说明|回答|指出|给出|告诉|介绍|注明)$"
)
_NEGATED_MODIFIER = re.compile(
    r"(?:不要|不得|别|请勿|勿|禁止|不能|不可|不应|不准)"
    r"(?:把|将|让|使)?(?:其他|其余|别的|之外)[^，,。；;！!？?]*"
)
_QUESTION_SKELETON = re.compile(
    r"请问|某项|分别|各自|各是|各为|各|涉及|哪些|哪个|哪几|哪一|什么|怎样|怎么|如何|是否|多少"
    r"|何时|哪天|几号|几点|安排|都有谁|是谁|有谁|哪位|谁|吗|呢"
)
# 在校/在读/在职/在籍 is population identity, not a locative lead-in.
_CLAUSE_LEAD = re.compile(r"^(?:对|对于|关于|在(?!校|读|职|籍|线)|其中)")
_INSTITUTION = re.compile(
    r"[\u4e00-\u9fff]{1,8}(?:学院|大学|学校|学部|系|部|处(?!理|置|于|境)|中心)"
)
_STEM_STRIP = " 　.。：:；;、，,（）()【】[]？?"
_HARD_SPLIT = re.compile(r"[，,？?；;。！!]")
_SHARED_DEADLINE = re.compile(
    r"期限|时限|多久|几天|几日|多少日|多少天|多长时间|什么时候|截止"
)
_AGENCY_ASK_TEXT = re.compile(
    r"谁|哪个|什么(?:部门|机关|单位|机构)|审批|批准|核准|审定|审核|同意"
)
_CONTEXT_PREFIX = re.compile(
    r"(?:时|后|前|期间|情况下|条件下|之前|之后)$"
    r"|^(?:如果|若|假如|倘若|当)"
)
# A leading “我还是本科在校生”/“本科考试中” clause frames the scope; it is not a
# fact the answer must restate. Only the leading comma segment is eligible.
_CONTEXT_SUFFIX = re.compile(
    r"(?:的时候|时候|情况下|条件下|期间|之前|之后|以前|以后|时|后|前|中)$"
)
_FIRST_PERSON_FRAME = re.compile(
    r"^(?:我们|我|本人|自己)"
    r"(?:目前|现在|已经|刚|还|也|都|就|正|正在|想|要|需要|打算|准备|计划|希望|是|在)*"
)
_CONTEXT_SCAFFOLD = re.compile(r"发现|遇到|发生|出现|涉及")
# Distinctive anchors a context frame must still bind to the same claim/source.
_CONTEXT_ANCHOR = re.compile(
    r"本科|硕士|博士|研究生|在校生|毕业生|新生|学生"
    r"|成绩单|证明|证书|学历|学位|课程|专业|学分|学籍|奖学金|助学金|补助|津贴"
    r"|考试|考查|考核|作弊|违纪|违规|打印|办理|申请|查询|补办|领取|平时|期末|期中"
)


def _is_context_clause(part: str) -> bool:
    """Whether a leading segment only frames scope instead of asking a fact.

    A real ask (date/duration/quantity/yes-no/payment/timing) is never
    demoted to a topic, even when it ends in 时/后/中.
    """
    text = (part or "").strip(_STEM_STRIP)
    chars = re.sub(r"[^\u4e00-\u9fffA-Za-z]", "", text)
    if len(chars) < 3:
        return False
    if (
        _QUESTION_MARKER.search(text)
        or _META_CLAUSE.search(text)
        or _DATE_ASK.search(text)
        or _DURATION_ASK.search(text)
        or _NUMERIC_ASK.search(text)
        or SEMESTER_ASK.search(text)
        or BOUND_MAX_ASK.search(text)
        or BOUND_MIN_ASK.search(text)
        or PAYMENT_ASK.search(text)
        or TIMING_ASK.search(text)
    ):
        return False
    if _FIRST_PERSON_FRAME.match(text):
        return True
    return bool(_CONTEXT_SUFFIX.search(text))


# “实行学分制的本科生，……” names the condition/scope before a separate
# question. It is background, not a fact the answer must restate; an explicit
# “是什么意思/适用哪些学生” ask is excluded and stays a real question.
_BACKGROUND_POPULATION = re.compile(
    r"的(?:本科生|专科生|硕士研究生|博士研究生|硕士生|博士生|研究生"
    r"|在校生|毕业生|新生|学生)$"
)
_DEFINITION_ASK = re.compile(
    r"是什么|什么意思|指什么|指的是|定义|含义"
    r"|适用(?:于)?(?:哪些|什么|谁)|哪些学生|什么人"
)


def _is_background_condition(part: str) -> bool:
    text = (part or "").strip(_STEM_STRIP)
    if not text or not _BACKGROUND_POPULATION.search(text):
        return False
    if (
        _QUESTION_MARKER.search(text)
        or _META_CLAUSE.search(text)
        or _DATE_ASK.search(text)
        or _DURATION_ASK.search(text)
        or _NUMERIC_ASK.search(text)
        or SEMESTER_ASK.search(text)
        or BOUND_MAX_ASK.search(text)
        or BOUND_MIN_ASK.search(text)
        or PAYMENT_ASK.search(text)
        or TIMING_ASK.search(text)
    ):
        return False
    return len(_BACKGROUND_POPULATION.sub("", text).strip(_STEM_STRIP)) >= 2


def _is_scope_topic(topic: str) -> bool:
    return _is_context_clause(topic) or _is_background_condition(topic)



def _context_anchors(topic: str) -> tuple[str, ...]:
    return tuple(dict.fromkeys(_CONTEXT_ANCHOR.findall(topic or "")))


def _context_echo(item: str, topics: list[str]) -> bool:
    squeezed = _squeezed(item)
    if not squeezed:
        return False
    return any(
        squeezed in _squeezed(topic)
        for topic in topics
        if _is_scope_topic(topic)
    )


# Population identity is decided by explicit binding, never by a percentage
# of these anchors borrowed from a broad source title.
_POPULATION_CONTEXT_ANCHORS = frozenset(
    {"本科", "硕士", "博士", "研究生", "在校生", "毕业生", "新生", "学生"}
)


def _effective_population(
    business_text: str,
    topics: list[str] | None = None,
) -> Population | None:
    """Resolve the population the unit actually asks about.

    A leading biography is scope, but a group explicitly named by the business
    ask overrides it. A genuine two-group comparison returns an empty
    Population so existing unit/object gates keep the branches separate.
    """
    context_parts: list[str] = []
    business = business_text or ""
    for topic in topics or []:
        if topic and _is_scope_topic(topic):
            context_parts.append(topic)
            business = business.replace(topic, " ")
    if not context_parts:
        parts = [
            part.strip(_STEM_STRIP)
            for part in re.split(r"[，,；;。！!？?]", business)
            if part.strip()
        ]
        if parts and _is_scope_topic(parts[0]):
            context_parts.append(parts[0])
            business = " ".join(parts[1:])
    context_population = scan_population(" ".join(context_parts))
    business_population = scan_population(business)
    if business_population.has_critical and not business_population.is_single_branch:
        return Population()
    if business_population.is_single_branch and (
        business_population.statuses
        or (context_population.has_critical and business_population.levels)
    ):
        return business_population
    if context_population.has_critical and not context_population.is_single_branch:
        return Population()
    if context_population.is_single_branch and context_population.has_critical:
        return context_population
    return None

_SELECTION_HEAD = re.compile(r"以及|或者|并且|而且")
_SELECTION_ASK_TAIL = re.compile(r"成绩|分数|处理|怎么|如何|怎样|后果|结果")
_QUANTITY_HEAD_SCAFFOLD = re.compile(
    r"^(?:相关|有关|上述|该|其)|(?:应|应当|应该|需|需要|须|必须|准备|拟|拟需)+$"
)


def _strip_document_prefix(text: str) -> str:
    """冒号左侧只是文档范围时，不要求结论复述它；左侧本身是问题则保留。"""
    match = _COLON.search(text)
    if match is None:
        return text
    left = text[: match.start()]
    right = text[match.end() :].strip(_STEM_STRIP)
    if not right or _QUESTION_MARKER.search(left) or _DATE_ASK.search(left):
        return text
    if not _DOC_CONTEXT.search(left):
        return text
    return right


def _without_instructions(span: str) -> str:
    """排除修饰和纯操作指令不是必答事实，也不能抢走真正的问句职务。"""
    parts = [
        part.strip(_STEM_STRIP)
        for part in re.split(r"[，,；;。！!？?]", span or "")
        if part.strip()
    ]
    kept: list[str] = []
    changed = False
    for part in parts:
        cleaned = _NEGATED_MODIFIER.sub("", part).strip(_STEM_STRIP)
        if cleaned != part:
            changed = True
        if not cleaned or _INSTRUCTION_ONLY.fullmatch(cleaned):
            changed = True
            continue
        kept.append(cleaned)
    if not changed:
        return span
    return "，".join(kept)


_COURSE_SCOPE_ANCHOR = re.compile(
    r"(?:课程|科目)\s*(?:代码|编号|号)?\s*[:：]?\s*[A-Za-z]{0,6}\d{4,}[A-Za-z0-9-]*\s*$"
)


def _strip_course_scope_prefix(text: str, protected: tuple[str, ...]) -> str:
    """“某届…课程<代码>”只定位课程，不是结论必须复述的方面。"""
    if not text:
        return text
    for term in sorted(
        {term for term in protected if term and len(term) >= 2},
        key=len,
        reverse=True,
    ):
        pos = text.find(term)
        if pos <= 0:
            continue
        prefix = text[:pos].rstrip(_STEM_STRIP)
        if _COURSE_SCOPE_ANCHOR.search(prefix):
            return text[pos:]
    return text


def _clean_shared_head(head: str) -> str:
    value = head.strip(_STEM_STRIP)
    value = re.sub(r"(?:分别|各自|各|都|均|是|为|有)+$", "", value)
    return value.strip(_STEM_STRIP)


def _quantity_residual(text: str) -> str:
    residual = text
    for pattern in (
        _RANGE_VALUE,
        _NUMERIC_ASK,
        _DURATION_ASK,
        TIMING_ASK,
        BOUND_MAX_ASK,
        BOUND_MIN_ASK,
        _DATE_ASK,
        _BOUND_VALUE,
    ):
        residual = pattern.sub(" ", residual)
    residual = re.sub(r"以上|以下|以内|及以上|及以下|至|到|~|～|—|–|-|－", " ", residual)
    return _clean_property_core(residual)


def _shared_tail_clauses(
    segment: str, protected: tuple[str, ...]
) -> list[str] | None:
    """Split coordinated branches and attach the one shared structured ask."""
    meta = _META_CLAUSE.search(segment)
    if meta is None:
        return None
    prefix = segment[: meta.start()].strip(_STEM_STRIP)
    tail = segment[meta.start() :].strip(_STEM_STRIP)
    heads = [
        head
        for head in (_clean_shared_head(part) for part in _split_terms(prefix, _CLAUSE_SPLIT, protected))
        if head
    ]
    if len(heads) < 2:
        return None

    deadline = _SHARED_DEADLINE.search(tail)
    if deadline is None and heads:
        # “向…申诉的期限各是多少” leaves the shared deadline in the last
        # coordinated head before the meta ask; it governs every head.
        deadline = _SHARED_DEADLINE.search(heads[-1])
    if deadline is not None:
        marker = deadline.group(0)
        candidates: list[str] = []
        for head in heads:
            if requirement_answered(head, "") is not None:
                candidates.append(head)
                continue
            candidate = head if marker in head else f"{head}{marker}"
            candidates.append(
                candidate
                if requirement_answered(candidate, "") is not None
                else ""
            )
        if all(candidates):
            clauses = [_normalize_clause(item, protected) for item in candidates]
            if all(clauses):
                return clauses

    if _AGENCY_ASK_TEXT.search(tail):
        ask_tail = re.sub(
            r"^(?:分别|各自|各|都|均|是|为|有)+", "", tail
        ).strip(_STEM_STRIP)
        residuals = [_quantity_residual(head) for head in heads]
        threshold = [
            len(re.sub(r"[^\u4e00-\u9fff]", "", residual)) < 2
            for residual in residuals
        ]
        if any(threshold) and not all(threshold):
            stem = next(
                (
                    residual
                    for residual in residuals
                    if len(re.sub(r"[^\u4e00-\u9fff]", "", residual)) >= 2
                ),
                "",
            )
            candidates = []
            for head, is_threshold in zip(heads, threshold, strict=True):
                base = f"{stem}{head}" if is_threshold and stem else head
                candidate = f"{base}{ask_tail}"
                candidates.append(
                    candidate
                    if requirement_answered(candidate, "") is not None
                    else ""
                )
            if all(candidates):
                clauses = [_normalize_clause(item, protected) for item in candidates]
                if all(clauses):
                    return clauses
    return None


def _structured_clause(segment: str, protected: tuple[str, ...]) -> str:
    candidate = segment.strip(_STEM_STRIP)
    if not candidate or requirement_answered(candidate, "") is None:
        return ""
    return _normalize_clause(candidate, protected)


def _normalize_clause(text: str, protected: tuple[str, ...] = ()) -> str:
    """去掉问法骨架但保留具体对象；“哪些日期”统一成日期要求。"""
    clause = _strip_course_scope_prefix(text.strip(_STEM_STRIP), protected)
    clause = _strip_document_prefix(clause)
    if requirement_answered(clause, "") is not None:
        # Keep the structured ask (谁/什么/期限) intact; the legacy skeleton
        # strip would turn it into an unrecognizable lexical echo.
        chars = re.sub(r"[^\u4e00-\u9fff]", "", clause)
        if len(chars) < 2 or _GENERIC_CLAUSE.match(chars):
            return ""
        if _DOC_CLAUSE.search(clause):
            return ""
        if _DOC_HEAD.match(clause) and len(chars) >= 10:
            return ""
        return clause
    clause = _DATE_ASK.sub("日期", clause)
    clause = _QUESTION_SKELETON.sub("", clause)
    clause = _CLAUSE_LEAD.sub("", clause).strip(_STEM_STRIP)
    chars = re.sub(r"[^\u4e00-\u9fff]", "", clause)
    if len(chars) < 2 or _GENERIC_CLAUSE.match(chars):
        return ""
    if _DOC_CLAUSE.search(clause):
        return ""
    if _DOC_HEAD.match(clause) and len(chars) >= 10:
        return ""
    return clause


def _segment_clauses(segment: str, protected: tuple[str, ...] = ()) -> list[str]:
    """切一个逗号段；问句尾巴只保留它前面的具体对象。"""
    shared = _shared_tail_clauses(segment, protected)
    if shared:
        return shared
    structured = _structured_clause(segment, protected)
    if structured:
        return [structured]
    if _SHARE_ASK.search(segment):
        # A ratio ask owns the action around it (“多少比例后不允许转专业”);
        # truncating at 多少 leaves only the business prefix and loses both the
        # requested value and the consequence that must be verified.
        clause = _normalize_clause(segment, protected)
        if clause:
            return [clause]
    meta = _META_CLAUSE.search(segment)
    if meta is None:
        return [
            clause
            for part in _split_terms(segment, _CLAUSE_SPLIT, protected)
            if (clause := _normalize_clause(part, protected))
        ]
    prefix = segment[: meta.start()].strip(_STEM_STRIP)
    if not prefix:
        # “分别涉及哪些……”只是问法本身，旧行为也不把它当独立方面。
        return []
    heads = _split_terms(prefix, _CLAUSE_SPLIT, protected)
    if heads:
        # “有哪些/是什么”的有/是只是问法骨架，不能粘进具体方面。
        heads[-1] = re.sub(r"[有是]$", "", heads[-1])
    asks_date = bool(_DATE_ASK.search(segment[meta.start() :]))
    if not asks_date and len(heads) >= 2 and _DATE_ASK.search(heads[-1]):
        asks_date = True
    amount = _AMOUNT_ASK.search(heads[-1]) if heads else None
    if amount is None and len(heads) > 1:
        # “报名费：博士生和硕士生……”把金额问在冒号左侧，右侧对象同样在问它；
        # 文档范围左侧（“招生细则：……”）不是金额问法，不参与。
        left_colon = _COLON.search(heads[0])
        left = heads[0][: left_colon.start()] if left_colon else ""
        if left and not _DOC_CONTEXT.search(left):
            amount = _AMOUNT_ASK.search(left)
    amount_label = amount.group(0) if amount and not _DATE_ASK.search(heads[-1]) else None
    period_label = (
        next((marker for marker in _PERIOD_MARKERS if marker in heads[-1]), "")
        if amount_label and heads
        else ""
    )
    clauses: list[str] = []
    for head in heads:
        clause = _normalize_clause(head, protected)
        if not clause:
            continue
        if asks_date and not _DATE_ASK.search(clause):
            clause = f"{clause}日期"
        # 末项才带有金额属性时，前面的并列对象也共享这个属性；
        # 日期项独立结算，不能因为同段出现报名费就要求它带金额。
        if amount_label and not _DATE_ASK.search(clause):
            if period_label and period_label not in clause:
                clause = f"{clause}{period_label}"
            if not _AMOUNT_ASK.search(clause):
                clause = f"{clause}{amount_label}"
        clauses.append(clause)
    return clauses


def _is_topic_part(part: str) -> bool:
    chars = re.sub(r"[^\u4e00-\u9fff]", "", part)
    return (
        len(chars) >= 3
        and not _QUESTION_MARKER.search(part)
        # A duration/quantity/bound/semester ask is an answer target, never a
        # comma-prefix topic. The patterns are defined later in this module;
        # lookup happens at call time.
        and not _DURATION_ASK.search(part)
        and not _NUMERIC_ASK.search(part)
        and not SEMESTER_ASK.search(part)
        and not BOUND_MAX_ASK.search(part)
        and not BOUND_MIN_ASK.search(part)
        and bool(_SCOPE_ANCHOR.search(part))
    )


def _selection_branch(text: str) -> bool:
    return bool(
        SELECTION_NEG_ASK.search(text)
        or _SELECTION_STATE.search(text)
        or _SELECTION_POSITIVE.search(text)
    )


def _attach_selection_outcome(
    parts: list[str],
) -> list[tuple[str, str]]:
    """Attach a shared grade/handling ask to each contrasting selection state."""
    if len(parts) < 2 or _selection_branch(parts[-1]):
        return [(part, "") for part in parts]
    tail = parts[-1]
    if not _SELECTION_ASK_TAIL.search(tail):
        return [(part, "") for part in parts]
    elements: list[tuple[str, str]] = []
    attached = False
    for part in parts[:-1]:
        if _selection_branch(part):
            candidate = f"{part}{tail}"
            if requirement_answered(candidate, "") is not None:
                elements.append((part, candidate))
                attached = True
                continue
        elements.append((part, ""))
    if not attached:
        return [(part, "") for part in parts]
    return elements


def _question_clauses(
    span: str, protected: tuple[str, ...] = ()
) -> tuple[list[str], list[str]]:
    """把“整句问题当方面”的 fallback 单元切成 (子问题, 逗号前缀主题)。

    主题只解释范围，不单独算缺口；它必须能被同源引用标题验证。
    “学生自评与班级评议分别安排在哪些日期”这类并列主语要逐个保留。
    注册专业名和引号内课程名里的“与/及/和”不参与切分。
    独立问句按硬标点逐句核对，不能拼成一条要求。
    """
    shadow, mapping = _protect_terms(span, protected)
    text = _without_instructions(shadow).strip(_STEM_STRIP)
    parts: list[str] = []
    for raw in _HARD_SPLIT.split(text):
        part = _restore_terms(raw.strip(_STEM_STRIP), mapping)
        part = _SELECTION_HEAD.sub("", part, count=1).strip(_STEM_STRIP)
        if part:
            parts.append(part)
    elements = _attach_selection_outcome(parts)
    topics: list[str] = []
    if (
        len(elements) >= 2
        and not elements[0][1]
        and _is_topic_part(elements[0][0])
    ):
        topics.append(elements[0][0])
        elements = elements[1:]
    while (
        len(elements) >= 2
        and not elements[0][1]
        and _is_context_clause(elements[0][0])
    ):
        topics.append(elements[0][0])
        elements = elements[1:]
    while (
        len(elements) >= 2
        and not elements[0][1]
        and requirement_answered(elements[0][0], "") is None
        and not _QUESTION_MARKER.search(elements[0][0])
        and not _META_CLAUSE.search(elements[0][0])
        and not _selection_branch(elements[0][0])
        and _CONTEXT_PREFIX.search(elements[0][0])
        and (
            _QUESTION_MARKER.search(elements[1][0])
            or _META_CLAUSE.search(elements[1][0])
            or requirement_answered(elements[1][0], "") is not None
        )
    ):
        elements = [
            (f"{elements[0][0]}{elements[1][0]}", elements[1][1]),
            *elements[2:],
        ]
    while (
        len(elements) >= 2
        and not elements[0][1]
        and _is_background_condition(elements[0][0])
        and not _DEFINITION_ASK.search(elements[1][0])
        and (
            _SHARE_ASK.search(elements[1][0])
            or _QUESTION_MARKER.search(elements[1][0])
            or _META_CLAUSE.search(elements[1][0])
            or requirement_answered(elements[1][0], "") is not None
        )
    ):
        # A leading condition/population frame is scope for the real ask that
        # follows; the background itself is not a separate required fact.
        topics.append(elements[0][0])
        elements = elements[1:]
    clauses: list[str] = []
    for part, candidate in elements:
        if candidate:
            clause = _normalize_clause(candidate, protected)
            if clause:
                clauses.append(clause)
        else:
            clauses.extend(_segment_clauses(part, protected))
    clauses = list(dict.fromkeys(clauses))
    if len(clauses) < 2 and not topics:
        # An explicit student-definition/audience question is still a real ask
        # even when it is the only clause; do not let unit-level matching
        # answer it. Generic non-population definitions keep the legacy path.
        if any(
            _DEFINITION_ASK.search(part) and scan_population(part).has_critical
            for part in parts
        ):
            return clauses, topics
        # A lone recognized structured ask (appeal/authority/program/selection)
        # is a real clause, not an unknown noun. Keep it so the normal
        # deterministic clause binding can verify or reject the claim.
        if len(clauses) == 1 and requirement_answered(clauses[0], "") is not None:
            return clauses, topics
        return [], topics
    return clauses, topics


# 结论常用同义说法回答同一行为；两字子问题只按字面比对会误判为漏答。
_CLAUSE_ALIASES: dict[str, tuple[str, ...]] = {
    "替考": ("替考", "代考", "代替他人", "代替自己"),
    "作弊": ("作弊", "违纪", "舞弊"),
}
# 仅限明确的自然等价；博士/硕士仍严格区分，也不让“研究生”互相覆盖。
_ACADEMIC_EQUIVALENTS = (
    ("博士研究生", "博士生"),
    ("硕士研究生", "硕士生"),
    ("本科学生", "本科生"),
    ("已毕业学生", "毕业生"),
    ("在校学生", "在校生"),
)


def _canonical_academic_terms(text: str) -> str:
    for full, short in _ACADEMIC_EQUIVALENTS:
        text = text.replace(full, short)
    return text


# “每人每年”与计价单位写法是同义表达；按月计价不能覆盖按年要求。
_PER_PERSON_PERIOD = re.compile(
    r"元\s*[/／]\s*[人生]\s*(?:[·•.．/／\-–—]\s*)?(年|月)"
    r"|元\s*[/／]\s*(年|月)\s*[·•.．/／\-–—]\s*[人生]"
)


def _canonical_unit_terms(text: str) -> str:
    text = _PER_STUDENT_PERIOD.sub("每人每", text)

    def replace(match: re.Match[str]) -> str:
        period = match.group(1) or match.group(2)
        return f"元每人每{period}"

    return _PER_PERSON_PERIOD.sub(replace, text)


def _clause_covered(clause: str, text: str, *, min_ratio: float = 0.6) -> bool:
    """子问题的实词双字组至少六成命中；只命中“学术”不算答出“学术不端”。"""
    clause = _canonical_unit_terms(_canonical_academic_terms(clause))
    text = _canonical_unit_terms(_canonical_academic_terms(text))
    chars = re.sub(r"[^\u4e00-\u9fff]", "", clause)
    aliases = _CLAUSE_ALIASES.get(chars)
    if aliases:
        return any(alias in text for alias in aliases)
    bigrams = [chars[i : i + 2] for i in range(max(0, len(chars) - 1))]
    bigrams = [bigram for bigram in bigrams if bigram not in _STOP_BIGRAMS]
    if not bigrams:
        return True
    hits = sum(1 for bigram in bigrams if bigram in text)
    return hits / len(bigrams) >= min_ratio


_CLAIM_SEGMENT = re.compile(r"[。；;！？!?，,]")

# 必要性/可选性问法：“需要自己选课吗”问的是同一动作是否需要，
# 自己/自行/本人只是执行方式，不是新的业务对象。
_SELF_SCAFFOLD = re.compile(r"自己|自行|本人|亲自|个人")
_NECESSITY_ASK = re.compile(
    r"需不需要|要不要|用不用|是否需要|是否必须|是否要|是否需|需要|必须"
)
_NECESSITY_INTERROGATIVE = frozenset(
    {"需不需要", "要不要", "用不用", "是否需要", "是否必须", "是否要", "是否需"}
)
_NECESSITY_QUESTION_TAIL = re.compile(r"[吗嘛呢]\s*[？?。]?\s*$")
_NECESSITY_TAIL_STOP = re.compile(r"[，,。；;！!？?\n]")
_NECESSITY_PARTICLE = re.compile(r"(?:吗|(?<!什)么|呢|嘛|否|的)\s*$")
_NECESSITY_SUBJECT = re.compile(
    r"^(?:(?:全日制|非全日制)?(?:本科|专科|硕士|博士)(?:新生|学生|生)"
    r"|(?:全日制|非全日制)?(?:硕士|博士)?研究生|新生|学生|同学|大家|我们|你们)+"
)
_NECESSITY_ACTION_LEAD = re.compile(
    r"^(?:去|来|再|还|又|在|于|到|进行|办理|完成)"
)
_NECESSITY_ACTION_STOP = re.compile(
    r"谁|什么|哪个|哪些|如何|怎么|怎样|多久|多长|多长时间|多少|几"
    r"|是否|能否|可否|需不需要|要不要|用不用|需要|需|须|吗|呢"
)
_NECESSITY_EXISTING_PROPERTY = re.compile(
    r"缴费|交费|收费|缴纳|交纳|付费|交钱|提交|递交|上交|报送|材料"
    r"|授予|获得|取得|颁发|学位"
)
_NECESSITY_ECHO = re.compile(
    r"是否|需不需要|要不要|用不用|不一定|未必|可能|也许|或许|好像|疑似"
    r"|尚未|不明确|未明确|未说明|未提及|未规定|未知|不详|不确定|无法确定|证据不足"
    r"|[吗嘛呢]\s*$"
)
_NECESSITY_NEGATIVE = re.compile(
    r"(?<!并非)(?<!不是)(?:不是需要|并非需要|不太需要|不怎么需要"
    r"|不需要|不必须|无需|无须|不需|不须|不必|不用|毋须)"
)
_NECESSITY_AFFIRM = re.compile(
    r"(?<![不无须毋未非])(?:必须要|必须|需要|需|须|应当|应该|应|要)"
)
_NECESSITY_WRONG_BEFORE = re.compile(
    r"退|停|免|改|补|重|旁听|试听|听课|退课|停课|免修|重修|改选"
)
# Normalized clauses drop “吗/是否”, so a bare generic verb after 自己 is not
# enough to prove the ask was about whether that verb is required.
_NECESSITY_BARE_GENERIC = frozenset(
    {"准备", "办理", "进行", "完成", "处理", "安排", "提供", "说明"}
)


def _necessity_clean_tail(tail: str) -> str:
    text = _NECESSITY_PARTICLE.sub("", tail.strip(_STEM_STRIP)).strip(_STEM_STRIP)
    text = _SELF_SCAFFOLD.sub("", text)
    text = _NECESSITY_SUBJECT.sub("", text)
    text = _NECESSITY_ACTION_LEAD.sub("", text)
    return text.strip(_STEM_STRIP)


def _necessity_action(requirement: str) -> str | None:
    text = requirement or ""
    match = _NECESSITY_ASK.search(text)
    if match is None:
        return None
    # Scope the new semantics to asks that actually carry a self-performance
    # marker. A plain “是否需要X” is already covered by the legacy matcher
    # (the need word remains in both branches); intercepting it here would
    # duplicate payment/submission properties and change existing behavior.
    if not _SELF_SCAFFOLD.search(text):
        return None
    interrogative = match.group(0) in _NECESSITY_INTERROGATIVE
    question_tail = bool(_NECESSITY_QUESTION_TAIL.search(text))
    tail = _NECESSITY_TAIL_STOP.split(text[match.end() :])[0]
    action = _necessity_clean_tail(tail)
    if not (2 <= len(action) <= 16):
        return None
    if not re.fullmatch(r"[\u4e00-\u9fffA-Za-z]+", action):
        return None
    if "的" in action:
        return None
    if not interrogative and not question_tail and action in _NECESSITY_BARE_GENERIC:
        return None
    if _NECESSITY_ACTION_STOP.search(action):
        return None
    if _NECESSITY_EXISTING_PROPERTY.search(action):
        return None
    return action


def _necessity_response_matches(action: str, tail: str) -> bool:
    text = _necessity_clean_tail(tail)
    position = text.rfind(action)
    if position < 0:
        return False
    before = text[:position]
    after = text[position + len(action) :].strip(_STEM_STRIP)
    if after and not re.fullmatch(r"(?:了|的|啊|呀|吧|哦)", after):
        return False
    return not _NECESSITY_WRONG_BEFORE.search(before)


def _necessity_answered(action: str, answer: str) -> bool:
    """Explicit need/no-need for the same action; echoes and unknowns do not count."""
    for part in _CLAIM_SEGMENT.split(answer or ""):
        part = part.strip()
        if not part or _UNCERTAIN.search(part) or _NECESSITY_ECHO.search(part):
            continue
        for pattern in (_NECESSITY_NEGATIVE, _NECESSITY_AFFIRM):
            for match in pattern.finditer(part):
                if _necessity_response_matches(action, part[match.end() :]):
                    return True
    return False


def _date_core(clause: str) -> str:
    core = _DATE_ASK.sub("", clause)
    core = _DATE_PROPERTY.sub("", core)
    core = _DATE_CORE_NOISE.sub("", core)
    return core.strip(_STEM_STRIP)


def _amount_core(clause: str) -> str:
    core = _AMOUNT_PROPERTY.sub("", clause)
    core = _AMOUNT_CORE_NOISE.sub("", core)
    return core.strip(_STEM_STRIP)


def _required_markers(text: str) -> tuple[str, ...]:
    """单一级别人群或计价周期必须在同一结论分句出现，不能被另一级凑数。"""
    levels = [term for term in ("博士", "硕士", "本科") if term in text]
    periods = [marker for marker in _PERIOD_MARKERS if marker in text]
    markers: list[str] = []
    if len(levels) == 1:
        markers.append(levels[0])
    if len(periods) == 1:
        markers.append(periods[0])
    grade = re.search(r"[一二三四五六七八九十\d]+年级", text)
    if grade is not None:
        # “一年级学生人数”是年级口径，不是时间单位；同数字的其他年级不能顶替。
        markers.append(grade.group(0))
    return tuple(markers)


def _scoped_token_ok(
    core: str,
    text: str,
    token: re.Pattern[str],
    *,
    min_ratio: float = 0.6,
    required: tuple[str, ...] = (),
    exclude: tuple[str, ...] = (),
) -> bool:
    """A value token must sit in the same clause as the asked action/object."""
    if not token.search(text):
        return False
    if not re.sub(r"[^\u4e00-\u9fffA-Za-z]", "", core):
        return True
    segments = [
        part
        for part in _CLAIM_SEGMENT.split(text)
        if part.strip()
        and token.search(part)
        and all(term in part for term in required)
        and not any(term and term in part for term in exclude)
        and not _person_conflict(core, part)
    ]
    return any(
        _clause_covered(core, part, min_ratio=min_ratio) for part in segments
    )


def _clause_content_ok(clause: str, text: str) -> bool:
    """Dates and amounts are properties; the claim body must state them.

    The property word itself (“日期”“奖励标准”) need not be repeated, but the
    action/object and the concrete token must appear in the same claim clause.
    Titles are not passed here, so they can never supply the value.
    """
    necessity = _necessity_action(clause)
    if necessity is not None:
        return _necessity_answered(necessity, text)
    structured = requirement_answered(clause, text)
    if structured is not None:
        return structured
    clause = _canonical_unit_terms(_canonical_academic_terms(clause))
    text = _canonical_unit_terms(_canonical_academic_terms(text))
    if _DATE_ASK.search(clause):
        core = _date_core(clause)
        return _scoped_token_ok(
            core,
            text,
            _DATE_TOKEN,
            min_ratio=0.75,
            required=_required_markers(core),
        )
    if _AMOUNT_ASK.search(clause):
        clause = _canonical_fee_text(clause)
        text = _canonical_fee_text(text)
        core = _amount_core(clause)
        return _scoped_token_ok(
            core, text, _AMOUNT_TOKEN, required=_required_markers(core)
        )
    if _SHARE_ASK.search(clause):
        core = _SHARE_ASK.sub(" ", clause)
        core = _QUESTION_SKELETON.sub("", core)
        core = _DATE_ASK.sub("", core)
        core = _CLAUSE_LEAD.sub("", core).strip(_STEM_STRIP)
        return _scoped_token_ok(
            core, text, _RATIO_VALUE, required=_required_markers(core)
        )
    return _clause_covered(clause, text)


@dataclass(frozen=True)
class _Property:
    """A property the question asks for, with its object/action binding."""

    kind: str
    cue: str
    core: str = ""
    direction: str = ""
    measure: str = ""
    calendar: bool = False
    polarity: str = ""
    action: str = ""
    required: tuple[str, ...] = ()
    ask: str = ""
    exclude: tuple[str, ...] = ()
    business: tuple[str, ...] = ()


_CALENDAR_ASK = re.compile(r"日期|几月|几号|哪天|哪一天|几时")
_CALENDAR_TOKEN = re.compile(
    r"20\d{2}\s*[年./-]\s*\d{1,2}\s*[月./-]\s*\d{1,2}\s*[日号]?"
    r"|20\d{2}\s*年\s*\d{1,2}\s*月"
    r"|\d{1,2}\s*月\s*\d{1,2}\s*[日号]"
    r"|\d{1,2}\s*月|\d{1,2}\s*[日号]|\d{1,2}\s*[:：]\s*\d{2}"
    r"|上午|下午|中午|晚上|周[一二三四五六日天]|星期[一二三四五六日天]"
)
# Relative anchors (第五学期/学期末) answer “什么时候完成/时间安排”, but an
# explicit 日期 ask must be answered with an actual calendar token.
_TIMING_TOKEN = re.compile(
    rf"(?:{_DATE_TOKEN.pattern})"
    r"|第[一二三四五六七八九十\d]+(?:学期|学年)"
    r"|[一二三四五六七八九十\d]+年级(?:[上下]学期)?"
    r"|(?:入学|开学|毕业|期末|学年|学期|考试|答辩)(?:前|后|时|期间|之前|之后|以前|以后|末|初)"
    r"|毕业前|毕业后|入学前|入学后|开学前|开学后|下学期|上学期|本学期|下个月|本月"
)
_SEMESTER_TOKEN = re.compile(
    r"春季|秋季|夏季|冬季|春学期|秋学期"
    r"|第[一二三四五六七八九十\d]+学期|上学期|下学期|本学期|每学期|学期"
)
# 一年级是年级口径，不是修业时长；只在年级问法里出现时不能当 duration 单位。
_GRADE_SCOPE = re.compile(r"[一二三四五六七八九十\d]+年级")
# The action inside a timing ask ("什么时候提交") stays part of the core so a
# date for a different action cannot answer it. “时间安排/日程安排” are labels,
# not the asked action, and must not become required words.
_TIMING_ACTION = re.compile(
    r"开始|结束|完成|提交|递交|进行|办理|申请|报名|答辩|考试|举行|举办|召开"
    r"|开课|上课|报到|入学|选课|毕业|评选|评审"
)
# Only explicit competing actions reject a timing answer; “第五学期结束前” is a
# relative anchor, and an action-free date may still imply the asked completion.
_TIMING_COMPETITORS = ("提交", "递交", "完成", "结束", "开始", "办理", "进行")
_TIMING_EQUIVALENT = {
    "提交": ("提交", "递交", "上交", "报送"),
    "递交": ("递交", "提交", "上交", "报送"),
    "完成": ("完成", "结束"),
    "结束": ("结束", "完成"),
    "开始": ("开始", "启动"),
    "办理": ("办理", "进行"),
    "进行": ("进行", "办理"),
}
_TIMING_ANCHOR = re.compile(
    r"(?:学期|学年|期末|毕业|开学|入学)(?:末|初|结束|开始)?(?:前|后|之前|之后)?"
)
# A calendar year (“2026年”) is not a duration; neither is the month part of
# a date (“2026年9月”).
_DURATION_TOKEN = re.compile(
    r"(?!20\d{2}\s*年)(?<![\d年第月])\d+(?:\.\d+)?\s*(?:个)?"
    r"(?:年|月|天|日|工作日|自然日|周|学期|学年|小时|分钟)"
    r"|(?<!第)(?:[一二三四五六七八九十两]+|半)(?:个)?"
    r"(?:年|月|天|日|工作日|自然日|周|学期|学年|小时|分钟)"
    r"|满[一二三四五六七八九十\d]+(?:个)?(?:学期|学年|年|月)"
)
_VALUE_MEASURE = re.compile(
    r"工作日|自然日|学时|学分|小时|分钟|学期|天|周|次|门|人|"
    r"个(?!\s*(?:工作日|自然日|学时|学分|小时|分钟|学期|天|周|次|门|人|名|位|份|页|字|元|比例|名额|年|月))"
    r"|元|份|页|字|名|位|%|％|比例|名额|年|月"
)
_RATIO_ASK = re.compile(
    r"(?:比例|百分比|占比|%|％)"
    r"|(?:相对(?:于)?|相比|相较于|基准|以)[^。；，,？?]{0,12}"
    r"(?:人数|数量|总数|总额|规模|基数|标准|水平)"
    r"|(?:人数|数量|总数|总额|规模|基数)[^。；，,？?]{0,8}(?:相对(?:于)?|相比|基准)"
)
_RATIO_VALUE = re.compile(
    r"\d+(?:\.\d+)?\s*(?:%|％)"
    r"|\d+\s*/\s*\d+"
    r"|[〇零一二三四五六七八九十百千万亿两]+\s*分之\s*"
    r"[〇零一二三四五六七八九十百千万亿两]+"
    r"|一半|半数"
)
_BOUND_BASELINE = re.compile(
    r"(?:相对(?:于)?|相比|相较于|占|按)[^。；，,？?]{0,16}?"
    r"(?:人数|数量|总数|总额|规模|基数|基准)[^。；，,？?]{0,4}"
)
_BOUND_VALUE = re.compile(
    r"\d+(?:\.\d+)?\s*(?:工作日|%|％|学时|学分|小时|分钟|天|周|次|门|人|个|元|名|份|页|字|位)?"
)
_BOUND_UNITS = r"工作日|%|％|学时|学分|小时|分钟|天|周|次|门|人|个|元|名|份|页|字|位"
_RANGE_VALUE = re.compile(
    r"(?<!\d)(?!20\d{2}\s*[—–~～\-至]\s*20\d{2})"
    rf"\d+(?:\.\d+)?\s*(?:{_BOUND_UNITS})?"
    r"\s*[—–~～\-至]\s*\d+(?:\.\d+)?\s*"
    rf"(?:{_BOUND_UNITS})?"
)
_COUNT_VALUE = re.compile(
    r"(?<!第)(?:\d+(?:\.\d+)?|[一二三四五六七八九十百千万两壹贰叁肆伍陆柒捌玖拾佰仟]+)"
    r"\s*(?:个)?(?:工作日|学分|学时|小时|分钟|天|周|次|门|人|名|位|份|页|字|元|学期)"
    r"|(?:人数|名额)[^。；，,？?]{0,6}"
    r"(?:\d+|[一二三四五六七八九十百千万两壹贰叁肆伍陆柒捌玖拾佰仟]+)"
    r"|\d+(?:\.\d+)?\s*(?:%|％)"
)
_LEVEL_TERMS = ("博士", "硕士", "本科")
_LEVEL_CUES = ("博士", "硕士", "研究生", "本科")
_NUMERIC_EFFECT_OBJECT = re.compile(
    r"严重警告(?:处分)?|警告(?:处分)?|通报批评|留校察看|开除学籍|记过"
    r"|处分|(?:红色|橙色|黄色|蓝色)?预警"
)
_NUMERIC_EFFECT = re.compile(
    r"(?:会|将|会被|可能|可|需|须)?(?:收到|受到|给予|予以|处以|触发|获得)"
    rf"(?:{_NUMERIC_EFFECT_OBJECT.pattern})"
)
_NUMERIC_COUNT_SCAFFOLD = re.compile(r"由|组成|构成|包含|包括|共有|共计|合计|分为|一共")
_ENUM_SPLIT = re.compile(r"、")
_MEASURE_OBJECT_NOUNS = ("人数", "人员", "名额", "课程")
_NUMERIC_ASK = re.compile(
    r"(?<!第)(?:计|共计|合计|共|应|需|须|要)?(?:多少|几)(?:个)?"
    r"(?P<unit>工作日|学分|学时|小时|分钟|天|周|次|门|人|名|位|份|页|字|元|学期)"
    r"|人数(?:为|是)?(?:多少|几人|多少人|有多少人)"
    r"|一式(?:几|多少)份"
)
_BOUND_MAX_TOKEN = re.compile(
    r"最多|至多|最高|最长|上限|不超过|不得超过|不大于|封顶|以下|≤"
)
_BOUND_MIN_TOKEN = re.compile(
    r"最少|至少|最低|最短|下限|不少于|不得低于|不低于|以上|≥"
)
_UNCERTAIN = re.compile(
    r"证据不足|未涉及|未提及|未说明|未明确|无法确定|不能确定|不确定|不知道|不详"
    r"|尚不明确|有待明确|无法回答"
)
_SELECTION_POSITIVE = re.compile(r"(?<!未)(?<!没)(?<!不)选修")
# Only an explicit selection state (“选修了/已修”) or an explicit both-branch
# ask is a polarity property. “选修课程有哪些” stays an ordinary list ask.
_SELECTION_STATE = re.compile(
    r"(?:已|已经|曾|此前|之前)?(?:选修|修读)(?:了|过)"
    r"|已(?:经)?选(?:课|修|了)?|已(?:经)?修(?:读|了|过)?"
)
# Explicit negative enrolment handling (“未按规定办理选课手续”) is the same
# state as 未选/没选; generic absence or an unfinished course is not.
_NEGATIVE_ENROLMENT_STATE = re.compile(
    r"(?:未|没|没有|尚未|并未)"
    r"(?:按(?:照)?(?:学校)?(?:规定|要求))?"
    r"(?:办理|参加|进行)?"
    r"(?:选课|选修|选读)"
    r"(?!课程|考试|考核|成绩|学分)"
    r"(?:手续|报名|登记)?"
)
_LIST_OMISSION = re.compile(
    r"第[一二三四五六七八九十\d]+(?:项|条|门|个|名|位|章|节)"
    r"|名单|清单|其余|其他|剩余|漏列|未列出|缺项"
)
_PROPERTY_EQUIVALENTS = (
    ("基本修业年限", "学制"),
    ("修业年限", "学制"),
    ("学习年限", "学制"),
    ("培养年限", "学制"),
    ("毕业总学分", "总学分"),
    ("毕业学分", "总学分"),
    ("本科学生", "本科"),
    ("本科生", "本科"),
    ("辅修专业", "辅修"),
    ("含重修", "重修"),
    ("每生每年", "每年"),
    ("每人每年", "每年"),
    ("每生每月", "每月"),
    ("每人每月", "每月"),
    ("不足", "少于"),
)
# 一学期/每学期都是学期口径；只在界限/数量匹配里等价，修业时长仍保留原词。
_PERIOD_EQUIVALENT = re.compile(
    r"(?<![第同这那本下上前后每])一(?:个)?学期"
)


def _period_equivalent(text: str) -> str:
    return _PERIOD_EQUIVALENT.sub("每学期", text)


def _bound_period(text: str) -> tuple[str, ...]:
    return _period_requirement(_period_equivalent(text))
_PROPERTY_PREFIX = re.compile(
    r"^(?:请问|帮我|我想问|我想|想问|对于|关于|对|在(?!校|读|职|籍|线)|其中)"
)
_PROPERTY_MODAL = re.compile(r"可以|能够|能否|允许|应当|应该|必须|会不会|需要|最长|通常")
_PROPERTY_EDGE = " 　.。：:；;、，,（）()【】[]？?和与及的是为在把将需能可要得会"
_GENERIC_PROPERTY_CORE = re.compile(
    r"^(?:完成|提交|递交|上交|报送|进行|安排|办理|申请|报名|答辩|考试"
    r"|毕业|开始|结束|开课|上课|报到|入学|选课)$"
)
_BOOL_PREDICATE = {
    "payment": re.compile(r"缴费|交费|收费|收取|收[^。；，,？?]{0,2}费|缴纳|交纳|付费|收钱|免费"),
    "degree": re.compile(r"学位"),
    "submission": re.compile(r"提交|递交|上交|报送"),
}
_BOOL_NEGATIVE = {
    "payment": re.compile(
        r"不收费|不收|免费|免收|不缴|无需缴|不需缴|免缴|不交|不用交|无需交"
        r"|不需交|不必交|不收取|不缴纳|不交纳"
    ),
    "degree": re.compile(
        r"不授予|不予授予|不获得|不能获得|无法获得|不颁发|不予颁发|拿不到"
    ),
    "submission": re.compile(
        r"无需提交|不需提交|不必提交|不用提交|免提交|无需递交|不必递交"
        r"|不需要提交|可不提交|无须提交|免交"
    ),
}
_BOOL_AFFIRM = {
    "payment": re.compile(r"需(?:要)?缴|应(?:当)?缴|须缴|缴纳|交纳|缴费|交费|收费|付费|收钱"),
    "degree": re.compile(r"授予|获得|取得|颁发|拿到"),
    "submission": re.compile(r"需(?:要)?提交|应提交|须提交|必须提交|提交|递交|上交|报送"),
}
_BOOL_NOISE = re.compile(r"标准|条件|流程|办法|细则|材料清单|要求")


def _canonical_property_text(text: str) -> str:
    for full, short in _PROPERTY_EQUIVALENTS:
        text = text.replace(full, short)
    return _canonical_unit_terms(_canonical_academic_terms(text))


def _canonical_fee_text(text: str) -> str:
    """Fee-tier paraphrases only; never loosen general numeric thresholds."""
    text = re.sub(r"前\s*(\d+)\s*份", r"\1份内", text)
    text = re.sub(r"(\d+)\s*份\s*(?:以内|之内)", r"\1份内", text)
    return re.sub(r"超出(?=\s*\d+\s*份)", "超过", text)


_PROPERTY_KEEP_PREFIX = ("在校", "在读", "在职", "在籍", "在线")


def _strip_property_edge(text: str) -> str:
    while text and text[0] in _PROPERTY_EDGE and not text.startswith(
        _PROPERTY_KEEP_PREFIX
    ):
        text = text[1:]
    return text.rstrip(_PROPERTY_EDGE)


def _clean_property_core(text: str) -> str:
    core = _QUESTION_SKELETON.sub("", text)
    core = _DATE_ASK.sub("", core)
    core = _PROPERTY_PREFIX.sub("", core)
    core = _PROPERTY_MODAL.sub("", core)
    core = re.sub(r"^(?:分别|各自|都|均)", "", core)
    core = _strip_property_edge(core)
    return "" if _GENERIC_PROPERTY_CORE.fullmatch(core) else core


def _without_span(text: str, start: int, end: int) -> str:
    return f"{text[:start]} {text[end:]}"


def _period_requirement(text: str) -> tuple[str, ...]:
    periods = [term for term in ("每年", "每学年", "每学期", "每月", "每天", "每周") if term in text]
    return (periods[0],) if len(periods) == 1 else ()


def _property_heads(
    core: str, protected: tuple[str, ...], *, split: bool
) -> list[str]:
    if not split or not core:
        return [core]
    heads = [
        _clean_property_core(head)
        for head in _split_terms(core, _HEAD_SPLIT, protected)
    ]
    heads = [
        head
        for head in heads
        if len(re.sub(r"[^\u4e00-\u9fffA-Za-z]", "", head)) >= 2
    ]
    return heads or [_clean_property_core(core)]


_DURATION_ASK = re.compile(
    r"多长时间|多少时间|多少个月|多少星期|多少年|多少天|多少日|多少周"
    r"|多少(?:个)?工作日|几个工作日|多少个工作日"
    r"|几个星期|几年|几个月|几天|几日|几周|多久|多长"
)
_DURATION_BOUNDARY = re.compile(r"[、，,；;。！!？?]|以及|或者")
_DURATION_SCOPE_NOISE = re.compile(
    r"一般|通常|原则上|最少|最多|至少|至多|最长|最短|又|也|均|都|分别|各自"
    r"|应当|应该|必须|需要|需|要|得|应|以"
)
_DURATION_SCOPE_EDGE = re.compile(
    r"^(?:相关|有关|上述|该|其|这些|那些)"
    r"|(?:应当|应该|必须|需要|需|须|应|要|提前|至少|最多|最晚|最早)+$"
)
_DURATION_CLAUSE_NOISE = re.compile(
    r"(?:相关|有关|上述|该|其|这些|那些|以及|或者|和|与|及|在|应|应当"
    r"|应该|必须|需要|需|须|将|把|的|是|为|内)"
)
_DURATION_ACTION_LEAD = re.compile(
    r"^(?:才(?:可以|可|能)?|就(?:可以|可|能)?|即可|可以|可|能够|能|允许|应当|应该|必须|需要|需|要|得)"
)
_DURATION_ACTION_MODAL = re.compile(
    r"^(?:不能|不得|不准|不可|不可以|无法|不予|不再|未|没有|没|不必|无需|无须)"
)
_DURATION_ACTION_TAIL = re.compile(
    r"(?:向?哪里|何处|哪儿|什么地方|哪个地方)(?:处理|办理|提交|报送|申请)?$"
)
# “到达考场/离开教室” names the venue as the action's object; the action verb
# itself is what must be equivalent to the claim's wording.
_DURATION_VENUE = re.compile(r"(?:考场|试室|教室|会场|现场)$")
_DURATION_POST_NOISE = re.compile(
    r"^(?:以内|之内|内|以后|之后|后|期间|时|为止|为|是|共|合计|总共|大约|左右|上下)"
)
_DURATION_GENERIC_ACTION = re.compile(r"^[一二三四五六七八九十两\d]+(?:期|次|届|年|月|周)$")
_DURATION_MODIFIER_CORE = re.compile(r"^(?:累计|总共|合计|共计|一般|通常|原则)$")
_DURATION_ACTION_WORD = re.compile(
    r"备案|公布|公示|答复|回复|办结|处理|办理|审批|审核|提交|报送|上报|申请"
    r"|转专业|离校|退学|休学|复学|毕业|完成|执行|实施|开展|组织|安排|发放|支付|缴纳"
    r"|统计|告知|预警|参加|进入|离开|离场|到场|入场|退场|到达"
)
_DURATION_PROCESSING = ("统计", "告知", "预警", "书面告知", "给予书面预警")
_DURATION_ACTION_EQUIVALENT = {
    "处理": ("处理", *_DURATION_PROCESSING),
    "到场": ("到场", "进入考场", "进入", "入场", "到考场"),
    "到达": ("到达", "进入考场", "进入", "入场", "到场", "到考场"),
    "进入": ("进入", "进入考场", "入场", "到场"),
    "离场": ("离场", "离开考场", "离开", "退场"),
    "离开": ("离开", "离开考场", "离场", "退场"),
    "参加": ("参加", "参加考试", "参加该课程考试"),
    "报送": ("报送", "上报", "提交"),
}
# Explicit business nouns the question asks for. A registered program name or
# a generic placeholder (事项/工作/事宜) is not a business fact by itself.
_DURATION_BUSINESS_OBJECTS = ("名单", "汇总表", "申请表", "登记表", "材料", "试卷")
_DURATION_NOTIFY = re.compile(r"告知|通知|通告")
# 入学/毕业/开学 are events a duration can be measured from; the surrounding
# action context (“申请转专业需要…”) is not something the claim must repeat.
_DURATION_SCOPE_ANCHOR = re.compile(r"(?:入学|毕业|开学)$")
_DURATION_ANCHOR_TERMS = re.compile(r"入学|毕业|开学")
_DURATION_SCOPE_COHORT = (
    "博士生",
    "硕士生",
    "研究生",
    "本科生",
    "辅导员",
    "班主任",
    "学生",
    "新生",
    "教师",
    "老师",
    "导师",
)


def _duration_anchor_scope(text: str, protected: tuple[str, ...]) -> str:
    """Keep the event anchor plus a directly attached object/person/cohort."""
    match = _DURATION_SCOPE_ANCHOR.search(text)
    if match is None:
        return ""
    head = text[: match.start()]
    terms = sorted(
        {term for term in (*protected, *_DURATION_SCOPE_COHORT) if term},
        key=len,
        reverse=True,
    )
    for term in terms:
        if len(term) >= 2 and head.endswith(term):
            return f"{term}{match.group(0)}"
    return match.group(0)


def _duration_scope(pre: str, protected: tuple[str, ...] = ()) -> str:
    text = pre.strip(_STEM_STRIP)
    locations = list(re.finditer(r"[在于]", text))
    if locations:
        tail = text[locations[-1].end() :]
        # “又应在几周内…” ends on the modal 在, not on a scope marker.
        if tail.strip():
            text = tail
    text = _DURATION_SCOPE_NOISE.sub("", text).strip(" 　")
    anchor = _duration_anchor_scope(text, protected)
    if anchor:
        return anchor
    text = _clean_property_core(text)
    text = _DURATION_SCOPE_EDGE.sub("", text).strip(_PROPERTY_EDGE)
    return text.strip(_PROPERTY_EDGE)


def _duration_action(post: str) -> str:
    text = post.strip(_STEM_STRIP)
    while True:
        stripped = _DURATION_ACTION_LEAD.sub("", text)
        stripped = _DURATION_ACTION_MODAL.sub("", stripped)
        stripped = _DURATION_POST_NOISE.sub("", stripped).strip(_STEM_STRIP)
        if stripped == text:
            break
        text = stripped
    text = _clean_property_core(text)
    text = _DURATION_ACTION_TAIL.sub("", text).strip(_PROPERTY_EDGE)
    if _DURATION_VENUE.search(text):
        trimmed = _DURATION_VENUE.sub("", text).strip(_PROPERTY_EDGE)
        if _DURATION_ACTION_WORD.fullmatch(trimmed):
            text = trimmed
    if text and not _DURATION_ACTION_WORD.fullmatch(text) and text.endswith(("处理", "办理")):
        trimmed = re.sub(r"(?:处理|办理)$", "", text).strip(_PROPERTY_EDGE)
        if trimmed and _DURATION_ACTION_WORD.search(trimmed):
            text = trimmed
    return "" if _DURATION_GENERIC_ACTION.fullmatch(text) else text


_DURATION_GENERIC_SUBJECT = re.compile(r"^(?:本科生|本科学生|大学生|本科|学生|同学|考生)")
_DURATION_PAREN = re.compile(r"[（(]([^（）()]*)[）)]")
_DURATION_PAREN_QUALIFIER = re.compile(
    r"^(?:含|包括|其中|即|也就是|不含|不包括|另含|外加)"
)


def _duration_core(scope: str) -> str:
    """Drop a generic student prefix when a concrete action/object remains.

    The unit is already gated by its subject, so “本科生休学” and the claim's
    “学生休学” must not require the exact question prefix.
    """
    text = scope.strip(_STEM_STRIP)
    text = re.sub(r"^(?:相关|有关|上述|该|其|这些|那些)", "", text).strip(_PROPERTY_EDGE)
    # A list of business objects is one object set: “试卷和登记表” must match
    # a claim that separates them with 、 without demanding the conjunction.
    text = re.sub(r"(?<=[\u4e00-\u9fff])(?:以及|和|与|及)(?=[\u4e00-\u9fff])", "", text)
    stripped = _DURATION_GENERIC_SUBJECT.sub("", text).strip(_PROPERTY_EDGE)
    # An event anchor carries its attached cohort/level identity (“本科生入学”
    # must not become bare “入学”); the prefix still yields to a concrete action.
    if stripped and not _DURATION_SCOPE_ANCHOR.search(text):
        return stripped
    return text


def _duration_business(
    pre: str, post: str = "", protected: tuple[str, ...] = ()
) -> tuple[str, ...]:
    """Explicit business nouns the question asks, kept apart from the anchor."""
    text = f"{pre} {post}"
    business_terms = (*_DURATION_BUSINESS_OBJECTS, "告知")
    for term in sorted(
        {term for term in protected if term and len(term) >= 2},
        key=len,
        reverse=True,
    ):
        # A registered program name is not a business fact, but a protected
        # object that ends in 名单/汇总表 still names the asked business.
        if any(term.endswith(business) for business in business_terms):
            continue
        text = text.replace(term, " ")
    terms = [term for term in _DURATION_BUSINESS_OBJECTS if term in text]
    if "告知" in text:
        terms.append("告知")
    return tuple(dict.fromkeys(terms))


def _duration_expand_parentheticals(text: str) -> str:
    """Expand parentheses only when they assert their own duration branch.

    A qualifier such as “（含3天）” is not a separate promise and stays with
    the clause it modifies.
    """

    def expand(match: re.Match[str]) -> str:
        body = match.group(1).strip()
        if (
            not body
            or _DURATION_PAREN_QUALIFIER.match(body)
            or not _DURATION_TOKEN.search(body)
            or not _DURATION_ACTION_WORD.search(body)
        ):
            return match.group(0)
        return f"，{body}，"

    return _DURATION_PAREN.sub(expand, text)


def _duration_claim_segments(text: str) -> list[str]:
    return [
        part
        for part in _CLAIM_SEGMENT.split(_duration_expand_parentheticals(text))
        if part.strip()
    ]


_STRONG_CLAIM_SEGMENT = re.compile(r"[。；;！？!?]")
_COMMA_CLAIM_SEGMENT = re.compile(r"[，,]")


def _duration_opens_branch(fragment: str, current: str) -> bool:
    if _DURATION_TOKEN.search(fragment) and _DURATION_TOKEN.search(current):
        return True
    new_anchors = set(_DURATION_ANCHOR_TERMS.findall(fragment))
    old_anchors = set(_DURATION_ANCHOR_TERMS.findall(current))
    if new_anchors and old_anchors and not new_anchors & old_anchors:
        return True
    new_subjects = {term for term in _DURATION_SCOPE_COHORT if term in fragment}
    old_subjects = {term for term in _DURATION_SCOPE_COHORT if term in current}
    return bool(new_subjects and old_subjects and not new_subjects & old_subjects)


def _duration_claim_branches(text: str) -> list[str]:
    """Join comma fragments until the next independent duration assertion.

    The duration and the action it controls often sit on opposite sides of a
    comma (“开学2周内，统计名单，书面告知”), while a semicolon or a new
    duration value starts a separate promise.
    """
    branches: list[str] = []
    for sentence in _STRONG_CLAIM_SEGMENT.split(_duration_expand_parentheticals(text)):
        if not sentence.strip():
            continue
        fragments: list[str] = []
        for fragment in _COMMA_CLAIM_SEGMENT.split(sentence):
            fragment = fragment.strip()
            if not fragment:
                continue
            if len(_DURATION_TOKEN.findall(fragment)) > 1:
                fragments.extend(
                    part.strip()
                    for part in _DURATION_BOUNDARY.split(fragment)
                    if part.strip()
                )
            else:
                fragments.append(fragment)
        current: list[str] = []
        for fragment in fragments:
            if current and _duration_opens_branch(fragment, "，".join(current)):
                branches.append("，".join(current))
                current = [fragment]
            else:
                current.append(fragment)
        if current:
            branches.append("，".join(current))
    return branches


def _duration_properties(
    part: str, protected: tuple[str, ...] = ()
) -> list[_Property]:
    """Every 多久/几周 ask keeps its own pre-scope and post-action.

    “学生休学…一期、累计…、创业休学…” must not collapse into one core.
    """
    properties: list[_Property] = []
    asks = list(_DURATION_ASK.finditer(part))
    for index, ask in enumerate(asks):
        from_pos = asks[index - 1].end() if index else 0
        boundaries = list(_DURATION_BOUNDARY.finditer(part, from_pos, ask.start()))
        segment_start = boundaries[-1].end() if boundaries else from_pos
        pre = part[segment_start : ask.start()]
        scope = _duration_scope(pre, protected)
        next_stop = asks[index + 1].start() if index + 1 < len(asks) else len(part)
        stop = _DURATION_BOUNDARY.search(part, ask.end(), next_stop)
        post_end = stop.start() if stop else next_stop
        post = part[ask.end() : post_end]
        action = _duration_action(post)
        business = _duration_business(pre, post, protected)
        core = _duration_core(scope or action)
        if not core:
            continue
        anchor = _DURATION_SCOPE_ANCHOR.search(scope)
        anchor_term = anchor.group(0) if anchor is not None else ""
        required = tuple(
            dict.fromkeys(
                [
                    *((anchor_term,) if anchor_term and anchor_term not in core else ()),
                    *_required_markers(core),
                    *_period_requirement(part),
                    *(
                        ("工作日",)
                        if "工作日" in ask.group(0)
                        else ()
                    ),
                ]
            )
        )
        properties.append(
            _Property(
                kind="duration",
                cue=re.sub(r"\s+", "", f"{scope or action}{ask.group(0)}"),
                core=core,
                action=action,
                required=required,
                ask=ask.group(0),
                exclude=("自然日",) if "工作日" in ask.group(0) else (),
                business=business,
            )
        )
    return properties


def _semester_properties(
    part: str, protected: tuple[str, ...], scope_hint: str = ""
) -> list[_Property]:
    """An explicit 春季/秋季 choice is a required value, not background scope."""
    properties: list[_Property] = []
    for match in SEMESTER_ASK.finditer(part):
        rest = part[: match.start()] + part[match.end() :]
        codes = _numeric_course_codes(part) or _numeric_course_codes(scope_hint)
        anchors = _numeric_identity_anchors(part[: match.start()], protected)
        if not anchors:
            anchors = _numeric_identity_anchors(scope_hint, protected)
        if codes:
            anchors = codes
            core = " ".join(codes)
        else:
            core = _clean_property_core(rest)
            if len(re.sub(r"[^\u4e00-\u9fffA-Za-z]", "", core)) < 2:
                core = _clean_property_core(scope_hint)
        properties.append(
            _Property(
                kind="semester",
                cue=re.sub(r"\s+", "", match.group(0)),
                core=core,
                required=tuple(anchors),
                ask="开课学期",
            )
        )
    return properties


def _with_scope_exclusions(properties: list[_Property]) -> list[_Property]:
    """A sibling scope's value cannot answer this duration ask."""
    cores = {
        prop.core for prop in properties if prop.kind == "duration" and prop.core
    }
    result: list[_Property] = []
    for prop in properties:
        if (
            prop.kind != "duration"
            or not prop.core
            or _DURATION_MODIFIER_CORE.fullmatch(prop.core)
        ):
            result.append(prop)
            continue
        exclude = tuple(
            other
            for other in cores
            if other != prop.core
            # 开学/入学 is a shared deadline anchor, not a competing object.
            and not _DURATION_SCOPE_ANCHOR.search(other)
            and len(re.sub(r"[^\u4e00-\u9fffA-Za-z]", "", other)) >= 2
            and other not in prop.core
        )
        result.append(replace(prop, exclude=exclude) if exclude else prop)
    return result


_DAY_PROPERTY_MEASURES = ("天", "工作日", "自然日")


def _duration_duplicated_by_bound(
    durations: list[_Property], bounds: list[_Property]
) -> bool:
    """One day-duration ask already represented by one explicit min/max bound."""
    if len(durations) != 1:
        return False
    duration = durations[0]
    if not any(term in duration.ask for term in _DAY_PROPERTY_MEASURES):
        return False
    day_bounds = [bound for bound in bounds if bound.measure in _DAY_PROPERTY_MEASURES]
    if len(day_bounds) != 1:
        return False
    bound = day_bounds[0]
    own = _canonical_property_text(duration.core)
    other = _canonical_property_text(_bound_core(bound.core, bound.measure))
    if not own or not other:
        return False
    return _clause_covered(own, other, min_ratio=0.6) and _clause_covered(
        other, own, min_ratio=0.6
    )


def _requested_bound_measure(part: str) -> str:
    """The unit the user actually asks for, not background period/grade text."""
    if _RATIO_ASK.search(part):
        return "比例"
    for match in _NUMERIC_ASK.finditer(part):
        parsed = _numeric_measure(match, part)
        if parsed is not None:
            return parsed[1]
    text = _SCOPE_PERIOD.sub(" ", part)
    text = _GRADE_SCOPE.sub(" ", text)
    for token in ("每学年", "每学期", "每年", "每月", "每天", "每周"):
        text = text.replace(token, "")
    match = _VALUE_MEASURE.search(text)
    return match.group(0) if match is not None else ""


_TARGET_LOCATION_ASK = re.compile(
    r"(?:哪些|哪个|哪几个|什么)(?:个)?(?:地点|地方|位置)"
    r"|(?:在)?哪里(?:可以|能|可|有|提供|办理|自助|打印|领取)|在哪儿|(?<!如)何处"
)
# “由哪里提供/由哪个部门出具” asks the provider unit, not a physical venue.
# A who/department ask without 提供/出具/开具/发放 (由谁审批/由谁安排) is an
# owner/approval ask, not a provider ask.
# Bare “在哪里可以自助打印” stays a location ask because 哪里 here needs an
# explicit provider verb or a department-oriented ask word.
_TARGET_PROVIDER_ASK = re.compile(
    r"(?:由|经)\s*"
    r"(?:谁|哪个(?:部门|机构|单位|学院)|哪些(?:部门|机构|单位|学院)"
    r"|什么(?:部门|机构|单位)|哪一个(?:部门|机构|单位))"
    r"\s*(?:负责|进行)?\s*(?:提供|出具|开具|发放)"
    r"|(?:由|经)\s*(?:哪里|何处|哪儿)\s*(?:负责|进行)?\s*(?:提供|出具|开具|发放)"
    r"|(?:谁|哪个(?:部门|机构|单位)|哪些(?:部门|机构|单位)"
    r"|什么(?:部门|机构|单位)|哪一个(?:部门|机构|单位))"
    r"\s*(?:负责|进行)?\s*(?:提供|出具|开具|发放)"
)
_TARGET_TYPE_ASK = re.compile(
    r"(?:哪(?:一)?种|哪种|哪类|什么|何种)(?:的)?(?P<base>证明|证书)"
)
_CAPABILITY_ASK = re.compile(
    r"(?:能否|能不能|可不可以|是否可以"
    r"|(?<![不未无没非可只])能|(?<![不未无没非可只])可以"
    r"|(?<![不未无没非可只])可|(?<![不未无没非可只])能够)"
    r"(?P<verb>办理|出具|开具|申请|打印|提供|补办|领取|参加|报名)"
)
_CAPABILITY_YESNO = re.compile(r"是否|能否|能不能|可不可以|可以不可以|吗|嘛|呢|否")
_TARGET_AGENCY_ASK = re.compile(
    r"(?:哪里|(?<!如)何处|哪儿|哪个部门|什么部门|哪一个部门)"
    r"(?:进行|负责)?(?:处理|办理|提交|报送)?"
)
_AGENCY_ACTION = re.compile(
    r"报送|提交|递交|上交|送交|转交|处理|办理|申请|审核|审批|批准"
    r"|提供|出具|开具|发放"
)
_PROVIDER_ACTION_EQUIVALENT = {
    "提供": ("提供", "出具", "开具", "发放"),
    "出具": ("出具", "开具", "提供", "发放"),
    "开具": ("开具", "出具", "提供", "发放"),
    "发放": ("发放", "提供", "出具", "开具"),
}
_AGENCY_NOUN = re.compile(
    r"([\u4e00-\u9fff]{1,12}?(?:本科生院|研究生院|学院|大学|学校|学部"
    r"|办公室|委员会|中心|部门|档案馆|部|处(?!理|置|于|境)|科|室|所|局|厅|署|院))"
)
_AGENCY_LEAD = re.compile(
    r"^(?:报送|提交|递交|上交|送交|转交|报|交|向|至|到|往|由|经|请|需|应"
    r"|将|把|给|在|负责|进行)+"
)
_AGENCY_TARGET_BREAK = re.compile(r"(?:由|经|向|至|到|往|给|在)")
_AGENCY_STOP = frozenset(
    {"全部", "部分", "内部", "外部", "各部门", "有关部门", "相关部门", "部门"}
)


def _duration_owns_capability(part: str) -> bool:
    """A duration ask owns its action; 能 inside it is not a yes/no ask."""
    if _DURATION_ASK.search(part) is None:
        return False
    return _CAPABILITY_YESNO.search(part) is None


def _agency_action(text: str) -> str:
    matches = list(_AGENCY_ACTION.finditer(text))
    return matches[-1].group(0) if matches else ""


def _agency_target_present(text: str) -> bool:
    for match in _AGENCY_NOUN.finditer(text or ""):
        candidate = match.group(1)
        breaks = list(_AGENCY_TARGET_BREAK.finditer(candidate))
        if breaks:
            candidate = candidate[breaks[-1].end() :]
        candidate = _AGENCY_LEAD.sub("", candidate)
        if len(candidate) >= 2 and candidate not in _AGENCY_STOP:
            return True
    return False


_PROVIDER_POPULATION_TERMS = (
    "在校",
    "毕业",
    "新生",
    "本科",
    "硕士",
    "博士",
    "研究生",
    "学生",
    "教师",
    "老师",
)


def _provider_population_anchor(head: str) -> str:
    population = _strip_property_edge(head.split("的", 1)[0])
    for term in _PROVIDER_POPULATION_TERMS:
        if term in population:
            return term
    compact = re.sub(r"[^\u4e00-\u9fffA-Za-z]", "", population)
    return compact[:2] if len(compact) >= 2 else compact


def _provider_heads(
    text: str, protected: tuple[str, ...]
) -> list[tuple[str, tuple[str, ...]]]:
    """Split “在校生与毕业生的成绩单” into one branch per population."""
    text = _clean_property_core(text)
    text = _strip_property_edge(
        re.sub(r"(?:分别|各自|都|均|是|为|有)+$", "", text)
    )
    heads = [
        head
        for head in (
            _clean_property_core(part)
            for part in _split_terms(text, _HEAD_SPLIT, protected)
        )
        if head
    ]
    if not heads:
        return []
    shared = ""
    if len(heads) > 1 and "的" in heads[-1]:
        candidate = _strip_property_edge(heads[-1].split("的", 1)[1])
        if len(re.sub(r"[^\u4e00-\u9fffA-Za-z]", "", candidate)) >= 2:
            shared = candidate
    result: list[tuple[str, tuple[str, ...]]] = []
    for head in heads:
        core = f"{head}的{shared}" if shared and "的" not in head else head
        anchor = _provider_population_anchor(core)
        result.append((core, (anchor,) if anchor else ()))
    return result


def _provider_target_properties(
    part: str, match: re.Match[str], protected: tuple[str, ...]
) -> list[_Property]:
    ask = re.sub(r"\s+", "", match.group(0))
    source = f"{part[: match.start()]} {part[match.end() :]}".strip()
    heads = _provider_heads(source, protected)
    action = _agency_action(part)
    if not heads:
        return [
            _Property(
                kind="target",
                cue=ask,
                direction="provider",
                action=action,
                ask="提供方",
            )
        ]
    return [
        _Property(
            kind="target",
            cue=ask,
            core=core,
            direction="provider",
            action=action,
            required=required,
            ask="提供方",
        )
        for core, required in heads
    ]


_PROOF_SCAFFOLD = re.compile(
    r"(?:只|仅|还|可|可以|能|能够|不|未|不予|不得|无法|办理|出具|开具|提供"
    r"|打印|发放|领取|申请|补办|的|是|为|有|学生|本科生|在校生|毕业生|未毕业"
    r"|任何|相关|其他|其它|该|此|这类|此类)"
)
_LOCATION_SUFFIX = (
    r"(?:服务大厅|服务中心|学生社区|大厅|校区|教学楼|图书馆|社区|楼|馆|站|室"
    r"|食堂|饭堂|广场|门|区|点)"
)
_LOCATION_SCAFFOLD = re.compile(
    r"学校|我校|本校|校内|学生|自助|服务|打印|办理|设有|设置|设立|提供|开放"
    r"|包括|共有|有|的|在|\d+|小时|分钟|社区|宿舍区"
)
_AMOUNT_PAYMENT_VERBS = frozenset({"收费", "收取", "缴费", "交费", "缴纳", "交纳", "付费"})


def _amount_tier_terms(text: str) -> tuple[str, tuple[str, ...]]:
    text = _canonical_fee_text(_canonical_property_text(text or ""))
    over = _AMOUNT_TIER_OVER.search(text)
    if over is not None:
        return "over", (f"超过{over.group(1)}份",)
    within = _AMOUNT_TIER_WITHIN.search(text)
    if within is not None:
        limit = within.group(1) or within.group(2)
        return "within", (f"{limit}份内",)
    return "", ()


_AMOUNT_TIER_SPAN = re.compile(
    r"(?:累计\s*)?(?:前\s*|超过\s*|超出\s*|多于\s*|高于\s*|以上\s*|不超过\s*|不足\s*)?"
    r"\d+\s*份\s*(?:以内|之内|内)?"
    r"|每份"
)


def _amount_subject_core(core: str, business: tuple[str, ...]) -> str:
    """Tier values and fee objects are verified separately from the subject."""
    text = _AMOUNT_TIER_SPAN.sub(" ", core or "")
    for term in business:
        if term:
            text = text.replace(term, " ")
    return _clean_property_core(text)


_FEE_POPULATION_TERMS = ("在校生", "毕业生", "新生", "本科生", "硕士生", "博士生", "研究生")


def _fee_population_terms(text: str) -> tuple[str, ...]:
    return tuple(term for term in _FEE_POPULATION_TERMS if term in (text or ""))


def _amount_objects(text: str) -> tuple[str, ...]:
    objects: list[str] = []
    for match in _AMOUNT_OBJECT.finditer(text or ""):
        value = match.group(0)
        if value in _AMOUNT_PAYMENT_VERBS:
            continue
        # The generic “…费” fallback must not turn “份收费” / “每份收取” into
        # a business object; those are payment-verb scaffolds.
        if any(value.endswith(verb) for verb in _AMOUNT_PAYMENT_VERBS):
            continue
        objects.append(value)
    return tuple(dict.fromkeys(objects))


def _proof_types(text: str) -> tuple[str, ...]:
    candidates: list[str] = []
    for end in re.finditer(r"(?:证明|证书)", text or ""):
        tail = text[max(0, end.start() - 8) : end.end()]
        for length in range(2, min(len(tail), 10) + 1):
            candidate = tail[-length:]
            if (
                4 <= len(candidate) <= 10
                and candidate.endswith(end.group(0))
                and candidate not in {"证明", "证书"}
                and not _PROOF_SCAFFOLD.search(candidate)
                and re.fullmatch(r"[\u4e00-\u9fff]+", candidate)
                and candidate not in candidates
            ):
                candidates.append(candidate)
    return tuple(candidates)


_MATERIAL_GRADE_ASK = re.compile(
    r"(?:什么|何种|哪(?:一)?(?:级|种|类)?|几)\s*(?:级|等级|级别)"
    r"[^，,。；;？?]{0,6}(?:医院|医疗机构)"
    r"|(?:医院|医疗机构)[^，,。；;？?]{0,6}"
    r"(?:什么|何种|哪(?:一)?(?:级|种|类)?|几)\s*(?:级|等级|级别)"
    r"|(?:医院|医疗机构)\s*(?:的)?\s*(?:级别|等级)"
    r"\s*(?:要求|如何|怎么|怎样|是什么|是多少|有何|有哪些|多少)"
)
_MATERIAL_STAMP_ASK = re.compile(
    r"(?:何种|什么|哪种|哪个|哪一个|哪些|何类)\s*(?:的)?"
    r"(?:盖章|用印|印章|公章|专用章|章)"
    r"|盖\s*(?:何种|什么|哪种|哪个|哪些|哪一)\s*(?:的)?"
    r"(?:章|印章|公章|专用章)"
    r"|用\s*(?:何种|什么|哪种|哪个|哪些|哪一)\s*(?:的)?"
    r"(?:章|印章|公章|专用章)"
    r"|(?:盖章|用印|印章)\s*(?:的)?\s*(?:名称|种类|类型)"
    r"|(?:盖章|用印|印章)\s*(?:的)?\s*要求"
    r"\s*(?:是什么|是啥|如何|怎么|怎样|有哪些|为何|多少)"
    r"|(?:印章|公章|章)\s*(?:的)?\s*(?:名称|种类|类型)"
)
_MATERIAL_ATTRIBUTE_ASKS = (
    ("grade", _MATERIAL_GRADE_ASK, "医院级别"),
    ("stamp", _MATERIAL_STAMP_ASK, "盖章名称"),
)
_MATERIAL_FALLBACK_NOUNS = ("病例", "病历", "报告", "材料", "申请表", "复印件")
_HOSPITAL_GRADE_VALUE = re.compile(
    r"(?:[一二三四五六七八九十\d]+\s*(?:级|等)\s*(?:甲|乙|丙|丁)?\s*等?\s*"
    r"(?:[^，,。；;：:、]{0,4})?(?:医院|医疗机构)"
    r"|[一二三四五六七八九十\d]+\s*甲\s*(?:医院|医疗机构)"
    r"|[甲乙丙丁]\s*等\s*(?:医院|医疗机构)"
    r"|[一二三四五六七八九十\d]+\s*(?:级|等)\s*(?:甲|乙|丙|丁)\s*等?)"
)
_NAMED_STAMP = re.compile(
    r"[\u4e00-\u9fff]{2,32}?(?:专用印章|专用章|公章|印章|章)"
)
_STAMP_SUFFIXES = ("专用印章", "专用章", "公章", "印章", "章")
# Stamp actions mark the start of the actual stamp name; they intentionally
# exclude 应/要 so names like 应急专用章 are not truncated.
_STAMP_VERB_TAIL = re.compile(
    r"加盖|盖有|盖|使用|加印|钤印|提供|提交|递交|出具|开具|携带|准备"
)
_GENERIC_STAMP_STEMS = frozenset(
    {
        "单位",
        "部门",
        "相关",
        "有关",
        "有效",
        "清晰",
        "完整",
        "统一",
        "规定",
        "要求",
        "上述",
        "证明",
        "材料",
        "文件",
        "证件",
        "证书",
        "该",
        "此",
        "本",
        "其",
    }
)
_PROOF_ANAPHOR = re.compile(
    r"(?:在|于)?(?:该|此|上述|前述|本|同一|同份|这份|那份)"
    r"[^，,。；;！？!?]{0,4}?(?:证明|证书|材料|文件)"
)
_PROOF_ANAPHOR_CORE = re.compile(
    r"^(?:在|于)?(?:该|此|上述|前述|本|同一|同份|这份|那份)"
    r"(?:证明|证书|材料|文件)$"
)


def _material_attribute_asks(part: str) -> tuple[tuple[str, str, str], ...]:
    asks: list[tuple[str, str, str]] = []
    for direction, pattern, label in _MATERIAL_ATTRIBUTE_ASKS:
        match = pattern.search(part)
        if match is not None:
            asks.append((direction, re.sub(r"\s+", "", match.group(0)), label))
    return tuple(asks)


def _material_core(part: str, scope_hint: str) -> str:
    proofs = _proof_types(f"{scope_hint} {part}".strip())
    if proofs:
        return max(proofs, key=len)
    return next(
        (noun for noun in _MATERIAL_FALLBACK_NOUNS if noun in part),
        "",
    )


def _material_properties(part: str, scope_hint: str = "") -> list[_Property]:
    asks = _material_attribute_asks(part)
    if not asks:
        return []
    core = _material_core(part, scope_hint)
    return [
        _Property(
            kind="material_attribute",
            cue=cue,
            core=core,
            direction=direction,
            ask=label,
        )
        for direction, cue, label in asks
    ]


def _hospital_grade_value(text: str) -> bool:
    return bool(_HOSPITAL_GRADE_VALUE.search(text or ""))


def _named_stamp(text: str) -> bool:
    for match in _NAMED_STAMP.finditer(text or ""):
        candidate = match.group(0)
        verbs = list(_STAMP_VERB_TAIL.finditer(candidate))
        if verbs:
            candidate = candidate[verbs[-1].end() :]
        suffix = next(
            (item for item in _STAMP_SUFFIXES if candidate.endswith(item)), ""
        )
        stem = re.sub(
            r"[^\u4e00-\u9fff]", "", candidate[: len(candidate) - len(suffix)]
        )
        if len(stem) < 2 or stem in _GENERIC_STAMP_STEMS:
            continue
        if re.search(r"什么|何种|哪种|哪个|哪一|哪些|需要|需|应|须|要", stem):
            continue
        return True
    return False


def _mask_material_stamps(text: str) -> str:
    """Hide stamp names so 疾病诊断证明专用章 is not read as a 诊断证明 mention."""
    masked = text or ""
    spans: list[tuple[int, int]] = []
    for match in _NAMED_STAMP.finditer(masked):
        candidate = match.group(0)
        verbs = list(_STAMP_VERB_TAIL.finditer(candidate))
        start = match.start() + (verbs[-1].end() if verbs else 0)
        if start < match.end():
            spans.append((start, match.end()))
    for start, end in sorted(spans, reverse=True):
        masked = masked[:start] + " " * (end - start) + masked[end:]
    return masked


def _same_material_proof(candidate: str, core: str) -> bool:
    left = _canonical_property_text(candidate)
    right = _canonical_property_text(core)
    if not left or not right:
        return False
    if left == right:
        return True
    return len(right) >= 4 and (
        left.endswith(right) or right.endswith(left)
    )


def _material_binding_segments(
    text: str, core: str
) -> list[tuple[str, bool]]:
    """Each material slot must stay with the asked proof's local clause.

    A clause naming another proof ends the prior binding; a proper anaphor
    (该证明/上述材料) keeps it. Stamp names are masked first because a name
    such as 疾病诊断证明专用章 contains the word 诊断证明.
    """
    segments = [part for part in _CLAIM_SEGMENT.split(text) if part.strip()]
    scrubbed = [_mask_material_stamps(part) for part in segments]
    mentions = [mention for part in scrubbed for mention in _proof_types(part)]
    has_other = any(
        not _same_material_proof(mention, core)
        and not _PROOF_ANAPHOR_CORE.fullmatch(mention)
        for mention in mentions
    )
    bound = False
    result: list[tuple[str, bool]] = []
    for segment, scrubbed_segment in zip(segments, scrubbed, strict=True):
        found = _proof_types(scrubbed_segment)
        asked = any(_same_material_proof(mention, core) for mention in found)
        other = any(
            not _same_material_proof(mention, core)
            and not _PROOF_ANAPHOR_CORE.fullmatch(mention)
            for mention in found
        )
        anaphor = bool(_PROOF_ANAPHOR.search(scrubbed_segment))
        if asked:
            bound = True
            allowed = True
        else:
            if other:
                bound = False
            # Identity in this clause, an already-bound continuation, or a
            # lone proper anaphor with no competing proof may carry the slot.
            allowed = bound or (anaphor and not has_other)
        result.append((segment, allowed))
    return result


def _named_locations(text: str) -> bool:
    for match in re.finditer(
        rf"([\u4e00-\u9fffA-Za-z0-9]{{2,16}}){_LOCATION_SUFFIX}", text or ""
    ):
        prefix = match.group(1)
        cleaned = _LOCATION_SCAFFOLD.sub("", prefix)
        if len(re.sub(r"[^\u4e00-\u9fffA-Za-z]", "", cleaned)) >= 2:
            return True
    return False


def _target_properties(
    part: str, protected: tuple[str, ...], scope_hint: str
) -> list[_Property]:
    properties: list[_Property] = []
    provider = _TARGET_PROVIDER_ASK.search(part)
    if provider is not None:
        return _provider_target_properties(part, provider, protected)
    location = _TARGET_LOCATION_ASK.search(part)
    if location is not None:
        core = _without_span(part, *location.span())
        core = _clean_property_core(core)
        core = _PROPERTY_MODAL.sub("", core)
        core = re.sub(
            r"^(?:学校|我校|本校|校内|地点|地方|位置|哪里|何处|哪儿|在)+",
            "",
            core,
        ).strip(_PROPERTY_EDGE)
        required = ("24小时",) if "24小时" in core else ()
        properties.append(
            _Property(
                kind="target",
                cue=re.sub(r"\s+", "", location.group(0)),
                core=core,
                direction="location",
                required=required,
                ask="地点",
            )
        )
        return properties
    target = _TARGET_TYPE_ASK.search(part)
    if target is not None:
        base = target.group("base")
        excluded = tuple(term for term in _proof_types(scope_hint) if term.endswith(base))
        properties.append(
            _Property(
                kind="target",
                cue=re.sub(r"\s+", "", target.group(0)),
                core=base,
                direction="type",
                ask=f"哪一种{base}",
                exclude=excluded,
            )
        )
        return properties
    agency = _TARGET_AGENCY_ASK.search(part)
    if agency is not None:
        business = _duration_business("", part)
        action = _agency_action(part[: agency.start()])
        core = " ".join(business) if business else action
        properties.append(
            _Property(
                kind="target",
                cue=re.sub(r"\s+", "", agency.group(0)),
                core=core,
                direction="agency",
                action=action,
                ask="处理地点",
                business=business,
            )
        )
        return properties
    capability = _CAPABILITY_ASK.search(part)
    if capability is not None and not _duration_owns_capability(part):
        rest = _without_span(part, *capability.span())
        core = _clean_property_core(rest)
        if core:
            properties.append(
                _Property(
                    kind="capability",
                    cue=re.sub(r"\s+", "", capability.group(0)),
                    core=core,
                    action=capability.group("verb"),
                    ask=f"能否{capability.group('verb')}",
                )
            )
    return properties


def _amount_properties(
    part: str, business_scope: str = "", fee_population: tuple[str, ...] = ()
) -> list[_Property]:
    properties: list[_Property] = []
    objects = _amount_objects(f"{business_scope} {part}")
    for match in _AMOUNT_VALUE_ASK.finditer(part):
        direction, tier = _amount_tier_terms(part)
        per_copy = "每份" in part
        properties.append(
            _Property(
                kind="amount",
                cue=re.sub(r"\s+", "", match.group(0)),
                core=" ".join(objects),
                direction=direction,
                measure="元",
                required=tuple(dict.fromkeys([*tier, *fee_population])),
                ask="每份费用" if per_copy else "费用",
                business=objects,
            )
        )
    return properties


def _target_supported(prop: _Property, text: str) -> bool:
    if prop.direction == "location":
        if any(term not in text for term in prop.required):
            return False
        if prop.core and not _clause_covered(prop.core, text, min_ratio=0.75):
            return False
        return _named_locations(text)
    if prop.direction == "provider":
        required = tuple(
            _canonical_property_text(term) for term in prop.required if term
        )
        equivalents = (
            _PROVIDER_ACTION_EQUIVALENT.get(prop.action, (prop.action,))
            if prop.action
            else ()
        )
        for segment in _CLAIM_SEGMENT.split(text):
            if not segment.strip():
                continue
            if any(term not in segment for term in required):
                continue
            if prop.core and not _clause_covered(
                prop.core, segment, min_ratio=0.6
            ):
                continue
            if prop.core and _person_conflict(prop.core, segment):
                continue
            if equivalents and not any(
                term in segment for term in equivalents
            ):
                continue
            if not _agency_target_present(segment):
                continue
            return True
        return False
    if prop.direction == "agency":
        objects = tuple(
            _canonical_property_text(term) for term in prop.business if term
        )
        action = prop.action
        equivalents = (
            _DURATION_ACTION_EQUIVALENT.get(action, (action,)) if action else ()
        )
        for branch in _duration_claim_branches(text):
            if objects and any(term not in branch for term in objects):
                continue
            if action and not any(term and term in branch for term in equivalents):
                continue
            if not _agency_target_present(branch):
                continue
            if prop.core and not _property_core_ok(
                prop.core, branch, min_ratio=0.5
            ):
                continue
            return True
        return False
    excluded = tuple(_canonical_property_text(term) for term in prop.exclude if term)
    for candidate in _proof_types(text):
        canonical = _canonical_property_text(candidate)
        if any(canonical == term or canonical.endswith(term) for term in excluded):
            continue
        return True
    return False


def _capability_supported(prop: _Property, text: str) -> bool:
    core = _canonical_property_text(prop.core)
    verb = prop.action
    if not verb:
        return False
    for segment in _CLAIM_SEGMENT.split(text):
        if not segment.strip():
            continue
        if core and not _clause_covered(core, segment, min_ratio=0.75):
            continue
        if core and _person_conflict(core, segment):
            continue
        if re.search(
            rf"(?:不|未|不予|不能|无法|不可|不得|不再|无需|无须)?\s*{re.escape(verb)}",
            segment,
        ):
            return True
    return False


def _amount_supported(prop: _Property, text: str) -> bool:
    text = _canonical_fee_text(text)
    business = tuple(
        _canonical_property_text(term) for term in prop.business if term
    )
    if business and not any(term in text for term in business):
        return False
    required = tuple(
        _canonical_fee_text(_canonical_property_text(term))
        for term in prop.required
        if term
    )
    core = _canonical_fee_text(_canonical_property_text(prop.core))
    segments = [part for part in _CLAIM_SEGMENT.split(text) if part.strip()]
    for index, segment in enumerate(segments):
        if not (_AMOUNT_TOKEN.search(segment) or _AMOUNT_FREE.search(segment)):
            continue
        if prop.ask.startswith("每份") and not (
            re.search(r"每份|每件|份均", segment) or _AMOUNT_FREE.search(segment)
        ):
            continue
        window = _canonical_fee_text(
            " ".join(segments[max(0, index - 2) : index + 1])
        )
        if required:
            if prop.direction in ("within", "over"):
                # A tier amount must keep its own tier and population near the
                # value; a free within-tier clause cannot answer the over-tier ask.
                if any(term not in window for term in required):
                    continue
            elif not any(term in window for term in required):
                continue
        if core and not _property_core_ok(core, text, min_ratio=0.5):
            continue
        return True
    return False


def _material_attribute_supported(prop: _Property, text: str) -> bool:
    core = _canonical_property_text(prop.core)
    if core:
        segments = [
            segment
            for segment, allowed in _material_binding_segments(text, core)
            if allowed
        ]
    else:
        segments = [part for part in _CLAIM_SEGMENT.split(text) if part.strip()]
    if prop.direction == "grade":
        return any(_hospital_grade_value(part) for part in segments)
    if prop.direction == "stamp":
        return any(_named_stamp(part) for part in segments)
    return False


_SUBMISSION_YESNO = re.compile(r"是否|需不需要|要不要|用不用|吗|嘛|呢|否")


def _submission_is_duration_scaffold(part: str) -> bool:
    """“应在几日内报送哪里” is a duration/recipient ask, not a yes/no one."""
    return bool(_DURATION_ASK.search(part)) and not _SUBMISSION_YESNO.search(part)


def _submission_is_material_scaffold(part: str) -> bool:
    """“应提交什么级别医院的证明” asks an attribute, not a yes/no submission."""
    return bool(
        _material_attribute_asks(part)
        or _EXPLICIT_MATERIAL_LIST_ASK.search(part)
    ) and not _SUBMISSION_YESNO.search(part)


def _properties_for_part(
    part: str,
    protected: tuple[str, ...],
    *,
    calendar_ask: bool = False,
    fallback_measure: str = "",
    scope_hint: str = "",
    shared_scope: str = "",
    business_scope: str = "",
    fee_population: tuple[str, ...] = (),
) -> list[_Property]:
    properties: list[_Property] = []
    boolean_asks = (
        ("payment", PAYMENT_ASK),
        ("degree", DEGREE_ASK),
        ("submission", SUBMISSION_ASK),
    )
    suppressed = _submission_is_duration_scaffold(part) or (
        _submission_is_material_scaffold(part)
    )
    matched_kinds = [
        kind
        for kind, pattern in boolean_asks
        if pattern.search(part) and not (kind == "submission" and suppressed)
    ]
    for kind, pattern in boolean_asks:
        if kind == "submission" and suppressed:
            continue
        match = pattern.search(part)
        if match is None:
            continue
        # A shared yes/no stem (“是否需要缴费和提交材料”) leaves the other
        # property's object behind; it is not this property's object.
        rest = _without_span(part, *match.span())
        for other_kind, other in boolean_asks:
            if other_kind != kind:
                rest = other.sub(" ", rest)
        if len(matched_kinds) > 1:
            rest = ""
        core = _clean_property_core(rest)
        direction, tier = _amount_tier_terms(core or part)
        business = (
            _amount_objects(f"{business_scope} {part}")
            if kind == "payment"
            else ()
        )
        head_core = (
            _amount_subject_core(core, business) if kind == "payment" else core
        )
        required = tuple(
            dict.fromkeys(
                [
                    *_required_markers(core),
                    *_period_requirement(part),
                    *tier,
                    *fee_population,
                ]
            )
        )
        for head in _property_heads(head_core, protected, split=True):
            properties.append(
                _Property(
                    kind=kind,
                    cue=re.sub(r"\s+", "", match.group(0)),
                    core=head,
                    direction=direction,
                    required=required,
                    ask=_PROPERTY_KEYWORDS[kind][0],
                    business=business,
                )
            )
    properties.extend(_target_properties(part, protected, scope_hint))
    properties.extend(_material_properties(part, scope_hint))
    properties.extend(_amount_properties(part, business_scope, fee_population))
    duration_props = _duration_properties(part, protected)
    properties.extend(duration_props)
    properties.extend(_semester_properties(part, protected, scope_hint))
    timing = TIMING_ASK.search(part)
    if timing is not None:
        action = _TIMING_ACTION.search(timing.group(0))
        action_term = action.group(0) if action is not None else ""
        if action_term:
            core = _clean_property_core(
                f"{part[: timing.start()]}{action_term}{part[timing.end() :]}"
            )
        else:
            core = _clean_property_core(_without_span(part, *timing.span()))
        calendar = calendar_ask or bool(_CALENDAR_ASK.search(part))
        for head in _property_heads(core, protected, split=True):
            properties.append(
                _Property(
                    kind="timing",
                    cue=re.sub(r"\s+", "", timing.group(0)),
                    core=head,
                    calendar=calendar,
                    action=action_term,
                    required=_required_markers(head) + _period_requirement(part),
                    ask="日期" if calendar else "时间安排",
                )
            )
    bound_measure = _requested_bound_measure(part) or fallback_measure
    period = _bound_period(part)
    bound_measures: set[str] = set()
    bound_props: list[_Property] = []
    for direction, pattern in (("max", BOUND_MAX_ASK), ("min", BOUND_MIN_ASK)):
        # A bare “至少” without a quantity unit (“至少提交哪些材料”) is not a
        # numeric bound and must not invent one.
        if not bound_measure:
            continue
        match = pattern.search(part)
        if match is None:
            continue
        raw_core = _clean_property_core(_without_span(part, *match.span()))
        raw_core = BOUND_MAX_ASK.sub("", raw_core)
        raw_core = BOUND_MIN_ASK.sub("", raw_core)
        raw_core = _clean_property_core(raw_core)
        hint_core = _clean_property_core(scope_hint)
        core = raw_core
        if not _bound_core(core, bound_measure) and hint_core:
            # “相对一年级学生人数的原则下限和上限”把对象留在了上一分句。
            core = hint_core
        required = tuple(
            dict.fromkeys([*_required_markers(raw_core or core), *period])
        )
        bound_props.append(
            _Property(
                kind="bound",
                cue=re.sub(r"[？?。\s]", "", f"{match.group(0)}{bound_measure}"),
                core=core,
                direction=direction,
                measure=bound_measure,
                required=required,
                ask=match.group(0),
            )
        )
        bound_measures.add(bound_measure)
    if _duration_duplicated_by_bound(duration_props, bound_props):
        # “至少几天” is the same day property as the explicit bound; keeping
        # the legacy exact-duration core would demand a second, differently
        # worded answer. The bound property still proves the value and unit.
        suppressed = set(duration_props)
        properties = [
            prop
            for prop in properties
            if not (prop.kind == "duration" and prop in suppressed)
        ]
        duration_props = []
    properties.extend(bound_props)
    for prop in _numeric_properties(
        part, protected, scope_hint=scope_hint, shared_scope=shared_scope
    ):
        if prop.measure in bound_measures:
            continue
        if prop.measure in ("小时", "分钟", "天", "工作日", "周") and duration_props:
            continue
        properties.append(prop)
    negative = bool(SELECTION_NEG_ASK.search(part))
    stripped = SELECTION_NEG_ASK.sub("", part)
    state = bool(_SELECTION_STATE.search(stripped))
    elective = bool(_SELECTION_POSITIVE.search(stripped)) or state
    required = "必修" in stripped
    contrast = bool(re.search(r"分别|各自", part))
    either = bool(re.search(r"还是|或者", part))
    if elective and required:
        polarities = ("any",) if either else ("selected", "required")
    elif elective and state:
        polarities = ("selected",)
    else:
        polarities = ()
    if negative:
        if contrast:
            polarities = tuple(
                dict.fromkeys(
                    [
                        *(["selected"] if elective else []),
                        *(["required"] if required else []),
                        "unselected",
                    ]
                )
            )
        else:
            polarities = (*polarities, "unselected")
    for polarity in polarities:
        properties.append(
            _Property(
                kind="selection",
                cue={
                    "selected": "选修课程",
                    "required": "必修课程",
                    "unselected": "未选修课程",
                    "any": "必修或选修课程",
                }[polarity],
                polarity=polarity,
            )
        )
    return properties


_STUDY_DURATION_LEGACY_TOPICS = ("学制", "学习年限", "修业年限", "培养年限")
_STUDY_DURATION_LEGACY_UNRELATED = re.compile(
    r"学费|费用|收费|缴纳|材料|截止|日期|时间|金额|住宿费"
)


def _is_redundant_study_duration_legacy(
    prop: _Property, fields: list[StudyDurationField]
) -> bool:
    """A year-study duration/bound ask is replaced by the scoped fields."""
    text = f"{prop.core} {prop.action} {prop.cue}"
    if _STUDY_DURATION_LEGACY_UNRELATED.search(text):
        return False
    if "休学" in text and "创业" not in text:
        # Plain suspension duration is not the study-year branch.
        return False
    conditions = {field.condition for field in fields}
    if prop.kind == "duration":
        if not any(term in prop.ask for term in ("年", "多长", "多久")):
            return False
        return (
            any(term in text for term in _STUDY_DURATION_LEGACY_TOPICS)
            or any(condition in text for condition in conditions)
            or ("休学" in text and "创业" in text)
        )
    if prop.kind == "bound":
        return prop.measure in ("年", "学年", "学期") and (
            any(term in text for term in _STUDY_DURATION_LEGACY_TOPICS)
            or any(condition in text for condition in conditions)
            or ("休学" in text and "创业" in text)
        )
    return False


def _requested_properties(
    span: str, protected: tuple[str, ...] = ()
) -> list[_Property]:
    """Detect explicitly asked properties; ordinary object mentions are ignored."""
    study_fields = study_duration_fields(span)
    shadow, mapping = _protect_terms(span, protected)
    text = _without_instructions(shadow).strip(_STEM_STRIP)
    if not text:
        return []
    parts = [
        _restore_terms(part.strip(_STEM_STRIP), mapping)
        for part in re.split(r"[，,；;。！!？?]", text)
        if part.strip()
    ]
    properties: list[_Property] = []
    calendar_ask = bool(_CALENDAR_ASK.search(text))
    carried_measure = ""
    previous_scope = ""
    shared_scope = ""
    business_scope = ""
    fee_population: tuple[str, ...] = ()
    numeric_ask_count = len(_NUMERIC_ASK.findall(text))
    for part in parts:
        if numeric_ask_count > 1:
            shared_scope = _numeric_shared_scope(part, protected) or shared_scope
        if _amount_objects(part):
            business_scope = part
        part_population = _fee_population_terms(part)
        if part_population:
            fee_population = part_population
        part_properties = _properties_for_part(
            part,
            protected,
            calendar_ask=calendar_ask,
            fallback_measure=carried_measure,
            scope_hint=previous_scope,
            shared_scope=shared_scope if numeric_ask_count > 1 else "",
            business_scope=business_scope,
            fee_population=fee_population,
        )
        properties.extend(part_properties)
        for prop in part_properties:
            if prop.kind in ("bound", "numeric") and prop.measure:
                carried_measure = prop.measure
        hint = _property_scope_hint(part, protected)
        if hint:
            previous_scope = hint
    properties.extend(
        _Property(
            kind="study_duration",
            cue=study_field.cue,
            core=study_field.condition,
            action=study_field.kind,
        )
        for study_field in study_fields
    )
    deduped: list[_Property] = []
    for prop in properties:
        if prop not in deduped:
            deduped.append(prop)
    if study_fields:
        deduped = [
            prop
            for prop in deduped
            if not _is_redundant_study_duration_legacy(prop, study_fields)
        ]
    return _with_scope_exclusions(_with_bound_exclusions(deduped))


def _property_core_ok(core: str, segment: str, *, min_ratio: float = 0.6) -> bool:
    canonical = _canonical_property_text(core)
    if len(re.sub(r"[^\u4e00-\u9fffA-Za-z]", "", canonical)) < 2:
        return True
    return _clause_covered(
        canonical, _canonical_property_text(segment), min_ratio=min_ratio
    )


# A value may not migrate from the asked person/role to another one.
_PERSON_TERMS = (
    "博士生",
    "硕士生",
    "研究生",
    "本科生",
    "学生",
    "教师",
    "老师",
    "导师",
    "辅导员",
    "班主任",
)


def _person_conflict(core: str, segment: str) -> bool:
    asked = [term for term in _PERSON_TERMS if term in core]
    if not asked or any(term in segment for term in asked):
        return False
    return any(term in segment for term in _PERSON_TERMS)


def _timing_action_conflict(action: str, segment: str) -> bool:
    if action not in _TIMING_COMPETITORS:
        return False
    scrubbed = _TIMING_ANCHOR.sub(" ", segment)
    if any(term in scrubbed for term in _TIMING_EQUIVALENT.get(action, (action,))):
        return False
    return any(term in scrubbed for term in _TIMING_COMPETITORS)


def _possessive_head_ok(core: str, segment: str) -> bool:
    """The phrase after 的 carries the actual action/object, e.g. 学生参加交流."""
    heads = [head.strip(_PROPERTY_EDGE) for head in core.split("的")[1:]]
    heads = [
        head
        for head in heads
        if len(re.sub(r"[^\u4e00-\u9fffA-Za-z]", "", head)) >= 3
    ]
    return all(_clause_covered(head, segment, min_ratio=0.75) for head in heads)


def _grade_conflict(required: tuple[str, ...], segment: str) -> bool:
    asked = {term for term in required if _GRADE_SCOPE.fullmatch(term)}
    if not asked:
        return False
    found = set(_GRADE_SCOPE.findall(segment))
    return bool(found and not found & asked)


def _level_conflict(required: tuple[str, ...], segment: str) -> bool:
    asked = {term for term in required if term in _LEVEL_TERMS}
    if not asked:
        return False
    found = {term for term in _LEVEL_CUES if term in segment}
    return bool(found and not found & asked)


def _measure_present(measure: str, text: str) -> bool:
    if not measure:
        return True
    if measure in ("比例", "%", "％"):
        return bool(re.search(r"%|％|比例|百分比", text))
    if measure == "学时":
        return bool(re.search(r"学时|小时", text))
    if measure == "天":
        # 泛称“几天”可由工作日回答；显式问工作日时另走严格分支。
        return bool(re.search(r"天|工作日|自然日", text))
    if measure == "工作日":
        return "工作日" in text
    if measure == "人":
        return bool(re.search(r"人|名|位", text))
    return measure in text


_BOUND_SCOPE_TERMS = (
    "辅修",
    "双学位",
    "主修",
    "本科",
    "研究生",
    "博士生",
    "硕士生",
    "重修",
)
_BOUND_CORE_NOISE = re.compile(r"相对(?:于)?|原则上|原则|一般|通常")
_BOUND_SELECTION = re.compile(r"修读|所选|选课|选修|选择")
_BOUND_ECHO_SELECTION = re.compile(r"修读|所选|选课|选修|选择|选")


def _bound_core(core: str, measure: str) -> str:
    text = _period_equivalent(core)
    text = _BOUND_BASELINE.sub("", text)
    if measure:
        shadow, mapping = _protect_terms(text, _MEASURE_OBJECT_NOUNS)
        text = _restore_terms(shadow.replace(measure, ""), mapping)
    for token in (
        "每人每年",
        "每生每年",
        "每人每月",
        "每生每月",
        "每年",
        "每学年",
        "每学期",
        "每月",
        "每天",
        "每周",
        "一学期",
        "一个学期",
        "比例",
        "百分比",
    ):
        text = text.replace(token, "")
    text = _BOUND_SELECTION.sub("", text)
    if measure == "门":
        # “多少门课程”的问法对象就是课程；claim 只需给出数量。
        text = text.replace("课程", "").replace("课", "")
    text = _BOUND_CORE_NOISE.sub("", text)
    text = re.sub(r"\s+", "", text)  # ask-span removal leaves a space before 选
    text = re.sub(r"(?<=[\u4e00-\u9fff])选$", "", text)
    return _clean_property_core(text)


def _bound_scope_terms(core: str) -> tuple[str, ...]:
    return tuple(term for term in _BOUND_SCOPE_TERMS if term in core)


def _bound_scope_conflict(
    own_core: str, context: str, exclude: tuple[str, ...]
) -> bool:
    own = set(_bound_scope_terms(own_core))
    return any(term and term not in own and term in context for term in exclude)


def _bound_context(segments: list[str], index: int, core: str) -> str:
    """Current clause plus the nearest earlier clause carrying the same core."""
    if not core:
        return segments[index] if index < len(segments) else ""
    start = index
    for cursor in range(index - 1, -1, -1):
        if _property_core_ok(core, segments[cursor], min_ratio=0.5):
            start = cursor
            break
    return "，".join(segments[start : index + 1])


def _with_bound_exclusions(properties: list[_Property]) -> list[_Property]:
    """A sibling bound scope (辅修/本科/研究生) cannot answer this one."""
    cores = [
        prop.core for prop in properties if prop.kind == "bound" and prop.core
    ]
    result: list[_Property] = []
    for prop in properties:
        if prop.kind != "bound" or not prop.core:
            result.append(prop)
            continue
        own = set(_bound_scope_terms(prop.core))
        exclude = tuple(
            dict.fromkeys(
                term
                for core in cores
                if core != prop.core
                for term in _bound_scope_terms(core)
                if term not in own
            )
        )
        result.append(replace(prop, exclude=exclude) if exclude else prop)
    return result


_NUMERIC_SCOPE_NOISE = re.compile(
    r"20\d{2}\s*[—–~～\-至]\s*20\d{2}\s*年?级?"
    r"|20\d{2}\s*年?级?"
    r"|全日制|非全日制|分别|各自|何时|什么时候|完成|时间安排|安排"
    r"|一般|原则上|原则|通常|累计"
)
_SCOPE_PERIOD = re.compile(
    r"每(?:人每|生每)?(?:年|月|学期|学年|天|周)|一(?:个)?学期"
)


def _strip_protected(text: str, protected: tuple[str, ...]) -> str:
    for term in sorted(
        {term for term in protected if term and len(term) >= 2},
        key=len,
        reverse=True,
    ):
        text = text.replace(term, " ")
    return text


# A long numeric token is a course identity, never a credit value. Six digits
# keeps calendar years (`2024—2025`) out while accepting real course codes.
_NUMERIC_COURSE_CODE = re.compile(
    r"(?<![0-9A-Za-z])([A-Za-z]{0,6}\d{6,}[A-Za-z0-9-]*)(?![0-9A-Za-z])"
)


def _numeric_course_codes(text: str) -> tuple[str, ...]:
    return tuple(
        dict.fromkeys(match.group(1) for match in _NUMERIC_COURSE_CODE.finditer(text or ""))
    )


_NUMERIC_COHORT_BOUNDARY = re.compile(
    r"20\d{2}\s*(?:[—–~～\-至]\s*20\d{2}\s*)?(?:年?级|学年)"
)
_NUMERIC_ENTITY_NOISE = re.compile(
    r"材料|手续|备案|证明|准备|相关|办理|提交|申请|应当|应该|需要|必须"
)
_NUMERIC_ENTITY_LEAD = re.compile(
    r"^(?:请问|帮我|我想问|我想|想问|关于|对于|根据|按照|涉及|以及)+"
)


def _numeric_generic_entity_anchors(text: str) -> tuple[str, ...]:
    """An unknown entity named immediately before its explicit cohort boundary.

    Only the atomic name before “2024—2025级” is an identity anchor; the year
    range and the procedural question tail are not.
    """
    match = _NUMERIC_COHORT_BOUNDARY.search(text or "")
    if match is None:
        return ()
    prefix = (text or "")[: match.start()].strip(_STEM_STRIP)
    prefix = _NUMERIC_ENTITY_LEAD.sub("", prefix).strip(_STEM_STRIP)
    if not prefix:
        return ()
    candidate = re.split(
        r"[\s　，,。；;：:、（）()【】\[\]？?]", prefix
    )[-1]
    candidate = _NUMERIC_ENTITY_LEAD.sub("", candidate).strip(_STEM_STRIP)
    chars = re.sub(r"[^\u4e00-\u9fffA-Za-z]", "", candidate)
    if not (2 <= len(chars) <= 16):
        return ()
    if _NUMERIC_ENTITY_NOISE.search(candidate):
        return ()
    return (candidate,)


def _numeric_identity_anchors(
    text: str, protected: tuple[str, ...]
) -> tuple[str, ...]:
    """Identity is a code, a registered name, or an atomic unknown entity.

    Procedural question scaffolding (“应准备/相关/及”) is not an identity;
    treating it as an exact head makes complete verified facts look missing.
    """
    codes = _numeric_course_codes(text)
    if codes:
        return codes
    anchors: list[str] = []
    for term in sorted(
        {term for term in protected if term and len(term) >= 2},
        key=len,
        reverse=True,
    ):
        if term in text:
            anchors.append(term)
    anchors.extend(_numeric_generic_entity_anchors(text))
    return tuple(dict.fromkeys(anchors))


def _numeric_required_identity(
    text: str, protected: tuple[str, ...]
) -> tuple[str, ...]:
    """Codes and unknown entities bind the body; registered names use unit gates."""
    return tuple(
        term
        for term in _numeric_identity_anchors(text, protected)
        if term not in protected
    )


_PROPERTY_SCOPE_NOISE = re.compile(r"一般|原则上|原则|通常|累计")


def _property_scope_hint(part: str, protected: tuple[str, ...] = ()) -> str:
    """Carry the object of an earlier comma clause into a bare follow-up ask."""
    text = _strip_protected(part, protected)
    for pattern in (
        _NUMERIC_ASK,
        _DURATION_ASK,
        TIMING_ASK,
        BOUND_MAX_ASK,
        BOUND_MIN_ASK,
        _RATIO_ASK,
        _BOUND_BASELINE,
        _DATE_ASK,
        _SCOPE_PERIOD,
        _PROPERTY_SCOPE_NOISE,
    ):
        text = pattern.sub(" ", text)
    return _clean_property_core(text).strip(_PROPERTY_EDGE)


def _numeric_core(
    text: str, protected: tuple[str, ...] = (), measure: str = ""
) -> str:
    text = _strip_protected(text, protected)
    text = _NUMERIC_ASK.sub(" ", text)
    text = _DURATION_ASK.sub(" ", text)
    text = TIMING_ASK.sub(" ", text)
    text = BOUND_MAX_ASK.sub(" ", text)
    text = BOUND_MIN_ASK.sub(" ", text)
    text = _DATE_ASK.sub(" ", text)
    text = _NUMERIC_SCOPE_NOISE.sub(" ", text)
    text = _NUMERIC_EFFECT.sub(" ", text)
    text = text.replace("总共", "总").replace("一共", "总")
    text = _NUMERIC_COUNT_SCAFFOLD.sub(" ", text)
    text = _SCOPE_PERIOD.sub(" ", text)
    text = _BOUND_VALUE.sub(" ", text)
    if measure:
        # The unit can also sit inside an object noun (“人数”, “课程”).
        shadow, mapping = _protect_terms(text, _MEASURE_OBJECT_NOUNS)
        text = _restore_terms(shadow.replace(measure, " "), mapping)
    core = _clean_property_core(text)
    return core.strip(_PROPERTY_EDGE)


_NUMERIC_SHARED_SCOPE_STOP = frozenset(
    {
        "总",
        "总共",
        "全部",
        "合计",
        "一共",
        "各",
        "都",
        "均",
        "课程",
        "课",
        "门",
        "学分",
        "学期",
        "周期",
        "培养周期",
        "人数",
        "名额",
        "数量",
    }
)
_NUMERIC_SHARED_SCOPE_NOISE = re.compile(
    r"材料|手续|备案|证明|准备|相关|办理|提交|申请|应当|应该|需要|必须|及"
)


def _numeric_shared_scope(segment: str, protected: tuple[str, ...]) -> str:
    """Raw noun head shared by enumerated count fields, sans count scaffolding."""
    first = _NUMERIC_ASK.search(segment)
    if first is None:
        return ""
    prefix = segment[: first.start()]
    codes = _numeric_course_codes(prefix)
    if codes:
        return " ".join(codes)
    cleaned = _numeric_core(prefix, protected, "")
    cleaned = _QUANTITY_HEAD_SCAFFOLD.sub("", cleaned).strip(_PROPERTY_EDGE)
    chars = re.sub(r"[^\u4e00-\u9fffA-Za-z]", "", cleaned)
    if not (2 <= len(chars) <= 16):
        return ""
    if cleaned in _NUMERIC_SHARED_SCOPE_STOP:
        return ""
    if _NUMERIC_SHARED_SCOPE_NOISE.search(cleaned):
        return ""
    return cleaned


def _numeric_measure(
    match: re.Match[str], part: str
) -> tuple[str, str] | None:
    cue = re.sub(r"\s+", "", match.group(0))
    unit = match.groupdict().get("unit") or ""
    if unit in ("名", "位"):
        # “多少位”也可能是学号位数；只有明确的人群语境才按人数处理。
        if unit == "位" and not re.search(
            r"人数|人员|学生|教师|老师|名单|名额|成员", part
        ):
            return None
        return cue, "人"
    if unit:
        return cue, unit
    if "人数" in cue:
        return cue, "人"
    if "份" in cue:
        return cue, "份"
    return None


def _split_quantity_heads(text: str, protected: tuple[str, ...]) -> list[str]:
    shadow, mapping = _protect_terms(text, protected)
    heads: list[str] = []
    for raw in _HEAD_SPLIT.split(shadow):
        head = _clean_property_core(raw)
        head = _QUANTITY_HEAD_SCAFFOLD.sub("", head).strip(_PROPERTY_EDGE)
        head = _restore_terms(head, mapping)
        chars = re.sub(r"[^\u4e00-\u9fffA-Za-z]", "", head)
        if len(chars) >= 2 or _numeric_course_codes(head):
            heads.append(head)
    return heads


def _numeric_shared_properties(
    part: str,
    ask: re.Match[str],
    heads: list[str],
    protected: tuple[str, ...],
    scope_hint: str,
    shared_scope: str = "",
) -> list[_Property]:
    parsed = _numeric_measure(ask, part)
    if parsed is None:
        return []
    cue, measure = parsed
    effects = tuple(dict.fromkeys(_NUMERIC_EFFECT_OBJECT.findall(part)))
    properties: list[_Property] = []
    for head in heads:
        codes = _numeric_course_codes(head)
        identity = codes or _numeric_required_identity(head, protected)
        core = " ".join(codes) if codes else _numeric_core(
            head, protected, measure
        )
        if not core:
            core = _numeric_core(scope_hint, protected, measure)
        required = tuple(
            dict.fromkeys(
                [
                    *_required_markers(core),
                    *_bound_period(part),
                    *effects,
                    *identity,
                    *((shared_scope,) if shared_scope else ()),
                ]
            )
        )
        properties.append(
            _Property(
                kind="numeric",
                cue=cue,
                core=core,
                measure=measure,
                required=required,
                ask=cue,
            )
        )
    return properties


def _numeric_properties(
    part: str,
    protected: tuple[str, ...],
    *,
    scope_hint: str = "",
    shared_scope: str = "",
) -> list[_Property]:
    properties: list[_Property] = []
    segments = _split_terms(part, _ENUM_SPLIT, protected) or [part]
    asks = list(_NUMERIC_ASK.finditer(part))
    if len(asks) > 1:
        # Coordinated count fields share the first named program noun head.
        shared_scope = _numeric_shared_scope(part, protected) or shared_scope
    if len(asks) == 1:
        heads = _split_quantity_heads(part[: asks[0].start()], protected)
        if len(heads) > 1:
            # One shared count ask governing several business heads: each head
            # needs its own value, so one object's count cannot borrow another.
            return _numeric_shared_properties(
                part, asks[0], heads, protected, scope_hint, shared_scope
            )
    first_ask = _NUMERIC_ASK.search(segments[0])
    shared_pre = segments[0][: first_ask.start()] if first_ask is not None else ""
    shared_codes = _numeric_course_codes(shared_pre)
    shared_identity = _numeric_required_identity(shared_pre, protected)
    effects = tuple(dict.fromkeys(_NUMERIC_EFFECT_OBJECT.findall(part)))
    for index, segment in enumerate(segments):
        for match in _NUMERIC_ASK.finditer(segment):
            parsed = _numeric_measure(match, part)
            if parsed is None:
                continue
            cue, measure = parsed
            pre = segment[: match.start()]
            codes = _numeric_course_codes(pre)
            if index and not codes and shared_codes:
                codes = shared_codes
            identity = codes or _numeric_required_identity(pre, protected)
            if index and not identity:
                identity = shared_identity
            # An exact code is sufficient identity; requiring the surrounding
            # title would make the answer depend on wording the user did not
            # need to repeat. The unit gate handles protected program names.
            core = (
                " ".join(codes)
                if codes
                else _numeric_core(
                    segment[: match.start()] + segment[match.end() :],
                    protected,
                    measure,
                )
            )
            if not core:
                core = _numeric_core(scope_hint, protected, measure)
            required = tuple(
                dict.fromkeys(
                    [
                        *_required_markers(core),
                        *_bound_period(part),
                        *effects,
                        *identity,
                        *((shared_scope,) if shared_scope else ()),
                    ]
                )
            )
            properties.append(
                _Property(
                    kind="numeric",
                    cue=cue,
                    core=core,
                    measure=measure,
                    required=required,
                    ask=cue,
                )
            )
    return properties


# A course-enrollment threshold and its stop-course effect may be split by the
# grammar: “当选课人数少于15人时，该课程停开”. Bind only a comma-adjacent
# condition/anaphor pair; unrelated person counts and negated effects must not
# borrow the threshold.
_COURSE_ENROLLMENT_COUNT = re.compile(
    r"(?:选课|修读|选读|修课|修习|上课)人数"
    r"|课程(?:的)?(?:学生|选课|修读|选读|修课|修习)?人数"
)
_COURSE_ENROLLMENT_LIMIT = re.compile(
    r"不足"
    r"|(?<!不)(?<!得)(?<!能)(?<!未)少于"
    r"|(?<!不)(?<!得)(?<!能)(?<!未)低于"
    r"|不超过|至多|不多于"
    r"|(?<!不)(?<!得)(?<!能)(?<!未)小于"
)
_COURSE_STOP_WORD = re.compile(r"停开|停止开课")
_COURSE_STOP_EFFECT = re.compile(
    r"\s*(?:(?:该|此|本|这门|上述|前述)(?:门)?)?课程?"
    r"(?:原则上|一般|通常)?(?:将|会)?(?:停开|停止开课)(?:的)?\s*"
)
_COURSE_STOP_NEGATED = re.compile(
    r"(?:不|未|没有|不会|不予|不再|不得|无需|无须)[^。；，,？?]{0,3}"
    r"(?:停开|停止开课)"
    r"|(?:停开|停止开课)[^。；，,？?]{0,2}(?:吗|么|否)"
)
_COURSE_STOP_OTHER = re.compile(r"另一|其他|其它|别的|不同")
_COURSE_STOP_RELATION = re.compile(
    r"(?P<count>[^。；;！？!?，,]+?)[，,]\s*(?P<effect>[^。；;！？!?，,]+)"
)


def _course_stop_asked(core: str, measure: str) -> bool:
    return (
        measure == "人"
        and bool(_COURSE_ENROLLMENT_COUNT.search(core))
        and bool(_COURSE_STOP_WORD.search(core))
    )


def _course_stop_relation_ok(
    prop: _Property,
    core: str,
    numeric_core: str,
    text: str,
    required: tuple[str, ...],
    exclude: tuple[str, ...],
) -> bool:
    if not numeric_core:
        return False

    def bound(candidate: str) -> bool:
        if not _COURSE_ENROLLMENT_COUNT.search(candidate):
            return False
        if not _COURSE_ENROLLMENT_LIMIT.search(candidate):
            return False
        if _COURSE_STOP_OTHER.search(candidate):
            return False
        if _COURSE_STOP_NEGATED.search(candidate):
            return False
        if not _COUNT_VALUE.search(candidate) or not _measure_present(
            prop.measure, candidate
        ):
            return False
        if any(term not in candidate for term in required) and not all(
            term in text for term in required
        ):
            return False
        if _grade_conflict(required, candidate) or _level_conflict(
            required, candidate
        ):
            return False
        if _person_conflict(core, candidate):
            return False
        if _bound_scope_conflict(core, candidate, exclude):
            return False
        if not _property_core_ok(numeric_core, candidate, min_ratio=0.5):
            return False
        return _possessive_head_ok(numeric_core, candidate)

    for segment in _CLAIM_SEGMENT.split(text):
        segment = segment.strip()
        if segment and _COURSE_STOP_WORD.search(segment) and bound(segment):
            return True
    for match in _COURSE_STOP_RELATION.finditer(text):
        count = match.group("count").strip()
        effect = match.group("effect").strip()
        if not _COURSE_STOP_EFFECT.fullmatch(effect):
            continue
        if bound(f"{count}，{effect}"):
            return True
    return False


def _numeric_supported(prop: _Property, text: str) -> bool:
    core = _canonical_property_text(prop.core)
    text = _period_equivalent(text)
    required = tuple(
        _canonical_property_text(term) for term in prop.required if term
    )
    if not any(term in text for term in _LEVEL_CUES):
        required = tuple(term for term in required if term not in _LEVEL_TERMS)
    exclude = tuple(
        _canonical_property_text(term) for term in prop.exclude if term
    )
    segments = [part for part in _CLAIM_SEGMENT.split(text) if part.strip()]
    if not segments:
        return False
    numeric_core = _bound_core(core, prop.measure)
    if _course_stop_asked(core, prop.measure):
        # The requested relation is a course-enrollment threshold plus a stop
        # effect. Keep it local: a broad clause window must not borrow a count
        # from a different entity or neighbouring sentence.
        return _course_stop_relation_ok(
            prop, core, numeric_core, text, required, exclude
        )
    core_seen = bool(numeric_core) and any(
        _property_core_ok(numeric_core, part, min_ratio=0.5)
        and not _person_conflict(core, part)
        and not _grade_conflict(required, part)
        and not _level_conflict(required, part)
        for part in segments
    )
    for index, segment in enumerate(segments):
        if any(term not in segment for term in required) and not all(
            term in text for term in required
        ):
            continue
        if _grade_conflict(required, segment):
            continue
        if _level_conflict(required, segment):
            continue
        if _person_conflict(core, segment):
            continue
        if not _measure_present(prop.measure, segment):
            continue
        if not _COUNT_VALUE.search(segment):
            continue
        context = segment
        if not _property_core_ok(numeric_core, segment, min_ratio=0.5):
            if not numeric_core or not core_seen:
                continue
            context = _bound_context(segments, index, numeric_core)
            if not context or not _property_core_ok(
                numeric_core, context, min_ratio=0.5
            ):
                continue
            if _person_conflict(core, context):
                continue
            if _bound_scope_conflict(prop.core, context, exclude):
                continue
        if not _possessive_head_ok(numeric_core, context):
            continue
        return True
    return False


def _without_uncertain(text: str) -> str:
    return "".join(
        part
        for part in _CLAUSE_BOUNDARY.split(text)
        if part.strip() and not _UNCERTAIN.search(part)
    )


def _duration_business_ok(
    business: tuple[str, ...], part: str, asked: str
) -> bool:
    """Business nouns must be answered in the same duration branch."""
    objects = tuple(term for term in business if term in _DURATION_BUSINESS_OBJECTS)
    if any(term not in part for term in objects):
        return False
    if "告知" in business and not _DURATION_NOTIFY.search(part):
        # A bare 告知 ask needs its own cue. When the question also names a
        # business object, the generic 处理 may be expressed by 统计/预警.
        if not objects or asked != "处理":
            return False
        if not any(term in part for term in _DURATION_PROCESSING):
            return False
    return True


def _duration_supported(prop: _Property, text: str) -> bool:
    core = _canonical_property_text(prop.core)
    required = tuple(
        _canonical_property_text(term) for term in prop.required if term
    )
    exclude = tuple(
        _canonical_property_text(term) for term in prop.exclude if term
    )
    asked = _canonical_property_text(prop.action)
    business = tuple(
        _canonical_property_text(term) for term in prop.business if term
    )
    for part in _duration_claim_branches(text):
        if not part.strip() or not _DURATION_TOKEN.search(part):
            continue
        if any(term not in part for term in required):
            continue
        if any(term and term in part for term in exclude):
            continue
        if _person_conflict(core, part):
            continue
        if not _clause_covered(core, part, min_ratio=0.75):
            continue
        if not _duration_business_ok(business, part, asked):
            continue
        if asked and asked not in part:
            equivalents = tuple(
                term
                for term in _DURATION_ACTION_EQUIVALENT.get(asked, ())
                if term and term in part
            )
            # “入学满一学期” need not repeat the asked action; a different
            # concrete action (“开学3周内公布” vs 备案) still competes.
            residual = part
            for term in (prop.core, *prop.required, *business, *equivalents):
                if term:
                    residual = residual.replace(term, " ")
            residual = _DURATION_TOKEN.sub(" ", residual)
            if _DURATION_ACTION_WORD.search(residual):
                continue
            if equivalents:
                return True
        return True
    return False


def _semester_supported(prop: _Property, text: str) -> bool:
    core = _canonical_property_text(prop.core)
    required = tuple(
        _canonical_property_text(term) for term in prop.required if term
    )
    text = _canonical_property_text(text)
    # The course identity may be established in an earlier comma clause; the
    # actual term must still appear as a value in this claim.
    if any(term not in text for term in required):
        return False
    for segment in _CLAIM_SEGMENT.split(text):
        if not segment.strip() or not _SEMESTER_TOKEN.search(segment):
            continue
        if _person_conflict(core, segment):
            continue
        if core and not _property_core_ok(core, segment, min_ratio=0.5):
            continue
        return True
    return False


def _property_supported(prop: _Property, text: str) -> bool:
    if prop.kind == "study_duration":
        # The scoped helper owns local uncertainty, question echo and quotes;
        # the legacy whole-fragment filter would drop a supported branch.
        # Dispatch before alias canonicalization: 学习年限→学制 would collapse
        # basic and maximum asks into one property.
        field = StudyDurationField(
            condition=prop.core, kind=prop.action, cue=prop.cue
        )
        return study_duration_supported(field, text)
    text = _without_uncertain(text)
    if not text:
        return False
    text = _canonical_property_text(text)
    core = _canonical_property_text(prop.core)
    required = tuple(
        _canonical_property_text(term) for term in prop.required if term
    )
    if prop.kind in ("payment", "amount"):
        text = _canonical_fee_text(text)
        core = _canonical_fee_text(core)
        required = tuple(_canonical_fee_text(term) for term in required)
    if prop.kind == "material_attribute":
        return _material_attribute_supported(prop, text)
    if prop.kind in _BOOL_PREDICATE:
        predicate = _BOOL_PREDICATE[prop.kind]
        negative = _BOOL_NEGATIVE[prop.kind]
        affirmative = _BOOL_AFFIRM[prop.kind]
        if prop.kind == "payment" and prop.business:
            business = tuple(
                _canonical_property_text(term) for term in prop.business if term
            )
            if not any(term in text for term in business):
                return False
        for segment in _CLAIM_SEGMENT.split(text):
            if not segment.strip():
                continue
            if any(term not in segment for term in required):
                continue
            if _person_conflict(core, segment):
                continue
            if _BOOL_NOISE.search(segment) and not negative.search(segment):
                continue
            if not predicate.search(segment):
                continue
            if not (negative.search(segment) or affirmative.search(segment)):
                continue
            if _property_core_ok(core, segment):
                return True
        return False
    if prop.kind == "duration":
        return _duration_supported(prop, text)
    if prop.kind == "semester":
        return _semester_supported(prop, text)
    if prop.kind == "target":
        return _target_supported(prop, text)
    if prop.kind == "capability":
        return _capability_supported(prop, text)
    if prop.kind == "amount":
        return _amount_supported(prop, text)
    if prop.kind == "timing":
        token = _CALENDAR_TOKEN if prop.calendar else _TIMING_TOKEN
        if not token.search(text):
            return False
        if not re.sub(r"[^\u4e00-\u9fffA-Za-z]", "", core):
            return True
        segments = [
            part
            for part in _CLAIM_SEGMENT.split(text)
            if part.strip()
            and token.search(part)
            and all(term in part for term in required)
            and not _person_conflict(core, part)
            and not _timing_action_conflict(prop.action, part)
        ]
        return any(_clause_covered(core, part, min_ratio=0.75) for part in segments)
    if prop.kind == "bound":
        bound_core = _bound_core(core, prop.measure)
        marker = _BOUND_MAX_TOKEN if prop.direction == "max" else _BOUND_MIN_TOKEN
        text = _period_equivalent(text)
        segments = [
            part for part in _CLAIM_SEGMENT.split(text) if part.strip()
        ]
        core_seen = bool(bound_core) and any(
            _property_core_ok(bound_core, part, min_ratio=0.5)
            and not _person_conflict(bound_core, part)
            and not _grade_conflict(required, part)
            for part in segments
        )
        for index, segment in enumerate(segments):
            # “每学期最低20学时，最高40学时” shares one period across clauses.
            if any(term not in segment for term in required) and not all(
                term in text for term in required
            ):
                continue
            if _grade_conflict(required, segment):
                continue
            if _person_conflict(bound_core, segment):
                continue
            if not _measure_present(prop.measure, segment):
                continue
            if not _BOUND_VALUE.search(segment):
                continue
            context = segment
            if not _property_core_ok(bound_core, segment, min_ratio=0.5):
                if not bound_core or not core_seen:
                    continue
                context = _bound_context(segments, index, bound_core)
                if not context or not _property_core_ok(
                    bound_core, context, min_ratio=0.5
                ):
                    continue
                if _person_conflict(bound_core, context):
                    continue
                if _bound_scope_conflict(core, context, prop.exclude):
                    continue
            if not _possessive_head_ok(bound_core, context):
                continue
            if _RANGE_VALUE.search(segment) or marker.search(segment):
                return True
        return False
    if prop.kind == "numeric":
        return _numeric_supported(prop, text)
    if prop.kind == "selection":
        for segment in _CLAIM_SEGMENT.split(text):
            if not segment.strip():
                continue
            if any(term not in segment for term in required):
                continue
            if not _property_core_ok(core, segment):
                continue
            # Remove negated spans first so “没有选修” cannot also read as
            # a positive selection statement.
            positive = _NEGATIVE_ENROLMENT_STATE.sub("", segment)
            positive = SELECTION_NEG_ASK.sub("", positive)
            if prop.polarity == "unselected" and (
                SELECTION_NEG_ASK.search(segment)
                or _NEGATIVE_ENROLMENT_STATE.search(segment)
            ):
                return True
            if prop.polarity == "selected" and (
                _SELECTION_POSITIVE.search(positive)
                or _SELECTION_STATE.search(positive)
            ):
                return True
            if prop.polarity == "required" and "必修" in positive:
                return True
            if prop.polarity == "any" and (
                SELECTION_WORD.search(positive) or _SELECTION_STATE.search(positive)
            ):
                return True
        return False
    return False


def _property_covers_aspect(prop: _Property, aspect: str) -> bool:
    mapped: set[str] = set()
    if prop.kind == "timing":
        mapped = {"时间安排"}
    elif prop.kind == "duration":
        mapped = {"学制"}
        if prop.core and prop.core in aspect:
            return True
        if _DURATION_ASK.search(aspect):
            equivalents = (
                _DURATION_ACTION_EQUIVALENT.get(prop.action, (prop.action,))
                if prop.action
                else ()
            )
            if (
                not prop.action
                or any(term and term in aspect for term in equivalents)
                or any(term and term in aspect for term in prop.business)
            ):
                return True
    elif prop.kind == "study_duration":
        mapped = {"学制", "学习年限", "修业年限", "培养年限"}
    elif prop.kind == "degree":
        mapped = {"授予学位"}
    elif prop.kind == "submission":
        mapped = {"提交材料"}
    elif prop.kind == "material_attribute":
        mapped = {"提交材料", "材料", "证明", "证书", "医院级别", "盖章", "盖章要求"}
    elif prop.kind == "selection":
        mapped = {"选修课程"}
    elif prop.kind == "payment":
        mapped = {"费用"}
        if PAYMENT_ASK.search(aspect) or "收费" in aspect:
            return True
    elif prop.kind == "amount":
        mapped = {"费用"}
        if _AMOUNT_ASK.search(aspect) or _AMOUNT_VALUE_ASK.search(aspect):
            return True
    elif prop.kind == "target":
        if prop.direction == "location":
            mapped = {"地点", "位置", "办理地点"}
        elif prop.direction in ("agency", "provider"):
            mapped = {"办理地点", "处理地点", "受理部门", "办理部门"}
            if prop.direction == "provider":
                mapped.add("提供方")
            if re.search(r"哪里|何处|哪儿|部门|机关|单位|机构|处理", aspect) or (
                prop.direction == "provider"
                and re.search(r"提供|出具|开具|发放|提供方", aspect)
            ):
                return True
        else:
            mapped = {"证书", "证明"}
    elif prop.kind == "capability":
        mapped = {"办理程序", "申请条件"}
    elif prop.kind == "semester":
        mapped = {"开课学期"}
    elif prop.kind == "bound":
        if prop.measure == "学分":
            mapped = {"毕业学分", "申请条件"}
        elif prop.measure in ("年", "学期", "学年"):
            mapped = {"学制"}
    if aspect in mapped:
        return True
    return bool(prop.core) and len(aspect) >= 2 and aspect in prop.core


_PROPERTY_KEYWORDS = {
    "payment": ("缴费", "交费", "收费", "缴纳", "费用"),
    "amount": ("多少钱", "多少元", "单价", "每份", "费用", "价格", "金额", "收费"),
    "target": ("地点", "哪里", "何处", "哪一种", "哪种", "证明", "证书", "可以", "能", "能够", "提供方"),
    "capability": ("能否", "能不能", "可以", "能够", "能", "办理", "出具"),
    "degree": ("学位", "授予", "获得"),
    "submission": ("提交", "递交", "上交", "报送"),
    "material_attribute": ("材料", "证明", "证书"),
    "duration": ("多久", "几日", "几天", "多长时间", "多长", "学制", "修业", "年限", "就读时间"),
    "timing": ("时间安排", "日程安排", "何时", "什么时候", "第几学期", "完成时间"),
    "bound": ("最高", "最低", "上限", "下限", "比例", "每年", "最多", "最少"),
    "selection": ("选修", "必修", "已选", "未选"),
    "numeric": ("多少", "几份", "几人", "数量"),
    "semester": ("开课学期", "春季", "秋季", "学期"),
}


def _contradicts_covered_property(
    item: str, covered: list[_Property]
) -> bool:
    """Drop model gap echoes that a concrete verified property answer contradicts.

    Numbered/list omissions stay: the model may have found a real missing row
    even when the property itself was answered.
    """
    if _LIST_OMISSION.search(item):
        return False
    if any(prop.kind == "study_duration" for prop in covered) and _clause_is_property_echo(
        item, covered
    ):
        return True
    if _material_clause_echo(item, covered):
        return True
    for prop in covered:
        terms = [prop.core, prop.measure, *_PROPERTY_KEYWORDS.get(prop.kind, ())]
        if any(term and len(term) >= 2 and term in item for term in terms):
            return True
    return False


_STUDY_DURATION_ECHO = re.compile(
    r"全日制|非全日制|硕士|博士|本科|研究生|学生|专业|学习|修读|休学|创业|"
    r"[0-9零〇一二两三四五六七八九十百]{1,4}年制|"
    r"[零〇一二两三四五六七八九十百]{1,3}年(?!制|级|度)|"
    r"基本|学制|学习年限|修业年限|培养年限|最长|最短|通常|一般|原则上|"
    r"分别|各自|各|均|都|统一|几年|多少年|多少|多久|多长|多长时间|年限|"
    r"如何|怎么|怎样|什么|哪些|哪个|哪一|比较|对比|区别|差异|更长|"
    r"未提供|未说明|未明确|未提及|未给出|缺失|缺少|不详|不明确|"
    r"需要|需|是|为|有|的|在|内|期间|以及|或者|和|与|及"
)
_STUDY_DURATION_MODES = re.compile(r"非全日制|全日制")
_STUDY_DURATION_YEAR_SYSTEMS = re.compile(
    r"[0-9零〇一二两三四五六七八九十百]{1,4}年制"
)
# Only ask-scaffold modals may be ignored by study echo stripping; other
# modalities (应当/必须 etc.) can carry an actual requirement.
_STUDY_ECHO_MODAL = re.compile(r"可以|能够|可")


def _study_clause_echo(
    clause: str, properties: list[_Property] | tuple[_Property, ...]
) -> bool:
    study = [prop for prop in properties if prop.kind == "study_duration"]
    if not study:
        return False
    if (
        _AMOUNT_ASK.search(clause)
        or _DATE_ASK.search(clause)
        or _LIST_OMISSION.search(clause)
    ):
        return False
    conditions = {prop.core for prop in study}
    actions = {prop.action for prop in study}
    if "休学" in clause and "创业" not in clause:
        return False
    if ("休学" in clause or "创业" in clause) and "startup_max" not in actions:
        return False
    if "基本" in clause and "basic" not in actions:
        return False
    if set(_STUDY_DURATION_MODES.findall(clause)) - conditions:
        return False
    if set(_STUDY_DURATION_YEAR_SYSTEMS.findall(clause)) - conditions:
        return False
    remainder = clause
    for term in sorted(
        {
            term
            for prop in study
            for term in (prop.core, prop.cue)
            if term
        },
        key=len,
        reverse=True,
    ):
        remainder = remainder.replace(term, " ")
    remainder = _STUDY_DURATION_ECHO.sub("", remainder)
    remainder = _STUDY_ECHO_MODAL.sub("", remainder)
    remainder = re.sub(r"[^\u4e00-\u9fffA-Za-z]", "", remainder)
    return len(remainder) <= 1


_MATERIAL_YESNO = re.compile(
    r"是否|能否|可否|能不能|可不可以|要不要|用不用|需不需要|吗|嘛|呢"
)
_MATERIAL_ECHO_SCAFFOLD = re.compile(
    r"提供|提交|递交|上交|报送|出具|开具|携带|准备|"
    r"以及|或者|和|与|及|并|且|还|同时|统一|相关|有关|该|此|本|其|"
    r"需|须|应|应当|应该|要|需要|必须|请|可以|能够|可|能|的|"
    r"什么|何种|哪种|哪些|哪个|哪一|怎么|如何|是否"
)


def _material_clause_echo(
    clause: str, properties: list[_Property] | tuple[_Property, ...]
) -> bool:
    """A material-attribute clause is covered by its own typed properties."""
    material = [prop for prop in properties if prop.kind == "material_attribute"]
    if not material:
        return False
    if (
        _DATE_ASK.search(clause)
        or _AMOUNT_ASK.search(clause)
        or _LIST_OMISSION.search(clause)
        or _MATERIAL_YESNO.search(clause)
    ):
        return False
    asks_attribute = any(
        (prop.direction == "grade" and _MATERIAL_GRADE_ASK.search(clause))
        or (prop.direction == "stamp" and _MATERIAL_STAMP_ASK.search(clause))
        for prop in material
    )
    if not asks_attribute and any(
        prop.core and prop.core in clause for prop in material
    ):
        # “需提供检查证明” may be a real provision/yes-no requirement; only the
        # bare scaffold (“应提供”) is an echo of the attribute asks.
        return False
    remainder = clause
    for prop in material:
        if prop.direction == "grade":
            remainder = _MATERIAL_GRADE_ASK.sub(" ", remainder)
        elif prop.direction == "stamp":
            remainder = _MATERIAL_STAMP_ASK.sub(" ", remainder)
        if prop.core:
            remainder = remainder.replace(prop.core, " ")
    if any(prop.direction == "grade" for prop in material):
        remainder = re.sub(r"医院级别|级别|等级", " ", remainder)
    if any(prop.direction == "stamp" for prop in material):
        remainder = re.sub(r"盖章|用印|印章|公章|专用章", " ", remainder)
    remainder = _MATERIAL_ECHO_SCAFFOLD.sub("", remainder)
    remainder = _QUESTION_SKELETON.sub("", remainder)
    remainder = re.sub(r"[^\u4e00-\u9fffA-Za-z]", "", remainder)
    return len(remainder) <= 1


def _clause_is_property_echo(
    clause: str, properties: list[_Property] | tuple[_Property, ...]
) -> bool:
    """A clause that only restates an explicitly asked property is not a gap.

    The property itself is still checked against the claims, so dropping this
    surface echo cannot hide an unanswered value.
    """
    if _study_clause_echo(clause, properties):
        return True
    if _material_clause_echo(clause, properties):
        return True
    for prop in properties:
        if (
            prop.kind == "duration"
            and prop.business
            and any(term in clause for term in prop.business if term)
        ):
            # A split list item (“相关试卷”) only repeats one business object;
            # the duration property itself still requires every asked object,
            # action and the full business list.
            remainder = clause
            for term in (*prop.business, prop.core, prop.action, *prop.required):
                if term:
                    remainder = remainder.replace(term, "")
            remainder = _DURATION_ASK.sub("", remainder)
            remainder = _TARGET_AGENCY_ASK.sub("", remainder)
            remainder = _DURATION_SCOPE_EDGE.sub("", remainder)
            remainder = _QUESTION_SKELETON.sub("", remainder)
            remainder = _PROPERTY_MODAL.sub("", remainder)
            remainder = _DURATION_CLAUSE_NOISE.sub("", remainder)
            remainder = re.sub(r"[^\u4e00-\u9fffA-Za-z]", "", remainder)
            if len(remainder) <= 1:
                return True
        ask_terms = [prop.ask, *_PROPERTY_KEYWORDS.get(prop.kind, ())]
        echo_terms = (
            [*ask_terms, "至多", "至少", "不超过", "不少于"]
            if prop.kind == "bound"
            else ask_terms
        )
        if not any(term and len(term) >= 2 and term in clause for term in echo_terms):
            continue
        if prop.kind in {"target", "capability"}:
            remainder = clause
            for term in (*ask_terms, prop.core):
                if term:
                    remainder = remainder.replace(term, "")
            remainder = _PROPERTY_MODAL.sub("", remainder)
            remainder = re.sub(r"^(?:可以|能|能够|可|能否|是否|要|需|需要)+", "", remainder)
            remainder = _QUESTION_SKELETON.sub("", remainder)
            remainder = re.sub(r"[^\u4e00-\u9fffA-Za-z]", "", remainder)
            if len(remainder) <= 2:
                return True
        if prop.kind == "numeric" and prop.ask and prop.ask in clause:
            # 数量本身由 numeric property 单独核对，子句回显不再是额外缺口。
            return True
        if prop.kind == "bound":
            if (
                _LIST_OMISSION.search(clause)
                or _AMOUNT_ASK.search(clause)
                or _DATE_ASK.search(clause)
            ):
                continue
            remainder = clause
            for term in (*echo_terms, prop.ask, prop.measure):
                if term:
                    remainder = remainder.replace(term, "")
            remainder = _PROPERTY_MODAL.sub("", remainder)
            remainder = _QUESTION_SKELETON.sub("", remainder)
            remainder = _BOUND_ECHO_SELECTION.sub("", remainder)
            # “至多选” only repeats the bound ask plus the generic selection
            # verb. The bound itself is still checked as its own property.
            if not re.sub(r"[^\u4e00-\u9fffA-Za-z]", "", remainder):
                return True
        if prop.kind == "amount":
            # The fee clause is fully represented by the amount property, which
            # separately binds object, tier, per-copy unit and a value token.
            fee_clause = _canonical_fee_text(clause)
            remainder = fee_clause
            required_terms = tuple(
                _canonical_fee_text(term) for term in prop.required if term
            )
            for term in (*ask_terms, prop.core, *prop.business, *required_terms):
                if term:
                    remainder = remainder.replace(term, "")
            remainder = _AMOUNT_CORE_NOISE.sub("", remainder)
            remainder = _PROPERTY_MODAL.sub("", remainder)
            remainder = _QUESTION_SKELETON.sub("", remainder)
            remainder = re.sub(r"[^\u4e00-\u9fffA-Za-z]", "", remainder)
            if len(remainder) <= 2:
                return True
        if prop.core:
            if _clause_covered(prop.core, clause, min_ratio=0.5):
                return True
            chars = re.sub(r"[^\u4e00-\u9fffA-Za-z]", "", prop.core)
            if len(chars) > 4 and chars[-4:] in clause:
                return True
            continue
        if _DATE_ASK.search(clause) or _AMOUNT_ASK.search(clause):
            continue
        remainder = clause
        for term in ask_terms:
            if term:
                remainder = remainder.replace(term, "")
        remainder = _PROPERTY_MODAL.sub("", remainder)
        remainder = _QUESTION_SKELETON.sub("", remainder)
        remainder = re.sub(r"[^\u4e00-\u9fffA-Za-z]", "", remainder)
        if len(remainder) <= 2:
            return True
    return False


def _drop_property_echoes(
    clauses: list[str], properties: list[_Property] | tuple[_Property, ...]
) -> list[str]:
    if not clauses or not properties:
        return list(clauses)
    return [
        clause for clause in clauses if not _clause_is_property_echo(clause, properties)
    ]


def _squeezed(text: str) -> str:
    return re.sub(r"[\s　、，,。；;：:？?（）()]", "", text or "")


def _is_question_echo(item: str, question: str) -> bool:
    item_text = _squeezed(item)
    question_text = _squeezed(question)
    if not item_text:
        return True
    if item_text == question_text:
        return True
    return bool(
        question_text
        and item_text in question_text
        and len(item_text) * 5 >= len(question_text) * 4
    )


def _unanswered_is_redundant(
    item: str, proven: list[_Property], topics: list[str] | None = None
) -> bool:
    if _LIST_OMISSION.search(item):
        return False
    if topics and _context_echo(item, topics):
        return True
    if _clause_is_property_echo(item, proven):
        return True
    return any(
        prop.kind in ("payment", "degree", "submission", "amount", "target", "capability")
        and _contradicts_covered_property(item, [prop])
        for prop in proven
    )


def _applicable_properties(
    unit: QuestionUnit, properties: list[_Property]
) -> list[_Property]:
    aspects = [aspect for aspect in unit.requested_aspects if aspect]
    # A fallback unit stores the whole question as its only “aspect”; that
    # placeholder must not filter out every detected property.
    if not aspects or _is_fallback_unit(unit):
        return list(properties)
    return [
        prop
        for prop in properties
        if prop.kind in ("numeric", "study_duration")
        or any(_property_covers_aspect(prop, aspect) for aspect in aspects)
    ]


def _gated_content(
    unit: QuestionUnit,
    claim: Claim,
    class_terms: dict[str, tuple[str, ...]] | None,
    alias_groups: tuple[tuple[str, ...], ...],
    *,
    topics: list[str] | None = None,
    population: Population | None = None,
) -> str:
    content = _answer_text(claim.text)
    if not content:
        return ""
    if population is None:
        population = _effective_population(unit.original_span or "", topics)
    citation_text = " ".join(
        f"{citation.doc_title or ''} {citation.article_no or ''}"
        for citation in claim.citations
    )
    haystack = f"{claim.text} {citation_text}"
    if not _alias_present(unit.subject, haystack, alias_groups):
        return ""
    if unit.objects and not any(
        _alias_present(obj, haystack, alias_groups) for obj in unit.objects if obj
    ):
        return ""
    if not _claim_version_ok(unit, claim, content):
        return ""
    if not _class_scope_ok(unit, claim, class_terms, content):
        return ""
    return _scoped_content(
        unit, claim, content, class_terms, population=population
    )


def _property_gaps(
    properties: list[_Property],
    claims: list[Claim],
    unit: QuestionUnit,
    *,
    class_terms: dict[str, tuple[str, ...]] | None = None,
    alias_groups: tuple[tuple[str, ...], ...] = (),
    topics: list[str] | None = None,
) -> tuple[list[_Property], list[str]]:
    if not properties:
        return [], []
    population = _effective_population(unit.original_span or "", topics)
    eligible: list[tuple[Claim, str]] = []
    for claim in claims:
        scoped = _gated_content(
            unit,
            claim,
            class_terms,
            alias_groups,
            topics=topics,
            population=population,
        )
        if not scoped:
            continue
        if topics and not all(
            _topic_covered(
                topic,
                claim,
                alias_groups=alias_groups,
                population=population,
            )
            for topic in topics
        ):
            continue
        eligible.append((claim, scoped))
    uncovered: list[_Property] = []
    contributors: list[str] = []
    for prop in properties:
        hits = [
            claim.claim_id
            for claim, scoped in eligible
            if _property_supported(prop, scoped)
        ]
        if hits:
            contributors.extend(
                claim_id for claim_id in hits if claim_id not in contributors
            )
        else:
            uncovered.append(prop)
    return uncovered, contributors


def _clause_gaps(
    clauses: list[str],
    claims: list[Claim],
    topics: list[str] | None = None,
    *,
    alias_groups: tuple[tuple[str, ...], ...] = (),
) -> tuple[list[str], list[str]]:
    """返回 (未被任何结论覆盖的子问题, 参与覆盖的 claim_id)。

    有主题前缀时，只有能由同源标题解释该主题的结论才算候选；
    主题本身不是必须回答的方面，不进入缺口列表。
    """
    required_topics = topics or []
    population = _effective_population(" ".join(clauses), required_topics)
    eligible = [
        claim
        for claim in claims
        if all(
            _topic_covered(
                topic,
                claim,
                alias_groups=alias_groups,
                population=population,
            )
            for topic in required_topics
        )
    ]
    uncovered: list[str] = []
    contributors: list[str] = []
    for clause in clauses:
        hit = False
        for claim in eligible:
            content = _answer_text(claim.text)
            citation_text = " ".join(
                f"{citation.doc_title or ''} {citation.article_no or ''}"
                for citation in claim.citations
            )
            if population is not None and population_conflicts(
                population, content, citation_text
            ):
                continue
            scoped = filter_population_branches(population, content, citation_text)
            if scoped and _clause_content_ok(clause, scoped):
                hit = True
                if claim.claim_id not in contributors:
                    contributors.append(claim.claim_id)
        if not hit:
            uncovered.append(clause)
    return uncovered, contributors


def _is_fallback_unit(unit: QuestionUnit) -> bool:
    """decompose 没能识别出方面、把整句问题当成方面。"""
    aspects = [aspect for aspect in unit.requested_aspects if aspect]
    if not aspects:
        return False
    span = (unit.original_span or "").strip("？?。 ")
    return all(aspect.strip("？?。 ") == span for aspect in aspects)


def _matches_unit_subject(unit: QuestionUnit, claim: Claim) -> bool:
    haystack = f"{claim.text} " + " ".join(
        f"{citation.doc_title or ''} {citation.article_no or ''}" for citation in claim.citations
    )
    if unit.subject and unit.subject not in haystack:
        return False
    return not (unit.objects and not any(obj in haystack for obj in unit.objects if obj))


# A verified statement about missing evidence is still a gap. Scope this to the
# gap clause only: the same claim may also contain a complete answer to another
# aspect (2024 data plus “2022 未提供完整对比” must still answer 2024).
_GAP_CLAUSE = re.compile(
    r"(?:证据|材料|资料).{0,12}(?:不足|缺失)|缺少.{0,12}(?:证据|材料|资料)"
    r"|(?:未|没有|并未|未能)(?:提供|列出|给出|包含|说明|明确|告知|公布|公开)"
    r"[^。；，,]{0,80}(?:对比|细节|完整|全部|名单|清单|方式)"
    r"|(?:仅|只)(?:包含|列出|提供)[^。；，,]{0,40}(?:片段|部分)"
    r"|(?:没有|缺乏)[^。；，,]{0,20}(?:完整|具体)[^。；，,]{0,20}(?:信息|内容|说明)"
    r"|(?:无法|不能|未能)[^。；，,]{0,8}(?:回答|作答|对比|确认|确定|核实)"
    # Object-before-verb form: “公示方式未提供”“名单未公布”.
    r"|(?:方式|名单|清单|细节)[^。；，,]{0,8}"
    r"(?:未|没有|并未|未能)(?:提供|列出|给出|说明|明确|告知|公布|公开)"
)
_SENTENCE_BOUNDARY = re.compile(r"(?<=[。；;！？!?])")
_CLAUSE_BOUNDARY = re.compile(r"(?<=[，,])|(?=但(?:是)?)|(?=然而)|(?=不过)")


def _answer_text(text: str) -> str:
    """Drop only the clauses that declare a gap; keep answered clauses intact."""
    kept: list[str] = []
    for sentence in _SENTENCE_BOUNDARY.split(text):
        if not sentence.strip():
            continue
        if not _GAP_CLAUSE.search(sentence):
            kept.append(sentence)
            continue
        kept.extend(
            clause
            for clause in _CLAUSE_BOUNDARY.split(sentence)
            if clause.strip() and not _GAP_CLAUSE.search(clause)
        )
    return "".join(kept).strip()


def _groups_for(
    text: str, alias_groups: tuple[tuple[str, ...], ...]
) -> tuple[tuple[str, ...], ...]:
    return tuple(
        group for group in alias_groups if any(term and term in text for term in group)
    )


def _canonical_aliases(
    text: str, alias_groups: tuple[tuple[str, ...], ...]
) -> str:
    for group in alias_groups:
        terms = sorted({term for term in group if term}, key=len, reverse=True)
        if not terms:
            continue
        pattern = re.compile("|".join(re.escape(term) for term in terms))
        text = pattern.sub(terms[0], text)
    return text


def _alias_present(
    term: str, text: str, alias_groups: tuple[tuple[str, ...], ...]
) -> bool:
    if not term or term in text:
        return True
    return any(
        alias in text
        for group in _groups_for(term, alias_groups)
        for alias in group
        if alias
    )


# 决策异议的三种说法共享同一诉求：不服X / 对X不服 / 对X有异议。
# 只在主题范围核对时归一，被质疑的决定对象原样保留，不降低全局阈值。
_DECISION_OBJECT = r"[^，,。；;：:？?！!、\s（）()【】\[\]“”\"「」『』《》]{2,20}"
_DECISION_SPAN = re.compile(
    rf"对(?P<target>{_DECISION_OBJECT})(?:的)?不服"
    rf"|不服(?P<after>{_DECISION_OBJECT})"
    rf"|对(?P<existing>{_DECISION_OBJECT})(?:的)?有异议"
)
_DECISION_OBJECT_NOISE = re.compile(
    r"(?:(?:的)?(?:决定书|决定|处理|处分|结果|通知))+$"
)


def _dissent_object(match: re.Match[str]) -> str:
    obj = (
        match.group("target")
        or match.group("after")
        or match.group("existing")
        or ""
    )
    return obj.strip(_STEM_STRIP).removesuffix("的").strip(_STEM_STRIP)


def _canonical_dissent_terms(text: str) -> str:
    """把“不服X/对X不服”改写成“对X有异议”，X 原样保留。"""

    def replace(match: re.Match[str]) -> str:
        obj = _dissent_object(match)
        if len(obj) < 2:
            return match.group(0)
        return f"对{obj}有异议"

    return _DECISION_SPAN.sub(replace, text)


def _dissent_scope(topic: str) -> tuple[str, tuple[str, ...]]:
    """去掉异议谓语，返回剩余范围与需要单独核对的决定对象。"""
    objects: list[str] = []

    def replace(match: re.Match[str]) -> str:
        obj = _dissent_object(match)
        if len(obj) < 2:
            return match.group(0)
        objects.append(obj)
        return " "

    residual = _DECISION_SPAN.sub(replace, topic)
    return residual, tuple(dict.fromkeys(objects))


def _decision_object_ok(obj: str, text: str) -> bool:
    core = _DECISION_OBJECT_NOISE.sub("", obj).strip(_STEM_STRIP)
    if len(re.sub(r"[^\u4e00-\u9fffA-Za-z]", "", core)) < 2:
        core = obj.strip(_STEM_STRIP)
    if not core:
        return True
    return _clause_covered(core, text, min_ratio=0.75)


_CONDITION_LEAD = re.compile(
    r"^(?:实行|来自|属于|具有|作为|凡是|所有|全部|已经|已|是|为)+"
)
_CONDITION_TAIL = re.compile(r"(?:地区|地方|人员|同学)+$")
_CONDITION_SPLIT = re.compile(r"且|并且|而且|同时|以及|、")
_CONDITION_GENERIC = re.compile(
    r"^(?:相关|有关|上述|该|其|这些|那些|普通|一般|通常|所有|全部|各类)+$"
)
_CONDITION_NEGATION_BEFORE = re.compile(
    r"(?:不|未|非|无|没有|未曾|并未|不予|不得|不是|并非)"
    r"(?:实行|属于|具有|来自|是|为|有|在|修|达到|满足|取得|完成|通过|"
    r"符合|采用|使用|执行|实施|要求|需要|需|能|能够)?"
    r"(?:完全|普通|标准|所有|任何|学校|我校|本校)?$"
)
_CONDITION_POLARITY_CHARS = frozenset("不非未无没")


def _condition_chars(text: str) -> str:
    return re.sub(r"[^\u4e00-\u9fffA-Za-z]", "", text or "")


def _condition_exact_positive(segment: str, body: str) -> bool:
    start = body.find(segment)
    while start >= 0:
        before = body[max(0, start - 8) : start]
        if not _CONDITION_NEGATION_BEFORE.search(before):
            return True
        start = body.find(segment, start + 1)
    return False


def _condition_externally_negated(chars: str, body: str) -> bool:
    key = chars[:2]
    for match in re.finditer(re.escape(key), body):
        before = body[max(0, match.start() - 10) : match.start()]
        if _CONDITION_NEGATION_BEFORE.search(before):
            return True
    return False


def _condition_ordered_present(chars: str, body: str) -> bool:
    body_chars = _condition_chars(body)
    limit = max(12, len(chars) * 3)
    asked_polarity = set(chars) & _CONDITION_POLARITY_CHARS
    start = body_chars.find(chars[0])
    while start >= 0:
        cursor = start
        for char in chars[1:]:
            cursor = body_chars.find(char, cursor + 1)
            if cursor < 0:
                break
        else:
            span = body_chars[start : cursor + 1]
            inserted = (set(span) & _CONDITION_POLARITY_CHARS) - asked_polarity
            if cursor - start + 1 <= limit and not inserted:
                return True
        start = body_chars.find(chars[0], start + 1)
    return False


def _background_condition_residual(
    topic: str, groups: tuple[tuple[str, ...], ...]
) -> str:
    residual = _BACKGROUND_POPULATION.sub("", topic)
    residual = _canonical_aliases(residual, groups)
    residual = _canonical_property_text(residual)
    residual = _CONDITION_LEAD.sub("", residual)
    residual = _CONDITION_TAIL.sub("", residual)
    return residual.strip(_STEM_STRIP)


def _background_condition_covered(
    topic: str, body: str, groups: tuple[tuple[str, ...], ...]
) -> bool:
    """A demoted condition is scope, but its predicates still need body support."""
    residual = _background_condition_residual(topic, groups)
    if not residual or _CONDITION_GENERIC.fullmatch(residual):
        return True
    segments = [
        part.strip(_STEM_STRIP)
        for part in _CONDITION_SPLIT.split(residual)
        if part.strip(_STEM_STRIP)
    ]
    if not segments:
        return True
    body = _canonical_property_text(_canonical_aliases(body, groups))
    for segment in segments:
        segment = _canonical_property_text(segment)
        chars = _condition_chars(segment)
        if len(chars) < 2:
            continue
        if _condition_exact_positive(segment, body):
            continue
        if len(chars) <= 3:
            return False
        if _condition_externally_negated(chars, body):
            return False
        if not _condition_ordered_present(chars, body):
            return False
    return True


def _topic_covered(
    topic: str,
    claim: Claim,
    *,
    alias_groups: tuple[tuple[str, ...], ...] = (),
    population: Population | None = None,
) -> bool:
    """A comma-prefix topic is scope, not an answer.

    Its year and institution must be explained by the claim body or the cited
    same-source title. Titles may explain scope; they never supply the actual
    dates or amounts, which are checked on the claim body per clause.
    """
    body = _answer_text(claim.text)
    citation_text = " ".join(
        f"{citation.doc_title or ''} {citation.article_no or ''}" for citation in claim.citations
    )
    topic_years = {int(year) for year in _RAW_YEAR.findall(topic)}
    if topic_years:
        body_years = {int(year) for year in _RAW_YEAR.findall(body)}
        if body_years:
            if not topic_years <= body_years:
                return False
        else:
            cited_years = {int(year) for year in _RAW_YEAR.findall(citation_text)}
            if not topic_years & cited_years:
                return False
    groups = _groups_for(topic, alias_groups)
    topic_text = _canonical_aliases(topic, groups)
    # “某项”这类问句填充词不属于范围内容；范围本身仍要同源标题解释。
    topic_text = _QUESTION_SKELETON.sub("", topic_text).strip(_STEM_STRIP) or topic_text
    combined = _canonical_aliases(f"{body} {citation_text}", groups)
    residual, decision_objects = _dissent_scope(topic_text)
    if decision_objects:
        combined = _canonical_dissent_terms(combined)
        # 谓语可以同义改写，被质疑的决定对象不能被另一对象顶替。
        if not all(_decision_object_ok(obj, combined) for obj in decision_objects):
            return False
        residual = residual.strip(_STEM_STRIP)
        if residual and not _clause_covered(residual, combined, min_ratio=0.75):
            return False
        topic_text = _canonical_dissent_terms(topic_text)
    for institution in _INSTITUTION.findall(topic_text):
        if institution not in combined:
            return False
    if _is_scope_topic(topic):
        required = population if population is not None else scan_population(topic)
        if required.has_critical and population_conflicts(
            required, body, citation_text
        ):
            return False
        if _is_background_condition(topic) and not _background_condition_covered(
            topic, body, groups
        ):
            return False
        # Critical identity was bound above; the remaining anchors may still
        # use the ratio, but generic student frames need no literal echo.
        anchors = tuple(
            anchor
            for anchor in _context_anchors(topic)
            if anchor not in _POPULATION_CONTEXT_ANCHORS
        )
        if not anchors:
            return True
        return sum(1 for anchor in anchors if anchor in combined) * 2 >= len(anchors)
    return _clause_covered(topic_text, combined)


_YEAR_SCOPE = re.compile(
    r"((?:20\d{2}\s*(?:[、,，和与及—–\-至]\s*)?)+)\s*(?:版|级)"
)
_RAW_YEAR = re.compile(r"20\d{2}")
_SHARED_YEAR_SEP = re.compile(r"[—–\-至]")


def _years_in(text: str) -> set[int]:
    years: set[int] = set()
    for match in _YEAR_SCOPE.finditer(text or ""):
        years.update(int(year) for year in _RAW_YEAR.findall(match.group(1)))
    return years


def _single_scope_years(text: str) -> set[int] | None:
    """Return the one effective version scope in a body, if unambiguous.

    An explicit dash/至 range such as “2031—2032级” is one atomic scope, so
    unmarked answer clauses after the scope inherit it. A 、/， list or
    separate declarations denote distinct scopes; mixing them keeps unmarked
    clauses ambiguous instead of unioning years into support.
    """
    scopes: list[frozenset[int]] = []
    for match in _YEAR_SCOPE.finditer(text or ""):
        years = frozenset(int(year) for year in _RAW_YEAR.findall(match.group(1)))
        if len(years) == 1 or (len(years) == 2 and _SHARED_YEAR_SEP.search(match.group(1))):
            scopes.append(years)
        else:
            return None
    if not scopes or len(set(scopes)) != 1:
        return None
    return set(scopes[0])


def _unit_version_years(unit: QuestionUnit) -> set[int]:
    """Plan version only; cohort is a separate scope and never a substitute."""
    if not unit.version:
        return set()
    years = _years_in(unit.version)
    if not years:
        years = {int(year) for year in _RAW_YEAR.findall(unit.version)}
    return years


def _claim_version_ok(unit: QuestionUnit, claim: Claim, content: str) -> bool:
    """Bind a version to the answered content, not to the citation union.

    A 2024 fact plus an extra 2022 citation is still a 2024 fact. Citation
    metadata is only a fallback when the answered text itself has no year and
    all cited metadata points at the asked version; a mixed citation set cannot
    be attributed to one unit. Year names inside a declared gap clause are not
    support because ``content`` already had gap clauses removed.
    """
    asked = _unit_version_years(unit)
    if not asked:
        return True
    content_years = _years_in(content)
    if content_years:
        return bool(asked & content_years)
    cited: set[int] = set()
    for citation in claim.citations:
        cited |= _years_in(f"{citation.doc_title or ''} {citation.article_no or ''}")
    if not cited:
        return True
    return cited <= asked


def _class_scope_ok(
    unit: QuestionUnit,
    claim: Claim,
    class_terms: dict[str, tuple[str, ...]] | None,
    content: str,
) -> bool:
    """A 创新班 fact cannot answer a 普通班 unit (and vice versa).

    Bind the class to the answered clauses first. Citation titles are only a
    fallback when the effective answer body carries no class marker; a gap
    clause such as “普通班未提供对比” must not leak the other class.
    """
    if not unit.class_type or not class_terms:
        return True
    own = set(class_terms.get(unit.class_type, ()))
    if not own:
        return True
    other = {
        term
        for class_id, terms in class_terms.items()
        if class_id != unit.class_type
        for term in terms
    }
    if any(term in content for term in own):
        return True
    if any(term in content for term in other):
        return False
    citation_text = " ".join(
        f"{citation.doc_title or ''} {citation.article_no or ''}" for citation in claim.citations
    )
    title_own = any(term in citation_text for term in own)
    title_other = any(term in citation_text for term in other)
    if title_own and not title_other:
        return True
    if title_other:
        return False
    # Untitled/base plan files are acceptable for a general class; a specific
    # class needs its registered marker in the answered body or source title.
    return bool(re.search(r"普通", " ".join(own)))


_SCOPE_CLAUSE = re.compile(r"(?<=[。；;！？!?，,])")


def _scoped_content(
    unit: QuestionUnit,
    claim: Claim,
    content: str,
    class_terms: dict[str, tuple[str, ...]] | None,
    *,
    population: Population | None = None,
) -> str:
    """Keep only clauses that can belong to this unit's version and class.

    A mixed claim can answer 2024's goal and 2022's credits in separate
    clauses; the aspect mentioned in one scope must not cover the other.
    Unmarked clauses count only when the effective body has a single scope;
    one explicit dash/至 range is a single scope, not two cohorts.
    Population branches follow the same rule: a claim about both enrolled and
    graduated students must not lend the graduate alternative to an enrolled
    unit.
    """
    citation_text = " ".join(
        f"{citation.doc_title or ''} {citation.article_no or ''}" for citation in claim.citations
    )
    if population is None:
        population = _effective_population(unit.original_span or "")
    if population is not None and population_conflicts(
        population, content, citation_text
    ):
        return ""
    scoped_content = filter_population_branches(population, content, citation_text)
    if not scoped_content:
        return ""
    clauses = [part for part in _SCOPE_CLAUSE.split(scoped_content) if part.strip()]
    if not clauses:
        return scoped_content
    asked_versions = _unit_version_years(unit)
    content_years = _years_in(content)
    content_scope = _single_scope_years(content)
    own: set[str] = set()
    other: set[str] = set()
    title_own = title_other = False
    if unit.class_type and class_terms:
        own = set(class_terms.get(unit.class_type, ()))
        other = {
            term
            for class_id, terms in class_terms.items()
            if class_id != unit.class_type
            for term in terms
        }
        title_own = any(term in citation_text for term in own)
        title_other = any(term in citation_text for term in other)
    content_own = any(term in content for term in own) if own else False
    content_other = any(term in content for term in other) if other else False
    if not asked_versions and not own:
        return scoped_content

    def version_ok(clause: str) -> bool:
        if not asked_versions:
            return True
        years = _years_in(clause)
        if years:
            return bool(asked_versions & years)
        if not content_years:
            # No year in the body: the citation fallback is checked globally.
            return True
        return bool(content_scope is not None and content_scope & asked_versions)

    def class_ok(clause: str) -> bool:
        if not own:
            return True
        if any(term in clause for term in own):
            return True
        if any(term in clause for term in other):
            return False
        if content_own and content_other:
            # Mixed classes in one body: an unmarked clause is ambiguous.
            return False
        if content_own:
            return True
        if content_other:
            return False
        if title_own and not title_other:
            return True
        if title_other:
            return False
        return "普通" in " ".join(own)

    return "".join(clause for clause in clauses if version_ok(clause) and class_ok(clause))


_MEMBER_ASK = re.compile(
    r"谁|哪些人|名单|人选|姓名|哪几位|成员(?:有哪|是谁|包括)"
    r"|人员(?:有哪|名单)"
)
_MEMBER_TAIL_ASK = re.compile(r"有哪(?:些|几)(?:位|人)?[？?。\s]*$")
_ROLE_TOKEN = re.compile(
    r"[\u4e00-\u9fff]{2,8}?"
    r"(?:委员(?!会)|负责人|主任|院长|书记|专家|成员|人员|联络员|秘书|组长|队长|主席)"
)
_ROLE_TAIL = re.compile(
    r"(?:委员(?!会)|负责人|主任|院长|书记|专家|成员|人员|联络员|秘书|组长|队长|主席)$"
)
_ROLE_ASPECT = re.compile(r"负责人|成员|名单|人选|人员|专家|委员|谁")


def _member_question(span: str) -> bool:
    return bool(_MEMBER_ASK.search(span) or _MEMBER_TAIL_ASK.search(span))


def _requested_role(unit: QuestionUnit) -> str:
    span = _without_instructions(unit.original_span or "")
    matches = _ROLE_TOKEN.findall(span)
    if not matches:
        return ""
    # “联络委员和宣传委员是谁”两个职务都要满足，不能只取最后一个；
    # 先去掉后续职务前的并列连词，再按顺序合成一个需要逐项核对的复合角色。
    roles = [
        re.sub(r"^(?:以及|或者|和|与|及|、)", "", match).strip(" 　的")
        for match in matches
    ]
    role = "和".join(dict.fromkeys(role for role in roles if role))
    role = re.sub(r"^.*?(?:学院|大学|学校|学部|系|部|处|中心)", "", role)
    role = re.sub(r"^.*(?:中|的)", "", role)
    role = re.sub(r"^(?:委员会|小组|办公室|学生会)", "", role)
    # 党组织前缀只在后面仍是完整职务词时剥离，避免“党委委员”降成泛称“委员”。
    stripped = re.sub(r"^(?:党委|党总支|党支部)", "", role)
    if stripped != role and _ROLE_TOKEN.fullmatch(stripped):
        role = stripped
    role = re.sub(r"^(?:查询|请问|关于|根据|按照|详见)", "", role)
    # “谁是负责人”里的问法前缀不属于职务名。
    role = role.rsplit("是", 1)[-1]
    return role.strip(" 　的")


def _member_role(unit: QuestionUnit) -> str | None:
    if not _member_question(unit.original_span or ""):
        return None
    role = _requested_role(unit)
    if role:
        return role
    if any(_ROLE_ASPECT.search(aspect) for aspect in unit.requested_aspects if aspect):
        return ""
    return None


def _role_matches(role: str, content: str) -> bool:
    if not role:
        return True
    # 复合职务逐个核对：只答其中一个职务不能算回答并列问题。
    parts = [part for part in re.split(r"[、和与及]", role) if part]
    if len(parts) > 1:
        return all(_role_matches(part, content) for part in parts)
    if role in content:
        return True
    core = _ROLE_TAIL.sub("", role).strip(" 　的")
    return bool(core) and _clause_covered(core, content, min_ratio=0.75)


def _member_support(
    unit: QuestionUnit,
    content: str,
    required_items: list[str] | None,
) -> bool | None:
    """Reliable member rows become required names for a person-list question."""
    if not required_items:
        return None
    role = _member_role(unit)
    if role is None:
        return None
    if not _role_matches(role, content):
        return False
    normalized = normalize_text(content)
    return all(
        normalize_text(name) in normalized for name in required_items if name
    )


_CERT_NOUN = r"(?:证书|证(?!明|件|券|据|词|人|物|言))"
_CERT_NOUN_RE = re.compile(_CERT_NOUN)
_CERTIFICATE_TYPE_ASK = re.compile(
    rf"(?:哪(?:一)?种|哪种|哪类|什么|何种)(?:类型|种类|类别)?(?:的)?{_CERT_NOUN}"
    rf"|{_CERT_NOUN}(?:是|为|有)?\s*(?:什么|哪种|哪类|何种)"
    rf"|(?:颁发|发放|授予|取得|获得|发|拿|领)\s*(?:什么|哪些|哪种|哪类|何种)\s*{_CERT_NOUN}"
)
_CERTIFICATE_YESNO_ASK = re.compile(
    rf"(?:是否|会不会|能不能|可不可以|有无|有没有|是否有|发不发|颁不颁|给不给)"
    rf"[^。；，,？?]{{0,8}}{_CERT_NOUN}"
    rf"|{_CERT_NOUN}[^。；，,？?]{{0,8}}(?:吗|嘛|呢|否)"
)
_CERTIFICATE_GENERIC_EXACT = frozenset(
    {
        "发",
        "证",
        "不",
        "未",
        "没",
        "会",
        "将",
        "可",
        "能",
        "要",
        "需",
        "是",
        "为",
        "有",
        "的",
        "该",
        "此",
        "本",
        "其",
    }
)
_CERTIFICATE_GENERIC_PARTS = (
    "发放",
    "颁发",
    "授予",
    "补发",
    "换发",
    "寄发",
    "领取",
    "拿到",
    "获得",
    "取得",
    "出具",
    "开具",
    "提供",
    "提交",
    "持有",
    "具有",
    "申请",
    "办理",
    "相关",
    "有关",
    "相应",
    "一种",
    "某种",
    "任何",
    "学校",
    "我校",
    "本校",
    "统一",
    "制作",
    "统一制作",
    "学校统一制作",
    "什么",
    "哪种",
    "哪类",
    "何种",
    "哪些",
    "上述",
    "这种",
    "那种",
    "此类",
    "各类",
    "一类",
    "可以",
    "能够",
    "会发",
    "不会",
    "不发",
    "未发",
    "没有",
    "无需",
    "无须",
)
_CERTIFICATE_NON_AWARD = re.compile(
    r"身份证|学生证|工作证|通行证|出入证|准考证|报名证|驾驶证|行驶证|房产证"
)
_CERTIFICATE_MODIFIER_SUFFIX = re.compile(
    r"(?:学校统一制作|学校统一|统一制作|统一|制作|相应|相关|有关"
    r"|一种|某种|任何|上述|这种|那种|此类|各类|一类|学校|我校|本校"
    r"|该|此|本|其|的)+$"
)
_CERTIFICATE_MODIFIER_PREFIX = re.compile(
    r"^(?:学校统一制作|学校统一|统一制作|统一|制作|相应|相关|有关"
    r"|一种|某种|任何|上述|这种|那种|此类|各类|一类|学校|我校|本校|的)+"
)
_CERTIFICATE_PENDING = re.compile(
    r"尚未|还未|仍未|暂未|未(?:曾)?(?:颁发|发放|授予|出具|寄发|补发|换发|制发)"
    r"|待(?:颁发|发放|授予|出具)|拟(?:颁发|发放|授予)|即将(?:颁发|发放|授予)"
)
_CERTIFICATE_VALUE_UNCERTAIN = re.compile(
    rf"(?:{_UNCERTAIN.pattern})|未知|待确认|未公布|待定|暂无|没有明确"
    rf"|{_CERTIFICATE_PENDING.pattern}"
)
_CERTIFICATE_QUOTED = re.compile(r"《([^》]{1,80})》")
_CERTIFICATE_AWARD_BEFORE = re.compile(
    r"(?:颁发|发放|授予|取得|获得|补发|换发|寄发|出具|发|拿|领)"
    r"[^。；，,？?]{0,8}$"
)
_CERTIFICATE_ISSUANCE = re.compile(
    rf"(?:不|未|没|没有|无|不会|不予|不再|无需|无须|可以|可|能|能够|会|将|已经|已)?"
    rf"[^。；，,？?]{{0,6}}"
    rf"(?:颁发|发放|发|授予|出具|补发|换发|寄发)"
    rf"[^。；，,？?]{{0,8}}"
    rf"{_CERT_NOUN}"
)
_CERTIFICATE_WITHHELD = re.compile(
    rf"{_CERT_NOUN}[^。；，,？?]{{0,8}}"
    rf"(?:不|未|没有|尚未|不予|不再)"
    rf"(?:颁发|发放|授予|出具|寄发|补发|换发)"
)
_CERTIFICATE_NONE = re.compile(
    rf"(?:无|没有|未颁发|未发放|不颁发|不发放|不发|不予颁发|不予发放)"
    rf"[^。；，,？?]{{0,3}}{_CERT_NOUN}"
)
_CERTIFICATE_DEFINITE_NONE = re.compile(
    rf"(?:不|不予|不会|不再|不予以|无需|无须)"
    rf"[^。；，,？?]{{0,4}}(?:颁发|发放|授予|出具|寄发|补发|换发|制发|发)"
    rf"[^。；，,？?]{{0,4}}{_CERT_NOUN}"
    rf"|{_CERT_NOUN}[^。；，,？?]{{0,8}}(?:不|不予|不会|不再)"
    rf"(?:颁发|发放|授予|出具|寄发|补发|换发|制发)"
    rf"|(?:无|没有)[^。；，,？?]{{0,3}}{_CERT_NOUN}"
)
_VALUE_QUESTION_ECHO = re.compile(
    r"是否|会不会|能不能|可不可以|能否|可否|有无|有没有|吗|嘛|呢|[？?]"
)


def _certificate_stem(name: str) -> str:
    stem = re.sub(r"[（(][^）)]*[）)]", "", name or "")
    stem = stem.strip(" 　。；;，,、/／-—－·")
    stem = _CERTIFICATE_MODIFIER_PREFIX.sub("", stem).strip(" 　的")
    stem = _CERTIFICATE_MODIFIER_SUFFIX.sub("", stem).strip(" 　的")
    if len(stem) < 2 or stem in _CERTIFICATE_GENERIC_EXACT:
        return ""
    if any(part in stem for part in _CERTIFICATE_GENERIC_PARTS):
        return ""
    return stem


def _certificate_quoted_value(clause: str) -> str:
    for match in _CERTIFICATE_QUOTED.finditer(clause):
        inner = re.sub(r"\s+", "", match.group(1))
        inner = re.sub(r"[（(][^）)]*[）)]", "", inner)
        nouns = list(_CERT_NOUN_RE.finditer(inner))
        if not nouns or nouns[-1].end() != len(inner):
            continue
        # A governance title such as 《结业证书管理办法》 is not an award.
        if not _CERTIFICATE_AWARD_BEFORE.search(clause[: match.start()]):
            continue
        noun = nouns[-1].group(0)
        stem = _certificate_stem(inner[: nouns[-1].start()])
        if not stem:
            continue
        candidate = f"{stem}{noun}"
        if _CERTIFICATE_NON_AWARD.search(candidate):
            continue
        return candidate
    return ""


def _certificate_unquoted_value(clause: str) -> str:
    masked = re.sub(r"《[^》]{0,80}》", " ", clause)
    for match in _CERT_NOUN_RE.finditer(masked):
        noun = match.group(0)
        prefix = re.sub(r"[^\u4e00-\u9fff]", "", masked[: match.start()])
        if prefix and prefix[-1] in "发颁授予领取获得拿给补换寄":
            # “发证/发证书” is an issuance statement, not a named type.
            continue
        stem_prefix = _CERTIFICATE_MODIFIER_SUFFIX.sub("", prefix).strip(" 　的")
        for length in range(min(len(stem_prefix), 8), 1, -1):
            stem = stem_prefix[-length:]
            if stem in _CERTIFICATE_GENERIC_EXACT:
                continue
            if any(part in stem for part in _CERTIFICATE_GENERIC_PARTS):
                continue
            candidate = f"{stem}{noun}"
            if _CERTIFICATE_NON_AWARD.search(candidate):
                continue
            return candidate
    return ""


def _certificate_value(text: str) -> str:
    for clause in _CLAIM_SEGMENT.split(_without_uncertain(text or "")):
        clause = clause.strip()
        if (
            not clause
            or _QUESTION_MARKER.search(clause)
            or _VALUE_QUESTION_ECHO.search(clause)
            or _CERTIFICATE_VALUE_UNCERTAIN.search(clause)
        ):
            continue
        quoted = _certificate_quoted_value(clause)
        if quoted:
            return quoted
        unquoted = _certificate_unquoted_value(clause)
        if unquoted:
            return unquoted
    return ""


def _certificate_definite_none(text: str) -> bool:
    for clause in _CLAIM_SEGMENT.split(_without_uncertain(text or "")):
        clause = re.sub(r"《[^》]{0,80}》", " ", clause).strip()
        if (
            not clause
            or _QUESTION_MARKER.search(clause)
            or _VALUE_QUESTION_ECHO.search(clause)
            or _CERTIFICATE_VALUE_UNCERTAIN.search(clause)
        ):
            continue
        if _CERTIFICATE_DEFINITE_NONE.search(clause):
            return True
    return False


def _certificate_issuance(text: str) -> bool:
    for clause in _CLAIM_SEGMENT.split(_without_uncertain(text or "")):
        clause = re.sub(r"《[^》]{0,80}》", " ", clause).strip()
        if (
            not clause
            or _QUESTION_MARKER.search(clause)
            or _VALUE_QUESTION_ECHO.search(clause)
            or _CERTIFICATE_VALUE_UNCERTAIN.search(clause)
        ):
            continue
        if (
            _CERTIFICATE_ISSUANCE.search(clause)
            or _CERTIFICATE_WITHHELD.search(clause)
            or _CERTIFICATE_NONE.search(clause)
        ):
            return True
    return False


def _certificate_aspect_supported(text: str, span: str) -> bool:
    if _CERTIFICATE_TYPE_ASK.search(span or ""):
        # A definite no-certificate statement answers “what type” as none,
        # while pending/unknown absence does not.
        return bool(_certificate_value(text)) or _certificate_definite_none(text)
    if _CERTIFICATE_YESNO_ASK.search(span or ""):
        return bool(_certificate_value(text)) or _certificate_issuance(text)
    return bool(_CERT_NOUN_RE.search(text or "")) or _certificate_issuance(text)


_REGISTRY_PLATFORM = (
    r"(?:中国高等教育学生信息网(?:\s*[（(]\s*学信网\s*[）)])?"
    r"|学信网|学信档案|CHSI)"
)
_REGISTRY_ACTION = r"(?:备注|标注|备案|登记|注册|显示|体现|注明|记录)"
_REGISTRY_UNCERTAIN = re.compile(
    rf"(?:{_UNCERTAIN.pattern})|未公布|待确认|待定|暂无|未知|没有明确|未载明|未列明"
)
_REGISTRY_ASSERTION = re.compile(
    rf"(?:(?:已|已经|将|将要|会|可以|可|能|能够|不|未|没|没有|不会|不予|无需|无须|无法)"
    rf"[^。；，,？?]{{0,4}}(?:在|于)?\s*{_REGISTRY_PLATFORM}(?:上|中)?"
    rf"[^。；，,？?]{{0,8}}{_REGISTRY_ACTION})"
    rf"|(?:[^。；，,？?]{{1,12}}(?:在|于)\s*{_REGISTRY_PLATFORM}(?:上|中)?"
    rf"[^。；，,？?]{{0,8}}{_REGISTRY_ACTION})"
    rf"|(?:{_REGISTRY_PLATFORM}(?:上|中)?"
    rf"[^。；，,？?]{{0,6}}(?:不|未|不会|不予|将|会|已|已经|可以|可|能|能够|予以|进行)"
    rf"[^。；，,？?]{{0,6}}{_REGISTRY_ACTION})"
)


def _registry_aspect_supported(text: str) -> bool:
    body = _without_uncertain(text or "")
    for clause in _CLAIM_SEGMENT.split(body):
        clause = re.sub(r"《[^》]{0,80}》", " ", clause).strip()
        if (
            not clause
            or _QUESTION_MARKER.search(clause)
            or _VALUE_QUESTION_ECHO.search(clause)
        ):
            continue
        if _REGISTRY_UNCERTAIN.search(clause):
            continue
        if _REGISTRY_ASSERTION.search(clause):
            return True
    return False


def _aspect_supported(
    aspect: str,
    scoped: str,
    content: str,
    citation_text: str,
    member_state: bool | None,
    *,
    original_span: str = "",
) -> bool:
    if aspect == "证书":
        return _certificate_aspect_supported(scoped, original_span)
    if aspect == "学信网备案":
        return _registry_aspect_supported(scoped)
    if aspect == "办理程序":
        if _procedure_answer_supported(scoped, original_span):
            return True
        pattern = _ASPECT_PATTERNS.get(aspect)
        mentioned = bool(pattern and re.search(pattern, scoped)) or aspect in scoped
        # Existing ask-word wording (“如何申请/办理程序”) still counts, but only
        # when the steps belong to the asked event; a cited heading adds no steps.
        return bool(
            (mentioned or aspect in citation_text)
            and _procedure_topic_ok(f"{scoped} {citation_text}", original_span)
            and _content_supports("办理程序", scoped)
        )
    if aspect == "提交材料":
        # Only the answer text supplies material names/properties; never borrow
        # unrendered source text from the citation. A general submission ask may
        # also be answered by a bound actor/action+recipient+date route.
        return _material_answer_supported(scoped, original_span)
    role_aspect = bool(_ROLE_ASPECT.search(aspect))
    if role_aspect and member_state is not None:
        # 有可靠名单时，“负责人很多”这类泛称不算回答具体职务。
        return bool(member_state)
    pattern = _ASPECT_PATTERNS.get(aspect)
    mentioned = bool(pattern and re.search(pattern, scoped)) or aspect in scoped
    if not mentioned and aspect == "定义区别":
        mentioned = "是指" in scoped or "指的是" in scoped
    if mentioned and _content_supports(aspect, scoped):
        return True
    # 标题只解释对象，不提供清单内容；“标题+短正文”不能凑成已答。
    # An aspect may be carried by the cited heading only when the scoped clause
    # still provides concrete content for it, e.g. a section titled “课程设置”
    # must still list courses. A mixed-scope heading cannot lend 培养目标 to a
    # clause that only states another year's credits.
    filtered = scoped != content
    return bool(
        aspect in citation_text
        and (not filtered or aspect in _CONTENT_CHECKED_ASPECTS)
        and _content_supports(aspect, scoped)
        and not (member_state is False and _ROLE_ASPECT.search(aspect))
    )


def _claim_supports_unit(
    unit: QuestionUnit,
    claim: Claim | None,
    *,
    class_terms: dict[str, tuple[str, ...]] | None = None,
    required_items: list[str] | None = None,
    alias_groups: tuple[tuple[str, ...], ...] = (),
    require_all_fallback: bool = True,
    fallback_clauses: list[str] | None = None,
    properties: list[_Property] | tuple[_Property, ...] = (),
    require_all_properties: bool = True,
    protected: tuple[str, ...] = (),
) -> bool:
    """R4/A5：一条结论必须绑定到该单元的对象、版本、班型和所问方面。

    比较题每个对象各占一个单元，因此“两个专业都有实践环节”这类泛称断言
    无法同时支撑两个单元；清单题要有课程名，数字题要有数值，流程题要有步骤。
    多结论集体绑定时 require_all_fallback=False：一条只需覆盖至少一个必要
    子方面，整体是否覆盖完整由调用方对 contributors 求并集后核对。
    """
    if claim is None:
        return False
    content = _answer_text(claim.text)
    if not content:
        return False
    population = _effective_population(unit.original_span or "")
    citation_text = " ".join(
        f"{citation.doc_title or ''} {citation.article_no or ''}" for citation in claim.citations
    )
    haystack = f"{claim.text} {citation_text}"
    if not _alias_present(unit.subject, haystack, alias_groups):
        return False
    if unit.objects and not any(
        _alias_present(obj, haystack, alias_groups) for obj in unit.objects if obj
    ):
        return False
    if not _claim_version_ok(unit, claim, content):
        return False
    if not _class_scope_ok(unit, claim, class_terms, content):
        return False
    scoped = _scoped_content(
        unit, claim, content, class_terms, population=population
    )
    if not scoped:
        return False
    aspects = [aspect for aspect in unit.requested_aspects if aspect]
    member_state = _member_support(unit, scoped, required_items)
    supported_props = [
        prop for prop in properties if _property_supported(prop, scoped)
    ]
    if not aspects:
        if not properties:
            return True
        if require_all_properties:
            return len(supported_props) == len(properties)
        return bool(supported_props)
    # 没有可识别方面时，decompose 会把整句问题当成方面；此时对象已匹配，
    # 一条通过核对的结论就覆盖了该问题，不再要求它重复问题原句。
    span = (unit.original_span or "").strip("？?。 ")
    if all(aspect.strip("？?。 ") == span for aspect in aspects):
        # 035：默认一条结论要覆盖全部子问题；多结论并集绑定由调用方核对。
        if require_all_fallback and member_state is not None:
            return member_state
        clauses, topics = _question_clauses(span, protected)
        fallback_population = _effective_population(
            unit.original_span or span, topics
        )
        if fallback_population != population:
            scoped = _scoped_content(
                unit,
                claim,
                content,
                class_terms,
                population=fallback_population,
            )
            if not scoped:
                return False
            member_state = _member_support(unit, scoped, required_items)
            supported_props = [
                prop for prop in properties if _property_supported(prop, scoped)
            ]
            population = fallback_population
        clauses = _drop_property_echoes(clauses, properties)
        if topics and not all(
            _topic_covered(
                topic,
                claim,
                alias_groups=alias_groups,
                population=population,
            )
            for topic in topics
        ):
            return False
        targets = fallback_clauses if fallback_clauses is not None else clauses
        if require_all_fallback:
            clause_ok = (
                all(_clause_content_ok(clause, scoped) for clause in targets)
                if targets
                else True
            )
            property_ok = (
                len(supported_props) == len(properties) if properties else True
            )
            return clause_ok and property_ok
        hit = any(_clause_content_ok(clause, scoped) for clause in targets)
        if properties and supported_props:
            hit = True
        return hit
    aspect_ok = any(
        _aspect_supported(
            aspect,
            scoped,
            content,
            citation_text,
            member_state,
            original_span=unit.original_span or "",
        )
        or any(
            _property_covers_aspect(prop, aspect) for prop in supported_props
        )
        for aspect in aspects
    )
    if not aspect_ok:
        return False
    if properties:
        if require_all_properties:
            return len(supported_props) == len(properties)
        return bool(supported_props)
    return True


def _fallback_union_binding(
    unit: QuestionUnit,
    claims: list[Claim],
    clauses: list[str],
    *,
    class_terms: dict[str, tuple[str, ...]] | None = None,
    required_items: list[str] | None = None,
    alias_groups: tuple[tuple[str, ...], ...] = (),
    properties: list[_Property] | tuple[_Property, ...] = (),
    protected: tuple[str, ...] = (),
) -> tuple[list[str], list[str]]:
    """多结论并集：每条只需覆盖至少一个子方面，整体必须覆盖全部子方面。

    每条候选仍逐条经过 subject/版本/班型门禁与同源主题核对；不相关结论
    不能挂到支持 ids，也不能让另一单元的一条结论替本单元扩全。
    """
    eligible = [
        claim
        for claim in claims
        if _claim_supports_unit(
            unit,
            claim,
            class_terms=class_terms,
            required_items=required_items,
            alias_groups=alias_groups,
            require_all_fallback=False,
            fallback_clauses=clauses,
            properties=properties,
            require_all_properties=False,
            protected=protected,
        )
    ]
    uncovered = [
        clause
        for clause in clauses
        if not any(
            _claim_supports_unit(
                unit,
                claim,
                class_terms=class_terms,
                required_items=required_items,
                alias_groups=alias_groups,
                require_all_fallback=False,
                fallback_clauses=[clause],
                properties=(),
                protected=protected,
            )
            for claim in eligible
        )
    ]
    uncovered.extend(
        prop.cue or prop.kind
        for prop in properties
        if not any(
            _claim_supports_unit(
                unit,
                claim,
                class_terms=class_terms,
                required_items=required_items,
                alias_groups=alias_groups,
                require_all_fallback=False,
                fallback_clauses=[],
                properties=[prop],
                require_all_properties=False,
                protected=protected,
            )
            for claim in eligible
        )
    )
    return [claim.claim_id for claim in eligible], uncovered


def _requirement_union_binding(
    unit: QuestionUnit,
    claims: list[Claim],
    properties: list[_Property],
    *,
    class_terms: dict[str, tuple[str, ...]] | None = None,
    required_items: list[str] | None = None,
    alias_groups: tuple[tuple[str, ...], ...] = (),
) -> tuple[list[str], list[str]]:
    """Join aspects and explicitly asked properties across verified claims.

    No single claim has to repeat every term: one may state the selected course
    list, another the unselected one, while each still passes the unit gates.
    """
    aspects = [
        aspect
        for aspect in unit.requested_aspects
        if aspect
        and not any(_property_covers_aspect(prop, aspect) for prop in properties)
    ]
    eligible = [
        (claim, _gated_content(unit, claim, class_terms, alias_groups))
        for claim in claims
    ]
    eligible = [(claim, scoped) for claim, scoped in eligible if scoped]
    contributors: list[str] = []
    missing: list[str] = []

    def add(claim_ids: list[str]) -> None:
        for claim_id in claim_ids:
            if claim_id not in contributors:
                contributors.append(claim_id)

    for aspect in aspects:
        aspect_unit = unit.model_copy(update={"requested_aspects": [aspect]})
        hits = [
            claim.claim_id
            for claim, _ in eligible
            if _claim_supports_unit(
                aspect_unit,
                claim,
                class_terms=class_terms,
                required_items=required_items,
                alias_groups=alias_groups,
            )
        ]
        if hits:
            add(hits)
        else:
            missing.append(aspect)
    for prop in properties:
        hits = [
            claim.claim_id
            for claim, scoped in eligible
            if _property_supported(prop, scoped)
        ]
        if hits:
            add(hits)
        else:
            missing.append(prop.cue or prop.kind)
    return contributors, missing


_ASPECT_ECHO_NOISE = re.compile(r"[\s　、，,。；;：:？?（）()]")


def _exact_aspect_echo(item: str, unit: QuestionUnit) -> str | None:
    key = _ASPECT_ECHO_NOISE.sub("", item or "")
    if not key:
        return None
    for aspect in unit.requested_aspects:
        if aspect and _ASPECT_ECHO_NOISE.sub("", aspect) == key:
            return aspect
    return None


_STEP_LAYOUT_NOISE = re.compile(
    r"[\s　，,。；;：:、！!？?（）()【】\[\]「」『』《》“”\"'‘’·…—–\-/／]+"
)


def _normalized_step_text(text: str) -> str:
    """Only whitespace/layout punctuation; negation, numbers and actors stay."""
    return _STEP_LAYOUT_NOISE.sub("", text or "")


def _procedure_steps_established(
    unit: QuestionUnit,
    scoped_claims: list[Claim],
    evidence: list[Evidence],
) -> bool:
    """Every enumerated source step must be represented in the final claims."""
    by_key = {item.key: item.article for item in evidence}
    steps: list[str] = []
    for claim in scoped_claims:
        for citation in claim.citations:
            article = by_key.get((citation.article_id, citation.part_no))
            if article is None:
                continue
            candidate = _procedure_source_steps(
                article.text, unit.original_span or ""
            )
            if len(candidate) > len(steps):
                steps = candidate
    if len(steps) < 2:
        return False
    combined = _normalized_step_text(
        "\n".join(claim.text for claim in scoped_claims)
    )
    for step in steps:
        key = _normalized_step_text(step)
        if not key or key not in combined:
            return False
    return True


def _aspect_echo_established(
    unit: QuestionUnit,
    aspect: str,
    claims: list[Claim],
    evidence: list[Evidence],
    *,
    class_terms: dict[str, tuple[str, ...]] | None,
    required_items: list[str] | None,
    alias_groups: tuple[tuple[str, ...], ...],
) -> bool:
    if aspect != "办理程序":
        aspect_unit = unit.model_copy(update={"requested_aspects": [aspect]})
        return any(
            _claim_supports_unit(
                aspect_unit,
                claim,
                class_terms=class_terms,
                required_items=required_items,
                alias_groups=alias_groups,
            )
            for claim in claims
        )
    gate = unit.model_copy(update={"requested_aspects": []})
    scoped = [
        claim
        for claim in claims
        if _claim_supports_unit(
            gate,
            claim,
            class_terms=class_terms,
            required_items=required_items,
            alias_groups=alias_groups,
        )
    ]
    return _procedure_steps_established(unit, scoped, evidence)


def _flow_supports_unit(unit: QuestionUnit, step_id: str, checklist: list[ChecklistItem]) -> bool:
    """A flow step must match the unit's object and ask, not just exist (A5)."""
    step = next((item for item in checklist if item.step_id == step_id), None)
    if step is None:
        return False
    haystack = " ".join(
        str(part)
        for part in (
            step.title,
            step.description,
            step.department,
            " ".join(step.materials or []),
        )
        if part
    )
    if unit.objects and not any(obj in haystack for obj in unit.objects if obj):
        return False
    if unit.subject and unit.subject not in haystack:
        return False
    aspects = [aspect for aspect in unit.requested_aspects if aspect]
    if not aspects:
        return True
    for aspect in aspects:
        pattern = _ASPECT_PATTERNS.get(aspect)
        if (pattern and re.search(pattern, haystack)) or aspect in haystack:
            return True
        if _content_supports(aspect, haystack):
            return True
    return False


@dataclass
class CoverageAudit:
    units: list[dict] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)
    unavailable: bool = False
    error: str = ""
    # R8：服务故障分类与尝试次数，进入 trace 而非写成“缺资料”。
    error_kind: str = ""
    error_attempts: int | None = None
    prompt_tokens: int = 0
    completion_tokens: int = 0
    latency_ms: int = 0


class CoverageChecker:
    def __init__(
        self,
        provider: ChatProvider,
        *,
        profile_lexicon: ProfileLexicon | None = None,
    ):
        self._provider = provider
        self._class_terms = profile_lexicon.class_terms if profile_lexicon else None
        groups: list[tuple[str, ...]] = []
        if profile_lexicon:
            for mapping in (profile_lexicon.college_terms, profile_lexicon.major_terms):
                for terms in mapping.values():
                    clean = tuple(term for term in terms if term)
                    if clean:
                        groups.append(clean)
        self._alias_groups = tuple(dict.fromkeys(groups))

    def _protected_terms(self, unit: QuestionUnit, span: str) -> tuple[str, ...]:
        terms: list[str] = [unit.subject, *unit.objects]
        for group in self._alias_groups:
            terms.extend(group)
        terms.extend(
            re.findall(r"[“\"「『《]([^”\"」』》]{2,30})[”\"」』》]", span)
        )
        terms.extend(_syntactic_course_terms(span))
        return tuple(dict.fromkeys(term for term in terms if term))

    def check(
        self,
        question: str,
        units: list[QuestionUnit],
        claims: list[Claim],
        evidence: list[Evidence],
        checklist: list[ChecklistItem],
        *,
        retrieval_warnings: list[str] | None = None,
    ) -> CoverageAudit:
        if not units:
            return CoverageAudit()
        # Only hydrated original citations may support checklist coverage.
        evidence_ids = {e.article.article_id for e in evidence}
        valid_flow = [
            item
            for item in checklist
            if item.citations
            and set(item.citations) <= evidence_ids
            and item.status != "not_applicable"
        ]
        known_claims = {claim.claim_id for claim in claims}
        known_claims.update("flow:" + item.step_id for item in valid_flow)
        required_by_unit = {
            u.unit_id: required_source_items(u, evidence, class_terms=self._class_terms)
            for u in units
        }
        payload = {
            "question": question,
            "units": [
                {
                    **u.model_dump(mode="json"),
                    "required_source_items": required_by_unit[u.unit_id],
                }
                for u in units
            ],
            "verified_claims": [c.model_dump(mode="json") for c in claims],
            "verified_flow_checklist": [
                {**c.model_dump(mode="json"), "claim_id": "flow:" + c.step_id} for c in valid_flow
            ],
            "retrieval_diagnostics": [
                w
                for w in (retrieval_warnings or [])
                if w.startswith(("COLLECTION_", "SOURCE_VIEW_"))
            ],
        }
        messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {
                "role": "user",
                "content": json.dumps(payload, ensure_ascii=False)
                + "\n本轮原始证据（只用于判断遗漏，不算已答）：\n"
                + build_evidence_block(evidence),
            },
        ]
        audit = CoverageAudit()
        try:
            response = self._provider.complete(messages, temperature=0, max_tokens=2400)
            audit.prompt_tokens = response.prompt_tokens
            audit.completion_tokens = response.completion_tokens
            audit.latency_ms = response.latency_ms
            text = (
                response.content.strip()
                .removeprefix("```json")
                .removeprefix("```")
                .removesuffix("```")
                .strip()
            )
            raw = json.loads(text)
            rows = raw["units"]
            if not isinstance(rows, list):
                raise ValueError("coverage units must be a list")
            by_id = {r["unit_id"]: r for r in rows if isinstance(r, dict)}
            if set(by_id) != {u.unit_id for u in units} or len(by_id) != len(rows):
                raise ValueError("coverage unit identifiers mismatch")
            statuses = {
                "answered",
                "missing_evidence",
                "generation_missing",
                "verification_failed",
                "clarification_needed",
            }
            proven_props: list[_Property] = []
            proven_contexts: list[str] = []
            unit_scopes = {
                unit.unit_id: tuple(
                    term for term in (unit.subject, *unit.objects) if term
                )
                for unit in units
            }
            for unit in units:
                row = by_id[unit.unit_id]
                status = row.get("status")
                if status not in statuses:
                    raise ValueError("unknown coverage status")
                ids = row.get("claim_ids", [])
                if not isinstance(ids, list) or set(ids) - known_claims:
                    raise ValueError("coverage references unverified claims")
                missing = row.get("missing_items", [])
                if not isinstance(missing, list) or any(not isinstance(m, str) for m in missing):
                    raise ValueError("invalid missing items")
                # Never accept answered without actual final support, even if the model says so.
                deterministic_missing = missing_source_items(
                    unit,
                    evidence,
                    "\n".join(c.text for c in claims),
                    class_terms=self._class_terms,
                )
                # 035：整句问题当方面时，必须每个并列子问题都有结论覆盖；
                # 只答“科研成果加分”不能把“学术不端”一起算作已答。
                span = (unit.original_span or "").strip("？?。 ")
                required_items = required_by_unit[unit.unit_id]
                protected = self._protected_terms(unit, span)
                properties = _requested_properties(span, protected)
                fallback = _is_fallback_unit(unit)
                applicable = _applicable_properties(unit, properties)
                clauses, topics = (
                    _question_clauses(span, protected) if fallback else ([], [])
                )
                clauses = _drop_property_echoes(clauses, applicable)
                property_gaps, property_ids = (
                    _property_gaps(
                        applicable,
                        claims,
                        unit,
                        class_terms=self._class_terms,
                        alias_groups=self._alias_groups,
                        topics=topics,
                    )
                    if applicable
                    else ([], [])
                )
                property_missing = [
                    prop.cue or prop.kind for prop in property_gaps
                ]
                # A concrete verified property answer (“不收费”“不授予学位”)
                # contradicts a model gap echo; numbered/list omissions survive.
                covered_props = [
                    prop for prop in applicable if prop not in property_gaps
                ]
                filtered_missing = [
                    item
                    for item in missing
                    if not _contradicts_covered_property(item, covered_props)
                ]
                if topics:
                    filtered_missing = [
                        item
                        for item in filtered_missing
                        if not _context_echo(item, topics)
                    ]
                model_missing = _clean_missing(filtered_missing, question)
                concrete_gaps = split_missing(filtered_missing, question)[1]
                missing = list(dict.fromkeys([*model_missing, *deterministic_missing]))
                clause_gaps, clause_ids = (
                    _clause_gaps(
                        clauses, claims, topics, alias_groups=self._alias_groups
                    )
                    if clauses
                    else ([], [])
                )
                if fallback:
                    clause_gaps = list(
                        dict.fromkeys([*clause_gaps, *property_missing])
                    )
                # A5 反向核对：模型常把已经答出的单元判成缺项并回显问题本身。
                # 只要有一条通过核对的结论实际覆盖了该单元对象与方面，且原文清单
                # 没有确定性漏项、模型也没有点名具体缺口，就按已答处理并写回真实
                # claim_ids；泛称断言与漏列清单项仍会被内容检查挡住。
                recoverable = (
                    status != "answered"
                    and not unit.ambiguity
                    and not deterministic_missing
                    and not clause_gaps
                    and not property_gaps
                )
                recovered_aspect_echo = False
                if recoverable:
                    residual: list[str] = []
                    for item in filtered_missing:
                        aspect = _exact_aspect_echo(item, unit)
                        if aspect is None or not _aspect_echo_established(
                            unit,
                            aspect,
                            claims,
                            evidence,
                            class_terms=self._class_terms,
                            required_items=required_items,
                            alias_groups=self._alias_groups,
                        ):
                            residual.append(item)
                    if len(residual) != len(filtered_missing):
                        recovered_aspect_echo = True
                        filtered_missing = residual
                        model_missing = _clean_missing(residual, question)
                        concrete_gaps = split_missing(residual, question)[1]
                        missing = list(
                            dict.fromkeys([*model_missing, *deterministic_missing])
                        )
                if (
                    recoverable
                    and not concrete_gaps
                    and (not model_missing or not recovered_aspect_echo)
                    and not any(
                        _exact_aspect_echo(item, unit) is not None
                        for item in filtered_missing
                    )
                ):
                    if clauses:
                        recovered = list(
                            dict.fromkeys([*clause_ids, *property_ids])
                        )
                    elif fallback:
                        recovered = property_ids
                    elif applicable:
                        recovered, union_missing = _requirement_union_binding(
                            unit,
                            claims,
                            applicable,
                            class_terms=self._class_terms,
                            required_items=required_items,
                            alias_groups=self._alias_groups,
                        )
                        if union_missing:
                            recovered = []
                            missing = list(
                                dict.fromkeys([*missing, *union_missing])
                            )
                    else:
                        recovered = [
                            claim.claim_id
                            for claim in claims
                            if _claim_supports_unit(
                                unit,
                                claim,
                                class_terms=self._class_terms,
                                required_items=required_items,
                                alias_groups=self._alias_groups,
                            )
                        ]
                    if recovered:
                        status = "answered"
                        ids = recovered
                        missing = []
                if status == "answered" and fallback and property_gaps:
                    status = "generation_missing"
                    missing = list(dict.fromkeys([*missing, *property_missing]))
                    ids = []
                if status == "answered" and clauses and clause_gaps:
                    status = "generation_missing"
                    missing = list(dict.fromkeys([*missing, *clause_gaps]))
                    # 缺子方面时整条结论链不算完整支持；不留下误导性的 claim_ids。
                    ids = []
                if status == "answered" and (not ids or missing or unit.ambiguity):
                    status = "generation_missing"
                if status == "answered" and ids:
                    if clauses:
                        # 多结论并集：每条只需覆盖一个子方面，整体必须覆盖全部；
                        # 每条仍要通过本单元的 subject/版本/班型/同源门禁。
                        union_ids, uncovered = _fallback_union_binding(
                            unit,
                            claims,
                            clauses,
                            class_terms=self._class_terms,
                            required_items=required_items,
                            alias_groups=self._alias_groups,
                            properties=applicable,
                            protected=protected,
                        )
                        flow_ids = [
                            claim_id
                            for claim_id in ids
                            if claim_id.startswith("flow:")
                            and _flow_supports_unit(
                                unit, claim_id[len("flow:") :], valid_flow
                            )
                        ]
                        if union_ids and not uncovered:
                            ids = list(dict.fromkeys([*union_ids, *flow_ids]))
                        else:
                            status = "generation_missing"
                            missing = list(
                                dict.fromkeys([*missing, *(uncovered or clauses)])
                            )
                            ids = []
                    elif applicable:
                        # 子问题由多条已验证结论共同覆盖，不要求单条重复全部问法。
                        if fallback:
                            union_ids, uncovered = property_ids, property_missing
                        else:
                            union_ids, uncovered = _requirement_union_binding(
                                unit,
                                claims,
                                applicable,
                                class_terms=self._class_terms,
                                required_items=required_items,
                                alias_groups=self._alias_groups,
                            )
                        if union_ids and not uncovered:
                            ids = union_ids
                        else:
                            status = "generation_missing"
                            missing = list(
                                dict.fromkeys(
                                    [
                                        *missing,
                                        *(
                                            uncovered
                                            or [
                                                f"{unit.subject} {'、'.join(unit.requested_aspects)}".strip()
                                            ]
                                        ),
                                    ]
                                )
                            )
                            ids = []
                    else:
                        claims_by_id = {claim.claim_id: claim for claim in claims}
                        bound = [
                            claim_id
                            for claim_id in ids
                            if (
                                _flow_supports_unit(
                                    unit, claim_id[len("flow:") :], valid_flow
                                )
                                if claim_id.startswith("flow:")
                                else _claim_supports_unit(
                                    unit,
                                    claims_by_id.get(claim_id),
                                    class_terms=self._class_terms,
                                    required_items=required_items,
                                    alias_groups=self._alias_groups,
                                )
                            )
                        ]
                        ids = bound
                        if not bound:
                            recovered, uncovered = (
                                ([], [])
                                if unit.ambiguity
                                else _requirement_union_binding(
                                    unit,
                                    claims,
                                    [],
                                    class_terms=self._class_terms,
                                    required_items=required_items,
                                    alias_groups=self._alias_groups,
                                )
                            )
                            if recovered and not uncovered:
                                ids = recovered
                            else:
                                status = "generation_missing"
                                missing = list(
                                    dict.fromkeys(
                                        [
                                            *missing,
                                            *(
                                                uncovered
                                                or [
                                                    f"{unit.subject} {'、'.join(unit.requested_aspects)}".strip()
                                                ]
                                            ),
                                        ]
                                    )
                                )
                                ids = []
                if status != "answered":
                    audit.missing.extend(
                        missing or [f"{unit.subject} {'、'.join(unit.requested_aspects)}".strip()]
                    )
                audit.units.append(
                    {
                        "unit_id": unit.unit_id,
                        "status": status,
                        "claim_ids": ids,
                        "missing_items": missing,
                    }
                )
                if status == "answered":
                    proven_props.extend(covered_props)
                    proven_contexts.extend(topics)
            unanswered = raw.get("unanswered_questions", [])
            if not isinstance(unanswered, list) or any(not isinstance(m, str) for m in unanswered):
                raise ValueError("invalid unanswered questions")
            answered_all = bool(audit.units) and all(
                row["status"] == "answered" for row in audit.units
            )
            kept_unanswered: list[str] = []
            for item in unanswered:
                # 整句回显不是新缺口；只有已按单元/属性证明时才抑制属性回显。
                if _is_question_echo(item, question):
                    continue
                if answered_all and _unanswered_is_redundant(
                    item, proven_props, proven_contexts
                ):
                    continue
                kept_unanswered.append(item)
            audit.missing = list(
                dict.fromkeys([*audit.missing, *kept_unanswered])
            )[:12]
            if kept_unanswered and answered_all:
                for item in kept_unanswered:
                    target = next(
                        (
                            row
                            for row in audit.units
                            if any(
                                term and term in item
                                for term in unit_scopes.get(row["unit_id"], ())
                            )
                        ),
                        audit.units[0],
                    )
                    target["status"] = "generation_missing"
                    target["missing_items"] = list(
                        dict.fromkeys([*target["missing_items"], item])
                    )
        except (ChatProviderError, ValueError, KeyError, TypeError) as exc:
            audit.unavailable = True
            audit.error = safe_error_summary(exc)
            audit.error_kind = exc.kind if isinstance(exc, ChatProviderError) else "internal"
            audit.error_attempts = exc.attempts if isinstance(exc, ChatProviderError) else None
            audit.missing = ["本轮未能完成答案完整性核对"]
            audit.units = [
                {
                    "unit_id": u.unit_id,
                    "status": "verification_failed",
                    "claim_ids": [],
                    "missing_items": ["完整性核对服务不可用"],
                }
                for u in units
            ]
        return audit
