"""Xiaomi-OCR-0 可选影子适配器。

调用方提供真实页面 PNG、来源文件 SHA、页码，以及本地推理 callable 或
回环 OpenAI 兼容后端和官方后处理函数。本模块只做输入核验、调用编排、
OTSL→HTML 表格恢复与缓存，不加载模型、不读取认证环境、不接入生产解析链路。
"""

from __future__ import annotations

import base64
import hashlib
import json
import math
import os
import re
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field
from html.parser import HTMLParser
from pathlib import Path
from typing import Annotated, Any, Literal, Protocol

from pydantic import (
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    ValidationInfo,
    field_validator,
    model_validator,
)

from campusguide.ingest.structure import (
    SourceLocator,
    TableCell,
    TableRow,
    TableStructure,
    join_row_texts,
    row_leaf_slots,
)
from campusguide.observability.errors import safe_error_summary

XIAOMI_OCR_ADAPTER_VERSION = "xiaomi-ocr-adapter-0.2.0"
XIAOMI_OCR_SCHEMA_VERSION = "1.0"
XIAOMI_OCR_TABLE_HTML_VERSION = "otsl-html-grid/1"
XIAOMI_OCR_PREPROCESS_VERSION = "png-validate-identity/1"
OFFICIAL_PROMPT_REVISION = "xiaomi-ocr-official-document/1"
OFFICIAL_DOCUMENT_PROMPT = (
    "Extract all information from the main body of the document image and represent it "
    "in markdown format, ignoring headers and footers. Tables should be expressed in OTSL "
    "format, formulas in the document should be represented using LATEX format, and the "
    "parsing should be organized according to the reading order."
)

MAX_TABLE_SPAN = 100
MAX_TABLE_CELLS = 10_000
MAX_RESPONSE_BYTES = 32 * 1024 * 1024
TRUNCATED_FINISH_REASONS = frozenset({"length", "max_tokens"})
PYDANTIC_CONFIG = ConfigDict(extra="forbid")
_SHA256_RE = re.compile(r"^[0-9a-fA-F]{64}$")
_BACKEND_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,63}$")
_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
_OTSL_TAG_RE = re.compile(
    r"<\s*(/?)\s*(fcel|ecel|lcel|ucel|xcel|nl)\b[^>]*>", re.IGNORECASE
)
_OTSL_CELL_TAGS = frozenset({"fcel", "ecel", "lcel", "ucel", "xcel"})
_OTSL_BLANK_LINE_RE = re.compile(r"\r?\n[ \t]*\r?\n")
_HTML_TABLE_START_RE = re.compile(r"<table\b", re.IGNORECASE)
_TABLE_WARNINGS = [
    "ocr_table_no_physical_bbox: OCR 只提供逻辑 rowspan/colspan，单元格物理 bbox 未知",
    "ocr_table_header_unknown: OCR 未提供表头信息，未推断表头",
    "ocr_table_kind_not_inferred: OCR 未判定表格类型，kind 仅为结构默认值 data",
]


class XiaomiOcrError(Exception):
    """适配器错误的基类。"""


class XiaomiOcrValidationError(XiaomiOcrError):
    """输入文件或请求与实际页面不一致。"""


class XiaomiOcrBackendError(XiaomiOcrError):
    """后端调用或响应解析失败。"""


