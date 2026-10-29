"""Read-only diagnostic of retrieval service with local model cache only."""

import json
import time
import traceback
from pathlib import Path

out = Path("data/eval/corpus_random_100_live/diagnostic_backend.json")
result = {
    "started_at": __import__("datetime").datetime.now().astimezone().isoformat(),
    "offline_model_loading": True,
}
try:
    from campusguide.mcp.policy_search_server import create_service
    from campusguide.mcp.schemas import SearchArticlesRequest

    start = time.monotonic()
    service = create_service()
    result["construct_elapsed_s"] = time.monotonic() - start
    result["backend_type"] = type(service._backend).__name__
    try:
        service.warmup()
        result["warmup"] = "ok"
    except Exception as e:
        result["warmup_error"] = {
            "type": type(e).__name__,
            "message": str(e),
            "traceback": traceback.format_exc(),
        }
    try:
        response = service.search_articles(SearchArticlesRequest(query="农学院院长是谁？"))
        result["search_response"] = response
    except Exception as e:
        result["search_error"] = {
            "type": type(e).__name__,
            "message": str(e),
            "traceback": traceback.format_exc(),
        }
except Exception as e:
    result["construction_error"] = {
        "type": type(e).__name__,
        "message": str(e),
        "traceback": traceback.format_exc(),
    }
out.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
print(json.dumps(result, ensure_ascii=False, indent=2)[:15000])
