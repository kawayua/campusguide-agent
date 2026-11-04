"""原生表格行必须保留完整列位、跨度和章节上下文，且可贯通公开接口。"""

from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import ClassVar

import pytest

from campusguide.domain.enums import ArticleStatus, Audience
from campusguide.domain.models import Article, ArticlePublic, DocumentVersion
from campusguide.ingest.parse import ParsedDocument, parse_html_file
from campusguide.ingest.segment import segment_document
from campusguide.ingest.structure import extract_pdf_table
from campusguide.mcp.policy_search_server import PolicySearchService
from campusguide.retrieval.corpus_views import CorpusViews
from campusguide.retrieval.indexer import article_payload
from campusguide.retrieval.profile_expand import ProfileLexicon

_HTML = """<!doctype html>
<html><body>
<h1>软件工程专业人才培养方案（2024版）</h1>
<p>十、培养计划进程表</p>
<table>
  <tr>
    <th rowspan="2">课程类别</th>
    <th colspan="2">课程信息</th>
    <th rowspan="2">学分</th>
    <th rowspan="2">备注</th>
  </tr>
  <tr><th>编号</th><th>名称</th></tr>
  <tr>
    <td rowspan="2">公共必修</td>
    <td>19021000000004</td>
    <td>新时代中国特色社会主义理论与实践</td>
    <td>2</td>
    <td></td>
  </tr>
  <tr>
    <td>19021000000002</td>
    <td>马克思主义与社会科学方法论</td>
    <td>1</td>
    <td>二选一</td>
  </tr>
</table>
</body></html>
"""


def _version() -> DocumentVersion:
    return DocumentVersion(
        doc_id="se-plan",
        version_id="v2024",
        title="软件工程专业人才培养方案（2024版）",
        doc_type="guide",
        issuing_department="信息学院",
        source_url="https://info.scau.edu.cn/plan.htm",
        published_at=date(2024, 6, 1),
        effective_from=date(2024, 9, 1),
        status=ArticleStatus.ACTIVE,
        source_sha256="b" * 64,
        fetched_at=datetime(2026, 10, 30, tzinfo=UTC),
        parser_version="test",
        audience=[Audience.UNDERGRADUATE],
    )


def _segment(tmp_path: Path):
    path = tmp_path / "plan.html"
    path.write_text(_HTML, encoding="utf-8")
    parsed = parse_html_file(path, doc_id="se-plan", version_id="v2024")
    assert parsed.structure is not None
    document = ParsedDocument(
        doc_id="se-plan",
        version_id="v2024",
        title=parsed.title,
        text=parsed.text,
        format="html",
        source_sha256="b" * 64,
        parser_version=parsed.parser_version,
        warnings=parsed.warnings,
        tables=parsed.tables,
        structure=parsed.structure,
        ingestion_mode="native_only",
    )
    return segment_document(document, _version())


def test_table_row_keeps_full_grid_and_section_chapter(tmp_path: Path) -> None:
    result = _segment(tmp_path)
    parents = [item for item in result.articles if item.block_kind == "table_parent"]
    rows = [item for item in result.articles if item.block_kind == "table_row"]
    assert len(parents) == 1
    assert len(rows) == 2

    first, second = rows
    assert [cell.col for cell in first.cell_provenance] == [0, 1, 2, 3, 4]
    assert first.cell_provenance[0].rowspan == 2
    assert first.cell_provenance[0].origin_row == 2
    assert first.cell_provenance[3].text == "2"
    assert first.cell_provenance[4].text == ""
    assert first.header_paths == [
        ["课程类别"],
        ["课程信息", "编号"],
        ["课程信息", "名称"],
        ["学分"],
        ["备注"],
    ]
    assert first.chapter_path[-1] == "十、培养计划进程表"

    covered = second.cell_provenance[0]
    assert covered.covered is True
    assert covered.origin_row == 2
    assert covered.origin_col == 0
    assert second.cell_provenance[4].text == "二选一"
    assert first.parent_article_id == parents[0].article_id
    assert first.table_id == parents[0].table_id


