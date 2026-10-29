"""Typed entity lists from continuous Chinese text (R1/A2).

The 098 corpus page is a single unpunctuated run of names:

    "基于云平台的计算机实验教学示范中心信息数学与应用统计实验教学示范中心
     数学建模实验教学示范中心计算机软件实验室……"

Regexy "category: items" parsing cannot tell the three demonstration centres from
the ordinary laboratories that follow. This module splits the run at entity-type
suffixes (示范中心 / 实验室 / 中心), keeps connectors such as 与 inside a name, and
tags each entry with its own type so membership can be judged per item.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# longest first so 实验教学示范中心 wins over 示范中心 and 中心
TYPE_WORDS: tuple[str, ...] = (
    "国家级实验教学示范中心",
    "省级实验教学示范中心",
    "实验教学示范中心",
    "示范中心",
    "重点实验室",
    "实验室",
    "研究中心",
    "工程中心",
)

# Entries longer than this are not a name; stop the backward expansion.
_MAX_NAME = 40
_BOUNDARY = "，,。；;：:、\n（）()【】[]"


@dataclass(frozen=True)
class TypedEntity:
    name: str
    type_word: str
    start: int
    end: int

    @property
    def is_demonstration_centre(self) -> bool:
        return self.type_word.endswith("示范中心")


def _strip_name(text: str) -> str:
    name = text.strip().lstrip("的、，,：: ")
    name = re.sub(r"^(?:以及|还有|其中|另外|另有|另设|包括|设有|现有|等)", "", name)
    name = re.sub(r"(?:等|等等)$", "", name)
    return name.strip()


def typed_entities(text: str) -> list[TypedEntity]:
    """Split a name run into typed entities, preserving 与 inside names."""
    entities: list[TypedEntity] = []
    cursor = 0
    index = 0
    while index < len(text):
        matched = None
        for type_word in TYPE_WORDS:
            if text.startswith(type_word, index):
                matched = type_word
                break
        if matched is None:
            index += 1
            continue
        end = index + len(matched)
        name = _strip_name(text[cursor:end])
        # A name must contain more than the type word itself.
        if len(name) > len(matched) and len(name) <= _MAX_NAME:
            entities.append(
                TypedEntity(
                    name=name,
                    type_word=matched,
                    start=cursor,
                    end=end,
                )
            )
            cursor = end
            index = end
            continue
        # Bare type word ("中心" inside another name): keep scanning without
        # starting a new entry so the surrounding name is not truncated.
        index = end
    return entities


def _split_candidates(text: str) -> list[str]:
    parts = re.split(r"[、,，；;。\n]", text)
    return [part.strip() for part in parts if part.strip()]


def demo_centre_items(claim_text: str) -> list[str]:
    """Items the claim files under the demonstration-centre heading."""
    head = re.search(r"(?:实验教学)?示范中心", claim_text)
    if head is None:
        return []
    tail = claim_text[head.end() :]
    stop = re.search(r"[。；;\n]|另设|另有|此外|其他|其余|以及|还有", tail)
    if stop:
        tail = tail[: stop.start()]
    listed = tail.split("等")[0]
    items: list[str] = []
    for candidate in _split_candidates(listed):
        item = re.sub(r"^(?:包括|有|分别是|：|:)", "", candidate).strip()
        if len(item) >= 4:
            items.append(item)
    return items


def asked_types(question: str) -> list[str]:
    """Entity types the question actually asks about, longest match first."""
    found: list[str] = []
    for type_word in TYPE_WORDS:
        if type_word in question and not any(
            type_word in existing and existing != type_word for existing in found
        ):
            found.append(type_word)
    # 实验室/示范中心同时出现时，问题问的是“示范中心里有哪些实验室”这类组合，
    # 两种类型都要保留；只把被更长类型覆盖的短类型去掉。
    return [type_word for type_word in found if type_word in question]


def items_of_type(text: str, type_word: str) -> list[str]:
    """Names in ``text`` whose own type word is exactly ``type_word``."""
    names: list[str] = []
    for entity in typed_entities(text):
        if entity.type_word == type_word and entity.name not in names:
            names.append(entity.name)
    return names


def type_span(text: str, type_word: str) -> tuple[int, int] | None:
    """Start/end offsets covering all names of ``type_word``, for a verbatim quote."""
    hits = [entity for entity in typed_entities(text) if entity.type_word == type_word]
    if not hits:
        return None
    return hits[0].start, hits[-1].end


def category_violations(claim_text: str, quote_text: str) -> list[str]:
    """Claim items filed as demonstration centres that the source types otherwise."""
    items = demo_centre_items(claim_text)
    if not items:
        return []
    typed = typed_entities(quote_text)
    if len(typed) < 3:
        return []
    by_name = {entity.name: entity for entity in typed}
    violations: list[str] = []
    for item in items:
        entity = by_name.get(item)
        if entity is None:
            # tolerate a prefix/suffix difference such as "计算机软件实验室"
            entity = next(
                (
                    candidate
                    for name, candidate in by_name.items()
                    if item in name or name in item
                ),
                None,
            )
        if entity is not None and not entity.is_demonstration_centre:
            violations.append(f"{entity.name}（{entity.type_word}）")
    return violations


__all__ = [
    "TYPE_WORDS",
    "TypedEntity",
    "asked_types",
    "category_violations",
    "demo_centre_items",
    "items_of_type",
    "typed_entities",
    "type_span",
]