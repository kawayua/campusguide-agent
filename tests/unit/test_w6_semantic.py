from __future__ import annotations

import json

from campusguide.agents.providers import ChatProviderError, FakeChatProvider
from campusguide.domain.enums import BlockType
from campusguide.domain.models import Citation, Claim, Evidence
from campusguide.verification.semantic import (
    SEMANTIC_PROMPT_VERSION,
    SemanticLabel,
    SemanticVerifier,
)
from tests.w6_helpers import article


def make_claim(claim_id: str = "c1", text: str = "国家奖学金要求二年级及以上。") -> Claim:
    item = article("national-aid-undergrad@v2021#第九条")
    citation = Citation(
        article_id=item.article_id,
        quote=item.text[:60],
        quote_start=0,
        quote_end=60,
        doc_title=item.doc_title,
        article_no=item.article_no,
        part_no=item.part_no,
        block_type=BlockType.ARTICLE,
        effective_from=item.effective_from,
        date_confidence=item.date_confidence,
        source_url=item.source_url,
    )
    return Claim(claim_id=claim_id, text=text, citations=[citation])


def make_evidence() -> list[Evidence]:
    return [Evidence(article=article("national-aid-undergrad@v2021#第九条"))]


def labels_json(*pairs: tuple[str, str]) -> str:
    return json.dumps(
        {
            "labels": [
                {"claim_id": claim_id, "label": label, "detail": "d"} for claim_id, label in pairs
            ]
        },
        ensure_ascii=False,
    )


def test_four_labels_and_provider_isolation() -> None:
    response = labels_json(
        ("c1", "supported"),
        ("c2", "partially_supported"),
        ("c3", "contradicted"),
        ("c4", "insufficient"),
    )
    provider = FakeChatProvider([response])
    verifier = SemanticVerifier(provider)
    claims = [make_claim(f"c{index}") for index in range(1, 5)]
    report = verifier.check(claims, make_evidence())
    assert report.unavailable is False
    assert report.labels == {
        "c1": SemanticLabel.SUPPORTED,
        "c2": SemanticLabel.PARTIALLY_SUPPORTED,
        "c3": SemanticLabel.CONTRADICTED,
        "c4": SemanticLabel.INSUFFICIENT,
    }
    assert report.contradicted_ids == ["c3"]
    assert set(report.weak_ids) == {"c2", "c4"}
    # 输入只含 claim+quote，不含用户隐私/Advisor 推理。
    user_message = provider.calls[0][-1]["content"]
    assert "国家奖学金要求二年级及以上" in user_message
    assert "学号" not in user_message
    assert "Advisor" not in user_message


def test_prompt_version_and_temperature_zero() -> None:
    provider = FakeChatProvider([labels_json(("c1", "supported"))])
    SemanticVerifier(provider).check([make_claim()], make_evidence())
    assert SEMANTIC_PROMPT_VERSION == "w6b-semantic-2026-10-25"
    assert provider.options[0]["temperature"] == 0.0


def test_invalid_json_is_repaired_once() -> None:
    provider = FakeChatProvider(["not json", labels_json(("c1", "supported"))])
    report = SemanticVerifier(provider).check([make_claim()], make_evidence())
    assert report.unavailable is False
    assert report.labels["c1"] is SemanticLabel.SUPPORTED
    assert len(provider.calls) == 2


def test_invalid_json_twice_is_unavailable() -> None:
    provider = FakeChatProvider(["nope", "still nope"])
    report = SemanticVerifier(provider).check([make_claim()], make_evidence())
    assert report.unavailable is True
    assert report.error


def test_provider_error_is_unavailable() -> None:
    provider = FakeChatProvider([ChatProviderError("timeout")])
    report = SemanticVerifier(provider).check([make_claim()], make_evidence())
    assert report.unavailable is True
    assert "timeout" in report.error


def test_missing_label_becomes_insufficient() -> None:
    report = SemanticVerifier(FakeChatProvider([labels_json(("c1", "supported"))])).check(
        [make_claim("c1"), make_claim("c2")], make_evidence()
    )
    assert report.labels["c2"] is SemanticLabel.INSUFFICIENT


def test_unknown_label_becomes_insufficient() -> None:
    report = SemanticVerifier(FakeChatProvider([labels_json(("c1", "maybe"))])).check(
        [make_claim()], make_evidence()
    )
    assert report.labels["c1"] is SemanticLabel.INSUFFICIENT


def test_empty_claims_and_disabled_verifier() -> None:
    empty = SemanticVerifier(FakeChatProvider([])).check([], [])
    assert empty.checked == 0
    assert empty.unavailable is False
    disabled = SemanticVerifier(FakeChatProvider([]), enabled=False).check(
        [make_claim()], make_evidence()
    )
    assert disabled.unavailable is True
    assert "未启用" in disabled.error


def test_batch_size_capped_at_five() -> None:
    response = labels_json(*[(f"c{index}", "supported") for index in range(1, 7)])
    provider = FakeChatProvider([response])
    claims = [make_claim(f"c{index}") for index in range(1, 7)]
    report = SemanticVerifier(provider, batch_size=9).check(claims, make_evidence())
    assert report.checked == 5


def test_semantic_does_not_receive_evidence_text() -> None:
    provider = FakeChatProvider([labels_json(("c1", "supported"))])
    item = article("national-aid-undergrad@v2021#第九条")
    evidence = [Evidence(article=item)]
    SemanticVerifier(provider).check([make_claim()], evidence)
    user_message = provider.calls[0][-1]["content"]
    # quote 只来自 claim.citation，不会把完整 evidence 正文塞给语义模型。
    assert item.text[:20] in user_message
    assert item.text[-40:] not in user_message


def test_claim_citation_required() -> None:
    try:
        Claim(claim_id="c1", text="x", citations=[])
    except ValueError:
        pass
    else:  # pragma: no cover
        raise AssertionError("Claim 应当要求 citation")
