"""W6-B 图级 dev_v0 报告：结构、拒答、语义、trace 与 run manifest。

W6 是 W7 之后的独立能力；本模块不读取/修改 W7 冻结集与报告，也不把
W6 指标写进 W7。support 标注仍是记录项，不充当 W6 硬门禁。
"""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from pathlib import Path
from typing import Any

from campusguide.domain.enums import VerificationDecision
from campusguide.domain.models import Citation, Claim, Evidence
from campusguide.eval.w3 import _citation_payload, build_report

SCHEMA_VERSION = "w6b-run-v1"
RENDERED = {"ANSWERED", "PARTIAL_ANSWER"}


def _sha256(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def row_from_graph(item: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
    evidence: list[Evidence] = list(state.get("evidence") or [])
    evidence_by_key = {
        (entry.article.article_id, entry.article.part_no): entry for entry in evidence
    }
    evidence_ids = {entry.article.article_id for entry in evidence}
    claims: list[Claim] = list(state.get("claims") or [])
    citations = [
        _citation_payload(
            Citation.model_validate(citation),
            evidence_by_key,
            evidence_ids,
        )
        for citation in state.get("citations") or []
    ]
    valid_citations = sum(1 for citation in citations if citation["valid_id"])
    fabricated = sum(1 for citation in citations if citation["fabricated"])
    wrong_part = sum(1 for citation in citations if citation["wrong_part"])
    quotes_located = sum(1 for citation in citations if citation["span_ok"])
    issues = list(state.get("verification_issues") or [])
    decision = state.get("verification_decision")
    pass_with_blocking = decision is VerificationDecision.PASS and any(
        issue.get("severity") == "blocking" for issue in issues
    )
    expected_ids = set(item.get("expected_article_ids") or [])
    top8 = [entry.article.article_id for entry in evidence[:8]]
    recall = len(expected_ids & set(top8)) / len(expected_ids) if expected_ids else None
    outcome = (state.get("outcome") or {}).get("code", "UNKNOWN")
    plan = state.get("plan")
    trace = list(state.get("trace") or [])
    advisor_trace = [entry for entry in trace if entry.get("node") == "advisor"]
    structure = {
        "citations_total": len(citations),
        "valid_citations": valid_citations,
        "fabricated_id_count": fabricated,
        "wrong_part_count": wrong_part,
        "quotes_total": len(citations),
        "quotes_located": quotes_located,
        "pass_with_blocking": pass_with_blocking,
    }
    duration_ms = int(state.get("duration_ms", 0) or 0)
    probe_ms = int(state.get("probe_ms", 0) or 0)
    return {
        "id": item["id"],
        "category": item.get("category", ""),
        "question": item.get("question", ""),
        "expected_outcome": item.get("expected_outcome", ""),
        "expected_article_ids": list(item.get("expected_article_ids") or []),
        "block_types": list(item.get("block_types") or ["article"]),
        "outcome": outcome,
        "decision": decision.value if decision else None,
        "partial": decision is VerificationDecision.PARTIAL,
        "reason_code": (state.get("reason_code").value if state.get("reason_code") else None),
        "answer_markdown": state.get("final_answer", ""),
        "message": "",
        "claims": [claim.model_dump(mode="json") for claim in claims],
        "citations": citations,
        "drafts": list(state.get("draft_records") or []),
        "rendered_claim_ids": [claim.claim_id for claim in claims],
        "dropped_claim_ids": list(state.get("failed_claim_ids") or []),
        "issues": issues,
        "scope_issues": [
            issue.model_dump(mode="json", exclude_none=True)
            for issue in state.get("scope_issues") or []
        ],
        "evidence": [
            {
                "article_id": entry.article.article_id,
                "part_no": entry.article.part_no,
                "score": round(entry.score, 4),
                "dense_score": entry.dense_score,
                "sparse_score": entry.sparse_score,
                "rrf_score": entry.rrf_score,
                "rerank_score": entry.rerank_score,
                "rank": entry.rank,
                "block_type": entry.article.block_type.value,
                "source": entry.source,
            }
            for entry in evidence
        ],
        "retrieval": [
            {
                "article_id": entry.article.article_id,
                "part_no": entry.article.part_no,
                "score": round(entry.score, 4),
                "rank": entry.rank,
                "source": entry.source,
            }
            for entry in evidence
        ],
        "queries": [query.text for query in plan.queries] if plan else [],
        "index_version": state.get("index_version", ""),
        "retrieval_timing": {},
        "retrieval_warnings": list(state.get("retrieval_warnings") or []),
        "mcp": {},
        "quote_repair": (
            {
                "quote_repair_attempted": advisor_trace[-1].get("quote_repair_attempted"),
                "quote_repaired": advisor_trace[-1].get("quote_repaired"),
            }
            if advisor_trace
            else {}
        ),
        "probe": state.get("probe") or {},
        "probe_ms": probe_ms,
        "qa_ms": max(0, duration_ms - probe_ms),
        "duration_ms": duration_ms,
        "llm_model": advisor_trace[-1].get("model", "") if advisor_trace else "",
        "retrieval_recall_at_8": recall,
        "structure": structure,
        "outcome_correct": outcome == item.get("expected_outcome"),
        "graph": {
            "intent": plan.intent.value if plan else "",
            "flow_id": state.get("flow_id"),
            "flow_status": state.get("flow_status"),
            "checklist_count": len(state.get("checklist") or []),
            "planner_source": plan.planner_source if plan else "",
            "revision_count": int(state.get("revision_count", 0) or 0),
            "clarify_count": int(state.get("clarify_count", 0) or 0),
            "coverage": state.get("coverage") or {},
            "semantic": state.get("semantic"),
            "semantic_unchecked": bool(state.get("semantic_unchecked")),
            "handoff": state.get("handoff") or {},
            "trace": trace,
        },
    }


def build_manifest(
    *,
    graph_version: str,
    git: dict[str, Any],
    model_profile: str,
    models: dict[str, str],
    prompt_versions: dict[str, str],
    index_version: str,
    vector_manifest_sha256: str,
    test_set_sha256: str,
    config_sha256: str,
    retrieval_mode: str,
    flow_source: str,
    started_at: str,
) -> dict[str, Any]:
    return {
        "graph_version": graph_version,
        "git_commit": git.get("git_commit", ""),
        "git_dirty": bool(git.get("git_dirty")),
        "model_profile": model_profile,
        "models": models,
        "prompts": prompt_versions,
        "index_version": index_version,
        "vector_manifest_sha256": vector_manifest_sha256,
        "test_set": "dev_v0",
        "test_set_sha256": test_set_sha256,
        "config_sha256": config_sha256,
        "retrieval_mode": retrieval_mode,
        "flow_source": flow_source,
        "started_at": started_at,
        "w7_frozen": True,
        "w7_boundary": "W6 不重跑/不修改 W7；W7 总门禁未通过的结论不变。",
    }


def build_w6_report(
    run: dict[str, Any],
    labels: list[dict[str, Any]] | None,
    *,
    support_gate: float,
    refusal_recall_gate: float,
    false_refusal_gate: float,
    manifest: dict[str, Any] | None = None,
) -> dict[str, Any]:
    report = build_report(
        run,
        labels,
        support_gate=support_gate,
        refusal_recall_gate=refusal_recall_gate,
        false_refusal_gate=false_refusal_gate,
    )
    rows = run.get("rows", [])
    report["schema_version"] = SCHEMA_VERSION
    report["manifest"] = manifest or run.get("metadata", {}).get("manifest") or {}
    report["rows_data"] = [
        {
            "id": row.get("id"),
            "outcome": row.get("outcome"),
            "decision": row.get("decision"),
            "graph": row.get("graph") or {},
        }
        for row in rows
    ]
    report["w7_boundary"] = "W6 独立于 W7；不改 W7 冻结集/标签/报告，不声称 W7 通过。"
    report["support_hard_gate"] = False

    semantic = _semantic_summary(rows)
    graph = _graph_summary(rows)
    structure = report["structure"]
    quote_mismatch = int(structure["quotes_total"] - structure["quotes_located"])
    report["semantic"] = semantic
    report["graph"] = graph
    report["quote_mismatch_rendered"] = quote_mismatch
    profile = report["manifest"].get("model_profile") or "当前模型"
    report["latency"] = {
        "p50_ms": report["summary"]["p50_latency_ms"],
        "p95_ms": report["summary"]["p95_latency_ms"],
        "note": f"{profile} 长尾为已知限制；不设 W6 延迟硬门禁，不隐藏。",
    }

    gates = report["gates"]
    gates["support"]["hard_gate"] = False
    gates["support"]["note"] = "W6 design 13.4：support 为记录项，人工标注后单列。"
    gates["semantic"] = {
        "ok": semantic["contradicted_rendered"] == 0 and semantic["unchecked_rows"] == 0,
        "contradicted_rendered": semantic["contradicted_rendered"],
        "unchecked_rows": semantic["unchecked_rows"],
        "unavailable_rows": semantic["unavailable_rows"],
    }
    gates["quote_mismatch"] = {"ok": quote_mismatch == 0, "value": quote_mismatch}
    gates["trace"] = {"ok": graph["trace_complete"], "failed_rows": graph["trace_failed_rows"]}
    allowed_profiles = {
        "vpro": {"mimo-v2.6-pro"},
        "ultraspeed": {"mimo-v2.6-pro-ultraspeed"},
    }
    profile_name = report["manifest"].get("model_profile")
    profile_models = set(report["manifest"].get("models", {}).values())
    profile_ok = bool(report["manifest"]) and (
        profile_name in allowed_profiles and profile_models == allowed_profiles[profile_name]
    )
    gates["model_profile"] = {
        "ok": profile_ok,
        "model_profile": profile_name,
        "models": report["manifest"].get("models"),
        "allowed": {name: sorted(models) for name, models in allowed_profiles.items()},
    }
    blocking: list[str] = []
    if not gates["structure"]["ok"]:
        blocking.append("结构门禁未通过")
    if not gates["refusal_recall"]["ok"]:
        blocking.append("拒答召回未达 80%")
    if not gates["false_refusal"]["ok"]:
        blocking.append("误拒率超过 20%")
    if not gates["semantic"]["ok"]:
        blocking.append("语义门禁未通过：存在 contradicted 被渲染或语义未核对")
    if not gates["quote_mismatch"]["ok"]:
        blocking.append(f"最终渲染存在 {quote_mismatch} 条 quote mismatch")
    if not gates["trace"]["ok"]:
        blocking.append("部分请求缺少 planner/retriever/verifier/finalize trace")
    if not gates["model_profile"]["ok"]:
        blocking.append(
            "model_profile/manifest 不是 "
            "vpro + mimo-v2.6-pro 或 ultraspeed + mimo-v2.6-pro-ultraspeed"
        )
    gates["w6_overall"] = all(
        gates[key]["ok"]
        for key in (
            "structure",
            "refusal_recall",
            "false_refusal",
            "semantic",
            "quote_mismatch",
            "trace",
            "model_profile",
        )
    )
    gates["blocking_reasons"] = blocking
    report["note"] = (
        "W6-B dev_v0 图级回归（20 题，非 W7 冻结集）；support 待人工标注，W7 仍为 failed/tagged。"
    )
    return report


def _semantic_summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    label_counts: Counter[str] = Counter()
    checked_rows = 0
    unavailable_rows = 0
    unchecked_rows = 0
    contradicted_rendered = 0
    latency = 0
    for row in rows:
        semantic = (row.get("graph") or {}).get("semantic") or {}
        if (row.get("graph") or {}).get("semantic_unchecked"):
            unchecked_rows += 1
        if not semantic:
            continue
        if semantic.get("unavailable"):
            unavailable_rows += 1
            continue
        labels = semantic.get("labels") or {}
        if labels:
            checked_rows += 1
        label_counts.update(labels.values())
        latency += int(semantic.get("latency_ms", 0) or 0)
        rendered = set(row.get("rendered_claim_ids") or [])
        for claim_id, label in labels.items():
            if label == "contradicted" and claim_id in rendered:
                contradicted_rendered += 1
    return {
        "checked_rows": checked_rows,
        "unavailable_rows": unavailable_rows,
        "unchecked_rows": unchecked_rows,
        "labels": dict(label_counts),
        "contradicted_rendered": contradicted_rendered,
        "latency_ms_total": latency,
        "prompt_version": "w6b-semantic-2026-10-25",
    }


def _graph_summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    planner_sources: Counter[str] = Counter()
    node_counts: Counter[str] = Counter()
    revise_total = 0
    clarify_total = 0
    flow_rows = 0
    mixed_rows = 0
    policy_rows = 0
    trace_failed = []
    for row in rows:
        graph = row.get("graph") or {}
        planner_sources[graph.get("planner_source") or "unknown"] += 1
        trace = graph.get("trace") or []
        nodes = [entry.get("node") for entry in trace]
        node_counts.update(nodes)
        revise_total += int(graph.get("revision_count", 0) or 0)
        clarify_total += int(graph.get("clarify_count", 0) or 0)
        intent = graph.get("intent")
        if graph.get("flow_id"):
            flow_rows += 1
        if intent == "mixed":
            mixed_rows += 1
        if intent == "policy_qa":
            policy_rows += 1
        required = {"planner", "finalize"}
        evidence_present = bool(row.get("evidence"))
        outcome = row.get("outcome")
        retrieval_outcomes = {
            "NO_EVIDENCE",
            "ONLY_REPEALED",
            "TOOL_UNAVAILABLE",
            "INDEX_UNAVAILABLE",
            "VERSION_CONFLICT",
        }
        if evidence_present or outcome in retrieval_outcomes:
            required.add("retriever")
        if evidence_present:
            required |= {"advisor", "verifier"}
        if graph.get("flow_id") and outcome != "CLARIFY":
            required.add("flow")
        if outcome == "CLARIFY":
            required = {"planner", "finalize"}
        if not required <= set(nodes):
            trace_failed.append(row.get("id"))
    return {
        "planner_sources": dict(planner_sources),
        "node_counts": dict(node_counts),
        "revise_total": revise_total,
        "clarify_total": clarify_total,
        "flow_rows": flow_rows,
        "mixed_rows": mixed_rows,
        "policy_rows": policy_rows,
        "trace_complete": not trace_failed,
        "trace_failed_rows": trace_failed,
    }


def render_w6_markdown(report: dict[str, Any]) -> str:
    gates = report["gates"]
    manifest = report.get("manifest") or {}
    structure = report["structure"]
    abstention = report["abstention"]
    semantic = report["semantic"]
    graph = report["graph"]
    latency = report["latency"]
    lines = [
        "# CampusGuide W6-B 图级 dev_v0 报告",
        "",
        f"- 生成时间：{report['generated_at']}",
        f"- 数据集：{report['dataset']}（20 题，非 W7 冻结集）",
        f"- run：`{report['run_sha256']}`",
        f"- graph：`{manifest.get('graph_version', '')}`",
        f"- model_profile：`{manifest.get('model_profile', '')}`",
        f"- models：`{json.dumps(manifest.get('models', {}), ensure_ascii=False)}`",
        f"- prompts：`{json.dumps(manifest.get('prompts', {}), ensure_ascii=False)}`",
        f"- index_version：`{manifest.get('index_version', '')}`",
        f"- retrieval：`{manifest.get('retrieval_mode', '')}` / flows：`{manifest.get('flow_source', '')}`",
        "",
        "> W6 是 W7 之后的独立能力；不修改 W7 冻结集/标签/报告，不声称 W7 通过。",
        "> W7 仍为 failed/tagged，W7 总门禁未通过的结论不变。",
        "",
        "## 门禁",
        "",
        "| 门禁 | 结果 | 说明 |",
        "|---|---|---|",
        f"| 结构 | {'PASS' if gates['structure']['ok'] else 'FAIL'} | "
        f"引用有效 {structure['citation_id_valid_rate']:.1%}，"
        f"quote 可定位 {structure['quote_locatable_rate']:.1%} |",
        f"| 拒答召回 | {'PASS' if gates['refusal_recall']['ok'] else 'FAIL'} | "
        f"{abstention['refusal_recall']}（阈值 {gates['refusal_recall']['gate']}） |",
        f"| 误拒 | {'PASS' if gates['false_refusal']['ok'] else 'FAIL'} | "
        f"{abstention['false_refusal_rate']}（阈值 {gates['false_refusal']['gate']}） |",
        f"| 语义 | {'PASS' if gates['semantic']['ok'] else 'FAIL'} | "
        f"contradicted 渲染 {semantic['contradicted_rendered']}；"
        f"未核对 {semantic['unchecked_rows']}；不可用 {semantic['unavailable_rows']} |",
        f"| Quote mismatch | {'PASS' if gates['quote_mismatch']['ok'] else 'FAIL'} | "
        f"{report['quote_mismatch_rendered']} 条 |",
        f"| Trace | {'PASS' if gates['trace']['ok'] else 'FAIL'} | "
        f"缺失 {len(graph['trace_failed_rows'])} 行 |",
        f"| model_profile | {'PASS' if gates['model_profile']['ok'] else 'FAIL'} | "
        f"{gates['model_profile']['models']} |",
        f"| **W6 总门禁** | **{'PASS' if gates['w6_overall'] else 'FAIL'}** | "
        f"{'；'.join(gates['blocking_reasons']) or '全部 W6 硬门禁通过'} |",
        "",
        (
            f"support（记录项，非硬门禁）：标注 {report['support']['drafts_labeled']}/"
            f"{report['support']['drafts_total']}，rendered_support="
            f"{report['support']['rendered_support_rate']}。"
            if not report["support"]["drafts_unlabeled"]
            else (
                "support（记录项，非硬门禁）："
                f"0/{report['support']['drafts_total']} 已标注，待人工标注。"
            )
        ),
        "",
        "## 语义与图",
        "",
        f"- 语义标签：{semantic['labels']}；检查行 {semantic['checked_rows']}，"
        f"语义耗时合计 {semantic['latency_ms_total']}ms。",
        f"- Planner 来源：{graph['planner_sources']}；revise {graph['revise_total']} 次；"
        f"clarify {graph['clarify_total']} 次。",
        f"- 节点覆盖：{graph['node_counts']}。",
        f"- 流程行 {graph['flow_rows']}，混合行 {graph['mixed_rows']}，政策行 {graph['policy_rows']}；"
        "dev_v0 没有办理类问题，流程/混合路径由 5 个固定场景与 W5 回归覆盖。",
        f"- 延迟：p50 {latency['p50_ms']}ms / p95 {latency['p95_ms']}ms（单列记录，不设硬门禁）。",
        "",
        "## 逐题结果",
        "",
        "| id | outcome | decision | flow | semantic | revise | clarify |",
        "|---|---|---|---|---|---|---|",
    ]
    for row in report.get("rows_data") or []:
        graph_row = row.get("graph") or {}
        semantic_row = graph_row.get("semantic") or {}
        labels = semantic_row.get("labels") or {}
        label_text = ",".join(f"{key}:{value}" for key, value in labels.items()) or "-"
        lines.append(
            f"| {row['id']} | {row['outcome']} | {row.get('decision') or '-'} | "
            f"{graph_row.get('flow_id') or '-'} | {label_text} | "
            f"{graph_row.get('revision_count', 0)} | {graph_row.get('clarify_count', 0)} |"
        )
    lines.extend(
        [
            "",
            "## 边界",
            "",
            f"- {report.get('w7_boundary', '')}",
            "- 本报告只覆盖 W6 的 20 题 dev_v0、W5 流程回归与 12 条红队；不外推为 W7 通过。",
        ]
    )
    return "\n".join(lines).strip() + "\n"


__all__ = [
    "SCHEMA_VERSION",
    "build_manifest",
    "build_w6_report",
    "render_w6_markdown",
    "row_from_graph",
]
