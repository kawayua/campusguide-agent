"""Certificate values, registry certainty and verified-supporter recovery."""

from __future__ import annotations

import json
from datetime import date

from campusguide.agents.coverage import CoverageChecker
from campusguide.agents.providers import FakeChatProvider
from campusguide.agents.question_units import decompose_question
from campusguide.domain.models import Citation, Claim
from campusguide.retrieval.profile_expand import ProfileLexicon

QUESTION = "微专业完成后发什么证，学信网会备注吗，能授予学士学位吗？"
YES_NO_QUESTION = "微专业完成以后会发证吗？"
LEXICON = ProfileLexicon({}, {"micro": ("微专业",)})


def _claim(
    claim_id: str, text: str, *, doc_title: str = "微专业管理办法"
) -> Claim:
    return Claim(
        claim_id=claim_id,
        text=text,
        citations=[
            Citation(
                article_id=f"{doc_title}#{claim_id}",
                part_no=1,
                quote=text,
                quote_start=0,
                quote_end=len(text),
                doc_title=doc_title,
                article_no="第三章",
                effective_from=date(2024, 9, 1),
                source_url="https://example.edu.cn/micro",
            )
        ],
    )


CERT_CLAIM = _claim("c1", "微专业完成后颁发结业证书。")
REGISTRY_CLAIM = _claim("c2", "微专业不在学信网备注。")
DEGREE_CLAIM = _claim("c3", "微专业不授予学士学位。")


def _units(question: str = QUESTION):
    units = decompose_question(question, LEXICON)
    assert units
    return units


def _run(
    claims: list[Claim],
    *,
    question: str = QUESTION,
    selected_ids: tuple[str, ...] = (),
    status: str = "answered",
    missing_items: tuple[str, ...] = (),
):
    units = _units(question)
    response = {
        "units": [
            {
                "unit_id": unit.unit_id,
                "status": status,
                "claim_ids": list(selected_ids) if status == "answered" else [],
                "missing_items": list(missing_items),
            }
            for unit in units
        ],
        "unanswered_questions": [],
    }
    checker = CoverageChecker(
        FakeChatProvider([json.dumps(response, ensure_ascii=False)])
    )
    return units, checker.check(question, units, claims, [], [])


def _rows(units, audit) -> dict[str, dict]:
    return {
        unit.requested_aspects[0]: row
        for unit, row in zip(units, audit.units, strict=False)
    }


def test_bad_model_ids_are_replaced_by_each_aspects_actual_supporters() -> None:
    units, audit = _run(
        [CERT_CLAIM, REGISTRY_CLAIM, DEGREE_CLAIM],
        selected_ids=("c3",),
    )
    assert [unit.requested_aspects[0] for unit in units] == [
        "授予学位",
        "证书",
        "学信网备案",
    ]
    rows = _rows(units, audit)
    assert rows["证书"]["status"] == "answered"
    assert rows["证书"]["claim_ids"] == ["c1"]
    assert rows["学信网备案"]["status"] == "answered"
    assert rows["学信网备案"]["claim_ids"] == ["c2"]
    assert rows["授予学位"]["status"] == "answered"
    assert rows["授予学位"]["claim_ids"] == ["c3"]
    assert audit.missing == []


def test_recovery_never_promotes_generic_uncertain_or_other_scope_claims() -> None:
    generic_certificate = _claim("c4", "微专业完成后发放证书。")
    uncertain_registry = _claim("c5", "微专业学信网备案信息不详。")
    other_major = _claim(
        "c6",
        "计算机专业完成后颁发结业证书。",
        doc_title="计算机培养方案",
    )
    units, audit = _run(
        [
            CERT_CLAIM,
            REGISTRY_CLAIM,
            DEGREE_CLAIM,
            generic_certificate,
            uncertain_registry,
            other_major,
        ],
        selected_ids=("c3",),
    )
    rows = _rows(units, audit)
    assert rows["证书"]["claim_ids"] == ["c1"]
    assert rows["学信网备案"]["claim_ids"] == ["c2"]
    assert rows["授予学位"]["claim_ids"] == ["c3"]
    assert audit.missing == []


def test_deleting_each_supporting_field_leaves_its_unit_missing() -> None:
    for removed, aspect in (
        (CERT_CLAIM, "证书"),
        (REGISTRY_CLAIM, "学信网备案"),
        (DEGREE_CLAIM, "授予学位"),
    ):
        claims = [
            claim
            for claim in (CERT_CLAIM, REGISTRY_CLAIM, DEGREE_CLAIM)
            if claim.claim_id != removed.claim_id
        ]
        units, audit = _run(claims, selected_ids=(claims[-1].claim_id,))
        rows = _rows(units, audit)
        assert rows[aspect]["status"] == "generation_missing"
        assert rows[aspect]["claim_ids"] == []
        assert all(
            row["status"] == "answered"
            for name, row in rows.items()
            if name != aspect
        )


