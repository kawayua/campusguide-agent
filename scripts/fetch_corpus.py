"""按 configs/sources.yaml 抓取或复制原始快照。"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from campusguide.config import load_sources
from campusguide.ingest.fetch import fetch_corpus

ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default=str(ROOT / "configs" / "sources.yaml"))
    parser.add_argument("--raw-dir", default=str(ROOT / "data" / "raw"))
    parser.add_argument(
        "--only",
        action="append",
        default=None,
        help="只处理指定 doc_id 或 doc_id@version_id，可重复",
    )
    parser.add_argument("--skip-existing", action="store_true")
    parser.add_argument("--timeout", type=float, default=30.0)
    parser.add_argument("--delay", type=float, default=1.0, help="每次网抓后的等待秒数")
    args = parser.parse_args()

    config_path = Path(args.config).resolve()
    config = load_sources(config_path)
    if not config.documents:
        print("sources.yaml 中还没有 documents，请先按 docs/data-sources.md 填写。")
        return 0

    results = fetch_corpus(
        config,
        raw_dir=Path(args.raw_dir).resolve(),
        config_dir=config_path.parent,
        local_base_dir=ROOT,
        only=args.only,
        skip_existing=args.skip_existing,
        timeout=args.timeout,
        delay_seconds=args.delay,
    )

    counts: dict[str, int] = {}
    for result in results:
        counts[result.status] = counts.get(result.status, 0) + 1
        detail = result.error or result.format or ""
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
