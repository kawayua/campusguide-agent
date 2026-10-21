"""联系方式与个人敏感信息脱敏。

规则（规格书 6.6）：
- 身份证号、手机号、学号、非本校个人邮箱进入索引前替换为占位符；
- 部门公开座机（如 020-8528xxxx）保留，因为它来自公开制度原文；
- 联系人姓名替换为占位符，保留“联系人”标签；
- 原文快照不修改，只在可索引文本上脱敏。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

ID_CARD_RE = re.compile(r"(?<!\d)\d{17}[\dXx](?!\d)")
MOBILE_RE = re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")
STUDENT_ID_RE = re.compile(
    r"((?:学号|学籍号)\s*[:：]?\s*)([A-Za-z0-9]{6,20})",
    re.IGNORECASE,
)
EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
# 只在“联系人：张三”这类冒号引导的字段值上脱敏。
# “负责人履行第一责任人”“项目负责人提出申请”是角色描述，不能误伤。
CONTACT_NAME_RE = re.compile(
    r"((?:联系人|负责人|经办人)\s*[:：]\s*)([\u4e00-\u9fff]{2,4}?)"
    r"(?=(?:同志|老师)?(?:[，。；、：！？,\s)）]|$))"
)
OFFICE_EMAIL_DOMAIN = "scau.edu.cn"


@dataclass
class RedactionResult:
    text: str
    counts: dict[str, int] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)

    @property
    def changed(self) -> bool:
        return bool(self.counts)


def _count(counts: dict[str, int], key: str, amount: int = 1) -> None:
    if amount:
        counts[key] = counts.get(key, 0) + amount


def _redact_email(match: re.Match[str]) -> str:
    email = match.group(0)
    domain = email.rsplit("@", 1)[-1].lower()
    if domain == OFFICE_EMAIL_DOMAIN or domain.endswith("." + OFFICE_EMAIL_DOMAIN):
        return email
    return "[个人邮箱已脱敏]"


def redact_text(text: str, *, redact_contacts: bool = True) -> RedactionResult:
    """脱敏可索引文本。

    ``redact_contacts=False`` 用于院系官网公开的教职工名录：手机号、邮箱和
    联系人姓名按原文保留（这些信息本意就是供师生联系），身份证号与学号
    仍然脱敏。配置项见 ``SourceDocument.redact_contacts``。
    """

    counts: dict[str, int] = {}
    warnings: list[str] = []

    text, id_count = ID_CARD_RE.subn("[身份证号已脱敏]", text)
    _count(counts, "id_card", id_count)

    def replace_student_id(match: re.Match[str]) -> str:
        _count(counts, "student_id")
        return f"{match.group(1)}[学号已脱敏]"

    text = STUDENT_ID_RE.sub(replace_student_id, text)

    if redact_contacts:
        text, mobile_count = MOBILE_RE.subn("[手机号已脱敏]", text)
        _count(counts, "mobile", mobile_count)

        def replace_email(match: re.Match[str]) -> str:
            redacted = _redact_email(match)
            if redacted != match.group(0):
                _count(counts, "personal_email")
            return redacted

        text = EMAIL_RE.sub(replace_email, text)

        def replace_contact_name(match: re.Match[str]) -> str:
            _count(counts, "contact_name")
            return f"{match.group(1)}[姓名已脱敏]"

        text = CONTACT_NAME_RE.sub(replace_contact_name, text)

    if counts:
        warnings.append(
            "已脱敏：" + ", ".join(f"{key}={value}" for key, value in sorted(counts.items()))
        )
    return RedactionResult(text=text, counts=counts, warnings=warnings)
