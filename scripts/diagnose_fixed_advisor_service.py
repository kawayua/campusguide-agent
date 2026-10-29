"""Bounded diagnostic request for the configured advisor; never exports credentials."""

from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path

from campusguide.agents.providers import ChatProviderError
from campusguide.env import load_dotenv
from campusguide.observability.errors import safe_error_summary
from campusguide.qa.runtime import _role_provider

ROOT = Path(__file__).resolve().parents[1]
load_dotenv(ROOT / ".env")
result = {
    "checked_at": datetime.now().astimezone().isoformat(),
    "role": "advisor",
    "model": os.getenv("ADVISOR_MODEL", ""),
}
try:
    provider = _role_provider("ADVISOR_MODEL", 15)
    provider._max_retries = 0
    response = provider.complete(
        [{"role": "user", "content": "仅回复OK"}], temperature=0, max_tokens=16
    )
    result.update({"ok": True, "response": response.content})
except ChatProviderError as exc:
    result.update({"ok": False, "error_type": type(exc).__name__, "error": safe_error_summary(exc)})
(ROOT / "data/eval/corpus_random_100_fixed/advisor_service_diagnostic.json").write_text(
    json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
)
print(json.dumps(result, ensure_ascii=False, indent=2))
