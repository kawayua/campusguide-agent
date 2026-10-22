from __future__ import annotations

from datetime import date

import pytest

from campusguide.config import SourceVersion
from campusguide.domain.enums import BlockType
from campusguide.domain.models import ArticlePublic, Citation, PolicyFilters
from tests.w2_helpers import make_article


def test_source_version_infers_low_confidence_from_notes() -> None:
    version = SourceVersion(
        version_id="v2010",
        url="https://example.edu.cn/policy",
        published_at=date(2010, 12, 31),
        notes="仅知印发年份，published_at 取年末锚点（date_confidence=low）。",
    )
    assert version.date_confidence == "low"

    high = SourceVersion(
        version_id="v2011",
        url="https://example.edu.cn/policy",
        published_at=date(2011, 5, 1),
    )
    assert high.date_confidence == "high"


def test_article_public_carries_date_confidence_and_part_no() -> None:
    article = make_article(date_confidence="low")
    public = ArticlePublic.from_article(article)
    assert public.date_confidence == "low"
    assert public.part_no == 1


def test_citation_requires_part_identity_fields() -> None:
    citation = Citation(
        article_id="demo@v1#第九条",
        quote="学习成绩优异",
        quote_start=0,
        quote_end=6,
        doc_title="演示办法",
        article_no="第九条",
        part_no=2,
        block_type=BlockType.SECTION,
        effective_from=date(2021, 9, 1),
        date_confidence="low",
        source_url="https://example.edu.cn/policy",
    )
    assert citation.part_no == 2
    assert citation.block_type is BlockType.SECTION
    assert citation.date_confidence == "low"


def test_policy_filters_default_to_active_articles() -> None:
    filters = PolicyFilters()
    assert filters.block_types == [BlockType.ARTICLE]
    assert filters.status is not None
    assert filters.status[0].value == "active"


def test_build_index_low_confidence_gate(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    from campusguide.ingest.build_index import BuildError, build_index
    from tests.unit.test_build_index import write_parsed, write_sources

    sources = write_sources(tmp_path / "sources.yaml")
    write_parsed(tmp_path / "parsed")
    with pytest.raises(BuildError, match="date_confidence=low"):
        build_index(
            sources_path=sources,
            parsed_dir=tmp_path / "parsed",
            output_dir=tmp_path / "out",
            expected_low_confidence_versions=16,
        )
