"""Bounded, grounded views of multi-aspect queries for semantic reranking."""

from __future__ import annotations

import re

# These describe request facets, never policy facts or answers.
_FACETS = (
    (r"报名|条件|资格", "申请条件"),
    (r"区别|定义|是什么", "定义"),
    (r"证书|结业|完成后", "证书"),
    (r"实践课程|实践环节|集中实践|实践教学", "实践课程"),
    (r"核心课程", "核心课程"),
    (r"材料", "申请材料"),
    (r"程序|流程|办理", "办理程序"),
    (r"考场|考试纪律", "考场规定"),
    (r"处分|处罚", "纪律处分"),
)


def rerank_query_views(query: str, *, limit: int = 3) -> list[str]:
    views = [query]
    marker = re.search(
        r"有什么|有多少|有哪些|什么|需要|如何|怎样|怎么|的(?:报名|申请|核心|实践|毕业)|[，,？?]",
        query,
    )
    topic = query[: marker.start()] if marker else ""
    topic = re.sub(r"^(?:请问|请|对于|关于)", "", topic).strip(" 的：:")
    if not topic or len(topic) > 45:
        return views
    # A single-aspect query already has the right granularity.
    facets = [label for pattern, label in _FACETS if re.search(pattern, query)]
    if len(facets) < 2:
        return views
    for label in facets:
        view = f"{topic} {label}"
        if view not in views:
            views.append(view)
        if len(views) == limit:
            break
    return views
