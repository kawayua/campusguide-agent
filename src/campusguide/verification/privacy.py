"""H10 输出隐私扫描：只记录类别，不记录原始隐私值。"""

from __future__ import annotations

import re

from campusguide.domain.enums import IssueScope, IssueSeverity
from campusguide.domain.models import VerificationIssue

_MOBILE_RE = re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")
_ID_CARD_RE = re.compile(
    r"(?<!\d)\d{6}(?:18|19|20)\d{2}(?:0[1-9]|1[0-2])"
    r"(?:0[1-9]|[12]\d|3[01])\d{3}[\dXx](?!\d)"
)
_STUDENT_ID_RE = re.compile(r"(?:学号|学籍号|学生证号)\s*[:：]?\s*[A-Za-z]?\d{6,}")
_EMAIL_RE = re.compile(r"(?<![\w.+-])([\w.+-]+@(?:[\w-]+\.)+[A-Za-z]{2,})(?![\w.-])")


def _official_email(email: str) -> bool:
    domain = email.rsplit("@", 1)[-1].lower()
    return domain == "scau.edu.cn" or domain.endswith(".scau.edu.cn")


def scan_privacy(text: str) -> list[str]:
    """返回命中的隐私类别，按类别排序；不返回命中的原始值。"""
    categories: set[str] = set()
    if _MOBILE_RE.search(text):
        categories.add("mobile")
    if _ID_CARD_RE.search(text):
        categories.add("id_card")
    if _STUDENT_ID_RE.search(text):
        categories.add("student_id")
    for match in _EMAIL_RE.finditer(text):
        if not _official_email(match.group(1)):
            categories.add("personal_email")
            break
    return sorted(categories)


def privacy_issue(claim_id: str, categories: list[str]) -> VerificationIssue:
    ordered = sorted(set(categories))
    return VerificationIssue(
        code="PRIVACY_VIOLATION",
        scope=IssueScope.CLAIM,
        severity=IssueSeverity.BLOCKING,
        claim_id=claim_id,
        detail="检测到输出中的个人信息：" + "、".join(ordered),
        facts=ordered,
    )
