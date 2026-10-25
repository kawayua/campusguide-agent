"""配置加载与校验。

sources.yaml 支持两种写法：
1. 顶层直接是 documents 列表（规格书示例）；
2. 带 schema_version/corpus_id/school 元数据的对象。
"""

from __future__ import annotations

import os
import re
from datetime import date
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field, HttpUrl, ValidationError, model_validator

from campusguide.domain.audience import AudienceDetail, has_student_audience
from campusguide.domain.enums import (
    ArticleStatus,
    Audience,
    AuthorityLevel,
    DocType,
    IssuerLevel,
    PolicyRelation,
)


class SourcesConfigError(ValueError):
    """来源清单不合法。"""


class OrganizationConfigError(ValueError):
    """组织注册表不合法。"""


Slug = re.compile(r"^[a-z0-9][a-z0-9-]*$")


def _check_audience(audience: list[Audience]) -> list[Audience]:
    if not audience:
        raise ValueError("audience 不能为空")
    if len(audience) != len(set(audience)):
        raise ValueError("audience 不能重复")
    if Audience.ALL in audience and len(audience) > 1:
        raise ValueError("audience 为 all 时不能再与其他角色并列")
    return audience


def default_authority_level(doc_type: DocType) -> AuthorityLevel:
    if doc_type is DocType.REGULATION:
        return AuthorityLevel.BINDING
    if doc_type is DocType.NOTICE:
        return AuthorityLevel.OPERATIONAL
    if doc_type in {DocType.GUIDE, DocType.FAQ, DocType.REPORT}:
        return AuthorityLevel.INFORMATIONAL
    return AuthorityLevel.UNKNOWN


class SourceVersion(BaseModel):
    version_id: str
    url: HttpUrl | None = None
    local_path: str | None = None
    published_at: date | None = None
    effective_from: date | None = None
    effective_to: date | None = None
    status: ArticleStatus = ArticleStatus.ACTIVE
    format: Literal["auto", "html", "pdf"] = "auto"
    audience: list[Audience] | None = None
    audience_detail: AudienceDetail | None = None
    date_confidence: Literal["high", "low"] = "high"
    notes: str | None = None

    @model_validator(mode="after")
    def _validate_source(self) -> SourceVersion:
        if self.url is None and self.local_path is None:
            raise ValueError("url 与 local_path 至少填写一个")
        if self.local_path is not None and self.url is None:
            raise ValueError("local_path 只表示本地内容来源，仍需 url 作为引用链接")
        if self.effective_to and self.effective_from and self.effective_to < self.effective_from:
            raise ValueError("effective_to 不能早于 effective_from")
        if self.audience is not None:
            _check_audience(self.audience)
        if self.notes and "date_confidence=low" in self.notes:
            self.date_confidence = "low"
        return self


class SourceDocument(BaseModel):
    doc_id: str
    title: str
    doc_type: DocType = DocType.OTHER
    authority_level: AuthorityLevel | None = None
    issuer_level: IssuerLevel = IssuerLevel.UNIVERSITY
    issuer_college_id: str | None = None
    parent_doc_id: str | None = None
    relation: PolicyRelation = PolicyRelation.STANDALONE
    issuing_department: str
    audience: list[Audience] = Field(default_factory=lambda: [Audience.ALL])
    audience_detail: AudienceDetail | None = None
    flow_ids: list[str] = Field(default_factory=list)
    topic_tags: list[str] = Field(default_factory=list)
    # 院系官网公开的教职工名录可设为 false：保留手机号、邮箱、联系人姓名，
    # 身份证号与学号始终脱敏。制度类文档保持默认值。
    redact_contacts: bool = True
    versions: list[SourceVersion] = Field(min_length=1)

    @property
    def effective_authority_level(self) -> AuthorityLevel:
        return self.authority_level or default_authority_level(self.doc_type)

    @model_validator(mode="after")
    def _validate_doc_id(self) -> SourceDocument:
        if not Slug.match(self.doc_id):
            raise ValueError(f"doc_id 必须是小写 slug（a-z0-9-）：{self.doc_id!r}")
        _check_audience(self.audience)

        if self.issuer_level in {IssuerLevel.COLLEGE, IssuerLevel.DEPARTMENT}:
            if not self.issuer_college_id:
                raise ValueError(f"{self.doc_id} 为院/系级文件时必须填写 issuer_college_id")
        elif self.issuer_college_id is not None:
            raise ValueError("校级文件不能填写 issuer_college_id")

        if self.issuer_college_id is not None and not Slug.match(self.issuer_college_id):
            raise ValueError(f"issuer_college_id 必须是小写 slug：{self.issuer_college_id!r}")

        if self.parent_doc_id is not None:
            if not Slug.match(self.parent_doc_id):
                raise ValueError(f"parent_doc_id 必须是小写 slug：{self.parent_doc_id!r}")
            if self.parent_doc_id == self.doc_id:
                raise ValueError("parent_doc_id 不能指向自身")
        if self.relation in {PolicyRelation.IMPLEMENTS, PolicyRelation.SUPPLEMENTS}:
            if not self.parent_doc_id:
                raise ValueError(f"relation={self.relation.value} 时必须填写 parent_doc_id")
        elif self.parent_doc_id is not None:
            raise ValueError("relation=standalone 时不能填写 parent_doc_id")

        if (
            self.audience_detail is not None
            and self.audience_detail.has_student_constraints
            and not has_student_audience(self.audience)
        ):
            raise ValueError("非学生角色的 audience 不能携带年级/阶段/学籍状态等学生约束")

        version_ids = [version.version_id for version in self.versions]
        if len(version_ids) != len(set(version_ids)):
            raise ValueError(f"{self.doc_id} 下存在重复 version_id")

        for version in self.versions:
            effective_audience = version.audience or self.audience
            effective_detail = (
                version.audience_detail
                if version.audience_detail is not None
                else self.audience_detail
            )
            if (
                effective_detail is not None
                and effective_detail.has_student_constraints
                and not has_student_audience(effective_audience)
            ):
                raise ValueError(
                    f"{self.doc_id}@{version.version_id} 的 audience 与年级/阶段约束冲突"
                )
        return self


