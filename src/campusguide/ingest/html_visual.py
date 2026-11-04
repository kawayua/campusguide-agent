"""Offline HTML preparation for visual ingestion.

This module owns the HTML-specific boundary: decode and clean with the same
rules as ``parse.py``, localize official same-domain images into the document,
and print the cleaned page with an isolated local Edge process. It never calls
a vision model and never marks image content as recognized.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import io
import json
import os
import re
import subprocess
import tempfile
import time
import uuid
from collections.abc import Callable, Mapping
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urljoin, urlsplit, urlunsplit

import httpx
from bs4 import BeautifulSoup, FeatureNotFound, Tag

from campusguide.ingest.parse import _decode_html, _extract_content, _remove_unwanted
from campusguide.ingest.structure import AssetResource, dom_path, extract_html_assets

DEFAULT_EDGE_PATH = "C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe"
DEFAULT_CACHE_DIR = Path("data/processed/html_render")
DEFAULT_ALLOWED_HOSTS: tuple[str, ...] = ("scau.edu.cn",)
DEFAULT_MAX_BYTES = 20 * 1024 * 1024
DEFAULT_REQUEST_TIMEOUT = 20.0
DEFAULT_REDIRECT_LIMIT = 3
DEFAULT_RENDER_TIMEOUT = 90.0

__all__ = [
    "HtmlRenderBundle",
    "HtmlRenderError",
    "HtmlRenderOptions",
    "HtmlResource",
    "build_edge_command",
    "prepare_html_render",
]

_RESOURCE_SCHEMA = "campusguide.html_resources.v1"
_MAX_ATTEMPTS = 2
_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
_IMAGE_REF_ATTRS = (
    "src",
    "data-src",
    "data-original",
    "data-lazy-src",
    "data-lazy",
    "data-url",
)
_EVENT_ATTR_RE = re.compile(r"on[a-z]+", re.IGNORECASE)
_CSS_IMPORT_RE = re.compile(r"@import\b[^;]*;?", re.IGNORECASE)
_CSS_URL_RE = re.compile(r"url\(\s*(['\"]?)(.*?)\1\s*\)", re.IGNORECASE | re.DOTALL)
_HTML_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")
_USERINFO_RE = re.compile(r"(https?://)[^/\s@]+@", re.IGNORECASE)
_MISSING_IMAGE_URI = (
    "data:image/svg+xml;base64,"
    + base64.b64encode(
        (
            '<svg xmlns="http://www.w3.org/2000/svg" width="320" height="72">'
            '<rect width="100%" height="100%" fill="#f4f4f4" stroke="#b0b0b0"/>'
            '<text x="12" y="42" font-size="18" fill="#666">[图片资源缺失]</text>'
            "</svg>"
        ).encode()
    ).decode("ascii")
)


class HtmlRenderError(RuntimeError):
    """Raised when the local Edge renderer cannot produce a valid PDF."""


@dataclass(frozen=True, slots=True)
class HtmlRenderOptions:
    edge_path: str = DEFAULT_EDGE_PATH
    cache_dir: Path = DEFAULT_CACHE_DIR
    resource_cache_dir: Path | None = None
    resource_manifest: Path | None = None
    max_bytes: int = DEFAULT_MAX_BYTES
    request_timeout: float = DEFAULT_REQUEST_TIMEOUT
    redirect_limit: int = DEFAULT_REDIRECT_LIMIT
    render_timeout: float = DEFAULT_RENDER_TIMEOUT
    allowed_hosts: tuple[str, ...] = DEFAULT_ALLOWED_HOSTS
    sleeper: Callable[[float], None] | None = field(default=None, repr=False, compare=False)


@dataclass(frozen=True, slots=True)
class HtmlResource:
    """Audit record: ``ref`` is the source attribute name, ``original_ref`` its raw value."""

    asset_id: str
    dom_path: str
    ref: str
    original_ref: str
    url: str
    final_url: str | None = None
    status: Literal["embedded", "failed"] = "failed"
    http_status: int | None = None
    byte_size: int = 0
    sha256: str | None = None
    content_type: str | None = None
    image_format: str | None = None
    local_path: str | None = None
    cache_path: str | None = None
    source_manifest: str | None = None
    attempts: int = 0
    cooldown_seconds: float = 0.0
    error: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "asset_id": self.asset_id,
            "dom_path": self.dom_path,
            "ref": self.ref,
            "original_ref": self.original_ref,
            "url": self.url,
            "final_url": self.final_url,
            "status": self.status,
            "http_status": self.http_status,
            "bytes": self.byte_size,
            "sha256": self.sha256,
            "content_type": self.content_type,
            "image_format": self.image_format,
            "local_path": self.local_path,
            "cache_path": self.cache_path,
            "source_manifest": self.source_manifest,
            "attempts": self.attempts,
            "cooldown_seconds": self.cooldown_seconds,
            "error": self.error,
        }


@dataclass(slots=True)
class HtmlRenderBundle:
    pdf_path: Path
    resources: list[HtmlResource]
    manifest_path: Path
    warnings: list[str]
    assets: list[AssetResource]
    clean_html_sha256: str


@dataclass(frozen=True, slots=True)
class _ImageInfo:
    image_format: str
    mime: str
    extension: str


@dataclass(frozen=True, slots=True)
class _ManifestHit:
    data: bytes
    local_path: Path
    content_type: str
    final_url: str
    info: _ImageInfo
    sha256: str
    source_manifest: Path


@dataclass(frozen=True, slots=True)
class _FetchOutcome:
    data: bytes
    final_url: str
    http_status: int
    content_type: str
    info: _ImageInfo
    attempts: int
    cooldown_seconds: float


class _ResourceFailure(Exception):
    def __init__(
        self,
        message: str,
        *,
        http_status: int | None = None,
        final_url: str | None = None,
        attempts: int = 0,
        cooldown_seconds: float = 0.0,
    ) -> None:
        super().__init__(message)
        self.http_status = http_status
        self.final_url = final_url
        self.attempts = attempts
        self.cooldown_seconds = cooldown_seconds


def prepare_html_render(
    source_path: Path,
    options: HtmlRenderOptions,
    *,
    doc_id: str = "",
    version_id: str = "",
    client: httpx.Client | None = None,
) -> HtmlRenderBundle:
    """Clean, localize and print one HTML source without exposing Edge to the web."""
    _validate_options(options)
    source_path = Path(source_path).expanduser().resolve()
    if not source_path.is_file():
        raise FileNotFoundError(f"HTML 源文件不存在：{source_path}")

    cache_root = Path(options.cache_dir).expanduser().resolve()
    cache_root.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    work_dir = cache_root / f"html-{stamp}-{uuid.uuid4().hex[:10]}"
    work_dir.mkdir(parents=True, exist_ok=False)

    warnings: list[str] = []
    meta, meta_warnings = _read_source_meta(source_path.parent / "meta.json")
    warnings.extend(meta_warnings)
    base_url = _base_url_from_meta(meta)
    if base_url is None and meta:
        warnings.append("meta.json 中未找到可用的页面 URL，相对资源无法解析")

    raw = source_path.read_bytes()
    html, encoding, decode_warnings = _decode_html(raw, None)
    warnings.extend(decode_warnings)
    try:
        soup = BeautifulSoup(html, "lxml")
    except FeatureNotFound:
        soup = BeautifulSoup(html, "html.parser")
    _remove_unwanted(soup)
    content, content_warnings = _extract_content(soup)
    warnings.extend(content_warnings)
    _scrub_for_offline(content)

    document_doc_id = doc_id or "doc"
    document_version_id = version_id or "version"
    assets, asset_warnings = extract_html_assets(
        content,
        doc_id=document_doc_id,
        version_id=document_version_id,
        root=content,
    )
    warnings.extend(asset_warnings)

    manifest_records: dict[str, list[dict[str, Any]]] = {}
    source_manifest: Path | None = None
    if options.resource_manifest is not None:
        source_manifest = Path(options.resource_manifest).expanduser().resolve()
        manifest_records, manifest_warnings = _load_resource_manifest(source_manifest)
        warnings.extend(manifest_warnings)

    if options.resource_cache_dir is not None:
        resource_dir = Path(options.resource_cache_dir).expanduser().resolve()
    else:
        resource_dir = work_dir / "resources"
    resource_dir.mkdir(parents=True, exist_ok=True)

    if client is None:
        with httpx.Client(
            follow_redirects=False,
            timeout=httpx.Timeout(options.request_timeout),
            trust_env=False,
            headers={"User-Agent": "campusguide-html-resources/1.0"},
        ) as owned_client:
            resources = _localize_images(
                content,
                assets=assets,
                base_url=base_url,
                doc_id=document_doc_id,
                version_id=document_version_id,
                options=options,
                manifest_records=manifest_records,
                source_manifest=source_manifest,
                resource_dir=resource_dir,
                client=owned_client,
                warnings=warnings,
            )
    else:
        resources = _localize_images(
            content,
            assets=assets,
            base_url=base_url,
            doc_id=document_doc_id,
            version_id=document_version_id,
            options=options,
            manifest_records=manifest_records,
            source_manifest=source_manifest,
            resource_dir=resource_dir,
            client=client,
            warnings=warnings,
        )

    clean_html = _serialize_clean_html(content)
    clean_bytes = clean_html.encode("utf-8")
    clean_path = work_dir / "clean.html"
    clean_path.write_bytes(clean_bytes)
    clean_sha256 = _sha256_bytes(clean_bytes)
    manifest_path = work_dir / "resources_manifest.json"
    pdf_path = work_dir / "rendered.pdf"
    payload: dict[str, Any] = {
        "schema": _RESOURCE_SCHEMA,
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "doc_id": document_doc_id,
        "version_id": document_version_id,
        "source_path": str(source_path),
        "html_encoding": encoding,
        "base_url": base_url,
        "clean_html_sha256": clean_sha256,
        "warnings": warnings,
        "resources": [resource.as_dict() for resource in resources],
        "assets": [asset.model_dump(mode="json") for asset in assets],
        "render": {"status": "pending", "pdf_path": str(pdf_path)},
    }
    _write_manifest(manifest_path, payload)

    try:
        _render_html_to_pdf(clean_path, pdf_path, options)
    except Exception as exc:
        error = _safe_error_text(f"{type(exc).__name__}: {exc}")
        payload["render"] = {
            "status": "failed",
            "pdf_path": str(pdf_path),
            "error": error,
        }
        _write_manifest(manifest_path, payload)
        if isinstance(exc, HtmlRenderError):
            raise
        raise HtmlRenderError(f"Edge HTML 渲染失败：{error}") from exc

    pdf_bytes = pdf_path.read_bytes()
    payload["render"] = {
        "status": "ok",
        "pdf_path": str(pdf_path),
        "bytes": len(pdf_bytes),
        "sha256": _sha256_bytes(pdf_bytes),
    }
    _write_manifest(manifest_path, payload)
    return HtmlRenderBundle(
        pdf_path=pdf_path,
        resources=resources,
        manifest_path=manifest_path,
        warnings=warnings,
        assets=assets,
        clean_html_sha256=clean_sha256,
    )


def build_edge_command(
    *,
    edge_path: str,
    html_path: Path,
    pdf_path: Path,
    profile_dir: Path,
) -> list[str]:
    """Build the deterministic offline Edge command used by the renderer."""
    html_path = Path(html_path).resolve()
    pdf_path = Path(pdf_path).resolve()
    profile_dir = Path(profile_dir).resolve()
    return [
        edge_path,
        "--headless=new",
        "--disable-gpu",
        "--disable-extensions",
        "--disable-javascript",
        "--disable-background-networking",
        "--disable-component-update",
        "--disable-sync",
        "--disable-domain-reliability",
        "--disable-default-apps",
        "--no-first-run",
        "--no-default-browser-check",
        "--no-pdf-header-footer",
        f"--user-data-dir={profile_dir}",
        "--host-resolver-rules=MAP * ~NOTFOUND",
        f"--print-to-pdf={pdf_path}",
        html_path.as_uri(),
    ]


def _validate_options(options: HtmlRenderOptions) -> None:
    if not str(options.cache_dir).strip():
        raise ValueError("cache_dir 不能为空")
    if options.max_bytes <= 0:
        raise ValueError("max_bytes 必须大于 0")
    if options.request_timeout <= 0:
        raise ValueError("request_timeout 必须大于 0")
    if options.redirect_limit < 0:
        raise ValueError("redirect_limit 不能小于 0")
    if options.render_timeout <= 0:
        raise ValueError("render_timeout 必须大于 0")


def _read_source_meta(path: Path) -> tuple[dict[str, Any], list[str]]:
    if not path.is_file():
        return {}, [f"未找到 {path.name}，相对资源无法解析页面基址"]
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        return {}, [f"读取 {path.name} 失败，已忽略页面基址：{type(exc).__name__}"]
    if not isinstance(data, dict):
        return {}, [f"{path.name} 顶层不是对象，已忽略页面基址"]
    return data, []


def _base_url_from_meta(meta: Mapping[str, Any]) -> str | None:
    if not meta:
        return None
    for key in ("final_url", "source_url", "source"):
        candidate = _meta_url(meta.get(key))
        if candidate:
            return candidate
    for key, value in meta.items():
        lower = str(key).lower()
        if lower in {"doc_id", "version_id", "id", "sha256", "hash", "title"}:
            continue
        if not any(token in lower for token in ("url", "link", "href", "source")):
            continue
        candidate = _meta_url(value)
        if candidate:
            return candidate
    return None


def _meta_url(value: Any, depth: int = 0) -> str | None:
    if isinstance(value, str):
        text = value.strip()
        return text if _http_url_parts(text) is not None else None
    if isinstance(value, Mapping) and depth < 2:
        for key in ("final_url", "source_url", "source", "url", "href"):
            candidate = _meta_url(value.get(key), depth + 1)
            if candidate:
                return candidate
    return None


def _http_url_parts(value: str) -> Any | None:
    text = value.strip()
    if not text or _HTML_CONTROL_RE.search(text):
        return None
    try:
        parts = urlsplit(text)
        _ = parts.port
        host = parts.hostname
    except ValueError:
        return None
    if parts.scheme.lower() not in {"http", "https"}:
        return None
    if not host:
        return None
    if "@" in parts.netloc or parts.username is not None or parts.password is not None:
        return None
    return parts


def _allowed_hosts(options: HtmlRenderOptions) -> tuple[str, ...]:
    normalized: list[str] = []
    for host in options.allowed_hosts:
        clean = str(host).strip().lower().lstrip(".").rstrip(".")
        if clean:
            normalized.append(clean)
    return tuple(normalized)


def _host_allowed(host: str, allowed_hosts: tuple[str, ...]) -> bool:
    host = host.lower().rstrip(".")
    return any(host == allowed or host.endswith(f".{allowed}") for allowed in allowed_hosts)


def _validate_resource_url(url: str, allowed_hosts: tuple[str, ...]) -> str:
    parts = _http_url_parts(url)
    if parts is None:
        raise _ResourceFailure("资源 URL 不是有效的官方 http(s) 地址，或包含 userinfo")
    host = (parts.hostname or "").lower().rstrip(".")
    if not _host_allowed(host, allowed_hosts):
        raise _ResourceFailure("资源主机不在官方允许列表")
    return urlunsplit((parts.scheme.lower(), parts.netloc, parts.path, parts.query, ""))


def _resolve_resource_url(ref: str, base_url: str | None) -> str:
    if ref.startswith("//"):
        if base_url is None:
            raise _ResourceFailure("相对资源缺少页面基址")
        scheme = urlsplit(base_url).scheme.lower()
        if scheme not in {"http", "https"}:
            raise _ResourceFailure("页面基址不是 http(s)")
        return f"{scheme}:{ref}"
    if urlsplit(ref).scheme:
        return ref
    if base_url is None:
        raise _ResourceFailure("相对资源缺少页面基址")
    return urljoin(base_url, ref)


def _normalize_url_key(url: str) -> str:
    text = url.strip()
    try:
        parts = urlsplit(text)
    except ValueError:
        return text
    return urlunsplit(
        (
            parts.scheme.lower(),
            parts.netloc.lower(),
            parts.path,
            parts.query,
            "",
        )
    )


def _normalize_mime(value: str | None) -> str | None:
    if not value:
        return None
    mime = value.split(";", 1)[0].strip().lower()
    aliases = {
        "image/jpg": "image/jpeg",
        "image/pjpeg": "image/jpeg",
        "image/x-png": "image/png",
        "image/vnd.microsoft.icon": "image/x-icon",
    }
    return aliases.get(mime, mime)


def _detect_image(data: bytes) -> _ImageInfo | None:
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return _ImageInfo("png", "image/png", "png")
    if data.startswith(b"\xff\xd8\xff"):
        return _ImageInfo("jpeg", "image/jpeg", "jpg")
    if data[:6] in {b"GIF87a", b"GIF89a"}:
        return _ImageInfo("gif", "image/gif", "gif")
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return _ImageInfo("webp", "image/webp", "webp")
    if data.startswith(b"BM"):
        return _ImageInfo("bmp", "image/bmp", "bmp")
    if data.startswith(b"\x00\x00\x01\x00"):
        return _ImageInfo("ico", "image/x-icon", "ico")
    if data.startswith((b"II*\x00", b"MM\x00*")):
        return _ImageInfo("tiff", "image/tiff", "tiff")
    head = data[:4096].lstrip(b"\xef\xbb\xbf \t\r\n")
    lowered = head.lower()
    if lowered.startswith(b"<svg") or (lowered.startswith(b"<?xml") and b"<svg" in lowered):
        _validate_svg(data)
        return _ImageInfo("svg", "image/svg+xml", "svg")
    return None


def _validate_svg(data: bytes) -> None:
    text = data.decode("utf-8", errors="replace").lower()
    unsafe_tokens = (
        "<script",
        "<foreignobject",
        "<!entity",
        'href="http',
        "href='http",
        'xlink:href="//',
        "xlink:href='//",
        "url(http",
    )
    if any(token in text for token in unsafe_tokens) or re.search(r"\bon[a-z]+\s*=", text):
        raise _ResourceFailure("SVG 含脚本或外链，已拒绝")


def _validate_image_bytes(
    data: bytes,
    content_type: str | None,
    *,
    max_bytes: int,
    require_content_type: bool,
) -> _ImageInfo:
    if not data:
        raise _ResourceFailure("图片内容为空")
    if len(data) > max_bytes:
        raise _ResourceFailure(f"图片超过大小上限 {max_bytes} bytes")
    header_mime = _normalize_mime(content_type)
    if header_mime is None and require_content_type:
        raise _ResourceFailure("响应缺少图片 Content-Type")
    info = _detect_image(data)
    if info is None:
        raise _ResourceFailure("内容不是可识别的图片格式")
    if header_mime is not None:
        if not header_mime.startswith("image/"):
            raise _ResourceFailure(f"内容类型不是图片：{header_mime}")
        if header_mime != info.mime:
            raise _ResourceFailure(f"Content-Type 与实际图片格式不符：{header_mime} vs {info.mime}")
    _verify_with_pillow(data, info)
    return info


def _verify_with_pillow(data: bytes, info: _ImageInfo) -> None:
    if info.image_format == "svg":
        return
    try:
        from PIL import Image
    except ImportError:  # pragma: no cover - Pillow is present in production
        return
    try:
        with Image.open(io.BytesIO(data)) as image:
            image.verify()
    except Exception as exc:
        raise _ResourceFailure(f"图像解码失败：{type(exc).__name__}") from exc


def _decode_data_uri(value: str) -> tuple[bytes, str]:
    match = re.match(r"^data:([^;,]+);base64,(.*)$", value, re.IGNORECASE | re.DOTALL)
    if match is None:
        raise _ResourceFailure("不支持的 data URI 编码")
    payload = re.sub(r"\s+", "", match.group(2))
    try:
        data = base64.b64decode(payload, validate=True)
    except (ValueError, binascii.Error) as exc:
        raise _ResourceFailure("data URI base64 解码失败") from exc
    return data, match.group(1).strip()


def _load_resource_manifest(path: Path) -> tuple[dict[str, list[dict[str, Any]]], list[str]]:
    if not path.is_file():
        return {}, [f"资源清单不存在：{path.name}"]
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        return {}, [f"资源清单读取失败：{type(exc).__name__}"]
    grouped: dict[str, list[dict[str, Any]]] = {}
    records = _manifest_records(data)
    if not records:
        return {}, ["资源清单未包含可识别的资源记录"]
    for raw_record in records:
        record = _normalize_manifest_record(raw_record)
        url = record.get("url")
        if not isinstance(url, str) or not url.strip():
            continue
        grouped.setdefault(_normalize_url_key(url), []).append(record)
    if not grouped:
        return {}, ["资源清单未包含可用的 URL 记录"]
    return grouped, []


def _manifest_records(data: Any) -> list[Mapping[str, Any]]:
    if isinstance(data, list):
        return [item for item in data if isinstance(item, Mapping)]
    if not isinstance(data, Mapping):
        return []
    records: list[Mapping[str, Any]] = []
    for key in ("resources", "items", "images", "files", "downloads", "entries", "assets"):
        value = data.get(key)
        if isinstance(value, list):
            records.extend(item for item in value if isinstance(item, Mapping))
        elif isinstance(value, Mapping):
            for url, item in value.items():
                if isinstance(item, Mapping):
                    merged = dict(item)
                    merged.setdefault("url", url)
                    records.append(merged)
    if records:
        return records
    for key, value in data.items():
        if isinstance(key, str) and isinstance(value, Mapping) and _http_url_parts(key):
            merged = dict(value)
            merged.setdefault("url", key)
            records.append(merged)
    return records


def _normalize_manifest_record(record: Mapping[str, Any]) -> dict[str, Any]:
    normalized = {str(key).lower(): value for key, value in record.items()}
    url = _first_value(normalized, ("url", "original_url", "source_url", "request_url", "final_url"))
    sha256 = _first_value(normalized, ("sha256", "sha", "hash"))
    local_path = _first_value(
        normalized,
        ("local_path", "cache_path", "path", "file", "downloaded_path", "local_file"),
    )
    final_url = _first_value(normalized, ("final_url", "url"))
    content_type = _first_value(normalized, ("content_type", "mime", "media_type"))
    byte_size = _first_value(normalized, ("bytes", "byte_size", "size", "content_length"))
    status = _first_value(normalized, ("status", "state"))
    return {
        **normalized,
        "url": str(url).strip() if url is not None else None,
        "sha256": str(sha256).strip().lower() if sha256 is not None else None,
        "local_path": str(local_path).strip() if local_path is not None else None,
        "final_url": str(final_url).strip() if final_url is not None else None,
        "content_type": str(content_type).strip() if content_type is not None else None,
        "byte_size": _optional_int(byte_size),
        "status": str(status).strip().lower() if status is not None else None,
    }


def _first_value(record: Mapping[str, Any], keys: tuple[str, ...]) -> Any:
    for key in keys:
        value = record.get(key)
        if value not in (None, ""):
            return value
    return None


def _optional_int(value: Any) -> int | None:
    if value in (None, ""):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _manifest_local_path(
    record: Mapping[str, Any],
    source_manifest: Path,
    resource_cache_dir: Path | None,
) -> Path | None:
    raw = record.get("local_path")
    if not isinstance(raw, str) or not raw:
        return None
    candidate = Path(raw)
    if candidate.is_absolute():
        return candidate
    for base in (source_manifest.parent, resource_cache_dir):
        if base is None:
            continue
        resolved = base / candidate
        if resolved.is_file():
            return resolved
    return source_manifest.parent / candidate


def _lookup_manifest(
    url: str,
    records: dict[str, list[dict[str, Any]]],
    *,
    source_manifest: Path,
    resource_cache_dir: Path | None,
    max_bytes: int,
) -> tuple[_ManifestHit | None, str | None]:
    candidates = records.get(_normalize_url_key(url))
    if not candidates:
        return None, None
    issue: str | None = None
    for record in candidates:
        if record.get("status") in {"failed", "error", "missing", "invalid"}:
            continue
        expected_sha = record.get("sha256")
        if not isinstance(expected_sha, str) or not expected_sha:
            issue = issue or "清单记录缺少 SHA-256"
            continue
        local_path = _manifest_local_path(record, source_manifest, resource_cache_dir)
        if local_path is None or not local_path.is_file():
            issue = issue or "清单缓存文件不存在"
            continue
        try:
            size = local_path.stat().st_size
            if size > max_bytes:
                raise _ResourceFailure(f"清单缓存超过大小上限 {max_bytes} bytes")
            expected_bytes = record.get("byte_size")
            if isinstance(expected_bytes, int) and size != expected_bytes:
                raise _ResourceFailure("清单缓存字节数不匹配")
            data = local_path.read_bytes()
            digest = _sha256_bytes(data)
            if digest != expected_sha:
                raise _ResourceFailure("清单缓存 SHA-256 不匹配")
            info = _validate_image_bytes(
                data,
                record.get("content_type"),
                max_bytes=max_bytes,
                require_content_type=False,
            )
            final_url = record.get("final_url") or url
            return (
                _ManifestHit(
                    data=data,
                    local_path=local_path,
                    content_type=info.mime,
                    final_url=str(final_url),
                    info=info,
                    sha256=digest,
                    source_manifest=source_manifest,
                ),
                None,
            )
        except (OSError, _ResourceFailure) as exc:
            issue = issue or str(exc) or type(exc).__name__
    return None, issue or "清单缓存未通过校验"


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sha256_path(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _store_resource(data: bytes, info: _ImageInfo, target_dir: Path) -> Path:
    digest = _sha256_bytes(data)
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / f"{digest}.{info.extension}"
    if target.is_file():
        with suppress(OSError):
            if target.stat().st_size == len(data) and _sha256_path(target) == digest:
                return target
    temporary = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_bytes(data)
        temporary.replace(target)
    except OSError:
        with suppress(OSError):
            temporary.unlink(missing_ok=True)
        raise
    return target


def _image_ref(image: Tag) -> tuple[str, str]:
    for attr in _IMAGE_REF_ATTRS:
        value = image.get(attr)
        if value is None:
            continue
        text = str(value).strip()
        if text:
            return attr, text
    return "", ""


def _embed_image(image: Tag, *, asset_id: str, info: _ImageInfo, data: bytes, digest: str) -> None:
    image["src"] = f"data:{info.mime};base64,{base64.b64encode(data).decode('ascii')}"
    image["data-asset-id"] = asset_id
    image["data-resource-sha256"] = digest
    image["data-resource-status"] = "embedded"
    image["loading"] = "eager"
    for attr in _IMAGE_REF_ATTRS:
        if attr != "src":
            image.attrs.pop(attr, None)
    image.attrs.pop("srcset", None)
    image.attrs.pop("sizes", None)


def _mark_missing_image(image: Tag, *, asset_id: str) -> None:
    image["src"] = _MISSING_IMAGE_URI
    image["data-asset-id"] = asset_id
    image["data-resource-status"] = "missing"
    image["loading"] = "eager"
    if not image.get("title"):
        image["title"] = "图片资源缺失"
    for attr in _IMAGE_REF_ATTRS:
        if attr != "src":
            image.attrs.pop(attr, None)
    image.attrs.pop("srcset", None)
    image.attrs.pop("sizes", None)


def _localize_images(
    content: Tag,
    *,
    assets: list[AssetResource],
    base_url: str | None,
    doc_id: str,
    version_id: str,
    options: HtmlRenderOptions,
    manifest_records: dict[str, list[dict[str, Any]]],
    source_manifest: Path | None,
    resource_dir: Path,
    client: httpx.Client,
    warnings: list[str],
) -> list[HtmlResource]:
    asset_by_dom: dict[str, AssetResource] = {}
    for asset in assets:
        if asset.kind not in {"image", "table_image"}:
            continue
        locator_path = getattr(asset.locator, "dom_path", "")
        if locator_path:
            asset_by_dom[locator_path] = asset

    allowed_hosts = _allowed_hosts(options)
    sleeper = options.sleeper or time.sleep
    resources: list[HtmlResource] = []
    for index, image in enumerate(content.find_all("img"), start=1):
        image_dom_path = dom_path(image, content)
        native_asset = asset_by_dom.get(image_dom_path)
        asset_id = (
            native_asset.asset_id
            if native_asset is not None
            else f"{doc_id}@{version_id}#resource{index}"
        )
        ref_attr, ref = _image_ref(image)
        if not ref:
            message = "图片缺少可用 URL"
            resources.append(
                HtmlResource(
                    asset_id=asset_id,
                    dom_path=image_dom_path,
                    ref="",
                    original_ref="",
                    url="",
                    status="failed",
                    error=message,
                )
            )
            _mark_missing_image(image, asset_id=asset_id)
            warnings.append(f"{asset_id} 图片资源缺失：{message}")
            continue

        url = ref
        final_url: str | None = None
        http_status: int | None = None
        attempts = 0
        cooldown_seconds = 0.0
        local_path: str | None = None
        source_manifest_used: str | None = None
        try:
            if ref.lower().startswith("data:"):
                data, declared_mime = _decode_data_uri(ref)
                info = _validate_image_bytes(
                    data,
                    declared_mime,
                    max_bytes=options.max_bytes,
                    require_content_type=True,
                )
                url = f"data:{info.mime};base64"
                content_type = info.mime
            else:
                url = _validate_resource_url(
                    _resolve_resource_url(ref, base_url),
                    allowed_hosts,
                )
                hit: _ManifestHit | None = None
                if manifest_records and source_manifest is not None:
                    hit, manifest_issue = _lookup_manifest(
                        url,
                        manifest_records,
                        source_manifest=source_manifest,
                        resource_cache_dir=(
                            Path(options.resource_cache_dir).expanduser().resolve()
                            if options.resource_cache_dir is not None
                            else None
                        ),
                        max_bytes=options.max_bytes,
                    )
                    if manifest_issue:
                        warnings.append(f"{asset_id} 资源清单缓存未通过校验：{manifest_issue}")
                if hit is not None:
                    data = hit.data
                    info = hit.info
                    content_type = hit.content_type
                    final_url = hit.final_url
                    local_path = str(hit.local_path)
                    source_manifest_used = str(hit.source_manifest)
                else:
                    outcome = _fetch_remote(
                        url,
                        options=options,
                        allowed_hosts=allowed_hosts,
                        client=client,
                        sleeper=sleeper,
                    )
                    data = outcome.data
                    info = outcome.info
                    content_type = outcome.content_type
                    final_url = outcome.final_url
                    http_status = outcome.http_status
                    attempts = outcome.attempts
                    cooldown_seconds = outcome.cooldown_seconds
                    if cooldown_seconds > 0:
                        warnings.append(
                            f"{asset_id} 遇到 429，已冷却 {cooldown_seconds:g} 秒后重试"
                        )
            cache_path = _store_resource(data, info, resource_dir)
            digest = _sha256_bytes(data)
            _embed_image(
                image,
                asset_id=asset_id,
                info=info,
                data=data,
                digest=digest,
            )
            resources.append(
                HtmlResource(
                    asset_id=asset_id,
                    dom_path=image_dom_path,
                    ref=ref_attr,
                    original_ref=ref,
                    url=url,
                    final_url=final_url,
                    status="embedded",
                    http_status=http_status,
                    byte_size=len(data),
                    sha256=digest,
                    content_type=content_type,
                    image_format=info.image_format,
                    local_path=local_path,
                    cache_path=str(cache_path),
                    source_manifest=source_manifest_used,
                    attempts=attempts,
                    cooldown_seconds=cooldown_seconds,
                )
            )
        except _ResourceFailure as exc:
            message = str(exc)
            resources.append(
                HtmlResource(
                    asset_id=asset_id,
                    dom_path=image_dom_path,
                    ref=ref_attr,
                    original_ref=ref,
                    url=url,
                    final_url=exc.final_url or final_url,
                    status="failed",
                    http_status=exc.http_status or http_status,
                    attempts=exc.attempts or attempts,
                    cooldown_seconds=exc.cooldown_seconds or cooldown_seconds,
                    error=message,
                )
            )
            _mark_missing_image(image, asset_id=asset_id)
            warnings.append(f"{asset_id} 图片资源缺失：{message}")
            if exc.cooldown_seconds > 0:
                warnings.append(f"{asset_id} 429 冷却 {exc.cooldown_seconds:g} 秒后重试")
        except OSError as exc:
            message = f"本地资源缓存写入失败：{type(exc).__name__}"
            resources.append(
                HtmlResource(
                    asset_id=asset_id,
                    dom_path=image_dom_path,
                    ref=ref_attr,
                    original_ref=ref,
                    url=url,
                    final_url=final_url,
                    status="failed",
                    http_status=http_status,
                    attempts=attempts,
                    cooldown_seconds=cooldown_seconds,
                    error=message,
                )
            )
            _mark_missing_image(image, asset_id=asset_id)
            warnings.append(f"{asset_id} 图片资源缺失：{message}")
    return resources


def _fetch_remote(
    url: str,
    *,
    options: HtmlRenderOptions,
    allowed_hosts: tuple[str, ...],
    client: httpx.Client,
    sleeper: Callable[[float], None],
) -> _FetchOutcome:
    current = _validate_resource_url(url, allowed_hosts)
    redirect_chain = {current}
    redirects = 0
    attempts = 0
    cooldown_seconds = 0.0
    while True:
        attempts += 1
        try:
            with client.stream(
                "GET",
                current,
                follow_redirects=False,
                timeout=options.request_timeout,
                headers={"Accept": "image/*"},
            ) as response:
                status = response.status_code
                if status in _REDIRECT_STATUSES:
                    if redirects >= options.redirect_limit:
                        raise _ResourceFailure(
                            f"资源重定向超过 {options.redirect_limit} 次",
                            http_status=status,
                            final_url=current,
                            attempts=attempts,
                            cooldown_seconds=cooldown_seconds,
                        )
                    location = response.headers.get("location")
                    if not location:
                        raise _ResourceFailure(
                            "资源重定向缺少 Location",
                            http_status=status,
                            final_url=current,
                            attempts=attempts,
                            cooldown_seconds=cooldown_seconds,
                        )
                    redirects += 1
                    target = _validate_resource_url(
                        urljoin(current, location),
                        allowed_hosts,
                    )
                    if target in redirect_chain:
                        raise _ResourceFailure(
                            "资源重定向循环",
                            http_status=status,
                            final_url=current,
                            attempts=attempts,
                            cooldown_seconds=cooldown_seconds,
                        )
                    redirect_chain.add(target)
                    current = target
                    continue
                if status == 429:
                    delay = _retry_after_seconds(response.headers.get("retry-after"))
                    if attempts >= _MAX_ATTEMPTS:
                        raise _ResourceFailure(
                            "HTTP 429 冷却后仍被限流",
                            http_status=status,
                            final_url=current,
                            attempts=attempts,
                            cooldown_seconds=cooldown_seconds,
                        )
                    sleeper(delay)
                    cooldown_seconds += delay
                    continue
                if status != 200:
                    raise _ResourceFailure(
                        f"HTTP 状态 {status}",
                        http_status=status,
                        final_url=current,
                        attempts=attempts,
                        cooldown_seconds=cooldown_seconds,
                    )
                raw_length = response.headers.get("content-length")
                if raw_length:
                    try:
                        declared_length = int(raw_length)
                    except ValueError:
                        declared_length = -1
                    if declared_length > options.max_bytes:
                        raise _ResourceFailure(
                            f"图片超过大小上限 {options.max_bytes} bytes",
                            http_status=status,
                            final_url=current,
                            attempts=attempts,
                            cooldown_seconds=cooldown_seconds,
                        )
                chunks: list[bytes] = []
                total = 0
                for chunk in response.iter_bytes():
                    total += len(chunk)
                    if total > options.max_bytes:
                        raise _ResourceFailure(
                            f"图片超过大小上限 {options.max_bytes} bytes",
                            http_status=status,
                            final_url=current,
                            attempts=attempts,
                            cooldown_seconds=cooldown_seconds,
                        )
                    chunks.append(chunk)
                data = b"".join(chunks)
                content_type = response.headers.get("content-type")
                info = _validate_image_bytes(
                    data,
                    content_type,
                    max_bytes=options.max_bytes,
                    require_content_type=True,
                )
                return _FetchOutcome(
                    data=data,
                    final_url=current,
                    http_status=status,
                    content_type=_normalize_mime(content_type) or info.mime,
                    info=info,
                    attempts=attempts,
                    cooldown_seconds=cooldown_seconds,
                )
        except httpx.HTTPError as exc:
            raise _ResourceFailure(
                f"下载失败：{type(exc).__name__}",
                final_url=current,
                attempts=attempts,
                cooldown_seconds=cooldown_seconds,
            ) from exc


def _retry_after_seconds(value: str | None) -> float:
    if not value:
        return 180.0
    with suppress(ValueError):
        return max(180.0, float(value.strip()))
    try:
        parsed = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return 180.0
    if parsed is None:
        return 180.0
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return max(180.0, (parsed - datetime.now(UTC)).total_seconds())


def _scrub_for_offline(content: Tag) -> None:
    for tag in content.find_all(
        ["script", "iframe", "form", "object", "embed", "applet", "frame", "frameset", "base"]
    ):
        tag.decompose()
    for link in content.find_all("link"):
        link.decompose()
    for meta in content.find_all("meta"):
        http_equiv = str(meta.get("http-equiv") or "").lower()
        if http_equiv in {"refresh", "content-security-policy"}:
            meta.decompose()
    for style in content.find_all("style"):
        style.string = _scrub_css(style.get_text())
    for element in content.find_all(True):
        for attr in list(element.attrs):
            lower = str(attr).lower()
            if _EVENT_ATTR_RE.fullmatch(lower):
                element.attrs.pop(attr, None)
                continue
            if lower == "style":
                value = _scrub_css(str(element.get(attr) or ""))
                if value:
                    element[attr] = value
                else:
                    element.attrs.pop(attr, None)
                continue
            if lower in {"href", "xlink:href"}:
                value = str(element.get(attr) or "").strip()
                if element.name != "a" and value.startswith("#"):
                    continue
                element.attrs.pop(attr, None)
                continue
            if lower in {
                "src",
                "srcset",
                "imagesrcset",
                "poster",
                "data",
                "action",
                "formaction",
                "background",
                "manifest",
                "longdesc",
                "ping",
                "target",
                "srcdoc",
            } and not (element.name == "img" and lower == "src"):
                element.attrs.pop(attr, None)


def _scrub_css(value: str) -> str:
    value = _CSS_IMPORT_RE.sub("", value)

    def replace_url(match: re.Match[str]) -> str:
        inner = match.group(2).strip().lower()
        if inner.startswith(("data:", "#")):
            return match.group(0)
        return 'url("")'

    return _CSS_URL_RE.sub(replace_url, value)


def _serialize_clean_html(content: Tag) -> str:
    if isinstance(content, BeautifulSoup):
        body = content.body or content
        inner = body.decode_contents()
    elif content.name == "body":
        inner = content.decode_contents()
    else:
        inner = str(content)
    return (
        "<!doctype html>\n"
        '<html lang="zh-CN">\n<head>\n<meta charset="utf-8">\n'
        '<meta http-equiv="Content-Security-Policy" content="'
        "default-src 'none'; img-src data: file:; style-src 'unsafe-inline'; "
        "form-action 'none'; base-uri 'none'; frame-src 'none'; object-src 'none'"
        '">\n'
        "<style>html,body{margin:0;padding:0}img{max-width:100%;height:auto}"
        "table{border-collapse:collapse}</style>\n"
        f"</head>\n<body>\n{inner}\n</body>\n</html>\n"
    )


def _render_html_to_pdf(
    html_path: Path,
    pdf_path: Path,
    options: HtmlRenderOptions,
) -> None:
    edge_path = Path(options.edge_path)
    if not edge_path.is_file():
        raise HtmlRenderError("未找到 Edge 可执行文件")
    html_path = Path(html_path).resolve()
    pdf_path = Path(pdf_path).resolve()
    if not html_path.is_file():
        raise HtmlRenderError("清理后的 HTML 不存在")
    creationflags = (
        getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
    )
    with tempfile.TemporaryDirectory(
        prefix="campusguide-html-edge-",
        ignore_cleanup_errors=True,
    ) as temp:
        profile_dir = Path(temp) / "profile"
        profile_dir.mkdir()
        command = build_edge_command(
            edge_path=str(options.edge_path),
            html_path=html_path,
            pdf_path=pdf_path,
            profile_dir=profile_dir,
        )
        try:
            process = subprocess.Popen(
                command,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                creationflags=creationflags,
            )
        except OSError as exc:
            raise HtmlRenderError(f"无法启动 Edge：{type(exc).__name__}") from exc
        try:
            _stdout, stderr = process.communicate(timeout=options.render_timeout)
        except subprocess.TimeoutExpired as exc:
            _terminate_process_tree(process)
            with suppress(subprocess.TimeoutExpired, OSError):
                process.communicate(timeout=5)
            raise HtmlRenderError(
                f"Edge HTML 渲染超时（{options.render_timeout:g}s）"
            ) from exc
        except OSError as exc:
            raise HtmlRenderError(f"Edge HTML 渲染失败：{type(exc).__name__}") from exc
        finally:
            if process.poll() is None:
                _terminate_process_tree(process)
        if process.returncode != 0:
            detail = _stderr_summary(stderr)
            suffix = f"：{detail}" if detail else ""
            raise HtmlRenderError(
                f"Edge HTML 渲染失败（退出码 {process.returncode}）{suffix}"
            )
        if not pdf_path.is_file():
            raise HtmlRenderError("Edge 未生成 PDF")
        with pdf_path.open("rb") as handle:
            header = handle.read(5)
        if header != b"%PDF-":
            raise HtmlRenderError("Edge 生成的 PDF 无效")


def _stderr_summary(stderr: bytes | None) -> str:
    if not stderr:
        return ""
    return _safe_error_text(stderr.decode("utf-8", errors="replace"))


def _safe_error_text(text: str) -> str:
    text = _USERINFO_RE.sub(r"\1<redacted>@", str(text))
    text = re.sub(r"\s+", " ", text).strip()
    return text[:300]


def _terminate_process_tree(process: subprocess.Popen[Any]) -> None:
    try:
        import psutil
    except ImportError:  # pragma: no cover - psutil is optional
        psutil = None
    if psutil is not None:
        with suppress(psutil.Error):
            parent = psutil.Process(process.pid)
            for child in parent.children(recursive=True):
                with suppress(psutil.Error):
                    child.kill()
    with suppress(OSError):
        process.kill()


def _write_manifest(path: Path, payload: Mapping[str, Any]) -> None:
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
