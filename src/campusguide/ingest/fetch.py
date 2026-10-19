"""来源抓取：URL 下载或本地文件复制，生成 raw 快照与 meta.json。

设计约束：
- 只读取公开可访问页面；
- 快照写入 data/raw/{doc_id}/{version_id}/source.{html|pdf}；
- meta.json 记录 URL、抓取时间、Content-Type、状态码、SHA-256 与格式；
- 默认限速，避免对学校站点造成压力。
"""

from __future__ import annotations

import hashlib
import json
import mimetypes
import os
import time
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

import httpx

from campusguide.config import SourceDocument, SourcesConfig, SourceVersion

FORMAT = Literal["html", "pdf"]
FETCH_SCHEMA_VERSION = "1.0"
PARSER_VERSION = "campusguide-fetch-0.1.0"
MAX_BYTES = 50 * 1024 * 1024
DEFAULT_USER_AGENT = os.getenv(
    "CAMPUSGUIDE_USER_AGENT",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
)


class FetchError(RuntimeError):
    """单个来源抓取失败。"""


@dataclass
class FetchResult:
    doc_id: str
    version_id: str
    status: Literal["fetched", "copied", "skipped", "error"]
    source_path: Path | None = None
    meta_path: Path | None = None
    sha256: str | None = None
    byte_size: int = 0
    content_type: str | None = None
    final_url: str | None = None
    format: str | None = None
    warnings: list[str] = field(default_factory=list)
    error: str | None = None


def detect_format(
    explicit: str,
    *,
    url: str | None = None,
    local_path: str | None = None,
    content_type: str | None = None,
) -> FORMAT:
    if explicit in {"html", "pdf"}:
        return explicit  # type: ignore[return-value]

    candidates = []
    if url:
        candidates.append(url.split("?", 1)[0].lower())
    if local_path:
        candidates.append(local_path.lower())
    if content_type:
        candidates.append(content_type.lower())

    for candidate in candidates:
        if candidate.endswith(".pdf") or "application/pdf" in candidate:
            return "pdf"
        if (
            candidate.endswith((".html", ".htm", ".shtml", ".psp"))
            or "text/html" in candidate
            or "application/xhtml" in candidate
        ):
            return "html"

    raise FetchError("无法从 URL/文件扩展名/Content-Type 判断格式，请在 sources.yaml 指定 format")


def _version_dir(raw_dir: Path, doc_id: str, version_id: str) -> Path:
    return raw_dir / doc_id / version_id


def _atomic_write_bytes(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".part")
    temporary.write_bytes(content)
    os.replace(temporary, path)


def _atomic_write_json(path: Path, payload: dict[str, object]) -> None:
    text = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    _atomic_write_bytes(path, text.encode("utf-8"))


def _sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _selected(doc_id: str, version_id: str, only: set[str] | None) -> bool:
    if not only:
        return True
    return doc_id in only or f"{doc_id}@{version_id}" in only


def _request_with_retry(
    client: httpx.Client,
    url: str,
    *,
    timeout: float,
    retries: int = 1,
) -> httpx.Response:
    last_error: Exception | None = None
    for attempt in range(retries + 1):
        try:
            response = client.get(url, timeout=timeout, follow_redirects=True)
            if response.status_code >= 500 and attempt < retries:
                last_error = FetchError(f"HTTP {response.status_code}")
                time.sleep(1.0)
                continue
            return response
        except httpx.TransportError as exc:
            last_error = exc
            if attempt < retries:
                time.sleep(1.0)
                continue
    raise FetchError(f"请求失败：{last_error}") from last_error


