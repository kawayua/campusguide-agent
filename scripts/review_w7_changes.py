"""W7 第二轮：从自动沿用后的 labels 生成"只重标 changed"核对表。

输入是 ``eval_w7.py`` 在重跑后写出的 support/answer labels（未变更条目
已沿用旧标签），本脚本只输出仍需人工判定的条目，并生成 diff 摘要。
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from campusguide.eval.w3 import render_label_sheet
from campusguide.eval.w7 import (
    changed_answer_records,
    changed_support_records,
    load_jsonl,
    render_answer_review,
)


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--support-labels", required=True)
    parser.add_argument("--answer-labels", required=True)
    parser.add_argument("--support-review", required=True)
    parser.add_argument("--answer-review", required=True)
    parser.add_argument("--summary", required=True)
    args = parser.parse_args()

    support_path = Path(args.support_labels)
    answer_path = Path(args.answer_labels)
    support_labels = load_jsonl(support_path)
    answer_labels = load_jsonl(answer_path)

    changed_support = changed_support_records(support_labels)
    changed_answers = changed_answer_records(answer_labels)
    questions = {
        str(item.get("question_id")): str(item.get("question") or "") for item in support_labels
    }
    questions.update(
        {str(item.get("qid")): str(item.get("question") or "") for item in answer_labels}
    )

    _write(
        Path(args.support_review),
        render_label_sheet(
            changed_support,
            questions,
            title="W7 remediation2 changed claims（只含本轮新增/变化 claim）",
            allow_excluded=True,
        ),
    )
    _write(
        Path(args.answer_review),
        render_answer_review(
            changed_answers,
            title="W7 remediation2 changed answers（只含本轮变化题目）",
        ),
    )

    support_carried = sum(1 for item in support_labels if item.get("carried_over"))
    answer_carried = sum(1 for item in answer_labels if item.get("carried_over"))
    summary: dict[str, Any] = {
        "run_sha256": (support_labels[0].get("run_sha256") if support_labels else None),
        "support": {
            "total": len(support_labels),
            "carried": support_carried,
            "changed": len(changed_support),
        },
        "answer": {
            "total": len(answer_labels),
            "carried": answer_carried,
            "changed": len(changed_answers),
        },
        "support_review": str(Path(args.support_review)),
        "answer_review": str(Path(args.answer_review)),
    }
    _write(Path(args.summary), json.dumps(summary, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
