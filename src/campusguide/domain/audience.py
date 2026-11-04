"""适用人群的细粒度约束。

`audience` 角色回答“谁可以依据文件办事”，本模块只描述角色之外的
年级、阶段、培养类型和特定群体限制。所有字段都采用“空 = 不限制”
的语义；同字段多值为 OR，不同字段之间为 AND。
"""

from __future__ import annotations

import re

from pydantic import BaseModel, Field, field_validator, model_validator

from campusguide.domain.enums import (
    Audience,
    AudienceStage,
    DegreeType,
    LearningMode,
    ProgramType,
    ScopeStatus,
    StudentGroup,
    StudentStatus,
)

Slug = re.compile(r"^[a-z0-9][a-z0-9-]*$")
Cycle = re.compile(r"^\d{4}(?:-\d{4})?$")
MIN_YEAR = 1980
MAX_YEAR = 2100

STUDENT_AUDIENCES = frozenset(
    {
        Audience.UNDERGRADUATE,
        Audience.GRADUATE,
        Audience.GRADUATE_MASTER,
        Audience.GRADUATE_DOCTOR,
    }
)


def has_student_audience(audience: list[Audience]) -> bool:
    """ALL 视为未细分角色，允许携带学生约束。"""
    return Audience.ALL in audience or bool(STUDENT_AUDIENCES.intersection(audience))


class AudienceScope(BaseModel):
    """合订本中按章节原文收窄适用专业/班型；必须附原文依据。"""

    match: str
    majors: list[str] = Field(default_factory=list)
    class_types: list[str] = Field(default_factory=list)
    evidence: str

    @field_validator("majors", "class_types")
    @classmethod
    def _validate_scope_slugs(cls, value: list[str]) -> list[str]:
        for item in value:
            if not Slug.match(item):
                raise ValueError(f"章节适用范围 ID 必须是小写 slug：{item!r}")
        if len(value) != len(set(value)):
            raise ValueError("章节适用范围 ID 不能重复")
        return value

    @model_validator(mode="after")
    def _validate_scope(self) -> AudienceScope:
        if not self.match.strip():
            raise ValueError("章节适用范围必须提供章节匹配文本")
        if not (self.majors or self.class_types):
            raise ValueError("章节适用范围必须至少收窄专业或班型")
        if not self.evidence.strip():
            raise ValueError("章节适用范围必须提供原文依据")
        return self


class AudienceDetail(BaseModel):
    """角色之外的适用范围；不填表示不限制。"""

    colleges: list[str] = Field(default_factory=list)
    majors: list[str] = Field(default_factory=list)
    intake_years: list[int] = Field(default_factory=list)
    intake_from: int | None = None
    graduating_years: list[int] = Field(default_factory=list)
    admission_year: int | None = None
    stages: list[AudienceStage] = Field(default_factory=list)
    program_types: list[ProgramType] = Field(default_factory=list)
    learning_modes: list[LearningMode] = Field(default_factory=list)
    degree_types: list[DegreeType] = Field(default_factory=list)
    groups: list[StudentGroup] = Field(default_factory=list)
    status: list[StudentStatus] = Field(default_factory=list)
    class_types: list[str] = Field(default_factory=list)
    cycle: str | None = None
    scope_status: ScopeStatus = ScopeStatus.UNKNOWN
    scope_evidence: str | None = None
    note: str | None = None

    @field_validator("colleges", "majors")
    @classmethod
    def _validate_slugs(cls, value: list[str]) -> list[str]:
        for item in value:
            if not Slug.match(item):
                raise ValueError(f"学院/专业 ID 必须是小写 slug：{item!r}")
        if len(value) != len(set(value)):
            raise ValueError("学院/专业 ID 不能重复")
        return value

    @field_validator("class_types")
    @classmethod
    def _validate_class_types(cls, value: list[str]) -> list[str]:
        for item in value:
            if not Slug.match(item):
                raise ValueError(f"班型 ID 必须是小写 slug：{item!r}")
        if len(value) != len(set(value)):
            raise ValueError("班型 ID 不能重复")
        return value

    @field_validator("admission_year")
    @classmethod
    def _validate_admission_year(cls, value: int | None) -> int | None:
        if value is not None and not MIN_YEAR <= value <= MAX_YEAR:
            raise ValueError(f"招生年度超出合理范围：{value}")
        return value

    @field_validator("intake_years", "graduating_years")
    @classmethod
    def _validate_years(cls, value: list[int]) -> list[int]:
        for year in value:
            if not MIN_YEAR <= year <= MAX_YEAR:
                raise ValueError(f"年份超出合理范围：{year}")
        if len(value) != len(set(value)):
            raise ValueError("年份不能重复")
        return value

    @field_validator("intake_from")
    @classmethod
    def _validate_intake_from(cls, value: int | None) -> int | None:
        if value is not None and not MIN_YEAR <= value <= MAX_YEAR:
            raise ValueError(f"入学年份超出合理范围：{value}")
        return value

    @field_validator("cycle")
    @classmethod
    def _validate_cycle(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if not Cycle.match(value):
            raise ValueError("cycle 必须是 2025 或 2025-2026 形式")
        if "-" in value:
            start, end = (int(part) for part in value.split("-"))
            if end < start:
                raise ValueError("cycle 的结束年份不能早于开始年份")
        return value

    @model_validator(mode="after")
    def _validate_cohort_rule(self) -> AudienceDetail:
        if self.intake_years and self.intake_from is not None:
            raise ValueError("intake_years 与 intake_from 不能同时填写")
        if self.scope_status is ScopeStatus.EXACT and not (self.scope_evidence or "").strip():
            raise ValueError("scope_status=exact 必须提供 scope_evidence 原文依据")
        if self.scope_status is ScopeStatus.UNRESTRICTED and self.has_scope_constraints:
            raise ValueError("scope_status=unrestricted 不能同时携带适用范围限制")
        return self

    @property
    def has_scope_constraints(self) -> bool:
        return any(
            (
                self.colleges,
                self.majors,
                self.intake_years,
                self.intake_from is not None,
                self.graduating_years,
                self.admission_year is not None,
                self.stages,
                self.program_types,
                self.learning_modes,
                self.degree_types,
                self.groups,
                self.status,
                self.class_types,
                self.cycle,
            )
        )

    @property
    def has_student_constraints(self) -> bool:
        """是否包含只对在读学生有意义的约束。"""
        return any(
            (
                self.intake_years,
                self.intake_from is not None,
                self.graduating_years,
                self.stages,
                self.program_types,
                self.learning_modes,
                self.degree_types,
                self.groups,
                self.status,
                self.class_types,
            )
        )

    @property
    def has_org_constraints(self) -> bool:
        return bool(self.colleges or self.majors)

    @property
    def is_empty(self) -> bool:
        return not any(
            (
                self.colleges,
                self.majors,
                self.intake_years,
                self.intake_from is not None,
                self.graduating_years,
                self.admission_year is not None,
                self.stages,
                self.program_types,
                self.learning_modes,
                self.degree_types,
                self.groups,
                self.status,
                self.class_types,
                self.cycle,
                self.scope_status is not ScopeStatus.UNKNOWN,
                self.scope_evidence,
                self.note,
            )
        )