def _positive_int(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError("必须是正整数（bool 不接受）")
    return value


def _non_negative_int(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError("必须是非负整数（bool 不接受）")
    return value


def _optional_non_negative_int(value: object) -> int | None:
    if value is None:
        return None
    return _non_negative_int(value)


def _non_negative_float(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("必须是非负数")
    number = float(value)
    if not math.isfinite(number) or number < 0:
        raise ValueError("必须是非负有限数")
    return number


def _clean_identity(value: object, field_name: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field_name} 必须是字符串")
    text = value.strip()
    if not text or len(text) > 300 or any(ord(char) < 32 for char in text):
        raise ValueError(f"{field_name} 不能为空、不能含控制字符且长度不超过 300")
    return text


def _clean_sha256(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not _SHA256_RE.fullmatch(value.strip()):
        raise ValueError(f"{field_name} 必须是 64 位十六进制 SHA-256")
    return value.strip().lower()


def _clean_backend_id(value: object) -> str:
    if not isinstance(value, str) or not _BACKEND_ID_RE.fullmatch(value.strip()):
        raise ValueError("backend 必须是 1-64 位字母、数字或 . _ + - 组成的标识")
    return value.strip()


def _path_value(value: object) -> Path:
    if isinstance(value, Path):
        return value
    if isinstance(value, str) and value.strip():
        return Path(value.strip())
    raise ValueError("image_path 必须是非空路径")


PositiveInt = Annotated[int, BeforeValidator(_positive_int)]
NonNegativeInt = Annotated[int, BeforeValidator(_non_negative_int)]
OptionalNonNegativeInt = Annotated[int | None, BeforeValidator(_optional_non_negative_int)]
NonNegativeFloat = Annotated[float, BeforeValidator(_non_negative_float)]


class OcrBackendRequest(BaseModel):
    """传给本地推理 callable 的只读请求。"""

    model_config = PYDANTIC_CONFIG

    image_path: Path
    image_sha256: str
    page: PositiveInt
    prompt: str
    max_tokens: PositiveInt
    model: str
    model_revision: str
    backend: str

    @field_validator("image_path", mode="before")
    @classmethod
    def _image_path(cls, value: object) -> Path:
        return _path_value(value)

    @field_validator("image_sha256")
    @classmethod
    def _image_sha(cls, value: object) -> str:
        return _clean_sha256(value, "image_sha256")

    @field_validator("prompt")
    @classmethod
    def _prompt(cls, value: object) -> str:
        if not isinstance(value, str) or not value.strip():
            raise ValueError("prompt 不能为空")
        return value

    @field_validator("model", "model_revision", mode="before")
    @classmethod
    def _identities(cls, value: object, info: ValidationInfo) -> str:
        return _clean_identity(value, info.field_name)

    @field_validator("backend", mode="before")
    @classmethod
    def _backend(cls, value: object) -> str:
        return _clean_backend_id(value)


class OcrBackendResult(BaseModel):
    """本地后端返回的原始文本及可选终止/计费信息。"""

    model_config = PYDANTIC_CONFIG

    text: str
    finish_reason: str | None = None
    prompt_tokens: OptionalNonNegativeInt = None
    completion_tokens: OptionalNonNegativeInt = None
    total_tokens: OptionalNonNegativeInt = None

    @field_validator("finish_reason", mode="before")
    @classmethod
    def _finish_reason(cls, value: object) -> str | None:
        if value is None:
            return None
        if not isinstance(value, str):
            raise ValueError("finish_reason 必须是字符串或 null")
        return value.strip().lower() or None


class OcrBackend(Protocol):
    def __call__(self, request: OcrBackendRequest) -> OcrBackendResult | str: ...


class OcrPageRequest(BaseModel):
    """整页 OCR 请求；所有身份字段都参与缓存绑定。"""

    model_config = PYDANTIC_CONFIG

    doc_id: str
    version_id: str
    source_format: Literal["pdf", "html"]
    source_sha256: str
    page: PositiveInt
    image_path: Path
    image_sha256: str
    model: str
    model_revision: str
    backend: str
    postprocess_revision: str
    prompt: str = OFFICIAL_DOCUMENT_PROMPT
    prompt_revision: str = OFFICIAL_PROMPT_REVISION
    preprocess_revision: str = XIAOMI_OCR_PREPROCESS_VERSION
    max_tokens: PositiveInt = 4096

    @field_validator("image_path", mode="before")
    @classmethod
    def _image_path(cls, value: object) -> Path:
        return _path_value(value)

    @field_validator("doc_id", "version_id", "model", "model_revision", mode="before")
    @classmethod
    def _identities(cls, value: object, info: ValidationInfo) -> str:
        return _clean_identity(value, info.field_name)

    @field_validator("source_sha256")
    @classmethod
    def _source_sha(cls, value: object) -> str:
        return _clean_sha256(value, "source_sha256")

    @field_validator("image_sha256")
    @classmethod
    def _image_sha(cls, value: object) -> str:
        return _clean_sha256(value, "image_sha256")

    @field_validator("backend", mode="before")
    @classmethod
    def _backend(cls, value: object) -> str:
        return _clean_backend_id(value)

    @field_validator("prompt")
    @classmethod
    def _prompt(cls, value: object) -> str:
        if not isinstance(value, str) or not value.strip():
            raise ValueError("prompt 不能为空")
        return value

    @field_validator("prompt_revision", "postprocess_revision", mode="before")
    @classmethod
    def _revisions(cls, value: object, info: ValidationInfo) -> str:
        return _clean_identity(value, info.field_name)

    @field_validator("preprocess_revision", mode="before")
    @classmethod
    def _preprocess(cls, value: object) -> str:
        text = _clean_identity(value, "preprocess_revision")
        if text != XIAOMI_OCR_PREPROCESS_VERSION:
            raise ValueError(f"preprocess_revision 必须是 {XIAOMI_OCR_PREPROCESS_VERSION}")
        return text

    @model_validator(mode="after")
    def _official_prompt_consistent(self) -> OcrPageRequest:
        if self.prompt_revision == OFFICIAL_PROMPT_REVISION and self.prompt != OFFICIAL_DOCUMENT_PROMPT:
            raise ValueError("官方 prompt_revision 对应的 prompt 文本不一致")
        return self


class OcrUsage(BaseModel):
    """单次调用的成本核算；不可得 token 数保持 null，不能写成 0。"""

    model_config = PYDANTIC_CONFIG

    local_requests: NonNegativeInt = 0
    cloud_requests: NonNegativeInt = 0
    prompt_tokens: OptionalNonNegativeInt = None
    completion_tokens: OptionalNonNegativeInt = None
    total_tokens: OptionalNonNegativeInt = None


class OcrPageEvidence(BaseModel):
    """页面图像证据；OCR HTML 没有几何，因此 bbox 单位只能是 unknown。"""

    model_config = PYDANTIC_CONFIG

    source_format: Literal["pdf", "html"]
    source_sha256: str
    page: PositiveInt
    image_path: str
    image_sha256: str
    image_width: PositiveInt
    image_height: PositiveInt
    bbox_unit: Literal["unknown"] = "unknown"

    @field_validator("source_sha256", "image_sha256")
    @classmethod
    def _hashes(cls, value: object, info: ValidationInfo) -> str:
        return _clean_sha256(value, info.field_name)

    @field_validator("image_path")
    @classmethod
    def _image_path_text(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("image_path 不能为空")
        return value


class OcrPageResult(BaseModel):
    """整页 OCR 结果；任何情况下都不会自动成为可发布或已验证结果。"""

    model_config = PYDANTIC_CONFIG

    schema_version: str = XIAOMI_OCR_SCHEMA_VERSION
    adapter_version: str = XIAOMI_OCR_ADAPTER_VERSION
    mode: Literal["page"] = "page"
    doc_id: str
    version_id: str
    source_format: Literal["pdf", "html"]
    source_sha256: str
    page: PositiveInt
    image_path: str
    image_sha256: str
    image_width: PositiveInt
    image_height: PositiveInt
    evidence: OcrPageEvidence
    model: str
    model_revision: str
    backend: str
    prompt_revision: str
    preprocess_revision: str
    postprocess_revision: str
    max_tokens: PositiveInt
    finish_reason: str | None = None
    raw_response: str | None = None
    markdown: str | None = None
    tables: list[TableStructure] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)
    usage: OcrUsage = Field(default_factory=OcrUsage)
    elapsed_seconds: NonNegativeFloat = 0.0
    success: bool = False
    publishable: Literal[False] = False
    visually_verified: Literal[False] = False
    cache: Literal["disabled", "hit", "miss"] = "disabled"

    @field_validator("source_sha256", "image_sha256")
    @classmethod
    def _hashes(cls, value: object, info: ValidationInfo) -> str:
        return _clean_sha256(value, info.field_name)

    @field_validator("image_path")
    @classmethod
    def _image_path_text(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("image_path 不能为空")
        return value

    @field_validator("doc_id", "version_id", "model", "model_revision", mode="before")
    @classmethod
    def _identities(cls, value: object, info: ValidationInfo) -> str:
        return _clean_identity(value, info.field_name)

    @field_validator("backend", mode="before")
    @classmethod
    def _backend(cls, value: object) -> str:
        return _clean_backend_id(value)


@dataclass
class _RawCell:
    attrs: dict[str, str]
    parts: list[str] = field(default_factory=list)


@dataclass
class _RawRow:
    cells: list[_RawCell] = field(default_factory=list)


@dataclass
class _RawTable:
    rows: list[_RawRow] = field(default_factory=list)


@dataclass(frozen=True)
class _OtslScan:
    blocks: int
    errors: tuple[str, ...]


@dataclass(frozen=True)
class _Placement:
    origin_row: int
    origin_col: int
    rowspan: int
    colspan: int
    text: str


class _MalformedTable(ValueError):
    pass


class _TableCollector(HTMLParser):
    """只收集表格；任何不完整或嵌套结构都会留下错误，而不尝试修复。"""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.tables: list[_RawTable] = []
        self.errors: list[str] = []
        self._ignored_depth = 0
        self._nested_table_depth = 0
        self._table: _RawTable | None = None
        self._row: _RawRow | None = None
        self._cell: _RawCell | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        if tag in {"script", "style"}:
            self._ignored_depth += 1
            return
        if self._ignored_depth:
            return
        if self._nested_table_depth:
            if tag == "table":
                self._nested_table_depth += 1
            return
        if tag == "table":
            if self._table is not None:
                self.errors.append("嵌套 <table> 不受支持")
                self._nested_table_depth = 1
                return
            self._table = _RawTable()
            self._row = None
            self._cell = None
            return
        if self._table is None:
            return
        if tag == "tr":
            if self._cell is not None:
                self.errors.append("单元格内出现 <tr>")
                return
            if self._row is not None:
                self.errors.append("上一个 <tr> 未闭合")
                self._finish_row()
            self._row = _RawRow()
            return
        if tag in {"td", "th"}:
            if self._cell is not None:
                self.errors.append("嵌套单元格不受支持")
                return
            if self._row is None:
                self.errors.append("<td>/<th> 不在 <tr> 内")
                return
            self._cell = _RawCell(attrs={name.lower(): value or "" for name, value in attrs})
            return
        if tag == "br" and self._cell is not None:
            self._cell.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in {"script", "style"}:
            if self._ignored_depth:
                self._ignored_depth -= 1
            return
        if self._ignored_depth:
            return
        if self._nested_table_depth:
            if tag == "table":
                self._nested_table_depth -= 1
            return
        if self._table is None:
            return
        if tag == "table":
            if self._cell is not None:
                self.errors.append("单元格未闭合")
                self._finish_cell()
            if self._row is not None:
                self.errors.append("<tr> 未闭合")
                self._finish_row()
            self.tables.append(self._table)
            self._table = None
            return
        if tag in {"td", "th"}:
            if self._cell is not None:
                self._finish_cell()
        elif tag == "tr":
            if self._cell is not None:
                self.errors.append("单元格未闭合")
                self._finish_cell()
            if self._row is not None:
                self._finish_row()

    def handle_data(self, data: str) -> None:
        if self._ignored_depth or self._nested_table_depth:
            return
        if self._cell is not None:
            self._cell.parts.append(data)

    def finish(self) -> None:
        self.close()
        if self._ignored_depth:
            self.errors.append("<script>/<style> 未闭合")
        if self._nested_table_depth:
            self.errors.append("嵌套 <table> 未闭合")
        if self._table is not None:
            self.errors.append("<table> 未闭合")
            self._cell = None
            self._row = None
            self._table = None

    def _finish_cell(self) -> None:
        cell = self._cell
        self._cell = None
        if cell is None:
            return
        if self._row is None:
            self.errors.append("单元格不在 <tr> 内")
            return
        self._row.cells.append(cell)

    def _finish_row(self) -> None:
        row = self._row
        self._row = None
        if row is None or self._table is None:
            return
        self._table.rows.append(row)


class _RejectRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: Any,
        code: int,
        msg: str,
        headers: Any,
        newurl: str,
    ) -> None:
        raise XiaomiOcrBackendError(f"拒绝重定向（HTTP {code}）")


class LoopbackOpenAIBackend:
    """仅连接回环地址的 OpenAI 兼容后端；不读取代理或认证环境变量。"""

    def __init__(
        self,
        *,
        base_url: str,
        timeout_seconds: float,
        max_tokens: int,
        backend_id: str = "loopback-openai-compatible",
    ) -> None:
        self._chat_url = _loopback_chat_url(base_url)
        if isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, (int, float)):
            raise ValueError("timeout_seconds 必须是正数")
        if not math.isfinite(float(timeout_seconds)) or float(timeout_seconds) <= 0:
            raise ValueError("timeout_seconds 必须是正的有限数")
        self.timeout_seconds = float(timeout_seconds)
        self.max_tokens = _positive_int(max_tokens)
        self.backend_id = _clean_backend_id(backend_id)

    def __call__(self, request: OcrBackendRequest) -> OcrBackendResult:
        if request.max_tokens > self.max_tokens:
            raise XiaomiOcrBackendError(
                f"请求 max_tokens={request.max_tokens} 超过后端上限 {self.max_tokens}"
            )
        path = request.image_path.expanduser()
        try:
            image = path.read_bytes()
        except OSError as exc:
            raise XiaomiOcrBackendError(f"无法读取页面图像: {safe_error_summary(exc)}") from exc
        if hashlib.sha256(image).hexdigest() != request.image_sha256:
            raise XiaomiOcrBackendError("页面图像在核验后发生变化")
        payload = {
            "model": request.model,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": "data:image/png;base64,"
                                + base64.b64encode(image).decode("ascii")
                            },
                        },
                        {"type": "text", "text": request.prompt},
                    ],
                }
            ],
            "max_tokens": request.max_tokens,
            "temperature": 0.0,
            "top_p": 0.8,
            "presence_penalty": 0.0,
            "repetition_penalty": 1.0,
            "chat_template_kwargs": {"enable_thinking": False},
        }
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        http_request = urllib.request.Request(
            self._chat_url,
            data=body,
            headers={"Content-Type": "application/json", "Accept": "application/json"},
            method="POST",
        )
        opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}), _RejectRedirects()
        )
        try:
            with opener.open(http_request, timeout=self.timeout_seconds) as response:
                raw = response.read(MAX_RESPONSE_BYTES + 1)
        except urllib.error.HTTPError as exc:
            detail = exc.read(2048).decode("utf-8", errors="replace")
            raise XiaomiOcrBackendError(f"本地推理返回 HTTP {exc.code}: {detail[:500]}") from exc
        except XiaomiOcrBackendError:
            raise
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise XiaomiOcrBackendError(
                f"本地推理不可用: {safe_error_summary(exc)}"
            ) from exc
        if len(raw) > MAX_RESPONSE_BYTES:
            raise XiaomiOcrBackendError("本地推理响应超过大小上限")
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise XiaomiOcrBackendError("本地推理返回了无效 JSON") from exc
        return _openai_result(data)


