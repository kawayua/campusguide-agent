"""Trusted source metadata, resolved against the local organization registry."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from campusguide.config import load_organization
from campusguide.domain.models import Article

_REGISTRY = Path(__file__).resolve().parents[3] / "configs" / "organization.yaml"


@lru_cache(maxsize=2)
def _labels(stamp: tuple[int, int]) -> tuple[dict[str, str], dict[str, str]]:
    registry = load_organization(_REGISTRY)
    return (
        {c.college_id: c.name for c in registry.colleges},
        {m.major_id: m.name for m in registry.majors},
    )


def trusted_context(article: Article) -> dict:
    detail = article.audience_detail
    colleges: dict[str, str] = {}
    majors: dict[str, str] = {}
    if _REGISTRY.exists():
        stat = _REGISTRY.stat()
        colleges, majors = _labels((stat.st_mtime_ns, stat.st_size))
    return {
        "source_title": article.doc_title,
        "source_section": article.article_no,
        "chapter_path": article.chapter_path,
        "colleges": [colleges.get(key, key) for key in detail.colleges] if detail else [],
        "majors": [majors.get(key, key) for key in detail.majors] if detail else [],
        "intake_years": detail.intake_years if detail else [],
        "version_id": article.version_id,
        "use_boundary": "仅解释来源主体、名单归属、适用对象与表头；不得补实质金额、期限、角色或成员事实",
    }
