"""Table fact quote convergence and supplement stopping rules."""

from __future__ import annotations

import json
from datetime import date
from types import SimpleNamespace

from campusguide.agents.advisor import Advisor, CitationDraft, ClaimDraft
from campusguide.agents.graph import GraphNodes
from campusguide.agents.plan import Plan, QueryPlanItem, QuestionUnit
from campusguide.agents.providers import FakeChatProvider
from campusguide.agents.quote_repair import (
    QuoteProblem,
    SpanRegistry,
    deterministic_quote_repair,
    find_quote_problems,
)
from campusguide.domain.enums import ArticleStatus, Audience, BlockType
from campusguide.domain.models import (
    Article,
    CellProvenance,
    Citation,
    Claim,
    Evidence,
)
from campusguide.domain.text import normalize_text
from campusguide.retrieval.source_spans import SourceSpan
from campusguide.verification.quotes import locate_quote

BAD_TABLE_QUOTE = "4.专业实践 | 第五学期结束前 | 6"
TYPED_QUOTE = "专业实践6学分"
BAD_TABLE_TEXT = (
    "【表格】培养环节 | 时间安排全日制 非全日制 | 学分 | 备注\n"
    f"{BAD_TABLE_QUOTE}\n"
)
CLAIM_TEXT = "工业工程与管理2025—2026级全日制和非全日制专业实践计6学分。"


def _article(
    *,
    article_id: str = "iem-plan@v2025-2026#part1",
    doc_id: str = "iem-plan",
    version_id: str = "v2025-2026",
    doc_title: str = "工业工程与管理专业学位研究生培养方案（2025—2026级）",
    text: str = BAD_TABLE_TEXT + "培养环节：9学分，其中专业实践6学分，其他3学分\n",
    article_no: str = "三、培养环节及时间安排",
    part_no: int = 1,
    block_kind: str = "text",
    **extra,
) -> Article:
    return Article(
        article_id=article_id,
        doc_id=doc_id,
        version_id=version_id,
        doc_title=doc_title,
        article_no=article_no,
        article_index=1,
        part_no=part_no,
        block_type=BlockType.ARTICLE,
        block_kind=block_kind,
        text=text,
        text_normalized=normalize_text(text),
        audience=[Audience.GRADUATE],
        status=ArticleStatus.ACTIVE,
        effective_from=date(2025, 9, 1),
        source_url="https://example.edu/plan",
        published_at=date(2025, 9, 1),
        issuing_department="研究生院",
        source_sha256="a" * 64,
        **extra,
    )


def _evidence(article: Article, rank: int = 1) -> Evidence:
    return Evidence(article=article, rank=rank)


def _citation(article: Article, quote: str) -> Citation:
    located = locate_quote(article.text, quote)
    assert located is not None
    return Citation(
        article_id=article.article_id,
        quote=quote,
        quote_start=located.start,
        quote_end=located.end,
        doc_title=article.doc_title,
        article_no=article.article_no,
        part_no=article.part_no,
        effective_from=article.effective_from,
        source_url=article.source_url,
    )


def _draft(claim_id: str, text: str, *citations: Citation) -> ClaimDraft:
    return ClaimDraft(
        claim_id=claim_id,
        text=text,
        citations=[
            CitationDraft(article_id=citation.article_id, quote=citation.quote)
            for citation in citations
        ],
    )


def _structured_article(
    *,
    article_id: str = "iem-plan@v2025-2026#row1",
    text: str = "表头：课程名称 | 学分\n行：专业实践 | 6\n",
    headers: list[str] | None = None,
    paths: list[list[str]] | None = None,
    values: tuple[str, ...] = ("专业实践", "6"),
    row_id: str = "iem-plan@v2025-2026#t1#r1",
    row_index: int = 1,
) -> Article:
    headers = headers or ["课程名称", "学分"]
    paths = paths or [["课程名称"], ["学分"]]
    cells = [
        CellProvenance(
            cell_id=f"iem-plan@v2025-2026#t1#r{row_index}c{index}",
            row=row_index,
            col=index,
            text=value,
            origin_row=row_index,
            origin_col=index,
            text_source="pdf",
        )
        for index, value in enumerate(values)
    ]
    return _article(
        article_id=article_id,
        text=text,
        block_kind="table_row",
        table_id="iem-plan@v2025-2026#t1",
        row_id=row_id,
        row_index=row_index,
        table_headers=headers,
        header_paths=paths,
        cell_provenance=cells,
    )