def test_generic_or_uncertain_certificate_body_cannot_answer_type() -> None:
    for body in (
        "微专业完成后发放证书。",
        "微专业证书信息不详。",
        "微专业是否颁发结业证书尚不明确。",
    ):
        certificate = _claim("c1", body)
        units, audit = _run(
            [certificate, REGISTRY_CLAIM, DEGREE_CLAIM],
            selected_ids=("c3",),
        )
        rows = _rows(units, audit)
        assert rows["证书"]["status"] == "generation_missing"
        assert rows["证书"]["claim_ids"] == []
        assert any("证书" in item for item in audit.missing)
        assert rows["学信网备案"]["status"] == "answered"
        assert rows["授予学位"]["status"] == "answered"


def test_certificate_headings_cannot_supply_the_type() -> None:
    body_heading = _claim(
        "c1",
        "《结业证书管理办法》规定，微专业完成后发放证书。",
    )
    citation_heading = _claim(
        "c1",
        "微专业完成后发放证书。",
        doc_title="结业证书管理办法",
    )
    for certificate in (body_heading, citation_heading):
        units, audit = _run(
            [certificate, REGISTRY_CLAIM, DEGREE_CLAIM],
            selected_ids=("c3",),
        )
        rows = _rows(units, audit)
        assert rows["证书"]["status"] == "generation_missing"
        assert rows["证书"]["claim_ids"] == []


def test_yes_no_certificate_accepts_issuance_without_a_named_type() -> None:
    for body in (
        "微专业完成以后发证。",
        "微专业完成以后不发证。",
        "微专业完成以后颁发证书。",
    ):
        units, audit = _run(
            [_claim("c1", body)],
            question=YES_NO_QUESTION,
            selected_ids=("c1",),
        )
        assert len(units) == 1
        assert units[0].requested_aspects == ["证书"]
        assert audit.units[0]["status"] == "answered"
        assert audit.units[0]["claim_ids"] == ["c1"]


def test_registry_uncertainty_is_rejected_even_with_literal_aspect_name() -> None:
    for body in (
        "微专业学信网备案信息不详。",
        "微专业学信网备案信息未公布。",
        "微专业学信网备案信息待确认。",
        "微专业学信网备注信息不详。",
    ):
        registry = _claim("c2", body)
        units, audit = _run(
            [CERT_CLAIM, registry, DEGREE_CLAIM],
            selected_ids=("c3",),
        )
        rows = _rows(units, audit)
        assert rows["学信网备案"]["status"] == "generation_missing"
        assert rows["学信网备案"]["claim_ids"] == []
        assert any("学信网备案" in item for item in audit.missing)


def test_registry_heading_or_echo_cannot_answer_annotation_status() -> None:
    heading = _claim(
        "c2",
        "微专业信息暂未纳入。",
        doc_title="学信网备案说明",
    )
    units, audit = _run(
        [CERT_CLAIM, heading, DEGREE_CLAIM],
        selected_ids=("c3",),
    )
    rows = _rows(units, audit)
    assert rows["学信网备案"]["status"] == "generation_missing"
    assert rows["学信网备案"]["claim_ids"] == []


def test_definite_registry_assertions_answer_the_aspect() -> None:
    for body in (
        "微专业不在学信网备注。",
        "微专业将在学信网标注。",
        "微专业信息在学信网登记。",
    ):
        registry = _claim("c2", body)
        units, audit = _run(
            [CERT_CLAIM, registry, DEGREE_CLAIM],
            selected_ids=("c3",),
        )
        rows = _rows(units, audit)
        assert rows["学信网备案"]["status"] == "answered"
        assert rows["学信网备案"]["claim_ids"] == ["c2"]


def test_pessimistic_model_recovers_only_the_verified_supporters() -> None:
    generic_certificate = _claim("c4", "微专业完成后发放证书。")
    uncertain_registry = _claim("c5", "微专业学信网备案信息不详。")
    other_major = _claim(
        "c6",
        "计算机专业完成后颁发结业证书。",
        doc_title="计算机培养方案",
    )
    units, audit = _run(
        [
            CERT_CLAIM,
            REGISTRY_CLAIM,
            DEGREE_CLAIM,
            generic_certificate,
            uncertain_registry,
            other_major,
        ],
        status="generation_missing",
    )
    rows = _rows(units, audit)
    assert rows["证书"]["status"] == "answered"
    assert rows["证书"]["claim_ids"] == ["c1"]
    assert rows["学信网备案"]["status"] == "answered"
    assert rows["学信网备案"]["claim_ids"] == ["c2"]
    assert rows["授予学位"]["status"] == "answered"
    assert rows["授予学位"]["claim_ids"] == ["c3"]
    assert audit.missing == []
