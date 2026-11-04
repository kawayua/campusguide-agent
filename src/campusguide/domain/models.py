"""领域数据契约（Pydantic v2）。

字段与规格书第 5 节保持一致；Article 增加 block_type，用于把前言/附录等
非条款块与可检索条款区分开。
"""

from __future__ import annotations

import math
from datetime import date, datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, HttpUrl, field_validator, model_validator

from campusguide.domain.audience import AudienceDetail, AudienceScope, has_student_audience
from campusguide.domain.enums import (
    ArticleStatus,
    Audience,
    AuthorityLevel,
    BlockType,
    ClaimKind,
    DocType,
    FactKind,
    IssuerLevel,
    IssueScope,
    IssueSeverity,
    OutcomeCode,
    PolicyRelation,
    VerificationDecision,
    VersionMode,
)
from campusguide.domain.profiles import UserProfile

DateConfidence = Literal["high", "low"]
MetadataSourceStatus = Literal["not_checked", "verified", "review", "failed"]
HeaderColumnStatus = Literal["bound", "shared", "gap", "unresolved", "ambiguous", "offset"]
HeaderColumnMethod = Literal[
    "local_header", "physical_interval", "trailing_empty_recovery", "none"
]
# 2 元值只表示显式 x 区间，4 元值才是单元格矩形；两者不能互相冒充。
HeaderBBoxKind = Literal["x_interval", "rect"]


def _validate_bbox_values(
    value: list[float] | None,
    *,
    field_name: str,
    lengths: tuple[int, ...],
) -> list[float] | None:
    if value is None:
        return None
    if len(value) not in lengths:
        allowed = "/".join(str(length) for length in lengths)
        raise ValueError(f"{field_name} 必须是 {allowed} 元坐标")
    for item in value:
        if isinstance(item, bool) or not isinstance(item, (int, float)):
            raise ValueError(f"{field_name} 坐标必须是数值")
        if not math.isfinite(float(item)):
            raise ValueError(f"{field_name} 坐标必须是有限数值")
    if len(value) == 4:
        x0, y0, x1, y1 = (float(item) for item in value)
        if x1 <= x0 or y1 <= y0:
            raise ValueError(f"{field_name} 矩形必须满足 x1>x0 且 y1>y0")
    else:
        x0, x1 = (float(item) for item in value)
        if x1 <= x0:
            raise ValueError(f"{field_name} 区间必须满足 x1>x0")
    return [float(item) for item in value]


class DocumentVersion(BaseModel):
    doc_id: str
    version_id: str
    title: str
    doc_type: DocType = DocType.OTHER
    authority_level: AuthorityLevel = AuthorityLevel.UNKNOWN
    issuer_level: IssuerLevel = IssuerLevel.UNIVERSITY
    issuer_college_id: str | None = None
    parent_doc_id: str | None = None
    parent_version_id: str | None = None
    relation: PolicyRelation = PolicyRelation.STANDALONE
    issuing_department: str
    source_url: HttpUrl
    published_at: date
    effective_from: date
    effective_to: date | None = None
    status: ArticleStatus
    source_sha256: str
    metadata_source_status: MetadataSourceStatus = "not_checked"
    metadata_source_audit_sha256: str | None = None
    fetched_at: datetime
    parser_version: str
    audience: list[Audience]
    audience_detail: AudienceDetail | None = None
    # 合订本按章节标题收窄适用专业/班型；章节未命中时沿用文档级受众细目。
    audience_scopes: list[AudienceScope] = Field(default_factory=list)
    date_confidence: DateConfidence = "high"
    date_source: str | None = None
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
        if self.relation in {
            PolicyRelation.IMPLEMENTS,
            PolicyRelation.SUPPLEMENTS,
            PolicyRelation.BASIS,
            PolicyRelation.ATTACHMENT,
            PolicyRelation.REPLACES,
        }:
            if not self.parent_doc_id:
                raise ValueError(f"relation={self.relation.value} 必须填写 parent_doc_id")
        elif self.parent_doc_id is not None:
            raise ValueError("standalone 不能填写 parent_doc_id")
        if self.parent_version_id is not None and self.parent_doc_id is None:
            raise ValueError("parent_version_id 必须与 parent_doc_id 同时填写")
        if (
            self.audience_detail is not None
            and self.audience_detail.has_student_constraints
            and not has_student_audience(self.audience)
        ):
            raise ValueError("非学生角色不能携带年级/阶段/学籍状态等学生约束")
        if not has_student_audience(self.audience) and any(
            scope.class_types for scope in self.audience_scopes
        ):
            raise ValueError("非学生角色不能按班型收窄章节适用范围")
        return self