class SourcesConfig(BaseModel):
    schema_version: str = "1.0"
    corpus_id: str = "scau"
    school: str = "华南农业大学"
    documents: list[SourceDocument] = Field(default_factory=list)

    @model_validator(mode="after")
    def _unique_doc_ids(self) -> SourcesConfig:
        doc_ids = [document.doc_id for document in self.documents]
        if len(doc_ids) != len(set(doc_ids)):
            duplicates = sorted({doc_id for doc_id in doc_ids if doc_ids.count(doc_id) > 1})
            raise ValueError(f"存在重复 doc_id: {duplicates}")
        return self

    @property
    def version_count(self) -> int:
        return sum(len(document.versions) for document in self.documents)


class College(BaseModel):
    college_id: str
    name: str
    aliases: list[str] = Field(default_factory=list)
    valid_from: date | None = None
    valid_to: date | None = None

    @model_validator(mode="after")
    def _validate_college(self) -> College:
        if not Slug.match(self.college_id):
            raise ValueError(f"college_id 必须是小写 slug：{self.college_id!r}")
        if self.valid_to and self.valid_from and self.valid_to < self.valid_from:
            raise ValueError("college valid_to 不能早于 valid_from")
        return self


class Major(BaseModel):
    major_id: str
    name: str
    college_id: str
    major_code: str | None = None
    aliases: list[str] = Field(default_factory=list)
    valid_from: date | None = None
    valid_to: date | None = None

    @model_validator(mode="after")
    def _validate_major(self) -> Major:
        if not Slug.match(self.major_id):
            raise ValueError(f"major_id 必须是小写 slug：{self.major_id!r}")
        if not Slug.match(self.college_id):
            raise ValueError(f"major.college_id 必须是小写 slug：{self.college_id!r}")
        if self.valid_to and self.valid_from and self.valid_to < self.valid_from:
            raise ValueError("major valid_to 不能早于 valid_from")
        return self


class OrganizationConfig(BaseModel):
    schema_version: str = "1.0"
    corpus_id: str = "scau"
    colleges: list[College] = Field(default_factory=list)
    majors: list[Major] = Field(default_factory=list)

    @model_validator(mode="after")
    def _validate_registry(self) -> OrganizationConfig:
        college_ids = [college.college_id for college in self.colleges]
        if len(college_ids) != len(set(college_ids)):
            raise ValueError("存在重复 college_id")
        major_ids = [major.major_id for major in self.majors]
        if len(major_ids) != len(set(major_ids)):
            raise ValueError("存在重复 major_id")
        known_colleges = set(college_ids)
        for major in self.majors:
            if major.college_id not in known_colleges:
                raise ValueError(f"{major.major_id} 引用了未登记的学院：{major.college_id}")
        return self


class AbstentionConfig(BaseModel):
    """W3 v2 确定性核对参数与评测门禁。"""

    min_quote_chars: int = 6
    max_claims: int = 5
    enable_repealed_probe: bool = True
    support_gate: float = 0.85
    refusal_recall_gate: float = 0.80
    false_refusal_gate: float = 0.20


class GraphPlannerConfig(BaseModel):
    llm_enabled: bool = True
    fallback: Literal["rule"] = "rule"


class GraphSemanticConfig(BaseModel):
    enabled: bool = True
    on_unavailable: Literal["strict", "allow_with_warning"] = "strict"
    batch_size: int = Field(default=5, ge=1, le=5)
    timeout_seconds: float = Field(default=20.0, gt=0)


