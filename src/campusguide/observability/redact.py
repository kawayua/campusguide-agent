"""日志与 trace 脱敏。"""

from __future__ import annotations

from campusguide.ingest.redact import redact_text

MAX_TRACE_QUESTION_CHARS = 300


def redact_sensitive(text: str, *, max_chars: int = MAX_TRACE_QUESTION_CHARS) -> str:
    """脱敏手机号、身份证、学号、个人邮箱、联系人姓名，并限制长度。"""
    cleaned = redact_text(text, redact_contacts=True).text
    cleaned = " ".join(cleaned.split())
    if len(cleaned) > max_chars:
        return cleaned[:max_chars] + "…"
    return cleaned
