"""Sequential live UI callback evaluation; records real replies without changing app code."""

from __future__ import annotations

import argparse
import hashlib
import json
import time
import traceback
from collections import Counter
from datetime import datetime
from pathlib import Path

import httpx
from gradio_client import Client

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "data/eval/corpus_random_100_live"
OUT.mkdir(parents=True, exist_ok=True)
SOURCE = ROOT / "data/eval/corpus_random_100_questions.jsonl"
RESULTS = OUT / "results.jsonl"
URL = "http://127.0.0.1:7860/"
TOTAL = 100


def write_summary():
    rows = (
        [json.loads(s) for s in RESULTS.read_text(encoding="utf-8").splitlines()]
        if RESULTS.exists()
        else []
    )
    summary = {
        "completed": len(rows),
        "total": TOTAL,
        "outcomes": dict(Counter(r.get("outcome", "ERROR") for r in rows)),
        "elapsed_s": round(sum(r["elapsed_s"] for r in rows), 2),
        "updated_at": datetime.now().astimezone().isoformat(),
    }
    (OUT / "progress.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return summary


def main():
    global OUT, RESULTS, URL, TOTAL, SOURCE
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=OUT)
    parser.add_argument("--url", default=URL)
    parser.add_argument("--source", type=Path, default=SOURCE, help="Frozen JSONL question set")
    parser.add_argument("--limit", type=int)
    parser.add_argument(
        "--service-error-limit",
        type=int,
        default=3,
        help="Stop after consecutive infrastructure failures; resume after recovery",
    )
    parser.add_argument("--ids", help="Comma-separated question IDs for a smoke run")
    args = parser.parse_args()
    OUT = args.output_dir.resolve()
    OUT.mkdir(parents=True, exist_ok=True)
    RESULTS = OUT / "results.jsonl"
    URL = args.url
    SOURCE = args.source.resolve()
    cases = [json.loads(s) for s in SOURCE.read_text(encoding="utf-8").splitlines()]
    if args.ids:
        selected = set(args.ids.split(","))
        cases = [c for c in cases if c["id"] in selected]
        if {c["id"] for c in cases} != selected:
            parser.error("Unknown question IDs")
    if args.limit:
        cases = cases[: args.limit]
    TOTAL = len(cases)
    ready_deadline = time.monotonic() + 180
    while True:
        try:
            response = httpx.get(URL.rstrip("/") + "/config", timeout=10)
            response.raise_for_status()
            config = response.json()
            break
        except (httpx.HTTPError, ValueError):
            if time.monotonic() >= ready_deadline:
                raise
            time.sleep(2)
    (OUT / "app_config.json").write_text(
        json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    metadata_path = OUT / "run_metadata.json"
    if not metadata_path.exists():
        metadata_path.write_text(
            json.dumps(
                {
                    "started_at": datetime.now().astimezone().isoformat(),
                    "url": URL,
                    "transport": "gradio_client /on_send (same callback as UI submit)",
                    "mode": "sequential; independent fresh conversation per case; no automatic follow-up or retry",
                    "testset_sha256": hashlib.sha256(SOURCE.read_bytes()).hexdigest(),
                    "testset_path": str(SOURCE),
                    "business_code_sha256": {
                        str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest()
                        for p in [
                            *sorted((ROOT / "src").rglob("*.py")),
                            *sorted((ROOT / "configs").rglob("*.yaml")),
                        ]
                    },
                    "source_sha256": hashlib.sha256(
                        (ROOT / "data/processed/articles.jsonl").read_bytes()
                    ).hexdigest(),
                    "gradio_version": config.get("version"),
                    "code_changed": OUT.name != "corpus_random_100_live",
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
    completed = (
        {json.loads(s)["id"] for s in RESULTS.read_text(encoding="utf-8").splitlines()}
        if RESULTS.exists()
        else set()
    )
    client = Client(URL, verbose=False)
    consecutive_service_errors = 0
    for index, case in enumerate(cases, 1):
        if case["id"] in completed:
            continue
        started = time.monotonic()
        record = {
            "id": case["id"],
            "question": case["question"],
            "category": case["category"],
            "reference_case_id": case.get("reference_case_id", case["id"]),
            "started_at": datetime.now().astimezone().isoformat(),
        }
        try:
            outputs = client.predict({}, case["question"], api_name="/on_send")
            store = outputs[0]
            session = store["sessions"][store["current_id"]]
            reply = session.get("reply") or {}
            record.update(
                {
                    "outcome": reply.get("outcome") or session.get("outcome") or "UI_ERROR",
                    "answer": reply.get("text")
                    or next(
                        (
                            x["content"]
                            for x in reversed(session.get("messages", []))
                            if x["role"] == "assistant"
                        ),
                        "",
                    ),
                    "reply": reply,
                    "session_id": session["id"],
                    "raw_outputs": outputs,
                }
            )
        except Exception as exc:
            record.update(
                {
                    "outcome": "CLIENT_ERROR",
                    "answer": "",
                    "error_type": type(exc).__name__,
                    "error": str(exc),
                    "traceback": traceback.format_exc(),
                }
            )
        trace = (record.get("reply") or {}).get("trace") or []
        unavailable = [
            node.get("node")
            for node in trace
            if (node.get("node") == "coverage" and node.get("unavailable"))
            or (isinstance(node.get("semantic"), dict) and node["semantic"].get("unavailable"))
        ]
        record["service_failure_nodes"] = unavailable
        record["elapsed_s"] = round(time.monotonic() - started, 3)
        with RESULTS.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
            f.flush()
        print(
            f"{index:03d}/{TOTAL} {record['outcome']} {record['elapsed_s']:.1f}s {case['question']}",
            flush=True,
        )
        write_summary()
        if unavailable or record["outcome"] in {
            "INDEX_UNAVAILABLE",
            "TOOL_UNAVAILABLE",
            "GENERATION_FAILED",
            "CLIENT_ERROR",
        }:
            consecutive_service_errors += 1
        else:
            consecutive_service_errors = 0
        if args.service_error_limit > 0 and consecutive_service_errors >= args.service_error_limit:
            print(
                "Stopped: consecutive service errors; raw records retained for diagnosis.",
                flush=True,
            )
            break
    print(json.dumps(write_summary(), ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
