"""policy-meta MCP server：只读文档/版本元数据。

W4 只注册有真实数据的工具：文档、版本、版本解析、文档列表。
flow/calendar 工具留到 W5，server 不注册返回空数组的伪工具。
"""

from __future__ import annotations

import logging
import os
import sys
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, Protocol

from fastmcp import FastMCP

from campusguide.domain.enums import Audience, DocType, VersionMode
from campusguide.domain.models import DocumentVersion
from campusguide.ingest.versioning import resolve_version
from campusguide.mcp.schemas import (
    DocumentSummary,
    ErrorCode,
    ResolveVersionData,
    ToolError,
    success,
    validate_date,
)

logger = logging.getLogger("campusguide.mcp.policy_meta")
ROOT = Path(os.getenv("CAMPUSGUIDE_ROOT", Path(__file__).resolve().parents[3]))


class MetaBackend(Protocol):
    def documents(self) -> dict[str, list[DocumentVersion]]: ...


@dataclass
class JsonlMetaBackend:
    path: Path

    def __post_init__(self) -> None:
        self._documents: dict[str, list[DocumentVersion]] | None = None
        self._error: str | None = None
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            self._error = f"缺少文档元数据文件：{self.path}"
            return
        documents: dict[str, list[DocumentVersion]] = {}
        try:
            with self.path.open("r", encoding="utf-8") as handle:
                for line_no, line in enumerate(handle, start=1):
                    if not line.strip():
                        continue
                    try:
                        version = DocumentVersion.model_validate_json(line)
                    except Exception as exc:
                        raise ValueError(
                            f"{self.path}:{line_no} 不是合法 DocumentVersion：{exc}"
                        ) from exc
                    documents.setdefault(version.doc_id, []).append(version)
        except Exception as exc:
            self._error = f"文档元数据加载失败：{exc}"
            return
        for versions in documents.values():
            versions.sort(key=lambda item: (item.effective_from, item.version_id))
        self._documents = documents

    def documents(self) -> dict[str, list[DocumentVersion]]:
        if self._documents is None:
            raise ToolError(
                ErrorCode.INDEX_UNAVAILABLE,
                self._error or "文档元数据不可用",
            )
        return self._documents


