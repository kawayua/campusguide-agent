"""Offline corpus/source-view audit. No model calls or index mutations.

The source-view comparison is a recall-channel diagnostic, not an answer score.
A baseline revision must be trusted repository code; evaluation text is never executed.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
import types
from collections import Counter
from pathlib import Path

from campusguide.agents.question_units import decompose_question
from campusguide.config import load_organization
from campusguide.domain.models import Article, DocumentVersion
from campusguide.ingest.build_index import _content_hash
from campusguide.retrieval.corpus_views import CorpusViews
from campusguide.retrieval.profile_expand import ProfileLexicon

ROOT = Path(__file__).resolve().parents[1]


def read_rows(path):
    return [
        json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()
    ]


def baseline_views(revision, articles, lexicon):
    code = subprocess.run(
        ["git", "show", f"{revision}:src/campusguide/retrieval/corpus_views.py"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        encoding="utf-8",
    ).stdout
    module = types.ModuleType("_rag_audit_baseline")
    sys.modules[module.__name__] = module
    exec(compile(code, "<trusted repository baseline>", "exec"), module.__dict__)
    return module.CorpusViews(articles, lexicon)


def hit_summary(articles):
    return [
        {
            "doc_id": a.doc_id,
            "version_id": a.version_id,
            "article_no": a.article_no,
            "part_no": a.part_no,
        }
        for a in articles
    ]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument(
        "--source", type=Path, default=ROOT / "data/eval/answer_quality/gold_v1.jsonl"
    )
    parser.add_argument("--baseline-revision", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    corpus = ROOT / "data/processed/articles.jsonl"
    raw = corpus.read_bytes()
    articles = [Article.model_validate_json(line) for line in raw.splitlines() if line.strip()]
    lexicon = ProfileLexicon.from_organization(
        load_organization(ROOT / "configs/organization.yaml")
    )
    before = baseline_views(args.baseline_revision, articles, lexicon)
    after = CorpusViews(articles, lexicon)
    cases = {row["id"]: row for row in read_rows(args.source)}
    records = {row["id"]: row for row in read_rows(args.run_dir / "results.jsonl")}
    comparisons = []
    for case_id, record in records.items():
        question = record["question"]
        case = cases[case_id]
        expected = sorted(
            {(s["doc_id"], s["version_id"]) for s in case.get("source_documents", [])}
        )
        result = {
            "id": case_id,
            "question": question,
            "reference_documents": expected,
            "units": [u.model_dump(mode="json") for u in decompose_question(question, lexicon)],
        }
        for label, view in [("before", before), ("after", after)]:
            hits = view.search(question).articles
            present = {(a.doc_id, a.version_id) for a in hits}
            result[label] = {
                "source_view_top16": hit_summary(hits),
                "reference_documents_present": sum(tuple(s) in present for s in expected),
                "note": "source-view channel only; not full retrieval Recall@K",
            }
        retriever = next((t for t in record.get("trace", []) if t.get("node") == "retriever"), {})
        result["recorded_initial_evidence"] = retriever.get("selected_evidence", [])
        comparisons.append(result)
    table_fragments = [
        a for a in articles if a.text.count("|") >= 4 and re.search(r"\d{6}", a.text)
    ]
    manifest = json.loads((ROOT / "data/manifest.json").read_text(encoding="utf-8"))
    vector = json.loads((ROOT / "data/vector_manifest.json").read_text(encoding="utf-8"))
    documents = [
        DocumentVersion.model_validate(row)
        for row in read_rows(ROOT / "data/processed/documents.jsonl")
    ]
    actual_content_hash = _content_hash(articles, documents)
    payload = {
        "baseline_revision": args.baseline_revision,
        "source_view_code_sha256": hashlib.sha256(
            (ROOT / "src/campusguide/retrieval/corpus_views.py").read_bytes()
        ).hexdigest(),
        "organization_sha256": hashlib.sha256(
            (ROOT / "configs/organization.yaml").read_bytes()
        ).hexdigest(),
        "corpus": {
            "articles": len(articles),
            "documents": len({a.doc_id for a in articles}),
            "versions": len({(a.doc_id, a.version_id) for a in articles}),
            "sha256": hashlib.sha256(raw).hexdigest(),
            "max_chars": max(len(a.text) for a in articles),
            "blocks": dict(Counter(a.block_type.value for a in articles)),
            "course_table_fragments": len(table_fragments),
            "table_fragments_without_local_course_header": sum(
                "课程名称" not in a.text for a in table_fragments
            ),
            "actual_content_hash": actual_content_hash,
            "actual_content_hash_matches_manifest": actual_content_hash == manifest["content_hash"],
            "manifest_source_hash_matches": manifest["content_hash"]
            == vector["source_content_hash"],
            "embedding_model": vector["embedding_model"],
            "embedding_max_seq_length": vector["max_seq_length"],
        },
        "cases": comparisons,
        "limitations": [
            "No answer accuracy inferred from source-view presence.",
            "Frozen source_documents identify reference documents, not passage relevance labels.",
            "No embedding-model comparison was run; replacement benefit is unmeasured.",
        ],
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(
        json.dumps(
            {"corpus": payload["corpus"], "cases": len(comparisons), "output": str(args.output)},
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
