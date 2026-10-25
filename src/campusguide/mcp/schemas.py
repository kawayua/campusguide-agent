"""MCP 公开契约（Pydantic v2）。

所有工具返回统一信封：

- 成功：``{"ok": true, "schema_version": "1.0", "data": ..., "warnings": []}``
- 失败：``{"ok": false, "schema_version": "1.0", "error": {...}}``

工具参数里的枚举与日期用 ``str`` 接收，由服务端手工校验，这样非法
audience/status/version_mode 会返回 ``INVALID_FILTER`` 而不是把 Pydantic
traceback 透传给客户端。
"""

from __future__ import annotations

from datetime import date
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from campusguide.domain.enums import (
    ArticleStatus,
    Audience,
    BlockType,
    VersionMode,
)
from campusguide.domain.models import ArticlePublic, PolicyFilters
from campusguide.domain.profiles import UserProfile

SCHEMA_VERSION = "1.0"

__all__ = [
    "SCHEMA_VERSION",
    "ArticlePublic",
    "DocumentSummary",
    "ErrorCode",
    "FlowSummary",
    "GetArticleData",
    "SearchArticlesData",
    "SearchArticlesRequest",
    "SearchFilters",
    "SearchHit",
    "ToolError",
    "UserProfile",
    "failure",
    "success",
    "validate_date",
]


class ErrorCode(StrEnum):
    NOT_FOUND = "NOT_FOUND"
    INVALID_FILTER = "INVALID_FILTER"
    INVALID_ARGUMENT = "INVALID_ARGUMENT"
    INDEX_UNAVAILABLE = "INDEX_UNAVAILABLE"
    RERANK_UNAVAILABLE = "RERANK_UNAVAILABLE"
    NOT_IMPLEMENTED = "NOT_IMPLEMENTED"
    CONFLICT = "CONFLICT"
    RATE_LIMITED = "RATE_LIMITED"
    INTERNAL = "INTERNAL"
    TOOL_UNAVAILABLE = "TOOL_UNAVAILABLE"


DEFAULT_RETRYABLE = {
    ErrorCode.RERANK_UNAVAILABLE: True,
    ErrorCode.RATE_LIMITED: True,
    ErrorCode.TOOL_UNAVAILABLE: True,
}


class FlowSummary(BaseModel):
    flow_id: str
    version: str
    title: str
    description: str
    audience: list[str]
    batch_label: str | None = None
    effective_from: date
    effective_to: date | None = None
    slot_count: int
    step_count: int
    navigation_only: bool = False


