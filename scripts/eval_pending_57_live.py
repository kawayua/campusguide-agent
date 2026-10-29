"""Sequential live /on_send evaluation for the pending 57-case handoff list.

Reads docs/reports/CampusGuide_待测与补测57题清单_20261028.jsonl, sends each
original input to the running Gradio app in a fresh session, and appends one raw
record per case. Application code and corpus are not touched.
"""

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
SOURCE = ROOT / "docs/reports/CampusGuide_待测与补测57题清单_20261028.jsonl"
OUT = ROOT / "data/eval/answer_quality/batch_pending_57_20261028"
URL = "http://127.0.0.1:7860/"


def write_summary(results: Path, total: int) -> dict:
    rows = (
        [json.loads(s) for s in results.read_text(encoding="utf-8").splitlines()]
        if results.exists()
        else []
    )
    summary = {
        "updated_at": datetime.now().astimezone().isoformat(),
        "completed": len(rows),
        "total": total,
        "outcomes": dict(Counter(r.get("outcome", "ERROR") for r in rows)),
        "service_failures": sum(1 for r in rows if r.get("service_failure_nodes")),
        "elapsed_s": round(sum(r.get("elapsed_s") or 0 for r in rows), 2),
    }
    (results.parent / "progress.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=OUT)
    parser.add_argument("--url", default=URL)
    parser.add_argument("--source", type=Path, default=SOURCE)
    parser.add_argument("--limit", type=int)
    parser.add_argument("--ids", help="Comma-separated case numbers, e.g. 067,068")
    parser.add_argument("--service-error-limit", type=int, default=2)
    parser.add_argument(
        "--retry-failed",
        action="store_true",
        help="Re-run cases whose last record was a provider/service failure instead of skipping them",
    )
    parser.add_argument(
        "--retry-wait",
        type=int,
        default=120,
        help="Seconds to wait before starting (and between retry passes) when recovering from 429",
    )
    parser.add_argument("--retry-passes", type=int, default=2)
    args = parser.parse_args()

    out_dir = args.output_dir.resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    results = out_dir / "results.jsonl"
    source = args.source.resolve()

    cases = [json.loads(s) for s in source.read_text(encoding="utf-8").splitlines() if s.strip()]
    if args.ids:
        wanted = {int(x) for x in args.ids.split(",") if x.strip()}
        cases = [c for c in cases if c["case_number"] in wanted]
        missing = wanted - {c["case_number"] for c in cases}
        if missing:
            parser.error(f"unknown case numbers: {sorted(missing)}")
    if args.limit:
        cases = cases[: args.limit]

    deadline = time.monotonic() + 180
    while True:
        try:
            response = httpx.get(args.url.rstrip("/") + "/config", timeout=10)
            response.raise_for_status()
            config = response.json()
            break
        except (httpx.HTTPError, ValueError):
            if time.monotonic() >= deadline:
                raise
            time.sleep(2)
    (out_dir / "app_config.json").write_text(
        json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    metadata_path = out_dir / "run_metadata.json"
    if not metadata_path.exists():
        metadata_path.write_text(
            json.dumps(
                {
                    "started_at": datetime.now().astimezone().isoformat(),
                    "url": args.url,
                    "transport": "gradio_client /on_send (same callback as UI submit)",
                    "mode": "sequential; independent fresh conversation per case; no follow-up or retry",
                    "business_as_of": "2026-10-25",
                    "testset_path": str(source),
                    "testset_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
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
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )

    def last_records() -> dict[str, dict]:
        if not results.exists():
            return {}
        rows = [
            json.loads(s)
            for s in results.read_text(encoding="utf-8").splitlines()
            if s.strip()
        ]
        return {row["id"]: row for row in rows}

    def is_service_failure(record: dict) -> bool:
        return bool(record.get("service_failure_nodes")) or record.get("outcome") in {
            "INDEX_UNAVAILABLE",
            "TOOL_UNAVAILABLE",
            "GENERATION_FAILED",
            "CLIENT_ERROR",
        }

    client = Client(args.url, verbose=False)
    total = len(cases)
    known = last_records()
    if args.retry_failed:
        target = [c for c in cases if c["id"] not in known or is_service_failure(known[c["id"]])]
        if target:
            print(
                f"waiting {args.retry_wait}s before retrying {len(target)} case(s)",
                flush=True,
            )
            time.sleep(args.retry_wait)
    else:
        target = [c for c in cases if c["id"] not in known]

    consecutive = 0
    for index, case in enumerate(target, 1):
        started = time.monotonic()
        record = {
            "id": case["id"],
            "case_number": case["case_number"],
            "test_type": case.get("test_type"),
            "question": case["input"],
            "started_at": datetime.now().astimezone().isoformat(),
        }
        try:
            outputs = client.predict({}, case["input"], api_name="/on_send")
            store = outputs[0]
            session = store["sessions"][store["current_id"]]
            reply = session.get("reply") or {}
            trace = reply.get("trace") or []
            usage = {}
            for node in trace:
                tokens = {
                    k: node.get(k)
                    for k in ("prompt_tokens", "completion_tokens")
                    if isinstance(node.get(k), int)
                }
                if tokens:
                    usage[node.get("node", "?")] = tokens
                semantic = node.get("semantic")
                if isinstance(semantic, dict) and semantic.get("prompt_tokens") is not None:
                    usage.setdefault("verifier.semantic", {}).update(
                        {
                            "prompt_tokens": semantic.get("prompt_tokens"),
                            "completion_tokens": semantic.get("completion_tokens"),
                        }
                    )
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
                    "trace_id": session.get("trace_id"),
                    "duration_ms": session.get("duration_ms"),
                    "token_usage": usage,
                    "raw_outputs": outputs,
                }
            )
        except Exception as exc:  # the record must survive any transport fault
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
        with results.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
            handle.flush()
        print(
            f"{index:03d}/{len(target)} {record['outcome']:<18} {record['elapsed_s']:6.1f}s "
            f"fail={','.join(unavailable) or '-':<20} {case['input'][:48]}",
            flush=True,
        )
        write_summary(results, total)
        if is_service_failure(record):
            consecutive += 1
        else:
            consecutive = 0
        if args.service_error_limit > 0 and consecutive >= args.service_error_limit:
            print("Stopped: consecutive service errors; raw records retained.", flush=True)
            break
    print(json.dumps(write_summary(results, total), ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()