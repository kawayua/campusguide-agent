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
    ProgramType,
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


class AudienceDetail(BaseModel):
    """角色之外的适用范围；不填表示不限制。"""

    colleges: list[str] = Field(default_factory=list)
    majors: list[str] = Field(default_factory=list)
    intake_years: list[int] = Field(default_factory=list)
    intake_from: int | None = None
    graduating_years: list[int] = Field(default_factory=list)
    stages: list[AudienceStage] = Field(default_factory=list)
    program_types: list[ProgramType] = Field(default_factory=list)
    groups: list[StudentGroup] = Field(default_factory=list)
    status: list[StudentStatus] = Field(default_factory=list)
    cycle: str | None = None
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
        return self

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
                self.groups,
                self.status,
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
                self.stages,
                self.program_types,
                self.groups,
                self.status,
                self.cycle,
                self.note,
            )
        )
