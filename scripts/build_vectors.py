"""构建 W2 dense 向量索引：articles.jsonl -> Qdrant + vector_manifest.json。"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from qdrant_client import QdrantClient

from campusguide.config import load_retrieval_config
from campusguide.env import load_dotenv
from campusguide.retrieval.embedder import BgeM3Embedder
from campusguide.retrieval.indexer import (
    VectorIndexError,
    build_vector_index,
    load_articles,
    verify_vector_manifest,
    write_vector_manifest,
)

ROOT = Path(__file__).resolve().parents[1]


def _progress(written: int, total: int) -> None:
    print(f"  embedded {written}/{total}", flush=True)


def main() -> int:
    load_dotenv(ROOT / ".env")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--articles", default=str(ROOT / "data" / "processed" / "articles.jsonl"))
    parser.add_argument("--retrieval-config", default=str(ROOT / "configs" / "retrieval.yaml"))
    parser.add_argument("--text-manifest", default=str(ROOT / "data" / "manifest.json"))
    parser.add_argument("--vector-manifest", default=str(ROOT / "data" / "vector_manifest.json"))
    parser.add_argument("--qdrant-url", default=None)
    parser.add_argument("--qdrant-location", default=None)
    parser.add_argument("--recreate", action="store_true")
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

    if args.qdrant_location:
        Path(args.qdrant_location).mkdir(parents=True, exist_ok=True)
        client = QdrantClient(location=args.qdrant_location)
    else:
        client = QdrantClient(
            url=args.qdrant_url or "http://localhost:6333",
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
    manifest["embedding_mode"] = "dense-only"
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
        f"points={manifest['points']} dimension={manifest['dimension']} "
        f"device={manifest['device']}"
    )
    print(f"vector manifest: {path}")
    for warning in manifest.get("warnings", []):
        print(f"warning: {warning}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
