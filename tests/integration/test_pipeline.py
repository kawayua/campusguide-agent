from __future__ import annotations

import json
import shutil
from pathlib import Path

import httpx
import yaml

from campusguide.config import load_sources
from campusguide.ingest.build_index import build_index
from campusguide.ingest.fetch import fetch_corpus
from campusguide.ingest.parse import parse_corpus

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"


def prepare_corpus(tmp_path: Path) -> tuple[Path, Path, Path, Path]:
    config_dir = tmp_path / "configs"
    config_dir.mkdir()
    shutil.copy(FIXTURES / "html" / "sample_policy.html", config_dir / "sample_policy.html")
    sources_path = config_dir / "sources.yaml"
    sources_path.write_text(
        yaml.safe_dump(
            {
                "schema_version": "1.0",
                "corpus_id": "scau",
                "school": "华南农业大学",
                "documents": [
                    {
                        "doc_id": "undergraduate-status-rules",
                        "title": "华南农业大学本科学生学籍管理实施细则",
                        "doc_type": "regulation",
                        "issuing_department": "教务处",
                        "audience": ["undergraduate"],
                        "topic_tags": ["学籍", "转专业"],
                        "versions": [
                            {
                                "version_id": "v2021",
                                "url": "https://jwc.scau.edu.cn/example.htm",
                                "local_path": "sample_policy.html",
                                "published_at": "2021-08-30",
                                "effective_from": "2021-09-01",
                                "status": "active",
                                "format": "html",
                            }
                        ],
                    }
                ],
            },
            allow_unicode=True,
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    return (
        sources_path,
        tmp_path / "data" / "raw",
        tmp_path / "data" / "processed",
        config_dir,
    )


def test_local_html_from_fetch_to_index(tmp_path: Path) -> None:
    sources_path, raw_dir, processed_dir, config_dir = prepare_corpus(tmp_path)
    config = load_sources(sources_path)
    client = httpx.Client(
        transport=httpx.MockTransport(lambda request: httpx.Response(500, request=request))
    )
    try:
        fetched = fetch_corpus(
            config,
            raw_dir=raw_dir,
            config_dir=config_dir,
            client=client,
            delay_seconds=0,
        )
    finally:
        client.close()

    assert [result.status for result in fetched] == ["copied"]
    assert fetched[0].sha256

    parsed = parse_corpus(raw_dir=raw_dir, processed_dir=processed_dir)
    assert [result.status for result in parsed] == ["parsed"]
    assert parsed[0].cjk_count > 100

    result = build_index(
        sources_path=sources_path,
        parsed_dir=processed_dir / "parsed",
        output_dir=processed_dir,
    )
    # 3 条正文 + 2 行数据表可检索；父表块落盘供水合但不检索；前言仍算排除块。
    assert result.articles_written == 5
    assert result.stored_articles_written == 6
    assert result.manifest["counts"]["documents"] == 1
    assert result.manifest["counts"]["indexable_articles"] == 5
    assert result.manifest["counts"]["stored_articles"] == 6
    assert result.manifest["counts"]["blocks_excluded"] >= 1

    articles = [
        json.loads(line)
        for line in (processed_dir / "articles.jsonl").read_text("utf-8").splitlines()
    ]
    parent = next(article for article in articles if article["block_kind"] == "table_parent")
    row_articles = [article for article in articles if article["block_kind"] == "table_row"]
    assert len(row_articles) == 2
    assert all(row["parent_article_id"] == parent["article_id"] for row in row_articles)
    assert "转专业申请表" in parent["text"]
    third = next(article for article in articles if article["article_no"] == "第三条")
    assert "转专业申请表" in third["text"]
    assert "13800138000" not in third["text"]
    assert "[手机号已脱敏]" in third["text"]
    assert "[姓名已脱敏]" in third["text"]
    assert "020-85280000" in third["text"]  # 部门公开座机保留

    documents = [
        json.loads(line)
        for line in (processed_dir / "documents.jsonl").read_text("utf-8").splitlines()
    ]
    assert documents[0]["source_url"] == "https://jwc.scau.edu.cn/example.htm"
    assert documents[0]["published_at"] == "2021-08-30"

    manifest_path = processed_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text("utf-8"))
    assert len(manifest["content_hash"]) == 64
    assert manifest["sources"][0]["articles"] == 5
    assert manifest["sources"][0]["indexable_articles"] == 5
    assert manifest["sources"][0]["stored_articles"] == 6

    rebuilt = build_index(
        sources_path=sources_path,
        parsed_dir=processed_dir / "parsed",
        output_dir=processed_dir,
    )
    assert rebuilt.manifest["content_hash"] == manifest["content_hash"]