class CellProvenance(BaseModel):
    """表格行块的原始单元格坐标；按完整网格保存，覆盖合并格与空列。"""

    cell_id: str
    row: int
    col: int
    text: str = ""
    rowspan: int = 1
    colspan: int = 1
    origin_row: int
    origin_col: int
    covered: bool = False
    is_header: bool = False
    bbox: list[float] | None = None
    dom_path: str | None = None
    text_source: str | None = None
    needs_review: bool = False


class HeaderColumnProvenance(BaseModel):
    """表头列来源的运行时副本。

    只搬运结构层已经证明的标签、坐标和来源表引用；缺失字段保持为空，
    不补造标签、矩形或来源。``target_bbox`` 为 2 元时只表示显式 x 区间
    （``target_bbox_kind="x_interval"``），4 元才是单元格矩形。
    """

    target_col: int
    status: HeaderColumnStatus = "unresolved"
    method: HeaderColumnMethod = "none"
    source_table_id: str | None = None
    source_via_table_id: str | None = None
    source_page: int | None = None
    source_col: int | None = None
    source_header_cell_ids: list[str] = Field(default_factory=list)
    source_header_row_indices: list[int] = Field(default_factory=list)
    source_bbox: list[float] | None = None
    target_bbox: list[float] | None = None
    target_bbox_kind: HeaderBBoxKind | None = None
    header_text: str = ""
    header_path: list[str] = Field(default_factory=list)
    covered: bool = False
    shared_scope: bool = False
    reason: str = ""
    source_header_cells: list[CellProvenance] = Field(default_factory=list)
    # 复核审计镜像：来源表头 origin 被标记 needs_review 时，标准 source_*/
    # header_* 必须保持为空；这里只保存原始证据，供人工/本地 OCR 复核。
    review_source_table_id: str | None = None
    review_source_page: int | None = None
    review_source_col: int | None = None
    review_source_header_cell_ids: list[str] = Field(default_factory=list)
    review_source_header_row_indices: list[int] = Field(default_factory=list)
    review_source_bbox: list[float] | None = None
    review_source_text: str = ""
    review_source_header_cells: list[CellProvenance] = Field(default_factory=list)

    @field_validator(
        "target_col",
        "source_page",
        "source_col",
        "review_source_page",
        "review_source_col",
        mode="before",
    )
    @classmethod
    def _integer_not_bool(cls, value: Any, info: Any) -> Any:
        if isinstance(value, bool):
            raise ValueError(f"{info.field_name} 不能是布尔值")
        return value

    @field_validator("target_col")
    @classmethod
    def _target_col_nonnegative(cls, value: int) -> int:
        if value < 0:
            raise ValueError("target_col 必须是非负整数")
        return value

    @field_validator("source_col", "source_page", "review_source_col", "review_source_page")
    @classmethod
    def _nonnegative_or_positive(cls, value: int | None, info: Any) -> int | None:
        if value is None:
            return None
        if not isinstance(value, int):
            raise ValueError(f"{info.field_name} 必须是整数或 None")
        minimum = 1 if info.field_name.endswith("source_page") else 0
        if value < minimum:
            raise ValueError(f"{info.field_name} 必须 >= {minimum}")
        return value

    @field_validator(
        "source_header_row_indices", "review_source_header_row_indices", mode="before"
    )
    @classmethod
    def _row_indices_not_bool(cls, value: Any, info: Any) -> Any:
        if isinstance(value, list) and any(
            isinstance(item, bool) for item in value
        ):
            raise ValueError(f"{info.field_name} 必须是非负整数")
        return value

    @field_validator("source_header_row_indices", "review_source_header_row_indices")
    @classmethod
    def _source_rows_nonnegative(cls, value: list[int], info: Any) -> list[int]:
        if any(item < 0 for item in value):
            raise ValueError(f"{info.field_name} 必须是非负整数")
        return value

    @field_validator("source_bbox")
    @classmethod
    def _source_bbox_rect(cls, value: list[float] | None) -> list[float] | None:
        return _validate_bbox_values(value, field_name="source_bbox", lengths=(4,))

    @field_validator("review_source_bbox")
    @classmethod
    def _review_source_bbox_rect(
        cls, value: list[float] | None
    ) -> list[float] | None:
        return _validate_bbox_values(
            value, field_name="review_source_bbox", lengths=(4,)
        )

    @field_validator("target_bbox")
    @classmethod
    def _target_bbox_interval_or_rect(
        cls, value: list[float] | None
    ) -> list[float] | None:
        return _validate_bbox_values(value, field_name="target_bbox", lengths=(2, 4))

    @model_validator(mode="after")
    def _validate_review_source_audit(self) -> HeaderColumnProvenance:
        review_present = bool(
            self.review_source_table_id is not None
            or self.review_source_page is not None
            or self.review_source_col is not None
            or self.review_source_header_cell_ids
            or self.review_source_header_row_indices
            or self.review_source_bbox is not None
            or self.review_source_text
            or self.review_source_header_cells
        )
        if not review_present:
            return self
        if self.status != "unresolved" or self.method != "none":
            raise ValueError(
                "review_source_* 只允许用于 unresolved/method=none 的审计来源"
            )
        if (
            self.source_table_id is not None
            or self.source_via_table_id is not None
            or self.source_page is not None
            or self.source_col is not None
            or self.source_header_cell_ids
            or self.source_header_row_indices
            or self.source_bbox is not None
        ):
            raise ValueError("复核来源审计不能同时携带标准 source_* 证据")
        if self.header_text or self.header_path:
            raise ValueError("复核来源审计列必须保持空表头文本和路径")
        if (
            not self.review_source_table_id
            or self.review_source_table_id != self.review_source_table_id.strip()
        ):
            raise ValueError("review_source_table_id 不能为空或含首尾空白")
        if self.review_source_col is None:
            raise ValueError("review_source_col 必须显式填写")
        if not self.review_source_header_cell_ids:
            raise ValueError("review_source_header_cell_ids 不能为空")
        if not self.review_source_header_row_indices:
            raise ValueError("review_source_header_row_indices 不能为空")
        if [
            cell.cell_id for cell in self.review_source_header_cells
        ] != list(self.review_source_header_cell_ids):
            raise ValueError(
                "review_source_header_cells 必须与 "
                "review_source_header_cell_ids 一一对应"
            )
        return self

    @model_validator(mode="after")
    def _validate_bbox_kind(self) -> HeaderColumnProvenance:
        if self.target_bbox is None:
            if self.target_bbox_kind is not None:
                raise ValueError("target_bbox_kind 不能脱离 target_bbox 填写")
            return self
        expected: HeaderBBoxKind = (
            "x_interval" if len(self.target_bbox) == 2 else "rect"
        )
        if self.target_bbox_kind is None:
            self.target_bbox_kind = expected
        elif self.target_bbox_kind != expected:
            raise ValueError(
                "target_bbox_kind 与 target_bbox 长度不一致："
                f"期望 {expected}，实际 {self.target_bbox_kind}"
            )
        return self


