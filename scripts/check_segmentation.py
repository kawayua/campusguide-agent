"""从 articles.jsonl 抽样条款，生成人工核对清单（规格书 6.4 的 20 条抽检）。"""

from __future__ import annotations

import argparse
import json
import random
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]


def _load_articles(path: Path) -> list[dict[str, Any]]:
    articles = []
    with path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                articles.append(json.loads(line))
            except json.JSONDecodeError as exc:
                raise ValueError(f"{path}:{line_number} 不是合法 JSON：{exc}") from exc
    return articles


def _preview(text: str, limit: int = 120) -> str:
    text = " ".join(text.split())
    if len(text) <= limit:
        return text
    return text[:limit] + "…"


def _cell(value: str) -> str:
    return value.replace("|", "\\|")


def render_report(articles: list[dict[str, Any]], *, sample_size: int, seed: int) -> str:
    ordered = sorted(articles, key=lambda item: str(item.get("article_id", "")))
    if len(ordered) > sample_size:
        sampled = random.Random(seed).sample(ordered, sample_size)
        sampled.sort(key=lambda item: str(item.get("article_id", "")))
    else:
        sampled = ordered

    lines = [
        "# 条款切分人工抽检",
        "",
        f"- 生成时间：{datetime.now(UTC).isoformat(timespec='seconds')}",
        f"- 条款总数：{len(articles)}",
        f"- 抽样数量：{len(sampled)}（seed={seed}）",
        "- 判定项：边界、编号、章节路径、正文完整性；四项全部通过才算正确。",
        "- 目标：正确率 ≥95%；发现错误时记录类型并回到切分规则修正。",
        "",
        "| # | article_id | 文档 | 章节 | 条款 | 正文预览 | 边界 | 编号 | 章节 | 完整 |",
        "|---|---|---|---|---|---|---|---|---|---|",
    ]
    for index, article in enumerate(sampled, start=1):
        chapter = " > ".join(article.get("chapter_path") or []) or "-"
        lines.append(
            "| {index} | {article_id} | {doc} | {chapter} | {no} | {preview} | "
            "[ ] | [ ] | [ ] | [ ] |".format(
                index=index,
                article_id=_cell(str(article.get("article_id", ""))),
                doc=_cell(str(article.get("doc_title", ""))),
                chapter=_cell(chapter),
                no=_cell(str(article.get("article_no", ""))),
                preview=_cell(_preview(str(article.get("text", "")))),
            )
        )
    lines.extend(
        [
            "",
            "## 错误记录",
            "",
            "| article_id | 错误类型 | 说明 |",
            "|---|---|---|",
            "|  |  |  |",
            "",
        ]
    )
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--articles",
        default=str(ROOT / "data" / "processed" / "articles.jsonl"),
    )
    parser.add_argument(
        "--output",
        default=str(ROOT / "data" / "review" / "segmentation-sample.md"),
    )
    parser.add_argument("--sample-size", type=int, default=20)
    parser.add_argument("--seed", type=int, default=20261019)
    args = parser.parse_args()

    articles_path = Path(args.articles)
    if not articles_path.exists():
        print(f"error: 找不到 {articles_path}，请先执行 make index", file=sys.stderr)
        return 2
    if args.sample_size <= 0:
        print("error: --sample-size 必须大于 0", file=sys.stderr)
        return 2

    try:
        articles = _load_articles(articles_path)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    report = render_report(articles, sample_size=args.sample_size, seed=args.seed)
    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(report, encoding="utf-8")
    print(f"sampled {min(args.sample_size, len(articles))}/{len(articles)} articles")
    print(f"report: {output_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
