"""Section-aware 证据选择。

培养方案里的“教学计划进程表/课程结构/矩阵”通常是表格块，宽问题下会挤占
“培养目标/培养要求/毕业要求”等叙事 section，并显著增加 LLM JSON 失败率。
本模块只调整最终证据顺序与表格数量，不改检索/核对契约。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Protocol

from campusguide.domain.enums import BlockType

TABLE_INTENT_RE = re.compile(
    r"课程|学分|学时|进程|计划|安排|周|学期|矩阵|课程代码|课程名称|课程设置"
    r"|科目|选修组|方向选修|选修模块"
)
SECTION_INTENT_RE = re.compile(
    r"培养方案|培养目标|毕业要求|专业核心课程|课程结构|教学计划|进程表"
    r"|课程|学分|学时|科目|选修组|选修模块|方向选修"
)
JOB_INTENT_RE = re.compile(r"就业|职业|去向|工作方向|毕业后|从事.{0,12}工作|工作方面")
JOB_TEXT_RE = re.compile(
    r"(?:可在|可到|能够|能在)[^。；\n]{0,40}(?:从事|担任)[^。；\n]{0,40}(?:工作|岗位)"
)
NARRATIVE_MARKERS = (
    "培养目标",
    "培养要求",
    "毕业要求",
    "专业核心课程",
    "主干学科",
    "专业介绍",
    "学制",
    "学位",
    "基本要求",
)

_TABLE_MARKERS = ("课程代码", "课程名称", "开课单位", "学分", "学时", "周学时")


class SectionCandidate(Protocol):
    article: Any
    rerank_score: float | None


@dataclass
class SectionSelection:
    candidates: list[Any]
    warnings: list[str]


def is_table_like(article: Any) -> bool:
    if article.block_type is not BlockType.SECTION:
        return False
    text = article.text or ""
    if not text:
        return False
    pipe_count = text.count("|") + text.count("\t")
    digital = sum(1 for char in text if char.isdigit())
    digit_ratio = digital / max(len(text), 1)
    lines = [line for line in text.splitlines() if line.strip()]
    table_lines = sum(1 for line in lines if "|" in line or "\t" in line)
    if pipe_count >= 4:
        return True
    if table_lines >= 2 and digit_ratio > 0.15:
        return True
    if digit_ratio > 0.30 and len(lines) >= 3:
        return True
    return any(marker in text[:200] for marker in _TABLE_MARKERS) and digit_ratio > 0.20


def is_narrative_section(article: Any) -> bool:
    if article.block_type is not BlockType.SECTION:
        return False
    heading = f"{article.article_no} {article.text[:80]}"
    return any(marker in heading for marker in NARRATIVE_MARKERS)


def selection_bonus(article: Any, *, query: str = "") -> float:
    bonus = 0.0
    if is_narrative_section(article):
        bonus += 0.02
    if is_table_like(article) and not allows_tables(query):
        bonus -= 0.04
    if query and JOB_INTENT_RE.search(query) and JOB_TEXT_RE.search(article.text or ""):
        # 培养方案问“毕业后可从事的工作”时，把“人才培养目标/就业去向”
        # 一类的 section 提到科研成果要求之前，避免只答研究要求。
        bonus += 0.03
    return bonus


def allows_tables(query: str) -> bool:
    return bool(TABLE_INTENT_RE.search(query))


def allows_sections(query: str) -> bool:
    """课程/培养方案类问题需要 section 级证据；其他问题默认仍只查条款。"""
    return bool(SECTION_INTENT_RE.search(query))


def select_sections(
    candidates: list[Any],
    *,
    query: str,
    limit: int,
    max_table_sections: int = 2,
) -> SectionSelection:
    """按 rerank 分数 + section 类型重排，并限制表格 section 数量。"""
    if limit <= 0 or not candidates:
        return SectionSelection(candidates=[], warnings=[])
    table_allowed = allows_tables(query)
    table_limit = limit if table_allowed else max(0, max_table_sections)

    ordered = sorted(
        candidates,
        key=lambda item: (item.rerank_score or 0.0) + selection_bonus(item.article, query=query),
        reverse=True,
    )
    selected: list[Any] = []
    warnings: list[str] = []
    tables = 0
    for item in ordered:
        table_like = is_table_like(item.article)
        if table_like and tables >= table_limit:
            warnings.append(f"TABLE_SECTION_DEPRIORITIZED: {item.article.article_id}")
            continue
        selected.append(item)
        if table_like:
            tables += 1
        if len(selected) >= limit:
            break
    return SectionSelection(candidates=selected, warnings=warnings)
