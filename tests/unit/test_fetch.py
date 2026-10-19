from __future__ import annotations

import hashlib
import json
from pathlib import Path

import httpx
import pytest

from campusguide.config import SourceDocument, SourcesConfig, SourceVersion
from campusguide.ingest.fetch import (
    FetchError,
    detect_format,
    fetch_corpus,
    fetch_one,
)

HTML_BYTES = "<html><body><h1>制度</h1><p>第一条 测试。</p></body></html>".encode()


def make_source(**version_overrides: object) -> tuple[SourceDocument, SourceVersion]:
    version_data = {
        "version_id": "v2025",
        "url": "https://example.edu.cn/policy/page.htm",
        "published_at": "2025-07-15",
        "effective_from": "2025-09-01",
        "status": "active",
        "format": "auto",
    }
    version_data.update(version_overrides)
    document = SourceDocument.model_validate(
        {
            "doc_id": "status-rules",
            "title": "本科学生学籍管理实施细则",
            "doc_type": "regulation",
            "issuing_department": "教务处",
            "audience": ["undergraduate"],
            "versions": [version_data],
        }
    )
    return document, document.versions[0]


def test_detect_format_from_url_and_content_type() -> None:
    assert detect_format("auto", url="https://x/a.pdf") == "pdf"
    assert detect_format("auto", url="https://x/a.psp") == "html"
    assert detect_format("auto", content_type="application/pdf") == "pdf"
    with pytest.raises(FetchError):
        detect_format("auto", url="https://x/download")


def test_fetch_url_writes_snapshot_and_meta(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=HTML_BYTES,
            headers={"content-type": "text/html; charset=utf-8"},
            request=request,
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    document, version = make_source()
    result = fetch_one(
        document,
        version,
        raw_dir=tmp_path / "raw",
        config_dir=tmp_path,
        client=client,
        timeout=5,
    )

    assert result.status == "fetched"
    assert result.source_path is not None and result.source_path.exists()
    assert result.sha256 == hashlib.sha256(HTML_BYTES).hexdigest()
    meta = json.loads((tmp_path / "raw" / "status-rules" / "v2025" / "meta.json").read_text("utf-8"))
    assert meta["http_status"] == 200
    assert meta["format"] == "html"
    assert meta["source_file"] == "source.html"
    assert meta["source_url"] == "https://example.edu.cn/policy/page.htm"


def test_fetch_http_error_returns_error_result(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, request=request)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    document, version = make_source()
    result = fetch_one(
        document,
        version,
        raw_dir=tmp_path / "raw",
        config_dir=tmp_path,
        client=client,
    )
    assert result.status == "error"
    assert "404" in (result.error or "")


def test_fetch_local_file_and_skip_existing(tmp_path: Path) -> None:
    source = tmp_path / "local.html"
    source.write_text(HTML_BYTES.decode(), encoding="utf-8")
    document, version = make_source(
        url=None,
        local_path="local.html",
        format="html",
    )
    client = httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(500)))
    raw_dir = tmp_path / "raw"

    first = fetch_one(
        document,
        version,
        raw_dir=raw_dir,
        config_dir=tmp_path,
        client=client,
        local_base_dir=tmp_path,
    )
    assert first.status == "copied"

    second = fetch_one(
        document,
        version,
        raw_dir=raw_dir,
        config_dir=tmp_path,
        client=client,
        local_base_dir=tmp_path,
        skip_existing=True,
    )
    assert second.status == "skipped"


def test_fetch_corpus_respects_only_filter(tmp_path: Path) -> None:
    document, _ = make_source()
    other = SourceDocument.model_validate(
        {
            "doc_id": "award-rules",
            "title": "奖学金评审办法",
            "issuing_department": "学生工作部",
            "versions": [
                {
                    "version_id": "v2025",
                    "url": "https://example.edu.cn/award.htm",
                    "published_at": "2025-07-15",
                    "effective_from": "2025-09-01",
                    "status": "active",
                }
            ],
        }
    )
    config = SourcesConfig(documents=[document, other])

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=HTML_BYTES,
            headers={"content-type": "text/html"},
            request=request,
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    results = fetch_corpus(
        config,
        raw_dir=tmp_path / "raw",
        config_dir=tmp_path,
        client=client,
        only=["award-rules"],
        delay_seconds=0,
    )
    assert [result.doc_id for result in results] == ["award-rules"]
