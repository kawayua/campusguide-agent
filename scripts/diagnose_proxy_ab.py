"""Read-only A/B test of Qdrant proxy inheritance; no live configuration change."""

import json
import time
from pathlib import Path
from urllib.parse import urlsplit
from urllib.request import getproxies

from qdrant_client import QdrantClient

result = {"system_proxies": {}, "checks": []}
for name, value in getproxies().items():
    if name == "no":
        result["proxy_bypass_contains_localhost"] = "localhost" in value
    else:
        u = urlsplit(value if "://" in value else "http://" + value)
        result["system_proxies"][name] = {"host": u.hostname, "port": u.port}
for label, kwargs in [("default", {}), ("explicit_direct", {"trust_env": False})]:
    client = None
    t = time.monotonic()
    row = {"mode": label}
    try:
        client = QdrantClient(
            url="http://localhost:6333", timeout=4, check_compatibility=False, **kwargs
        )
        info = client.get_collection("articles_v1")
        row.update({"ok": True, "points": info.points_count, "status": str(info.status)})
    except Exception as exc:
        row.update({"ok": False, "type": type(exc).__name__, "message": str(exc)[:700]})
    finally:
        if client is not None:
            client.close()
    row["elapsed_s"] = round(time.monotonic() - t, 3)
    result["checks"].append(row)
out = (
    Path(__file__).resolve().parents[1]
    / "data/eval/corpus_random_100_live/diagnostic_proxy_ab.json"
)
out.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
print(json.dumps(result, ensure_ascii=False, indent=2))
