"""Aggregate-level checks for credits/hours (R5 / 报告 A5 补充).

A module subtotal (“选修课程37学分”) must not be presented as the graduation
total (“毕业总学分163学分”). Facts extraction compares numbers pairwise and
cannot tell the levels apart, so this module compares each stated total against
the source's own total and its module breakdown.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

_TOTAL_RE = re.compile(
    r"(?:毕业\s*(?:应取得|需取得|要求)?\s*总?学分|总学分)[^。；;\n]{0,20}?"
    r"(?:为|是|达到|共计|共)?\s*(\d+(?:\.\d+)?)\s*学分"
)
_MODULE_RE = re.compile(
    r"(?P<name>(?:必修|选修|实践|通识|专业|理论|实验)[^。；;，,\n]{0,8}?课程"
    r"|实践(?:教学)?环节|集中实践)"
    r"[^。；;\n]{0,20}?(?:为|是|共|计|应取得|取得)?\s*(\d+(?:\.\d+)?)\s*学分"
)


@dataclass(frozen=True)
class AggregateFinding:
    kind: str
    detail: str


def stated_total(text: str) -> float | None:
    match = _TOTAL_RE.search(text)
    return float(match.group(1)) if match else None


def module_totals(text: str) -> dict[str, float]:
    totals: dict[str, float] = {}
    for match in _MODULE_RE.finditer(text):
        name = re.sub(r"\s+", "", match.group("name"))
        totals.setdefault(name, float(match.group(2)))
    return totals


def check_aggregate_level(claim_text: str, quote_texts: list[str]) -> list[AggregateFinding]:
    """Flag a module subtotal stated as the graduation total."""
    claimed = stated_total(claim_text)
    if claimed is None:
        return []
    findings: list[AggregateFinding] = []
    for quote in quote_texts:
        quote_total = stated_total(quote)
        modules = module_totals(quote)
        if quote_total is None or quote_total == claimed or not modules:
            continue
        matched = [name for name, value in modules.items() if value == claimed]
        if not matched:
            continue
        findings.append(
            AggregateFinding(
                kind="aggregate_level",
                detail=(
                    f"数值层级混用：结论把“{'、'.join(matched)}”的 {claimed:g} 学分"
                    f"写成毕业总学分，原文毕业总学分为 {quote_total:g} 学分"
                ),
            )
        )
        break
    return findings


__all__ = ["AggregateFinding", "check_aggregate_level", "module_totals", "stated_total"]