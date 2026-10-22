"""查询规范化与可审计的别名扩展。

别名只扩展检索文本，不改变用户原始问题；每个问题最多生成
``max_variants`` 个检索变体，避免一次查询退化成大量噪声请求。
"""

from __future__ import annotations

import unicodedata
from pathlib import Path

import yaml

from campusguide.domain.text import normalize_text

__all__ = [
    "DEFAULT_ALIASES_PATH",
    "expand_query",
    "load_aliases",
    "normalize_query",
    "normalize_text",
]

DEFAULT_ALIASES_PATH = Path("configs/aliases.yaml")
DEFAULT_MAX_VARIANTS = 2


def normalize_query(query: str) -> str:
    """统一全半角、去首尾空白、压缩内部空白；保留原始大小写语义。"""
    query = unicodedata.normalize("NFKC", query).replace("\u00a0", " ")
    return " ".join(query.split())


def load_aliases(path: str | Path = DEFAULT_ALIASES_PATH) -> dict[str, list[str]]:
    """读取别名表；同一个 key 的候选词保持配置顺序。"""
    path = Path(path)
    if not path.exists():
        return {}
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        raise ValueError(f"别名表顶层必须是对象：{path}")
    aliases = raw.get("aliases", raw)
    if not isinstance(aliases, dict):
        raise ValueError(f"aliases 必须是对象：{path}")

    cleaned: dict[str, list[str]] = {}
    for key, values in aliases.items():
        if not isinstance(key, str) or not key.strip():
            raise ValueError(f"别名 key 必须是非空字符串：{key!r}")
        if isinstance(values, str):
            values = [values]
        if not isinstance(values, list) or not all(isinstance(item, str) for item in values):
            raise ValueError(f"别名 {key} 的值必须是字符串列表")
        cleaned[normalize_query(key)] = [
            normalize_query(item) for item in values if normalize_query(item)
        ]
    return cleaned


def expand_query(
    query: str,
    aliases: dict[str, list[str]] | None = None,
    *,
    max_variants: int = DEFAULT_MAX_VARIANTS,
) -> list[str]:
    """返回去重后的检索查询列表，第一项永远是规范化后的原始问题。"""
    normalized = normalize_query(query)
    if not normalized:
        return []
    queries = [normalized]
    if max_variants <= 0 or not aliases:
        return queries

    variants: list[str] = []
    for term, replacements in aliases.items():
        if term not in normalized:
            continue
        for replacement in replacements:
            if replacement == term:
                continue
            variant = normalize_query(normalized.replace(term, replacement))
            if variant and variant not in queries and variant not in variants:
                variants.append(variant)
            if len(variants) >= max_variants:
                return queries + variants
    return queries + variants
