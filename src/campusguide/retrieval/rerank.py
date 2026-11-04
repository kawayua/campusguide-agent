"""bge-reranker-v2-m3 重排封装。

模型只在 ``policy-search`` server 进程内加载；应用侧只看到 rerank 分数。
Reranker 不可用时抛 ``RerankUnavailable``，W4 默认失败关闭，不做无重排的
静默降级。
"""

from __future__ import annotations

import math
from typing import Protocol

DEFAULT_RERANKER_MODEL = "BAAI/bge-reranker-v2-m3"


class RerankUnavailable(RuntimeError):
    """重排模型不可用。"""


class Reranker(Protocol):
    @property
    def model_name(self) -> str: ...

    @property
    def device(self) -> str: ...

    def warmup(self) -> None: ...

    def score(self, query: str, passages: list[str]) -> list[float]: ...


def _sigmoid(value: float) -> float:
    if value >= 0:
        exponent = math.exp(-value)
        return 1.0 / (1.0 + exponent)
    exponent = math.exp(value)
    return exponent / (1.0 + exponent)


class BgeReranker:
    """CrossEncoder 形式的 bge-reranker-v2-m3，输出 sigmoid 归一化分数。"""

    def __init__(
        self,
        model_name: str = DEFAULT_RERANKER_MODEL,
        *,
        device: str = "auto",
        batch_size: int = 16,
        max_length: int = 512,
    ) -> None:
        self._model_name = model_name
        self._requested_device = device or "auto"
        self._batch_size = batch_size
        self._max_length = max_length
        self._model = None
        self._device = "cpu"

    @property
    def model_name(self) -> str:
        return self._model_name

    @property
    def device(self) -> str:
        return self._device

    def _load(self) -> None:
        if self._model is not None:
            return
        try:
            import torch
            from sentence_transformers import CrossEncoder
        except Exception as exc:  # pragma: no cover - 依赖缺失
            raise RerankUnavailable(f"reranker 依赖不可用：{exc}") from exc

        if self._requested_device == "auto":
            self._device = "cuda" if torch.cuda.is_available() else "cpu"
        else:
            self._device = self._requested_device
        try:
            model = CrossEncoder(
                self._model_name,
                device=self._device,
                max_length=self._max_length,
                activation_fn=torch.nn.Identity(),
                local_files_only=True,
            )
        except Exception as first_error:
            try:
                model = CrossEncoder(
                    self._model_name,
                    device=self._device,
                    max_length=self._max_length,
                    activation_fn=torch.nn.Identity(),
                )
            except Exception as exc:  # pragma: no cover - 需要真实模型
                raise RerankUnavailable(
                    f"reranker 加载失败：{type(exc).__name__}: {exc}；"
                    f"首次本地加载错误：{type(first_error).__name__}: {first_error}"
                ) from exc
        self._model = model

    def warmup(self) -> None:
        self._load()
        self.score("预热", ["CampusGuide 检索预热文本"])

    def score(self, query: str, passages: list[str]) -> list[float]:
        if not passages:
            return []
        self._load()
        assert self._model is not None
        pairs = [(query, passage) for passage in passages]
        try:
            predictions = self._model.predict(
                pairs,
                batch_size=self._batch_size,
                show_progress_bar=False,
                convert_to_numpy=True,
            )
        except Exception as exc:
            raise RerankUnavailable(f"reranker 推理失败：{type(exc).__name__}: {exc}") from exc

        # CrossEncoder defaults to Sigmoid for a single logit. Explicit Identity
        # above guarantees raw logits, so normalization happens exactly once.
        output: list[float] = []
        for row in predictions:
            if hasattr(row, "tolist"):
                row = row.tolist()
            value = float(row[-1]) if isinstance(row, list) else float(row)
            output.append(_sigmoid(value))
        return output


class FakeReranker:
    """离线测试用：按 query 与 passage 的字符重叠给确定性分数。"""

    def __init__(self, scores: dict[str, float] | None = None) -> None:
        self._scores = scores or {}
        self.calls = 0

    @property
    def model_name(self) -> str:
        return "fake-reranker"

    @property
    def device(self) -> str:
        return "cpu"

    def warmup(self) -> None:
        return None

    def score(self, query: str, passages: list[str]) -> list[float]:
        self.calls += 1
        query_chars = {char for char in query if not char.isspace()}
        values: list[float] = []
        for passage in passages:
            if passage in self._scores:
                values.append(self._scores[passage])
                continue
            overlap = len(query_chars & set(passage))
            values.append(min(0.99, 0.2 + overlap / max(len(query_chars), 1)))
        return values


class BrokenReranker:
    """契约测试用：始终抛 RerankUnavailable。"""

    @property
    def model_name(self) -> str:
        return "broken-reranker"

    @property
    def device(self) -> str:
        return "cpu"

    def warmup(self) -> None:
        raise RerankUnavailable("模拟 reranker 不可用")

    def score(self, query: str, passages: list[str]) -> list[float]:
        raise RerankUnavailable("模拟 reranker 不可用")
