"""查询侧用户画像。

W4 只收集检索过滤所需的细目：学院、专业、入学年份、阶段、培养类型、
特定群体与学籍状态。字段为空表示“用户未提供”，不是“不适用”。
学院/专业 ID 必须属于 ``configs/organization.yaml``；注册表校验在
MCP server 入口完成，领域模型只做格式校验。
"""

from __future__ import annotations

import re

from pydantic import BaseModel, Field, field_validator

from campusguide.domain.enums import (
    AudienceStage,
    DegreeType,
    LearningMode,
    ProgramType,
    StudentGroup,
    StudentStatus,
)

Slug = re.compile(r"^[a-z0-9][a-z0-9-]*$")
MIN_YEAR = 1980
MAX_YEAR = 2100


class UserProfile(BaseModel):
    """用户自述的适用人群信息；所有字段可选。"""

    colleges: list[str] = Field(default_factory=list)
    majors: list[str] = Field(default_factory=list)
    intake_year: int | None = None
    admission_year: int | None = None
    graduating_year: int | None = None
    academic_year: str | None = None
    stage: AudienceStage | None = None
    program_type: ProgramType | None = None
    learning_mode: LearningMode | None = None
    degree_type: DegreeType | None = None
    class_types: list[str] = Field(default_factory=list)
    groups: list[StudentGroup] = Field(default_factory=list)
    status: StudentStatus | None = None

    @field_validator("colleges", "majors", "class_types")
    @classmethod
    def _validate_slugs(cls, value: list[str]) -> list[str]:
        for item in value:
            if not Slug.match(item):
                raise ValueError(f"ID 必须是小写 slug：{item!r}")
        if len(value) != len(set(value)):
            raise ValueError("ID 不能重复")
        return value

    @field_validator("intake_year", "admission_year", "graduating_year")
    @classmethod
    def _validate_year(cls, value: int | None) -> int | None:
        if value is not None and not MIN_YEAR <= value <= MAX_YEAR:
            raise ValueError(f"年份超出合理范围：{value}")
        return value

    @field_validator("academic_year")
    @classmethod
    def _validate_academic_year(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if not re.match(r"^\d{4}(?:-\d{4})?$", value):
            raise ValueError("academic_year 必须是 2025 或 2025-2026 形式")
        return value

    @field_validator("groups")
    @classmethod
    def _validate_groups(cls, value: list[StudentGroup]) -> list[StudentGroup]:
        if len(value) != len(set(value)):
            raise ValueError("群体不能重复")
        return value

    @property
    def is_empty(self) -> bool:
        return not any(
            (
                self.colleges,
                self.majors,
                self.intake_year is not None,
                self.admission_year is not None,
                self.graduating_year is not None,
                self.academic_year is not None,
                self.stage is not None,
                self.program_type is not None,
                self.learning_mode is not None,
                self.degree_type is not None,
                self.class_types,
                self.groups,
                self.status is not None,
            )
        )

    def known_fields(self) -> set[str]:
        fields: set[str] = set()
        if self.colleges:
            fields.add("colleges")
        if self.majors:
            fields.add("majors")
        if self.intake_year is not None:
            fields.add("intake")
        if self.graduating_year is not None:
            fields.add("graduating")
        if self.class_types:
            fields.add("class_types")
        if self.stage is not None:
            fields.add("stages")
        if self.program_type is not None:
            fields.add("program_types")
        if self.learning_mode is not None:
            fields.add("learning_modes")
        if self.degree_type is not None:
            fields.add("degree_types")
        if self.groups:
            fields.add("groups")
        if self.status is not None:
            fields.add("status")
        return fields

    def missing_fields(self) -> set[str]:
        return {
            "colleges",
            "majors",
            "intake",
            "stages",
            "program_types",
            "groups",
            "status",
        } - self.known_fields()
