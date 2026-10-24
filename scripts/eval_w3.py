"""W3 v2 20 题开发集 runner、support 标注与报告。

流程：
1. 在最终代码上跑一次 dev_v0，写 `data/eval/w3_run.json`；
2. 生成 `data/eval/w3_support_labels.jsonl` 模板（不覆盖人工标注）；
3. 人工填 supported/partial/unsupported 后，用同一 run + labels 生成报告。

live 运行不在 CI 中；本脚本不调用外部服务做 support 标注。
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from qdrant_client import QdrantClient

from campusguide.agents.advisor import PROMPT_VERSION
from campusguide.config import load_abstention_config, load_retrieval_config
from campusguide.domain.enums import BlockType
from campusguide.env import load_dotenv
from campusguide.eval.w3 import (
    build_label_template,
    build_report,
    build_run_snapshot,
    git_info,
    hash_configs,
    load_labels,
    load_run,
    merge_labels,
    render_markdown,
    row_from_result,
    save_run,
    vector_manifest_info,
    verify_run_hash,
    write_labels,
)
from campusguide.qa.runtime import RuntimePaths, create_qa_service, create_qdrant_client

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DEV = ROOT / "data" / "eval" / "dev_v0.jsonl"
DEFAULT_RUN = ROOT / "data" / "eval" / "w3_run.json"
DEFAULT_LABELS = ROOT / "data" / "eval" / "w3_support_labels.jsonl"
DEFAULT_JSON = ROOT / "data" / "eval" / "w3-dev-report.json"
DEFAULT_MD = ROOT / "data" / "eval" / "w3-dev-report.md"
CONFIG_FILES = [
    ROOT / "configs" / "retrieval.yaml",
    ROOT / "configs" / "abstention.yaml",
    ROOT / "configs" / "aliases.yaml",
]


def load_dev(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as handle:
        for line_no, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError as exc:
                raise ValueError(f"{path}:{line_no} 不是合法 JSON") from exc
    return rows


def _metadata(
    *,
    rows: list[dict[str, Any]],
    warmup_ms: int,
    latency_scope: str,
    vector_manifest_path: Path = ROOT / "data" / "vector_manifest.json",
) -> dict[str, Any]:
    retrieval = load_retrieval_config(ROOT / "configs" / "retrieval.yaml")
    manifest = vector_manifest_info(vector_manifest_path)
    try:
        manifest["path"] = vector_manifest_path.relative_to(ROOT).as_posix()
    except ValueError:
        manifest["path"] = str(vector_manifest_path)
    models = sorted({row.get("llm_model") for row in rows if row.get("llm_model")})
    return {
        **git_info(ROOT),
        "model": ", ".join(models) or "n/a",
        "advisor_prompt_version": PROMPT_VERSION,
        "advisor_max_tokens": int(os.getenv("ADVISOR_MAX_TOKENS", "1200")),
        "embedding_model": retrieval.embedding_model,
        "vector_manifest": manifest,
        "config_hash": hash_configs(CONFIG_FILES),
        "config_files": [path.relative_to(ROOT).as_posix() for path in CONFIG_FILES],
        "warmup_ms": warmup_ms,
        "latency_scope": latency_scope,
    }


def _run_live(
    items: list[dict[str, Any]],
    *,
    no_warmup: bool,
    qdrant_location: str | None = None,
    vector_manifest: Path = ROOT / "data" / "vector_manifest.json",
    text_manifest: Path = ROOT / "data" / "manifest.json",
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    load_dotenv(ROOT / ".env")
    if qdrant_location:
        Path(qdrant_location).mkdir(parents=True, exist_ok=True)
        client = QdrantClient(path=qdrant_location)
    else:
        client = create_qdrant_client()
    paths = RuntimePaths(
        vector_manifest=vector_manifest,
        text_manifest=text_manifest,
    )
    service = create_qa_service(client=client, paths=paths)
    warmup_ms = 0
    if not no_warmup:
        started = time.perf_counter()
        service.warmup()
        warmup_ms = int((time.perf_counter() - started) * 1000)
        print(f"warmup: {warmup_ms}ms (latency_scope=warm)", flush=True)

    rows: list[dict[str, Any]] = []
    for index, item in enumerate(items, start=1):
        block_types = [BlockType(value) for value in item.get("block_types") or ["article"]]
        result = service.ask(item["question"], block_types=block_types)
        row = row_from_result(item, result)
        rows.append(row)
        print(
            f"[{index}/{len(items)}] {item['id']} {result.outcome.value} "
            f"{result.decision.value if result.decision else 'n/a'} "
            f"qa={result.qa_ms}ms probe={result.probe_ms}ms",
            flush=True,
        )
    metadata = _metadata(
        rows=rows,
        warmup_ms=warmup_ms,
        latency_scope="cold" if no_warmup else "warm",
        vector_manifest_path=vector_manifest,
    )
    return rows, metadata


def _write_report(
    *,
    run: dict[str, Any],
    labels_path: Path,
    output_json: Path,
    output_md: Path,
) -> dict[str, Any]:
    gates = load_abstention_config(ROOT / "configs" / "abstention.yaml")
    labels: list[dict[str, Any]] | None = None
    warning = ""
    if labels_path.exists():
        loaded = load_labels(labels_path)
        if loaded:
            try:
                merge_labels(run, loaded)
            except ValueError as exc:
                warning = (
                    f"support labels 与当前 run 不匹配（{exc}）；"
                    "报告按未标注处理，不声称 support 门禁通过。"
                )
                print(f"warning: {warning}", file=sys.stderr)
            else:
                labels = loaded
        else:
            labels = []
    else:
        warning = "缺少 support labels 文件；报告按未标注处理。"
        print(f"warning: {warning}", file=sys.stderr)

    report = build_report(
        run,
        labels,
        support_gate=gates.support_gate,
        refusal_recall_gate=gates.refusal_recall_gate,
        false_refusal_gate=gates.false_refusal_gate,
    )
    report["labels_warning"] = warning
    output_json.parent.mkdir(parents=True, exist_ok=True)
    output_json.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    output_md.write_text(render_markdown(report), encoding="utf-8")
    return report


def _print_summary(report: dict[str, Any], *, json_path: Path, md_path: Path) -> None:
    support = report["support"]
    abstention = report["abstention"]
    structure = report["structure"]
    print(
        "summary: "
        f"structure={'PASS' if structure['ok'] else 'FAIL'} "
        f"cite_valid={structure['citation_id_valid_rate']:.1%} "
        f"quotes={structure['quote_locatable_rate']:.1%} "
        f"rendered_support={support['rendered_support_rate']} "
        f"unlabeled={support['drafts_unlabeled']} "
        f"refusal_recall={abstention['refusal_recall']} "
        f"false_refusal={abstention['false_refusal_rate']} "
        f"overall={'PASS' if report['gates']['w3_overall'] else 'FAIL'}"
    )
    for reason in report["gates"]["blocking_reasons"]:
        print(f"blocking: {reason}")
    print(f"report: {json_path}")
    print(f"report: {md_path}")


def _prepare_labels(
    run: dict[str, Any],
    labels_path: Path,
    *,
    force: bool,
    prefill_from: Path | None = None,
) -> tuple[list[dict[str, Any]], str]:
    baseline_labels: list[dict[str, Any]] | None = None
    if prefill_from is not None:
        if prefill_from.exists():
            baseline_labels = load_labels(prefill_from)
            print(
                f"prefill: 从 {prefill_from} 读取 {len(baseline_labels)} 条旧标签，"
                "仅对 text+located_quotes 完全一致的 claim 写入待确认建议",
                file=sys.stderr,
            )
        else:
            print(
                f"warning: prefill 文件不存在：{prefill_from}；不写入 prefill 建议",
                file=sys.stderr,
            )
    template = build_label_template(run, baseline_labels=baseline_labels)
    prefill_count = sum(1 for item in template if item.get("prefill_label"))
    if prefill_count:
        print(f"prefill: {prefill_count}/{len(template)} 条有建议（未确认）", file=sys.stderr)
    run_sha256 = run["run_sha256"]
    if labels_path.exists():
        existing = load_labels(labels_path)
        hashes = {item.get("run_sha256") for item in existing}
        if existing and hashes == {run_sha256}:
            return existing, f"沿用已有 support labels：{labels_path}"
        if existing and not force:
            new_path = labels_path.with_suffix(".new.jsonl")
            write_labels(template, new_path)
            return (
                existing,
                f"已有 support labels 属于其他 run，未覆盖；新模板写入 {new_path}",
            )
        if existing and force:
            backup = labels_path.with_suffix(
                f".bak-{datetime.now(UTC).strftime('%Y%m%d%H%M%S')}.jsonl"
            )
            shutil.copy2(labels_path, backup)
            print(f"backup: {backup}", file=sys.stderr)
    write_labels(template, labels_path)
    return template, f"已生成 support label 模板：{labels_path}（{len(template)} 条 draft claim）"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dev", default=str(DEFAULT_DEV))
    parser.add_argument("--run", default=str(DEFAULT_RUN))
    parser.add_argument("--labels", default=str(DEFAULT_LABELS))
    parser.add_argument(
        "--prefill-from",
        default=None,
        help="从旧 run 的人工 labels 生成 prefill 建议（不自动确认）",
    )
    parser.add_argument("--output-json", default=str(DEFAULT_JSON))
    parser.add_argument("--output-md", default=str(DEFAULT_MD))
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument(
        "--qdrant-location",
        default=None,
        help="使用本地 Qdrant 持久化目录（不传则用 QDRANT_URL）",
    )
    parser.add_argument(
        "--vector-manifest",
        default=str(ROOT / "data" / "vector_manifest.json"),
    )
    parser.add_argument(
        "--text-manifest",
        default=str(ROOT / "data" / "manifest.json"),
    )
    parser.add_argument(
        "--no-warmup",
        action="store_true",
        help="不预加载 embedding；记录 latency_scope=cold",
    )
    parser.add_argument(
        "--run-only",
        action="store_true",
        help="只跑 live 并写 run/labels 模板，不生成报告",
    )
    parser.add_argument(
        "--report-only",
        action="store_true",
        help="复用已有 run + labels 生成报告，不调用检索/LLM",
    )
    parser.add_argument(
        "--force-labels",
        action="store_true",
        help="run 变化时备份并覆盖旧 labels（谨慎使用）",
    )
    args = parser.parse_args()

    run_path = Path(args.run)
    labels_path = Path(args.labels)
    output_json = Path(args.output_json)
    output_md = Path(args.output_md)

    if args.report_only:
        if not run_path.exists():
            print(f"error: run 文件不存在：{run_path}", file=sys.stderr)
            return 2
        run = load_run(run_path)
        try:
            verify_run_hash(run)
        except ValueError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        report = _write_report(
            run=run,
            labels_path=labels_path,
            output_json=output_json,
            output_md=output_md,
        )
        _print_summary(report, json_path=output_json, md_path=output_md)
        return 0 if report["gates"]["w3_overall"] else 1

    dev_path = Path(args.dev)
    items = load_dev(dev_path)
    if args.limit is not None:
        items = items[: args.limit]

    try:
        rows, metadata = _run_live(
            items,
            no_warmup=args.no_warmup,
            qdrant_location=args.qdrant_location,
            vector_manifest=Path(args.vector_manifest),
            text_manifest=Path(args.text_manifest),
        )
    except Exception as exc:
        print(
            f"error: W3 开发集未运行（{type(exc).__name__}）：{exc}\n"
            "请检查 .env、Qdrant、vector_manifest 与 configs/abstention.yaml。",
            file=sys.stderr,
        )
        return 2

    run = build_run_snapshot(
        dataset=dev_path.name,
        dataset_path=dev_path,
        rows=rows,
        metadata=metadata,
    )
    save_run(run, run_path)
    print(f"run: {run_path} sha256={run['run_sha256']}")

    _, label_message = _prepare_labels(
        run,
        labels_path,
        force=args.force_labels,
        prefill_from=Path(args.prefill_from) if args.prefill_from else None,
    )
    print(label_message)

    if args.run_only:
        return 0

    report = _write_report(
        run=run,
        labels_path=labels_path,
        output_json=output_json,
        output_md=output_md,
    )
    _print_summary(report, json_path=output_json, md_path=output_md)
    return 0 if report["gates"]["w3_overall"] else 1


if __name__ == "__main__":
    sys.exit(main())
