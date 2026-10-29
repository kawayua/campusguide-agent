"""Verify every conclusion-level citation of the 57-case run against the corpus.

For each citation: the article must exist, part_no must exist, the quote must
occur at the recorded offsets, and the quote must be a literal substring of that
part's text. Prints a per-case summary and the aggregate counts used in the
report.
"""

from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RESULTS = ROOT / "data/eval/answer_quality/batch_pending_57_20261028/results.jsonl"
ARTICLES = ROOT / "data/processed/articles.jsonl"
OUT = ROOT / "data/eval/answer_quality/batch_pending_57_20261028/citation_audit.json"


def load(path: Path) -> list[dict]:
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8-sig").splitlines()
        if line.strip()
    ]


def is_service_failure(record: dict) -> bool:
    return bool(record.get("service_failure_nodes")) or record.get("outcome") in {
        "GENERATION_FAILED",
        "CLIENT_ERROR",
        "INDEX_UNAVAILABLE",
        "TOOL_UNAVAILABLE",
    }


def main() -> None:
    articles = {}
    for row in load(ARTICLES):
        key = (row.get("article_id") or row.get("id"), row.get("part_no", 1))
        articles[key] = row
    attempts: dict[int, list[dict]] = {}
    for row in load(RESULTS):
        attempts.setdefault(row["case_number"], []).append(row)
    latest: dict[int, dict] = {}
    for number, rows in attempts.items():
        usable = [r for r in rows if not is_service_failure(r)]
        latest[number] = (usable or rows)[-1]

    total = 0
    errors = []
    per_case = {}
    for number in sorted(latest):
        citations = (latest[number].get("reply") or {}).get("citations") or []
        per_case[number] = len(citations)
        for cite in citations:
            total += 1
            article_id = cite.get("article_id")
            part_no = cite.get("part_no", 1)
            quote = cite.get("quote") or ""
            start = cite.get("quote_start")
            end = cite.get("quote_end")
            article = articles.get((article_id, part_no))
            if article is None:
                errors.append(
                    {"case": number, "article_id": article_id, "part_no": part_no, "error": "article_missing"}
                )
                continue
            text = article.get("text") or ""
            if not quote:
                errors.append({"case": number, "article_id": article_id, "error": "empty_quote"})
                continue
            if quote not in text:
                errors.append(
                    {
                        "case": number,
                        "article_id": article_id,
                        "part_no": part_no,
                        "error": "quote_not_in_text",
                        "quote_head": quote[:80],
                    }
                )
                continue
            if isinstance(start, int) and isinstance(end, int) and text[start:end] != quote:
                    errors.append(
                        {
                            "case": number,
                            "article_id": article_id,
                            "part_no": part_no,
                            "error": "offset_mismatch",
                            "start": start,
                            "end": end,
                            "found_at": text.find(quote),
                        }
                    )
    payload = {
        "cases": len(latest),
        "citations": total,
        "errors": errors,
        "per_case": per_case,
    }
    OUT.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"cases": len(latest), "citations": total, "error_count": len(errors)}, ensure_ascii=False))
    for error in errors[:20]:
        print(json.dumps(error, ensure_ascii=False))


if __name__ == "__main__":
    main()