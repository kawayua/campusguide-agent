"""Chat provider 抽象与 OpenAI-compatible 实现。

不绑定厂商 SDK；只要服务兼容 ``POST {base_url}/chat/completions`` 即可。

R8：供应商错误必须按可恢复性分类（限流/超时/鉴权/额度/解析/网络/服务端/熔断），
限流与超时按 Retry-After 或指数退避重试，连续失败达到阈值后熔断，避免把
“服务不可用”写成“知识库没有资料”。
"""

from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

import httpx

RATE_LIMIT = "rate_limit"
TIMEOUT = "timeout"
AUTH = "auth"
QUOTA = "quota"
PARSE = "parse"
NETWORK = "network"
SERVER = "server"
CIRCUIT_OPEN = "circuit_open"
UNKNOWN = "unknown"

_QUOTA_WORDS = (
    "insufficient_quota",
    "insufficient balance",
    "quota exhausted",
    "exceeded your current quota",
    "余额不足",
    "欠费",
    "额度不足",
)
_TIMEOUT_WORDS = ("timeout", "timed out", "超时")
_PARSE_WORDS = ("json", "choices", "decode", "解析")


def _structured_code(message: str) -> str:
    """Extract error.code / error.type from a JSON body, if present."""
    text = (message or "").strip()
    if not text.startswith("{"):
        return ""
    try:
        body = json.loads(text)
    except (ValueError, TypeError):
        return ""
    error = body.get("error") if isinstance(body, dict) else None
    if not isinstance(error, dict):
        return ""
    for key in ("code", "type"):
        value = error.get(key)
        if isinstance(value, str) and value:
            return value.lower()
    return ""


def classify_error(status_code: int | None, message: str) -> str:
    """Map a provider failure to one recovery class.

    结构化 error.code/type 先于状态码判断：HTTP 429 既可能是限流，也可能是
    额度耗尽，后者重试没有意义（报告 A7）。
    """
    text = (message or "").lower()
    code = _structured_code(message)
    combined = f"{code} {text}"
    if any(word in combined for word in _QUOTA_WORDS):
        return QUOTA
    if status_code in (401, 403):
        return AUTH
    if status_code == 429:
        return RATE_LIMIT
    if status_code in (401, 403):
        return AUTH
    if status_code is not None and status_code >= 500:
        return SERVER
    if any(word in text for word in _TIMEOUT_WORDS):
        return TIMEOUT
    if any(word in text for word in _PARSE_WORDS):
        return PARSE
    if status_code in (400, 404, 422):
        return PARSE
    return UNKNOWN


def is_retryable(kind: str, status_code: int | None) -> bool:
    if kind in {RATE_LIMIT, TIMEOUT, NETWORK, SERVER}:
        return True
    if kind in {AUTH, QUOTA, PARSE, CIRCUIT_OPEN}:
        return False
    return status_code is None


class ChatProviderError(RuntimeError):
    """Safe classification for bounded retries and diagnostics."""

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        retryable: bool | None = None,
        kind: str | None = None,
        retry_after: float | None = None,
        attempts: int = 1,
    ):
        super().__init__(message)
        self.status_code = status_code
        self.kind = kind or classify_error(status_code, message)
        self.retry_after = retry_after
        self.attempts = attempts
        self.retryable = (
            is_retryable(self.kind, status_code) if retryable is None else retryable
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "status_code": self.status_code,
            "retryable": self.retryable,
            "retry_after": self.retry_after,
            "attempts": self.attempts,
            "message": str(self)[:300],
        }


