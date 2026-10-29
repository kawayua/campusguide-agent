"""Sequential independent follow-on sets after a frozen core run finishes."""

from __future__ import annotations

import json
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "data/eval/answer_quality"
core = OUT / "final_20261028"
started = time.monotonic()
while True:
    progress = core / "progress.json"
    if progress.exists():
        status = json.loads(progress.read_text(encoding="utf8"))
        if status["completed"] == 100:
            break
    if time.monotonic() - started > 7200:
        raise SystemExit("Core run did not complete; follow-on stopped")
    time.sleep(5)
for dataset, name, expected in [
    ("holdout_v1.jsonl", "holdout_20261028", 20),
    ("stability_v1.jsonl", "stability_20261028", 20),
]:
    command = [
        sys.executable,
        str(ROOT / "scripts/eval_corpus_random_100_live.py"),
        "--output-dir",
        str(OUT / name),
        "--source",
        str(OUT / dataset),
    ]
    completed = subprocess.run(command, cwd=ROOT, check=False)
    progress = json.loads((OUT / name / "progress.json").read_text(encoding="utf8"))
    if completed.returncode or progress["completed"] != expected:
        raise SystemExit(f"Incomplete {name}: {progress}")
(OUT / "live_sets_complete.json").write_text(
    json.dumps(
        {
            "core": 100,
            "holdout": 20,
            "extra_stability": 20,
            "completed_at": datetime.now().astimezone().isoformat(),
        },
        indent=2,
    ),
    encoding="utf8",
)
