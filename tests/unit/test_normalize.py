from __future__ import annotations

from campusguide.retrieval.normalize import normalize_query


def test_normalize_query_full_width_and_spaces() -> None:
    assert normalize_query("  本科生　国家奖学金  ") == "本科生 国家奖学金"
