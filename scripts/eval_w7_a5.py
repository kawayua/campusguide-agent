"""W7 A5 检索消融：dense-only / hybrid RRF / hybrid+rerank。

在 50 题冻结集的 40 道可答题上运行；不调用 LLM，不改测试集。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import time
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from qdrant_client import QdrantClient

from campusguide.config import load_organization, load_retrieval_config
from campusguide.domain.enums import Audience, BlockType
from campusguide.domain.models import PolicyFilters
from campusguide.domain.profiles import UserProfile
from campusguide.env import load_dotenv
from campusguide.eval.w3 import git_info, hash_configs
from campusguide.eval.w4 import recall_at_k, reciprocal_rank, summarize_rows
from campusguide.eval.w7 import verify_frozen
from campusguide.retrieval.embedder import BgeM3Embedder
from campusguide.retrieval.hybrid import HybridRetriever
from campusguide.retrieval.indexer import read_vector_manifest
from campusguide.retrieval.normalize import load_aliases
from campusguide.retrieval.profile_expand import ProfileLexicon
from campusguide.retrieval.rerank import BgeReranker
from campusguide.retrieval.service import DenseRetriever
from campusguide.retrieval.sparse import BM25SparseEncoder

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_TESTSET = ROOT / "data" / "eval" / "test_v1.jsonl"
DEFAULT_MANIFEST = ROOT / "data" / "eval" / "test_v1.manifest.json"
DEFAULT_JSON = ROOT / "data" / "eval" / "w7-a5-retrieval.json"
DEFAULT_MD = ROOT / "data" / "eval" / "w7-a5-retrieval.md"
CONFIG_FILES = [
    ROOT / "configs" / "retrieval.yaml",
    ROOT / "configs" / "aliases.yaml",
]


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


def _filters(item: dict[str, Any]) -> PolicyFilters:
    block_types = [BlockType(value) for value in item.get("block_types") or ["article"]]
    audience_value = (item.get("user_profile") or {}).get("audience")
    audience = None
    if audience_value and audience_value != Audience.ALL.value:
        audience = [Audience(audience_value)]
    profile = UserProfile.model_validate(item.get("user_profile") or {})
    return PolicyFilters(
        block_types=block_types,
        audience=audience,
        profile=None if profile.is_empty else profile,
        as_of=date.fromisoformat(item["as_of"]),
    )


def _row(
    item: dict[str, Any],
    *,
    retrieved: list[str],
    latency_ms: int,
    timing: dict[str, float] | None,
    warnings: list[str],
) -> dict[str, Any]:
    expected = list(item.get("gold_article_ids") or [])
    return {
        "qid": item["qid"],
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


def _run_dense(retriever: DenseRetriever, items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for item in items:
        started = time.perf_counter()
        result = retriever.retrieve(item["question"], filters=_filters(item))
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


def _run_hybrid(retriever: HybridRetriever, items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for item in items:
        started = time.perf_counter()
        result = retriever.search_articles(item["question"], filters=_filters(item))
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


def _render(report: dict[str, Any]) -> str:
    def fmt(value: Any) -> str:
        return "n/a" if value is None else f"{value:.3f}"

    metadata = report["metadata"]
    lines = [
        "# W7 A5 检索消融",
        "",
        f"- 数据集：`{report['dataset']}`（文件 `{report['dataset_sha256'][:12]}…`）",
        f"- 冻结测试集 hash：`{report['testset_sha256'][:12]}…`",
        f"- 可答题：{report['questions']} 道",
        f"- git：`{metadata.get('git_commit', 'n/a')}`（dirty={metadata.get('git_dirty')}）",
        f"- 索引：`{metadata.get('index_version', 'n/a')}`",
        "",
        "## 指标（Recall@8 / MRR）",
        "",
        "| 模式 | article Recall@8 | article MRR | section Recall@8 | section MRR | "
        "overall Recall@8 | overall MRR |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]
    labels = {
        "dense": "dense-only",
        "hybrid_rrf": "dense+sparse RRF",
        "hybrid_rerank": "RRF + bge-reranker-v2-m3",
    }
    for mode, payload in report["modes"].items():
        summary = payload["summary"]
        article = summary.get("article") or {}
        section = summary.get("section") or {}
        overall = summary.get("overall") or {}
        lines.append(
            f"| `{mode}`（{labels.get(mode, mode)}） | "
            f"{fmt(article.get('recall_at_8'))} | {fmt(article.get('mrr'))} | "
            f"{fmt(section.get('recall_at_8'))} | {fmt(section.get('mrr'))} | "
            f"{fmt(overall.get('recall_at_8'))} | {fmt(overall.get('mrr'))} |"
        )
    lines.extend(
        [
            "",
            "## 延迟",
            "",
            "| 模式 | p50 | p95 | rerank p50 | rerank p95 |",
            "|---|---:|---:|---:|---:|",
        ]
    )
    for mode, payload in report["modes"].items():
        summary = payload["summary"]
        latency = summary["latency_ms"]
        rerank = summary["rerank_latency_ms"]
        rerank_p50 = "n/a" if rerank["p50"] is None else f"{rerank['p50']:.1f} ms"
        rerank_p95 = "n/a" if rerank["p95"] is None else f"{rerank['p95']:.1f} ms"
        lines.append(
            f"| `{mode}` | {latency['p50']:.1f} ms | {latency['p95']:.1f} ms | "
            f"{rerank_p50} | {rerank_p95} |"
        )
    lines.extend(
        [
            "",
            "## 说明",
            "",
            "- A5 只比较检索层；三种模式使用同一冻结题、同一索引、同一过滤配置。",
            "- dense-only 不做 profile 查询扩展；hybrid 模式保留 profile 扩展与过滤。",
            "- A1 固定分块、A3 流程 DAG 为 N/A。",
            "",
        ]
    )
    return "\n".join(lines)


def main() -> int:
    load_dotenv(ROOT / ".env")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--testset", default=str(DEFAULT_TESTSET))
    parser.add_argument("--manifest", default=str(DEFAULT_MANIFEST))
    parser.add_argument("--output-json", default=str(DEFAULT_JSON))
    parser.add_argument("--output-md", default=str(DEFAULT_MD))
    parser.add_argument("--qdrant-location", default=None)
    args = parser.parse_args()

    testset_path = Path(args.testset)
    rows, frozen_manifest = verify_frozen(
        testset_path=testset_path,
        manifest_path=Path(args.manifest),
    )
    items = [
        item for item in rows if not item.get("should_abstain") and item.get("gold_article_ids")
    ]

    config = load_retrieval_config(ROOT / "configs" / "retrieval.yaml")
    manifest = read_vector_manifest(ROOT / "data" / "vector_manifest.json")
    index_version = str(manifest.get("index_version", ""))
    sparse_path = Path(manifest.get("sparse", {}).get("vocab_file") or "data/sparse_vocab.json")
    if not sparse_path.is_absolute():
        sparse_path = ROOT / sparse_path
    sparse_encoder = BM25SparseEncoder.load(sparse_path)
    aliases = load_aliases(ROOT / "configs" / "aliases.yaml")
    organization = load_organization(ROOT / "configs" / "organization.yaml")
    profile_lexicon = ProfileLexicon.from_organization(organization)
    client = _client(args.qdrant_location)
    device = os.getenv("EMBEDDING_DEVICE", "auto")
    embedder = BgeM3Embedder(
        config.embedding_model or os.getenv("EMBEDDING_MODEL", "BAAI/bge-m3"),
        device=device,
    )

    modes: dict[str, dict[str, Any]] = {}
    print("warming dense embedder ...", flush=True)
    dense = DenseRetriever(
        client=client,
        embedder=embedder,
        config=config,
        top_k=config.recall_top_k,
        max_evidence=config.max_articles,
        min_dense_score=0.0,
        token_budget=config.token_budget,
        aliases=aliases,
    )
    dense.warmup()
    started = time.perf_counter()
    dense_rows = _run_dense(dense, items)
    modes["dense"] = {
        "summary": summarize_rows(dense_rows),
        "elapsed_ms": int((time.perf_counter() - started) * 1000),
        "rows": dense_rows,
    }
    print(f"dense: {modes['dense']['elapsed_ms']}ms", flush=True)

    print("warming bge-reranker-v2-m3 ...", flush=True)
    reranker = BgeReranker(
        config.reranker_model,
        device=os.getenv("RERANKER_DEVICE", device),
    )
    reranker.warmup()

    for mode, use_rerank in (("hybrid_rrf", False), ("hybrid_rerank", True)):
        hybrid = HybridRetriever(
            client=client,
            embedder=embedder,
            sparse_encoder=sparse_encoder,
            reranker=reranker if use_rerank else None,
            config=config,
            index_version=index_version,
            top_k=config.recall_top_k,
            rerank_candidates=config.rerank_candidates,
            rerank_top_n=config.rerank_top_n,
            max_articles=config.max_articles,
            max_table_sections=config.max_table_sections,
            token_budget=config.token_budget,
            min_rerank_score=config.min_rerank_score,
            min_score_margin=config.min_score_margin,
            rrf_k=config.rrf_k,
            aliases=aliases,
            profile_lexicon=profile_lexicon,
            use_rerank=use_rerank,
        )
        if use_rerank:
            hybrid.warmup()
        started = time.perf_counter()
        mode_rows = _run_hybrid(hybrid, items)
        modes[mode] = {
            "summary": summarize_rows(mode_rows),
            "elapsed_ms": int((time.perf_counter() - started) * 1000),
            "rows": mode_rows,
        }
        print(f"{mode}: {modes[mode]['elapsed_ms']}ms", flush=True)

    report = {
        "schema_version": "w7-a5-v1",
        "dataset": testset_path.name,
        "dataset_sha256": hashlib.sha256(testset_path.read_bytes()).hexdigest(),
        "testset_sha256": frozen_manifest["sha256"],
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "questions": len(items),
        "metadata": {
            **git_info(ROOT),
            "index_version": index_version,
            "vector_manifest_sha256": hashlib.sha256(
                (ROOT / "data" / "vector_manifest.json").read_bytes()
            ).hexdigest(),
            "config_hash": hash_configs(CONFIG_FILES),
            "embedding_model": config.embedding_model,
            "reranker_model": config.reranker_model,
        },
        "modes": modes,
    }
    json_path = Path(args.output_json)
    md_path = Path(args.output_md)
    json_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    md_path.write_text(_render(report), encoding="utf-8")
    for mode, payload in modes.items():
        summary = payload["summary"]
        print(
            f"{mode}: recall@8={summary['overall']['recall_at_8']} "
            f"mrr={summary['overall']['mrr']} "
            f"p95={summary['latency_ms']['p95']:.1f}ms",
            flush=True,
        )
    print(f"report: {json_path}")
    print(f"report: {md_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
