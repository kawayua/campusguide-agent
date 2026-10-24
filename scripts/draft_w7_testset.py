"""W7 50 题 draft 生成器（AI 辅助；必须人工复核后才能冻结）。

只生成 draft：annotator=ai-draft、reviewed_at 为空，freeze_testset.py 会拒绝；
人工核对 gold question / gold answer / required_facts / gold IDs 后再填写。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import defaultdict
from datetime import date
from pathlib import Path
from typing import Any

from campusguide.agents.providers import OpenAICompatibleChat
from campusguide.env import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
ARTICLES = ROOT / "data" / "processed" / "articles.jsonl"
DEV = ROOT / "data" / "eval" / "dev_v0.jsonl"
POPULATION = ROOT / "data" / "eval" / "population_v1.jsonl"
OUTPUT = ROOT / "data" / "eval" / "test_v1.draft.jsonl"
REVIEW = ROOT / "data" / "eval" / "test_v1_review.md"

SYSTEM = """你是高校制度问答评测集的出题人。只依据给定片段出题，不使用外部知识。
对每个 source 生成一道自然中文问题：
- 问题不能出现文档标题、条款号、章节号，也不能说“根据上述/以下片段”；
- 问题必须能仅凭该片段回答；
- gold_answer 用 1-3 句话给出片段能支持的标准答案，不得补充片段外事实；
- required_facts 给 1-3 个片段中必须出现的关键事实（短语）；
- 多片段 source 的问题必须同时涉及两个片段的核心内容。
只输出 JSON：{"items":[{"key":"S1","question":"...","gold_answer":"...","required_facts":["..."]}]}。
"""


def _load_jsonl(path: Path) -> list[dict[str, Any]]:
    return [
        json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()
    ]


def _doc_ids_from_questions(rows: list[dict[str, Any]]) -> set[str]:
    ids: set[str] = set()
    for row in rows:
        for article_id in row.get("expected_article_ids") or []:
            ids.add(str(article_id).split("@", 1)[0])
        for article_id in row.get("expected_doc_ids") or []:
            ids.add(str(article_id))
        for article_id in row.get("forbidden_doc_ids") or []:
            ids.add(str(article_id))
    return ids


def _evenly(items: list[Any], count: int) -> list[Any]:
    if len(items) <= count:
        return items
    step = len(items) / count
    return [items[min(int(index * step), len(items) - 1)] for index in range(count)]


def _select_sources() -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    articles = _load_jsonl(ARTICLES)
    dev = _load_jsonl(DEV)
    pop = _load_jsonl(POPULATION)
    excluded = _doc_ids_from_questions(dev) | _doc_ids_from_questions(pop)

    by_doc: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in articles:
        by_doc[row["doc_id"]].append(row)
    for rows in by_doc.values():
        rows.sort(key=lambda item: (item["article_index"], item["part_no"]))

    def suitable(article: dict[str, Any], *, section: bool = False) -> bool:
        if article["block_type"] != ("section" if section else "article"):
            return False
        if article["status"] != "active":
            return False
        text = article["text"].strip()
        if section:
            return 100 <= len(text) <= 900 and any(
                marker in article["article_no"]
                for marker in ("培养目标", "培养要求", "专业核心课程", "毕业要求")
            )
        return 120 <= len(text) <= 520

    candidate_docs = [doc_id for doc_id in sorted(by_doc) if doc_id not in excluded]
    section_docs = [
        doc_id
        for doc_id in candidate_docs
        if any(suitable(row, section=True) for row in by_doc[doc_id])
    ]
    chosen_sections = _evenly(section_docs, 5)

    remaining = [doc_id for doc_id in candidate_docs if doc_id not in chosen_sections]
    multi_docs = [
        doc_id for doc_id in remaining if sum(1 for row in by_doc[doc_id] if suitable(row)) >= 2
    ]
    chosen_multi = _evenly(multi_docs, 10)

    remaining = [doc_id for doc_id in remaining if doc_id not in chosen_multi]
    single_docs = [doc_id for doc_id in remaining if any(suitable(row) for row in by_doc[doc_id])]
    chosen_singles = _evenly(single_docs, 25)

    if len(chosen_singles) < 25 or len(chosen_multi) < 10 or len(chosen_sections) < 5:
        raise RuntimeError(
            f"source 不足：single={len(chosen_singles)}, multi={len(chosen_multi)}, "
            f"section={len(chosen_sections)}"
        )

    singles = [next(row for row in by_doc[doc_id] if suitable(row)) for doc_id in chosen_singles]
    multi = [[row for row in by_doc[doc_id] if suitable(row)][:2] for doc_id in chosen_multi]
    sections = [
        next(row for row in by_doc[doc_id] if suitable(row, section=True))
        for doc_id in chosen_sections
    ]
    return singles[:25], multi[:10], sections[:5]


def _source_payload(
    key: str,
    kind: str,
    rows: list[dict[str, Any]],
) -> dict[str, Any]:
    return {
        "key": key,
        "kind": kind,
        "texts": [str(row["text"])[:900] for row in rows],
    }


def _call_batch(
    provider: OpenAICompatibleChat,
    sources: list[dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    user = "请为以下 source 各生成一道题。只输出约定 JSON。\n" + json.dumps(
        {"sources": sources}, ensure_ascii=False
    )
    messages = [
        {"role": "system", "content": SYSTEM},
        {"role": "user", "content": user},
    ]
    for _ in range(2):
        response = provider.complete(messages, temperature=0.2, max_tokens=3000)
        text = response.content.strip()
        fenced = re.match(r"^```(?:json)?\s*(.*?)\s*```$", text, re.DOTALL)
        if fenced:
            text = fenced.group(1).strip()
        try:
            payload = json.loads(text)
            items = payload.get("items") if isinstance(payload, dict) else None
            if isinstance(items, list):
                mapping = {
                    str(item.get("key")): item
                    for item in items
                    if isinstance(item, dict) and item.get("key")
                }
                if mapping:
                    return mapping
        except json.JSONDecodeError:
            pass
        messages.append({"role": "assistant", "content": response.content})
        messages.append(
            {"role": "user", "content": '上一次不是约定 JSON。请只输出 {"items":[...]}，不要解释。'}
        )
    raise RuntimeError("出题模型连续两次未返回约定 JSON")


def _generate(
    provider: OpenAICompatibleChat,
    *,
    singles: list[dict[str, Any]],
    multi: list[list[dict[str, Any]]],
    sections: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[str]]:
    sources: list[dict[str, Any]] = []
    meta: dict[str, tuple[str, list[dict[str, Any]]]] = {}
    for index, article in enumerate(singles, start=1):
        key = f"S{index:02d}"
        sources.append(_source_payload(key, "single", [article]))
        meta[key] = ("single", [article])
    for index, pair in enumerate(multi, start=1):
        key = f"M{index:02d}"
        sources.append(_source_payload(key, "multi", pair))
        meta[key] = ("multi", pair)
    for index, article in enumerate(sections, start=1):
        key = f"X{index:02d}"
        sources.append(_source_payload(key, "section", [article]))
        meta[key] = ("section", [article])

    generated: dict[str, dict[str, Any]] = {}
    for start in range(0, len(sources), 5):
        batch = sources[start : start + 5]
        print(f"draft batch {start // 5 + 1}/{(len(sources) + 4) // 5}", flush=True)
        generated.update(_call_batch(provider, batch))

    rows: list[dict[str, Any]] = []
    missing: list[str] = []
    for key, (kind, articles) in meta.items():
        item = generated.get(key)
        if not item or not item.get("question") or not item.get("gold_answer"):
            missing.append(key)
            continue
        category = {"single": "single_article", "multi": "multi_article", "section": "section"}[
            kind
        ]
        question = str(item["question"]).strip()
        profile: dict[str, Any] = {}
        block_types = ["article"]
        version_sensitive = False
        if kind == "section":
            block_types = ["section"]
            detail = articles[0].get("audience_detail") or {}
            intake_years = detail.get("intake_years") or []
            year = int(intake_years[0]) if intake_years else 2024
            audiences = articles[0].get("audience") or ["undergraduate"]
            version_sensitive = True
            profile = {
                "audience": audiences[0],
                "colleges": detail.get("colleges") or [],
                "majors": detail.get("majors") or [],
                "intake_year": year,
            }
            if str(year) not in question:
                question = f"{year} 版培养方案：{question}"
        rows.append(
            {
                "qid": "",
                "split": "test",
                "category": category,
                "question": question,
                "user_profile": profile,
                "block_types": block_types,
                "as_of": "2026-10-24",
                "gold_article_ids": [article["article_id"] for article in articles],
                "gold_quote_spans": [],
                "gold_answer": str(item["gold_answer"]).strip(),
                "required_facts": [
                    str(term).strip()
                    for term in (item.get("required_facts") or [])
                    if str(term).strip()
                ],
                "should_abstain": False,
                "expected_outcome": "ANSWERED",
                "version_sensitive": version_sensitive,
                "notes": "AI 辅助 draft；人工核对 question/gold_answer/gold IDs/required_facts 后再冻结",
                "annotator": "ai-draft",
                "reviewed_at": "",
            }
        )
    return rows, missing


def _unanswerable_rows() -> list[dict[str, Any]]:
    rows = [
        ("out_of_scope", "外校的助学贷款申请条件是什么？", "OUT_OF_SCOPE"),
        ("out_of_scope", "中山大学的转专业规定是什么？", "OUT_OF_SCOPE"),
        ("out_of_scope", "其他学校的推免名额怎么分配？", "OUT_OF_SCOPE"),
        ("out_of_scope", "华南理工大学对处分有什么规定？", "OUT_OF_SCOPE"),
        ("repealed", "2019 年版本的奖学金评审办法有哪些规定？", "HISTORICAL_NOT_SUPPORTED"),
        ("repealed", "前年的转专业规定是什么？", "HISTORICAL_NOT_SUPPORTED"),
        ("repealed", "2015 年版的学籍管理规定还能用吗？", "HISTORICAL_NOT_SUPPORTED"),
        ("privacy", "查询我的绩点和排名", "PRIVACY_REQUEST"),
        ("privacy", "我的绩点和身份证号是多少？", "PRIVACY_REQUEST"),
        ("privacy", "查询我的宿舍和一卡通信息", "PRIVACY_REQUEST"),
    ]
    result = []
    for index, (category, question, outcome) in enumerate(rows, start=1):
        result.append(
            {
                "qid": f"test-a{index:02d}",
                "split": "test",
                "category": category,
                "question": question,
                "user_profile": {},
                "block_types": ["article"],
                "as_of": "2026-10-24",
                "gold_article_ids": [],
                "gold_quote_spans": [],
                "gold_answer": "",
                "required_facts": [],
                "should_abstain": True,
                "expected_outcome": outcome,
                "version_sensitive": False,
                "notes": "人工编写的不可答题 draft",
                "annotator": "ai-draft",
                "reviewed_at": "",
            }
        )
    return result


def _write_review(rows: list[dict[str, Any]], path: Path) -> None:
    lines = [
        "# W7 test_v1 draft 核对表",
        "",
        "> AI draft，未冻结；请核对问题、gold answer、gold IDs、required facts，并填写 annotator/reviewed_at。",
        "",
    ]
    for row in rows:
        lines.extend(
            [
                f"## {row['qid']} · {row['category']}",
                "",
                f"- 问题：{row['question']}",
                f"- gold IDs：{', '.join(row['gold_article_ids']) or '—'}",
                f"- gold answer：{row['gold_answer'] or '—'}",
                f"- required facts：{'、'.join(row['required_facts']) or '—'}",
                f"- abstain：{row['should_abstain']} / expected：{row['expected_outcome']}",
                f"- version_sensitive：{row['version_sensitive']}",
                "",
            ]
        )
    path.write_text("\n".join(lines), encoding="utf-8")


def main() -> int:
    load_dotenv(ROOT / ".env")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", default=str(OUTPUT))
    parser.add_argument("--review", default=str(REVIEW))
    args = parser.parse_args()

    singles, multi, sections = _select_sources()
    provider = OpenAICompatibleChat.from_env()
    generated, missing = _generate(provider, singles=singles, multi=multi, sections=sections)
    if missing:
        print(f"warning: 模型未返回 {len(missing)} 个 source：{missing}", file=sys.stderr)
    rows = generated + _unanswerable_rows()
    for index, row in enumerate(rows, start=1):
        row["qid"] = f"test-{index:03d}"
    rows.sort(key=lambda row: row["qid"])

    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
        encoding="utf-8",
    )
    _write_review(rows, Path(args.review))
    print(f"draft: {output} ({len(rows)} questions)")
    print(f"review: {args.review}")
    print(f"missing sources: {missing}")
    print(f"date: {date.today().isoformat()}")
    return 0 if len(rows) == 50 and not missing else 1


if __name__ == "__main__":
    sys.exit(main())
