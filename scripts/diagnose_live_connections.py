"""Observe only live search process TCP destinations while reproducing one UI failure."""

import concurrent.futures
import json
import subprocess
import time
from pathlib import Path

from gradio_client import Client

client = Client("http://127.0.0.1:7860/", verbose=False)
observations = []


def call():
    o = client.predict({}, "农学院院长是谁？", api_name="/on_send")
    s = o[0]
    return s["sessions"][s["current_id"]]["reply"]


with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
    job = pool.submit(call)
    start = time.monotonic()
    while not job.done() and time.monotonic() - start < 35:
        p = subprocess.run(
            ["netstat", "-ano", "-p", "tcp"], capture_output=True, text=True, timeout=5
        )
        for line in p.stdout.splitlines():
            parts = line.split()
            if len(parts) == 5 and parts[-1] == "41544":
                observations.append(
                    {
                        "elapsed_s": round(time.monotonic() - start, 3),
                        "local": parts[1],
                        "remote": parts[2],
                        "state": parts[3],
                    }
                )
        time.sleep(0.08)
    reply = job.result(timeout=10)
result = {"process_id": 41544, "outcome": reply["outcome"], "connections": observations}
Path("data/eval/corpus_random_100_live/diagnostic_live_connections.json").write_text(
    json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
)
print(json.dumps(result, ensure_ascii=False, indent=2))
