#!/usr/bin/env bash
# 本地与 CI 共用的质量门：ruff + 离线 pytest。
set -euo pipefail

if command -v uv >/dev/null 2>&1; then
  UV=(uv)
else
  UV=(python -m uv)
fi

"${UV[@]}" run ruff check .
"${UV[@]}" run pytest
