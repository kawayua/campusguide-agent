"""每轮 JSONL trace。

trace 是调试与评测的旁路：写入失败不应让已经生成的答案失败。
"""

from __future__ import annotations

import json
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

DEFAULT_TRACE_DIR = Path("data/traces")


class TraceWriter:
    def __init__(self, directory: str | Path = DEFAULT_TRACE_DIR) -> None:
        self._directory = Path(directory)
        self._lock = threading.Lock()

    @property
    def directory(self) -> Path:
        return self._directory

    def write(self, payload: dict[str, Any]) -> Path | None:
        timestamp = datetime.now(UTC)
        path = self._directory / f"{timestamp.date().isoformat()}.jsonl"
        record = {"written_at": timestamp.isoformat(timespec="seconds"), **payload}
        line = json.dumps(record, ensure_ascii=False, sort_keys=True)
        try:
            with self._lock:
                self._directory.mkdir(parents=True, exist_ok=True)
                with path.open("a", encoding="utf-8") as handle:
                    handle.write(line + "\n")
        except OSError:
            return None
        return path
