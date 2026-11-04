"""在 data/shadow 下独立构建影子索引，不触碰生产 collection/alias/manifest/vocab。

前置：用 ``scripts/parse_corpus.py --vision-first --processed-dir data/shadow/processed``
完成影子解析并生成 ``data/shadow/processed/parse_report.json``。
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

from campusguide.config import load_retrieval_config
from campusguide.ingest.build_index import (
    EXPECTED_LOW_CONFIDENCE_VERSIONS,
    BuildError,
    build_index,
)

ROOT = Path(__file__).resolve().parents[1]
PRODUCTION_CONFIG = ROOT / "configs" / "retrieval.yaml"
SHADOW_CONFIG = ROOT / "configs" / "retrieval.shadow.yaml"


def _inside(root: Path, target: Path, label: str) -> Path:
    resolved = target.resolve()
    if resolved != root and root not in resolved.parents:
        raise SystemExit(f"{label} 必须位于影子目录 {root} 内，拒绝写入：{resolved}")
    return resolved


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sources", default=str(ROOT / "configs" / "sources.yaml"))
    parser.add_argument("--retrieval-config", default=str(SHADOW_CONFIG))
    parser.add_argument("--shadow-root", default=str(ROOT / "data" / "shadow"))
    parser.add_argument(
        "--parse-report",
        default=None,
        help="默认 <shadow-root>/processed/parse_report.json",
    )
    parser.add_argument(
        "--text-only",
        action="store_true",
        help="只构建 articles/manifest，不加载 embedding 模型与 Qdrant",
    )
    parser.add_argument("--qdrant-url", default=None, help="影子向量库地址；仍使用影子 collection")
    parser.add_argument("--batch-size", type=int, default=16)
    args = parser.parse_args()

    shadow_root = Path(args.shadow_root).resolve()
    processed_dir = _inside(shadow_root, shadow_root / "processed", "processed-dir")
    parsed_dir = _inside(shadow_root, processed_dir / "parsed", "parsed-dir")
    text_manifest = _inside(shadow_root, shadow_root / "manifest.json", "manifest")
    parse_report = _inside(
        shadow_root,
        Path(args.parse_report) if args.parse_report else processed_dir / "parse_report.json",
        "parse-report",
    )
    if not parse_report.exists():
        raise SystemExit(
            f"缺少影子解析报告：{parse_report}\n"
            "请先运行：python scripts/parse_corpus.py --vision-first "
            f"--processed-dir {processed_dir}"
        )

    retrieval = load_retrieval_config(Path(args.retrieval_config))
    production = load_retrieval_config(PRODUCTION_CONFIG)
    if retrieval.collection == production.collection or retrieval.alias == production.alias:
        raise SystemExit(
            "影子 retrieval 配置的 collection/alias 与生产相同，"
            "请修改 configs/retrieval.shadow.yaml 后再构建"
        )

    try:
        result = build_index(
            sources_path=Path(args.sources).resolve(),
            parsed_dir=parsed_dir,
            output_dir=processed_dir,
            retrieval=retrieval,
            manifest_path=text_manifest,
            expected_low_confidence_versions=EXPECTED_LOW_CONFIDENCE_VERSIONS,
            require_vision=True,
            parse_report_path=parse_report,
            strict=True,
        )
    except BuildError as exc:
        print(f"error: 影子构建未通过门禁：{exc}", file=sys.stderr)
        return 1

    counts = result.manifest["counts"]
    if counts["vision_first_sources"] != counts["versions"]:
        print(
            "error: 影子构建存在非 vision_first 来源："
            f"{counts['vision_first_sources']}/{counts['versions']}",
            file=sys.stderr,
        )
        return 1
    print(
        "Shadow text index built: "
        f"collection={retrieval.collection}, versions={counts['versions']}, "
        f"indexable={counts['indexable_articles']}, stored={counts['stored_articles']}, "
        f"fatal={counts['fatal_warnings']}, review={counts['review_warnings']}"
    )
    print(f"manifest: {result.manifest_path}")

    if args.text_only:
        print("已跳过向量构建（--text-only）。")
        return 0

    vector_manifest = _inside(shadow_root, shadow_root / "vector_manifest.json", "vector-manifest")
    sparse_vocab = _inside(shadow_root, shadow_root / "sparse_vocab.json", "sparse-vocab")
    command = [
        sys.executable,
        str(ROOT / "scripts" / "build_vectors.py"),
        "--articles",
        str(processed_dir / "articles.jsonl"),
        "--retrieval-config",
        str(Path(args.retrieval_config).resolve()),
        "--text-manifest",
        str(text_manifest),
        "--vector-manifest",
        str(vector_manifest),
        "--sparse-vocab",
        str(sparse_vocab),
        "--batch-size",
        str(args.batch_size),
        "--recreate",
    ]
    if args.qdrant_url:
        command.extend(["--qdrant-url", args.qdrant_url])
    else:
        command.extend(
            ["--qdrant-location", str(_inside(shadow_root, shadow_root / "qdrant", "qdrant-dir"))]
        )
    print("运行影子向量构建：" + " ".join(command))
    completed = subprocess.run(command, cwd=ROOT)
    if completed.returncode != 0:
        print("error: 影子向量构建失败", file=sys.stderr)
        return completed.returncode
    print(f"vector manifest: {vector_manifest}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
