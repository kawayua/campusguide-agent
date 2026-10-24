"""W4 检索对比：dense-only vs hybrid RRF vs hybrid+rerank。

默认读取 `data/eval/dev_v0.jsonl` 与正式 `articles_v1` 索引；不调用 LLM。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import date
from pathlib import Path
from typing import Any

from qdrant_client import QdrantClient

from campusguide.config import load_retrieval_config
from campusguide.domain.enums import BlockType
from campusguide.domain.models import PolicyFilters
from campusguide.env import load_dotenv
from campusguide.eval.w3 import hash_configs
from campusguide.eval.w4 import (
    build_report,
    metadata,
    recall_at_k,
    reciprocal_rank,
    summarize_rows,
    write_report,
)
from campusguide.retrieval.embedder import BgeM3Embedder
from campusguide.retrieval.hybrid import HybridRetriever
from campusguide.retrieval.indexer import read_vector_manifest
from campusguide.retrieval.normalize import load_aliases
from campusguide.retrieval.rerank import BgeReranker
from campusguide.retrieval.service import DenseRetriever
from campusguide.retrieval.sparse import BM25SparseEncoder

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DEV = ROOT / "data" / "eval" / "dev_v0.jsonl"
DEFAULT_JSON = ROOT / "data" / "eval" / "w4-retrieval-report.json"
DEFAULT_MD = ROOT / "data" / "eval" / "w4-retrieval-report.md"
CONFIG_FILES = [
    ROOT / "configs" / "retrieval.yaml",
    ROOT / "configs" / "aliases.yaml",
]


def load_dev(path: Path) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as handle:
        for line_no, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            try:
                items.append(json.loads(line))
            except json.JSONDecodeError as exc:
                raise ValueError(f"{path}:{line_no} 不是合法 JSON") from exc
    return items


def _client(location: str | None) -> QdrantClient:
    location = location or os.getenv("QDRANT_LOCATION", "").strip()
    if location:
        Path(location).mkdir(parents=True, exist_ok=True)
        return QdrantClient(path=location)
    return QdrantClient(
        url=os.getenv("QDRANT_URL", "http://localhost:6333"),
        api_key=os.getenv("QDRANT_API_KEY") or None,
        timeout=30,
    )


def _row(
    item: dict[str, Any],
    *,
    retrieved: list[str],
    latency_ms: int,
    timing: dict[str, float] | None,
    warnings: list[str],
) -> dict[str, Any]:
    expected = list(item.get("expected_article_ids") or [])
    return {
        "id": item["id"],
        "question": item["question"],
        "category": item.get("category"),
        "block_types": item.get("block_types") or ["article"],
        "expected_article_ids": expected,
        "retrieved_article_ids": retrieved,
        "recall_at_8": recall_at_k(retrieved, expected),
        "reciprocal_rank": reciprocal_rank(retrieved, expected),
        "latency_ms": latency_ms,
        "timing": timing or {},
        "warnings": warnings,
    }


def _dense_rows(retriever: DenseRetriever, items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for item in items:
        block_types = [BlockType(value) for value in item.get("block_types") or ["article"]]
        filters = PolicyFilters(block_types=block_types, as_of=date.today())
        started = time.perf_counter()
        result = retriever.retrieve(item["question"], filters=filters)
        latency_ms = int((time.perf_counter() - started) * 1000)
        rows.append(
            _row(
                item,
                retrieved=[entry.article.article_id for entry in result.evidence],
                latency_ms=latency_ms,
                timing={},
                warnings=list(result.notes),
            )
        )
    return rows


def _hybrid_rows(
    retriever: HybridRetriever,
    items: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for item in items:
        block_types = [BlockType(value) for value in item.get("block_types") or ["article"]]
        filters = PolicyFilters(block_types=block_types, as_of=date.today())
        started = time.perf_counter()
        result = retriever.search_articles(item["question"], filters=filters)
        latency_ms = int((time.perf_counter() - started) * 1000)
        rows.append(
            _row(
                item,
                retrieved=[entry.article.article_id for entry in result.evidence],
                latency_ms=latency_ms,
                timing=dict(result.timing),
                warnings=list(result.warnings),
            )
        )
    return rows


def main() -> int:
    load_dotenv(ROOT / ".env")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dev", default=str(DEFAULT_DEV))
    parser.add_argument("--output-json", default=str(DEFAULT_JSON))
    parser.add_argument("--output-md", default=str(DEFAULT_MD))
    parser.add_argument("--qdrant-location", default=None)
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument(
        "--modes",
        default="dense,hybrid_rrf,hybrid_rerank",
        help="逗号分隔：dense, hybrid_rrf, hybrid_rerank",
    )
    args = parser.parse_args()
    modes = [mode.strip() for mode in args.modes.split(",") if mode.strip()]

    dev_path = Path(args.dev)
    items = load_dev(dev_path)
    if args.limit is not None:
        items = items[: args.limit]

    config = load_retrieval_config(ROOT / "configs" / "retrieval.yaml")
    manifest = read_vector_manifest(ROOT / "data" / "vector_manifest.json")
    index_version = str(manifest.get("index_version", ""))
    sparse_path = Path(manifest.get("sparse", {}).get("vocab_file") or "data/sparse_vocab.json")
    if not sparse_path.is_absolute():
        sparse_path = ROOT / sparse_path
    sparse_encoder = BM25SparseEncoder.load(sparse_path)
    aliases = load_aliases(ROOT / "configs" / "aliases.yaml")
    client = _client(args.qdrant_location)
    device = os.getenv("EMBEDDING_DEVICE", "auto")
    embedder = BgeM3Embedder(
        config.embedding_model or os.getenv("EMBEDDING_MODEL", "BAAI/bge-m3"),
        device=device,
    )

    mode_results: dict[str, dict[str, Any]] = {}
    if "dense" in modes:
        print("warming dense embedder ...", flush=True)
        retriever = DenseRetriever(
            client=client,
            embedder=embedder,
            config=config,
            top_k=config.recall_top_k,
            max_evidence=config.max_articles,
            min_dense_score=0.0,
            token_budget=config.token_budget,
            aliases=aliases,
        )
        retriever.warmup()
        started = time.perf_counter()
        rows = _dense_rows(retriever, items)
        elapsed = int((time.perf_counter() - started) * 1000)
        mode_results["dense"] = {
            "summary": summarize_rows(rows),
            "elapsed_ms": elapsed,
            "rows": rows,
        }
        print(f"dense: elapsed={elapsed}ms", flush=True)

    reranker = None
    if "hybrid_rerank" in modes:
        print("warming bge-reranker-v2-m3 ...", flush=True)
        reranker = BgeReranker(
            config.reranker_model,
            device=os.getenv("RERANKER_DEVICE", device),
        )
        reranker.warmup()

    for mode, use_rerank in (("hybrid_rrf", False), ("hybrid_rerank", True)):
        if mode not in modes:
            continue
        hybrid = HybridRetriever(
            client=client,
            embedder=embedder,
            sparse_encoder=sparse_encoder,
            reranker=reranker,
            config=config,
            index_version=index_version,
            top_k=config.recall_top_k,
            rerank_top_n=config.rerank_top_n,
            max_articles=config.max_articles,
            token_budget=config.token_budget,
            min_rerank_score=config.min_rerank_score,
            min_score_margin=config.min_score_margin,
            rrf_k=config.rrf_k,
            aliases=aliases,
            use_rerank=use_rerank,
        )
        if use_rerank:
            hybrid.warmup()
        started = time.perf_counter()
        rows = _hybrid_rows(hybrid, items)
        elapsed = int((time.perf_counter() - started) * 1000)
        mode_results[mode] = {
            "summary": summarize_rows(rows),
            "elapsed_ms": elapsed,
            "rows": rows,
        }
        print(f"{mode}: elapsed={elapsed}ms", flush=True)

    report = build_report(
        dataset=dev_path.name,
        dataset_path=dev_path,
        modes=mode_results,
        metadata=metadata(
            root=ROOT,
            index_version=index_version,
            vector_manifest_path=ROOT / "data" / "vector_manifest.json",
            config_hash=hash_configs(CONFIG_FILES),
        ),
    )
    json_path = Path(args.output_json)
    md_path = Path(args.output_md)
    write_report(report, json_path=json_path, md_path=md_path)

    for mode, payload in mode_results.items():
        summary = payload["summary"]
        overall = summary["overall"]
        print(
            f"{mode}: recall@8={overall['recall_at_8']} mrr={overall['mrr']} "
            f"p95={summary['latency_ms']['p95']:.1f}ms",
            flush=True,
        )
    print(f"report: {json_path}")
    print(f"report: {md_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