def _loopback_chat_url(base_url: str) -> str:
    if not isinstance(base_url, str) or not base_url.strip():
        raise ValueError("base_url 不能为空")
    parsed = urllib.parse.urlsplit(base_url.strip())
    if parsed.scheme != "http":
        raise ValueError("base_url 必须使用 http:// 回环地址")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("base_url 不得包含用户名或密码")
    if parsed.query or parsed.fragment:
        raise ValueError("base_url 不得包含查询串或片段")
    if parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
        raise ValueError("base_url 必须是 127.0.0.1、localhost 或 ::1")
    try:
        _ = parsed.port
    except ValueError as exc:
        raise ValueError("base_url 端口无效") from exc
    path = parsed.path.rstrip("/")
    if path not in {"", "/v1"}:
        raise ValueError("base_url 路径只能是空或 /v1")
    return f"http://{parsed.netloc}{path}/chat/completions"


def _openai_result(data: object) -> OcrBackendResult:
    if not isinstance(data, dict):
        raise XiaomiOcrBackendError("本地推理响应不是 JSON 对象")
    choices = data.get("choices")
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
        raise XiaomiOcrBackendError("本地推理响应缺少 choices")
    choice = choices[0]
    message = choice.get("message")
    if not isinstance(message, dict):
        raise XiaomiOcrBackendError("本地推理响应缺少 message")
    content = message.get("content")
    if isinstance(content, str):
        text = content
    elif isinstance(content, list):
        parts: list[str] = []
        for part in content:
            if isinstance(part, dict) and part.get("type") == "text" and isinstance(
                part.get("text"), str
            ):
                parts.append(part["text"])
        text = "".join(parts)
    else:
        raise XiaomiOcrBackendError("本地推理响应缺少文本 content")
    finish_reason = choice.get("finish_reason")
    usage = data.get("usage")
    usage = usage if isinstance(usage, dict) else {}

    def token(name: str) -> int | None:
        value = usage.get(name)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            return None
        return value

    return OcrBackendResult(
        text=text,
        finish_reason=finish_reason if isinstance(finish_reason, str) else None,
        prompt_tokens=token("prompt_tokens"),
        completion_tokens=token("completion_tokens"),
        total_tokens=token("total_tokens"),
    )


