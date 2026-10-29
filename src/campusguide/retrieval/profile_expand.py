"""Profile-aware query expansion。

只在用户提供学院/专业/入学年份时，把组织注册表中的名称/别名追加为检索变体；
不改写原始问题，也不把用户字段当成硬过滤条件（硬过滤仍由 Qdrant filter 完成）。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from campusguide.config import OrganizationConfig
from campusguide.domain.profiles import UserProfile
from campusguide.retrieval.normalize import normalize_query


def _dedup_terms(terms: list[str]) -> tuple[str, ...]:
    cleaned: list[str] = []
    for term in terms:
        term = normalize_query(term)
        if term and term not in cleaned:
            cleaned.append(term)
    return tuple(cleaned)


@dataclass(frozen=True)
class ProfileLexicon:
    college_terms: dict[str, tuple[str, ...]]
    major_terms: dict[str, tuple[str, ...]]
    class_terms: dict[str, tuple[str, ...]] = field(default_factory=dict)
    class_levels: dict[str, str] = field(default_factory=dict)
    class_majors: dict[str, tuple[str, ...]] = field(default_factory=dict)

    @classmethod
    def from_organization(cls, organization: OrganizationConfig | None) -> ProfileLexicon:
        if organization is None:
            return cls(college_terms={}, major_terms={})
        class_types = list(getattr(organization, "class_types", []) or [])
        return cls(
            college_terms={
                college.college_id: _dedup_terms([college.name, *college.aliases])
                for college in organization.colleges
            },
            major_terms={
                major.major_id: _dedup_terms([major.name, *major.aliases])
                for major in organization.majors
            },
            class_terms={
                class_type.class_id: _dedup_terms([class_type.name, *class_type.aliases])
                for class_type in class_types
            },
            class_levels={
                class_type.class_id: class_type.level for class_type in class_types
            },
            class_majors={
                class_type.class_id: tuple(class_type.majors)
                for class_type in class_types
            },
        )

    @property
    def is_empty(self) -> bool:
        return not (self.college_terms or self.major_terms or self.class_terms)

    def _first_missing(self, terms: tuple[str, ...], query: str) -> str | None:
        for term in terms:
            if term and term not in query:
                return term
        return None

    def query_variants(
        self,
        query: str,
        profile: UserProfile | None,
        *,
        max_variants: int = 2,
    ) -> list[str]:
        if profile is None or profile.is_empty or self.is_empty or max_variants <= 0:
            return []
        base = normalize_query(query)
        if not base:
            return []
        variants: list[str] = []

        def add(term: str | None) -> None:
            if not term:
                return
            candidate = normalize_query(f"{base} {term}")
            if candidate and candidate != base and candidate not in variants:
                variants.append(candidate)

        # 入学年份优先：同一专业/学院的不同届别培养方案主要靠它区分。
        if profile.intake_year is not None:
            add(f"{profile.intake_year}级")
        for major_id in profile.majors:
            add(self._first_missing(self.major_terms.get(major_id, ()), base))
        for college_id in profile.colleges:
            add(self._first_missing(self.college_terms.get(college_id, ()), base))
        return variants[:max_variants]
