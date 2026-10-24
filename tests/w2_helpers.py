"""W2 测试共用的 Article / Evidence 构造器。"""

from __future__ import annotations

from datetime import date

from campusguide.domain.enums import (
    ArticleStatus,
    Audience,
    AuthorityLevel,
    BlockType,
    DocType,
)
from campusguide.domain.models import Article, Evidence
from campusguide.domain.text import normalize_text


def make_article(
    article_id: str = "demo-policy@v2021#第一条",
    *,
    doc_id: str = "demo-policy",
    version_id: str = "v2021",
    doc_title: str = "演示办法",
    article_no: str = "第一条",
    article_index: int = 1,
    part_no: int = 1,
    block_type: BlockType = BlockType.ARTICLE,
    text: str = "申请奖学金应当学习成绩优异，品行端正。",
    status: ArticleStatus = ArticleStatus.ACTIVE,
    effective_from: date = date(2021, 9, 1),
    effective_to: date | None = None,
    date_confidence: str = "high",
    audience: list[Audience] | None = None,
) -> Article:
    return Article(
        article_id=article_id,
        doc_id=doc_id,
        version_id=version_id,
        doc_title=doc_title,
        doc_type=DocType.REGULATION,
        authority_level=AuthorityLevel.BINDING,
        chapter_path=["第一章 总则"],
        article_no=article_no,
        article_index=article_index,
        part_no=part_no,
        block_type=block_type,
        text=text,
        text_normalized=normalize_text(text),
        audience=audience or [Audience.UNDERGRADUATE],
        status=status,
        effective_from=effective_from,
        effective_to=effective_to,
        source_url="https://example.edu.cn/policy",
        published_at=date(2021, 8, 30),
        date_confidence=date_confidence,
        issuing_department="本科生院",
        source_sha256="a" * 64,
    )


def make_evidence(article: Article, score: float = 0.9, rank: int = 1) -> Evidence:
    return Evidence(article=article, dense_score=score, rank=rank)
