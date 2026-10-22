"""领域数据契约（Pydantic v2）。

字段与规格书第 5 节保持一致；Article 增加 block_type，用于把前言/附录等
非条款块与可检索条款区分开。
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, HttpUrl, field_validator, model_validator

from campusguide.domain.audience import AudienceDetail, has_student_audience
from campusguide.domain.enums import (
    ArticleStatus,
    Audience,
    AuthorityLevel,
    BlockType,
    ClaimKind,
    DocType,
    IssuerLevel,
    PolicyRelation,
    VersionMode,
)


class DocumentVersion(BaseModel):
    doc_id: str
    version_id: str
    title: str
    doc_type: DocType = DocType.OTHER
    authority_level: AuthorityLevel = AuthorityLevel.UNKNOWN
    issuer_level: IssuerLevel = IssuerLevel.UNIVERSITY
    issuer_college_id: str | None = None
    parent_doc_id: str | None = None
    relation: PolicyRelation = PolicyRelation.STANDALONE
    issuing_department: str
    source_url: HttpUrl
    published_at: date
    effective_from: date
    effective_to: date | None = None
    status: ArticleStatus
    source_sha256: str
    fetched_at: datetime
    parser_version: str
    audience: list[Audience]
    audience_detail: AudienceDetail | None = None
    notes: str | None = None

    @field_validator("audience")
    @classmethod
    def _audience_not_empty(cls, value: list[Audience]) -> list[Audience]:
        if not value:
            raise ValueError("audience 不能为空")
        return value

    @field_validator("effective_to")
    @classmethod
    def _date_order(cls, value: date | None, info: Any) -> date | None:
        effective_from = info.data.get("effective_from")
        if value is not None and effective_from is not None and value < effective_from:
            raise ValueError("effective_to 不能早于 effective_from")
        return value

    @model_validator(mode="after")
    def _validate_scope_metadata(self) -> DocumentVersion:
        if self.issuer_level in {IssuerLevel.COLLEGE, IssuerLevel.DEPARTMENT}:
            if not self.issuer_college_id:
                raise ValueError("院/系级版本必须填写 issuer_college_id")
        elif self.issuer_college_id is not None:
            raise ValueError("校级版本不能填写 issuer_college_id")
        if self.relation in {PolicyRelation.IMPLEMENTS, PolicyRelation.SUPPLEMENTS}:
            if not self.parent_doc_id:
                raise ValueError("implements/supplements 必须填写 parent_doc_id")
        elif self.parent_doc_id is not None:
            raise ValueError("standalone 不能填写 parent_doc_id")
        if (
            self.audience_detail is not None
            and self.audience_detail.has_student_constraints
            and not has_student_audience(self.audience)
        ):
            raise ValueError("非学生角色不能携带年级/阶段/学籍状态等学生约束")
        return self


class Article(BaseModel):
    article_id: str
    doc_id: str
    version_id: str
    doc_title: str
    doc_type: DocType = DocType.OTHER
    authority_level: AuthorityLevel = AuthorityLevel.UNKNOWN
    chapter_path: list[str] = Field(default_factory=list)
    article_no: str
    article_index: int
    part_no: int = 1
    block_type: BlockType = BlockType.ARTICLE
    text: str
    text_normalized: str
    text_raw_ref: str | None = None
    audience: list[Audience]
    audience_detail: AudienceDetail | None = None
    status: ArticleStatus
    effective_from: date
    effective_to: date | None = None
    source_url: HttpUrl
    published_at: date
    issuing_department: str
    topic_tags: list[str] = Field(default_factory=list)
    flow_ids: list[str] = Field(default_factory=list)
    source_sha256: str

    @property
    def is_indexable(self) -> bool:
        return self.block_type in {BlockType.ARTICLE, BlockType.SECTION} and bool(
            self.text_normalized
        )


class ArticlePublic(BaseModel):
    """对外投影：不包含本地原文引用。"""

    article_id: str
    doc_id: str
    version_id: str
    doc_title: str
    doc_type: DocType = DocType.OTHER
    authority_level: AuthorityLevel = AuthorityLevel.UNKNOWN
    chapter_path: list[str] = Field(default_factory=list)
    article_no: str
    article_index: int
    part_no: int = 1
    block_type: BlockType = BlockType.ARTICLE
    text: str
    text_normalized: str
    audience: list[Audience]
    audience_detail: AudienceDetail | None = None
    status: ArticleStatus
    effective_from: date
    effective_to: date | None = None
    source_url: HttpUrl
    published_at: date
    issuing_department: str
    topic_tags: list[str] = Field(default_factory=list)
    flow_ids: list[str] = Field(default_factory=list)
    source_sha256: str

    @classmethod
    def from_article(cls, article: Article) -> ArticlePublic:
        payload = article.model_dump(exclude={"text_raw_ref"})
        return cls.model_validate(payload)


class Evidence(BaseModel):
    article: Article
    matched_spans: list[tuple[int, int]] = Field(default_factory=list)
    dense_score: float | None = None
    sparse_score: float | None = None
    rerank_score: float | None = None
    retrieval_query: str = ""


class Citation(BaseModel):
    article_id: str
    quote: str
    quote_start: int
    quote_end: int
    doc_title: str
    article_no: str
    effective_from: date
    source_url: HttpUrl


class Claim(BaseModel):
    claim_id: str
    text: str
    kind: ClaimKind = ClaimKind.OTHER
    citations: list[Citation] = Field(default_factory=list)
    condition: str | None = None
    confidence: float | None = None

    @field_validator("citations")
    @classmethod
    def _citation_required(cls, value: list[Citation]) -> list[Citation]:
        if not value:
            raise ValueError("claim 至少需要一个 citation")
        return value


class SlotDefinition(BaseModel):
    name: str
    type: Literal["string", "integer", "boolean", "date", "enum"]
    required: bool
    question: str
    why_needed: str
    allowed_values: list[str] | None = None
    validation: dict[str, Any] | None = None
    priority: int = 100


class SlotValue(BaseModel):
    name: str
    value: str | int | bool | date
    source: Literal["user_explicit", "user_confirmed", "system_default"]
    turn_id: int


class FlowInstance(BaseModel):
    flow_id: str
    flow_version: str
    step_states: dict[
        str, Literal["available", "blocked", "conditional", "done", "not_applicable"]
    ] = Field(default_factory=dict)
    current_step_ids: list[str] = Field(default_factory=list)
    missing_slots: list[str] = Field(default_factory=list)
    updated_at: datetime


class FlowStep(BaseModel):
    id: str
    title: str
    description: str
    depends_on: list[str] = Field(default_factory=list)
    when: str | None = None
    materials: list[str] = Field(default_factory=list)
    deadline: str | None = None
    responsible_department: str
    citations: list[str] = Field(default_factory=list)
    on_unknown: Literal["ask", "conditional", "block"] = "ask"


class Prerequisite(BaseModel):
    id: str
    description: str
    when: str
    on_false: Literal["ineligible", "block"]
    on_unknown: Literal["ask", "conditional", "block"] = "ask"
    citations: list[str] = Field(default_factory=list)


class HandoffInfo(BaseModel):
    department: str
    url: HttpUrl | None = None
    note: str | None = None


class CalendarEvent(BaseModel):
    event_id: str
    title: str
    start: date
    end: date | None = None
    audience: list[Audience]
    academic_year: str
    source_url: HttpUrl


class FlowDefinition(BaseModel):
    flow_id: str
    version: str
    title: str
    description: str
    audience: list[Audience]
    aliases: list[str] = Field(default_factory=list)
    effective_from: date
    effective_to: date | None = None
    slots: list[SlotDefinition] = Field(default_factory=list)
    prerequisites: list[Prerequisite] = Field(default_factory=list)
    steps: list[FlowStep] = Field(default_factory=list)
    handoff: HandoffInfo


class PolicyFilters(BaseModel):
    audience: list[Audience] | None = None
    status: list[ArticleStatus] | None = None
    as_of: date = Field(default_factory=date.today)
    version_mode: VersionMode = VersionMode.CURRENT
    doc_ids: list[str] | None = None
    flow_ids: list[str] | None = None
