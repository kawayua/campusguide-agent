"""W8 UI 后端适配：真实 LangGraph 运行时与进度事件契约。

UI 只依赖 ``ConversationReply`` 和本文件的 ``ProgressEvent``；真实图接入
保持惰性初始化，测试通过 fake backend 注入，不加载模型。
"""

from __future__ import annotations

import os
import queue
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import date
from typing import Any, Protocol


@dataclass(frozen=True)
class ProgressEvent:
    """只表示节点进度，不携带未核对答案文本。"""

    node: str
    label: str


class DemoBackend(Protocol):
    profile: str

    def stream(
        self, thread_id: str, message: str
    ) -> Iterator[ProgressEvent | Any]:  # pragma: no cover - protocol
        ...

    def reset(self, thread_id: str) -> None: ...

    def metadata(self) -> dict[str, Any]: ...

    def close(self) -> None: ...


class GraphBackend:
    """惰性创建 W6 图；首次请求才拉起 MCP/模型。"""

    def __init__(
        self,
        as_of: date | None = None,
        *,
        request_timeout_seconds: float | None = None,
    ) -> None:
        self._as_of = as_of or date.fromisoformat(os.getenv("DEMO_AS_OF", "2026-10-25"))
        self._runtime: Any | None = None
        self._profile_default = os.getenv("W6_MODEL_PROFILE", "").strip() or "vpro"
        self._display_profile = os.getenv("W8_DEMO_PROFILE", "").strip()
        timeout = (
            request_timeout_seconds
            if request_timeout_seconds is not None
            else os.getenv("W8_REQUEST_TIMEOUT_SECONDS", "120")
        )
        self.request_timeout_seconds = max(0.0, float(timeout))
        self._warmup_ms = 0
        self._inflight: dict[str, threading.Event] = {}
        self._inflight_lock = threading.Lock()
        self._metadata: dict[str, Any] = {
            "model_profile": self._profile_default,
            "display_profile": self.profile,
            "models": {
                "planner": os.getenv("PLANNER_MODEL", "—"),
                "advisor": os.getenv("ADVISOR_MODEL", "—"),
                "semantic": os.getenv("VERIFIER_MODEL", "—"),
            },
            "prompt_versions": {},
            "index_version": "—",
            "graph_version": "w6-quality-2026-10-28",
            "warmup_ms": 0,
            "request_timeout_seconds": self.request_timeout_seconds,
        }

    @property
    def profile(self) -> str:
        """页面展示档位；可用 W8_DEMO_PROFILE 与真实 run profile 解耦。"""
        if self._display_profile:
            return self._display_profile
        if self._runtime is not None:
            return str(self._runtime.model_profile or self._profile_default)
        return self._profile_default

    def _ensure_runtime(self) -> Any:
        if self._runtime is not None:
            return self._runtime
        from campusguide.qa.runtime import create_graph_runtime

        runtime = create_graph_runtime(today=self._as_of)
        self._runtime = runtime
        self._metadata = {
            "model_profile": runtime.model_profile,
            "display_profile": self.profile,
            "models": dict(runtime.models),
            "prompt_versions": dict(runtime.prompt_versions),
            "index_version": runtime.index_version,
            "graph_version": "w6-quality-2026-10-28",
            "warmup_ms": self._warmup_ms,
            "request_timeout_seconds": self.request_timeout_seconds,
        }
        return runtime

    def warmup(self) -> int:
        """启动时拉起 MCP 并预热检索；返回耗时毫秒。"""
        if self._runtime is not None:
            return self._warmup_ms
        started = time.perf_counter()
        runtime = self._ensure_runtime()
        self._warmup_ms = int((time.perf_counter() - started) * 1000)
        self._profile_default = str(runtime.model_profile or self._profile_default)
        self._metadata["warmup_ms"] = self._warmup_ms
        self._metadata["display_profile"] = self.profile
        return self._warmup_ms

    def _begin_request(self, thread_id: str) -> threading.Event:
        with self._inflight_lock:
            current = self._inflight.get(thread_id)
            if current is not None and not current.is_set():
                raise RuntimeError(
                    "上一轮请求尚未结束（可能已超时并在后台收尾）；请稍后重试，或重置当前会话。"
                )
            done = threading.Event()
            self._inflight[thread_id] = done
            return done

    def _end_request(self, thread_id: str, done: threading.Event) -> None:
        with self._inflight_lock:
            if self._inflight.get(thread_id) is done:
                self._inflight.pop(thread_id, None)
        done.set()

    def stream(self, thread_id: str, message: str) -> Iterator[ProgressEvent | Any]:
        done = self._begin_request(thread_id)
        yield ProgressEvent("prepare", "正在准备检索")
        results: queue.Queue[tuple[str, Any]] = queue.Queue()

        def worker() -> None:
            try:
                runtime = self._ensure_runtime()
                service = runtime.service
                stream = getattr(service, "handle_stream", None)
                if callable(stream):
                    for event in stream(thread_id, message, as_of=self._as_of):
                        kind = getattr(event, "kind", "")
                        if kind == "progress":
                            results.put(("progress", event))
                        elif kind == "final" and getattr(event, "reply", None) is not None:
                            results.put(("reply", event.reply))
                else:
                    results.put(("reply", service.handle(thread_id, message, as_of=self._as_of)))
            except BaseException as exc:
                results.put(("error", exc))
            finally:
                self._end_request(thread_id, done)
                results.put(("done", None))

        threading.Thread(
            target=worker,
            name=f"w8-graph-{thread_id[:8]}",
            daemon=True,
        ).start()

        timeout = self.request_timeout_seconds
        deadline = time.monotonic() + timeout if timeout > 0 else None
        while True:
            try:
                if deadline is None:
                    kind, payload = results.get()
                else:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise TimeoutError(f"本轮超过 {timeout:.0f} 秒，已停止等待")
                    kind, payload = results.get(timeout=min(remaining, 0.5))
            except queue.Empty:
                continue
            if kind == "done":
                return
            if kind == "error":
                raise payload
            if kind == "reply":
                yield payload
                continue
            yield ProgressEvent(
                str(getattr(payload, "node", "")),
                str(getattr(payload, "label", "") or "处理中"),
            )

    def reset(self, thread_id: str) -> None:
        if self._runtime is not None:
            self._runtime.service.reset(thread_id)

    def metadata(self) -> dict[str, Any]:
        return dict(self._metadata)

    def close(self) -> None:
        if self._runtime is not None:
            self._runtime.close()
            self._runtime = None
