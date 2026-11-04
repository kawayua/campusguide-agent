"""新ingestion结构化表格：多行单元格、quote合并、单位与重复类别的最小回归。"""

from __future__ import annotations

from datetime import date

from campusguide.domain.models import Article, CellProvenance, Evidence
from campusguide.retrieval.source_spans import source_spans, structured_table_row
from campusguide.verification.facts import check_evidence_fact_coverage


def _article(
    headers: list[list[str]],
    values: list[str],
    *,
    doc_id: str = "binding",
    row_index: int = 1,
) -> Article:
    table_id = f"{doc_id}@v1#table0"
    text = "表头：" + " | ".join(" > ".join(header) for header in headers)
    text += "\n行：" + " | ".join(values)
    return Article(
        article_id=f"{table_id}.row{row_index}",
        doc_id=doc_id,
        version_id="v1",
        doc_title="测试培养方案",
        article_no=f"表格0第{row_index + 1}行",
        article_index=10000 + row_index,
        block_type="section",
        block_kind="table_row",
        text=text,
        text_normalized=text,
        audience=["undergraduate"],
        status="active",
        effective_from=date(2026, 1, 1),
        source_url="https://example.org/plan",
        published_at=date(2026, 1, 1),
        issuing_department="测试",
        source_sha256="a" * 64,
        parent_article_id=table_id,
        parent_table_id=table_id,
        table_id=table_id,
        row_id=f"{table_id}#row{row_index}",
        row_index=row_index,
        table_headers=[" > ".join(header) for header in headers],
        header_paths=headers,
        cell_provenance=[
            CellProvenance(
                cell_id=f"{table_id}#r{row_index}c{index}",
                row=row_index,
                col=index,
                text=value,
                origin_row=row_index,
                origin_col=index,
                text_source="native",
            )
            for index, value in enumerate(values)
        ],
    )


def test_multiline_header_and_row_keep_raw_offsets_and_merge_quote_spans() -> None:
    headers = [
        ["课程\n类别"],
        ["课程\n代码"],
        ["课程名称"],
        ["学分"],
        ["学时", "总数"],
        ["学时", "理论"],
        ["学时", "实验"],
        ["学时", "实习"],
        ["修读学期"],
    ]
    values = [
        "通识\n通修\n课程",
        "610013",
        "大学英语Ⅰ\nCollege English Ⅰ",
        "2",
        "32",
        "32",
        "0",
        "0",
        "1",
    ]
    article = _article(headers, values)
    row = structured_table_row(article)
    assert row is not None and row.reliable
    assert (row.value("credits"), row.value("total_hours"), row.value("semester")) == (
        "2",
        "32",
        "1",
    )
    row_content_start = article.text.index("行：")
    assert row.quote == article.text[row_content_start:]
    assert article.text[slice(*row.cell_spans[3])] == "2"
    assert article.text[slice(*row.cell_spans[2])] == "大学英语Ⅰ\nCollege English Ⅰ"

    span = next(
        item for key, item in source_spans([Evidence(article=article)]).items() if "_row_" in key
    )
    assert span.header_reliable
    assert (span.credits, span.hours, span.semester) == ("2", "32", "1")
    # 同一article的表头quote与行quote都合法时，行单元格必须仍被覆盖。
    coverage = check_evidence_fact_coverage(
        "大学英语Ⅰ为2学分，总学时32学时。",
        [row.header_quote, row.quote],
        [article, article],
    )
    assert not coverage.uncovered


def test_foreign_or_non_unit_currency_header_never_guesses_cny() -> None:
    for header in ("奖励标准（美元/人·年）", "奖励标准（单元）"):
        article = _article([["类别"], [header]], ["甲类研究生奖学金", "7500"])
        row = structured_table_row(article)
        assert row is not None and row.reliable
        assert row.columns[1].field == "other"
        assert check_evidence_fact_coverage(
            "甲类研究生奖学金为7500元。", [article.text], [article]
        ).uncovered

    article = _article([["类别"], ["奖励标准（元/人·年）"]], ["甲类研究生奖学金", "7500"])
    row = structured_table_row(article)
    assert row is not None and row.reliable
    assert row.columns[1].field == "amount"
    assert not check_evidence_fact_coverage(
        "甲类研究生奖学金为7500元。", [article.text], [article]
    ).uncovered


def test_duplicate_category_is_context_but_duplicate_credits_stay_invalid() -> None:
    article = _article(
        [["课程类别"], ["课程类别"], ["课程代码"], ["课程名称"], ["学分"], ["学时", "总数"]],
        ["专业教育", "专业基础课", "610013", "大学英语Ⅰ", "2", "32"],
    )
    row = structured_table_row(article)
    assert row is not None and row.reliable
    assert [column.field for column in row.columns[:2]] == ["category", "category"]
    assert not check_evidence_fact_coverage(
        "大学英语Ⅰ为2学分。", [article.text], [article]
    ).uncovered

    duplicated = _article(
        [["课程代码"], ["课程名称"], ["学分"], ["学分"]],
        ["610013", "大学英语Ⅰ", "2", "4"],
    )
    duplicate_row = structured_table_row(duplicated)
    assert duplicate_row is not None and not duplicate_row.reliable
    assert "DUPLICATE_FIELD:credits" in duplicate_row.problems


def test_fourteen_digit_course_code_is_an_entity_anchor() -> None:
    article = _article(
        [["课程编号"], ["课程中文名称"], ["学分"], ["学时", "总数"]],
        ["19021000000002", "马克思主义与社会科学\n方法论", "1", "16"],
    )
    assert not check_evidence_fact_coverage(
        "课程19021000000002为1学分。", [article.text], [article]
    ).uncovered
