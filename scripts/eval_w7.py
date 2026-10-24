"""W7 冻结测试集 runner：通过生产 MCP QAService 跑 50 题并生成报告。"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import date
from pathlib import Path
from typing import Any

from campusguide.agents.advisor import PROMPT_VERSION
from campusguide.config import load_retrieval_config
from campusguide.domain.enums import Audience, BlockType
from campusguide.domain.profiles import UserProfile
from campusguide.eval.w3 import (
    build_label_template,
    build_run_snapshot,
    finalize_run,
    git_info,
    hash_configs,
    load_run,
    row_from_result,
    save_run,
    vector_manifest_info,
    write_labels,
)
from campusguide.eval.w7 import (
    carry_answer_labels,
    carry_support_labels,
    load_jsonl,
    verify_frozen,
)
from campusguide.eval.w7_report import build_w7_report, render_w7_markdown
from campusguide.qa.runtime import RuntimePaths, create_mcp_qa_service

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_TESTSET = ROOT / "data" / "eval" / "test_v1.jsonl"
DEFAULT_MANIFEST = ROOT / "data" / "eval" / "test_v1.manifest.json"
DEFAULT_RUN = ROOT / "data" / "eval" / "w7_run.json"
DEFAULT_SUPPORT = ROOT / "data" / "eval" / "w7_support_labels.jsonl"
DEFAULT_ANSWERS = ROOT / "data" / "eval" / "w7_answer_labels.jsonl"
DEFAULT_JSON = ROOT / "data" / "eval" / "w7-dev-report.json"
DEFAULT_MD = ROOT / "data" / "eval" / "w7-dev-report.md"


def _load_labels(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    return load_jsonl(path)


def _profile(row: dict[str, Any]) -> UserProfile | None:
    data = row.get("user_profile") or {}
    profile = UserProfile.model_validate(data)
    return None if profile.is_empty else profile


def _audience(row: dict[str, Any]) -> Audience | None:
    value = (row.get("user_profile") or {}).get("audience")
    if not value:
        return None
    return Audience(value)


def _metadata(
    *,
    rows: list[dict[str, Any]],
    warmup_ms: int,
    limit: int | None,
    text_manifest: Path,
) -> dict[str, Any]:
    retrieval = load_retrieval_config(ROOT / "configs" / "retrieval.yaml")
    manifest = vector_manifest_info(ROOT / "data" / "vector_manifest.json")
    models = sorted({row.get("llm_model") for row in rows if row.get("llm_model")})
    config_files = [
        ROOT / "configs" / "retrieval.yaml",
        ROOT / "configs" / "abstention.yaml",
        ROOT / "configs" / "aliases.yaml",
        ROOT / "configs" / "mcp.yaml",
    ]
    return {
        **git_info(ROOT),
        "model": ", ".join(models) or "n/a",
        "advisor_prompt_version": PROMPT_VERSION,
        "embedding_model": retrieval.embedding_model,
        "vector_manifest": manifest,
        "config_hash": hash_configs(config_files),
        "config_files": [path.relative_to(ROOT).as_posix() for path in config_files],
        "warmup_ms": warmup_ms,
        "latency_scope": "warm" if warmup_ms else "cold",
        "limit": limit,
        "text_manifest": str(text_manifest),
    }


def _run(
    rows: list[dict[str, Any]],
    *,
    no_warmup: bool,
    limit: int | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    service = create_mcp_qa_service()
    warmup_ms = 0
    result_rows: list[dict[str, Any]] = []
    try:
        if not no_warmup:
            started = time.perf_counter()
            service.warmup()
            warmup_ms = int((time.perf_counter() - started) * 1000)
            print(f"warmup: {warmup_ms}ms", flush=True)
        for index, item in enumerate(rows, start=1):
            block_types = [BlockType(value) for value in item.get("block_types") or ["article"]]
            row = row_from_result(
                {
                    "id": item["qid"],
                    "question": item["question"],
                    "category": item["category"],
                    "expected_outcome": item["expected_outcome"],
                    "expected_article_ids": item.get("gold_article_ids") or [],
                    "block_types": [block_type.value for block_type in block_types],
                },
                service.ask(
                    item["question"],
                    audience=_audience(item),
                    profile=_profile(item),
                    block_types=block_types,
                    as_of=date.fromisoformat(item["as_of"]),
                ),
            )
            row.update(
                {
                    "qid": item["qid"],
                    "category": item["category"],
                    "should_abstain": bool(item.get("should_abstain")),
                    "version_sensitive": bool(item.get("version_sensitive")),
                    "gold_answer": item.get("gold_answer", ""),
                    "required_facts": item.get("required_facts") or [],
                    "user_profile": item.get("user_profile") or {},
                }
            )
            result_rows.append(row)
            print(
                f"[{index}/{len(rows)}] {item['qid']} {row['outcome']} "
                f"decision={row.get('decision')} qa={row.get('qa_ms')}ms",
                flush=True,
            )
    finally:
        service.close()
    metadata = _metadata(
        rows=result_rows,
        warmup_ms=warmup_ms,
        limit=limit,
        text_manifest=RuntimePaths().text_manifest,
    )
    return result_rows, metadata


def _answer_labels(
    rows: list[dict[str, Any]], existing: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """答案与旧 run 一致时沿用旧标签，changed 题目标未标注待重标。"""
    return carry_answer_labels(rows, existing)


def _write_report(
    *,
    run: dict[str, Any],
    support_path: Path,
    answers_path: Path,
    output_json: Path,
    output_md: Path,
    gates: dict[str, float] | None = None,
) -> dict[str, Any]:
    support_labels = _load_labels(support_path)
    answer_labels = _load_labels(answers_path)
    if support_labels:
        # Reuse W3 merge semantics so labels must match run hash.
        from campusguide.eval.w3 import merge_labels

        try:
            merge_labels(run, support_labels)
        except ValueError as exc:
            print(f"warning: support labels 与当前 run 不匹配：{exc}", file=sys.stderr)
            support_labels = []
    report = build_w7_report(
        run,
        support_labels=support_labels,
        answer_labels=answer_labels,
        gates_config=gates,
    )
    output_json.parent.mkdir(parents=True, exist_ok=True)
    output_json.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    output_md.write_text(render_w7_markdown(report), encoding="utf-8")
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--testset", default=str(DEFAULT_TESTSET))
    parser.add_argument("--manifest", default=str(DEFAULT_MANIFEST))
    parser.add_argument("--run", default=str(DEFAULT_RUN))
    parser.add_argument("--support-labels", default=str(DEFAULT_SUPPORT))
    parser.add_argument("--answer-labels", default=str(DEFAULT_ANSWERS))
    parser.add_argument("--output-json", default=str(DEFAULT_JSON))
    parser.add_argument("--output-md", default=str(DEFAULT_MD))
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--no-warmup", action="store_true")
    parser.add_argument("--report-only", action="store_true")
    args = parser.parse_args()

    testset_path = Path(args.testset)
    manifest_path = Path(args.manifest)
    run_path = Path(args.run)
    support_path = Path(args.support_labels)
    answer_path = Path(args.answer_labels)

    if args.report_only:
        if not run_path.exists():
            print(f"error: run 不存在：{run_path}", file=sys.stderr)
            return 2
        run = load_run(run_path)
        report = _write_report(
            run=run,
            support_path=support_path,
            answers_path=answer_path,
            output_json=Path(args.output_json),
            output_md=Path(args.output_md),
        )
        print(f"W7 overall={'PASS' if report['overall'] else 'FAIL'}")
        print(f"report: {args.output_json}")
        print(f"report: {args.output_md}")
        return 0 if report["overall"] else 1

    try:
        rows, frozen_manifest = verify_frozen(
            testset_path=testset_path,
            manifest_path=manifest_path,
        )
    except (ValueError, FileNotFoundError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if args.limit is not None:
        rows = rows[: args.limit]

    try:
        result_rows, metadata = _run(rows, no_warmup=args.no_warmup, limit=args.limit)
    except Exception as exc:
        print(f"error: W7 run 失败（{type(exc).__name__}）：{exc}", file=sys.stderr)
        return 2

    run = build_run_snapshot(
        dataset=testset_path.name,
        dataset_path=testset_path,
        rows=result_rows,
        metadata=metadata,
    )
    run["frozen"] = True
    run["testset_sha256"] = frozen_manifest["sha256"]
    run = finalize_run(run)
    save_run(run, run_path)
    print(f"run: {run_path} sha256={run['run_sha256']}")

    existing_support = _load_labels(support_path)
    same_run = bool(existing_support) and all(
        item.get("run_sha256") == run["run_sha256"] for item in existing_support
    )
    template = carry_support_labels(build_label_template(run, baseline_labels=existing_support))
    if existing_support and not same_run:
        backup = support_path.with_suffix(".bak.jsonl")
        backup.write_text(
            "".join(json.dumps(item, ensure_ascii=False) + "\n" for item in existing_support),
            encoding="utf-8",
        )
        print(f"support labels 属于旧 run，已备份：{backup}")
    write_labels(template, support_path)
    carried = sum(1 for item in template if item.get("carried_over"))
    changed = sum(
        1
        for item in template
        if item.get("label") not in {"supported", "partial", "unsupported"}
        and not item.get("excluded_from_support")
    )
    print(
        f"support labels: {support_path}（共 {len(template)} 条，沿用 {carried}，待重标 {changed}）"
    )

    answer_labels = _answer_labels(result_rows, _load_labels(answer_path))
    answer_path.write_text(
        "".join(json.dumps(item, ensure_ascii=False) + "\n" for item in answer_labels),
        encoding="utf-8",
    )
    answer_carried = sum(1 for item in answer_labels if item.get("carried_over"))
    answer_changed = sum(1 for item in answer_labels if item.get("changed"))
    print(
        f"answer labels: {answer_path}（共 {len(answer_labels)} 条，沿用 {answer_carried}，"
        f"changed {answer_changed}）"
    )

    report = _write_report(
        run=run,
        support_path=support_path,
        answers_path=answer_path,
        output_json=Path(args.output_json),
        output_md=Path(args.output_md),
    )
    print(f"W7 overall={'PASS' if report['overall'] else 'FAIL'}")
    print(f"report: {args.output_json}")
    print(f"report: {args.output_md}")
    return 0 if report["overall"] else 1


if __name__ == "__main__":
    sys.exit(main())
