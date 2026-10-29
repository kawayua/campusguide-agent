"""Bounded, credential-free exception summaries for diagnostics."""

from __future__ import annotations

import re
from urllib.parse import urlsplit, urlunsplit

from campusguide.observability.redact import redact_sensitive


def safe_error_summary(error: BaseException) -> str:
    def clean_url(match: re.Match[str]) -> str:
        url = urlsplit(match.group())
        return urlunsplit((url.scheme, url.netloc.rsplit("@", 1)[-1], url.path, "", ""))

    text = re.sub(r"https?://[^\s\"'<>]+", clean_url, str(error))
    text = re.sub(
        r"(?i)(api[_-]?key|authorization|bearer|token|secret|password)\s*[:= ]+[^\s,;]+",
        r"\1=[REDACTED]",
        text,
    )
    return redact_sensitive(text, max_chars=240)
