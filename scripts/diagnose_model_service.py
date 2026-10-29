"""One bounded provider probe without corpus; stores no credentials."""

from __future__ import annotations

import argparse
import json
import os
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from campusguide.env import load_dotenv

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--unique-context", action="store_true")
    args = parser.parse_args()
    load_dotenv(ROOT / ".env")
    role = "ADVISOR"
    base = (os.getenv(f"{role}_LLM_BASE_URL") or os.getenv("LLM_BASE_URL", "")).rstrip("/")
    key = os.getenv(f"{role}_LLM_API_KEY") or os.getenv("LLM_API_KEY", "")
    model = os.getenv(f"{role}_MODEL", "")
    url = urlsplit(base)
    result = {
        "checked_at": datetime.now().astimezone().isoformat(),
        "endpoint": f"{url.scheme}://{url.netloc}{url.path}",
        "model": model,
        "requests": 1,
        "retries": 0,
        "contains_local_corpus": False,
        "prompt": "Reply exactly OK.",
        "max_tokens": 32,
    }
    messages = [{"role": "user", "content": result["prompt"]}]
    if args.unique_context:
        messages.insert(
            0,
            {
                "role": "system",
                "content": "Diagnostic request at "
                + result["checked_at"]
                + ". Reply to the user briefly.",
            },
        )
    result["unique_context"] = args.unique_context
    result["messages"] = messages
    try:
        with httpx.Client(timeout=30) as client:
            response = client.post(
                base + "/chat/completions",
                headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                json={"model": model, "messages": messages, "temperature": 0, "max_tokens": 32},
            )
        result["status_code"] = response.status_code
        result["headers"] = {
            k: v
            for k, v in response.headers.items()
            if k.lower() in {"date", "retry-after", "x-request-id", "request-id", "cf-ray"}
            or k.lower().startswith(("x-ratelimit-", "ratelimit-"))
        }
        result["response_body"] = (
            response.text[:2500].replace(key, "[REDACTED]") if key else response.text[:2500]
        )
    except httpx.HTTPError as exc:
        result["error_type"] = type(exc).__name__
        result["error"] = str(exc).replace(key, "[REDACTED]") if key else str(exc)
    out = ROOT / "data/eval/answer_quality/provider_diagnostics"
    out.mkdir(parents=True, exist_ok=True)
    target = out / (datetime.now().strftime("probe_%Y%m%d_%H%M%S") + ".json")
    target.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"saved_to": str(target), **result}, ensure_ascii=True, indent=2))


if __name__ == "__main__":
    main()