def _flat_registry(article: Article, *extra_spans: SourceSpan) -> SpanRegistry:
    evidence = [_evidence(article)]
    body = SourceSpan(
        "source_1_body",
        article.article_id,
        article.part_no,
        0,
        len(article.text),
        article.text,
        "完整原始片段",
    )
    spans = {body.span_id: body}
    for span in extra_spans:
        spans[span.span_id] = span
    return SpanRegistry(evidence=evidence, spans=spans)


def _problem(claim_id: str = "c2") -> QuoteProblem:
    return QuoteProblem(
        index=0,
        claim_id=claim_id,
        reasons=("claim 的数字/期限未被 quote 覆盖：6学分",),
    )


def _claims_json(
    *,
    claim_id: str = "c2",
    article_id: str = "",
    quote: str = "",
    quote_id: str = "",
    text: str = CLAIM_TEXT,
) -> str:
    citation = (
        {"quote_id": quote_id}
        if quote_id
        else {"article_id": article_id, "quote": quote}
    )
    return json.dumps(
        {
            "claims": [
                {
                    "claim_id": claim_id,
                    "text": text,
                    "kind": "other",
                    "citations": [citation],
                }
            ]
        },
        ensure_ascii=False,
    )


def test_typed_same_entity_span_is_added_without_dropping_timing_quote() -> None:
    article = _article()
    evidence = [_evidence(article)]
    draft = _draft("c2", CLAIM_TEXT, _citation(article, BAD_TABLE_QUOTE))
    problems = find_quote_problems([draft], evidence)
    assert problems and "6学分" in problems[0].reasons[0]

    result = deterministic_quote_repair([draft], problems, evidence)

    assert result.changed
    repaired = result.drafts[0]
    assert len(repaired.citations) == 2
    assert any(citation.quote == BAD_TABLE_QUOTE for citation in repaired.citations)
    assert any(TYPED_QUOTE in citation.quote for citation in repaired.citations)
    assert find_quote_problems(result.drafts, evidence) == []


def test_mixed_deadline_and_credits_keeps_both_citations() -> None:
    claim_text = (
        "工业工程与管理2025—2026级全日制和非全日制专业实践"
        "在第五学期结束前完成，计6学分。"
    )
    article = _article()
    evidence = [_evidence(article)]
    draft = _draft("c2", claim_text, _citation(article, BAD_TABLE_QUOTE))
    problems = find_quote_problems([draft], evidence)

    result = deterministic_quote_repair([draft], problems, evidence)

    assert result.changed
    repaired = result.drafts[0]
    assert len(repaired.citations) >= 2
    assert any("第五学期" in citation.quote for citation in repaired.citations)
    assert any(TYPED_QUOTE in citation.quote for citation in repaired.citations)
    assert find_quote_problems(result.drafts, evidence) == []


def test_advisor_hook_uses_deterministic_repair_without_model_call() -> None:
    article = _article()
    evidence = [_evidence(article)]
    draft = _draft("c2", CLAIM_TEXT, _citation(article, BAD_TABLE_QUOTE))
    registry = SpanRegistry.build(evidence)
    bad_id = registry.ids_for_quote(
        article.article_id, BAD_TABLE_QUOTE, register=True
    )[0]
    provider = FakeChatProvider([_claims_json(quote_id=bad_id)])
    advisor = Advisor(provider)

    result = advisor._repair_quotes([], "", [draft], evidence, [], json_repaired=False)

    assert len(provider.calls) == 1
    assert result.quote_repair_attempted
    assert result.quote_repaired
    assert result.quote_repair_remaining == []
    assert len(result.drafts[0].citations) == 2
    assert any(
        TYPED_QUOTE in citation.quote for citation in result.drafts[0].citations
    )


