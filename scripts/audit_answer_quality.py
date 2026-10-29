"""Independent per-case source audit; never uses application ANSWERED as a grade.

Stores original model review plus criteria and a deterministic citation audit.
Manual corrections remain in a separate override file with reasons and run IDs.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import time
from datetime import datetime
from pathlib import Path

from campusguide.agents.providers import ChatProviderError
from campusguide.env import load_dotenv
from campusguide.observability.errors import safe_error_summary
from campusguide.qa.runtime import _role_provider
from campusguide.retrieval.sparse import tokenize

ROOT = Path(__file__).resolve().parents[1]
SYSTEM = """你是严谨的校园知识库问答验收员。依据冻结的问题、必要检查项、原始语料，独立评价实际最终答案。
不要因为系统标ANSWERED/检索命中/有引用/运行内覆盖检查通过就认为正确，也不要惩罚合法同义改写。
frozen_checks 是必答项；optional_checks 只用于说明“答了更好”，缺它不得降低判定、不得计入missing。
检查用户所问各对象×年份×方面、完整列表、适用身份、第一/第二角色、日期、定量统计、事实可追溯。
对于最近消息，只按给定知识库已收录集合的逐条发布日期判断；采集日、汇总文件日不算条目发布日期。
辨认明确不确定性与缺口：“未涉及”“无法回答”“证据不足”不等于已回答对应问题。
列表题如果问“有哪些/需要哪些”，只列少数加“等”不算完整。宽泛制度题检查核心条款，不强制抄录全部相邻原文。
不要把学分矩阵的“实现途径课程”当正式课程要求。不要把校外第一与第二导师对调。
第9题缺少实验室完整总量时，明确该口径未知属于合理拒答；第83题具体申诉操作若只有援引规定，明确缺口可合理边界回答，不能把离校5天当申诉期。
身份确实影响结果且未指定，可以正确澄清；题目已明确身份/比较对象，不能用再次询问身份逃避回答。
“两个学院”“这个办法”在无历史独立会话中缺少明确名称，应澄清。第89题只要求两项公示事项，不能额外强制公示期。
系统办理清单如果有对应原文支持，可算实际答案；无依据的条款/标题猜测不得算正确。
完整回答不得有错误的实质附加事实，即使所问主要事实答对。必要澄清、合理边界说明单独分类，不混成完整回答。
只输出JSON：{"status":"complete|partial|error|valid_abstention|valid_clarification|failed|needs_review",
"checks":[{"criterion":"检查项","passed":true,"reason":"原文/答案对照"}],
"missing":["具体缺项"],"errors":["具体错误事实或范围/日期错误"],"reason":"判断依据，100字左右",
"confidence":"high|medium|low"}。必须检查所有给定标准，不得空检查直接通过。证据不全/源表难读时标needs_review。"""


def load(path):
    return (
        [json.loads(s) for s in path.read_text(encoding="utf8").splitlines()]
        if path.exists()
        else []
    )


def citation_errors(record, articles):
    errors = []
    for citation in (record.get("reply") or {}).get("citations") or []:
        if not isinstance(citation, dict):
            errors.append("invalid citation object")
            continue
        key = (citation.get("article_id"), citation.get("part_no", 1))
        article = articles.get(key)
        if article is None:
            errors.append(f"unknown source {key}")
            continue
        start, end = citation.get("quote_start"), citation.get("quote_end")
        if (
            not isinstance(start, int)
            or not isinstance(end, int)
            or not 0 <= start < end <= len(article["text"])
        ):
            errors.append(f"bad offset {key}")
            continue
        if article["text"][start:end] != citation.get("quote"):
            errors.append(f"quote mismatch {key}")
    return errors


def source_context(case, record, articles):
    anchors = case.get("source_anchors")
    if not anchors:
        # Holdout: independently traverse corpus. Prefer literal entity terms,
        # cited documents and complete news collections, with no prewritten answer.
        terms = [t for t in set(tokenize(case["question"])) if len(t) >= 2]
        cited = {c.get("article_id") for c in (record.get("reply") or {}).get("citations") or []}
        scored = []
        for a in articles.values():
            context = (
                a["doc_title"] + " " + a["article_no"] + " " + " ".join(a.get("chapter_path") or [])
            )
            score = sum(3 * (t in context) + min(a["text"].count(t), 3) * 0.4 for t in terms)
            if a["article_id"] in cited:
                score += 12
            if score:
                scored.append((score, a))
        scored.sort(key=lambda row: row[0], reverse=True)
        anchors = [a for _, a in scored[:65]]
    # Always include sources actually cited so extraneous facts can be rejected.
    keys = {(a["article_id"], a.get("part_no", 1)) for a in anchors}
    for citation in (record.get("reply") or {}).get("citations") or []:
        key = (citation.get("article_id"), citation.get("part_no", 1))
        if key in articles and key not in keys:
            anchors = [*anchors, articles[key]]
            keys.add(key)
    text_parts = []
    used = 0
    terms = [t for t in set(tokenize(case["question"])) if len(t) >= 2]
    ranked = sorted(
        anchors,
        key=lambda a: sum(
            4 * (t in (a.get("article_no", "") + " " + a.get("doc_title", "")))
            + min(a["text"].count(t), 3) * 0.2
            for t in terms
        ),
        reverse=True,
    )
    truncated = False
    for a in ranked:
        text = json.dumps(
            {
                k: a[k]
                for k in ["article_id", "part_no", "doc_title", "article_no", "text"]
                if k in a
            },
            ensure_ascii=False,
        )
        if used + len(text) > 65000:
            truncated = True
            continue
        used += len(text)
        text_parts.append(text)
    return "\n".join(text_parts), truncated, len(anchors), len(text_parts)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input-dir", type=Path, required=True)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--follow", action="store_true")
    parser.add_argument("--expected", type=int)
    parser.add_argument("--timeout-minutes", type=int, default=120)
    args = parser.parse_args()
    out = args.input_dir.resolve()
    out.mkdir(parents=True, exist_ok=True)
    target = out / "source_review.jsonl"
    cases = {r["id"]: r for r in load(args.source)}
    gold = {r["id"]: r for r in load(ROOT / "data/eval/answer_quality/gold_v1.jsonl")}
    articles = {
        (r["article_id"], r.get("part_no", 1)): r
        for r in load(ROOT / "data/processed/articles.jsonl")
    }
    load_dotenv(ROOT / ".env")
    provider = _role_provider("VERIFIER_MODEL", 60)
    completed = {r["id"] for r in load(target)}
    deadline = time.monotonic() + args.timeout_minutes * 60
    consecutive_errors = 0
    while True:
        records = load(out / "results.jsonl")
        for record in records:
            if record["id"] in completed:
                continue
            case = cases[record["id"]]
            if "reference_case_id" in record and record["reference_case_id"] in gold:
                case = {**gold[record["reference_case_id"]], "id": record["id"]}
            context, truncated, total, shown = source_context(case, record, articles)
            payload = {
                "question": record["question"],
                "frozen_checks": case["required_checks"],
                "optional_checks": case.get("optional_checks") or [],
                "scope_and_boundary": case.get("scope_and_boundary"),
                "allowed_resolution": case.get("allowed_resolution"),
                "actual_answer": record.get("answer", ""),
                "source_context_truncated": truncated,
                "source_count": total,
                "shown_source_count": shown,
            }
            review = {
                "id": record["id"],
                "session_id": record.get("session_id"),
                "reviewed_at": datetime.now().astimezone().isoformat(),
                "source_truncated": truncated,
                "source_count": total,
                "shown_source_count": shown,
                "citation_errors": citation_errors(record, articles),
            }
            try:
                response = provider.complete(
                    [
                        {"role": "system", "content": SYSTEM},
                        {
                            "role": "user",
                            "content": json.dumps(payload, ensure_ascii=False)
                            + "\n原始语料：\n"
                            + context,
                        },
                    ],
                    temperature=0,
                    max_tokens=3000,
                )
                text = (
                    response.content.strip()
                    .removeprefix("```json")
                    .removeprefix("```")
                    .removesuffix("```")
                    .strip()
                )
                raw = json.loads(text)
                if raw.get("status") not in {
                    "complete",
                    "partial",
                    "error",
                    "valid_abstention",
                    "valid_clarification",
                    "failed",
                    "needs_review",
                } or not raw.get("checks"):
                    raise ValueError("invalid review schema/empty checks")
                review.update(raw)
                review.update(
                    model=response.model,
                    prompt_tokens=response.prompt_tokens,
                    completion_tokens=response.completion_tokens,
                    model_latency_ms=response.latency_ms,
                )
                if review["citation_errors"]:
                    review["status"] = "error"
                    review["errors"] = [*review.get("errors", []), *review["citation_errors"]]
                consecutive_errors = 0
            except (ChatProviderError, ValueError, TypeError, KeyError) as exc:
                review.update(
                    status="needs_review", reason=safe_error_summary(exc), review_service_error=True
                )
                consecutive_errors += 1
            with target.open("a", encoding="utf8") as f:
                f.write(json.dumps(review, ensure_ascii=False) + "\n")
            completed.add(record["id"])
            print(record["id"], review["status"], flush=True)
            if consecutive_errors >= 3:
                raise SystemExit("Review service stopped after three failures")
        if (
            not args.follow
            or (args.expected and len(completed) >= args.expected)
            or time.monotonic() >= deadline
        ):
            break
        time.sleep(3)
    (out / "review_metadata.json").write_text(
        json.dumps(
            {
                "review_prompt_sha256": hashlib.sha256(SYSTEM.encode()).hexdigest(),
                "testset_sha256": hashlib.sha256(args.source.read_bytes()).hexdigest(),
                "review_count": len(completed),
                "evaluation_kind": "independent source-based model-assisted audit; manual overrides separate",
                "model": provider.model,
                "updated_at": datetime.now().astimezone().isoformat(),
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf8",
    )


if __name__ == "__main__":
    main()
