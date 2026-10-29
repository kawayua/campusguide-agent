"""Freeze the 100 selected sessions, corpus version and verdicts (report R9 step 1).

Writes data/eval/answer_quality/frozen_100_20261028/manifest.json so later
reviews can be traced to exactly the sessions and hashes they judged.
"""

from __future__ import annotations

import hashlib
import json
import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[1]
AQ = ROOT / "data/eval/answer_quality"
OUT = AQ / "frozen_100_20261028"
MERGED = AQ / "merged_100_20261028/merged_cases.jsonl"
GOLD = AQ / "gold_v1.jsonl"
ARTICLES = ROOT / "data/processed/articles.jsonl"


def sha(path: pathlib.Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    merged = [
        json.loads(line)
        for line in MERGED.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    gold = {
        int(row["id"].rsplit("_", 1)[-1]): row
        for row in (
            json.loads(line)
            for line in GOLD.read_text(encoding="utf-8").splitlines()
            if line.strip()
        )
    }
    cases = []
    for row in sorted(merged, key=lambda r: r["case_number"]):
        number = row["case_number"]
        review = row["review"]
        cases.append(
            {
                "case_number": number,
                "id": row["id"],
                "question": row["question"],
                "round": row["round"],
                "session_id": row["session_id"],
                "started_at": row.get("started_at"),
                "outcome": row.get("outcome"),
                "original_status": review["status"],
                "original_reason": review["reason"],
                "review_v2_status": (gold[number].get("review_v2") or {}).get("status"),
                "required_checks": gold[number]["required_checks"],
                "optional_checks": gold[number].get("optional_checks") or [],
            }
        )
    manifest = {
        "frozen_at": "2026-10-28",
        "business_as_of": "2026-10-25",
        "cases": len(cases),
        "selection": "one selected session per case; retested cases use the round-2 success",
        "hashes": {
            "gold_v1.jsonl": sha(GOLD),
            "merged_cases.jsonl": sha(MERGED),
            "articles.jsonl": sha(ARTICLES),
        },
        "original_verdict_counts": _counts(c["original_status"] for c in cases),
        "review_v2_verdict_counts": _counts(
            c["review_v2_status"] for c in cases if c["review_v2_status"]
        ),
        "sessions": {str(c["case_number"]): c["session_id"] for c in cases},
        "questions_sha256": hashlib.sha256(
            "\n".join(c["question"] for c in cases).encode("utf-8")
        ).hexdigest(),
    }
    (OUT / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (OUT / "cases.jsonl").write_text(
        "".join(json.dumps(c, ensure_ascii=False) + "\n" for c in cases), encoding="utf-8"
    )
    print(
        json.dumps(
            {
                "cases": manifest["cases"],
                "original": manifest["original_verdict_counts"],
                "review_v2": manifest["review_v2_verdict_counts"],
                "hashes": manifest["hashes"],
            },
            ensure_ascii=False,
            indent=2,
        )
    )


def _counts(values) -> dict[str, int]:
    out: dict[str, int] = {}
    for value in values:
        out[value] = out.get(value, 0) + 1
    return out


if __name__ == "__main__":
    main()