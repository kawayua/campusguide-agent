"""校验并冻结 W7 测试集，生成 SHA-256 manifest。"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from campusguide.eval.w7 import (
    load_jsonl,
    validate_testset,
    write_frozen_testset,
)

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DRAFT = ROOT / "data" / "eval" / "test_v1.draft.jsonl"
DEFAULT_OUTPUT = ROOT / "data" / "eval" / "test_v1.jsonl"
DEFAULT_MANIFEST = ROOT / "data" / "eval" / "test_v1.manifest.json"
DEFAULT_ARTICLES = ROOT / "data" / "processed" / "articles.jsonl"
DEFAULT_DEV = ROOT / "data" / "eval" / "dev_v0.jsonl"
DEFAULT_POPULATION = ROOT / "data" / "eval" / "population_v1.jsonl"


def _questions(path: Path) -> list[str]:
    return [str(row.get("question", "")) for row in load_jsonl(path)]


def _article_index(path: Path) -> dict[str, dict]:
    articles: dict[str, dict] = {}
    for row in load_jsonl(path):
        article_id = str(row.get("article_id", ""))
        if article_id:
            articles[article_id] = row
    return articles


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--draft", default=str(DEFAULT_DRAFT))
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT))
    parser.add_argument("--manifest", default=str(DEFAULT_MANIFEST))
    parser.add_argument("--articles", default=str(DEFAULT_ARTICLES))
    parser.add_argument("--dev", default=str(DEFAULT_DEV))
    parser.add_argument("--population", default=str(DEFAULT_POPULATION))
    parser.add_argument("--force", action="store_true", help="覆盖已存在的冻结文件")
    args = parser.parse_args()

    output = Path(args.output)
    if output.exists() and not args.force:
        print(
            f"error: 测试集已存在：{output}；冻结文件不得覆盖，"
            "如确需重建请使用新的版本文件名或 --force 并记录 changelog",
            file=sys.stderr,
        )
        return 2

    draft_path = Path(args.draft)
    if not draft_path.exists():
        print(f"error: draft 不存在：{draft_path}", file=sys.stderr)
        return 2
    try:
        rows = load_jsonl(draft_path)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    errors = validate_testset(
        rows,
        articles=_article_index(Path(args.articles)),
        dev_questions=_questions(Path(args.dev)) if Path(args.dev).exists() else [],
        population_questions=(
            _questions(Path(args.population)) if Path(args.population).exists() else []
        ),
    )
    if errors:
        print(f"error: 测试集校验失败（{len(errors)} 项）：", file=sys.stderr)
        for error in errors:
            print(f"  - {error}", file=sys.stderr)
        return 1

    manifest = write_frozen_testset(
        rows,
        output=output,
        manifest_path=Path(args.manifest),
        source=str(draft_path),
    )
    print(
        f"frozen: {output}\n"
        f"manifest: {args.manifest}\n"
        f"questions: {manifest['questions']}\n"
        f"composition: {json.dumps(manifest['composition'], ensure_ascii=False)}\n"
        f"sha256: {manifest['sha256']}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
