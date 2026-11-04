"""Coverage repair rounds must mint claim IDs outside every preserved namespace.

报告 A1/A6：第二轮补答沿用 ``supplement_1`` 时，ClaimValidator 会把新结论当作
``DUPLICATE_CLAIM_ID`` 丢弃。这里直接验证生产者两轮产出的 ID、跨轮保留的
正文/引用，以及补查轮次与改写预算没有被改动。
"""

from __future__ import annotations

from datetime import date

from campusguide.agents.advisor import AdvisorResult, CitationDraft, ClaimDraft
from campusguide.agents.graph import MAX_COVERAGE_REPAIR_ROUNDS, GraphNodes
from campusguide.agents.plan import Plan
from campusguide.domain.models import Citation, Claim, Evidence
from campusguide.verification.claims import ClaimCheck, ClaimValidator
from tests.w2_helpers import make_article, make_evidence

BASE_QUOTE = "既有结论已经核实，保留原有引用。"
ROUND1_QUOTE = "第一轮补查新增一条可核对结论。"
ROUND2_QUOTE = "第二轮补查又新增一条可核对结论。"
FRESH_A = "补查新增的甲项结论已经核对。"
FRESH_B = "补查新增的乙项结论已经核对。"
FRESH_NEW = "补查新增的丙项结论已经核对。"


def _source(*quotes: str) -> tuple[Evidence, dict[str, Citation]]:
    text = "".join(quotes)
    article = make_article(
        article_id="supplement-source@v2021#第一条",
        doc_id="supplement-source",
        doc_title="补查来源",
        text=text,
    )
    citations: dict[str, Citation] = {}
    cursor = 0
    for quote in quotes:
        start = text.index(quote, cursor)
        end = start + len(quote)
        citations[quote] = Citation(
            article_id=article.article_id,
            quote=quote,
            quote_start=start,
            quote_end=end,
            doc_title=article.doc_title,
            article_no=article.article_no,
            part_no=article.part_no,
            effective_from=article.effective_from,
            source_url=article.source_url,
        )
        cursor = end
    return make_evidence(article), citations


def _claim(claim_id: str, quote: str, citation: Citation) -> Claim:
    return Claim(claim_id=claim_id, text=quote, citations=[citation])


def _draft(claim_id: str, quote: str, citation: Citation) -> ClaimDraft:
    return ClaimDraft(
        claim_id=claim_id,
        text=quote,
        kind="other",
        citations=[CitationDraft(article_id=citation.article_id, quote=citation.quote)],
    )


class _StubAdvisor:
    def __init__(self, drafts: list[ClaimDraft]) -> None:
        self._drafts = drafts
        self.calls = 0

    def generate(self, *args, **kwargs) -> AdvisorResult:
        self.calls += 1
        return AdvisorResult(drafts=[draft.model_copy(deep=True) for draft in self._drafts])


def _nodes(drafts: list[ClaimDraft]) -> GraphNodes:
    nodes = object.__new__(GraphNodes)
    nodes._advisor = _StubAdvisor(drafts)
    nodes._max_revise = 2
    return nodes


def _state(
    evidence: Evidence,
    claims: list[Claim],
    drafts: list[ClaimDraft],
    repair_round: int,
) -> dict:
    return {
        "question": "补查遗漏事项",
        "evidence": [evidence],
        "claims": list(claims),
        "repair_verified_claims": list(claims),
        "drafts": list(drafts),
        "coverage_repair_count": repair_round,
        "coverage_missing": ["遗漏事项"],
        "revision_count": 0,
        "failed_claim_ids": [],
    }


def _codes(check: ClaimCheck) -> list[str]:
    return [issue.code for issue in check.issues]


