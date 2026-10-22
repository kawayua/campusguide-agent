"""PolicyFilters -> Qdrant 过滤条件。

W2 只实现文章级硬过滤：块类型、状态、生效区间、受众与文档 ID。
学院/专业/年级的细粒度过滤留到 W4。
"""

from __future__ import annotations

from datetime import UTC, datetime, time

from qdrant_client.models import (
    DatetimeRange,
    FieldCondition,
    Filter,
    IsNullCondition,
    MatchAny,
)

from campusguide.domain.enums import Audience
from campusguide.domain.models import PolicyFilters


def _start_of_day(value) -> str:
    return datetime.combine(value, time.min, tzinfo=UTC).isoformat().replace("+00:00", "Z")


def _end_of_day(value) -> str:
    return datetime.combine(value, time.max, tzinfo=UTC).isoformat().replace("+00:00", "Z")


def build_qdrant_filter(filters: PolicyFilters) -> Filter:
    """把领域过滤条件翻译成 Qdrant Filter。

    ``effective_from <= as_of`` 且 ``effective_to is null or >= as_of``；
    ``effective_to`` 在 payload 中显式存 null，因此用 IsNullCondition 处理长期有效。
    """
    must: list = [
        FieldCondition(
            key="block_type",
            match=MatchAny(any=[block_type.value for block_type in filters.block_types]),
        ),
        FieldCondition(
            key="effective_from",
            range=DatetimeRange(lte=_end_of_day(filters.as_of)),
        ),
    ]

    if filters.status:
        must.append(
            FieldCondition(
                key="status",
                match=MatchAny(any=[status.value for status in filters.status]),
            )
        )

    must.append(
        Filter(
            should=[
                IsNullCondition(is_null={"key": "effective_to"}),
                FieldCondition(
                    key="effective_to",
                    range=DatetimeRange(gte=_start_of_day(filters.as_of)),
                ),
            ]
        )
    )

    if filters.audience:
        audiences = {audience.value for audience in filters.audience}
        audiences.add(Audience.ALL.value)
        must.append(
            FieldCondition(
                key="audience",
                match=MatchAny(any=sorted(audiences)),
            )
        )

    if filters.doc_ids:
        must.append(
            FieldCondition(
                key="doc_id",
                match=MatchAny(any=list(filters.doc_ids)),
            )
        )

    if filters.flow_ids:
        must.append(
            FieldCondition(
                key="flow_ids",
                match=MatchAny(any=list(filters.flow_ids)),
            )
        )

    return Filter(must=must)
