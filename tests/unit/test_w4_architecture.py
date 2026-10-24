from __future__ import annotations

import inspect
from pathlib import Path

import campusguide.qa.service as service_module


def test_qaservice_does_not_import_storage_or_models() -> None:
    source = inspect.getsource(service_module)
    forbidden = [
        "qdrant_client",
        "sentence_transformers",
        "campusguide.retrieval.embedder",
        "campusguide.retrieval.rerank",
        "campusguide.retrieval.indexer",
        "campusguide.retrieval.sparse",
    ]
    for name in forbidden:
        assert name not in source, f"QAService 不得直接依赖 {name}"


def test_search_client_contract_is_protocol_based() -> None:
    from campusguide.mcp.client import SearchClient

    assert hasattr(SearchClient, "search_articles")
    assert hasattr(SearchClient, "get_article")


def test_mcp_server_modules_exist() -> None:
    root = Path(__file__).resolve().parents[2] / "src" / "campusguide" / "mcp"
    assert (root / "policy_search_server.py").exists()
    assert (root / "policy_meta_server.py").exists()
    assert (root / "client.py").exists()
    assert (root / "schemas.py").exists()


def test_runtime_import_does_not_load_storage_or_models() -> None:
    import os
    import subprocess
    import sys

    root = Path(__file__).resolve().parents[2]
    code = (
        "import sys;"
        "import campusguide.qa.runtime;"
        "bad=[name for name in ('qdrant_client','sentence_transformers','torch') "
        "if name in sys.modules];"
        "print(','.join(bad))"
    )
    env = dict(os.environ)
    env["PYTHONPATH"] = str(root / "src") + (
        os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else ""
    )
    completed = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        check=True,
        env=env,
        cwd=root,
    )
    assert completed.stdout.strip() == ""
