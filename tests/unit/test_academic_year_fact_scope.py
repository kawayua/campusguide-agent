"""Academic-year labels are source scope, never durations or broad year scope."""

from __future__ import annotations

from datetime import date

from campusguide.domain.models import Article, CellProvenance
from campusguide.domain.text import normalize_text
from campusguide.verification.facts import check_evidence_fact_coverage
from tests.w2_helpers import make_article


def test_same_source_title_scopes_complete_academic_year_label() -> None:
    article = make_article(
        doc_title="乙学院2019—2020学年综合评议通知",
        text="学生自评在9月2日。",
    )
    for label in ("2019-2020", "2019—2020", "２０１９－２０２０", "2019至2020"):
        claim = f"乙学院{label}学年综合评议中，学生自评在9月2日。"
        coverage = check_evidence_fact_coverage(claim, [article.text], [article])
        assert not coverage.uncovered, (
            label,
            [fact.canonical for fact in coverage.uncovered],
        )


def test_wrong_inverted_or_incomplete_academic_year_keeps_gate() -> None:
    wrong = make_article(
        doc_title="乙学院2018—2019学年综合评议通知",
        text="学生自评在9月2日。",
    )
    for label in ("2019-2020", "2020-2019"):
        claim = f"乙学院{label}学年综合评议中，学生自评在9月2日。"
        assert check_evidence_fact_coverage(claim, [wrong.text], [wrong]).uncovered

    nonconsecutive = make_article(
        doc_title="乙学院2019—2021学年综合评议通知",
        text="学生自评在9月2日。",
    )
    assert check_evidence_fact_coverage(
        "乙学院2019-2021学年综合评议中，学生自评在9月2日。",
        [nonconsecutive.text],
        [nonconsecutive],
    ).uncovered


def test_academic_year_counts_stay_durations() -> None:
    article = make_article(
        doc_title="乙学院2019—2020学年综合评议通知",
        text="学生自评在9月2日。",
    )
    for claim in ("学生学制为2学年。", "学生可延长1学年。", "该安排自2019学年起执行。"):
        assert check_evidence_fact_coverage(claim, [article.text], [article]).uncovered


def test_academic_year_scope_requires_one_complete_cited_title() -> None:
    first = make_article(
        article_id="first#1",
        doc_id="first",
        doc_title="甲学院2019学年综合评议通知",
        text="学生自评在9月2日。",
    )
    second = make_article(
        article_id="second#1",
        doc_id="second",
        doc_title="乙学院2020学年综合评议通知",
        text="学生自评在9月2日。",
    )
    claim = "甲学院2019-2020学年综合评议中，学生自评在9月2日。"
    assert check_evidence_fact_coverage(
        claim, [first.text, second.text], [first, second]
    ).uncovered

    matching = make_article(
        article_id="matching#1",
        doc_id="matching",
        doc_title="丙学院2019—2020学年综合评议通知",
        text="学生自评在9月2日。",
    )
    other = make_article(
        article_id="other#1",
        doc_id="other",
        doc_title="丁学院2018—2019学年综合评议通知",
        text="申请材料为审批表。",
    )
    # The matching title is present, but only the other source backs the quote.
    assert check_evidence_fact_coverage(
        "丙学院2019-2020学年综合评议中，申请材料为审批表。",
        [other.text],
        [matching, other],
    ).uncovered


def test_title_academic_year_never_supports_deadline_or_amount() -> None:
    article = make_article(
        doc_title="乙学院2019—2020学年综合评议通知",
        text="学生自评在9月2日。申请截止时间为2020年9月2日。",
    )
    deadline = check_evidence_fact_coverage(
        "乙学院2019-2020学年综合评议中，申请截止时间为2020年9月2日。",
        ["学生自评在9月2日。"],
        [article],
    )
    assert {"2020-09-02", "2020"} <= {
        fact.canonical for fact in deadline.uncovered
    }

    amount = check_evidence_fact_coverage(
        "乙学院2019-2020学年综合评议中，奖励标准为7500元。",
        ["学生自评在9月2日。"],
        [article],
    )
    assert "7500元" in {fact.canonical for fact in amount.uncovered}


def _structured_row(headers: list[list[str]], values: list[str]) -> Article:
    table_id = "scope@v1#table0"
    text = "表头：" + " | ".join(" > ".join(header) for header in headers)
    text += "\n行：" + " | ".join(values)
    return Article(
        article_id=f"{table_id}.row1",
        doc_id="scope",
        version_id="v1",
        doc_title="乙学院2019—2020学年评奖通知",
        article_no="表格0第2行",
        article_index=10001,
        block_type="section",
        block_kind="table_row",
        text=text,
        text_normalized=normalize_text(text),
        audience=["undergraduate"],
        status="active",
        effective_from=date(2019, 9, 1),
        source_url="https://example.org/plan",
        published_at=date(2019, 9, 1),
        issuing_department="测试",
        source_sha256="a" * 64,
        parent_article_id=table_id,
        parent_table_id=table_id,
        table_id=table_id,
        row_id=f"{table_id}#row1",
        row_index=1,
        table_headers=[" > ".join(header) for header in headers],
        header_paths=headers,
        cell_provenance=[
            CellProvenance(
                cell_id=f"{table_id}#r1c{index}",
                row=1,
                col=index,
                text=value,
                origin_row=1,
                origin_col=index,
                text_source="native",
            )
            for index, value in enumerate(values)
        ],
    )


def test_structured_amount_wrong_category_stays_rejected() -> None:
    rejected = _structured_row(
        [["类别"], ["奖励标准（美元/人·年）"]],
        ["甲类研究生奖学金", "7500"],
    )
    assert check_evidence_fact_coverage(
        "甲类研究生奖学金为7500元。", [rejected.text], [rejected]
    ).uncovered

    supported = _structured_row(
        [["类别"], ["奖励标准（元/人·年）"]],
        ["甲类研究生奖学金", "7500"],
    )
    assert not check_evidence_fact_coverage(
        "甲类研究生奖学金为7500元。", [supported.text], [supported]
    ).uncovered