def _inspect_png(path: Path) -> tuple[int, int, str]:
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise XiaomiOcrValidationError(f"无法读取页面 PNG: {safe_error_summary(exc)}") from exc
    if len(data) < 24 or not data.startswith(_PNG_SIGNATURE):
        raise XiaomiOcrValidationError("页面图像不是 PNG")
    if int.from_bytes(data[8:12], "big") != 13 or data[12:16] != b"IHDR":
        raise XiaomiOcrValidationError("PNG 缺少合法 IHDR")
    width = int.from_bytes(data[16:20], "big")
    height = int.from_bytes(data[20:24], "big")
    if width <= 0 or height <= 0:
        raise XiaomiOcrValidationError("PNG 宽高必须为正")
    return width, height, hashlib.sha256(data).hexdigest()


def _normalize_cell_text(text: str) -> str:
    lines: list[str] = []
    for line in text.splitlines():
        cleaned = re.sub(r"[ \t\u00a0]+", " ", line).strip()
        if cleaned:
            lines.append(cleaned)
    return "\n".join(lines)


def _parse_span(attrs: dict[str, str], name: str) -> int:
    raw = attrs.get(name)
    if raw is None:
        return 1
    text = raw.strip()
    if not re.fullmatch(r"[0-9]{1,3}", text):
        raise _MalformedTable(f"{name} 不是整数")
    span = int(text)
    if span < 1 or span > MAX_TABLE_SPAN:
        raise _MalformedTable(f"{name} 超出 1-{MAX_TABLE_SPAN}")
    return span


def _ocr_table_id(doc_id: str, version_id: str, page: int, table_index: int) -> str:
    return f"{doc_id}@{version_id}#ocr-page{page}#table{table_index}"