class GraphCheckpointerConfig(BaseModel):
    backend: Literal["memory", "sqlite"] = "memory"
    ttl_hours: int = Field(default=24, ge=1)


class GraphConfig(BaseModel):
    """W6 图编排参数；硬校验阈值仍留在 abstention.yaml。"""

    enabled: bool = True
    model_profile: Literal["vpro", "ultraspeed"] = "vpro"
    max_queries: int = Field(default=3, ge=1, le=3)
    max_evidence: int = Field(default=8, ge=1)
    token_budget: int = Field(default=4000, ge=500)
    max_revise: int = Field(default=2, ge=0)
    max_clarify: int = Field(default=3, ge=0)
    max_per_doc: int = Field(default=3, ge=1)
    planner: GraphPlannerConfig = Field(default_factory=GraphPlannerConfig)
    semantic: GraphSemanticConfig = Field(default_factory=GraphSemanticConfig)
    checkpointer: GraphCheckpointerConfig = Field(default_factory=GraphCheckpointerConfig)


class HandoffEntry(BaseModel):
    department: str
    url: str | None = None
    note: str | None = None


class HandoffConfig(BaseModel):
    """Finalize 的人工入口兜底；不编造电话/个人联系方式。"""

    defaults: HandoffEntry
    flows: dict[str, HandoffEntry] = Field(default_factory=dict)
    reasons: dict[str, HandoffEntry] = Field(default_factory=dict)


class MCPServerConfig(BaseModel):
    command: list[str] = Field(
        default_factory=lambda: ["python", "-m", "campusguide.mcp.policy_search_server"]
    )
    timeout_seconds: float = 30.0
    retries: int = 1
    startup_timeout_seconds: float = 300.0


class MCPConfig(BaseModel):
    search_server: MCPServerConfig = Field(
        default_factory=lambda: MCPServerConfig(
            command=["python", "-m", "campusguide.mcp.policy_search_server"]
        )
    )
    meta_server: MCPServerConfig = Field(
        default_factory=lambda: MCPServerConfig(
            command=["python", "-m", "campusguide.mcp.policy_meta_server"],
            timeout_seconds=10.0,
            retries=0,
        )
    )


class RetrievalConfig(BaseModel):
    collection: str
    alias: str
    embedding_model: str
    dense_vector_name: str = "dense"
    dense_vector_size: int = 1024
    sparse_vector_name: str = "sparse"
    sparse_mode: str = "bm25"
    bm25_k1: float = 1.2
    bm25_b: float = 0.75
    rrf_k: int = 60
    recall_top_k: int = 30
    rerank_candidates: int = 20
    rerank_top_n: int = 8
    reranker_model: str = "BAAI/bge-reranker-v2-m3"
    token_budget: int = 4000
    max_articles: int = 8
    max_table_sections: int = 2
    min_rerank_score: float = 0.30
    min_score_margin: float = 0.03
    query_top_k: int = 20
    max_evidence: int = 8
    max_chars_per_evidence: int = 800
    min_dense_score: float = 0.35
    include_sections_default: bool = False


def load_yaml(path: str | Path) -> Any:
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"配置文件不存在：{path}")
    with path.open("r", encoding="utf-8") as handle:
        return yaml.safe_load(handle)


def expand_env(value: Any, *, strict: bool = False) -> Any:
    """递归展开 ${VAR}；strict=True 时缺失变量直接报错。"""
    if isinstance(value, dict):
        return {key: expand_env(item, strict=strict) for key, item in value.items()}
    if isinstance(value, list):
        return [expand_env(item, strict=strict) for item in value]
    if isinstance(value, str):
        pattern = re.compile(r"\$\{([A-Z0-9_]+)\}")

        def replace(match: re.Match[str]) -> str:
            name = match.group(1)
            replacement = os.getenv(name)
            if replacement is None:
                if strict:
                    raise SourcesConfigError(f"缺少环境变量：{name}")
                return ""
            return replacement

        return pattern.sub(replace, value)
    return value


def load_sources(path: str | Path) -> SourcesConfig:
    raw = load_yaml(path)
    try:
        if isinstance(raw, list):
            return SourcesConfig(documents=raw)
        if isinstance(raw, dict):
            return SourcesConfig.model_validate(raw)
    except ValidationError as exc:
        raise SourcesConfigError(str(exc)) from exc
    raise SourcesConfigError("sources.yaml 顶层必须是列表或包含 documents 的对象")


def load_organization(path: str | Path) -> OrganizationConfig:
    raw = load_yaml(path)
    if not isinstance(raw, dict):
        raise OrganizationConfigError("organization.yaml 顶层必须是对象")
    try:
        return OrganizationConfig.model_validate(raw)
    except ValidationError as exc:
        raise OrganizationConfigError(str(exc)) from exc


