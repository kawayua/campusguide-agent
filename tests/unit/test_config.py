from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from campusguide.config import (
    SourcesConfigError,
    load_retrieval_config,
    load_sources,
)

DOC = {
    "doc_id": "status-rules",
    "title": "本科学生学籍管理实施细则",
    "doc_type": "regulation",
    "issuing_department": "教务处",
    "audience": ["undergraduate"],
    "versions": [
        {
            "version_id": "v2025",
            "url": "https://jwc.scau.edu.cn/example/page.htm",
            "published_at": "2025-07-15",
            "effective_from": "2025-09-01",
            "status": "active",
            "format": "html",
        }
    ],
}


def write_yaml(path: Path, payload: object) -> Path:
    path.write_text(yaml.safe_dump(payload, allow_unicode=True), encoding="utf-8")
    return path


def test_load_actual_sources_template(repo_root: Path) -> None:
    config = load_sources(repo_root / "configs" / "sources.yaml")
    assert config.school == "华南农业大学"
    assert config.documents == []
    assert config.version_count == 0


def test_load_sources_object_format(tmp_path: Path) -> None:
    path = write_yaml(tmp_path / "sources.yaml", {"documents": [DOC]})
    config = load_sources(path)
    assert config.version_count == 1
    assert config.documents[0].versions[0].format == "html"


def test_load_sources_list_format(tmp_path: Path) -> None:
    path = write_yaml(tmp_path / "sources.yaml", [DOC])
    config = load_sources(path)
    assert config.documents[0].doc_id == "status-rules"


def test_local_path_requires_citation_url(tmp_path: Path) -> None:
    bad = {
        **DOC,
        "versions": [{**DOC["versions"][0], "url": None, "local_path": "local.html"}],
    }
    path = write_yaml(tmp_path / "sources.yaml", [bad])
    with pytest.raises(SourcesConfigError):
        load_sources(path)


def test_local_path_with_url_is_allowed(tmp_path: Path) -> None:
    good = {**DOC, "versions": [{**DOC["versions"][0], "local_path": "local.html"}]}
    path = write_yaml(tmp_path / "sources.yaml", [good])
    config = load_sources(path)
    assert config.documents[0].versions[0].local_path == "local.html"


def test_duplicate_doc_id_rejected(tmp_path: Path) -> None:
    path = write_yaml(tmp_path / "sources.yaml", [DOC, DOC])
    with pytest.raises(SourcesConfigError):
        load_sources(path)


def test_retrieval_config_uses_embedding_default(repo_root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("EMBEDDING_MODEL", raising=False)
    config = load_retrieval_config(repo_root / "configs" / "retrieval.yaml")
    assert config.collection == "articles_v1"
    assert config.dense_vector_size == 1024
    assert config.embedding_model == "BAAI/bge-m3"
