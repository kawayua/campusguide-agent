from __future__ import annotations

from datetime import UTC, date, datetime

import pytest
from pydantic import ValidationError

from campusguide.domain.enums import ArticleStatus, Audience, BlockType
from campusguide.domain.models import (
    Article,
    ArticlePublic,
    Claim,
    DocumentVersion,
)


def make_version(**overrides: object) -> DocumentVersion:
    payload = {
        "doc_id": "status-rules",
        "version_id": "v2025",
        "title": "本科学生学籍管理实施细则",
        "doc_type": "regulation",
        "issuing_department": "教务处",
        "source_url": "https://jwc.scau.edu.cn/2021/0830/c5127a288407/page.htm",
        "published_at": date(2025, 7, 15),
        "effective_from": date(2025, 9, 1),
        "effective_to": None,
        "status": ArticleStatus.ACTIVE,
        "source_sha256": "abc123",
        "fetched_at": datetime(2026, 10, 19, tzinfo=UTC),
        "parser_version": "test-1",
        "audience": [Audience.UNDERGRADUATE],
    }
    payload.update(overrides)
    return DocumentVersion.model_validate(payload)


def make_article(**overrides: object) -> Article:
    payload = {
        "article_id": "status-rules@v2025#第九条",
        "doc_id": "status-rules",
        "version_id": "v2025",
        "doc_title": "本科学生学籍管理实施细则",
        "chapter_path": ["第三章 学籍异动"],
        "article_no": "第九条",
        "article_index": 9,
        "part_no": 1,
        "block_type": BlockType.ARTICLE,
        "text": "学生申请转专业，应当符合学校规定。",
        "text_normalized": "学生申请转专业,应当符合学校规定.",
        "text_raw_ref": "status-rules/v2025/source.pdf",
        "audience": [Audience.UNDERGRADUATE],
        "status": ArticleStatus.ACTIVE,
        "effective_from": date(2025, 9, 1),
        "effective_to": None,
        "source_url": "https://jwc.scau.edu.cn/2021/0830/c5127a288407/page.htm",
        "published_at": date(2025, 7, 15),
        "issuing_department": "教务处",
        "topic_tags": ["学籍"],
        "flow_ids": [],
        "source_sha256": "abc123",
    }
    payload.update(overrides)
    return Article.model_validate(payload)


def test_document_version_rejects_reversed_dates() -> None:
    with pytest.raises(ValidationError):
        make_version(effective_to=date(2025, 8, 1))


def test_article_public_omits_local_reference() -> None:
    public = ArticlePublic.from_article(make_article())
    dumped = public.model_dump()
    assert "text_raw_ref" not in dumped
    assert dumped["article_id"] == "status-rules@v2025#第九条"


def test_claim_requires_citation() -> None:
    with pytest.raises(ValidationError):
        Claim(claim_id="c1", text="需要引用", citations=[])


def test_flow_configs_are_valid(repo_root) -> None:
    from campusguide.flows.dsl import load_flows

    flows = load_flows(repo_root / "configs" / "flows")
    assert len(flows) == 3
    assert {flow.flow_id for flow in flows} == {
        "scholarship-application",
        "recommendation-eligibility",
        "enrollment-certificate",
    }
    for flow in flows:
        assert flow.flow_id
        assert flow.steps
        assert flow.batch_label is not None or flow.flow_id == "enrollment-certificate"