def _detail_errors(
    label: str,
    detail: AudienceDetail | None,
    *,
    college_ids: set[str],
    major_colleges: dict[str, str],
) -> list[str]:
    if detail is None:
        return []
    errors: list[str] = []
    for college_id in detail.colleges:
        if college_id not in college_ids:
            errors.append(f"{label} 引用了未登记的学院：{college_id}")
    for major_id in detail.majors:
        college_id = major_colleges.get(major_id)
        if college_id is None:
            errors.append(f"{label} 引用了未登记的专业：{major_id}")
        elif detail.colleges and college_id not in detail.colleges:
            errors.append(f"{label} 的专业 {major_id} 不属于所列学院")
    return errors


def validate_organization_refs(
    config: SourcesConfig,
    organization: OrganizationConfig,
) -> list[str]:
    """检查 sources.yaml 中的院系/专业/上位文件引用是否自洽。"""
    college_ids = {college.college_id for college in organization.colleges}
    major_colleges = {major.major_id: major.college_id for major in organization.majors}
    doc_by_id = {document.doc_id: document for document in config.documents}
    errors: list[str] = []

    for document in config.documents:
        if document.issuer_college_id is not None and document.issuer_college_id not in college_ids:
            errors.append(f"{document.doc_id} 的发文学院未登记：{document.issuer_college_id}")

        errors.extend(
            _detail_errors(
                document.doc_id,
                document.audience_detail,
                college_ids=college_ids,
                major_colleges=major_colleges,
            )
        )

        for version in document.versions:
            if version.audience_detail is not None:
                errors.extend(
                    _detail_errors(
                        f"{document.doc_id}@{version.version_id}",
                        version.audience_detail,
                        college_ids=college_ids,
                        major_colleges=major_colleges,
                    )
                )

        if document.parent_doc_id is not None:
            parent = doc_by_id.get(document.parent_doc_id)
            if parent is None:
                errors.append(
                    f"{document.doc_id} 的 parent_doc_id 不存在：{document.parent_doc_id}"
                )
            elif (
                document.issuer_level is IssuerLevel.UNIVERSITY
                and parent.issuer_level is not IssuerLevel.UNIVERSITY
            ):
                errors.append(
                    f"{document.doc_id} 是校级文件，不能以院/系级文件 {parent.doc_id} 为上位文件"
                )

            seen = {document.doc_id}
            current: str | None = document.parent_doc_id
            while current is not None:
                if current in seen:
                    errors.append(f"{document.doc_id} 的 parent_doc_id 链存在循环")
                    break
                seen.add(current)
                parent = doc_by_id.get(current)
                current = parent.parent_doc_id if parent is not None else None

    if errors:
        return sorted(set(errors))
    return []


def load_retrieval_config(path: str | Path) -> RetrievalConfig:
    raw = load_yaml(path)
    if not isinstance(raw, dict):
        raise ValueError("retrieval.yaml 顶层必须是对象")
    expanded = expand_env(raw)
    if not expanded.get("embedding_model"):
        expanded["embedding_model"] = os.getenv("EMBEDDING_MODEL", "BAAI/bge-m3")
    return RetrievalConfig.model_validate(expanded)


def load_abstention_config(path: str | Path) -> AbstentionConfig:
    raw = load_yaml(path)
    if not isinstance(raw, dict):
        raise ValueError("abstention.yaml 顶层必须是对象")
    return AbstentionConfig.model_validate(raw)


def load_graph_config(path: str | Path) -> GraphConfig:
    raw = load_yaml(path)
    if not isinstance(raw, dict):
        raise ValueError("graph.yaml 顶层必须是对象")
    return GraphConfig.model_validate(raw)


def load_handoff_config(path: str | Path) -> HandoffConfig:
    raw = load_yaml(path)
    if not isinstance(raw, dict):
        raise ValueError("handoff.yaml 顶层必须是对象")
    return HandoffConfig.model_validate(raw)


def load_mcp_config(path: str | Path) -> MCPConfig:
    raw = load_yaml(path)
    if not isinstance(raw, dict):
        raise ValueError("mcp.yaml 顶层必须是对象")
    return MCPConfig.model_validate(expand_env(raw))


class SourceValidationReport(BaseModel):
    document_count: int
    version_count: int
    documents_without_dates: list[str] = Field(default_factory=list)
    versions_without_dates: list[str] = Field(default_factory=list)
    versions_without_source: list[str] = Field(default_factory=list)
    audience_values: list[str] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)

    @property
    def meets_w1_target(self) -> bool:
        return self.document_count >= 30 and self.version_count >= 50

    @property
    def ok(self) -> bool:
        return not self.errors