def fetch_one(
    doc: SourceDocument,
    version: SourceVersion,
    *,
    raw_dir: Path,
    config_dir: Path,
    client: httpx.Client,
    timeout: float = 30.0,
    skip_existing: bool = False,
    local_base_dir: Path | None = None,
) -> FetchResult:
    destination_dir = _version_dir(raw_dir, doc.doc_id, version.version_id)
    meta_path = destination_dir / "meta.json"

    if skip_existing and meta_path.exists():
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            source_path = destination_dir / str(meta.get("source_file", ""))
            if source_path.exists():
                return FetchResult(
                    doc_id=doc.doc_id,
                    version_id=version.version_id,
                    status="skipped",
                    source_path=source_path,
                    meta_path=meta_path,
                    sha256=meta.get("sha256"),
                    byte_size=int(meta.get("byte_size", 0)),
                    content_type=meta.get("content_type"),
                    final_url=meta.get("final_url"),
                    format=meta.get("format"),
                    warnings=["已存在快照，按 --skip-existing 跳过"],
                )
        except (OSError, ValueError):
            pass

    warnings: list[str] = []
    http_status: int | None = None
    final_url: str | None = None
    content_type: str | None = None
    local_path_value: str | None = None

    try:
        if version.url is not None:
            response = _request_with_retry(client, str(version.url), timeout=timeout)
            http_status = response.status_code
            final_url = str(response.url)
            content_type = response.headers.get("content-type")
            if response.status_code != 200:
                raise FetchError(f"HTTP {response.status_code}: {final_url}")
            content = response.content
            if len(content) > MAX_BYTES:
                raise FetchError(f"文件超过 {MAX_BYTES // (1024 * 1024)} MB 限制")
            source_name = str(version.url)
        else:
            assert version.local_path is not None
            local_path_value = version.local_path
            local_path = Path(version.local_path)
            if not local_path.is_absolute():
                local_path = (local_base_dir or config_dir) / local_path
            if not local_path.exists():
                raise FetchError(f"本地文件不存在：{local_path}")
            content = local_path.read_bytes()
            content_type = mimetypes.guess_type(local_path.name)[0]
            source_name = version.local_path

        source_format = detect_format(
            version.format,
            url=str(version.url) if version.url else None,
            local_path=local_path_value,
            content_type=content_type,
        )
        extension = "pdf" if source_format == "pdf" else "html"
        source_path = destination_dir / f"source.{extension}"
        _atomic_write_bytes(source_path, content)

        digest = _sha256(content)
        meta = {
            "schema_version": FETCH_SCHEMA_VERSION,
            "doc_id": doc.doc_id,
            "version_id": version.version_id,
            "title": doc.title,
            "source_url": str(version.url) if version.url else None,
            "local_path": local_path_value,
            "final_url": final_url,
            "http_status": http_status,
            "content_type": content_type,
            "format": source_format,
            "source_file": source_path.name,
            "sha256": digest,
            "byte_size": len(content),
            "fetched_at": datetime.now(UTC).isoformat(timespec="seconds"),
            "parser_version": PARSER_VERSION,
            "source_name": source_name,
            "notes": version.notes,
            "warnings": warnings,
        }
        _atomic_write_json(meta_path, meta)

        return FetchResult(
            doc_id=doc.doc_id,
            version_id=version.version_id,
            status="fetched" if version.url is not None else "copied",
            source_path=source_path,
            meta_path=meta_path,
            sha256=digest,
            byte_size=len(content),
            content_type=content_type,
            final_url=final_url,
            format=source_format,
            warnings=warnings,
        )
    except (FetchError, OSError, httpx.HTTPError) as exc:
        return FetchResult(
            doc_id=doc.doc_id,
            version_id=version.version_id,
            status="error",
            error=str(exc),
        )


def fetch_corpus(
    config: SourcesConfig,
    *,
    raw_dir: Path,
    config_dir: Path,
    client: httpx.Client | None = None,
    only: Iterable[str] | None = None,
    skip_existing: bool = False,
    timeout: float = 30.0,
    delay_seconds: float = 1.0,
    local_base_dir: Path | None = None,
) -> list[FetchResult]:
    only_set = set(only) if only else None
    owns_client = client is None
    if client is None:
        client = httpx.Client(headers={"User-Agent": DEFAULT_USER_AGENT})
    results: list[FetchResult] = []
    try:
        for doc in config.documents:
            for version in doc.versions:
                if not _selected(doc.doc_id, version.version_id, only_set):
                    continue
                result = fetch_one(
                    doc,
                    version,
                    raw_dir=raw_dir,
                    config_dir=config_dir,
                    client=client,
                    timeout=timeout,
                    skip_existing=skip_existing,
                    local_base_dir=local_base_dir,
                )
                results.append(result)
                if result.status in {"fetched", "copied"} and delay_seconds > 0:
                    time.sleep(delay_seconds)
    finally:
        if owns_client:
            client.close()
    return results