class PolicyMetaService:
    def __init__(self, backend: MetaBackend) -> None:
        self._backend = backend

    def _documents(self) -> dict[str, list[DocumentVersion]]:
        try:
            return self._backend.documents()
        except ToolError:
            raise
        except Exception as exc:
            raise ToolError(ErrorCode.INDEX_UNAVAILABLE, str(exc)) from exc

    def _versions(self, doc_id: str) -> list[DocumentVersion]:
        documents = self._documents()
        if not doc_id or not doc_id.strip():
            raise ToolError(ErrorCode.INVALID_ARGUMENT, "doc_id 不能为空")
        versions = documents.get(doc_id)
        if not versions:
            raise ToolError(ErrorCode.NOT_FOUND, f"未登记文档：{doc_id!r}")
        return versions

    @staticmethod
    def _dump_version(version: DocumentVersion) -> dict[str, Any]:
        payload = version.model_dump(mode="json")
        payload.pop("source_sha256", None)
        payload.pop("parser_version", None)
        payload.pop("fetched_at", None)
        return payload

    def get_document_versions(self, doc_id: str) -> dict[str, Any]:
        versions = self._versions(doc_id)
        data = {
            "doc_id": doc_id,
            "versions": [self._dump_version(version) for version in versions],
        }
        return success(data)

    def get_document(self, doc_id: str) -> dict[str, Any]:
        versions = self._versions(doc_id)
        latest = max(versions, key=lambda item: (item.effective_from, item.version_id))
        data = DocumentSummary(
            doc_id=latest.doc_id,
            title=latest.title,
            doc_type=latest.doc_type.value,
            issuer_level=latest.issuer_level.value,
            issuer_college_id=latest.issuer_college_id,
            issuing_department=latest.issuing_department,
            audience=[audience.value for audience in latest.audience],
            audience_detail=(
                latest.audience_detail.model_dump(mode="json") if latest.audience_detail else None
            ),
            version_count=len(versions),
            latest_version_id=latest.version_id,
            latest_effective_from=latest.effective_from,
            latest_status=latest.status.value,
        )
        return success(data.model_dump(mode="json"))

    def resolve_version(
        self,
        doc_id: str,
        as_of: str | None = None,
        mode: str = "current",
    ) -> dict[str, Any]:
        versions = self._versions(doc_id)
        parsed_as_of = validate_date(as_of, "as_of") or date.today()
        try:
            version_mode = VersionMode(mode)
        except ValueError as exc:
            raise ToolError(
                ErrorCode.INVALID_FILTER,
                f"mode 未知：{mode!r}；允许值：{[item.value for item in VersionMode]}",
                details={"field": "mode"},
            ) from exc
        resolution = resolve_version(versions, as_of=parsed_as_of, mode=version_mode)
        data = ResolveVersionData(
            doc_id=doc_id,
            as_of=parsed_as_of,
            mode=version_mode.value,
            selected_version_id=(resolution.selected.version_id if resolution.selected else None),
            reason_code=resolution.reason_code,
            candidate_version_ids=[item.version_id for item in resolution.candidates],
            warnings=list(resolution.warnings),
        )
        return success(data.model_dump(mode="json"), warnings=data.warnings)

    def list_documents(
        self,
        query: str | None = None,
        audience: str | None = None,
        doc_type: str | None = None,
        limit: int = 50,
    ) -> dict[str, Any]:
        if not 1 <= limit <= 200:
            raise ToolError(ErrorCode.INVALID_ARGUMENT, "limit 必须在 1..200")
        parsed_audience: Audience | None = None
        if audience:
            try:
                parsed_audience = Audience(audience)
            except ValueError as exc:
                raise ToolError(
                    ErrorCode.INVALID_FILTER,
                    f"audience 未知：{audience!r}",
                    details={"field": "audience"},
                ) from exc
        parsed_doc_type: DocType | None = None
        if doc_type:
            try:
                parsed_doc_type = DocType(doc_type)
            except ValueError as exc:
                raise ToolError(
                    ErrorCode.INVALID_FILTER,
                    f"doc_type 未知：{doc_type!r}",
                    details={"field": "doc_type"},
                ) from exc

        needle = (query or "").strip().lower()
        summaries: list[DocumentSummary] = []
        for doc_id, versions in sorted(self._documents().items()):
            latest = max(versions, key=lambda item: (item.effective_from, item.version_id))
            if needle and needle not in latest.title.lower() and needle not in doc_id.lower():
                continue
            if parsed_audience is not None:
                audiences = set(latest.audience)
                if Audience.ALL not in audiences and parsed_audience not in audiences:
                    continue
            if parsed_doc_type is not None and latest.doc_type is not parsed_doc_type:
                continue
            summaries.append(
                DocumentSummary(
                    doc_id=doc_id,
                    title=latest.title,
                    doc_type=latest.doc_type.value,
                    issuer_level=latest.issuer_level.value,
                    issuer_college_id=latest.issuer_college_id,
                    issuing_department=latest.issuing_department,
                    audience=[item.value for item in latest.audience],
                    audience_detail=(
                        latest.audience_detail.model_dump(mode="json")
                        if latest.audience_detail
                        else None
                    ),
                    version_count=len(versions),
                    latest_version_id=latest.version_id,
                    latest_effective_from=latest.effective_from,
                    latest_status=latest.status.value,
                )
            )
            if len(summaries) >= limit:
                break
        return success(
            {
                "documents": [item.model_dump(mode="json") for item in summaries],
                "count": len(summaries),
                "limit": limit,
            }
        )


def build_meta_server(service: PolicyMetaService) -> FastMCP:
    mcp = FastMCP("policy-meta")

    @mcp.tool(name="get_document_versions")
    def get_document_versions(doc_id: str) -> dict[str, Any]:
        """返回一个文档的全部版本状态、生效区间、来源与日期置信度。"""
        try:
            return service.get_document_versions(doc_id)
        except ToolError as exc:
            return exc.envelope()

    @mcp.tool(name="get_document")
    def get_document(doc_id: str) -> dict[str, Any]:
        """返回文档级元数据；不返回条款正文。"""
        try:
            return service.get_document(doc_id)
        except ToolError as exc:
            return exc.envelope()

    @mcp.tool(name="resolve_version")
    def resolve_version(
        doc_id: str,
        as_of: str | None = None,
        mode: str = "current",
    ) -> dict[str, Any]:
        """按 as_of 与 mode 解析唯一适用版本；冲突返回 reason_code。"""
        try:
            return service.resolve_version(doc_id, as_of, mode)
        except ToolError as exc:
            return exc.envelope()

    @mcp.tool(name="list_documents")
    def list_documents(
        query: str | None = None,
        audience: str | None = None,
        doc_type: str | None = None,
        limit: int = 50,
    ) -> dict[str, Any]:
        """按标题/ID/受众/类型列出文档元数据，不返回正文。"""
        try:
            return service.list_documents(query, audience, doc_type, limit)
        except ToolError as exc:
            return exc.envelope()

    return mcp


def create_service(*, root: Path = ROOT) -> PolicyMetaService:
    return PolicyMetaService(
        JsonlMetaBackend(root / "data" / "processed" / "documents.jsonl"),
    )


def main() -> None:
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO"),
        stream=sys.stderr,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    mcp = build_meta_server(create_service())
    logger.info("policy-meta MCP server starting (stdio)")
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
