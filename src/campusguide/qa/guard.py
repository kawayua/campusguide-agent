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
    r"(?:19|20)\d{2}\s*年.{0,8}(?:版|版本|规定|办法|政策)"
)
_PRIVACY_RE = re.compile(
    r"(?:我|本人|我的|帮我查|帮我查询|查询我的).{0,24}"
    r"(?:学号|成绩|绩点|排名|身份证|手机号|联系方式|宿舍|一卡通|银行卡|密码)"
    r"|(?:学号|成绩|绩点|排名|身份证|手机号|宿舍|一卡通|银行卡).{0,8}(?:是多少|查询|查一下)"
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
