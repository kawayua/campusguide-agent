"""Deterministic required list items from explicit source lists and course rows."""

from __future__ import annotations

import re

from campusguide.agents.plan import QuestionUnit
from campusguide.domain.models import Evidence
from campusguide.domain.source_context import trusted_context
from campusguide.domain.text import normalize_text
from campusguide.retrieval.source_spans import source_spans


_LIST_SPLIT = re.compile(r"[、，,；;。]")
_ITEM_NOISE = re.compile(
    r"最为核心|最核心|门左右|门以上|门以内|不少于\d+门|\d+\s*[至到~-]\s*\d+\s*门|等$|^重要$|^核心$|^主要$|^基本$"
)


def _clean_item(label: str) -> str:
    label = label.strip(" 。\n\t·•●○-—、）)（(")
    label = re.sub(r"^[（(]?\d{1,2}[）)、.]\s*", "", label)
    label = re.sub(r"[（(][^）)]*[）)]", "", label).strip()
    return label


def required_course_items(unit: QuestionUnit, evidence: list[Evidence]) -> list[str]:
    aspects = set(unit.requested_aspects)
    if not aspects & {"核心课程", "实践课程", "选修课程"}:
        return []
    scoped = []
    for item in evidence:
        article = item.article
        detail = article.audience_detail
        context = trusted_context(article)
        scope = " ".join(
            [article.doc_title, *article.chapter_path, *context["majors"], *context["colleges"]]
        )
        if unit.subject and unit.subject not in scope:
            continue
        if (
            unit.cohort
            and detail
            and detail.intake_years
            and unit.cohort not in detail.intake_years
        ):
            continue
        scoped.append(item)
    required = []
    for item in scoped:
        article = item.article
        if "核心课程" in aspects and "核心课程" in article.article_no:
            text = re.sub(r"[。；;]\s*$", "", article.text.strip())
            text = re.sub(r"^.*?(?:包括|为|：|:)", "", text)
            items = _LIST_SPLIT.split(text)
            if len(items) >= 2:
                for label in items:
                    # 中文句号后的说明句（“最为核心的5至8门…”“等”）不是课程名。
                    if _ITEM_NOISE.search(label):
                        continue
                    label = _clean_item(label)
                    if 2 <= len(label) <= 35 and not re.search(
                        r"学分|要求|应当|本专业|培养|环节|模块", label
                    ):
                        if re.fullmatch(r"(?:专业)?(?:核心|选修|实践|通识)?课程(?:设置|安排)?", label):
                            continue
                        required.append(label)
    for row in source_spans(scoped).values():
        if "_row_" not in row.span_id:
            continue
        # 支撑矩阵的行说明课程对毕业要求的支撑关系，不是开设课程清单。
        if row.is_matrix:
            continue
        label = row.label.strip()
        if _ITEM_NOISE.search(label):
            continue
        if "实践课程" in aspects:
            practical = bool(re.search(r"实验|实习|实践|毕业(?:论文|设计)|训练|劳动教育", label))
            if "集中实践" in unit.original_span and "实验" in label:
                practical = False
            if practical:
                required.append(label)
        if "选修课程" in aspects and row.module:
            module = re.sub(r"方向$|选修模块$|模块$", "", row.module).strip()
            if (
                len(module) >= 2
                and module in unit.original_span
                and ("实验" not in label or "实验" in unit.original_span)
            ):
                required.append(label)
    return list(dict.fromkeys(required))


def missing_course_items(
    unit: QuestionUnit, evidence: list[Evidence], answer_text: str
) -> list[str]:
    normalized = normalize_text(answer_text)
    return [
        item
        for item in required_course_items(unit, evidence)
        if normalize_text(item) not in normalized
    ]
