"""W2 最小确定性 guard。

只拦截明确的空请求、历史版本、隐私和校外问题；宁可漏拦，也不把本校问题
误判为校外。完整拒答判定树在 W3。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from campusguide.domain.enums import OutcomeCode

MAX_QUERY_CHARS = 300

_HISTORICAL_RE = re.compile(
    r"去年|前年|旧版|老版|历史版本|以前版本|废止|当时(?:的|有效)|"
    r"(?:19|20)\d{2}\s*年.{0,8}(?:版|版本|规定|办法|政策)"
)
_PRIVACY_RE = re.compile(
    r"(?:我|本人|我的|帮我查|帮我查询|查询我的).{0,24}"
    r"(?:学号|成绩|绩点|排名|身份证|手机号|联系方式|宿舍|一卡通|银行卡|密码)"
    r"|(?:学号|成绩|绩点|排名|身份证|手机号|宿舍|一卡通|银行卡).{0,8}(?:是多少|查询|查一下)"
)
_OUT_OF_SCOPE_RE = re.compile(r"隔壁学校|其他学校|外校|别的学校|另一所学校")
_SCHOOL_RE = re.compile(r"华南农业大学|华农|SCAU", re.IGNORECASE)

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


def check_guard(question: str, *, max_chars: int = MAX_QUERY_CHARS) -> GuardDecision:
    text = question.strip()
    if not text or len(text) > max_chars:
        return GuardDecision(
            allowed=False,
            outcome=OutcomeCode.INVALID_REQUEST,
            message=_GUIDANCE[OutcomeCode.INVALID_REQUEST],
        )

    if _PRIVACY_RE.search(text):
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

    if _OUT_OF_SCOPE_RE.search(text) and not _SCHOOL_RE.search(text):
        return GuardDecision(
            allowed=False,
            outcome=OutcomeCode.OUT_OF_SCOPE,
            message=_GUIDANCE[OutcomeCode.OUT_OF_SCOPE],
        )

    return GuardDecision(allowed=True)
