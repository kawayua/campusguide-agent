"""Chat provider 抽象与 OpenAI-compatible 实现。

不绑定厂商 SDK；只要服务兼容 ``POST {base_url}/chat/completions`` 即可。
"""

from __future__ import annotations

import os
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

import httpx


class ChatProviderError(RuntimeError):
    """生成服务不可用、超时或返回非法结构。"""


class ChatConfigurationError(ChatProviderError):
    """缺少必要配置；启动阶段应直接报错。"""


@dataclass
class ChatResponse:
    content: str
    model: str = ""
    latency_ms: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    raw: dict[str, Any] = field(default_factory=dict)


class ChatProvider(Protocol):
    def complete(
        self,
        messages: list[dict[str, str]],
        *,
        temperature: float = 0.2,
        max_tokens: int = 1200,
    ) -> ChatResponse: ...


class OpenAICompatibleChat:
    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        model: str,
        timeout: float = 40.0,
        max_retries: int = 1,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        if not base_url or not api_key or not model:
            raise ChatConfigurationError(
                "缺少 LLM 配置：需要 LLM_BASE_URL、LLM_API_KEY、ADVISOR_MODEL"
            )
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._model = model
        self._timeout = timeout
        self._max_retries = max_retries
        self._transport = transport

    @classmethod
    def from_env(cls, *, timeout: float = 40.0) -> OpenAICompatibleChat:
        return cls(
            base_url=os.getenv("LLM_BASE_URL", ""),
            api_key=os.getenv("LLM_API_KEY", ""),
            model=os.getenv("ADVISOR_MODEL", ""),
            timeout=timeout,
        )

    @property
    def model(self) -> str:
        return self._model

    def complete(
        self,
        messages: list[dict[str, str]],
        *,
        temperature: float = 0.2,
        max_tokens: int = 1200,
    ) -> ChatResponse:
        payload = {
            "model": self._model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        started = time.perf_counter()
        last_error: Exception | None = None
        for attempt in range(self._max_retries + 1):
            try:
                with httpx.Client(
                    timeout=self._timeout,
                    transport=self._transport,
                ) as client:
                    response = client.post(
                        f"{self._base_url}/chat/completions",
                        headers={
                            "Authorization": f"Bearer {self._api_key}",
                            "Content-Type": "application/json",
                        },
                        json=payload,
                    )
                if response.status_code >= 400:
                    retryable = response.status_code == 429 or response.status_code >= 500
                    message = f"LLM 返回 HTTP {response.status_code}：{response.text[:300]}"
                    if attempt < self._max_retries and retryable:
                        last_error = ChatProviderError(message)
                        continue
                    raise ChatProviderError(message)
                body = response.json()
                choices = body.get("choices") or []
                if not choices:
                    raise ChatProviderError("LLM 响应缺少 choices")
                content = choices[0].get("message", {}).get("content", "")
                usage = body.get("usage") or {}
                return ChatResponse(
                    content=content or "",
                    model=body.get("model", self._model),
                    latency_ms=int((time.perf_counter() - started) * 1000),
                    prompt_tokens=int(usage.get("prompt_tokens", 0) or 0),
                    completion_tokens=int(usage.get("completion_tokens", 0) or 0),
                    raw=body,
                )
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                last_error = exc
                if attempt < self._max_retries:
                    continue
                raise ChatProviderError(f"LLM 网络错误：{exc}") from exc
            except ValueError as exc:
                raise ChatProviderError(f"LLM 响应不是合法 JSON：{exc}") from exc
        raise ChatProviderError(str(last_error) if last_error else "LLM 调用失败")


class FakeChatProvider:
    """离线测试用；可以按顺序返回预置响应或异常。"""

    def __init__(
        self,
        responses: Sequence[str | Exception],
        *,
        model: str = "fake-advisor",
    ) -> None:
        self._responses = list(responses)
        self._model = model
        self.calls: list[list[dict[str, str]]] = []

    @property
    def model(self) -> str:
        return self._model

    def complete(
        self,
        messages: list[dict[str, str]],
        *,
        temperature: float = 0.2,
        max_tokens: int = 1200,
    ) -> ChatResponse:
        self.calls.append(messages)
        if not self._responses:
            raise ChatProviderError("FakeChatProvider 没有剩余响应")
        item = self._responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return ChatResponse(
            content=item,
            model=self._model,
            latency_ms=1,
            prompt_tokens=max(1, sum(len(message["content"]) for message in messages) // 2),
            completion_tokens=max(1, len(item) // 2),
        )
