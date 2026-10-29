"""Isolated reproduction of a cached stale proxy; live service and OS untouched."""

import json
import os
import time
from pathlib import Path
from unittest.mock import patch

from qdrant_client import QdrantClient

proxy_keys = [
    "http_proxy",
    "https_proxy",
    "all_proxy",
    "no_proxy",
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "ALL_PROXY",
    "NO_PROXY",
]
base_env = {k: v for k, v in os.environ.items() if k not in proxy_keys}
result = {
    "test": "client built with unavailable proxy, then environment restored before query",
    "live_app_changed": False,
    "system_proxy_changed": False,
    "checks": [],
}
with patch.dict(
    os.environ,
    {**base_env, "HTTP_PROXY": "http://127.0.0.1:1", "HTTPS_PROXY": "http://127.0.0.1:1"},
    clear=True,
):
    stale = QdrantClient(url="http://localhost:6333", timeout=2, check_compatibility=False)
    direct = QdrantClient(
        url="http://localhost:6333", timeout=2, check_compatibility=False, trust_env=False
    )
for label, client in [("cached_proxy_after_env_restored", stale), ("explicit_direct", direct)]:
    t = time.monotonic()
    row = {"mode": label}
    try:
        info = client.get_collection("articles_v1")
        row.update({"ok": True, "points": info.points_count})
    except Exception as exc:
        row.update({"ok": False, "type": type(exc).__name__, "message": str(exc)[:600]})
    finally:
        client.close()
    row["elapsed_s"] = round(time.monotonic() - t, 3)
    result["checks"].append(row)
assert result["checks"][0]["ok"] is False and result["checks"][1]["ok"] is True
p = (
    Path(__file__).resolve().parents[1]
    / "data/eval/corpus_random_100_live/diagnostic_proxy_cache_repro.json"
)
p.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
print(json.dumps(result, ensure_ascii=False, indent=2))
