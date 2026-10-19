"""解析 data/raw 下的 HTML/PDF 快照，输出结构化文本。"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from campusguide.ingest.parse import parse_corpus

ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--raw-dir", default=str(ROOT / "data" / "raw"))
    parser.add_argument("--processed-dir", default=str(ROOT / "data" / "processed"))
    parser.add_argument(
        "--only",
        action="append",
        default=None,
        help="只处理指定 doc_id 或 doc_id@version_id，可重复",
    )
    args = parser.parse_args()

    raw_dir = Path(args.raw_dir).resolve()
    if not raw_dir.exists():
        print(f"raw 目录不存在：{raw_dir}")
        return 1

    results = parse_corpus(
        raw_dir=raw_dir,
        processed_dir=Path(args.processed_dir).resolve(),
        only=args.only,
    )
    if not results:
        print("没有找到可解析的 meta.json，请先执行 make fetch。")
        return 0

    counts: dict[str, int] = {}
    for result in results:
        counts[result.status] = counts.get(result.status, 0) + 1
        detail = result.error or f"cjk={result.cjk_count}"
        print(f"[{result.status:7}] {result.doc_id}@{result.version_id} {detail}")
        for warning in result.warnings:
            print(f"          warning: {warning}")

    print(
        "Summary: "
        + ", ".join(f"{key}={value}" for key, value in sorted(counts.items()))
        + f", total={len(results)}"
    )
    return 1 if counts.get("error") else 0


if __name__ == "__main__":
    sys.exit(main())