def test_model_id_repair_success_stays_two_calls_with_correct_quote() -> None:
    wrong = _article(
        article_id="iem-plan@v2025-2026#flat",
        text=BAD_TABLE_TEXT,
    )
    correct = _article(
        article_id="iem-plan@v2025-2026#prose",
        part_no=2,
        text="培养环节：9学分，其中专业实践6学分，其他3学分",
    )
    evidence = [_evidence(wrong, 1), _evidence(correct, 2)]
    provider = FakeChatProvider(
        [
            _claims_json(article_id=wrong.article_id, quote=BAD_TABLE_QUOTE),
            _claims_json(quote_id="source_2_body"),
        ]
    )

    result = Advisor(provider).generate(
        "工业工程与管理专业实践计多少学分？", evidence
    )

    assert len(provider.calls) == 2
    assert result.quote_repair_attempted
    assert result.quote_repaired
    assert result.quote_repair_remaining == []
    assert len(result.drafts) == 1
    assert len(result.drafts[0].citations) == 1
    citation = result.drafts[0].citations[0]
    assert citation.article_id == correct.article_id
    assert citation.quote == correct.text


def test_no_safe_proof_keeps_quote_problem_after_model_attempt() -> None:
    wrong = _article(
        article_id="iem-plan@v2025-2026#flat",
        text=BAD_TABLE_TEXT,
    )
    evidence = [_evidence(wrong)]
    registry = SpanRegistry.build(evidence)
    bad_id = registry.ids_for_quote(
        wrong.article_id, BAD_TABLE_QUOTE, register=True
    )[0]
    provider = FakeChatProvider(
        [
            _claims_json(article_id=wrong.article_id, quote=BAD_TABLE_QUOTE),
            _claims_json(quote_id=bad_id),
        ]
    )

    result = Advisor(provider).generate(
        "工业工程与管理专业实践计多少学分？", evidence
    )

    assert len(provider.calls) == 2
    assert result.quote_repair_remaining == ["c2"]
    assert find_quote_problems(result.drafts, evidence)


def test_year_range_separators_are_canonicalized() -> None:
    for separator in ("–", "—", "－", "～", "至"):
        source = _article(
            article_id="iem-plan@v2025-2026#flat",
            text=BAD_TABLE_TEXT,
        )
        candidate = _article(
            article_id=f"iem-plan@v2025-2026#scope-{ord(separator)}",
            part_no=2,
            text=(
                f"2025{separator}2026级全日制和非全日制"
                "专业实践6学分。\n"
            ),
        )
        evidence = [_evidence(source, 1), _evidence(candidate, 2)]
        draft = _draft("c2", CLAIM_TEXT, _citation(source, BAD_TABLE_QUOTE))

        result = deterministic_quote_repair([draft], [_problem()], evidence)

        assert result.changed, separator


def test_year_range_endpoint_or_cohort_changes_fail_closed() -> None:
    for candidate_text in (
        "2025–2027级全日制和非全日制专业实践6学分。",
        "2024–2026级全日制和非全日制专业实践6学分。",
        "2025–2026届全日制和非全日制专业实践6学分。",
    ):
        source = _article(
            article_id="iem-plan@v2025-2026#flat",
            text=BAD_TABLE_TEXT,
        )
        candidate = _article(
            article_id="iem-plan@v2025-2026#scope-wrong",
            part_no=2,
            text=candidate_text,
        )
        evidence = [_evidence(source, 1), _evidence(candidate, 2)]
        draft = _draft("c2", CLAIM_TEXT, _citation(source, BAD_TABLE_QUOTE))

        result = deterministic_quote_repair([draft], [_problem()], evidence)

        assert not result.changed, candidate_text