def _raw_table_to_structure(
    raw: _RawTable,
    *,
    doc_id: str,
    version_id: str,
    page: int,
    table_index: int,
    source_format: Literal["pdf", "html"],
) -> TableStructure:
    if not raw.rows:
        raise _MalformedTable("表格没有行")
    grid: list[list[_Placement | None]] = []
    placed = 0
    for row_index, row in enumerate(raw.rows):
        while len(grid) <= row_index:
            grid.append([])
        if not row.cells:
            raise _MalformedTable(f"第 {row_index} 行没有单元格")
        col = 0
        for cell in row.cells:
            while col < len(grid[row_index]) and grid[row_index][col] is not None:
                col += 1
            rowspan = _parse_span(cell.attrs, "rowspan")
            colspan = _parse_span(cell.attrs, "colspan")
            text = _normalize_cell_text("".join(cell.parts))
            placed += rowspan * colspan
            if placed > MAX_TABLE_CELLS:
                raise _MalformedTable(f"表格展开超过 {MAX_TABLE_CELLS} 个单元格")
            for row_offset in range(rowspan):
                target_row = row_index + row_offset
                while len(grid) <= target_row:
                    grid.append([])
                for col_offset in range(colspan):
                    target_col = col + col_offset
                    while len(grid[target_row]) <= target_col:
                        grid[target_row].append(None)
                    if grid[target_row][target_col] is not None:
                        raise _MalformedTable(f"单元格重叠于 r{target_row}c{target_col}")
                    grid[target_row][target_col] = _Placement(
                        row_index, col, rowspan, colspan, text
                    )
            col += colspan

    row_count = len(raw.rows)
    if len(grid) != row_count:
        raise _MalformedTable("rowspan 超出最后一行")
    column_count = max((len(row) for row in grid), default=0)
    if column_count <= 0:
        raise _MalformedTable("表格没有列")
    for row_index, row in enumerate(grid):
        if len(row) != column_count or any(cell is None for cell in row):
            raise _MalformedTable(
                f"第 {row_index} 行宽度为 {len(row)}，期望矩形宽度 {column_count}"
            )

    table_id = _ocr_table_id(doc_id, version_id, page, table_index)
    cells_by_row: list[list[TableCell]] = []
    for row_index in range(row_count):
        cells: list[TableCell] = []
        for col_index in range(column_count):
            placement = grid[row_index][col_index]
            assert placement is not None
            is_origin = placement.origin_row == row_index and placement.origin_col == col_index
            text = placement.text if is_origin else ""
            cells.append(
                TableCell(
                    cell_id=f"{table_id}#r{row_index}c{col_index}",
                    row=row_index,
                    col=col_index,
                    rowspan=placement.rowspan,
                    colspan=placement.colspan,
                    origin_row=placement.origin_row,
                    origin_col=placement.origin_col,
                    covered=not is_origin,
                    text=text,
                    vision_text=text if is_origin else None,
                    text_source="vision" if is_origin and text else None,
                    is_header=False,
                    bbox=None,
                    dom_path=None,
                )
            )
        cells_by_row.append(cells)

    origin_map = {
        (cell.origin_row, cell.origin_col): cell
        for cells in cells_by_row
        for cell in cells
        if not cell.covered
    }
    locator = SourceLocator(format=source_format, page=page, bbox=None)
    table_rows: list[TableRow] = []
    for row_index, cells in enumerate(cells_by_row):
        slots = row_leaf_slots(
            cells,
            row_index=row_index,
            column_count=column_count,
            resolve_origin=lambda origin_row, origin_col: origin_map.get(
                (origin_row, origin_col)
            ),
        )
        table_rows.append(
            TableRow(
                row_id=f"{table_id}#row{row_index}",
                row_index=row_index,
                is_header=False,
                cells=cells,
                text=join_row_texts(
                    [cell.text if cell is not None else "" for cell in slots]
                ),
                locator=locator.model_copy(),
            )
        )

    return TableStructure(
        table_id=table_id,
        block_id=f"{table_id}#block",
        table_index=table_index,
        page=page,
        bbox=None,
        rows=table_rows,
        header_row_indices=[],
        column_count=column_count,
        row_count=row_count,
        column_headers=[],
        header_paths=[],
        caption=None,
        kind="data",
        source="vision",
        locator=locator,
        warnings=list(_TABLE_WARNINGS),
        vision=None,
    )


def _scan_otsl_blocks(text: str) -> _OtslScan:
    matches = list(_OTSL_TAG_RE.finditer(text))
    if not matches:
        return _OtslScan(blocks=0, errors=())
    errors: list[str] = []
    blocks = 0
    block_rows: list[int] = []
    row_cells = 0
    previous_end: int | None = None
    previous_name: str | None = None

    def finish_block() -> None:
        nonlocal blocks, block_rows
        if not block_rows:
            return
        if any(count != block_rows[0] for count in block_rows):
            errors.append(f"otsl_ragged_rows: OTSL 表格行列数不一致 {block_rows}")
        else:
            blocks += 1
        block_rows = []

    for match in matches:
        closing = bool(match.group(1))
        name = match.group(2).lower()
        if closing:
            errors.append(f"otsl_malformed: 不支持 OTSL 闭合标签 </{name}>")
            previous_end = match.end()
            previous_name = None
            continue
        if previous_end is not None and previous_name == "nl":
            gap = text[previous_end : match.start()]
            if _OTSL_BLANK_LINE_RE.search(gap) or gap.strip():
                finish_block()
        if name == "nl":
            if row_cells:
                block_rows.append(row_cells)
            else:
                errors.append("otsl_malformed: OTSL 行没有单元格")
            row_cells = 0
        elif name in _OTSL_CELL_TAGS:
            row_cells += 1
        previous_end = match.end()
        previous_name = name
    if row_cells:
        errors.append("otsl_malformed: 最后一个 OTSL 行缺少 <nl>")
    finish_block()
    if blocks == 0 and not errors:
        errors.append("otsl_malformed: 未识别到完整 OTSL 表格块")
    return _OtslScan(blocks=blocks, errors=tuple(errors))


def _html_table_count(text: str) -> int:
    return len(_HTML_TABLE_START_RE.findall(text))


def _otsl_conversion_errors(scan: _OtslScan, markdown: str) -> list[str]:
    errors: list[str] = []
    if _OTSL_TAG_RE.search(markdown):
        errors.append(
            "otsl_unconverted: 规范化输出仍含 OTSL 标记，后处理未完成表格转换"
        )
    html_tables = _html_table_count(markdown)
    if scan.blocks > html_tables:
        errors.append(
            f"otsl_conversion_missing: 识别到 {scan.blocks} 个 OTSL 表格块，"
            f"但规范化输出只有 {html_tables} 个 HTML 表格"
        )
    return errors


def _extract_tables(
    markdown: str,
    *,
    doc_id: str,
    version_id: str,
    page: int,
    source_format: Literal["pdf", "html"],
) -> tuple[list[TableStructure], list[str], list[str]]:
    collector = _TableCollector()
    try:
        collector.feed(markdown)
        collector.finish()
    except Exception as exc:
        return [], [], [f"ocr_table_malformed: HTML 解析失败: {safe_error_summary(exc)}"]
    errors = [f"ocr_table_malformed: {message}" for message in collector.errors]
    tables: list[TableStructure] = []
    for table_index, raw in enumerate(collector.tables):
        try:
            table = _raw_table_to_structure(
                raw,
                doc_id=doc_id,
                version_id=version_id,
                page=page,
                table_index=table_index,
                source_format=source_format,
            )
            _validate_ocr_table(
                table,
                doc_id=doc_id,
                version_id=version_id,
                page=page,
                table_index=table_index,
                source_format=source_format,
            )
        except Exception as exc:
            errors.append(
                f"ocr_table_malformed: table {table_index}: {safe_error_summary(exc)}"
            )
            continue
        tables.append(table)
    return tables, [], errors


