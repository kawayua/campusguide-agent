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

from campusguide.domain.enums import ArticleStatus, Audience, DocType


class SourcesConfigError(ValueError):
    """来源清单不合法。"""


Slug = re.compile(r"^[a-z0-9][a-z0-9-]*$")


class SourceVersion(BaseModel):
    version_id: str
    url: HttpUrl | None = None
    local_path: str | None = None
    published_at: date | None = None
    effective_from: date | None = None
    effective_to: date | None = None
    status: ArticleStatus = ArticleStatus.ACTIVE
    format: Literal["auto", "html", "pdf"] = "auto"
    notes: str | None = None

    @model_validator(mode="after")
    def _validate_source(self) -> SourceVersion:
        if self.url is None and self.local_path is None:
            raise ValueError("url 与 local_path 至少填写一个")
        if self.local_path is not None and self.url is None:
            raise ValueError("local_path 只表示本地内容来源，仍需 url 作为引用链接")
        if self.effective_to and self.effective_from and self.effective_to < self.effective_from:
            raise ValueError("effective_to 不能早于 effective_from")
        return self


class SourceDocument(BaseModel):
    doc_id: str
    title: str
    doc_type: DocType = DocType.OTHER
    issuing_department: str
    audience: list[Audience] = Field(default_factory=lambda: [Audience.ALL])
    flow_ids: list[str] = Field(default_factory=list)
    topic_tags: list[str] = Field(default_factory=list)
    versions: list[SourceVersion] = Field(min_length=1)

    @model_validator(mode="after")
    def _validate_doc_id(self) -> SourceDocument:
        if not Slug.match(self.doc_id):
            raise ValueError(f"doc_id 必须是小写 slug（a-z0-9-）：{self.doc_id!r}")
        if not self.audience:
            raise ValueError("audience 不能为空")
        version_ids = [version.version_id for version in self.versions]
        if len(version_ids) != len(set(version_ids)):
            raise ValueError(f"{self.doc_id} 下存在重复 version_id")
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


class RetrievalConfig(BaseModel):
    collection: str
    alias: str
    embedding_model: str
    dense_vector_name: str = "dense"
    dense_vector_size: int = 1024
    sparse_vector_name: str = "sparse"
    recall_top_k: int = 30
    rerank_top_n: int = 8
    token_budget: int = 4000
    max_articles: int = 8
    min_rerank_score: float = 0.30
    min_score_margin: float = 0.03


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


def load_retrieval_config(path: str | Path) -> RetrievalConfig:
    raw = load_yaml(path)
    if not isinstance(raw, dict):
        raise ValueError("retrieval.yaml 顶层必须是对象")
    expanded = expand_env(raw)
    if not expanded.get("embedding_model"):
        expanded["embedding_model"] = os.getenv("EMBEDDING_MODEL", "BAAI/bge-m3")
    return RetrievalConfig.model_validate(expanded)


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
