"""BM25 风格 sparse 编码器。

W4 不引入额外模型依赖：中文按“单字 + 双字”切词，英文/数字按连续串，
在语料上统计 df 并计算 BM25 IDF；文档向量存饱和 TF 权重，查询向量存
IDF。Qdrant 稀疏点积即得到 BM25 分数。token 通过稳定 blake2b 哈希映射
到 uint32，索引与查询必须使用同一份 vocab/idf。

接口保留 ``SparseEncoder``，W6/W7 可替换为 bge-m3 sparse 而无需改 MCP。
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import unicodedata
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from qdrant_client.models import SparseVector

TOKENIZER_VERSION = "cjk-char-bigram-v1"
DEFAULT_K1 = 1.2
DEFAULT_B = 0.75
SPARSE_SCHEMA_VERSION = "1.0"

_CJK = r"\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff"
_TOKEN_RE = re.compile(rf"[{_CJK}]+|[a-z0-9]+")
_CJK_RE = re.compile(rf"[{_CJK}]")


@runtime_checkable
class SparseEncoder(Protocol):
    def encode_documents(self, texts: list[str]) -> list[SparseVector]: ...

    def encode_query(self, text: str) -> SparseVector: ...

    @property
    def version(self) -> str: ...


def tokenize(text: str) -> list[str]:
    """确定性分词：CJK 单字 + 相邻双字；ASCII 字母/数字按连续串。"""
    if not text:
        return []
    normalized = unicodedata.normalize("NFKC", text).lower()
    tokens: list[str] = []
    for match in _TOKEN_RE.finditer(normalized):
        chunk = match.group(0)
        if _CJK_RE.match(chunk):
            tokens.extend(chunk)
            tokens.extend(chunk[index : index + 2] for index in range(len(chunk) - 1))
        else:
            tokens.append(chunk)
    return tokens


def _stable_index(token: str) -> int:
    digest = hashlib.blake2b(token.encode("utf-8"), digest_size=4).digest()
    return int.from_bytes(digest, "big")


@dataclass(frozen=True)
class SparseIndex:
    """可序列化的 vocab/idf 快照。"""

    tokens: tuple[str, ...]
    indices: tuple[int, ...]
    idf: tuple[float, ...]
    df: tuple[int, ...]
    doc_count: int
    avg_doc_len: float
    k1: float
    b: float
    tokenizer_version: str = TOKENIZER_VERSION
    schema_version: str = SPARSE_SCHEMA_VERSION

    @property
    def idf_hash(self) -> str:
        digest = hashlib.sha256()
        digest.update(self.tokenizer_version.encode("utf-8"))
        for token, index, idf in zip(self.tokens, self.indices, self.idf, strict=True):
            digest.update(token.encode("utf-8"))
            digest.update(index.to_bytes(4, "big"))
            digest.update(f"{idf:.8f}".encode("ascii"))
        return digest.hexdigest()

    @property
    def version(self) -> str:
        return f"bm25-k1{self.k1}-b{self.b}-{self.idf_hash[:12]}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "tokenizer_version": self.tokenizer_version,
            "k1": self.k1,
            "b": self.b,
            "doc_count": self.doc_count,
            "avg_doc_len": self.avg_doc_len,
            "vocab_size": len({*self.indices}),
            "token_count": len(self.tokens),
            "collisions": len(self.tokens) - len({*self.indices}),
            "idf_hash": self.idf_hash,
            "version": self.version,
            "tokens": list(self.tokens),
            "indices": list(self.indices),
            "idf": list(self.idf),
            "df": list(self.df),
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> SparseIndex:
        if payload.get("schema_version") != SPARSE_SCHEMA_VERSION:
            raise ValueError(f"不支持的 sparse schema：{payload.get('schema_version')!r}")
        return cls(
            tokens=tuple(payload["tokens"]),
            indices=tuple(int(value) for value in payload["indices"]),
            idf=tuple(float(value) for value in payload["idf"]),
            df=tuple(int(value) for value in payload["df"]),
            doc_count=int(payload["doc_count"]),
            avg_doc_len=float(payload["avg_doc_len"]),
            k1=float(payload["k1"]),
            b=float(payload["b"]),
            tokenizer_version=str(payload.get("tokenizer_version", TOKENIZER_VERSION)),
        )


class BM25SparseEncoder:
    """基于固定 vocab/idf 的 BM25 稀疏编码器。"""

    def __init__(self, index: SparseIndex) -> None:
        self._index = index
        self._entries: dict[str, tuple[int, float]] = {
            token: (position, idf)
            for position, (token, idf) in enumerate(zip(index.tokens, index.idf, strict=True))
        }
        self._index_positions = index.indices

    @classmethod
    def fit(
        cls,
        texts: list[str],
        *,
        k1: float = DEFAULT_K1,
        b: float = DEFAULT_B,
    ) -> BM25SparseEncoder:
        doc_tokens = [tokenize(text) for text in texts]
        doc_count = len(doc_tokens)
        df_by_token: Counter[str] = Counter()
        for tokens in doc_tokens:
            for token in set(tokens):
                df_by_token[token] += 1

        # 同哈希碰撞的词共享一个 uint32 桶；df 取桶内最大值，避免重复计分。
        bucket_df: dict[int, int] = {}
        bucket_tokens: dict[int, list[str]] = {}
        for token, count in df_by_token.items():
            index = _stable_index(token)
            bucket_df[index] = max(bucket_df.get(index, 0), count)
            bucket_tokens.setdefault(index, []).append(token)

        total_length = sum(len(tokens) for tokens in doc_tokens)
        avg_doc_len = (total_length / doc_count) if doc_count else 0.0

        tokens_out: list[str] = []
        indices_out: list[int] = []
        idf_out: list[float] = []
        df_out: list[int] = []
        for index in sorted(bucket_df):
            df = bucket_df[index]
            idf = math.log(1 + (doc_count - df + 0.5) / (df + 0.5))
            for token in sorted(bucket_tokens[index]):
                tokens_out.append(token)
                indices_out.append(index)
                idf_out.append(idf)
                df_out.append(df)

        return cls(
            SparseIndex(
                tokens=tuple(tokens_out),
                indices=tuple(indices_out),
                idf=tuple(idf_out),
                df=tuple(df_out),
                doc_count=doc_count,
                avg_doc_len=avg_doc_len,
                k1=k1,
                b=b,
            )
        )

    @property
    def version(self) -> str:
        return self._index.version

    @property
    def index(self) -> SparseIndex:
        return self._index

    def _weight(self, term_frequency: int, doc_length: int) -> float:
        if term_frequency <= 0:
            return 0.0
        k1 = self._index.k1
        b = self._index.b
        avg = self._index.avg_doc_len or 1.0
        denominator = term_frequency + k1 * (1 - b + b * doc_length / avg)
        return term_frequency * (k1 + 1) / denominator

    def encode_documents(self, texts: list[str]) -> list[SparseVector]:
        vectors: list[SparseVector] = []
        for text in texts:
            tokens = tokenize(text)
            if not tokens:
                vectors.append(SparseVector(indices=[], values=[]))
                continue
            counts = Counter(tokens)
            doc_length = len(tokens)
            weights: dict[int, float] = {}
            for token, count in counts.items():
                entry = self._entries.get(token)
                if entry is None:
                    continue
                position, _ = entry
                index = self._index_positions[position]
                weights[index] = weights.get(index, 0.0) + self._weight(count, doc_length)
            ordered = sorted(weights.items())
            vectors.append(
                SparseVector(
                    indices=[index for index, _ in ordered],
                    values=[value for _, value in ordered],
                )
            )
        return vectors

    def encode_query(self, text: str) -> SparseVector:
        tokens = tokenize(text)
        if not tokens:
            return SparseVector(indices=[], values=[])
        # 查询侧使用同一 vocab/idf；重复词只计一次 idf。
        weights: dict[int, float] = {}
        for token in dict.fromkeys(tokens):
            entry = self._entries.get(token)
            if entry is None:
                continue
            position, idf = entry
            index = self._index_positions[position]
            weights[index] = weights.get(index, 0.0) + idf
        ordered = sorted(weights.items())
        return SparseVector(
            indices=[index for index, _ in ordered],
            values=[value for _, value in ordered],
        )

    def to_dict(self) -> dict[str, Any]:
        return self._index.to_dict()

    def save(self, path: str | Path) -> Path:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(path.name + ".part")
        temporary.write_text(
            json.dumps(self.to_dict(), ensure_ascii=False, separators=(",", ":")) + "\n",
            encoding="utf-8",
        )
        temporary.replace(path)
        return path

    @classmethod
    def load(cls, path: str | Path) -> BM25SparseEncoder:
        path = Path(path)
        if not path.exists():
            raise FileNotFoundError(f"缺少 sparse vocab：{path}")
        payload = json.loads(path.read_text(encoding="utf-8"))
        return cls(SparseIndex.from_dict(payload))


class NullSparseEncoder:
    """dense-only 回退：所有文本返回空 sparse 向量。"""

    @property
    def version(self) -> str:
        return "null"

    def encode_documents(self, texts: list[str]) -> list[SparseVector]:
        return [SparseVector(indices=[], values=[]) for _ in texts]

    def encode_query(self, text: str) -> SparseVector:
        return SparseVector(indices=[], values=[])
