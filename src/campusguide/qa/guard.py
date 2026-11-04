"""W2 最小确定性 guard。

只拦截明确的空请求、历史版本、隐私和校外问题；宁可漏拦，也不把本校问题
误判为校外。完整拒答判定树在 W3。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from campusguide.domain.enums import OutcomeCode
from campusguide.domain.profiles import UserProfile

MAX_QUERY_CHARS = 300

_HISTORICAL_RE = re.compile(
    r"去年|前年|旧版|老版|历史版本|以前版本|废止|当时(?:的|有效)|"
    r"(?:19|20)\d{2}\s*年\s*(?:的\s*)?(?:版本|旧版|老版)"
)
_SURNAMES = (
    "赵钱孙李周吴郑王冯陈褚卫蒋沈韩杨朱秦尤许何吕施张孔曹严华金魏陶姜谢邹喻柏水窦章"
    "云苏潘葛范彭郎鲁韦马苗凤花方俞任袁柳鲍史唐费薛雷贺倪汤罗毕郝邬安常傅卞齐康伍"
    "余顾孟黄萧尹姚邵汪毛戴宋庞熊纪舒屈项祝董梁杜阮蓝闵席季贾路江童颜郭钟徐邱骆高"
    "夏蔡田樊胡凌霍虞万支柯昝管卢莫房裘缪解应宗丁宣邓单杭洪包诸左石崔吉龚程邢裴陆"
    "荣翁荀羊惠甄曲封储靳松井段富巫乌焦巴牧山谷车侯全班仰秋仲伊宫宁仇甘厉戎祖武符"
    "刘景詹龙叶幸司黎薄白蒲邰鄂索赖卓屠蒙池乔翟谭姬申扶冉雍桑桂牛寿通燕浦尚温庄晏"
    "柴瞿阎慕连茹艾容向古易廖终居衡步都耿满弘匡国文寇广"
)
_GRADE_FIELD_SRC = r"成绩(?!单|证明)|绩点|学分绩|加权成绩|平均分|均分|分数(?!线)"
_IDENTIFIER_FIELD_SRC = (
    r"学号|身份证(?:号)?|手机号|联系电话|联系方式|"
    r"宿舍(?:号|楼|房间|房号)?|寝室(?:号)?|一卡通(?:号|卡号)?|银行卡(?:号)?|密码|账号"
)
_RECORD_FIELD_SRC = rf"(?:{_GRADE_FIELD_SRC}|排名|{_IDENTIFIER_FIELD_SRC})"
_BARE_FIELD_SRC = (
    r"学号|成绩(?!单|证明)|绩点|排名|分数(?!线)|身份证(?:号)?|手机号|联系电话|"
    r"联系方式|宿舍(?:号|楼|房间|房号)|寝室(?:号)?|一卡通|银行卡(?:号)?|密码|账号"
)
_PERSON_REF_SRC = (
    rf"(?:[{_SURNAMES}][\u4e00-\u9fa5]{{1,2}}|某某|某人|某同学|某学生|这位同学|"
    r"那位同学|(?<!其)他|她|室友|舍友|同桌|同班同学|同学|老师|辅导员|班主任|导师|"
    r"谁|哪位|哪几位|哪些(?:同学|学生|人)|(?<![A-Za-z])(?:TA|ta)(?![A-Za-z]))"
)
_INSTITUTION_WORDS = (
    "华南",
    "华农",
    "SCAU",
    "学校",
    "学院",
    "大学",
    "全校",
    "全院",
    "全班",
    "全系",
    "全国",
    "全省",
    "全市",
    "年级",
    "班级",
    "专业",
    "广东",
    "广州",
)
_STATUS_SRC = (
    r"学业预警|退学|留级|降级|试读|开除|记过|处分|挂科|"
    r"挂(?:了)?几(?:科|门)|不及格|补考|重修"
)
# 姓氏字类会把抽象制度词识别为姓名（“纪律处分” → 纪律/纪律处），这些不是人员引用。
_ABSTRACT_POLICY_TERMS = frozenset({"纪律"})
# 姓氏字类也会把制度角色或事件名词识别为姓名（“违纪者” → 纪者、
# “违纪行为” → 纪行为），这些泛称不是具名个人。
_POLICY_FRAGMENT_SUFFIXES = (
    "者",
    "学生",
    "人",
    "行为",
    "情节",
    "事件",
    "案件",
    "事实",
    "情形",
    "情况",
    "记录",
    "后果",
)

_OFFICE_CONTACT_RE = re.compile(
    r"(?:华南农业大学|华农|学校|招生办|教务处|研究生院|学生处|学生工作部|"
    r"学院办公室|校医院|图书馆|保卫处|财务处|就业指导中心|档案室|"
    r"服务大厅|综合服务大厅|咨询处)(?:的)?"
    r"(?:办公电话|咨询电话|联系电话|联系方式)"
)
_INSTITUTION_RECORD_RE = re.compile(
    r"(?:华南农业大学|华农|SCAU|学校|本校|我校|我院|我系|我专业|贵校|学院|本院|"
    r"大学|全校|全院|全班|全年级|全系|我们班|本班|我班)(?:的)?(?:成绩)?"
    r"(?:排名|平均分|分数线|录取线)"
)
_DOCUMENT_RE = re.compile(r"成绩(?:单|证明)")
_FORMAT_RULE_RE = re.compile(
    r"(?:保留|显示|打印)(?:到)?(?:小数点后)?(?:几位|多少位|\d+\s*位)(?:小数(?:位)?)?"
)
_PRIVACY_POLICY_RE = re.compile(
    r"(?:他人|别人|其他人)(?:能否|能不能|可不可以|是否可以|可以|是否|有没有权利|有权)"
    r"[^，。；！？?]{0,10}(?:查询|查看|查|获取|看到|知道)[^，。；！？?]{0,10}"
    r"|(?:他人|别人|其他人)[^，。；！？?]{0,10}(?:查询|查看|查|获取|看到|知道)"
    r"[^，。；！？?]{0,10}(?:吗|么|呢|[？?])"
    r"|(?:隐私|个人信息)(?:保护|安全|权益)(?:规定|政策|措施|办法|制度)?"
    r"|(?:有哪些|有什么|哪些|什么)[^，。；！？?]{0,6}"
    r"保护(?:规定|政策|措施|办法|制度)"
)
_STUDENT_NUMBER_RULE_RE = re.compile(
    r"学号.{0,8}(?:是多少位|多少位|几位|位数|编码|格式|规则|构成|组成|"
    r"怎么编|如何编|怎么排|如何排)"
)
_RECORD_RULE_RE = re.compile(
    r"(?:成绩|绩点|排名)(?:的)?(?:要求|标准|合格线|及格线|分数线|门槛|"
    r"管理办法|管理规定)"
    r"|(?:成绩)?排名(?:规则|办法|计算方法|评定办法|计算办法)"
    r"|(?:成绩排名|成绩|绩点|排名)(?:的)?(?:如何|怎么)(?:计算|算|评定)"
    r"|(?:学业预警|退学|留级|降级|处分|挂科|补考|重修)(?:的)?"
    r"[\u4e00-\u9fa5]{0,4}?(?:标准|条件|规定|办法|政策|要求|流程)"
    r"|成绩管理|学籍管理|成绩档案管理"
    r"|(?:成绩|成绩单|证明)(?:打印|补办|办理|开具)?(?:收费|费用|价格|标准)"
    r"|(?:成绩|分数|绩点).{0,2}多少分.{0,4}(?:算|才|能|可以|及格|合格|过|达标|达到)"
    r"|(?:成绩|绩点|排名).{0,2}多少名.{0,4}(?:算|才|能|可以|进|拿|获得|达到)"
)
_METHOD_RE = re.compile(
    r"(?:如何|怎么(?!样)|怎样)[^，。；！？?]{0,20}?"
    r"(?:查询|查看|查|打印|办理|申请|重置|找回|修改|补办|开具|申领|预约|申诉|"
    r"复核|复议|获取|下载|登录|绑定|解绑|缴费|支付|操作|处理|分配|安排|走|去|改|"
    r"算|计算|评定)"
    r"|怎么办|咋办|咋整|在哪|哪里|哪儿|入口|网址|流程|步骤|手续|材料|"
    r"需要(?:带|准备|提交|提供|哪些|什么)|要带|"
    r"收费|价格|费用|多少钱|多久|多长时间|办公时间|开放时间|"
    r"补办|办理|打印|开具|申领|预约|申诉|复核|复议|重置|找回|挂失|绑定|解绑|"
    r"查询方式|查看方式|登录方式|操作|忘记|忘了|丢失|遗失|丢了|过期"
)
_FIRST_PERSON_RE = re.compile(r"我(?!们|校|院|系|班|专业)|本人|自己|咱|俺")
_VALUE_RE = re.compile(
    r"是多少|多少(?:分|名|科|门|位)?|几(?:分|名|科|门|位)|第几(?:名)?|"
    r"怎么样|什么(?:成绩|排名|分数)|"
    r"告诉我|给我(?:查|查询|看|发|报|找)?|帮我(?:查|查询|找|调|看|看看)|"
    r"查一下|查查|查(?:出|到)|查询我的|查我的|查看我的|看看我的|看一下我的|看一下|"
    r"列出来|显示|报一下|发我|发给我"
)
_EXAM_SCORE_RE = re.compile(r"(?:考|得)了?多少分|考了?几(?:分|门)|得了?几分")
_STATUS_QUERY_RE = re.compile(
    r"了吗|了没|没有|是否|是不是|会不会|有没有|几(?:科|门)|记录|情况|结果|"
    r"受过|有过|过吗|处分"
)
# 程序性“怎么查/去哪办”不能绕过具名第三方记录、结果或当前状态查询。
_NAMED_RECORD_STATUS_QUERY_RE = re.compile(
    r"记录|结果|(?:当前|目前|现在)?状态|办理(?:情况|进度)|进度|"
    r"(?:是否|有没有|是不是|已经|已)(?:被|已经|已|成功)*"
    r"(?:解除|撤销|办完|办理完|结束|完成)"
    r"|解除(?:了吗|了没|没有|的结果|结果|情况|状态)"
)
_RECORD_QUERY_ACTION_RE = re.compile(r"查询|查看|查")
_RECORD_FIELD_RE = re.compile(_RECORD_FIELD_SRC)
_BARE_FIELD_RE = re.compile(_BARE_FIELD_SRC)
_FIELD_WORD_RE = re.compile(rf"(?:{_RECORD_FIELD_SRC}|{_STATUS_SRC})")
_STATUS_RE = re.compile(_STATUS_SRC)
_PERSON_REF_RE = re.compile(_PERSON_REF_SRC)
_NAMED_POSSESSIVE_RE = re.compile(
    rf"(?P<person>{_PERSON_REF_SRC})(?:同学|老师|辅导员|班主任|导师)?"
    rf"的[\u4e00-\u9fa5]{{0,6}}?(?:{_RECORD_FIELD_SRC})"
)
_PERSON_FIELD_ADJ_RE = re.compile(
    rf"(?P<person>{_PERSON_REF_SRC})(?:同学|老师|辅导员|班主任|导师)?"
    rf"(?:的[\u4e00-\u9fa5]{{0,6}}?)?"
    rf"(?P<field>{_RECORD_FIELD_SRC})"
)
_PERSON_STATUS_POSSESSIVE_RE = re.compile(
    rf"(?P<person>{_PERSON_REF_SRC})(?:同学|老师|辅导员|班主任|导师)?"
    rf"的[\u4e00-\u9fa5]{{0,4}}?(?P<status>{_STATUS_SRC})"
)
_PERSON_STATUS_PREDICATE_RE = re.compile(
    rf"(?P<person>{_PERSON_REF_SRC})(?:同学|老师|辅导员|班主任|导师)?"
    rf"(?:的)?(?:被|已经|曾|是否|是不是|有没有|会不会|会|要|正在|受到|受|"
    rf"收到|获得|得到|给予|达到|符合|属于|构成|评为|评定为|认定为|列为|处以|记)"
    rf"[\u4e00-\u9fa5]{{0,6}}?(?P<status>{_STATUS_SRC})"
)
_BARE_PERSON_STATUS_RE = re.compile(
    rf"(?P<person>{_PERSON_REF_SRC})(?:同学|老师|辅导员|班主任|导师)?"
    rf"(?P<status>{_STATUS_SRC})"
)
_PERSONAL_STATE_SUFFIX_RE = re.compile(r"^(?:了吗|了没|没有)")
_CLAUSE_SPLIT_RE = re.compile(r"[，。；！？?;；,\n]+")
_PROCEDURE_HOW_RE = re.compile(
    r"如何|怎么(?!样)|怎样|咋办|咋整|在哪|哪里|哪儿|去哪|找谁|"
    r"哪个部门|什么部门|哪个窗口|什么窗口"
)
_PROCEDURE_ACTION_RE = re.compile(
    r"解除|撤销|申诉|复核|复议|查询|查看|查|办理|申请|补办|恢复|消除|处理|找谁|"
    r"怎么办|咋办|咋整"
)
_DIRECT_RECORD_MARKER_RE = re.compile(
    r"请确认|直接确认|直接查询|直接查|到底|结果是多少|是否已经"
)
# “告诉我”只有紧跟记录、结果、数值或具体状态时才算直接取数，
# 不能把“请告诉我他怎么办这个手续”这类礼貌前缀当成查询个人记录。
_POLITE_RECORD_REQUEST_RE = re.compile(
    r"告诉我[^，。；！？?;；,\n]{0,12}?"
    r"(?:记录|结果|数值|多少|几(?:分|名|科|门|位)|第几(?:名)?|是否|有没有|"
    r"(?:当前|目前|现在|具体)?(?:状态|进度))"
)
_RULE_TOPIC_SRC = (
    r"条件|标准|规定|要求|等级|级别|种类|类型|类别|颜色|区别|不同|后果|影响|"
    r"流程|程序|手续|办法|政策|细则|条例|认定|评定|触发|解除|撤销|分类|划分|"
    r"定义|含义|意思|学分|不及格|挂科|是什么|是啥|指什么|包括什么|"
    r"处理(?:结果|方式|流程|规定|办法|程序)"
)
_RULE_INTENT_RE = re.compile(
    rf"(?:哪些|哪种|哪几种|哪几个|哪几级|哪一级|几种|什么|何种|如何|怎么(?!样)|怎样|多少|几)"
    rf"[^，。；！？?;；,\n]{{0,12}}?(?:{_RULE_TOPIC_SRC})"
    rf"|(?:什么是|何为|什么叫|指的是)[^，。；！？?;；,\n]{{0,10}}?(?:{_STATUS_SRC})"
    rf"|(?:{_STATUS_SRC})[^，。；！？?;；,\n]{{0,10}}?(?:{_RULE_TOPIC_SRC})"
    rf"|(?:条件|标准|规定|要求|流程|程序|手续|办法|政策|细则|条例)"
    r"(?:是|有|为|包括|包含|有哪些|是什么)"
)
# 学分不是 _RECORD_FIELD_SRC/_STATUS_SRC 中的字段，只识别明确索要个人挂科/不及格学分数值的窄问法。
_FAILED_CREDIT_ASK_RE = re.compile(
    r"挂(?:科|了|过|掉)?(?:了)?\s*"
    r"(?:几|多少|哪几|哪些|\d+|[一二三四五六七八九十]+)\s*(?:门)?(?:课)?(?:的)?\s*学分"
    r"|挂(?:科|了|过|掉)?(?:的)?学分\s*(?:是|有|为|到|达到)?\s*(?:多少|几)"
    r"|不及格(?:累计)?(?:了)?\s*"
    r"(?:几|多少|哪几|哪些|\d+|[一二三四五六七八九十]+)\s*(?:门)?(?:课)?(?:的)?\s*学分"
    r"|不及格(?:累计)?(?:的)?学分\s*(?:是|有|为|到|达到)?\s*(?:多少|几)"
)
_CREDIT_DIRECT_REQUEST_RE = re.compile(
    r"查询|查出|查一下|查查|帮我查|请查|调取|调出|报出|报一下|报给|"
    r"告诉我|发给我|发我|直接(?:报|说|告诉|给)|到底|究竟|显示|列出来"
)
_CREDIT_ANCHOR_BLOCK_RE = re.compile(
    r"预警|学业|学分|挂科|不及格|政策|条件|标准|触发|流程|复核|黄色|红色"
)
_OUT_OF_SCOPE_RE = re.compile(r"隔壁学校|其他学校|外校|别的学校|另一所学校")
_SCHOOL_RE = re.compile(r"华南农业大学|华农|SCAU", re.IGNORECASE)
_UNIVERSITY_RE = re.compile(r"[一-龥]{2,12}大学(?!生)")
_OWN_UNIVERSITY_NAMES = ("华南农业大学", "华农")
_VERSION_CONTEXT_RE = re.compile(r"培养方案|培养计划|版本|版|届")
_VERSION_YEAR_RE = re.compile(r"(20\d{2})\s*(?:版|级)")

_GUIDANCE = {
    OutcomeCode.INVALID_REQUEST: "请输入一个具体问题（不超过 300 字）。",
    OutcomeCode.HISTORICAL_NOT_SUPPORTED: (
        "W2 只回答现行有效制度，暂不支持历史版本比较或已废止文件查询。"
        "如需现行规定，请直接描述办理事项。"
    ),
    OutcomeCode.PRIVACY_REQUEST: (
        "本系统不查询个人学号、成绩、排名等个人信息，请通过学校教务系统或相关部门办理。"
    ),
    OutcomeCode.OUT_OF_SCOPE: "本系统只回答华南农业大学的公开制度与办事问题。",
}


@dataclass(frozen=True)
class GuardDecision:
    allowed: bool
    outcome: OutcomeCode | None = None
    message: str = ""


def _is_abstract_policy_term(value: str) -> bool:
    return any(value.startswith(term) for term in _ABSTRACT_POLICY_TERMS)


def _is_person_ref(value: str) -> bool:
    if _is_abstract_policy_term(value):
        return False
    if value and value[0] in _SURNAMES and value.endswith(_POLICY_FRAGMENT_SUFFIXES):
        return False
    if _FIELD_WORD_RE.fullmatch(value):
        return False
    return not any(word in value for word in _INSTITUTION_WORDS)


def _is_person_match(text: str, match: re.Match[str]) -> bool:
    person = match.group("person")
    if not _is_person_ref(person):
        return False
    start = match.start("person")
    return not (start > 0 and text[start - 1] == "违")


def _scrub_public_context(text: str) -> str:
    for pattern in (
        _PRIVACY_POLICY_RE,
        _OFFICE_CONTACT_RE,
        _INSTITUTION_RECORD_RE,
        _DOCUMENT_RE,
        _STUDENT_NUMBER_RULE_RE,
        _FORMAT_RULE_RE,
        _RECORD_RULE_RE,
    ):
        text = pattern.sub("", text)
    return text


def _has_direct_record_marker(text: str) -> bool:
    return bool(
        _DIRECT_RECORD_MARKER_RE.search(text)
        or _POLITE_RECORD_REQUEST_RE.search(text)
    )


def _named_record_request(
    text: str,
    *,
    had_method: bool,
    has_value: bool,
    has_status_query: bool,
) -> bool:
    for match in _NAMED_POSSESSIVE_RE.finditer(text):
        if not _is_person_match(text, match):
            continue
        if had_method and not has_value and not has_status_query:
            continue
        return True
    return False


def _named_possessive_status_query(clause: str) -> bool:
    if not _NAMED_RECORD_STATUS_QUERY_RE.search(clause):
        return False
    if not _RECORD_QUERY_ACTION_RE.search(clause):
        return False
    return any(
        _is_person_match(clause, match)
        for match in _PERSON_STATUS_POSSESSIVE_RE.finditer(clause)
    )


def _named_status_request(text: str) -> bool:
    has_direct_marker = _has_direct_record_marker(text)
    for clause in _CLAUSE_SPLIT_RE.split(text):
        if not clause.strip():
            continue
        if (
            not has_direct_marker
            and not _named_possessive_status_query(clause)
            and _PROCEDURE_HOW_RE.search(clause)
            and _PROCEDURE_ACTION_RE.search(clause)
        ):
            continue
        for pattern in (_PERSON_STATUS_POSSESSIVE_RE, _PERSON_STATUS_PREDICATE_RE):
            for match in pattern.finditer(clause):
                if _is_person_match(clause, match):
                    return True
        if _RULE_INTENT_RE.search(clause):
            continue
        for match in _BARE_PERSON_STATUS_RE.finditer(clause):
            if not _is_person_match(clause, match):
                continue
            trailing = clause[match.end():]
            if (
                _VALUE_RE.search(match.group("status"))
                or _VALUE_RE.match(trailing[:6])
                or _PERSONAL_STATE_SUFFIX_RE.match(trailing)
            ):
                return True
    return False


def _named_field_request(text: str) -> bool:
    for clause in _CLAUSE_SPLIT_RE.split(text):
        for match in _PERSON_FIELD_ADJ_RE.finditer(clause):
            if _is_person_match(clause, match):
                return True
    return False


def _personal_failed_credits_request(text: str) -> bool:
    for clause in _CLAUSE_SPLIT_RE.split(text):
        ask = _FAILED_CREDIT_ASK_RE.search(clause)
        if ask is None:
            continue
        residual = _METHOD_RE.sub("", clause)
        if not _CREDIT_DIRECT_REQUEST_RE.search(residual):
            if _METHOD_RE.search(clause):
                continue
            if _RULE_INTENT_RE.search(_FAILED_CREDIT_ASK_RE.sub("", clause)):
                continue
        head = clause[: ask.start()]
        if _FIRST_PERSON_RE.search(head):
            return True
        for match in _PERSON_REF_RE.finditer(head):
            person = match.group(0)
            if _is_person_ref(person) and not _CREDIT_ANCHOR_BLOCK_RE.search(person):
                return True
    return False


def _is_privacy_request(text: str) -> bool:
    if _personal_failed_credits_request(text):
        return True
    pre_method_text = _scrub_public_context(text)
    had_method = bool(_METHOD_RE.search(pre_method_text))
    text = _METHOD_RE.sub("", pre_method_text)

    has_value = bool(_VALUE_RE.search(text) or _EXAM_SCORE_RE.search(text))
    has_status_query = bool(_STATUS_QUERY_RE.search(text))
    has_field = bool(_RECORD_FIELD_RE.search(text))
    has_status = bool(_STATUS_RE.search(text))

    if _FIRST_PERSON_RE.search(text):
        if _EXAM_SCORE_RE.search(text):
            return True
        if has_field and has_value:
            return True
        if (
            has_status
            and has_status_query
            and (
                not had_method
                or has_value
                or _has_direct_record_marker(pre_method_text)
            )
        ):
            return True
        if not had_method and (has_field or has_status):
            return True

    if _named_record_request(
        text,
        had_method=had_method,
        has_value=has_value,
        has_status_query=has_status_query,
    ):
        return True
    if _named_status_request(pre_method_text):
        return True
    if has_field and has_value and _named_field_request(text):
        return True
    return bool(_BARE_FIELD_RE.search(text) and has_value)


def check_guard(question: str, *, max_chars: int = MAX_QUERY_CHARS) -> GuardDecision:
    text = question.strip()
    if not text or len(text) > max_chars:
        return GuardDecision(
            allowed=False,
            outcome=OutcomeCode.INVALID_REQUEST,
            message=_GUIDANCE[OutcomeCode.INVALID_REQUEST],
        )

    if _is_privacy_request(text):
        return GuardDecision(
            allowed=False,
            outcome=OutcomeCode.PRIVACY_REQUEST,
            message=_GUIDANCE[OutcomeCode.PRIVACY_REQUEST],
        )

    if _HISTORICAL_RE.search(text):
        return GuardDecision(
            allowed=False,
            outcome=OutcomeCode.HISTORICAL_NOT_SUPPORTED,
            message=_GUIDANCE[OutcomeCode.HISTORICAL_NOT_SUPPORTED],
        )

    other_university = next(
        (
            match.group(0)
            for match in _UNIVERSITY_RE.finditer(text)
            if not any(own in match.group(0) for own in _OWN_UNIVERSITY_NAMES)
        ),
        None,
    )
    if other_university or (_OUT_OF_SCOPE_RE.search(text) and not _SCHOOL_RE.search(text)):
        return GuardDecision(
            allowed=False,
            outcome=OutcomeCode.OUT_OF_SCOPE,
            message=_GUIDANCE[OutcomeCode.OUT_OF_SCOPE],
        )

    return GuardDecision(allowed=True)


def check_version_intent(
    question: str,
    profile: UserProfile | None,
) -> GuardDecision | None:
    """用户提供的入学年份与问题中的版本年份不一致时提前拒答。

    只处理“培养方案/版本/届”语境，避免把“2022 年奖学金标准”这类日期问题误判。
    """
    if profile is None or profile.intake_year is None:
        return None
    text = question.strip()
    if not _VERSION_CONTEXT_RE.search(text):
        return None
    years = sorted({int(year) for year in _VERSION_YEAR_RE.findall(text)})
    if not years or profile.intake_year in years:
        return None
    asked = "、".join(str(year) for year in years)
    return GuardDecision(
        allowed=False,
        outcome=OutcomeCode.VERSION_MISMATCH,
        message=(
            f"问题指向 {asked} 版，但你提供的是 {profile.intake_year} 级；"
            f"当前适用版本可能不同。请确认届别，或改问 {profile.intake_year} 版。"
        ),
    )