def test_scope_range_endash_metadata_keeps_nd19_unscoped_prose() -> None:
    source = _article(
        article_id="iem-plan@v2025-2026#part1",
        doc_title="工业工程与管理专业学位研究生培养方案（2025–2026级）",
        text=BAD_TABLE_TEXT,
    )
    prose = _article(
        article_id="iem-plan@v2025-2026#prose",
        doc_title="工业工程与管理专业学位研究生培养方案（2025–2026级）",
        part_no=2,
        text="培养环节：9学分，其中专业实践6学分，其他3学分\n",
    )
    evidence = [_evidence(source, 1), _evidence(prose, 2)]
    draft = _draft("c2", CLAIM_TEXT, _citation(source, BAD_TABLE_QUOTE))

    result = deterministic_quote_repair([draft], [_problem()], evidence)

    assert result.changed
    assert any(
        TYPED_QUOTE in citation.quote for citation in result.drafts[0].citations
    )


def test_multi_occurrence_repair_requires_each_fact_to_be_proven() -> None:
    local_flat_text_4 = BAD_TABLE_TEXT.replace("6", "4")
    local_flat_quote_4 = BAD_TABLE_QUOTE.replace("6", "4")
    assert local_flat_quote_4 in local_flat_text_4
    cases = (
        (
            "全日制专业实践4学分，非全日制专业实践4学分。",
            (
                "全日制专业实践4学分。非全日制专业实践8学分。",
                "全日制专业实践8学分。非全日制专业实践4学分。",
            ),
        ),
        (
            "科研训练4学分，专业实践4学分。",
            (
                "科研训练4学分。专业实践8学分。",
                "科研训练8学分。专业实践4学分。",
            ),
        ),
    )
    for claim_text, source_texts in cases:
        for source_text in source_texts:
            source = _article(
                article_id="iem-plan@v2025-2026#flat",
                text=local_flat_text_4,
            )
            candidate = _article(
                article_id="iem-plan@v2025-2026#multi",
                part_no=2,
                text=source_text,
            )
            evidence = [_evidence(source, 1), _evidence(candidate, 2)]
            draft = _draft(
                "c2", claim_text, _citation(source, local_flat_quote_4)
            )
            problems = find_quote_problems([draft], evidence)
            assert problems

            result = deterministic_quote_repair([draft], problems, evidence)

            assert not result.changed, source_text
            assert result.drafts[0] is draft
            assert find_quote_problems(result.drafts, evidence) == problems


def test_multi_occurrence_repair_accepts_window_proving_every_fact() -> None:
    local_flat_text_4 = BAD_TABLE_TEXT.replace("6", "4")
    local_flat_quote_4 = BAD_TABLE_QUOTE.replace("6", "4")
    assert local_flat_quote_4 in local_flat_text_4
    cases = (
        (
            "全日制专业实践4学分，非全日制专业实践4学分。",
            "全日制专业实践4学分。非全日制专业实践4学分。",
            ("全日制专业实践4学分", "非全日制专业实践4学分"),
        ),
        (
            "科研训练4学分，专业实践4学分。",
            "科研训练4学分。专业实践4学分。",
            ("科研训练4学分", "专业实践4学分"),
        ),
    )
    for claim_text, source_text, markers in cases:
        source = _article(
            article_id="iem-plan@v2025-2026#flat",
            text=local_flat_text_4,
        )
        candidate = _article(
            article_id="iem-plan@v2025-2026#multi",
            part_no=2,
            text=source_text,
        )
        evidence = [_evidence(source, 1), _evidence(candidate, 2)]
        draft = _draft("c2", claim_text, _citation(source, local_flat_quote_4))
        problems = find_quote_problems([draft], evidence)
        assert problems

        result = deterministic_quote_repair([draft], problems, evidence)

        assert result.changed, source_text
        assert any(
            all(marker in citation.quote for marker in markers)
            for citation in result.drafts[0].citations
        )
        assert find_quote_problems(result.drafts, evidence) == []


def test_bare_table_cell_alone_fails_closed() -> None:
    article = _article(text=BAD_TABLE_TEXT)
    evidence = [_evidence(article)]
    draft = _draft("c2", CLAIM_TEXT, _citation(article, BAD_TABLE_QUOTE))
    problems = find_quote_problems([draft], evidence)
    assert problems

    result = deterministic_quote_repair([draft], problems, evidence)

    assert not result.changed
    assert result.drafts[0] is draft
    assert find_quote_problems(result.drafts, evidence) == problems


