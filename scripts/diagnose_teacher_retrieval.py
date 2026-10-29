import json
import sys
from pathlib import Path

from campusguide.env import load_dotenv
from campusguide.mcp.client import MCPClientConfig, MCPPolicySearchClient
from campusguide.mcp.schemas import SearchArticlesRequest

root = Path.cwd()
load_dotenv(root / ".env")
c = MCPPolicySearchClient(
    MCPClientConfig(
        command=[sys.executable, "-m", "campusguide.mcp.policy_search_server"],
        cwd=str(root),
        env={"HF_HUB_OFFLINE": "1", "TRANSFORMERS_OFFLINE": "1"},
        startup_timeout_seconds=180,
        timeout_seconds=60,
        retries=0,
    )
)
rows = []
try:
    c.start()
    for q, docs in [
        ("梁云是数学与信息学院 属于哪个系", None),
        ("梁 云 计算机科学与工程系教师", None),
        ("梁云是数学与信息学院 属于哪个系", ["info-teachers-cs"]),
        ("梁 云 属于哪个系", ["info-teachers-cs"]),
    ]:
        r = c.search_articles(
            SearchArticlesRequest(
                query=q,
                filters={
                    "as_of": "2026-10-25",
                    "block_types": ["article", "section"],
                    "doc_ids": docs,
                },
                top_k=50,
                rerank_top_n=8,
            )
        )
        rows.append(
            {
                "query": q,
                "docs": docs,
                "response": {"data": r.data.model_dump(mode="json"), "warnings": r.data.warnings},
            }
        )
finally:
    c.close()
    (root / "data/eval/corpus_random_100_fixed/teacher_retrieval_diagnostic.json").write_text(
        json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    for row in rows:
        print(
            row["query"],
            [
                (h["article"]["doc_id"], h["rerank_score"])
                for h in row["response"]["data"]["articles"]
            ],
            row["response"]["warnings"],
        )
