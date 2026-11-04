"""生成条款索引、版本清单与 manifest；可选初始化 Qdrant 集合骨架。"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from campusguide.config import load_retrieval_config
from campusguide.ingest.build_index import (
    EXPECTED_LOW_CONFIDENCE_VERSIONS,
    BuildError,
    build_index,
)

ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default=str(ROOT / "configs" / "sources.yaml"))
    parser.add_argument(
        "--organization-config",
        default=str(ROOT / "configs" / "organization.yaml"),
        help="组织注册表；默认用于来源元数据审计",
    )
    parser.add_argument(
        "--skip-metadata-source-audit",
        action="store_true",
        help="显式跳过来源元数据审计，仅用于旧版诊断构建",
    )
    parser.add_argument(
        "--retrieval-config",
        default=str(ROOT / "configs" / "retrieval.yaml"),
    )
    parser.add_argument("--parsed-dir", default=str(ROOT / "data" / "processed" / "parsed"))
    parser.add_argument(
        "--raw-dir",
        default=str(ROOT / "data" / "raw"),
        help="原始快照目录；来源审计按 raw/<doc>/<version>/meta.json 核对实际字节",
    )
    parser.add_argument(
        "--output-dir",
        default=None,
        help="索引输出目录；显式指定时 manifest 默认也落在该目录，便于影子构建",
    )
    parser.add_argument(
        "--manifest",
        default=None,
        help="manifest 路径；默认生产目录或（显式 --output-dir 时）输出目录内",
    )
    parser.add_argument(
        "--parse-report",
        default=None,
        help="本次 parse 运行报告；正式门禁用它拒绝混入旧 parsed 文件",
    )
    parser.add_argument(
        "--require-vision",
        action="store_true",
        help="正式构建门禁：全部来源必须 vision_first 且页覆盖完整",
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help="任一致命质量告警即失败；--require-vision 隐含开启",
    )
    parser.add_argument("--init-qdrant", action="store_true", help="创建集合与 payload 索引")
    parser.add_argument("--recreate", action="store_true", help="重建已存在的 Qdrant 集合")
    parser.add_argument("--qdrant-url", default=None)
    args = parser.parse_args()
    if args.require_vision and args.skip_metadata_source_audit:
        parser.error("--require-vision 不能与 --skip-metadata-source-audit 同时使用")

    output_dir = (
        Path(args.output_dir).resolve()
        if args.output_dir
        else (ROOT / "data" / "processed")
    )
    manifest_path = (
        Path(args.manifest).resolve()
        if args.manifest
        else (
            output_dir / "manifest.json"
            if args.output_dir
            else ROOT / "data" / "manifest.json"
        )
    )
    parsed_dir = Path(args.parsed_dir).resolve()
    parse_report_path = (
        Path(args.parse_report).resolve()
        if args.parse_report
        else (
            parsed_dir.parent / "parse_report.json"
            if args.require_vision
            else None
        )
    )

    retrieval = load_retrieval_config(Path(args.retrieval_config))
    try:
        result = build_index(
            sources_path=Path(args.config).resolve(),
            metadata_organization_path=(
                None
                if args.skip_metadata_source_audit
                else Path(args.organization_config).resolve()
            ),
            metadata_raw_dir=Path(args.raw_dir).resolve(),
            parsed_dir=parsed_dir,
            output_dir=output_dir,
            retrieval=retrieval,
            init_qdrant=args.init_qdrant,
            qdrant_url=args.qdrant_url,
            manifest_path=manifest_path,
            recreate_collection=args.recreate,
            expected_low_confidence_versions=EXPECTED_LOW_CONFIDENCE_VERSIONS,
            require_vision=args.require_vision,
            parse_report_path=parse_report_path,
            strict=args.strict or args.require_vision,
        )
    except BuildError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    counts = result.manifest["counts"]
    print(
        "Index built: "
        f"documents={counts['documents']}, versions={counts['versions']}, "
        f"indexable={counts['indexable_articles']}, stored={counts['stored_articles']}, "
        f"blocks_excluded={counts['blocks_excluded']}, "
        f"missing_versions={counts['missing_versions']}, "
        f"date_confidence_low={counts['date_confidence_low']}"
    )
    print(
        "Ingestion modes: "
        + ", ".join(
            f"{mode}={count}"
            for mode, count in result.manifest.get("ingestion_modes", {}).items()
        )
    )
    gates = result.manifest.get("quality_gates", {})
    print(
        "Quality gates: "
        f"fatal={len(gates.get('fatal', []))}, "
        f"review={len(gates.get('review', []))}, "
        f"strict={gates.get('strict')}, "
        f"require_vision={gates.get('require_vision')}"
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
