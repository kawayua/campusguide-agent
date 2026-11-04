"""Duplicate-collapse replacement IDs must point at retained merge survivors."""

from __future__ import annotations

from datetime import date

from campusguide.domain.models import Citation, Claim
from campusguide.qa.merge import MergeResult, merge_claims

TEXT = "本科生申请奖学金需要提交成绩单。"


def _citation(claim_id: str) -> Citation:
    quote = f"引文-{claim_id}"
    return Citation(
        article_id=f"rule@{claim_id}#第一条",
        quote=quote,
        quote_start=0,
        quote_end=len(quote),
        doc_title="测试办法",
        article_no="第一条",
        effective_from=date(2024, 1, 1),
        source_url="https://example.edu/rules/test",
    )


def _claim(claim_id: str, text: str) -> Claim:
    return Claim(claim_id=claim_id, text=text, citations=[_citation(claim_id)])


def _retained_ids(result: MergeResult) -> set[str]:
    return {claim.claim_id for claim in result.claims}


def _assert_map_targets_retained_claims(result: MergeResult) -> None:
    retained = _retained_ids(result)
    assert set(result.replacement_claim_ids) <= set(result.dropped_claim_ids)
    assert not set(result.replacement_claim_ids) & set(result.replacement_claim_ids.values())
    assert set(result.replacement_claim_ids.values()) <= retained


def test_exact_duplicates_map_directly_to_the_surviving_twin() -> None:
    result = merge_claims(
        [_claim("a", TEXT), _claim("b", TEXT), _claim("c", TEXT)]
    )

    assert [claim.claim_id for claim in result.claims] == ["a"]
    assert result.dropped_claim_ids == ["b", "c"]
    assert result.replacement_claim_ids == {"b": "a", "c": "a"}
    _assert_map_targets_retained_claims(result)


def test_punctuation_equivalent_duplicate_maps_to_the_retained_twin() -> None:
    plain = "本科生申请奖学金需要提交成绩单"

    result = merge_claims([_claim("punctuated", TEXT), _claim("plain", plain)])

    assert [claim.claim_id for claim in result.claims] == ["plain"]
    assert result.dropped_claim_ids == ["punctuated"]
    assert result.replacement_claim_ids == {"punctuated": "plain"}
    _assert_map_targets_retained_claims(result)


def test_complementary_facts_under_a_shared_key_are_not_mapped() -> None:
    original = "本科生申请奖学金需要提交成绩单原件。"
    copy = "本科生申请奖学金需要提交成绩单复印件。"

    result = merge_claims([_claim("original", original), _claim("copy", copy)])

    assert _retained_ids(result) == {"original", "copy"}
    assert result.dropped_claim_ids == []
    assert result.replacement_claim_ids == {}


def test_class_or_version_differences_are_not_mapped() -> None:
    for other in (
        "创新班本科生申请奖学金需要提交成绩单。",
        "2024级本科生申请奖学金需要提交成绩单。",
    ):
        result = merge_claims([_claim("base", TEXT), _claim("other", other)])

        assert _retained_ids(result) == {"base", "other"}
        assert result.dropped_claim_ids == []
        assert result.replacement_claim_ids == {}


def test_stale_missing_and_table_fragment_removals_are_not_mapped() -> None:
    positive = _claim("positive", TEXT)
    stale = _claim("stale", "本科生申请奖学金未提及需要提交成绩单。")
    fragment = _claim("fragment", "| 项目 | 金额 |")

    result = merge_claims([positive, stale, fragment])

    assert [claim.claim_id for claim in result.claims] == ["positive"]
    assert set(result.dropped_claim_ids) == {"stale", "fragment"}
    assert result.replacement_claim_ids == {}
    assert {note.kind for note in result.notes} == {"stale_missing", "table_fragment"}
    _assert_map_targets_retained_claims(result)