def test_non_table_quote_stays_on_model_repair_path() -> None:
    article = _article()
    evidence = [_evidence(article)]
    partial_quote = "培养环节：9学分"
    draft = _draft("c2", CLAIM_TEXT, _citation(article, partial_quote))
    problems = find_quote_problems([draft], evidence)
    assert problems and "6学分" in problems[0].reasons[0]

    result = deterministic_quote_repair([draft], problems, evidence)

    assert not result.changed
    assert result.drafts[0] is draft


def test_numeric_distractor_course_with_practice_name_is_rejected() -> None:
    article = _article(text="社会实践课程6学分。\n" + BAD_TABLE_TEXT)
    evidence = [_evidence(article)]
    draft = _draft("c2", CLAIM_TEXT, _citation(article, BAD_TABLE_QUOTE))
    problems = find_quote_problems([draft], evidence)

    result = deterministic_quote_repair([draft], problems, evidence)

    assert not result.changed


def test_total_credit_amount_cannot_stand_for_practice_credits() -> None:
    article = _article(text="专业实践总学分9学分。\n" + BAD_TABLE_TEXT)
    evidence = [_evidence(article)]
    draft = _draft("c2", CLAIM_TEXT, _citation(article, BAD_TABLE_QUOTE))
    problems = find_quote_problems([draft], evidence)

    result = deterministic_quote_repair([draft], problems, evidence)

    assert not result.changed


def test_cross_entity_span_is_rejected() -> None:
    article = _article(text=BAD_TABLE_TEXT + "社会实践课程6学分。\n")
    evidence = [_evidence(article)]
    candidate = SourceSpan(
        "source_1_row_1",
        article.article_id,
        article.part_no,
        article.text.index("社会实践课程"),
        article.text.index("社会实践课程") + len("社会实践课程6学分"),
        "社会实践课程6学分",
        "课程行",
    )
    registry = _flat_registry(article, candidate)
    draft = _draft("c2", CLAIM_TEXT, _citation(article, BAD_TABLE_QUOTE))

    result = deterministic_quote_repair(
        [draft], [_problem()], evidence, registry=registry
    )

    assert not result.changed


def test_cross_class_span_is_rejected() -> None:
    source = _article(text=BAD_TABLE_TEXT)
    wrong_class = _article(
        article_id="iem-plan@v2025-2026#part2",
        part_no=2,
        text="2025级非全日制专业实践6学分。\n",
    )
    evidence = [_evidence(source, 1), _evidence(wrong_class, 2)]
    claim_text = "2025级全日制专业实践计6学分。"
    draft = _draft("c2", claim_text, _citation(source, BAD_TABLE_QUOTE))

    result = deterministic_quote_repair([draft], [_problem()], evidence)

    assert not result.changed


def test_cross_year_span_is_rejected() -> None:
    source = _article(text=BAD_TABLE_TEXT)
    wrong_year = _article(
        article_id="iem-plan@v2025-2026#part2",
        part_no=2,
        text="2026级全日制专业实践6学分。\n",
    )
    evidence = [_evidence(source, 1), _evidence(wrong_year, 2)]
    claim_text = "2025级全日制专业实践计6学分。"
    draft = _draft("c2", claim_text, _citation(source, BAD_TABLE_QUOTE))

    result = deterministic_quote_repair([draft], [_problem()], evidence)

    assert not result.changed


def test_cross_version_span_is_rejected() -> None:
    source = _article(text=BAD_TABLE_TEXT)
    other_version = _article(
        article_id="iem-plan@v2026-2027#part1",
        version_id="v2026-2027",
        text="培养环节：其中专业实践6学分。\n",
    )
    evidence = [_evidence(source, 1), _evidence(other_version, 2)]
    draft = _draft("c2", CLAIM_TEXT, _citation(source, BAD_TABLE_QUOTE))

    result = deterministic_quote_repair([draft], [_problem()], evidence)

    assert not result.changed


