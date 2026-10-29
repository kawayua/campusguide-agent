import json
import time
from pathlib import Path

import psutil

out = Path("data/eval/corpus_random_100_fixed")
rows = []
ids = set()
for _ in range(50):
    for p in psutil.process_iter(["pid", "cmdline"]):
        try:
            cmd = p.info["cmdline"] or []
            if "campusguide.mcp.policy_search_server" in cmd:
                ids.add(p.pid)
                for c in p.net_connections(kind="tcp"):
                    if c.raddr:
                        rows.append(
                            {
                                "pid": p.pid,
                                "remote_ip": c.raddr.ip,
                                "remote_port": c.raddr.port,
                                "status": c.status,
                            }
                        )
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass
    time.sleep(0.2)
result = {
    "search_pids": sorted(ids),
    "observations": rows,
    "ports": sorted({r["remote_port"] for r in rows}),
    "note": "Bounded TCP snapshot, not a packet capture; absence outside the sampling interval is not established.",
}
(out / "fixed_connection_sample.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
print({k: v for k, v in result.items() if k != "observations"})
