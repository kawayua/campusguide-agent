"""把 W7 核对表 Markdown 里的 ``[x]`` 判定写回 labels JSONL。

示例：
    python scripts/import_w7_review.py --annotator yaukin \
        --support-labels data/eval/w7_remediation2_support_labels.jsonl \
        --support-review data/eval/w7_remediation2_support_review.md \
        --answer-labels data/eval/w7_remediation2_answer_labels.jsonl \
        --answer-review data/eval/w7_remediation2_answer_review.md
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from campusguide.eval.w7 import (
    analyze_answer_labels,
    analyze_support_labels,
    apply_answer_review,
    apply_support_review,
    load_jsonl,
    parse_answer_review,
    parse_support_review,
)


def _write_labels(path: Path, labels: list[dict]) -> None:
    lines = [json.dumps(item, ensure_ascii=False) for item in labels]
    path.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--support-labels")
    parser.add_argument("--support-review")
    parser.add_argument("--answer-labels")
    parser.add_argument("--answer-review")
    parser.add_argument("--annotator", required=True)
    args = parser.parse_args()
    if not (args.support_labels and args.support_review) and not (
        args.answer_labels and args.answer_review
    ):
        parser.error("至少要给一组 support 或 answer 的 labels + review")

    if args.support_labels and args.support_review:
        labels = load_jsonl(Path(args.support_labels))
        review = parse_support_review(Path(args.support_review).read_text(encoding="utf-8"))
        updated = apply_support_review(labels, review, annotator=args.annotator)
        _write_labels(Path(args.support_labels), labels)
        stats = analyze_support_labels(labels)
        print(f"support：核对表 {len(review)} 条，写回 {updated} 条；{stats}")

    if args.answer_labels and args.answer_review:
        labels = load_jsonl(Path(args.answer_labels))
        review = parse_answer_review(Path(args.answer_review).read_text(encoding="utf-8"))
        updated = apply_answer_review(labels, review, annotator=args.annotator)
        _write_labels(Path(args.answer_labels), labels)
        stats = analyze_answer_labels(labels)
        print(f"answer：核对表 {len(review)} 题，写回 {updated} 题；{stats}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
