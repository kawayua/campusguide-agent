"""Qdrant connection policy: explicit database proxy configuration only."""

from __future__ import annotations

import os
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from qdrant_client import QdrantClient


def qdrant_trust_env() -> bool:
    """Local/internal database traffic must not inherit desktop proxy settings."""
    return os.getenv("QDRANT_TRUST_ENV", "false").strip().lower() in {"1", "true", "yes"}


def create_qdrant_client() -> QdrantClient:
    from qdrant_client import QdrantClient

    location = os.getenv("QDRANT_LOCATION", "").strip()
    if location:
        Path(location).mkdir(parents=True, exist_ok=True)
        return QdrantClient(path=location)
    return QdrantClient(
        url=os.getenv("QDRANT_URL", "http://localhost:6333"),
        api_key=os.getenv("QDRANT_API_KEY") or None,
        timeout=10,
        trust_env=qdrant_trust_env(),
    )