def test_public_payload_and_mcp_expose_cell_provenance(tmp_path: Path) -> None:
    result = _segment(tmp_path)
    row = next(item for item in result.articles if item.block_kind == "table_row")
    parent = next(item for item in result.articles if item.block_kind == "table_parent")

    payload = article_payload(row)
    assert [cell["col"] for cell in payload["cell_provenance"]] == [0, 1, 2, 3, 4]
    assert payload["header_paths"][2] == ["课程信息", "名称"]

    class _Backend:
        def get_article(self, article_id: str, part_no: int | None = None):
            for article in (row, parent):
                if article.article_id == article_id:
                    return article
            return None

    service = PolicySearchService(_Backend())
    envelope = service.get_article(row.article_id)
    assert envelope["ok"] is True
    data = envelope["data"]
    assert data["article"]["cell_provenance"][3]["col"] == 3
    assert data["article"]["header_paths"][1] == ["课程信息", "编号"]
    assert data["parent"]["article_id"] == parent.article_id
    assert data["metadata"]["cell_provenance"][0]["rowspan"] == 2


_CONTACT_HTML = """<!doctype html>
<html><body>
<h1>学院公开联系方式</h1>
<table>
  <tr><th>联系人</th><th>手机</th></tr>
  <tr><td>办公室</td><td>13800138000</td></tr>
  <tr><td>教务员</td><td>学号：2023123456</td></tr>
</table>
</body></html>
"""


def _segment_contacts(tmp_path: Path, *, redact_contacts: bool):
    path = tmp_path / "contacts.html"
    path.write_text(_CONTACT_HTML, encoding="utf-8")
    parsed = parse_html_file(path, doc_id="se-plan", version_id="v2024")
    assert parsed.structure is not None
    document = ParsedDocument(
        doc_id="se-plan",
        version_id="v2024",
        title=parsed.title,
        text=parsed.text,
        format="html",
        source_sha256="b" * 64,
        parser_version=parsed.parser_version,
        warnings=parsed.warnings,
        tables=parsed.tables,
        structure=parsed.structure,
        ingestion_mode="native_only",
    )
    return segment_document(document, _version(), redact_contacts=redact_contacts)


def test_table_rows_and_provenance_are_redacted_by_default(tmp_path: Path) -> None:
    result = _segment_contacts(tmp_path, redact_contacts=True)
    rows = [item for item in result.articles if item.block_kind == "table_row"]

    assert rows
    for article in rows:
        assert "13800138000" not in article.text
        assert all("13800138000" not in cell.text for cell in article.cell_provenance)
        assert "13800138000" not in ArticlePublic.from_article(article).model_dump_json()


def test_public_staff_contacts_survive_explicit_opt_out(tmp_path: Path) -> None:
    result = _segment_contacts(tmp_path, redact_contacts=False)
    rows = [item for item in result.articles if item.block_kind == "table_row"]

    phone_row = next(item for item in rows if "13800138000" in item.text)
    assert any("13800138000" in cell.text for cell in phone_row.cell_provenance)
    assert "13800138000" in ArticlePublic.from_article(phone_row).model_dump_json()

    student_row = next(item for item in rows if "教务员" in item.text)
    assert "2023123456" not in student_row.text
    assert all("2023123456" not in cell.text for cell in student_row.cell_provenance)


_STUDENT_ID_HTML = """<!doctype html>
<html><body>
<h1>学生名单</h1>
<p>名单如下：</p>
<table>
  <tr><th rowspan="2">学号</th><th colspan="2">课程信息</th></tr>
  <tr><th>编号</th><th>名称</th></tr>
  <tr><td>202012345678</td><td>19021000000004</td><td>测试课程</td></tr>
</table>
</body></html>
"""


