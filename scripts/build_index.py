"""生成条款索引、版本清单与 manifest；可选初始化 Qdrant 集合骨架。"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from campusguide.config import load_retrieval_config
from campusguide.ingest.build_index import BuildError, build_index

ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default=str(ROOT / "configs" / "sources.yaml"))
    parser.add_argument(
        "--retrieval-config",
        default=str(ROOT / "configs" / "retrieval.yaml"),
    )
    parser.add_argument("--parsed-dir", default=str(ROOT / "data" / "processed" / "parsed"))
    parser.add_argument("--output-dir", default=str(ROOT / "data" / "processed"))
    parser.add_argument("--manifest", default=str(ROOT / "data" / "manifest.json"))
    parser.add_argument("--init-qdrant", action="store_true", help="创建集合与 payload 索引")
    parser.add_argument("--recreate", action="store_true", help="重建已存在的 Qdrant 集合")
    parser.add_argument("--qdrant-url", default=None)
    args = parser.parse_args()

    retrieval = load_retrieval_config(Path(args.retrieval_config))
    try:
        result = build_index(
            sources_path=Path(args.config).resolve(),
            parsed_dir=Path(args.parsed_dir).resolve(),
            output_dir=Path(args.output_dir).resolve(),
            retrieval=retrieval,
            init_qdrant=args.init_qdrant,
            qdrant_url=args.qdrant_url,
            manifest_path=Path(args.manifest).resolve(),
            recreate_collection=args.recreate,
        )
    except BuildError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    counts = result.manifest["counts"]
    print(
        "Index built: "
        f"documents={counts['documents']}, versions={counts['versions']}, "
        f"articles={counts['articles']}, blocks_excluded={counts['blocks_excluded']}, "
        f"missing_versions={counts['missing_versions']}"
    )
    print(f"content_hash: {result.manifest['content_hash']}")
    print(f"output: {result.output_dir}")
    if result.missing_versions:
        print("missing parsed versions: " + ", ".join(result.missing_versions))
    for warning in result.warnings:
        print(f"warning: {warning}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
