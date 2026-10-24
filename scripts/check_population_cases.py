"""W4/P0 人群错配与 profile-aware 检索验收（真实 MCP）。

数据集：`data/eval/population_v1.jsonl`。每个 case 校验：
- forbidden_doc_ids 不得出现在返回结果；
- require_expected=true 时，expected_doc_ids 至少命中一个。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from campusguide.config import load_mcp_config
from campusguide.env import load_dotenv
from campusguide.mcp.client import MCPClientConfig, MCPPolicySearchClient
from campusguide.mcp.schemas import SearchArticlesRequest

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DATASET = ROOT / "data" / "eval" / "population_v1.jsonl"
DEFAULT_OUTPUT = ROOT / "data" / "eval" / "w4-population-report.json"


def load_cases(path: Path) -> list[dict[str, Any]]:
    cases: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as handle:
        for line_no, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            try:
                cases.append(json.loads(line))
            except json.JSONDecodeError as exc:
                raise ValueError(f"{path}:{line_no} 不是合法 JSON") from exc
    return cases


def _client_config(server) -> MCPClientConfig:
    command = list(server.command)
    if command and command[0] in {"python", "python3"}:
        command[0] = sys.executable
    return MCPClientConfig(
        command=command,
        timeout_seconds=server.timeout_seconds,
        retries=server.retries,
        startup_timeout_seconds=server.startup_timeout_seconds,
        cwd=str(ROOT),
    )


def _run_case(
    client: MCPPolicySearchClient,
    case: dict[str, Any],
) -> dict[str, Any]:
    started = time.perf_counter()
    response = client.search_articles(
        SearchArticlesRequest(
            query=case["query"],
            filters=case.get("filters") or {},
            top_k=30,
            rerank_top_n=8,
        )
    )
    latency_ms = int((time.perf_counter() - started) * 1000)
    docs = sorted({hit.article.doc_id for hit in response.data.articles})
    forbidden = sorted(set(docs) & set(case.get("forbidden_doc_ids") or []))
    expected_hits = sorted(set(docs) & set(case.get("expected_doc_ids") or []))
    required = bool(case.get("require_expected"))
    passed = not forbidden and (bool(expected_hits) if required else True)
    return {
        "id": case["id"],
        "name": case["name"],
        "query": case["query"],
        "filters": case.get("filters") or {},
        "returned_doc_ids": docs,
        "forbidden_hits": forbidden,
        "expected_hits": expected_hits,
        "require_expected": required,
        "passed": passed,
        "latency_ms": latency_ms,
        "index_version": response.data.index_version,
        "warnings": response.data.warnings,
    }


def main() -> int:
    load_dotenv(ROOT / ".env")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", default=str(DEFAULT_DATASET))
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT))
    args = parser.parse_args()

    dataset_path = Path(args.dataset)
    cases = load_cases(dataset_path)
    mcp_config = load_mcp_config(ROOT / "configs" / "mcp.yaml")
    client = MCPPolicySearchClient(_client_config(mcp_config.search_server))
    rows: list[dict[str, Any]] = []
    ready_ms = 0
    try:
        started = time.perf_counter()
        client.start()
        ready_ms = int((time.perf_counter() - started) * 1000)
        print(f"policy-search ready in {ready_ms}ms")
        for case in cases:
            row = _run_case(client, case)
            rows.append(row)
            status = "PASS" if row["passed"] else "FAIL"
            print(
                f"[{status}] {row['id']} {row['name']}\n"
                f"   docs={row['returned_doc_ids']}\n"
                f"   forbidden={row['forbidden_hits'] or '-'} "
                f"expected_hits={row['expected_hits'] or '-'}"
            )
    finally:
        client.close()

    passed = sum(1 for row in rows if row["passed"])
    report = {
        "schema_version": "population-v1",
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "dataset": dataset_path.name,
        "dataset_sha256": hashlib.sha256(dataset_path.read_bytes()).hexdigest(),
        "index_version": rows[0]["index_version"] if rows else "",
        "ready_ms": ready_ms,
        "cases": rows,
        "summary": {
            "total": len(rows),
            "passed": passed,
            "failed": len(rows) - passed,
            "forbidden_hits": sum(len(row["forbidden_hits"]) for row in rows),
            "expected_hit_cases": sum(1 for row in rows if row["expected_hits"]),
        },
    }
    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(f"\npopulation cases: {passed}/{len(rows)}")
    print(f"report: {output_path}")
    return 0 if passed == len(rows) else 1


if __name__ == "__main__":
    sys.exit(main())