def test_mixed_cited_families_are_left_to_verification() -> None:
    first = _article()
    second = _article(
        article_id="other-plan@v1#part1",
        doc_id="other-plan",
        version_id="v1",
        text=BAD_TABLE_TEXT,
    )
    evidence = [_evidence(first, 1), _evidence(second, 2)]
    draft = _draft(
        "c2",
        CLAIM_TEXT,
        _citation(first, BAD_TABLE_QUOTE),
        _citation(second, BAD_TABLE_QUOTE),
    )
    problems = find_quote_problems([draft], evidence)
    assert problems

    result = deterministic_quote_repair([draft], problems, evidence)

    assert not result.changed


def test_structured_row_uses_column_provenance_for_credits() -> None:
    article = _structured_article()
    source = _article(article_id="iem-plan@v2025-2026#part1", text=BAD_TABLE_TEXT)
    evidence = [_evidence(source, 1), _evidence(article, 2)]
    claim_text = "工业工程与管理2025—2026级专业实践计6学分。"
    draft = _draft("c2", claim_text, _citation(source, BAD_TABLE_QUOTE))
    problems = find_quote_problems([draft], evidence)
    assert problems

    result = deterministic_quote_repair([draft], problems, evidence)

    assert result.changed
    assert any(citation.quote.startswith("行：") for citation in result.drafts[0].citations)


def test_structured_row_does_not_borrow_another_rows_credit() -> None:
    wrong_entity = _structured_article(
        article_id="iem-plan@v2025-2026#row-a",
        row_id="iem-plan@v2025-2026#t1#ra",
        text="表头：课程名称 | 学分\n行：社会实践 | 6\n",
        values=("社会实践", "6"),
    )
    wrong_value = _structured_article(
        article_id="iem-plan@v2025-2026#row-b",
        row_id="iem-plan@v2025-2026#t1#rb",
        text="表头：课程名称 | 学分\n行：专业实践 | 9\n",
        values=("专业实践", "9"),
    )
    source = _article(article_id="iem-plan@v2025-2026#part1", text=BAD_TABLE_TEXT)
    evidence = [
        _evidence(source, 1),
        _evidence(wrong_entity, 2),
        _evidence(wrong_value, 3),
    ]
    draft = _draft("c2", CLAIM_TEXT, _citation(source, BAD_TABLE_QUOTE))

    result = deterministic_quote_repair([draft], [_problem()], evidence)

    assert not result.changed


def test_structured_row_does_not_borrow_another_columns_number() -> None:
    article = _structured_article(
        text="表头：课程名称 | 学分 | 总学时\n行：专业实践 | 6 | 9\n",
        headers=["课程名称", "学分", "总学时"],
        paths=[["课程名称"], ["学分"], ["总学时"]],
        values=("专业实践", "6", "9"),
    )
    source = _article(article_id="iem-plan@v2025-2026#part1", text=BAD_TABLE_TEXT)
    evidence = [_evidence(source, 1), _evidence(article, 2)]
    claim_text = "工业工程与管理2025—2026级专业实践总学时6学时。"
    draft = _draft("c2", claim_text, _citation(source, BAD_TABLE_QUOTE))

    result = deterministic_quote_repair([draft], [_problem()], evidence)

    assert not result.changed


def test_unreliable_or_missing_header_row_is_rejected() -> None:
    source = _article(article_id="iem-plan@v2025-2026#part1", text=BAD_TABLE_TEXT)
    draft = _draft("c2", CLAIM_TEXT, _citation(source, BAD_TABLE_QUOTE))

    missing_header = _structured_article(
        text="行：专业实践 | 6\n",
        headers=["课程名称", "学分"],
        paths=[["课程名称"], ["学分"]],
    )
    evidence = [_evidence(source, 1), _evidence(missing_header, 2)]

    result = deterministic_quote_repair([draft], [_problem()], evidence)

    assert not result.changed

    unreliable = _structured_article(
        article_id="iem-plan@v2025-2026#row2",
        text="表头：课程名称 | 学分\n行：专业实践 | 6 | 9\n",
    )
    evidence = [_evidence(source, 1), _evidence(unreliable, 2)]

    result = deterministic_quote_repair([draft], [_problem()], evidence)

    assert not result.changed


