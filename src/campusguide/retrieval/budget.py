"""Shared conservative character estimate for evidence budgets (2 chars/token).

Include source headings, table headers (in body), and envelope overhead in both
retrieval layers. Original bodies are never concatenated or normalized for quoting.
"""

from __future__ import annotations

import math
from typing import Any


def evidence_cost(article: Any) -> int:
    return math.ceil(
        (
            len(article.text)
            + len(article.doc_title)
            + len(article.article_no)
            + sum(len(p) for p in article.chapter_path)
            + 160
        )
        / 2
    )
