"""构建 W4 混合向量索引：articles.jsonl -> Qdrant + vector_manifest.json。

默认写入 bge-m3 dense + BM25 sparse；``--dense-only`` 保留 W2/W3 旧行为。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from pathlib import Path

from qdrant_client import QdrantClient

from campusguide.config import load_retrieval_config
from campusguide.env import load_dotenv
from campusguide.retrieval.connections import qdrant_trust_env
from campusguide.retrieval.embedder import BgeM3Embedder
from campusguide.retrieval.indexer import (
    VectorIndexError,
    build_vector_index,
    embedding_text,
    load_articles,
    verify_vector_manifest,
    write_vector_manifest,
)
from campusguide.retrieval.sparse import BM25SparseEncoder

ROOT = Path(__file__).resolve().parents[1]


def _progress(written: int, total: int) -> None:
    print(f"  embedded {written}/{total}", flush=True)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main() -> int:
    load_dotenv(ROOT / ".env")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--articles", default=str(ROOT / "data" / "processed" / "articles.jsonl"))
    parser.add_argument("--retrieval-config", default=str(ROOT / "configs" / "retrieval.yaml"))
    parser.add_argument("--text-manifest", default=str(ROOT / "data" / "manifest.json"))
    parser.add_argument("--vector-manifest", default=str(ROOT / "data" / "vector_manifest.json"))
    parser.add_argument("--sparse-vocab", default=str(ROOT / "data" / "sparse_vocab.json"))
    parser.add_argument("--qdrant-url", default=None)
    parser.add_argument("--qdrant-location", default=None)
    parser.add_argument("--recreate", action="store_true")
    parser.add_argument("--dense-only", action="store_true", help="只写 dense，保留 W3 兼容路径")
    parser.add_argument("--batch-size", type=int, default=16)
    parser.add_argument("--device", default=None)
    parser.add_argument("--limit", type=int, default=None, help="只索引前 N 个块，用于 smoke")
    parser.add_argument("--model", default=None)
    args = parser.parse_args()

    if (
        args.limit is not None
        and Path(args.vector_manifest).resolve()
        == (ROOT / "data" / "vector_manifest.json").resolve()
    ):
        print(
            "error: --limit 只能配合独立的 --vector-manifest 和 --qdrant-location 使用，"
            "避免半量索引覆盖正式 manifest",
            file=sys.stderr,
        )
        return 1
    if (
        args.limit is not None
        and not args.dense_only
        and Path(args.sparse_vocab).resolve() == (ROOT / "data" / "sparse_vocab.json").resolve()
    ):
        print(
            "error: --limit 必须配合独立的 --sparse-vocab，避免半量 vocab 覆盖正式文件",
            file=sys.stderr,
        )
        return 1

    retrieval = load_retrieval_config(Path(args.retrieval_config))
    articles = load_articles(args.articles)
    if args.limit is not None:
        articles = articles[: args.limit]

    text_manifest_path = Path(args.text_manifest)
    source_content_hash = None
    if text_manifest_path.exists():
        try:
            source_content_hash = json.loads(text_manifest_path.read_text(encoding="utf-8")).get(
                "content_hash"
            )
        except json.JSONDecodeError:
            source_content_hash = None

    sparse_encoder = None
    if not args.dense_only:
        indexable = [article for article in articles if article.is_indexable]
        sparse_encoder = BM25SparseEncoder.fit(
            [embedding_text(article) for article in indexable],
            k1=retrieval.bm25_k1,
            b=retrieval.bm25_b,
        )
        sparse_path = Path(args.sparse_vocab)
        sparse_encoder.save(sparse_path)
        print(
            f"Sparse vocab built: tokens={len(sparse_encoder.index.tokens)} "
            f"docs={sparse_encoder.index.doc_count} version={sparse_encoder.version} "
            f"file={sparse_path}"
        )

    if args.qdrant_location:
        Path(args.qdrant_location).mkdir(parents=True, exist_ok=True)
        client = QdrantClient(path=args.qdrant_location)
    else:
        client = QdrantClient(
            url=args.qdrant_url or "http://localhost:6333",
            trust_env=qdrant_trust_env(),
            timeout=30,
        )

    embedder = BgeM3Embedder(
        args.model or retrieval.embedding_model,
        device=args.device or os.getenv("EMBEDDING_DEVICE", "auto"),
        batch_size=args.batch_size,
    )
    try:
        manifest = build_vector_index(
            client=client,
            articles=articles,
            embedder=embedder,
            retrieval=retrieval,
            sparse_encoder=sparse_encoder,
            source_content_hash=source_content_hash,
            batch_size=args.batch_size,
            recreate=args.recreate,
            progress=_progress,
        )
    except VectorIndexError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:
        print(
            f"error: 向量构建失败（{type(exc).__name__}）：{exc}\n"
            "提示：Qdrant 服务是否已启动？模型是否已下载？",
            file=sys.stderr,
        )
        return 1

    manifest["source_content_hash"] = source_content_hash
    manifest["source_manifest"] = str(text_manifest_path)
    manifest["max_seq_length"] = 1024
    if sparse_encoder is not None:
        sparse_path = Path(args.sparse_vocab)
        manifest["sparse"]["vocab_file"] = str(sparse_path)
        manifest["sparse"]["vocab_sha256"] = _sha256_file(sparse_path)
    problems = verify_vector_manifest(
        client=client,
        manifest=manifest,
        source_content_hash=source_content_hash,
    )
    if problems:
        print("error: 构建后校验失败：" + "；".join(problems), file=sys.stderr)
        return 1
    path = write_vector_manifest(args.vector_manifest, manifest)
    print(
        f"Vector index built: collection={manifest['collection']} "
        f"mode={manifest['embedding_mode']} points={manifest['points']} "
        f"dimension={manifest['dimension']} device={manifest['device']}"
    )
    print(f"index_version: {manifest['index_version']}")
    print(f"vector manifest: {path}")
    for warning in manifest.get("warnings", []):
        print(f"warning: {warning}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
