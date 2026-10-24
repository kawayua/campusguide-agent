"""W3 56 条 draft claim 的人工 support 标注 CLI。

脚本只展示 claim 与 quote，不自动判定；人工逐条选择
supported / partial / unsupported，并写入 annotator 与 labeled_at。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

from campusguide.eval.w3 import (
    LABELED,
    apply_support_label,
    build_label_template,
    effective_label,
    label_counts,
    load_labels,
    load_run,
    render_label_sheet,
    verify_run_hash,
    write_labels,
)

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_RUN = ROOT / "data" / "eval" / "w3_run.json"
DEFAULT_LABELS = ROOT / "data" / "eval" / "w3_support_labels.jsonl"
DEFAULT_SHEET = ROOT / "data" / "eval" / "w3_support_review.md"

RUBRIC = """严格引文级判定口径（只看 quote 文本，不看条款上下文/问题/外部知识）：
- supported：quote 本身直接支持 claim 的主要事实，数字、日期、条件、范围一致。
- partial：quote 支持主要事实，但 claim 多了关键条件/范围/第二个断言，
  或措辞强弱不一致。
- unsupported：quote 与 claim 的主要事实不符、矛盾，或只是主题相关。
可按 ? 再次查看；k 跳过；q 保存并退出。"""


def _load_records(run: dict[str, Any], labels_path: Path) -> list[dict[str, Any]]:
    if not labels_path.exists():
        records = build_label_template(run)
        write_labels(records, labels_path)
        print(f"已生成 support label 模板：{labels_path}（{len(records)} 条）")
        return records
    records = load_labels(labels_path)
    if not records:
        records = build_label_template(run)
        write_labels(records, labels_path)
        print(f"labels 为空，已生成模板：{labels_path}（{len(records)} 条）")
        return records
    expected = run["run_sha256"]
    mismatched = [item for item in records if item.get("run_sha256") != expected]
    if mismatched:
        raise SystemExit(
            "labels 与当前 run 的 run_sha256 不一致，拒绝标注。\n"
            f"  run:    {expected}\n"
            f"  labels: {mismatched[0].get('run_sha256')}\n"
            "请先重跑 run，或把旧 labels 备份后重新生成模板；"
            "不要在新 run 上沿用旧标注。"
        )
    return records


def _questions(run: dict[str, Any]) -> dict[str, str]:
    return {row["id"]: row.get("question", "") for row in run.get("rows", [])}


def _print_item(record: dict[str, Any], question: str, index: int, total: int) -> None:
    kept = "kept" if record.get("kept") else "dropped"
    if record.get("dropped_reason"):
        kept += f"（{record['dropped_reason']}）"
    located = record.get("located_quotes") or []
    draft_quotes = record.get("quotes") or []
    quotes = located or draft_quotes
    print("\n" + "=" * 78)
    print(f"[{index}/{total}] {record['question_id']} / {record['claim_id']}  {kept}")
    print(f"问题：{question}")
    print(f"claim：{record.get('text', '')}")
    if located:
        header = (
            "quote（located，渲染时使用）："
            if record.get("kept")
            else "quote（located，该 claim 已被丢弃）："
        )
    else:
        header = "quote（模型 draft）："
    print(header)
    if quotes:
        for quote_index, quote in enumerate(quotes, start=1):
            print(f"  {quote_index}. {quote}")
    else:
        print("  （无可用 quote）")
    issues = record.get("issues") or []
    facts = record.get("fact_uncovered") or []
    if issues:
        print(f"issues：{', '.join(issues)}")
    if facts:
        print(f"fact_uncovered：{', '.join(facts)}")
    current = effective_label(record)
    if current != "unlabeled":
        print(f"当前标签：{current}（{record.get('annotator', '')}）")
    if record.get("prefill_label"):
        print(
            f"prefill 建议：{record['prefill_label']}（来自 "
            f"{record.get('prefill_from') or record.get('prefill_source', 'baseline')}，待确认）"
        )
        if record.get("prefill_note"):
            print(f"prefill note：{record['prefill_note']}")


def _prompt_choice(prefill_label: str | None = None) -> str | None:
    hint = f"[Enter=接受 prefill {prefill_label}] / " if prefill_label in LABELED else ""
    while True:
        try:
            choice = (
                input(f"判定 {hint}[s]upported/[p]artial/[u]nsupported/[k]ip/[q]uit/[?]help: ")
                .strip()
                .lower()
            )
        except EOFError:
            return "q"
        if choice == "?":
            print(RUBRIC)
            continue
        if choice == "" and prefill_label in LABELED:
            return "prefill"
        if choice in {"s", "p", "u", "k", "q"}:
            return choice
        print("输入无效：请输入 s / p / u / k / q / ?")


def _print_counts(records: list[dict[str, Any]]) -> None:
    counts = label_counts(records)
    print(
        "进度："
        f"supported={counts['supported']} "
        f"partial={counts['partial']} "
        f"unsupported={counts['unsupported']} "
        f"unlabeled={counts['unlabeled']}"
    )


def _write_sheet(
    records: list[dict[str, Any]],
    questions: dict[str, str],
    output: Path,
    *,
    title: str,
) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        render_label_sheet(records, questions, title=title),
        encoding="utf-8",
    )
    print(f"核对表：{output}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", default=str(DEFAULT_RUN))
    parser.add_argument("--labels", default=str(DEFAULT_LABELS))
    parser.add_argument(
        "--annotator",
        default="",
        help="人工标注者标识（不能为空或以 ai 开头）",
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="复查全部 56 条（默认只显示 unlabeled）",
    )
    parser.add_argument(
        "--status",
        action="store_true",
        help="只打印标注进度，不进入交互",
    )
    parser.add_argument(
        "--sheet",
        action="store_true",
        help="生成 Markdown 核对表，不进入交互",
    )
    parser.add_argument(
        "--sheet-output",
        default=str(DEFAULT_SHEET),
        help="核对表输出路径",
    )
    parser.add_argument(
        "--force-sheet",
        action="store_true",
        help="允许覆盖已存在的核对表（默认拒绝，避免抹掉人工勾选/note）",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="本次最多标注多少条",
    )
    args = parser.parse_args()

    run_path = Path(args.run)
    labels_path = Path(args.labels)
    if not run_path.exists():
        print(f"error: run 不存在：{run_path}", file=sys.stderr)
        return 2
    run = load_run(run_path)
    try:
        verify_run_hash(run)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    records = _load_records(run, labels_path)
    questions = _questions(run)

    if args.status:
        print(f"run_sha256: {run['run_sha256']}")
        print(f"labels: {labels_path}")
        _print_counts(records)
        prefill_pending = sum(
            1
            for record in records
            if record.get("prefill_label") and effective_label(record) == "unlabeled"
        )
        if prefill_pending:
            print(f"prefill 待确认：{prefill_pending}")
        unlabeled = [
            f"{record['question_id']}/{record['claim_id']}"
            for record in records
            if effective_label(record) == "unlabeled"
        ]
        if unlabeled:
            print(f"未标注：{', '.join(unlabeled[:20])}")
            if len(unlabeled) > 20:
                print(f"... 另有 {len(unlabeled) - 20} 条")
        return 0

    if args.sheet:
        output = Path(args.sheet_output)
        if output.exists() and not args.force_sheet:
            print(
                f"error: 核对表已存在：{output}；为避免覆盖人工勾选/note，"
                "请先备份或加 --force-sheet。",
                file=sys.stderr,
            )
            return 2
        prompt_version = str(run.get("metadata", {}).get("advisor_prompt_version") or "")
        if prompt_version.startswith("w6a2"):
            sheet_title = "W6-A2 support 标注核对表"
        elif prompt_version.startswith("w6"):
            sheet_title = "W6-A support 标注核对表"
        else:
            sheet_title = "W3 support 标注核对表"
        _write_sheet(records, questions, output, title=sheet_title)
        _print_counts(records)
        return 0

    annotator = args.annotator.strip()
    if not annotator or annotator.lower().startswith("ai"):
        print(
            "error: 必须提供人工 annotator，例如 --annotator yaukin；不能为空或以 ai 开头。",
            file=sys.stderr,
        )
        return 2

    indices = [
        index
        for index, record in enumerate(records)
        if args.all or effective_label(record) == "unlabeled"
    ]
    if args.limit is not None:
        indices = indices[: args.limit]
    if not indices:
        print("没有待标注条目（全部已标注）。用 --all 可复查。")
        _print_counts(records)
        return 0

    print(f"标注者：{annotator}")
    print(f"run_sha256: {run['run_sha256']}")
    print(RUBRIC)
    print(f"本次待处理：{len(indices)} 条")

    try:
        for position, index in enumerate(indices, start=1):
            record = records[index]
            _print_item(record, questions.get(record["question_id"], ""), position, len(indices))
            choice = _prompt_choice(record.get("prefill_label"))
            if choice == "q":
                print("\n保存并退出。")
                break
            if choice == "k":
                continue
            if choice == "prefill":
                label = record["prefill_label"]
                default_note = record.get("prefill_note", "")
            else:
                label = {"s": "supported", "p": "partial", "u": "unsupported"}[choice]
                default_note = ""
            prompt = "note（Enter 保留 prefill note）: " if default_note else "note（Enter 跳过）: "
            try:
                entered = input(prompt).strip()
            except EOFError:
                entered = ""
            records[index] = apply_support_label(
                record,
                label,
                annotator=annotator,
                note=entered or default_note,
            )
            write_labels(records, labels_path)
            _print_counts(records)
    except KeyboardInterrupt:
        print("\n已中断；当前进度已保存。")

    print(f"\nlabels：{labels_path}")
    _print_counts(records)
    print("下一步：make eval-w3-report（或运行 scripts/eval_w3.py --report-only）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