class ToolError(Exception):
    """可映射到 MCP 错误信封的异常。"""

    def __init__(
        self,
        code: ErrorCode,
        message: str,
        *,
        retryable: bool | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = DEFAULT_RETRYABLE.get(code, False) if retryable is None else retryable
        self.details = details or {}

    def envelope(self) -> dict[str, Any]:
        return failure(
            self.code,
            self.message,
            retryable=self.retryable,
            details=self.details,
        )


def success(data: Any, warnings: list[str] | None = None) -> dict[str, Any]:
    return {
        "ok": True,
        "schema_version": SCHEMA_VERSION,
        "data": data,
        "warnings": list(warnings or []),
    }


def failure(
    code: ErrorCode,
    message: str,
    *,
    retryable: bool | None = None,
    details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "ok": False,
        "schema_version": SCHEMA_VERSION,
        "error": {
            "code": code.value,
            "message": message,
            "retryable": DEFAULT_RETRYABLE.get(code, False) if retryable is None else retryable,
            "details": details or {},
        },
    }


def validate_date(value: str | None, field: str) -> date | None:
    if value in (None, ""):
        return None
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise ToolError(
            ErrorCode.INVALID_ARGUMENT,
            f"{field} 必须是 YYYY-MM-DD：{value!r}",
            details={"field": field},
        ) from exc


class SearchFilters(BaseModel):
    """``search_articles`` 的过滤条件；枚举值在服务端校验。"""

    model_config = ConfigDict(extra="forbid")

    block_types: list[str] | None = None
    audience: list[str] | None = None
    status: list[str] | None = None
    as_of: str | None = None
    version_mode: str = "current"
    doc_ids: list[str] | None = None
    flow_ids: list[str] | None = None
    profile: UserProfile | None = None
    # 仅供 W3 废止探针使用：忽略生效区间，读取已过期版本。
    ignore_effective_dates: bool = False


class SearchArticlesRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    query: str = Field(min_length=1, max_length=500)
    filters: SearchFilters = Field(default_factory=SearchFilters)
    top_k: int = 30
    rerank_top_n: int = 8


class SearchHit(BaseModel):
    article_id: str
    part_no: int
    block_type: str
    article: ArticlePublic
    dense_score: float | None = None
    sparse_score: float | None = None
    rrf_score: float | None = None
    rerank_score: float | None = None
    matched_spans: list[tuple[int, int]] = Field(default_factory=list)
    retrieval_query: str = ""
    rank: int = 0


class SearchArticlesData(BaseModel):
    query: str
    queries: list[str] = Field(default_factory=list)
    articles: list[SearchHit] = Field(default_factory=list)
    index_version: str = ""
    degraded: bool = False
    candidates: int = 0
    applied_filters: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    timing: dict[str, float] = Field(default_factory=dict)


class GetArticleData(BaseModel):
    article: ArticlePublic
    metadata: dict[str, Any] = Field(default_factory=dict)


class DocumentSummary(BaseModel):
    doc_id: str
    title: str
    doc_type: str
    issuer_level: str
    issuer_college_id: str | None = None
    issuing_department: str
    audience: list[str]
    audience_detail: dict[str, Any] | None = None
    version_count: int
    latest_version_id: str
    latest_effective_from: date
    latest_status: str


class ResolveVersionData(BaseModel):
    doc_id: str
    as_of: date
    mode: str
    selected_version_id: str | None = None
    reason_code: str | None = None
    candidate_version_ids: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


def _parse_enum_list(
    values: list[str] | None,
    enum_type: Any,
    field: str,
) -> list[Any] | None:
    if values is None:
        return None
    result = []
    for value in values:
        try:
            result.append(enum_type(value))
        except ValueError as exc:
            allowed = [item.value for item in enum_type]
            raise ToolError(
                ErrorCode.INVALID_FILTER,
                f"{field} 含未知值 {value!r}；允许值：{allowed}",
                details={"field": field},
            ) from exc
    return result


def policy_filters_from_request(
    request: SearchArticlesRequest,
    *,
    known_doc_ids: set[str] | None = None,
    known_flow_ids: set[str] | None = None,
    organization: Any = None,
) -> PolicyFilters:
    """把 MCP 请求转换为领域过滤条件；含枚举、日期与注册表校验。"""
    raw = request.filters
    block_types = _parse_enum_list(raw.block_types, BlockType, "block_types")
    if block_types is None:
        block_types = [BlockType.ARTICLE]
    audiences = _parse_enum_list(raw.audience, Audience, "audience")
    if audiences is not None:
        audiences = [audience for audience in audiences if audience is not Audience.ALL]
        if not audiences:
            audiences = None
    statuses = _parse_enum_list(raw.status, ArticleStatus, "status")
    try:
        version_mode = VersionMode(raw.version_mode)
    except ValueError as exc:
        raise ToolError(
            ErrorCode.INVALID_FILTER,
            f"version_mode 未知：{raw.version_mode!r}",
            details={"field": "version_mode"},
        ) from exc
    as_of = validate_date(raw.as_of, "filters.as_of") or date.today()

    if raw.doc_ids and known_doc_ids:
        unknown = sorted(set(raw.doc_ids) - known_doc_ids)
        if unknown:
            raise ToolError(
                ErrorCode.INVALID_FILTER,
                f"未登记的 doc_id：{unknown}",
                details={"field": "doc_ids"},
            )
    if raw.flow_ids and known_flow_ids:
        unknown = sorted(set(raw.flow_ids) - known_flow_ids)
        if unknown:
            raise ToolError(
                ErrorCode.INVALID_FILTER,
                f"未登记的 flow_id：{unknown}",
                details={"field": "flow_ids"},
            )

    if request.top_k < 1 or request.top_k > 50:
        raise ToolError(
            ErrorCode.INVALID_ARGUMENT,
            "top_k 必须在 1..50",
            details={"field": "top_k"},
        )
    if request.rerank_top_n < 1 or request.rerank_top_n > 20:
        raise ToolError(
            ErrorCode.INVALID_ARGUMENT,
            "rerank_top_n 必须在 1..20",
            details={"field": "rerank_top_n"},
        )
    if not request.query.strip():
        raise ToolError(ErrorCode.INVALID_ARGUMENT, "query 不能为空")

    validate_profile(raw.profile, organization)

    if statuses is None and version_mode is VersionMode.CURRENT:
        statuses = [ArticleStatus.ACTIVE]
    return PolicyFilters(
        block_types=block_types,
        audience=audiences,
        status=statuses,
        as_of=as_of,
        version_mode=version_mode,
        doc_ids=list(raw.doc_ids) if raw.doc_ids else None,
        flow_ids=list(raw.flow_ids) if raw.flow_ids else None,
        profile=raw.profile,
        ignore_effective_dates=raw.ignore_effective_dates,
    )


def validate_profile(profile: UserProfile | None, organization: Any) -> None:
    if profile is None or organization is None:
        return
    college_ids = {college.college_id for college in organization.colleges}
    major_colleges = {major.major_id: major.college_id for major in organization.majors}
    unknown_colleges = sorted(set(profile.colleges) - college_ids)
    if unknown_colleges:
        raise ToolError(
            ErrorCode.INVALID_FILTER,
            f"未登记的学院 ID：{unknown_colleges}",
            details={"field": "profile.colleges"},
        )
    unknown_majors = sorted(set(profile.majors) - set(major_colleges))
    if unknown_majors:
        raise ToolError(
            ErrorCode.INVALID_FILTER,
            f"未登记的专业 ID：{unknown_majors}",
            details={"field": "profile.majors"},
        )
    if profile.colleges and profile.majors:
        wrong = sorted(
            major_id
            for major_id in profile.majors
            if major_colleges[major_id] not in profile.colleges
        )
        if wrong:
            raise ToolError(
                ErrorCode.INVALID_FILTER,
                f"专业 {wrong} 不属于所选学院 {profile.colleges}",
                details={"field": "profile.majors"},
            )


def search_request_from_filters(
    query: str,
    filters: PolicyFilters,
    *,
    top_k: int,
    rerank_top_n: int,
) -> SearchArticlesRequest:
    """领域过滤条件 -> MCP 请求（QAService 使用）。"""
    return SearchArticlesRequest(
        query=query,
        filters=SearchFilters(
            block_types=[block_type.value for block_type in filters.block_types],
            audience=(
                [audience.value for audience in filters.audience] if filters.audience else None
            ),
            status=(
                [status.value for status in filters.status] if filters.status is not None else None
            ),
            as_of=filters.as_of.isoformat(),
            version_mode=filters.version_mode.value,
            doc_ids=list(filters.doc_ids) if filters.doc_ids else None,
            flow_ids=list(filters.flow_ids) if filters.flow_ids else None,
            profile=filters.profile,
            ignore_effective_dates=filters.ignore_effective_dates,
        ),
        top_k=top_k,
        rerank_top_n=rerank_top_n,
    )
