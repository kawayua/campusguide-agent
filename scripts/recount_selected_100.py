"""Recount citations and timing on the 100 selected sessions only.

The earlier report summed the two rounds' audit totals (149 + 176 = 325), which
double counts nothing but does count every round-1 attempt. This script follows
the selected session of each merged case, so each question is counted once, and
uses the statistical median (mean of the two middle values for even n).
"""

from __future__ import annotations

import json
import pathlib
import statistics

ROOT = pathlib.Path(__file__).resolve().parents[1]
MERGED = ROOT / "data/eval/answer_quality/merged_100_20261028/merged_cases.jsonl"
R1 = ROOT / "data/eval/answer_quality/batch_retest_20261028_162015"
R2 = ROOT / "data/eval/answer_quality/batch_pending_57_20261028"


def load_jsonl(path: pathlib.Path) -> list[dict]:
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8-sig").splitlines()
        if line.strip()
    ]


def main() -> None:
    merged = load_jsonl(MERGED)
    rows: dict[str, dict] = {}
    for path in sorted(R1.glob("batch_*/results.jsonl")):
        for row in load_jsonl(path):
            rows[row["session_id"]] = row
    for row in load_jsonl(R2 / "results.jsonl"):
        rows[row["session_id"]] = row

    per_case = {}
    citations_total = 0
    durations = []
    missing = []
    for case in merged:
        session = case.get("session_id")
        row = rows.get(session)
        if row is None:
            missing.append(case["case_number"])
            continue
        count = len((row.get("reply") or {}).get("citations") or [])
        per_case[case["case_number"]] = count
        citations_total += count
        durations.append(row.get("elapsed_s") or 0.0)

    stats = {
        "cases": len(per_case),
        "missing_sessions": missing,
        "citations": citations_total,
        "citations_by_case": per_case,
        "elapsed_total_s": round(sum(durations), 3),
        "elapsed_median_s": round(statistics.median(durations), 3),
        "elapsed_median_note": "statistical median (mean of two middle values)",
        "elapsed_max_s": round(max(durations), 3),
    }
    out = ROOT / "data/eval/answer_quality/merged_100_20261028/selection_stats.json"
    out.write_text(json.dumps(stats, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({k: v for k, v in stats.items() if k != "citations_by_case"}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()