"""W6-B 图级 dev_v0 runner、support 模板与报告。

默认使用进程内 HybridRetriever（与 MCP policy-search 同一 SearchClient 契约）
和本地 flow registry；`--mcp` 可切到生产 stdio MCP 路径。不读取/修改 W7。

用法：
  python scripts/eval_w6b.py                 # 跑 20 题并生成 run + labels + report
  python scripts/eval_w6b.py --run-only
  python scripts/eval_w6b.py --report-only
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import time
from datetime import UTC, date, datetime
from pathlib import Path

from qdrant_client import QdrantClient

from campusguide.config import load_abstention_config
from campusguide.env import load_dotenv
from campusguide.eval.w3 import (
    build_label_template,
    build_run_snapshot,
    finalize_run,
    git_info,
    hash_configs,
    load_labels,
    load_run,
    save_run,
    sha256_file,
    vector_manifest_info,
    verify_run_hash,
    write_labels,
)
from campusguide.eval.w6b import (
    build_manifest,
    build_w6_report,
    render_w6_markdown,
    row_from_graph,
)
from campusguide.flows.calendar import load_calendar
from campusguide.flows.catalog import LocalFlowCatalog
from campusguide.flows.registry import FlowRegistry
from campusguide.qa.runtime import (
    GraphRuntime,
    RuntimePaths,
    create_graph_runtime,
    create_local_hybrid_search_client,
    create_qdrant_client,
)

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DEV = ROOT / "data" / "eval" / "dev_v0.jsonl"
DEFAULT_RUN = ROOT / "data" / "eval" / "w6b-run.json"
DEFAULT_LABELS = ROOT / "data" / "eval" / "w6b_support_labels.jsonl"
DEFAULT_JSON = ROOT / "data" / "eval" / "w6b-dev-report.json"
DEFAULT_MD = ROOT / "data" / "eval" / "w6b-dev-report.md"
CONFIG_FILES = [
    ROOT / "configs" / "graph.yaml",
    ROOT / "configs" / "handoff.yaml",
    ROOT / "configs" / "retrieval.yaml",
    ROOT / "configs" / "abstention.yaml",
    ROOT / "configs" / "aliases.yaml",
]


class _NullMeta:
    def close(self) -> None:
        return None


def load_dev(path: Path) -> list[dict]:
    rows = []
    with path.open("r", encoding="utf-8") as handle:
        for line_no, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError as exc:
                raise ValueError(f"{path}:{line_no} 不是合法 JSON") from exc
    return rows


def _configure_runtime(
    *,
    mcp: bool,
    qdrant_location: str | None,
    as_of: date,
    no_warmup: bool,
) -> tuple[GraphRuntime, int, str, str]:
    paths = RuntimePaths()
    if mcp:
        runtime = create_graph_runtime(paths=paths, today=as_of)
        return runtime, 0, "mcp-hybrid", "mcp:policy-meta"
    if qdrant_location:
        Path(qdrant_location).mkdir(parents=True, exist_ok=True)
        client = QdrantClient(path=qdrant_location)
    else:
        client = create_qdrant_client()
    search_client = create_local_hybrid_search_client(client=client, paths=paths)
    registry = FlowRegistry.from_directory(ROOT / "configs" / "flows")
    calendar = {
        event.event_id: event for event in load_calendar(ROOT / "configs" / "calendar.yaml")
    }
    catalog = LocalFlowCatalog(registry, calendar=calendar)
    runtime = create_graph_runtime(
        paths=paths,
        search_client=search_client,
        meta_client=_NullMeta(),
        flow_catalog=catalog,
        start=False,
        today=as_of,
    )
    warmup_ms = 0
    if not no_warmup:
        started = time.perf_counter()
        search_client.warmup()
        warmup_ms = int((time.perf_counter() - started) * 1000)
        print(f"warmup: {warmup_ms}ms", flush=True)
    return runtime, warmup_ms, "local-hybrid", "local:flow-registry"


def _manifest(
    runtime: GraphRuntime,
    *,
    started_at: str,
    retrieval_mode: str,
    flow_source: str,
) -> dict:
    vector = vector_manifest_info(ROOT / "data" / "vector_manifest.json")
    return build_manifest(
        graph_version="w6-2026-10-25",
        git=git_info(ROOT),
        model_profile=runtime.model_profile,
        models=runtime.models,
        prompt_versions=runtime.prompt_versions,
        index_version=runtime.index_version,
        vector_manifest_sha256=vector["sha256"],
        test_set_sha256=sha256_file(DEFAULT_DEV),
        config_sha256=hash_configs(CONFIG_FILES),
        retrieval_mode=retrieval_mode,
        flow_source=flow_source,
        started_at=started_at,
    )


def _prepare_labels(run: dict, path: Path, *, force: bool, prefill_from: Path | None) -> str:
    baseline = None
    if prefill_from and prefill_from.exists():
        baseline = load_labels(prefill_from)
        print(
            f"prefill: 从 {prefill_from.name} 读取 {len(baseline)} 条旧标签；"
            "只对 text+quote 完全一致的 claim 写建议（不自动确认）",
            file=sys.stderr,
        )
    template = build_label_template(run, baseline_labels=baseline)
    prefill_count = sum(1 for item in template if item.get("prefill_label"))
    if prefill_count:
        print(f"prefill: {prefill_count}/{len(template)} 条有建议（未确认）", file=sys.stderr)
    run_sha = run["run_sha256"]
    if path.exists():
        existing = load_labels(path)
        if existing and {item.get("run_sha256") for item in existing} == {run_sha}:
            return f"沿用已有 support labels：{path}"
        if existing and not force:
            new_path = path.with_suffix(".new.jsonl")
            write_labels(template, new_path)
            return f"已有 labels 属于其他 run，新模板写入 {new_path}"
        if existing and force:
            backup = path.with_suffix(f".bak-{datetime.now(UTC).strftime('%Y%m%d%H%M%S')}.jsonl")
            shutil.copy2(path, backup)
            print(f"backup: {backup}", file=sys.stderr)
    write_labels(template, path)
    return f"已生成 support label 模板：{path}（{len(template)} 条 claim）"


def _write_report(run: dict, labels_path: Path, json_path: Path, md_path: Path) -> dict:
    gates = load_abstention_config(ROOT / "configs" / "abstention.yaml")
    labels = None
    warning = ""
    if labels_path.exists():
        loaded = load_labels(labels_path)
        labels = loaded or []
    else:
        warning = "缺少 support labels；support 按未标注记录，不影响 W6 硬门禁。"
    report = build_w6_report(
        run,
        labels,
        support_gate=gates.support_gate,
        refusal_recall_gate=gates.refusal_recall_gate,
        false_refusal_gate=gates.false_refusal_gate,
        manifest=run.get("metadata", {}).get("manifest"),
    )
    report["labels_warning"] = warning
    json_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    md_path.write_text(render_w6_markdown(report), encoding="utf-8")
    return report


def _print_summary(report: dict, json_path: Path, md_path: Path) -> None:
    gates = report["gates"]
    print(
        "summary: "
        f"structure={'PASS' if gates['structure']['ok'] else 'FAIL'} "
        f"refusal={report['abstention']['refusal_recall']} "
        f"false_refusal={report['abstention']['false_refusal_rate']} "
        f"semantic={'PASS' if gates['semantic']['ok'] else 'FAIL'} "
        f"quote_mismatch={report['quote_mismatch_rendered']} "
        f"w6_overall={'PASS' if gates['w6_overall'] else 'FAIL'}"
    )
    for reason in gates["blocking_reasons"]:
        print(f"blocking: {reason}")
    print(f"report: {json_path}")
    print(f"report: {md_path}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dev", default=str(DEFAULT_DEV))
    parser.add_argument("--run", default=str(DEFAULT_RUN))
    parser.add_argument("--labels", default=str(DEFAULT_LABELS))
    parser.add_argument("--output-json", default=str(DEFAULT_JSON))
    parser.add_argument("--output-md", default=str(DEFAULT_MD))
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--as-of", default="2026-10-25")
    parser.add_argument("--qdrant-location", default=None)
    parser.add_argument("--mcp", action="store_true", help="走生产 stdio MCP 路径")
    parser.add_argument("--no-warmup", action="store_true")
    parser.add_argument("--run-only", action="store_true")
    parser.add_argument("--report-only", action="store_true")
    parser.add_argument("--prefill-from", default=None)
    parser.add_argument("--force-labels", action="store_true")
    args = parser.parse_args()

    run_path = Path(args.run)
    labels_path = Path(args.labels)
    json_path = Path(args.output_json)
    md_path = Path(args.output_md)

    if args.report_only:
        if not run_path.exists():
            print(f"error: run 不存在：{run_path}", file=sys.stderr)
            return 2
        run = load_run(run_path)
        verify_run_hash(run)
        report = _write_report(run, labels_path, json_path, md_path)
        _print_summary(report, json_path, md_path)
        return 0 if report["gates"]["w6_overall"] else 1

    load_dotenv(ROOT / ".env")
    as_of = date.fromisoformat(args.as_of)
    items = load_dev(Path(args.dev))
    if args.limit is not None:
        items = items[: args.limit]
    started_at = datetime.now(UTC).isoformat(timespec="seconds")
    runtime, warmup_ms, retrieval_mode, flow_source = _configure_runtime(
        mcp=args.mcp,
        qdrant_location=args.qdrant_location,
        as_of=as_of,
        no_warmup=args.no_warmup,
    )
    manifest = _manifest(
        runtime,
        started_at=started_at,
        retrieval_mode=retrieval_mode,
        flow_source=flow_source,
    )
    rows = []
    try:
        for index, item in enumerate(items, start=1):
            from campusguide.domain.enums import BlockType

            block_types = [BlockType(value) for value in item.get("block_types") or ["article"]]
            state = runtime.runner.invoke(
                f"w6b-{item['id']}",
                item["question"],
                as_of=as_of,
                block_types=block_types,
            )
            row = row_from_graph(item, state)
            rows.append(row)
            labels = (row.get("graph") or {}).get("semantic") or {}
            label_text = ",".join((labels.get("labels") or {}).values()) or "-"
            print(
                f"[{index}/{len(items)}] {item['id']} {row['outcome']} "
                f"decision={row.get('decision')} semantic={label_text} "
                f"{row['duration_ms']}ms",
                flush=True,
            )
    finally:
        runtime.close()

    metadata = {
        **git_info(ROOT),
        "manifest": manifest,
        "warmup_ms": warmup_ms,
        "latency_scope": "cold" if args.no_warmup else "warm",
        "retrieval_mode": retrieval_mode,
        "flow_source": flow_source,
        "config_hash": hash_configs(CONFIG_FILES),
        "vector_manifest": vector_manifest_info(ROOT / "data" / "vector_manifest.json"),
    }
    run = build_run_snapshot(
        dataset=Path(args.dev).name,
        dataset_path=Path(args.dev),
        rows=rows,
        metadata=metadata,
    )
    run["schema_version"] = "w6b-run-v1"
    run = finalize_run(run)
    save_run(run, run_path)
    print(f"run: {run_path} sha256={run['run_sha256']}")
    message = _prepare_labels(
        run,
        labels_path,
        force=args.force_labels,
        prefill_from=Path(args.prefill_from) if args.prefill_from else None,
    )
    print(message)
    if args.run_only:
        return 0
    report = _write_report(run, labels_path, json_path, md_path)
    _print_summary(report, json_path, md_path)
    return 0 if report["gates"]["w6_overall"] else 1


if __name__ == "__main__":
    sys.exit(main())