class ChatConfigurationError(ChatProviderError):
    """缺少必要配置；启动阶段应直接报错。"""

    def __init__(self, message: str):
        super().__init__(message, kind=AUTH, retryable=False)


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
        backoff_base: float = 0.4,
        backoff_cap: float = 8.0,
        circuit_threshold: int = 3,
        circuit_cooldown: float = 60.0,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
        retry_budget: float = 120.0,
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
        self._backoff_base = max(0.0, backoff_base)
        self._backoff_cap = max(0.0, backoff_cap)
        self._circuit_threshold = max(1, circuit_threshold)
        self._circuit_cooldown = max(0.0, circuit_cooldown)
        self._retry_budget = max(0.0, retry_budget)
        self._budget_spent = 0.0
        self._attempt_log: list[dict[str, Any]] = []
        self._sleep = sleep
        self._clock = clock
        self._consecutive_failures = 0
        self._open_until = 0.0
        self.last_error: ChatProviderError | None = None
        self.last_recovered = False

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

    def circuit_state(self) -> dict[str, Any]:
        return {
            "consecutive_failures": self._consecutive_failures,
            "open": self._clock() < self._open_until,
            "open_until": self._open_until,
            "threshold": self._circuit_threshold,
        }

    def _check_circuit(self) -> None:
        if self._clock() < self._open_until:
            raise ChatProviderError(
                "LLM 连续失败已熔断，稍后自动恢复",
                kind=CIRCUIT_OPEN,
                retryable=False,
            )

    def _record_failure(self) -> None:
        self._consecutive_failures += 1
        if self._consecutive_failures >= self._circuit_threshold:
            self._open_until = self._clock() + self._circuit_cooldown

    def attempt_log(self) -> list[dict[str, Any]]:
        """Attempt chain for traces: node-level fault, attempts, recovery."""
        return list(self._attempt_log)

    def reset_attempts(self) -> None:
        self._attempt_log.clear()
        self._budget_spent = 0.0
        self.last_recovered = False

    def _record_success(self) -> None:
        self._consecutive_failures = 0
        self._open_until = 0.0

    def _wait(self, attempt: int, retry_after: float | None) -> bool:
        """Sleep before a retry; return False when the wait budget is exhausted.

        A real Retry-After is honoured as sent — truncating it to the small
        backoff cap would just earn another 429 (报告 A7).
        """
        if retry_after is not None:
            delay = max(retry_after, 0.0)
            remaining = max(self._retry_budget - self._budget_spent, 0.0)
            if delay > remaining:
                return False
        else:
            delay = min(self._backoff_base * 2**attempt, self._backoff_cap)
        self._budget_spent += delay
        self._attempt_log.append(
            {"attempt": attempt + 1, "wait_s": round(delay, 3), "retry_after": retry_after}
        )
        if delay > 0:
            self._sleep(delay)
        return True

    @staticmethod
    def _retry_after(response: httpx.Response) -> float | None:
        raw = response.headers.get("Retry-After") or response.headers.get("retry-after")
        if not raw:
            return None
        token = raw.strip()
        try:
            return max(float(token), 0.0)
        except ValueError:
            pass
        try:
            target = parsedate_to_datetime(token)
        except (TypeError, ValueError):
            return None
        if target.tzinfo is None:
            target = target.replace(tzinfo=timezone.utc)
        return max((target - datetime.now(timezone.utc)).total_seconds(), 0.0)

    def complete(
        self,
        messages: list[dict[str, str]],
        *,
        temperature: float = 0.2,
        max_tokens: int = 1200,
    ) -> ChatResponse:
        self._check_circuit()
        payload = {
            "model": self._model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        started = time.perf_counter()
        last_error: ChatProviderError | None = None
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
                    kind = classify_error(response.status_code, response.text)
                    retryable = is_retryable(kind, response.status_code)
                    message = f"LLM 返回 HTTP {response.status_code}：{response.text[:300]}"
                    error = ChatProviderError(
                        message,
                        status_code=response.status_code,
                        retryable=retryable,
                        kind=kind,
                        retry_after=self._retry_after(response),
                        attempts=attempt + 1,
                    )
                    self.last_error = error
                    if attempt < self._max_retries and retryable:
                        self._record_failure()
                        if not self._wait(attempt, error.retry_after):
                            # 等待预算不足：提前重试只会再次 429，直接报告。
                            error.retryable = False
                            raise error
                        last_error = error
                        continue
                    self._record_failure()
                    raise error
                body = response.json()
                choices = body.get("choices") or []
                if not choices:
                    raise ChatProviderError("LLM 响应缺少 choices", kind=PARSE, retryable=False)
                content = choices[0].get("message", {}).get("content", "")
                self.last_recovered = bool(self._attempt_log)
                self._attempt_log.append({"attempt": attempt + 1, "outcome": "ok"})
                self._record_success()
                self.last_error = None
                return ChatResponse(
                    content=content or "",
                    model=body.get("model", self._model),
                    latency_ms=int((time.perf_counter() - started) * 1000),
                    prompt_tokens=int((body.get("usage") or {}).get("prompt_tokens", 0) or 0),
                    completion_tokens=int(
                        (body.get("usage") or {}).get("completion_tokens", 0) or 0
                    ),
                    raw=body,
                )
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                kind = TIMEOUT if isinstance(exc, httpx.TimeoutException) else NETWORK
                error = ChatProviderError(
                    f"LLM 网络错误（{kind}）：{exc}",
                    kind=kind,
                    retryable=True,
                    attempts=attempt + 1,
                )
                self.last_error = error
                self._record_failure()
                if attempt < self._max_retries:
                    self._wait(attempt, None)
                    last_error = error
                    continue
                raise error from exc
            except ValueError as exc:
                raise ChatProviderError(
                    f"LLM 响应不是合法 JSON：{exc}",
                    kind=PARSE,
                    retryable=False,
                    attempts=attempt + 1,
                ) from exc
        raise last_error or ChatProviderError("LLM 调用失败")


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
        self.options: list[dict[str, Any]] = []

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
        self.options.append({"temperature": temperature, "max_tokens": max_tokens})
        if not self._responses:
            raise ChatProviderError("FakeChatProvider 没有剩余响应", retryable=False)
        item = self._responses.pop(0)
        if isinstance(item, Exception):
            if isinstance(item, ChatProviderError):
                raise item
            # 预置异常也要带分类，但保留原始异常类型供调用方断言。
            raise item
        return ChatResponse(
            content=item,
            model=self._model,
            latency_ms=1,
            prompt_tokens=max(1, sum(len(message["content"]) for message in messages) // 2),
            completion_tokens=max(1, len(item) // 2),
        )