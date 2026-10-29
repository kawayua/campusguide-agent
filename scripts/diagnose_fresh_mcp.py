"""Fresh isolated MCP smoke; leave live app untouched and preserve original error text."""

import json
import sys
import time
import traceback
from pathlib import Path

from campusguide.env import load_dotenv
from campusguide.mcp.client import MCPClientConfig, MCPPolicySearchClient
from campusguide.mcp.schemas import SearchArticlesRequest

ROOT = Path(__file__).resolve().parents[1]
load_dotenv(ROOT / ".env")
config = MCPClientConfig(
    command=[sys.executable, "-m", "campusguide.mcp.policy_search_server"],
    cwd=str(ROOT),
    env={"HF_HUB_OFFLINE": "1", "TRANSFORMERS_OFFLINE": "1"},
    startup_timeout_seconds=180,
    timeout_seconds=45,
    retries=0,
)
client = MCPPolicySearchClient(config)
result = {
    "started_at": __import__("datetime").datetime.now().astimezone().isoformat(),
    "live_app_changed": False,
    "requests": [],
}
try:
    t = time.monotonic()
    client.start()
    result["startup_s"] = time.monotonic() - t
    requests = [
        SearchArticlesRequest(
            query="本科生国家奖学金申请条件",
            filters={
                "block_types": ["article"],
                "audience": ["undergraduate"],
                "as_of": "2026-10-27",
            },
            top_k=10,
            rerank_top_n=5,
        ),
        SearchArticlesRequest(
            query="农学院院长是谁",
            filters={"block_types": ["article", "section"], "as_of": "2026-10-27"},
            top_k=30,
            rerank_top_n=8,
        ),
    ]
    for req in requests:
        t = time.monotonic()
        record = {"request": req.model_dump(mode="json")}
        try:
            response = client.search_articles(req)
            record.update(
                {
                    "ok": True,
                    "hits": [
                        {
                            "article_id": h.article_id,
                            "doc_id": h.article.doc_id,
                            "score": h.rerank_score,
                        }
                        for h in response.data.articles
                    ],
                    "index_version": response.data.index_version,
                }
            )
        except Exception as e:
            record.update(
                {
                    "ok": False,
                    "type": type(e).__name__,
                    "message": str(e),
                    "traceback": traceback.format_exc(),
                }
            )
        record["elapsed_s"] = time.monotonic() - t
        result["requests"].append(record)
except Exception as e:
    result["startup_error"] = {
        "type": type(e).__name__,
        "message": str(e),
        "traceback": traceback.format_exc(),
    }
finally:
    client.close()
    (ROOT / "data/eval/corpus_random_100_live/diagnostic_fresh_mcp.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
