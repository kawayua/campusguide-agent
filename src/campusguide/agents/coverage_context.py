"""Typed student-population binding for coverage context frames.

A leading frame such as “我还是本科在校生” is scope, not an answered fact.
It still has to bind to the same population branch: an explicit opposing
enrolment status or academic level in the claim body wins over a broad
catalogue title, and a title naming both groups cannot lend either branch.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

ENROLLED = "enrolled"
COMPLETED = "completed"

# 硕士研究生/博士研究生 name one branch; the trailing 研究生 is the generic
# head of the same compound, not a second group. Department names such as
# 研究生院/本科生院 are recipients, not student populations.
_LEVEL = re.compile(
    r"硕士研究生|博士研究生|本科(?!生院)|研究生(?!院)|硕士|博士|专科"
)
_LEVEL_CANONICAL = {"硕士研究生": "硕士", "博士研究生": "博士"}
_ENROLLED = re.compile(
    r"在校生|在校(?!证明|证书)|在读(?!证明|证书)|在籍(?!证明|证书)|未毕业"
)
_COMPLETED = re.compile(
    r"(?<!未)毕业生|(?:已|已经)毕业|往届|(?<!未)毕业(?=学生)|毕业(?:后|以后|之后|了)"
)

_LEVEL_COMPAT = {
    "本科": frozenset({"本科"}),
    "研究生": frozenset({"研究生", "硕士", "博士"}),
    "硕士": frozenset({"硕士", "研究生"}),
    "博士": frozenset({"博士", "研究生"}),
    "专科": frozenset({"专科"}),
}

# A new branch starts after sentence punctuation, or after a comma when the
# next clause begins with a population marker ("在校生…，已毕业学生…").
_SEGMENT_BOUNDARY = re.compile(
    r"(?<=[。；;！？!?])"
    r"|(?<=[，,])(?=(?:在校|在读|在籍|未毕业|已毕业|毕业生|本科|研究生|硕士|博士|专科))"
)


@dataclass(frozen=True)
class Population:
    levels: frozenset[str] = frozenset()
    statuses: frozenset[str] = frozenset()

    @property
    def has_critical(self) -> bool:
        return bool(self.levels or self.statuses)

    @property
    def is_single_branch(self) -> bool:
        return len(self.levels) <= 1 and len(self.statuses) <= 1


def scan_population(text: str) -> Population:
    value = text or ""
    statuses: set[str] = set()
    if _ENROLLED.search(value):
        statuses.add(ENROLLED)
    if _COMPLETED.search(value):
        statuses.add(COMPLETED)
    return Population(
        levels=frozenset(
            _LEVEL_CANONICAL.get(level, level) for level in _LEVEL.findall(value)
        ),
        statuses=frozenset(statuses),
    )


def population_conflicts(required: Population, body: str, title: str) -> bool:
    """Whether the claim or its title explicitly contradicts the asked branch.

    The body is checked first: an explicit opposing enrolment status or level
    wins over a broad catalogue title. A title may explain an unmentioned
    dimension only when it is unambiguous; a title naming both groups cannot
    lend either branch. An unmentioned dimension is not itself a conflict.
    """
    if not required.has_critical or not required.is_single_branch:
        # A genuine comparison/context naming both groups is kept separate by
        # the existing unit/object gates instead of being collapsed here.
        return False
    body_population = scan_population(body)
    title_population = scan_population(title)
    return _dimension_conflicts(
        required.statuses,
        body_population.statuses,
        title_population.statuses,
        _status_compatible,
    ) or _dimension_conflicts(
        required.levels,
        body_population.levels,
        title_population.levels,
        _level_compatible,
    )


def _dimension_conflicts(
    required: frozenset[str],
    body: frozenset[str],
    title: frozenset[str],
    compatible,
) -> bool:
    if not required:
        return False
    if body:
        return not any(compatible(observed, wanted) for observed in body for wanted in required)
    if title:
        if len(title) > 1:
            return True
        return not any(compatible(observed, wanted) for observed in title for wanted in required)
    return False


def filter_population_branches(
    required: Population | None, content: str, title: str
) -> str:
    """Keep only claim branches that belong to the asked population."""
    if required is None or not required.has_critical:
        return content
    segments = [
        segment for segment in _SEGMENT_BOUNDARY.split(content or "") if segment.strip()
    ]
    if not segments:
        return content
    groups: list[list[str]] = []
    for segment in segments:
        # A continuation such as “；只出具在读证明” belongs to the preceding
        # explicitly marked branch instead of becoming an ambiguous clause.
        if not groups or scan_population(segment).has_critical:
            groups.append([segment])
        else:
            groups[-1].append(segment)
    claim_population = scan_population(content)
    title_population = scan_population(title)
    kept: list[str] = []
    for group in groups:
        branch = "".join(group)
        branch_population = scan_population(branch)
        if _branch_matches(
            required.statuses,
            branch_population.statuses,
            claim_population.statuses,
            title_population.statuses,
            _status_compatible,
        ) and _branch_matches(
            required.levels,
            branch_population.levels,
            claim_population.levels,
            title_population.levels,
            _level_compatible,
        ):
            kept.append(branch)
    return "".join(kept)


def _branch_matches(
    required: frozenset[str],
    branch: frozenset[str],
    claim: frozenset[str],
    title: frozenset[str],
    compatible,
) -> bool:
    if not required:
        return True
    if branch:
        return any(compatible(observed, wanted) for observed in branch for wanted in required)
    matched_claim = any(
        compatible(observed, wanted) for observed in claim for wanted in required
    )
    if matched_claim:
        # An unmarked clause inherits a single matching branch, but is
        # ambiguous when the same claim also carries the opposing branch.
        return all(
            any(compatible(observed, wanted) for wanted in required)
            for observed in claim
        )
    if claim:
        return False
    if title:
        if len(title) > 1:
            return False
        return any(compatible(observed, wanted) for observed in title for wanted in required)
    return True


def _status_compatible(observed: str, required: str) -> bool:
    return observed == required


def _level_compatible(observed: str, required: str) -> bool:
    return observed in _LEVEL_COMPAT.get(required, frozenset({required}))