class _Audit:
    def __init__(self, missing: list[str]) -> None:
        self.missing = missing
        self.unavailable = False
        self.units = [
            {
                "unit_id": "u1",
                "status": "generation_missing",
                "missing_items": list(missing),
                "claim_ids": ["c1"],
            }
        ]
        self.error = ""
        self.error_kind = None
        self.error_attempts = 0
        self.prompt_tokens = 0
        self.completion_tokens = 0
        self.latency_ms = 0


class _CoverageChecker:
    def __init__(self, audit: _Audit) -> None:
        self.audit = audit

    def check(self, *args, **kwargs) -> _Audit:
        return self.audit


class _EmptyRetriever:
    def retrieve(self, plan, base) -> SimpleNamespace:
        return SimpleNamespace(evidence=[], warnings=[])


def _graph_state(*, claims: list[Claim] | None = None) -> dict:
    unit = QuestionUnit(
        unit_id="u1",
        original_span="工业工程与管理专业实践学分",
        subject="工业工程与管理",
        requested_aspects=["学分"],
        cohort=2026,
    )
    claim = Claim(
        claim_id="c1",
        text="工业工程与管理专业实践计6学分。",
        citations=[_citation(_article(), TYPED_QUOTE)],
    )
    return {
        "question": "工业工程与管理专业实践计多少学分？",
        "plan": Plan(
            queries=[
                QueryPlanItem(
                    query_id="q1",
                    text="工业工程与管理 专业实践 学分",
                    unit_ids=["u1"],
                )
            ],
            question_units=[unit],
        ),
        "claims": claims if claims is not None else [claim],
        "evidence": [_evidence(_article())],
        "unit_coverage": [
            {
                "unit_id": "u1",
                "status": "generation_missing",
                "missing_items": ["培养环节学分"],
                "claim_ids": ["c1"],
            }
        ],
        "coverage_missing": ["培养环节学分"],
        "coverage_repair_count": 0,
        "trace": [],
        "block_types": [BlockType.ARTICLE],
        "as_of": date(2026, 1, 1),
    }


def _nodes(missing: list[str]) -> GraphNodes:
    return GraphNodes(
        planner=object(),
        retriever=_EmptyRetriever(),
        advisor=object(),
        verifier=object(),
        catalog=object(),
        coverage_checker=_CoverageChecker(_Audit(missing)),
    )


def test_failed_repeated_supplement_converges_to_partial() -> None:
    nodes = _nodes(["培养环节学分"])
    state = _graph_state()
    supplement_update = nodes.supplement(state)
    state = {**state, **supplement_update}
    assert state["coverage_repair_count"] == 1

    coverage_update = nodes.coverage(state)

    assert coverage_update["coverage_checked"] is True
    assert coverage_update["coverage_repair_pending"] is False
    assert coverage_update["coverage_missing"] == ["培养环节学分"]
    assert coverage_update["trace"][-1]["repair_converged"] is True


def test_new_supported_claim_keeps_the_next_round_available() -> None:
    nodes = _nodes(["培养环节学分"])
    state = _graph_state()
    state = {**state, **nodes.supplement(state)}
    progress_claim = Claim(
        claim_id="c2",
        text="专业实践在第五学期结束前完成。",
        citations=[_citation(_article(), BAD_TABLE_QUOTE)],
    )
    state["claims"] = [*state["claims"], progress_claim]

    coverage_update = nodes.coverage(state)

    assert coverage_update["coverage_repair_pending"] is True
    assert coverage_update["trace"][-1]["repair_converged"] is False


def test_changed_missing_set_or_evidence_keeps_next_round_available() -> None:
    state = _graph_state()
    supplement_update = _nodes(["培养环节学分"]).supplement(state)
    state = {**state, **supplement_update}

    changed_missing = _nodes(["另一缺项"])
    missing_update = changed_missing.coverage(state)
    assert missing_update["coverage_repair_pending"] is True

    extra_evidence = _evidence(
        _article(article_id="iem-plan@v2025-2026#part2", part_no=2),
        rank=2,
    )
    state["evidence"] = [*state["evidence"], extra_evidence]
    evidence_update = _nodes(["培养环节学分"]).coverage(state)
    assert evidence_update["coverage_repair_pending"] is True