def _segment_student_ids(tmp_path: Path, *, redact_contacts: bool):
    path = tmp_path / "student-ids.html"
    path.write_text(_STUDENT_ID_HTML, encoding="utf-8")
    parsed = parse_html_file(path, doc_id="se-plan", version_id="v2024")
    assert parsed.structure is not None
    document = ParsedDocument(
        doc_id="se-plan",
        version_id="v2024",
        title=parsed.title,
        text=parsed.text,
        format="html",
        source_sha256="b" * 64,
        parser_version=parsed.parser_version,
        warnings=parsed.warnings,
        tables=parsed.tables,
        structure=parsed.structure,
        ingestion_mode="native_only",
    )
    return segment_document(document, _version(), redact_contacts=redact_contacts)


@pytest.mark.parametrize("redact_contacts", [True, False])
def test_student_id_column_is_redacted_even_when_contacts_are_public(
    tmp_path: Path, redact_contacts: bool
) -> None:
    result = _segment_student_ids(tmp_path, redact_contacts=redact_contacts)
    rows = [item for item in result.articles if item.block_kind == "table_row"]
    parents = [item for item in result.articles if item.block_kind == "table_parent"]
    assert len(rows) == 1
    assert len(parents) == 1

    for article in result.articles:
        public = ArticlePublic.from_article(article).model_dump_json()
        assert "202012345678" not in public
        assert "202012345678" not in article.text

    row = rows[0]
    assert "[学号已脱敏]" in row.text
    assert row.cell_provenance[0].text == "[学号已脱敏]"
    # 同一行的课程代码是长数字但不是学号，不能按值形态误伤。
    assert "19021000000004" in row.text
    assert row.cell_provenance[1].text == "19021000000004"
    assert row.cell_provenance[2].text == "测试课程"


def test_corpus_views_hydrate_parent_but_do_not_recall_it(tmp_path: Path) -> None:
    result = _segment(tmp_path)
    row = next(item for item in result.articles if item.block_kind == "table_row")
    parent = next(item for item in result.articles if item.block_kind == "table_parent")

    views = CorpusViews(
        [parent, row],
        ProfileLexicon(college_terms={}, major_terms={"se": ("软件工程",)}),
    )
    assert views.article_by_id(parent.article_id) is parent

    matches = views.search("软件工程 培养计划 课程 学分", limit=1)
    assert matches.articles
    assert all(item.article_id != parent.article_id for item in matches.articles)
    assert any(item.article_id == row.article_id for item in matches.articles)


_SPAN_GRID_HTML = """<!doctype html>
<html><body>
<h1>学生名单</h1>
<table>
  <tr><th>角色</th><th colspan="2">课程</th><th>学分</th><th>备注</th></tr>
  <tr><td rowspan="2">班长</td><td colspan="2">软件工程实践</td><td>2</td><td></td></tr>
  <tr><td>19021000000004</td><td>专业实习</td><td>3</td><td>必修</td></tr>
</table>
</body></html>
"""


def _segment_html(
    tmp_path: Path,
    html: str,
    filename: str,
    *,
    redact_contacts: bool = True,
):
    path = tmp_path / filename
    path.write_text(html, encoding="utf-8")
    parsed = parse_html_file(path, doc_id="se-plan", version_id="v2024")
    assert parsed.structure is not None
    document = ParsedDocument(
        doc_id="se-plan",
        version_id="v2024",
        title=parsed.title,
        text=parsed.text,
        format="html",
        source_sha256="b" * 64,
        parser_version=parsed.parser_version,
        warnings=parsed.warnings,
        tables=parsed.tables,
        structure=parsed.structure,
        ingestion_mode="native_only",
    )
    return segment_document(document, _version(), redact_contacts=redact_contacts)


def _row_value(article: Article) -> str:
    return article.text.rsplit("行：", 1)[-1]