def _validate_ocr_table(
    table: TableStructure,
    *,
    doc_id: str,
    version_id: str,
    page: int,
    table_index: int,
    source_format: Literal["pdf", "html"],
) -> None:
    table_id = _ocr_table_id(doc_id, version_id, page, table_index)
    if table.table_id != table_id or table.block_id != f"{table_id}#block":
        raise ValueError("table/block ID 与绑定不一致")
    if table.table_index != table_index or table.page != page:
        raise ValueError("table 索引或页码不一致")
    if table.locator.format != source_format or table.locator.page != page:
        raise ValueError("table locator 不一致")
    if table.locator.bbox is not None or table.locator.dom_path is not None:
        raise ValueError("OCR 表格不得携带物理 bbox 或原生 DOM 路径")
    if table.locator.char_start is not None or table.locator.char_end is not None:
        raise ValueError("OCR 表格不得携带原文字符偏移")
    if table.bbox is not None or table.caption is not None:
        raise ValueError("OCR 表格不得携带物理 bbox 或推断标题")
    if table.kind != "data" or table.source != "vision" or table.vision is not None:
        raise ValueError("OCR 表格来源/类型字段被改写")
    if table.header_row_indices or table.column_headers or table.header_paths:
        raise ValueError("OCR 表格不得推断表头")
    if (
        table.continuation_of is not None
        or table.header_source_table_id is not None
        or table.header_source_page is not None
    ):
        raise ValueError("OCR 表格不得推断跨页表头来源")
    if table.row_count != len(table.rows) or table.row_count < 1 or table.column_count < 1:
        raise ValueError("table 行列数不一致")
    if any(row.is_header for row in table.rows):
        raise ValueError("OCR 表格不得标记表头行")

    origins: dict[tuple[int, int], TableCell] = {}
    for row_index, row in enumerate(table.rows):
        if row.row_index != row_index:
            raise ValueError("row_index 不连续")
        if row.row_id != f"{table_id}#row{row_index}":
            raise ValueError("row_id 与行号不一致")
        if row.locator.format != source_format or row.locator.page != page:
            raise ValueError("row locator 不一致")
        if (
            row.locator.bbox is not None
            or row.locator.dom_path is not None
            or row.locator.char_start is not None
            or row.locator.char_end is not None
        ):
            raise ValueError("OCR 行不得携带物理位置")
        if len(row.cells) != table.column_count:
            raise ValueError("行单元格数不等于 column_count")
        for col_index, cell in enumerate(row.cells):
            if any(
                isinstance(getattr(cell, name), bool)
                or not isinstance(getattr(cell, name), int)
                for name in ("row", "col", "origin_row", "origin_col")
            ):
                raise ValueError("cell 坐标类型无效")
            if not isinstance(cell.covered, bool):
                raise ValueError("covered 类型无效")
            if cell.row != row_index or cell.col != col_index:
                raise ValueError("cell 坐标与所在位置不一致")
            if cell.cell_id != f"{table_id}#r{row_index}c{col_index}":
                raise ValueError("cell_id 与坐标不一致")
            if cell.is_header or cell.needs_review:
                raise ValueError("OCR 单元格不得推断表头或复核状态")
            if cell.bbox is not None or cell.dom_path is not None:
                raise ValueError("OCR 单元格不得携带物理 bbox 或 DOM 路径")
            if cell.native_text is not None:
                raise ValueError("OCR 单元格不得携带原生文本")
            if isinstance(cell.rowspan, bool) or not isinstance(cell.rowspan, int):
                raise ValueError("rowspan 类型无效")
            if isinstance(cell.colspan, bool) or not isinstance(cell.colspan, int):
                raise ValueError("colspan 类型无效")
            if not 1 <= cell.rowspan <= MAX_TABLE_SPAN:
                raise ValueError("rowspan 超出范围")
            if not 1 <= cell.colspan <= MAX_TABLE_SPAN:
                raise ValueError("colspan 超出范围")
            if not (
                0 <= cell.origin_row < table.row_count
                and 0 <= cell.origin_col < table.column_count
            ):
                raise ValueError("origin 坐标超出表格")
            if not cell.covered:
                if (cell.origin_row, cell.origin_col) != (row_index, col_index):
                    raise ValueError("origin 单元格必须指向自身")
                if cell.vision_text != cell.text:
                    raise ValueError("origin 单元格视觉文本不一致")
                expected_source = "vision" if cell.text else None
                if cell.text_source != expected_source:
                    raise ValueError("origin 单元格 text_source 不一致")
                origins[(row_index, col_index)] = cell
            else:
                if (cell.origin_row, cell.origin_col) == (row_index, col_index):
                    raise ValueError("covered 单元格不能指向自身")
                if cell.text or cell.vision_text is not None or cell.text_source is not None:
                    raise ValueError("covered 单元格不得保存文本")

    for (origin_row, origin_col), origin in origins.items():
        if origin_row + origin.rowspan > table.row_count:
            raise ValueError("origin rowspan 超出表格")
        if origin_col + origin.colspan > table.column_count:
            raise ValueError("origin colspan 超出表格")
        for row_index in range(origin_row, origin_row + origin.rowspan):
            for col_index in range(origin_col, origin_col + origin.colspan):
                cell = table.rows[row_index].cells[col_index]
                if (cell.origin_row, cell.origin_col) != (origin_row, origin_col):
                    raise ValueError("origin 矩形覆盖不完整")
                is_origin = (row_index, col_index) == (origin_row, origin_col)
                if cell.covered is is_origin:
                    raise ValueError("origin/covered 标记错误")
                if cell.rowspan != origin.rowspan or cell.colspan != origin.colspan:
                    raise ValueError("覆盖单元格 span 与 origin 不一致")

    for row_index, row in enumerate(table.rows):
        for cell in row.cells:
            if not cell.covered:
                continue
            origin = origins.get((cell.origin_row, cell.origin_col))
            if origin is None:
                raise ValueError("covered 单元格缺少 origin")
            if not (
                origin.origin_row <= row_index < origin.origin_row + origin.rowspan
                and origin.origin_col <= cell.col <= origin.origin_col + origin.colspan
            ):
                raise ValueError("covered 单元格超出 origin 矩形")

    for row_index, row in enumerate(table.rows):
        slots = row_leaf_slots(
            row.cells,
            row_index=row_index,
            column_count=table.column_count,
            resolve_origin=lambda origin_row, origin_col: origins.get(
                (origin_row, origin_col)
            ),
        )
        expected = join_row_texts(
            [cell.text if cell is not None else "" for cell in slots]
        )
        if row.text != expected:
            raise ValueError("row.text 未保留空 leaf 或覆盖 origin")