class Article(BaseModel):
    article_id: str
    doc_id: str
    version_id: str
    doc_title: str
    doc_type: DocType = DocType.OTHER
    authority_level: AuthorityLevel = AuthorityLevel.UNKNOWN
    issuer_level: IssuerLevel = IssuerLevel.UNIVERSITY
    issuer_college_id: str | None = None
    parent_doc_id: str | None = None
    parent_version_id: str | None = None
    relation: PolicyRelation = PolicyRelation.STANDALONE
    chapter_path: list[str] = Field(default_factory=list)
    article_no: str
    article_index: int
    part_no: int = 1
    block_type: BlockType = BlockType.ARTICLE
    block_kind: str = "text"
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
    date_confidence: DateConfidence = "high"
    date_source: str | None = None
    issuing_department: str
    topic_tags: list[str] = Field(default_factory=list)
    flow_ids: list[str] = Field(default_factory=list)
    source_sha256: str
    metadata_source_status: MetadataSourceStatus = "not_checked"
    metadata_source_audit_sha256: str | None = None
    parent_article_id: str | None = None
    parent_table_id: str | None = None
    table_id: str | None = None
    row_id: str | None = None
    row_index: int | None = None
    table_headers: list[str] = Field(default_factory=list)
    header_paths: list[list[str]] = Field(default_factory=list)
    cell_provenance: list[CellProvenance] = Field(default_factory=list)
    table_column_bindings: list[HeaderColumnProvenance] = Field(default_factory=list)
    page: int | None = None
    bbox: list[float] | None = None
    dom_path: str | None = None
    source_cell_ids: list[str] = Field(default_factory=list)
    # 跨页续表头来源：只传递解析结构层已建立的引用，旧解析保持 None。
    table_continuation_of: str | None = None
    table_header_source_id: str | None = None
    table_header_source_page: int | None = None

    @field_validator("table_header_source_page", mode="before")
    @classmethod
    def _header_source_page_positive(cls, value: Any) -> int | None:
        if value is None:
            return None
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise ValueError("table_header_source_page 必须是正整数或 None")
        return value

    @model_validator(mode="after")
    def _validate_table_header_provenance(self) -> Article:
        if self.table_header_source_page is not None and not self.table_header_source_id:
            raise ValueError("table_header_source_page 必须与 table_header_source_id 同时填写")
        if self.table_header_source_id is not None and not self.table_header_source_id.strip():
            raise ValueError("table_header_source_id 不能为空")
        if self.table_continuation_of is not None and not self.table_continuation_of.strip():
            raise ValueError("table_continuation_of 不能为空")
        return self

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
    issuer_level: IssuerLevel = IssuerLevel.UNIVERSITY
    issuer_college_id: str | None = None
    parent_doc_id: str | None = None
    parent_version_id: str | None = None
    relation: PolicyRelation = PolicyRelation.STANDALONE
    chapter_path: list[str] = Field(default_factory=list)
    article_no: str
    article_index: int
    part_no: int = 1
    block_type: BlockType = BlockType.ARTICLE
    block_kind: str = "text"
    text: str
    text_normalized: str
    audience: list[Audience]
    audience_detail: AudienceDetail | None = None
    status: ArticleStatus
    effective_from: date
    effective_to: date | None = None
    source_url: HttpUrl
    published_at: date
    date_confidence: DateConfidence = "high"
    date_source: str | None = None
    issuing_department: str
    topic_tags: list[str] = Field(default_factory=list)
    flow_ids: list[str] = Field(default_factory=list)
    source_sha256: str
    metadata_source_status: MetadataSourceStatus = "not_checked"
    metadata_source_audit_sha256: str | None = None
    parent_article_id: str | None = None
    parent_table_id: str | None = None
    table_id: str | None = None
    row_id: str | None = None
    row_index: int | None = None
    table_headers: list[str] = Field(default_factory=list)
    header_paths: list[list[str]] = Field(default_factory=list)
    cell_provenance: list[CellProvenance] = Field(default_factory=list)
    table_column_bindings: list[HeaderColumnProvenance] = Field(default_factory=list)
    page: int | None = None
    bbox: list[float] | None = None
    dom_path: str | None = None
    source_cell_ids: list[str] = Field(default_factory=list)
    table_continuation_of: str | None = None
    table_header_source_id: str | None = None
    table_header_source_page: int | None = None

    @field_validator("table_header_source_page", mode="before")
    @classmethod
    def _header_source_page_positive(cls, value: Any) -> int | None:
        if value is None:
            return None
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise ValueError("table_header_source_page 必须是正整数或 None")
        return value

    @model_validator(mode="after")
    def _validate_table_header_provenance(self) -> ArticlePublic:
        if self.table_header_source_page is not None and not self.table_header_source_id:
            raise ValueError("table_header_source_page 必须与 table_header_source_id 同时填写")
        if self.table_header_source_id is not None and not self.table_header_source_id.strip():
            raise ValueError("table_header_source_id 不能为空")
        if self.table_continuation_of is not None and not self.table_continuation_of.strip():
            raise ValueError("table_continuation_of 不能为空")
        return self

    @classmethod
    def from_article(cls, article: Article) -> ArticlePublic:
        payload = article.model_dump(exclude={"text_raw_ref"})
        return cls.model_validate(payload)


