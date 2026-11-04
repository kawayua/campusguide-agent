"""Structured role-member lists: row-level selection and deterministic coverage."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date

from campusguide.agents.coverage import (
    CoverageChecker,
    _clause_covered,
    _clause_gaps,
)
from campusguide.agents.plan import QuestionUnit
from campusguide.agents.providers import FakeChatProvider
from campusguide.domain.enums import Audience, BlockType
from campusguide.domain.models import Article, CellProvenance, Citation, Claim
from campusguide.retrieval.section_select import is_structured_leaf_row, select_sections
from campusguide.verification.coverage_items import (
    missing_source_items,
    required_source_items,
)
from tests.w2_helpers import make_article, make_evidence

_HEADERS = ("分工", "姓名", "所在单位", "备注")
_PATHS = (("分工",), ("姓名",), ("所在单位",), ("备注",))


def _cell(
    table_id: str,
    row_index: int,
    col: int,
    text: str,
    *,
    covered: bool = False,
    origin_row: int | None = None,
    text_source: str | None = "native",
) -> CellProvenance:
    return CellProvenance(
        cell_id=f"{table_id}-{row_index}-{col}",
        row=row_index,
        col=col,
        text=text,
        origin_row=row_index if origin_row is None else origin_row,
        origin_col=col,
        covered=covered,
        text_source=text_source,
    )


def _member_row(
    *,
    row_index: int,
    role: str,
    name: str,
    doc_id: str = "award-a",
    version_id: str = "v2024",
    doc_title: str = "甲学院评优办法",
    table_id: str = "t1",
    covered_role: bool = False,
    role_origin_row: int | None = None,
    name_text_source: str | None = "native",
    audience: list[Audience] | None = None,
) -> Article:
    role_cell = (
        _cell(
            table_id,
            row_index,
            0,
            "",
            covered=True,
            origin_row=role_origin_row,
        )
        if covered_role
        else _cell(table_id, row_index, 0, role)
    )
    cells = [
        role_cell,
        _cell(table_id, row_index, 1, name, text_source=name_text_source),
        _cell(table_id, row_index, 2, "甲学院"),
        _cell(table_id, row_index, 3, ""),
    ]
    header = "表头：" + " | ".join(_HEADERS)
    row = "行：" + " | ".join([role, name, "甲学院", ""])
    article = make_article(
        f"{doc_id}@{version_id}#{table_id}.row{row_index}",
        doc_id=doc_id,
        version_id=version_id,
        doc_title=doc_title,
        article_no=f"表格1第{row_index}行",
        block_type=BlockType.SECTION,
        text=f"{header}\n{row}",
        effective_from=date(2024, 9, 1),
        audience=audience,
    )
    return article.model_copy(
        update={
            "block_kind": "table_row",
            "parent_article_id": f"{doc_id}@{version_id}#table1",
            "parent_table_id": table_id,
            "table_id": table_id,
            "row_id": f"{table_id}-r{row_index}",
            "row_index": row_index,
            "table_headers": list(_HEADERS),
            "header_paths": [list(path) for path in _PATHS],
            "cell_provenance": cells,
        }
    )


def _award_rows() -> list[Article]:
    return [
        _member_row(row_index=1, role="审查委员", name="赵一"),
        _member_row(
            row_index=2,
            role="审查委员",
            name="钱二",
            covered_role=True,
            role_origin_row=1,
        ),
        _member_row(
            row_index=3,
            role="审查委员",
            name="孙 三",
            covered_role=True,
            role_origin_row=1,
        ),
        _member_row(row_index=4, role="财务委员", name="李四"),
        _member_row(row_index=5, role="委员", name="周七"),
        # 不可靠行：姓名格没有原生来源，不能生成必答成员。
        _member_row(
            row_index=6,
            role="审查委员",
            name="污一",
            name_text_source=None,
        ),
        # 纵向职务的 origin 行缺失，不能凭本行重复文本猜关系。
        _member_row(
            row_index=7,
            role="审查委员",
            name="缺一",
            covered_role=True,
            role_origin_row=99,
        ),
        _member_row(
            row_index=1,
            role="审查委员",
            name="郑十",
            version_id="v2023",
            doc_title="甲学院评优办法（2023版）",
            table_id="t2",
        ),
        _member_row(
            row_index=1,
            role="审查委员",
            name="吴九",
            doc_id="award-b",
            doc_title="乙学院评优办法",
            table_id="t3",
        ),
    ]


def _evidence(rows: list[Article]) -> list:
    return [make_evidence(article, rank=index) for index, article in enumerate(rows, 1)]


def _member_unit(
    *,
    span: str = "甲学院评优办法中审查委员有哪些人？",
    subject: str = "甲学院",
    version: str | None = "2024版",
    audience_scope: list[str] | None = None,
) -> QuestionUnit:
    return QuestionUnit(
        unit_id="u1",
        original_span=span,
        subject=subject,
        objects=[subject] if subject else [],
        version=version,
        audience_scope=audience_scope or [],
        requested_aspects=[span.strip("？?。 ")],
    )


def test_required_source_items_keep_all_members_of_requested_role() -> None:
    items = required_source_items(_member_unit(), _evidence(_award_rows()))
    assert items == ["赵一", "钱二", "孙三"]


def test_requested_role_never_mixes_other_committee_roles() -> None:
    unit = _member_unit(span="甲学院评优办法中财务委员有哪些人？")
    items = required_source_items(unit, _evidence(_award_rows()))
    assert items == ["李四"]


def test_missing_source_items_exclude_only_answered_member() -> None:
    missing = missing_source_items(
        _member_unit(),
        _evidence(_award_rows()),
        "甲学院评优办法的审查委员包括赵一。",
    )
    assert missing == ["钱二", "孙三"]


def _coverage_response() -> str:
    return json.dumps(
        {
            "units": [
                {
                    "unit_id": "u1",
                    "status": "answered",
                    "claim_ids": ["c1"],
                    "missing_items": [],
                }
            ],
            "unanswered_questions": [],
        },
        ensure_ascii=False,
    )


def _citation() -> Citation:
    quote = "审查委员 | 赵一 | 甲学院 |"
    return Citation(
        article_id="award-a@v2024#t1.row1",
        quote=quote,
        quote_start=0,
        quote_end=len(quote),
        doc_title="甲学院评优办法",
        article_no="表格1第1行",
        effective_from=date(2024, 9, 1),
        source_url="https://example.edu.cn/award",
    )


def test_coverage_marks_generation_missing_when_claims_omit_members() -> None:
    unit = _member_unit()
    evidence = _evidence(_award_rows())
    claim = Claim(
        claim_id="c1",
        text="甲学院评优办法的审查委员包括赵一。",
        citations=[_citation()],
    )
    audit = CoverageChecker(FakeChatProvider([_coverage_response()])).check(
        unit.original_span, [unit], [claim], evidence, []
    )
    row = audit.units[0]
    assert row["status"] == "generation_missing"
    assert {"钱二", "孙三"} <= set(row["missing_items"])


def test_member_name_whitespace_keeps_real_names_separate() -> None:
    rows = [
        _member_row(
            doc_title="甲学院评审专家名单",
            table_id="t9",
            row_index=1,
            role="评审专家",
            name="王 五 赵 六",
        ),
        _member_row(
            doc_title="甲学院评审专家名单",
            table_id="t9",
            row_index=2,
            role="评审专家",
            name="欧阳 明",
        ),
        _member_row(
            doc_title="甲学院评审专家名单",
            table_id="t9",
            row_index=3,
            role="评审专家",
            name="李 小 明",
        ),
        _member_row(
            doc_title="甲学院评审专家名单",
            table_id="t9",
            row_index=4,
            role="评审专家",
            name="周 八",
        ),
    ]
    unit = _member_unit(span="甲学院评审专家有哪些人？")
    items = required_source_items(unit, _evidence(rows))
    assert items == ["王五", "赵六", "欧阳明", "李小明", "周八"]
    assert "王五赵六" not in items


def test_member_items_respect_unit_audience_scope() -> None:
    undergrad = _member_row(
        row_index=1,
        role="评审专家",
        name="甲一",
        audience=[Audience.UNDERGRADUATE],
    )
    graduate = _member_row(
        row_index=2,
        role="评审专家",
        name="乙二",
        audience=[Audience.GRADUATE_MASTER],
    )
    unit = _member_unit(
        span="甲学院本科评优的评审专家有哪些人？",
        audience_scope=["本科"],
    )
    items = required_source_items(unit, _evidence([undergrad, graduate]))
    assert items == ["甲一"]


def test_parent_and_plain_text_never_become_member_items() -> None:
    parent = make_article(
        "award-a@v2024#table1",
        doc_id="award-a",
        version_id="v2024",
        doc_title="甲学院评优办法",
        article_no="表格1（完整表）",
        block_type=BlockType.TABLE,
        text="表头：分工 | 姓名\n行：审查委员 | 父一",
    ).model_copy(
        update={
            "block_kind": "table_parent",
            "table_id": "t1",
            "parent_table_id": "t1",
            "table_headers": ["分工", "姓名"],
            "header_paths": [["分工"], ["姓名"]],
        }
    )
    body = make_article(
        "award-a@v2024#正文",
        doc_id="award-a",
        version_id="v2024",
        doc_title="甲学院评优办法",
        article_no="评审说明",
        text="审查委员包括父二、父三。",
    )
    items = required_source_items(_member_unit(), _evidence([parent, body]))
    assert items == []
    assert is_structured_leaf_row(parent) is False
    assert is_structured_leaf_row(body) is False


@dataclass
class _Candidate:
    article: Article
    rerank_score: float | None = None


def test_leaf_member_rows_survive_table_section_cap() -> None:
    candidates = [
        _Candidate(article=article, rerank_score=0.50)
        for article in _award_rows()[:3]
    ]
    selection = select_sections(
        candidates,
        query="甲学院评优办法中审查委员有哪些人？",
        limit=3,
        max_table_sections=0,
    )
    assert [item.article.article_id for item in selection.candidates] == [
        item.article.article_id for item in candidates
    ]
    assert all(is_structured_leaf_row(item.article) for item in candidates)


def test_leaf_rows_still_obey_global_limit() -> None:
    candidates = [
        _Candidate(article=article, rerank_score=0.50)
        for article in _award_rows()[:4]
    ]
    selection = select_sections(
        candidates,
        query="甲学院评优办法中审查委员有哪些人？",
        limit=2,
        max_table_sections=0,
    )
    assert len(selection.candidates) == 2


def _large_course_table(index: int) -> _Candidate:
    rows = "\n".join(
        f"60000{index}{sub:02d} | 课程{index}-{sub} | 4 | 64" for sub in range(1, 16)
    )
    article = make_article(
        f"plan@v2024#课程表{index}",
        doc_id="plan",
        version_id="v2024",
        doc_title="甲专业培养方案",
        article_no=f"课程表{index}",
        block_type=BlockType.SECTION,
        text=f"课程代码 | 课程名称 | 学分 | 学时\n{rows}",
    )
    return _Candidate(article=article, rerank_score=0.70 + index * 0.01)


def test_large_unstructured_course_tables_remain_capped() -> None:
    selection = select_sections(
        [_large_course_table(1), _large_course_table(2), _large_course_table(3)],
        query="甲专业培养方案",
        limit=3,
        max_table_sections=2,
    )
    assert len(selection.candidates) == 2
    assert any(
        warning.startswith("TABLE_SECTION_DEPRIORITIZED")
        for warning in selection.warnings
    )


def test_doctoral_term_equivalence_is_local() -> None:
    assert _clause_covered("博士生", "博士研究生的研究成果要求如下。")
    assert _clause_covered("博士研究生", "博士生的研究成果要求如下。")
    assert _clause_covered("硕士生", "硕士研究生的研究成果要求如下。")
    assert _clause_covered("硕士研究生", "硕士生的研究成果要求如下。")
    assert not _clause_covered("硕士生", "博士研究生的研究成果要求如下。")
    assert not _clause_covered("博士生", "硕士研究生的研究成果要求如下。")
    assert not _clause_covered("研究生", "博士研究生的研究成果要求如下。")
    assert not _clause_covered("研究生", "硕士研究生的研究成果要求如下。")
    assert not _clause_covered("博士", "硕士研究生的研究成果要求如下。")
    assert not _clause_covered("硕士", "博士研究生的研究成果要求如下。")


def test_doctoral_equivalence_removes_false_clause_gap() -> None:
    claim = Claim(
        claim_id="c1",
        text="博士研究生的研究成果要求如下。",
        citations=[_citation()],
    )
    gaps, claim_ids = _clause_gaps(["博士生"], [claim])
    assert gaps == [] and claim_ids == ["c1"]

    gaps, claim_ids = _clause_gaps(["硕士生"], [claim])
    assert gaps == ["硕士生"] and claim_ids == []