def test_html_rowspan_role_and_colspan_data_keep_leaf_columns(tmp_path: Path) -> None:
    result = _segment_html(tmp_path, _SPAN_GRID_HTML, "span-grid.html")
    rows = [item for item in result.articles if item.block_kind == "table_row"]
    assert len(rows) == 2

    first, second = rows
    assert [cell.strip() for cell in _row_value(first).split("|")] == [
        "班长",
        "软件工程实践",
        "",
        "2",
        "",
    ]
    assert [cell.strip() for cell in _row_value(second).split("|")] == [
        "班长",
        "19021000000004",
        "专业实习",
        "3",
        "必修",
    ]
    # 同一行 colspan 留空位，不把 origin 数据广播到占用的下一列。
    assert first.cell_provenance[2].covered is True
    assert first.cell_provenance[2].text == ""
    # 纵向 origin 只在 origin_col 带入一次，后续列保持原列位。
    assert second.cell_provenance[0].covered is True
    assert second.cell_provenance[0].origin_row == 1


def _placed(text: str, *, rowspan: int = 1, colspan: int = 1) -> SimpleNamespace:
    return SimpleNamespace(
        text=text,
        rowspan=rowspan,
        colspan=colspan,
        bbox=[0.0, 0.0, 10.0, 10.0],
    )


class _PlacementTable:
    col_count = 4
    header_rows = 1
    bbox: ClassVar[list[float]] = [0.0, 0.0, 100.0, 100.0]
    rows: ClassVar[list[object]] = []
    placements: ClassVar[list[list[SimpleNamespace]]] = [
        [_placed("角色"), _placed("课程", colspan=2), _placed("学分")],
        [
            _placed("班长", rowspan=2),
            _placed("软件工程实践", colspan=2),
            _placed("2"),
        ],
        [_placed("19021000000004"), _placed("专业实习"), _placed("3")],
    ]

    def extract(self) -> list[list[str]]:
        return [
            ["角色", "课程", "", "学分"],
            ["班长", "软件工程实践", "", "2"],
            ["", "19021000000004", "专业实习", "3"],
        ]


def test_pdf_horizontal_span_keeps_following_numbers_in_columns() -> None:
    structure, _ = extract_pdf_table(
        _PlacementTable(),
        doc_id="se-plan",
        version_id="v2024",
        table_index=0,
        page=1,
        block_id="b0",
    )
    assert structure.column_count == 4
    assert structure.rows[1].text.split(" | ") == [
        "班长",
        "软件工程实践",
        "",
        "2",
    ]
    assert structure.rows[2].text.split(" | ") == [
        "班长",
        "19021000000004",
        "专业实习",
        "3",
    ]


_STUDENT_ID_ROWSPAN_HTML = """<!doctype html>
<html><body>
<h1>学生名单</h1>
<table>
  <tr><th>学号</th><th>姓名</th><th>课程</th></tr>
  <tr><td rowspan="2">202012345678</td><td>张三</td><td>课程A</td></tr>
  <tr><td>李四</td><td>课程B</td></tr>
</table>
</body></html>
"""


@pytest.mark.parametrize("redact_contacts", [True, False])
def test_student_id_vertical_origin_stays_redacted_and_full_width(
    tmp_path: Path, redact_contacts: bool
) -> None:
    result = _segment_html(
        tmp_path,
        _STUDENT_ID_ROWSPAN_HTML,
        "student-id-rowspan.html",
        redact_contacts=redact_contacts,
    )
    rows = [item for item in result.articles if item.block_kind == "table_row"]
    assert len(rows) == 2

    first, second = rows
    assert _row_value(first).split(" | ") == ["[学号已脱敏]", "张三", "课程A"]
    assert _row_value(second).split(" | ") == ["[学号已脱敏]", "李四", "课程B"]
    assert second.cell_provenance[0].covered is True
    assert second.cell_provenance[0].text == ""
    for article in result.articles:
        public = ArticlePublic.from_article(article).model_dump_json()
        assert "202012345678" not in public
        assert "202012345678" not in article.text
