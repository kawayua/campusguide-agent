from __future__ import annotations

from campusguide.retrieval.normalize import expand_query, load_aliases, normalize_query


def test_normalize_query_collapses_whitespace() -> None:
    assert normalize_query("  保研　需要  什么条件 ") == "保研 需要 什么条件"


def test_expand_query_adds_at_most_two_variants() -> None:
    aliases = {"保研": ["推免", "推荐免试", "免试攻读"]}
    queries = expand_query("保研需要什么条件", aliases, max_variants=2)

    assert queries[0] == "保研需要什么条件"
    assert len(queries) == 3
    assert "推免需要什么条件" in queries
    assert "推荐免试需要什么条件" in queries


def test_expand_query_keeps_original_when_no_alias() -> None:
    assert expand_query("学籍异动", {"休学": ["学籍异动"]}) == ["学籍异动"]


def test_load_aliases_from_repo_config() -> None:
    aliases = load_aliases("configs/aliases.yaml")
    assert "保研" in aliases
    assert "推免" in aliases["保研"]
