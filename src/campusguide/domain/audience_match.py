"""Audience hierarchy so a document's stated audience can satisfy a related role.

A regulation tagged ``graduate_master`` applies to a question about graduate
students, and a ``graduate`` tag covers both master and doctor students. The same
rule must hold in the Qdrant filter, the scope filter and the rerank warning,
otherwise检索 and核对 disagree about the same document (report R2).
"""

from __future__ import annotations

from campusguide.domain.enums import Audience

# role -> audiences that also satisfy it
_ASSIGNABLE: dict[Audience, frozenset[Audience]] = {
    Audience.GRADUATE: frozenset({Audience.GRADUATE_MASTER, Audience.GRADUATE_DOCTOR}),
    Audience.GRADUATE_MASTER: frozenset({Audience.GRADUATE}),
    Audience.GRADUATE_DOCTOR: frozenset({Audience.GRADUATE}),
    Audience.FACULTY: frozenset({Audience.STAFF}),
    Audience.STAFF: frozenset({Audience.FACULTY}),
}

_BROAD = frozenset({Audience.ALL, Audience.PUBLIC})


def expand_audience(targets: list[Audience] | tuple[Audience, ...]) -> list[Audience]:
    """Audience values that a document may carry and still apply to ``targets``."""
    expanded: list[Audience] = []
    for target in targets:
        if target is Audience.ALL:
            return [Audience.ALL]
        expanded.append(target)
        expanded.extend(sorted(_ASSIGNABLE.get(target, frozenset()), key=str))
    return list(dict.fromkeys(expanded))


def audience_matches(article_audiences: set[Audience], target: Audience) -> bool:
    """True when a document tagged ``article_audiences`` applies to ``target``."""
    if not article_audiences:
        return True
    if article_audiences & _BROAD or target in article_audiences:
        return True
    if target is Audience.ALL:
        return True
    return bool(article_audiences & _ASSIGNABLE.get(target, frozenset()))


__all__ = ["audience_matches", "expand_audience"]