class Evidence(BaseModel):
    article: Article
    matched_spans: list[tuple[int, int]] = Field(default_factory=list)
    dense_score: float | None = None
    sparse_score: float | None = None
    rrf_score: float | None = None
    rerank_score: float | None = None
    retrieval_query: str = ""
    rank: int = 0
    # W6：同一 block 命中多个子查询时记录来源；source=flow 表示由流程
    # citation 通过 get_article 水合，分数固定为 0，但仍参与引用核对。
    matched_queries: list[str] = Field(default_factory=list)
    source: Literal["search", "flow"] = "search"
    # Independent structural provenance from source views. These never alter
    # rerank_score; downstream selection may use them as a separate signal.
    structured: bool = False
    structured_rank: int | None = None

    @property
    def key(self) -> tuple[str, int]:
        return (self.article.article_id, self.article.part_no)

    @property
    def score(self) -> float:
        if self.rerank_score is not None:
            return self.rerank_score
        if self.rrf_score is not None:
            return self.rrf_score
        if self.dense_score is not None:
            return self.dense_score
        if self.sparse_score is not None:
            return self.sparse_score
        return 0.0


class Citation(BaseModel):
    article_id: str
    quote: str
    quote_start: int
    quote_end: int
    doc_title: str
    article_no: str
    part_no: int = 1
    block_type: BlockType = BlockType.ARTICLE
    effective_from: date
    date_confidence: DateConfidence = "high"
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


