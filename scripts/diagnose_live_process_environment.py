"""Read only allowlisted operational environment of live CampusGuide processes."""

import json
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import psutil

ROOT = Path(__file__).resolve().parents[1]
rows = []
for p in psutil.process_iter(["pid", "name"]):
    if p.info["name"] != "python.exe":
        continue
    try:
        cmd = p.cmdline()
        if not any(
            x in {"campusguide.ui.app", "campusguide.mcp.policy_search_server"} for x in cmd
        ):
            continue
        env = p.environ()
        allowed = [
            "QDRANT_URL",
            "QDRANT_LOCATION",
            "CAMPUSGUIDE_ROOT",
            "EMBEDDING_DEVICE",
            "RERANKER_DEVICE",
            "HF_HUB_OFFLINE",
            "TRANSFORMERS_OFFLINE",
            "HF_HOME",
            "HUGGINGFACE_HUB_CACHE",
            "PYTHONPATH",
        ]
        selected = {k: env[k] for k in allowed if k in env}
        if "QDRANT_URL" in selected:
            u = urlsplit(selected["QDRANT_URL"])
            selected["QDRANT_URL"] = urlunsplit((u.scheme, u.netloc.split("@")[-1], u.path, "", ""))
        rows.append(
            {
                "pid": p.pid,
                "ppid": p.ppid(),
                "cmd": cmd,
                "create_time": p.create_time(),
                "operational_environment": selected,
                "proxy_presence": {
                    k: bool(env.get(k))
                    for k in ["HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY"]
                },
            }
        )
    except (psutil.AccessDenied, psutil.NoSuchProcess) as e:
        rows.append({"pid": p.pid, "error": type(e).__name__})
p = ROOT / "data/eval/corpus_random_100_live/diagnostic_process_environment.json"
p.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
print(json.dumps(rows, ensure_ascii=False, indent=2))
