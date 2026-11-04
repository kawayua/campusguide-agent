"""Follow-up 2026-10-30: table header/row source context must stay separate."""

from __future__ import annotations

from campusguide.agents.advisor import build_evidence_block
from campusguide.agents.plan import QuestionUnit
from campusguide.domain.models import Citation, Claim
from campusguide.retrieval.source_spans import source_spans, table_header_context
from campusguide.verification.coverage_items import required_course_items
from campusguide.verification.semantic import _item_payload
from tests.w2_helpers import make_article, make_evidence

HEADER = "课程代码 | 课程名称 | 学分 | 总学时 | 周学时 | 周数 | 开课学期"
ROW = "617017 | 毕业论文（设计） | 6 | 210 | 10 | 14 | 第8学期"
DETAIL_HEADER = "课程代码 | 课程名称 | 学分 | 学时 | 修读学期"
MATRIX_HEADER = "课程代码 | 课程名称 | 毕业要求1 | 支撑强度"
MATRIX_ROW = "617017 | 毕业论文（设计） | H | 强"


def _part(text: str, part_no: int, *, article_no: str = "十、培养计划进程表"):
    return make_article(
        f"plan@v2024#{article_no}",
        doc_id="plan",
        version_id="v2024",
        doc_title="2024版农学专业培养方案",
        article_no=article_no,
        part_no=part_no,
        text=text,
    )


def test_cross_fragment_header_keeps_columns_and_original_quotes_separate() -> None:
    header = _part(HEADER, 1)
    row = _part(ROW, 3)
    evidence = [make_evidence(header, rank=1), make_evidence(row, rank=2)]
    spans = source_spans(evidence)
    row_span = next(span for span in spans.values() if "_row_" in span.span_id)
    assert row_span.headers[:3] == ("课程代码", "课程名称", "学分")
    assert (row_span.header_article_id, row_span.header_part_no) == ("plan@v2024#十、培养计划进程表", 1)
    assert row_span.header_quote == HEADER
    # 行 quote 仍是本片段的原文，不拼接表头，start/end 指向本片段。
    assert row_span.quote == ROW
    assert (row_span.start, row_span.end) == (0, len(ROW))
    assert row_span.credits == "6"
    assert row_span.hours == "210"
    assert row_span.weekly_hours == "10"
    assert row_span.weeks == "14"
    assert row_span.semester == "第8学期"
    assert not row_span.is_matrix


def test_cross_fragment_matrix_header_marks_rows_and_excludes_course_list() -> None:
    header = _part(MATRIX_HEADER, 1, article_no="课程设置")
    row = _part(MATRIX_ROW, 3, article_no="课程设置")
    evidence = [make_evidence(header), make_evidence(row, rank=2)]
    spans = source_spans(evidence)
    row_span = next(span for span in spans.values() if "_row_" in span.span_id)
    assert row_span.is_matrix
    unit = QuestionUnit(
        unit_id="u1",
        original_span="农学专业实践课程有哪些？",
        requested_aspects=["实践课程"],
    )
    assert required_course_items(unit, evidence) == []


def test_table_header_context_reports_header_fragment_without_joining() -> None:
    header = _part(DETAIL_HEADER, 1)
    row = _part(ROW, 3)
    evidence = [make_evidence(header), make_evidence(row, rank=2)]
    context = table_header_context(evidence, row.article_id, row.part_no, 0)
    assert context is not None
    assert context["part_no"] == 1
    assert context["quote"] == DETAIL_HEADER
    assert context["columns"] == ["课程代码", "课程名称", "学分", "学时", "修读学期"]


def test_advisor_evidence_points_row_to_independent_header() -> None:
    header = _part(DETAIL_HEADER, 1)
    row = _part(ROW, 3)
    block = build_evidence_block([make_evidence(header, rank=1), make_evidence(row, rank=2)])
    assert "同表表头=证据1" in block
    assert "同表后续行（引用行时须同时引用本表头）：证据2 source_2_row_1" in block
    # 表头只作为证据1的正文出现一次，未拼进证据2的正文。
    assert block.count(DETAIL_HEADER) == 1
    assert block.count(ROW) == 1


def test_semantic_payload_adds_separate_table_header_metadata() -> None:
    header = _part(DETAIL_HEADER, 1)
    row = _part(ROW, 3)
    evidence = [make_evidence(header, rank=1), make_evidence(row, rank=2)]
    citation = Citation(
        article_id=row.article_id,
        part_no=row.part_no,
        quote=ROW,
        quote_start=0,
        quote_end=len(ROW),
        doc_title=row.doc_title,
        article_no=row.article_no,
        effective_from=row.effective_from,
        source_url=row.source_url,
    )
    claim = Claim(claim_id="c1", text="毕业论文（设计）6学分，第8学期。", citations=[citation])
    payload = _item_payload(claim, evidence)
    source = payload["sources"][0]
    assert source["article_id"] == row.article_id
    assert source["part_no"] == 3
    assert source["quote"] == ROW
    assert source["table_header"] == {
        "article_id": row.article_id,
        "part_no": 1,
        "quote": DETAIL_HEADER,
        "columns": ["课程代码", "课程名称", "学分", "学时", "修读学期"],
    }
    assert source["table_role"] == "curriculum_row"


def test_semantic_payload_marks_support_matrix_rows() -> None:
    header = _part(MATRIX_HEADER, 1, article_no="课程设置")
    row = _part(MATRIX_ROW, 3, article_no="课程设置")
    evidence = [make_evidence(header), make_evidence(row, rank=2)]
    citation = Citation(
        article_id=row.article_id,
        part_no=row.part_no,
        quote=MATRIX_ROW,
        quote_start=0,
        quote_end=len(MATRIX_ROW),
        doc_title=row.doc_title,
        article_no=row.article_no,
        effective_from=row.effective_from,
        source_url=row.source_url,
    )
    claim = Claim(claim_id="c1", text="毕业论文（设计）支撑毕业要求。", citations=[citation])
    source = _item_payload(claim, evidence)["sources"][0]
    assert source["table_role"] == "support_matrix"
    assert source["table_header"]["quote"] == MATRIX_HEADER


def test_row_without_header_keeps_column_semantics_empty() -> None:
    row = _part(ROW, 3)
    evidence = [make_evidence(row)]
    span = next(span for span in source_spans(evidence).values() if "_row_" in span.span_id)
    assert span.headers == ()
    assert (span.credits, span.hours, span.weeks, span.semester) == ("", "", "", "")
    assert table_header_context(evidence, row.article_id, row.part_no, 0) is None
    citation = Citation(
        article_id=row.article_id,
        part_no=row.part_no,
        quote=ROW,
        quote_start=0,
        quote_end=len(ROW),
        doc_title=row.doc_title,
        article_no=row.article_no,
        effective_from=row.effective_from,
        source_url=row.source_url,
    )
    claim = Claim(claim_id="c1", text="毕业论文（设计）安排在第8学期。", citations=[citation])
    source = _item_payload(claim, evidence)["sources"][0]
    assert "table_header" not in source
    assert source["table_role"] == "text"
