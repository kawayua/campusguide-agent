"""Controlled restart of the verified local CampusGuide listener; no unrelated processes."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

import psutil

ROOT = Path(__file__).resolve().parents[1]
parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--output-dir", type=Path, default=ROOT / "data/eval/corpus_random_100_fixed")
OUT = parser.parse_args().output_dir.resolve()
OUT.mkdir(parents=True, exist_ok=True)
listeners = {
    c.pid
    for c in psutil.net_connections(kind="tcp")
    if c.status == psutil.CONN_LISTEN and c.laddr.port == 7860
}
assert len(listeners) <= 1, f"Unexpected multiple listeners: {listeners}"
stopped = []
for pid in listeners:
    p = psutil.Process(pid)
    cmd = p.cmdline()
    assert (
        "campusguide.ui.app" in cmd and Path(cmd[0]).resolve() == Path(sys.executable).resolve()
    ), "Port 7860 is not this workspace app"
    parent = p.parent()
    if parent and parent.cmdline() == cmd:
        p = parent
    children = p.children(recursive=True)
    for target in [p, *children]:
        try:
            target.terminate()
            stopped.append(target.pid)
        except psutil.NoSuchProcess:
            pass
    _, alive = psutil.wait_procs([p, *children], timeout=8)
    for target in alive:
        target.kill()
with (OUT / "app_stdout.log").open("ab") as stdout, (OUT / "app_stderr.log").open("ab") as stderr:
    env = dict(os.environ)
    env.update(
        {
            "GRADIO_SERVER_NAME": "127.0.0.1",
            "GRADIO_SERVER_PORT": "7860",
            "PYTHONIOENCODING": "utf-8",
            "PYTHONUTF8": "1",
            "HF_HUB_OFFLINE": "1",
            "TRANSFORMERS_OFFLINE": "1",
            "QDRANT_TRUST_ENV": "false",
            "DEMO_AS_OF": "2026-10-25",
        }
    )
    proc = subprocess.Popen(
        [sys.executable, "-m", "campusguide.ui.app"],
        cwd=ROOT,
        stdin=subprocess.DEVNULL,
        stdout=stdout,
        stderr=stderr,
        env=env,
        creationflags=subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP,
    )
    result = {
        "stopped_pids": stopped,
        "started_pid": proc.pid,
        "url": "http://127.0.0.1:7860/",
        "as_of": "2026-10-25",
        "as_of_note": "Preserves the previous GraphBackend demo default for comparable regression.",
        "qdrant_trust_env": False,
        "started_at": __import__("datetime").datetime.now().astimezone().isoformat(),
    }
    (OUT / "service_restart.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
