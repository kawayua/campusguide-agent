"""Embedding provider 抽象与 bge-m3 实现。

离线测试只使用 FakeEmbedder；真实向量索引需要安装 ``w2`` 依赖组并下载模型。
"""

from __future__ import annotations

import hashlib
import math
from collections.abc import Sequence
from typing import Protocol


class EmbeddingProvider(Protocol):
    @property
    def model_name(self) -> str: ...

    @property
    def dimension(self) -> int: ...

    @property
    def device(self) -> str: ...

    def embed_query(self, text: str) -> list[float]: ...

    def embed_documents(self, texts: Sequence[str]) -> list[list[float]]: ...


def _l2_normalize(vector: list[float]) -> list[float]:
    norm = math.sqrt(sum(value * value for value in vector))
    if norm == 0:
        return vector
    return [value / norm for value in vector]


class FakeEmbedder:
    """确定性哈希向量：只用于离线测试，不具备语义能力。"""

    def __init__(self, dimension: int = 64) -> None:
        if dimension <= 0:
            raise ValueError("dimension 必须大于 0")
        self._dimension = dimension

    @property
    def model_name(self) -> str:
        return "fake-char-ngram"

    @property
    def dimension(self) -> int:
        return self._dimension

    @property
    def device(self) -> str:
        return "cpu"

    def _embed(self, text: str) -> list[float]:
        vector = [0.0] * self._dimension
        normalized = "".join(text.split())
        if not normalized:
            return vector
        # 单字 + 双字哈希：既保留词面重叠，又不依赖中文分词。
        grams = list(normalized) + [
            normalized[index : index + 2] for index in range(len(normalized) - 1)
        ]
        for gram in grams:
            digest = hashlib.sha256(gram.encode("utf-8")).digest()
            bucket = int.from_bytes(digest[:4], "big") % self._dimension
            sign = 1.0 if digest[4] % 2 == 0 else -1.0
            vector[bucket] += sign
        return _l2_normalize(vector)

    def embed_query(self, text: str) -> list[float]:
        return self._embed(text)

    def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
        return [self._embed(text) for text in texts]


class BgeM3Embedder:
    """本地 BAAI/bge-m3 dense embedding（1024 维，归一化）。"""

    def __init__(
        self,
        model_name: str = "BAAI/bge-m3",
        *,
        device: str = "auto",
        max_seq_length: int = 1024,
        batch_size: int = 16,
    ) -> None:
        self._model_name = model_name
        self._max_seq_length = max_seq_length
        self._batch_size = batch_size
        self._requested_device = device
        self._model = None
        self._device = "cpu"
        self._dimension = 0

    @property
    def model_name(self) -> str:
        return self._model_name

    @property
    def dimension(self) -> int:
        return self._dimension or 1024

    @property
    def device(self) -> str:
        return self._device

    def _load(self) -> None:
        if self._model is not None:
            return
        import torch
        from sentence_transformers import SentenceTransformer

        if self._requested_device == "auto":
            self._device = "cuda" if torch.cuda.is_available() else "cpu"
        else:
            self._device = self._requested_device
        try:
            # 缓存存在时优先本地加载：sentence-transformers 6.x 联网模式会
            # 反复请求 bge-m3 不存在的 processor 配置，导致启动等待数分钟。
            model = SentenceTransformer(
                self._model_name,
                device=self._device,
                local_files_only=True,
            )
        except (OSError, ValueError):
            # 首次运行或缓存不完整时再走在线下载。
            model = SentenceTransformer(
                self._model_name,
                device=self._device,
            )
        self._model = model
        self._model.max_seq_length = self._max_seq_length
        dimension_getter = getattr(
            self._model,
            "get_embedding_dimension",
            self._model.get_sentence_embedding_dimension,
        )
        self._dimension = int(dimension_getter())

    def _encode(self, texts: Sequence[str]) -> list[list[float]]:
        self._load()
        assert self._model is not None
        vectors = self._model.encode(
            list(texts),
            batch_size=self._batch_size,
            normalize_embeddings=True,
            show_progress_bar=False,
            convert_to_numpy=True,
        )
        return [[float(value) for value in vector] for vector in vectors]

    def embed_query(self, text: str) -> list[float]:
        return self._encode([text])[0]

    def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
        return self._encode(texts)
