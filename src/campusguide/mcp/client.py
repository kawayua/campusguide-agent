"""FastMCP stdio client 生命周期与错误映射。

应用侧只依赖 ``SearchClient`` / ``MetaClient`` 协议，不 import Qdrant、
embedding 或 reranker。客户端在后台线程维护 asyncio 事件循环与持久
MCP 会话；每次调用有独立超时，可重试错误最多重试 ``retries`` 次。
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import contextlib
import json
import os
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from campusguide.mcp.schemas import (
    ErrorCode,
    GetArticleData,
    SearchArticlesData,
    SearchArticlesRequest,
    ToolError,
)

ROOT = Path(__file__).resolve().parents[3]


@dataclass
class MCPClientConfig:
    command: list[str] = field(
        default_factory=lambda: [sys.executable, "-m", "campusguide.mcp.policy_search_server"]
    )
    timeout_seconds: float = 30.0
    retries: int = 1
    startup_timeout_seconds: float = 300.0
    cwd: str | None = None
    env: dict[str, str] | None = None


@dataclass
class SearchResponse:
    data: SearchArticlesData
    server: str = "policy-search"
    tool: str = "search_articles"
    latency_ms: int = 0
    retries: int = 0
    error_code: str = ""


class SearchClient(Protocol):
    def search_articles(
        self,
        request: SearchArticlesRequest,
        *,
        timeout: float | None = None,
    ) -> SearchResponse: ...

    def get_article(self, article_id: str, part_no: int | None = None) -> GetArticleData: ...


class MetaClient(Protocol):
    def get_document_versions(self, doc_id: str) -> dict[str, Any]: ...

    def get_document(self, doc_id: str) -> dict[str, Any]: ...

    def resolve_version(
        self,
        doc_id: str,
        as_of: str | None = None,
        mode: str = "current",
    ) -> dict[str, Any]: ...

    def list_documents(self, **filters: Any) -> dict[str, Any]: ...


def _unwrap_structured(payload: Any) -> Any:
    if isinstance(payload, dict) and set(payload) == {"result"}:
        return payload["result"]
    return payload


class _AsyncMCPClient:
    """后台事件循环 + 单个持久 FastMCP 会话。"""

    def __init__(self, config: MCPClientConfig, *, server_name: str) -> None:
        self.config = config
        self.server_name = server_name
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._client: Any = None
        self._ready = threading.Event()
        self._queue: asyncio.Queue | None = None
        self._start_error: BaseException | None = None
        self._tool_names: set[str] = set()
        self._closed = False

    def start(self) -> None:
        if self._thread is not None:
            if self._start_error is not None:
                raise ToolError(
                    ErrorCode.TOOL_UNAVAILABLE,
                    f"{self.server_name} 启动失败：{self._start_error}",
                )
            return
        self._thread = threading.Thread(
            target=self._thread_main,
            name=f"mcp-{self.server_name}",
            daemon=True,
        )
        self._thread.start()
        if not self._ready.wait(self.config.startup_timeout_seconds):
            raise ToolError(
                ErrorCode.TOOL_UNAVAILABLE,
                f"{self.server_name} 启动超时（{self.config.startup_timeout_seconds}s）",
            )
        if self._start_error is not None:
            raise ToolError(
                ErrorCode.TOOL_UNAVAILABLE,
                f"{self.server_name} 启动失败：{self._start_error}",
            )

    def _thread_main(self) -> None:
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(self._serve())
        except BaseException as exc:
            self._start_error = exc
            self._ready.set()
        finally:
            with contextlib.suppress(Exception):
                # 给 transport 的 stopper/connect task 一点时间收尾，
                # 避免 loop 关闭时 asyncgen 在别的 task 里被 GC。
                self._loop.run_until_complete(asyncio.sleep(0.2))
            with contextlib.suppress(Exception):
                self._loop.run_until_complete(self._loop.shutdown_asyncgens())
            self._loop.close()

    async def _serve(self) -> None:
        from fastmcp import Client
        from fastmcp.client.transports import StdioTransport

        self._queue = asyncio.Queue()
        transport = StdioTransport(
            command=self.config.command[0],
            args=list(self.config.command[1:]),
            env=self._env(),
            cwd=self.config.cwd or str(ROOT),
            # keep_alive=True 会在 Client 退出后保留 subprocess/stdio asyncgen，
            # 事件循环关闭时触发 “asyncgen already running”。我们的生命周期
            # 由 QAService.close() 显式控制，不需要 transport 复用。
            keep_alive=False,
        )
        try:
            async with Client(
                transport,
                # SDK 的 read timeout 也覆盖 initialize；server 首次加载模型
                # 可能远超单次调用超时，因此这里放宽到 startup_timeout。
                # 单次工具调用仍由 _call_async 的 asyncio.wait_for 限制。
                timeout=self.config.startup_timeout_seconds,
                init_timeout=self.config.startup_timeout_seconds,
            ) as client:
                self._client = client
                try:
                    tools = await client.list_tools()
                    self._tool_names = {tool.name for tool in tools}
                except Exception:
                    self._tool_names = set()
                self._ready.set()
                # 所有工具调用都在进入 Client context 的同一 task 内执行；
                # 跨 task 关闭 anyio cancel scope 会在 stdio transport 上崩溃。
                while True:
                    item = await self._queue.get()
                    if item is None:
                        break
                    future, tool, arguments, timeout = item
                    try:
                        payload = await self._call_async(tool, arguments, timeout)
                    except BaseException as exc:
                        if not future.cancelled():
                            future.set_exception(exc)
                    else:
                        if not future.cancelled():
                            future.set_result(payload)
        except BaseException as exc:
            self._start_error = exc
            self._ready.set()
        finally:
            self._client = None

    def _env(self) -> dict[str, str]:
        env = dict(os.environ)
        src = str(ROOT / "src")
        existing = env.get("PYTHONPATH", "")
        env["PYTHONPATH"] = src + (os.pathsep + existing if existing else "")
        if self.config.env:
            env.update(self.config.env)
        return env

    def _ensure_started(self) -> None:
        if self._closed:
            raise ToolError(ErrorCode.TOOL_UNAVAILABLE, f"{self.server_name} 已关闭")
        self.start()

    async def _call_async(self, tool: str, arguments: dict[str, Any], timeout: float) -> Any:
        assert self._client is not None
        result = await asyncio.wait_for(
            self._client.call_tool(tool, arguments, raise_on_error=False),
            timeout=timeout,
        )
        if result.is_error:
            text = " ".join(
                getattr(content, "text", "") for content in (result.content or [])
            ).strip()
            if "validation" in text.lower() or "invalid" in text.lower():
                # 不透传 Pydantic 字段/类型细节给客户端。
                raise ToolError(
                    ErrorCode.INVALID_ARGUMENT,
                    f"{tool} 参数校验失败：字段类型或必填项不合法",
                )
            raise ToolError(ErrorCode.INTERNAL, f"{tool} 调用失败：{text[:500]}")
        payload = _unwrap_structured(result.structured_content)
        if payload is None:
            text = " ".join(
                getattr(content, "text", "") for content in (result.content or [])
            ).strip()
            if not text:
                raise ToolError(ErrorCode.INTERNAL, f"{tool} 返回空结果")
            try:
                payload = json.loads(text)
            except json.JSONDecodeError as exc:
                raise ToolError(ErrorCode.INTERNAL, f"{tool} 返回非 JSON 内容") from exc
        return payload

    def _call_once(
        self,
        tool: str,
        arguments: dict[str, Any],
        timeout: float,
    ) -> tuple[Any, int]:
        started = time.perf_counter()
        assert self._loop is not None and self._queue is not None
        future: concurrent.futures.Future = concurrent.futures.Future()
        self._loop.call_soon_threadsafe(
            self._queue.put_nowait,
            (future, tool, arguments, timeout),
        )
        try:
            payload = future.result(timeout=timeout + 5)
        except TimeoutError as exc:
            raise ToolError(
                ErrorCode.TOOL_UNAVAILABLE,
                f"{self.server_name}.{tool} 超时（{timeout}s）",
                retryable=True,
            ) from exc
        except ToolError:
            raise
        except Exception as exc:
            raise ToolError(
                ErrorCode.TOOL_UNAVAILABLE,
                f"{self.server_name}.{tool} 连接失败：{type(exc).__name__}: {exc}",
                retryable=True,
            ) from exc
        latency = int((time.perf_counter() - started) * 1000)
        return payload, latency

    def call(
        self,
        tool: str,
        arguments: dict[str, Any] | None = None,
        *,
        timeout: float | None = None,
    ) -> tuple[dict, int, int]:
        self._ensure_started()
        call_timeout = timeout if timeout is not None else self.config.timeout_seconds
        if self._tool_names and tool not in self._tool_names:
            if tool in {"get_flow", "list_flows", "get_calendar"}:
                raise ToolError(
                    ErrorCode.NOT_IMPLEMENTED,
                    f"{tool} 尚未实现（W5 提供），W4 不返回空数据伪装成功",
                )
            raise ToolError(
                ErrorCode.NOT_IMPLEMENTED,
                f"{self.server_name} 未注册工具 {tool}",
            )
        attempts = max(0, self.config.retries) + 1
        last_error: ToolError | None = None
        for attempt in range(attempts):
            try:
                payload, latency = self._call_once(tool, arguments or {}, call_timeout)
            except ToolError as exc:
                last_error = exc
                if exc.retryable and attempt + 1 < attempts:
                    continue
                raise
            if not isinstance(payload, dict):
                raise ToolError(ErrorCode.INTERNAL, f"{tool} 返回结构不是对象")
            if payload.get("schema_version") != "1.0":
                raise ToolError(
                    ErrorCode.INTERNAL,
                    f"{tool} schema_version 不匹配：{payload.get('schema_version')!r}",
                )
            if not payload.get("ok"):
                error = payload.get("error") or {}
                code_value = str(error.get("code", "INTERNAL"))
                try:
                    code = ErrorCode(code_value)
                except ValueError:
                    code = ErrorCode.INTERNAL
                error = ToolError(
                    code,
                    str(error.get("message", "未知 MCP 错误")),
                    retryable=bool(error.get("retryable", False)),
                    details=error.get("details") or {},
                )
                last_error = error
                if error.retryable and attempt + 1 < attempts:
                    continue
                raise error
            return payload, latency, attempt
        raise last_error or ToolError(ErrorCode.INTERNAL, f"{tool} 调用失败")

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._loop is not None and self._queue is not None:
            self._loop.call_soon_threadsafe(self._queue.put_nowait, None)
        if self._thread is not None:
            self._thread.join(timeout=10)
        self._thread = None


class _BaseToolClient:
    def __init__(self, config: MCPClientConfig, *, server_name: str) -> None:
        self._client = _AsyncMCPClient(config, server_name=server_name)

    def start(self) -> None:
        self._client.start()

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> _BaseToolClient:
        self.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def call(self, tool: str, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
        payload, _, _ = self._client.call(tool, arguments)
        return payload


class MCPPolicySearchClient(_BaseToolClient):
    def __init__(self, config: MCPClientConfig | None = None) -> None:
        super().__init__(config or MCPClientConfig(), server_name="policy-search")

    def search_articles(
        self,
        request: SearchArticlesRequest,
        *,
        timeout: float | None = None,
    ) -> SearchResponse:
        payload, latency, retries = self._client.call(
            "search_articles",
            {"request": request.model_dump(mode="json")},
            timeout=timeout,
        )
        data = SearchArticlesData.model_validate(payload.get("data") or {})
        return SearchResponse(
            data=data,
            latency_ms=latency,
            retries=retries,
        )

    def get_article(self, article_id: str, part_no: int | None = None) -> GetArticleData:
        payload = self.call(
            "get_article",
            {"article_id": article_id, "part_no": part_no},
        )
        return GetArticleData.model_validate(payload.get("data") or {})


class MCPPolicyMetaClient(_BaseToolClient):
    def __init__(self, config: MCPClientConfig | None = None) -> None:
        super().__init__(config or MCPClientConfig(), server_name="policy-meta")

    def get_document_versions(self, doc_id: str) -> dict[str, Any]:
        return self.call("get_document_versions", {"doc_id": doc_id})["data"]

    def get_document(self, doc_id: str) -> dict[str, Any]:
        return self.call("get_document", {"doc_id": doc_id})["data"]

    def resolve_version(
        self,
        doc_id: str,
        as_of: str | None = None,
        mode: str = "current",
    ) -> dict[str, Any]:
        return self.call(
            "resolve_version",
            {"doc_id": doc_id, "as_of": as_of, "mode": mode},
        )["data"]

    def list_documents(self, **filters: Any) -> dict[str, Any]:
        return self.call("list_documents", filters)["data"]

    def list_flows(
        self,
        *,
        audience: str | None = None,
        as_of: str | None = None,
    ) -> dict[str, Any]:
        return self.call(
            "list_flows",
            {"audience": audience, "as_of": as_of},
        )["data"]

    def get_flow(self, flow_id: str, *, as_of: str | None = None) -> dict[str, Any]:
        return self.call("get_flow", {"flow_id": flow_id, "as_of": as_of})["data"]

    def get_calendar(
        self,
        *,
        start: str | None = None,
        end: str | None = None,
        audience: str | None = None,
    ) -> dict[str, Any]:
        return self.call(
            "get_calendar",
            {"start": start, "end": end, "audience": audience},
        )["data"]
