"""Bounded Advisor diagnosis, using the authorized local corpus/model configuration."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from campusguide.agents.advisor import Advisor
from campusguide.agents.question_units import decompose_question
from campusguide.config import load_organization
from campusguide.domain.models import Evidence
from campusguide.env import load_dotenv
from campusguide.qa.runtime import _role_provider
from campusguide.retrieval.corpus_views import CorpusViews
from campusguide.retrieval.profile_expand import ProfileLexicon
from campusguide.verification.claims import ClaimValidator

ROOT = Path(__file__).resolve().parents[1]
parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--id", default="corpus_random_100_017")
args = parser.parse_args()
load_dotenv(ROOT / ".env")
lex = ProfileLexicon.from_organization(load_organization(ROOT / "configs/organization.yaml"))
view = CorpusViews.load(ROOT / "data/processed/articles.jsonl", lex)
cases = [
    json.loads(s)
    for s in (ROOT / "data/eval/corpus_random_100_questions.jsonl")
    .read_text(encoding="utf8")
    .splitlines()
]
question = next(c["question"] for c in cases if c["id"] == args.id)
rows = view.search(question).articles[:8]
evidence = [Evidence(article=a, rank=i) for i, a in enumerate(rows, 1)]
result = Advisor(_role_provider("ADVISOR_MODEL", 40)).generate(
    question, evidence, question_units=decompose_question(question, lex)
)
check = ClaimValidator(max_claims=12).check(result.drafts, evidence)
out = ROOT / f"data/eval/answer_quality/advisor_probe_{args.id[-3:]}.json"
out.write_text(
    json.dumps(
        {
            "drafts": [d.model_dump() for d in result.drafts],
            "records": check.records,
            "issues": [i.model_dump(mode="json") for i in check.issues],
            "responses": [
                {
                    "content": r.content,
                    "prompt_tokens": r.prompt_tokens,
                    "completion_tokens": r.completion_tokens,
                }
                for r in result.responses
            ],
        },
        ensure_ascii=False,
        indent=2,
        default=str,
    ),
    encoding="utf8",
)
print(
    json.dumps(
        {
            "output": str(out),
            "claims": len(check.claims),
            "issues": [i.model_dump(mode="json") for i in check.issues],
        },
        ensure_ascii=False,
    )
)