class Fact(BaseModel):
    kind: FactKind
    raw: str
    canonical: str


class VerificationIssue(BaseModel):
    code: str
    scope: IssueScope
    severity: IssueSeverity
    claim_id: str | None = None
    article_id: str | None = None
    part_no: int | None = None
    detail: str = ""
    facts: list[str] = Field(default_factory=list)


class VerificationResult(BaseModel):
    """一次问答的确定性核对结果。

    契约（W3 v2 第 4.2 节）：
    - PARTIAL 必须仍有可渲染 claim，且 dropped_claim_ids 非空；
    - PASS 不得同时存在 CLAIM + BLOCKING issue；
    - ABSTAIN 不携带任何 claim，且必须给出原因码。
    """

    decision: VerificationDecision
    claims: list[Claim] = Field(default_factory=list)
    issues: list[VerificationIssue] = Field(default_factory=list)
    dropped_claim_ids: list[str] = Field(default_factory=list)
    reason_code: OutcomeCode | None = None

    @model_validator(mode="after")
    def _validate_decision(self) -> VerificationResult:
        blocking_claims = [
            issue
            for issue in self.issues
            if issue.scope is IssueScope.CLAIM and issue.severity is IssueSeverity.BLOCKING
        ]
        if self.decision is VerificationDecision.PASS:
            if blocking_claims:
                raise ValueError("PASS 不得包含 severity=blocking 的 claim issue")
            if self.dropped_claim_ids:
                raise ValueError("PASS 不得包含 dropped_claim_ids")
            if self.reason_code is not None:
                raise ValueError("PASS 的 reason_code 必须为空")
        elif self.decision is VerificationDecision.PARTIAL:
            if not self.claims:
                raise ValueError("PARTIAL 必须至少保留一个 claim")
            if not self.dropped_claim_ids:
                raise ValueError("PARTIAL 必须至少有一个 dropped_claim_ids")
            if self.reason_code is not OutcomeCode.PARTIAL_ANSWER:
                raise ValueError("PARTIAL 的 reason_code 必须是 PARTIAL_ANSWER")
        else:
            if self.claims:
                raise ValueError("ABSTAIN 不得携带 claim")
            if self.reason_code is None:
                raise ValueError("ABSTAIN 必须给出 reason_code")
        return self


class SlotDefinition(BaseModel):
    name: str
    type: Literal["string", "integer", "boolean", "date", "enum"]
    required: bool
    question: str
    why_needed: str
    allowed_values: list[str] | None = None
    aliases: dict[str, list[str]] | None = None
    validation: dict[str, Any] | None = None
    priority: int = 100

    @model_validator(mode="after")
    def _validate_definition(self) -> SlotDefinition:
        if self.type == "enum" and not self.allowed_values:
            raise ValueError(f"enum 槽位 {self.name} 必须提供 allowed_values")
        if self.aliases:
            for canonical, values in self.aliases.items():
                if self.allowed_values and canonical not in self.allowed_values:
                    raise ValueError(f"槽位 {self.name} 的别名键不在 allowed_values：{canonical}")
                if not values:
                    raise ValueError(f"槽位 {self.name} 的别名 {canonical} 不能为空")
        return self


