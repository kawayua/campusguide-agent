from __future__ import annotations

from datetime import date

from qdrant_client.models import FieldCondition, Filter

from campusguide.domain.enums import ArticleStatus, Audience, BlockType
from campusguide.domain.models import PolicyFilters
from campusguide.retrieval.filters import build_qdrant_filter


def _keys(condition) -> set[str]:
    keys: set[str] = set()
    if isinstance(condition, FieldCondition):
        keys.add(condition.key)
    elif isinstance(condition, Filter):
        for item in condition.must or []:
            keys |= _keys(item)
        for item in condition.should or []:
            keys |= _keys(item)
    return keys


def test_filters_include_block_type_status_and_dates() -> None:
    result = build_qdrant_filter(
        PolicyFilters(
            block_types=[BlockType.ARTICLE],
            audience=[Audience.UNDERGRADUATE],
            as_of=date(2026, 10, 22),
        )
    )

    keys = _keys(result)
    assert {"block_type", "status", "effective_from", "effective_to", "audience"} <= keys

    block_condition = next(
        item
        for item in result.must
        if isinstance(item, FieldCondition) and item.key == "block_type"
    )
    assert block_condition.match.any == ["article"]

    audience_condition = next(
        item for item in result.must if isinstance(item, FieldCondition) and item.key == "audience"
    )
    assert set(audience_condition.match.any) == {"undergraduate", "all", "public"}


def test_filters_default_to_active_articles_and_skip_audience() -> None:
    result = build_qdrant_filter(PolicyFilters(as_of=date(2026, 10, 22)))
    keys = _keys(result)
    assert "audience" not in keys

    status_condition = next(
        item for item in result.must if isinstance(item, FieldCondition) and item.key == "status"
    )
    assert status_condition.match.any == [ArticleStatus.ACTIVE.value]


def test_filters_can_include_sections_and_doc_ids() -> None:
    result = build_qdrant_filter(
        PolicyFilters(
            block_types=[BlockType.ARTICLE, BlockType.SECTION],
            doc_ids=["demo-policy"],
            as_of=date(2026, 10, 22),
        )
    )
    block_condition = next(
        item
        for item in result.must
        if isinstance(item, FieldCondition) and item.key == "block_type"
    )
    assert set(block_condition.match.any) == {"article", "section"}
    assert "doc_id" in _keys(result)