def test_two_supplement_rounds_mint_collision_free_ids() -> None:
    evidence, cites = _source(BASE_QUOTE, ROUND1_QUOTE, ROUND2_QUOTE)
    base = _claim("c1", BASE_QUOTE, cites[BASE_QUOTE])

    nodes = _nodes([_draft("c1", ROUND1_QUOTE, cites[ROUND1_QUOTE])])
    first = nodes.advisor(_state(evidence, [base], [], 1))
    assert [draft.claim_id for draft in first["drafts"]] == ["c1", "supplement_1_1"]
    check1 = ClaimValidator().check(first["drafts"], [evidence])
    assert [claim.claim_id for claim in check1.claims] == ["c1", "supplement_1_1"]
    assert "DUPLICATE_CLAIM_ID" not in _codes(check1)
    assert "revision_count" not in first

    nodes = _nodes([_draft("c1", ROUND2_QUOTE, cites[ROUND2_QUOTE])])
    second = nodes.advisor(_state(evidence, check1.claims, first["drafts"], 2))
    assert [draft.claim_id for draft in second["drafts"]] == [
        "c1",
        "supplement_1_1",
        "supplement_2_1",
    ]
    check2 = ClaimValidator().check(second["drafts"], [evidence])
    assert [claim.claim_id for claim in check2.claims] == [
        "c1",
        "supplement_1_1",
        "supplement_2_1",
    ]
    assert "DUPLICATE_CLAIM_ID" not in _codes(check2)
    assert "revision_count" not in second
    # 旧 ID、正文与引用原样保留，只有新结论换号。
    assert second["drafts"][0].text == BASE_QUOTE
    assert second["drafts"][1].text == ROUND1_QUOTE
    assert second["drafts"][1].citations[0].quote == ROUND1_QUOTE


def test_fresh_id_skips_ids_reserved_outside_repair_claims() -> None:
    evidence, cites = _source(
        BASE_QUOTE, ROUND1_QUOTE, ROUND2_QUOTE, FRESH_NEW
    )
    kept = [
        _claim("c1", BASE_QUOTE, cites[BASE_QUOTE]),
        _claim("supplement_1_1", ROUND1_QUOTE, cites[ROUND1_QUOTE]),
    ]
    reserved_elsewhere = _claim("supplement_1_2", ROUND2_QUOTE, cites[ROUND2_QUOTE])
    nodes = _nodes([_draft("c9", FRESH_NEW, cites[FRESH_NEW])])
    state = _state(evidence, kept, [], 1)
    state["claims"] = [*kept, reserved_elsewhere]

    update = nodes.advisor(state)
    assert update["drafts"][-1].claim_id == "supplement_1_3"
    assert update["drafts"][-1].text == FRESH_NEW
    check = ClaimValidator().check(update["drafts"], [evidence])
    assert [claim.claim_id for claim in check.claims] == [
        "c1",
        "supplement_1_1",
        "supplement_1_3",
    ]
    assert "DUPLICATE_CLAIM_ID" not in _codes(check)


def test_multiple_fresh_claims_in_one_round_get_unique_ids() -> None:
    evidence, cites = _source(BASE_QUOTE, ROUND1_QUOTE, FRESH_A, FRESH_B)
    kept = [
        _claim("c1", BASE_QUOTE, cites[BASE_QUOTE]),
        _claim("supplement_1_1", ROUND1_QUOTE, cites[ROUND1_QUOTE]),
    ]
    nodes = _nodes(
        [
            _draft("c1", FRESH_A, cites[FRESH_A]),
            # 模型复述已核实内容时不应重复产出。
            _draft("c2", BASE_QUOTE, cites[BASE_QUOTE]),
            _draft("c3", FRESH_B, cites[FRESH_B]),
        ]
    )
    update = nodes.advisor(_state(evidence, kept, [], 2))
    assert [draft.claim_id for draft in update["drafts"]] == [
        "c1",
        "supplement_1_1",
        "supplement_2_1",
        "supplement_2_2",
    ]
    assert [draft.text for draft in update["drafts"]] == [
        BASE_QUOTE,
        ROUND1_QUOTE,
        FRESH_A,
        FRESH_B,
    ]
    check = ClaimValidator().check(update["drafts"], [evidence])
    assert len(check.claims) == 4
    assert "DUPLICATE_CLAIM_ID" not in _codes(check)


def test_supplement_path_keeps_round_cap_and_revise_budget() -> None:
    class NoRetrieval:
        def retrieve(self, *args, **kwargs):
            raise AssertionError("没有遗漏查询时不应调用检索")

    nodes = object.__new__(GraphNodes)
    nodes._retriever = NoRetrieval()
    nodes._today = date(2026, 11, 2)
    nodes._max_evidence = 4
    nodes._token_budget = 1000
    state = {"plan": Plan(), "unit_coverage": [], "claims": [], "evidence": []}

    first = nodes.supplement(state)
    assert first["coverage_repair_count"] == 1
    assert first["failed_claim_ids"] == []
    assert "revision_count" not in first

    second = nodes.supplement({**state, **first})
    assert second["coverage_repair_count"] == 2
    assert "revision_count" not in second

    stopped = nodes.supplement({**state, **first, **second})
    assert stopped == {"coverage_repair_pending": False}
    assert MAX_COVERAGE_REPAIR_ROUNDS == 2