class SlotValue(BaseModel):
    name: str
    value: str | int | bool | date
    source: Literal["user_explicit", "user_confirmed", "system_default"]
    turn_id: int


class FlowInstance(BaseModel):
    flow_id: str
    flow_version: str
    step_states: dict[
        str,
        Literal["available", "blocked", "conditional", "done", "not_applicable", "closed"],
    ] = Field(default_factory=dict)
    current_step_ids: list[str] = Field(default_factory=list)
    missing_slots: list[str] = Field(default_factory=list)
    asked_slots: list[str] = Field(default_factory=list)
    declined_slots: list[str] = Field(default_factory=list)
    pending_slot: str | None = None
    clarify_count: int = 0
    done_step_ids: list[str] = Field(default_factory=list)
    last_question: str | None = None
    batch_label: str | None = None
    status: Literal["open", "partial", "closed", "unknown"] = "unknown"
    updated_at: datetime


class FlowStep(BaseModel):
    id: str
    title: str
    description: str
    depends_on: list[str] = Field(default_factory=list)
    when: str | None = None
    materials: list[str] = Field(default_factory=list)
    deadline: str | None = None
    deadline_note: str | None = None
    closed_observed_at: date | None = None
    responsible_department: str
    citations: list[str] = Field(default_factory=list)
    citation_exempt: bool = False
    on_unknown: Literal["ask", "conditional", "block"] = "ask"

    @model_validator(mode="after")
    def _validate_step(self) -> FlowStep:
        if self.citation_exempt and (self.materials or self.deadline or self.closed_observed_at):
            raise ValueError(f"citation_exempt 步骤不能携带材料/截止事实：{self.id}")
        if self.deadline and self.closed_observed_at:
            raise ValueError(f"步骤不能同时填写 deadline 与 closed_observed_at：{self.id}")
        if not self.deadline and not self.closed_observed_at and not self.deadline_note:
            raise ValueError(f"步骤必须填写 deadline/deadline_note/closed_observed_at：{self.id}")
        return self


class ChecklistItem(BaseModel):
    step_id: str
    title: str
    status: Literal["available", "blocked", "conditional", "done", "not_applicable", "closed"]
    description: str
    materials: list[str] = Field(default_factory=list)
    deadline: str | None = None
    deadline_note: str | None = None
    department: str
    citations: list[str] = Field(default_factory=list)
    blocked_reason: str | None = None
    status_note: str | None = None


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
    source_url: HttpUrl | None = None
    note: str | None = None


class FlowDefinition(BaseModel):
    flow_id: str
    version: str
    title: str
    description: str
    audience: list[Audience]
    aliases: list[str] = Field(default_factory=list)
    effective_from: date
    effective_to: date | None = None
    batch_label: str | None = None
    navigation_only: bool = False
    slots: list[SlotDefinition] = Field(default_factory=list)
    prerequisites: list[Prerequisite] = Field(default_factory=list)
    steps: list[FlowStep] = Field(default_factory=list)
    handoff: HandoffInfo


class PolicyFilters(BaseModel):
    block_types: list[BlockType] = Field(default_factory=lambda: [BlockType.ARTICLE])
    audience: list[Audience] | None = None
    status: list[ArticleStatus] | None = Field(default_factory=lambda: [ArticleStatus.ACTIVE])
    as_of: date = Field(default_factory=date.today)
    version_mode: VersionMode = VersionMode.CURRENT
    doc_ids: list[str] | None = None
    flow_ids: list[str] | None = None
    profile: UserProfile | None = None
    # 明确目标年度：不把“当前学年”硬套到招生/毕业届别文件。
    academic_year: str | None = None
    admission_year: int | None = None
    graduating_year: int | None = None
    # 废止探针需要看到已过期的 repealed 版本，默认仍启用生效区间过滤。
    ignore_effective_dates: bool = False