def _cache_binding(
    request: OcrPageRequest,
    *,
    image_sha256: str,
    image_width: int,
    image_height: int,
) -> dict[str, Any]:
    return {
        "schema_version": XIAOMI_OCR_SCHEMA_VERSION,
        "adapter_version": XIAOMI_OCR_ADAPTER_VERSION,
        "table_html_version": XIAOMI_OCR_TABLE_HTML_VERSION,
        "doc_id": request.doc_id,
        "version_id": request.version_id,
        "source_format": request.source_format,
        "source_sha256": request.source_sha256,
        "page": request.page,
        "image_sha256": image_sha256,
        "image_width": image_width,
        "image_height": image_height,
        "model": request.model,
        "model_revision": request.model_revision,
        "backend": request.backend,
        "prompt_revision": request.prompt_revision,
        "prompt_sha256": hashlib.sha256(request.prompt.encode("utf-8")).hexdigest(),
        "preprocess_revision": request.preprocess_revision,
        "postprocess_revision": request.postprocess_revision,
        "max_tokens": request.max_tokens,
    }


def _cache_key(binding: dict[str, Any]) -> str:
    canonical = json.dumps(
        binding, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _validate_cached_result(result: OcrPageResult, binding: dict[str, Any]) -> None:
    if result.success is not True or result.errors:
        raise ValueError("缓存结果不是成功结果")
    if result.publishable is not False or result.visually_verified is not False:
        raise ValueError("缓存结果违反了不可发布/不可验证约束")
    if result.mode != "page":
        raise ValueError("缓存结果不是 page 模式")
    if result.raw_response is None or result.markdown is None or not result.markdown.strip():
        raise ValueError("缓存结果缺少原始输出或 Markdown")
    if not result.raw_response.strip():
        raise ValueError("缓存成功结果含空原始输出")
    if result.finish_reason in TRUNCATED_FINISH_REASONS:
        raise ValueError("缓存成功结果标记为截断")
    if result.finish_reason not in {None, "stop"}:
        raise ValueError("缓存成功结果含异常 finish_reason")
    if result.usage.cloud_requests != 0:
        raise ValueError("缓存结果不得含云端请求")
    scan = _scan_otsl_blocks(result.raw_response)
    otsl_errors = [*scan.errors, *_otsl_conversion_errors(scan, result.markdown)]
    if otsl_errors:
        raise ValueError("缓存结果未通过 OTSL 转换校验: " + "; ".join(otsl_errors))
    if _html_table_count(result.markdown) != len(result.tables):
        raise ValueError("缓存 HTML 表格数与解析表格数不一致")
    for name in (
        "schema_version",
        "adapter_version",
        "doc_id",
        "version_id",
        "source_format",
        "source_sha256",
        "page",
        "image_sha256",
        "image_width",
        "image_height",
        "model",
        "model_revision",
        "backend",
        "prompt_revision",
        "preprocess_revision",
        "postprocess_revision",
        "max_tokens",
    ):
        if getattr(result, name) != binding[name]:
            raise ValueError(f"缓存结果字段与绑定不一致: {name}")
    evidence = result.evidence
    if (
        evidence.source_format != result.source_format
        or evidence.source_sha256 != result.source_sha256
        or evidence.page != result.page
        or evidence.image_path != result.image_path
        or evidence.image_sha256 != result.image_sha256
        or evidence.image_width != result.image_width
        or evidence.image_height != result.image_height
        or evidence.bbox_unit != "unknown"
    ):
        raise ValueError("缓存页面证据不一致")
    for table_index, table in enumerate(result.tables):
        _validate_ocr_table(
            table,
            doc_id=result.doc_id,
            version_id=result.version_id,
            page=result.page,
            table_index=table_index,
            source_format=result.source_format,
        )


def _read_cache(
    path: Path, binding: dict[str, Any]
) -> tuple[OcrPageResult | None, str | None]:
    if not path.exists():
        return None, None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("缓存不是 JSON 对象")
        if payload.get("schema_version") != XIAOMI_OCR_SCHEMA_VERSION:
            raise ValueError("缓存 schema 不匹配")
        if payload.get("binding") != binding:
            raise ValueError("缓存绑定不匹配")
        result_payload = payload.get("result")
        if not isinstance(result_payload, dict):
            raise ValueError("缓存缺少结果")
        result = OcrPageResult.model_validate(result_payload)
        _validate_cached_result(result, binding)
        return result, None
    except Exception as exc:
        return None, f"cache_entry_rejected: {safe_error_summary(exc)}"


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    descriptor, temporary = tempfile.mkstemp(
        prefix=path.name + ".", suffix=".part", dir=path.parent
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _coerce_backend_result(
    value: object, warnings: list[str]
) -> OcrBackendResult:
    if isinstance(value, OcrBackendResult):
        return value
    if isinstance(value, str):
        warnings.append(
            "backend_returned_bare_text: 未提供 finish_reason 与 token usage，"
            "无法核验截断或计费"
        )
        return OcrBackendResult(text=value)
    raise XiaomiOcrBackendError(
        f"backend 返回了不支持的类型: {type(value).__name__}"
    )


def run_page_ocr(
    request: OcrPageRequest,
    *,
    backend: OcrBackend,
    postprocess: Callable[[str], str],
    cache_dir: Path | str | None = None,
) -> OcrPageResult:
    """执行一次整页 OCR。

    输入文件与声明哈希不一致时抛 ``XiaomiOcrValidationError``；后端、后处理或
    表格转换失败时返回 ``success=False`` 的结果，并保留已取得的原始输出。
    缓存命中不会调用后端，``usage.local_requests`` 归零、token 数为 null。
    """
    started = time.monotonic()
    if not callable(backend):
        raise XiaomiOcrValidationError("backend 必须可调用")
    if not callable(postprocess):
        raise XiaomiOcrValidationError("postprocess 必须可调用")

    path = request.image_path.expanduser()
    try:
        if not path.is_file():
            raise XiaomiOcrValidationError(f"页面 PNG 不存在: {path}")
    except OSError as exc:
        raise XiaomiOcrValidationError(
            f"无法访问页面 PNG: {safe_error_summary(exc)}"
        ) from exc
    width, height, image_sha256 = _inspect_png(path)
    if image_sha256 != request.image_sha256:
        raise XiaomiOcrValidationError(
            f"页面 PNG 哈希不匹配: 声明 {request.image_sha256}，实际 {image_sha256}"
        )
    resolved = path.resolve()

    binding = _cache_binding(
        request,
        image_sha256=image_sha256,
        image_width=width,
        image_height=height,
    )
    warnings: list[str] = []
    cache_path: Path | None = None
    if cache_dir is not None:
        if isinstance(cache_dir, str) and not cache_dir.strip():
            raise XiaomiOcrValidationError("cache_dir 不能是空字符串")
        cache_path = Path(cache_dir).expanduser() / f"{_cache_key(binding)}.json"
        cached, cache_warning = _read_cache(cache_path, binding)
        if cache_warning:
            warnings.append(cache_warning)
        if cached is not None:
            return cached.model_copy(
                update={
                    "image_path": str(resolved),
                    "evidence": cached.evidence.model_copy(
                        update={"image_path": str(resolved)}
                    ),
                    "usage": OcrUsage(local_requests=0, cloud_requests=0),
                    "elapsed_seconds": round(time.monotonic() - started, 6),
                    "cache": "hit",
                }
            )

    evidence = OcrPageEvidence(
        source_format=request.source_format,
        source_sha256=request.source_sha256,
        page=request.page,
        image_path=str(resolved),
        image_sha256=image_sha256,
        image_width=width,
        image_height=height,
    )
    backend_request = OcrBackendRequest(
        image_path=resolved,
        image_sha256=image_sha256,
        page=request.page,
        prompt=request.prompt,
        max_tokens=request.max_tokens,
        model=request.model,
        model_revision=request.model_revision,
        backend=request.backend,
    )

    usage = OcrUsage(local_requests=1, cloud_requests=0)
    errors: list[str] = []
    raw_response: str | None = None
    markdown: str | None = None
    finish_reason: str | None = None
    tables: list[TableStructure] = []
    missing = object()
    value: object = missing
    try:
        value = backend(backend_request)
    except Exception as exc:
        errors.append(f"ocr_backend_error: {safe_error_summary(exc)}")

    if value is not missing:
        try:
            backend_result = _coerce_backend_result(value, warnings)
        except Exception as exc:
            errors.append(f"ocr_backend_error: {safe_error_summary(exc)}")
        else:
            raw_response = backend_result.text
            finish_reason = backend_result.finish_reason
            usage = OcrUsage(
                local_requests=1,
                cloud_requests=0,
                prompt_tokens=backend_result.prompt_tokens,
                completion_tokens=backend_result.completion_tokens,
                total_tokens=backend_result.total_tokens,
            )
            if finish_reason in TRUNCATED_FINISH_REASONS:
                errors.append(
                    f"ocr_truncated: finish_reason={finish_reason}，回退到更小输入或更大预算"
                )
            elif finish_reason is not None and finish_reason != "stop":
                errors.append(f"ocr_finish_reason_unexpected: {finish_reason}")
            elif not raw_response.strip():
                errors.append("ocr_empty_response: 本地模型返回空文本")
            else:
                if finish_reason is None:
                    warnings.append(
                        "backend_finish_reason_unavailable: 无法核验响应是否被截断"
                    )
                otsl_scan = _scan_otsl_blocks(raw_response)
                errors.extend(otsl_scan.errors)
                try:
                    normalized = postprocess(raw_response)
                except Exception as exc:
                    errors.append(
                        f"ocr_postprocess_error: {safe_error_summary(exc)}"
                    )
                else:
                    if not isinstance(normalized, str):
                        errors.append("ocr_postprocess_error: 后处理未返回字符串")
                    elif not normalized.strip():
                        markdown = normalized
                        errors.append("ocr_postprocess_error: 后处理返回空 Markdown")
                    else:
                        markdown = normalized
                        conversion_errors = _otsl_conversion_errors(
                            otsl_scan, markdown
                        )
                        errors.extend(conversion_errors)
                        if not otsl_scan.errors and not conversion_errors:
                            tables, table_warnings, table_errors = _extract_tables(
                                markdown,
                                doc_id=request.doc_id,
                                version_id=request.version_id,
                                page=request.page,
                                source_format=request.source_format,
                            )
                            warnings.extend(table_warnings)
                            errors.extend(table_errors)

    success = not errors
    result = OcrPageResult(
        doc_id=request.doc_id,
        version_id=request.version_id,
        source_format=request.source_format,
        source_sha256=request.source_sha256,
        page=request.page,
        image_path=str(resolved),
        image_sha256=image_sha256,
        image_width=width,
        image_height=height,
        evidence=evidence,
        model=request.model,
        model_revision=request.model_revision,
        backend=request.backend,
        prompt_revision=request.prompt_revision,
        preprocess_revision=request.preprocess_revision,
        postprocess_revision=request.postprocess_revision,
        max_tokens=request.max_tokens,
        finish_reason=finish_reason,
        raw_response=raw_response,
        markdown=markdown,
        tables=tables,
        warnings=warnings,
        errors=errors,
        usage=usage,
        elapsed_seconds=round(time.monotonic() - started, 6),
        success=success,
        cache="disabled" if cache_path is None else "miss",
    )

    if success and cache_path is not None:
        try:
            _atomic_write_json(
                cache_path,
                {
                    "schema_version": XIAOMI_OCR_SCHEMA_VERSION,
                    "binding": binding,
                    "result": result.model_dump(mode="json"),
                },
            )
        except Exception as exc:
            result = result.model_copy(
                update={
                    "warnings": [
                        *result.warnings,
                        f"cache_write_failed: {safe_error_summary(exc)}",
                    ]
                }
            )
    return result


__all__ = [
    "OFFICIAL_DOCUMENT_PROMPT",
    "OFFICIAL_PROMPT_REVISION",
    "XIAOMI_OCR_ADAPTER_VERSION",
    "XIAOMI_OCR_PREPROCESS_VERSION",
    "XIAOMI_OCR_SCHEMA_VERSION",
    "XIAOMI_OCR_TABLE_HTML_VERSION",
    "LoopbackOpenAIBackend",
    "OcrBackend",
    "OcrBackendRequest",
    "OcrBackendResult",
    "OcrPageEvidence",
    "OcrPageRequest",
    "OcrPageResult",
    "OcrUsage",
    "XiaomiOcrBackendError",
    "XiaomiOcrError",
    "XiaomiOcrValidationError",
    "run_page_ocr",
]