def _repair_bare_source(claim_text: str, candidate_text: str):
    source = _article(
        article_id="iem-plan@v2025-2026#flat",
        text="专业实践 | 第三学期 | 4 |",
    )
    candidate = _article(
        article_id="iem-plan@v2025-2026#prose",
        text=candidate_text,
    )
    evidence = [_evidence(source, 1), _evidence(candidate, 2)]
    draft = _draft("c2", claim_text, _citation(source, source.text))
    return deterministic_quote_repair([draft], [_problem()], evidence)


def test_conflicting_class_prefix_fails_closed() -> None:
    result = _repair_bare_source(
        "创新班专业实践计4学分。",
        "普通班专业实践4学分。",
    )

    assert not result.changed


def test_conflicting_region_prefix_fails_closed() -> None:
    result = _repair_bare_source(
        "本省专业实践计4学分。",
        "外省专业实践4学分。",
    )

    assert not result.changed


def test_mixed_body_wrong_mode_does_not_borrow_other_clause() -> None:
    for candidate_text in (
        "全日制专业实践4学分。非全日制专业实践8学分。",
        "非全日制专业实践8学分。全日制专业实践4学分。",
    ):
        result = _repair_bare_source(
            "非全日制专业实践计4学分。",
            candidate_text,
        )
        assert not result.changed, candidate_text


def test_mixed_body_wrong_year_does_not_borrow_other_clause() -> None:
    for candidate_text in (
        "2028级专业实践4学分。2027级专业实践8学分。",
        "2027级专业实践8学分。2028级专业实践4学分。",
    ):
        result = _repair_bare_source(
            "2027级专业实践计4学分。",
            candidate_text,
        )
        assert not result.changed, candidate_text


def test_mixed_body_same_occurrence_scope_is_accepted() -> None:
    result = _repair_bare_source(
        "非全日制专业实践计4学分。",
        "非全日制专业实践4学分。全日制专业实践8学分。",
    )

    assert result.changed
    assert any("4学分" in citation.quote for citation in result.drafts[0].citations)


def test_unscoped_typed_prose_uses_cited_section_scope() -> None:
    source = _article(
        article_id="iem-plan@v2025-2026#part1",
        text=BAD_TABLE_TEXT,
    )
    prose = _article(
        article_id="iem-plan@v2025-2026#prose",
        part_no=2,
        text="培养环节：9学分，其中专业实践6学分，其他3学分\n",
    )
    evidence = [_evidence(source, 1), _evidence(prose, 2)]
    draft = _draft("c2", CLAIM_TEXT, _citation(source, BAD_TABLE_QUOTE))

    result = deterministic_quote_repair([draft], [_problem()], evidence)

    assert result.changed
    assert any(
        TYPED_QUOTE in citation.quote for citation in result.drafts[0].citations
    )


class _AddingRetriever:
    def __init__(self, extra: Evidence) -> None:
        self.extra = extra
        self.calls = 0

    def retrieve(self, plan, base) -> SimpleNamespace:
        self.calls += 1
        return SimpleNamespace(evidence=[self.extra], warnings=[])


def test_supplement_that_adds_evidence_keeps_next_round_available() -> None:
    retriever = _AddingRetriever(
        _evidence(
            _article(article_id="iem-plan@v2025-2026#added", part_no=2),
            rank=2,
        )
    )
    nodes = GraphNodes(
        planner=object(),
        retriever=retriever,
        advisor=object(),
        verifier=object(),
        catalog=object(),
        coverage_checker=_CoverageChecker(_Audit(["培养环节学分"])),
    )
    state = _graph_state()

    supplement_update = nodes.supplement(state)

    assert retriever.calls == 1
    assert supplement_update["coverage_repair_last"]["added_evidence"] == 1
    assert any(
        item.article.article_id == "iem-plan@v2025-2026#added"
        for item in supplement_update["evidence"]
    )
    state = {**state, **supplement_update}

    coverage_update = nodes.coverage(state)

    assert coverage_update["coverage_repair_pending"] is True
    assert coverage_update["trace"][-1]["repair_converged"] is False
