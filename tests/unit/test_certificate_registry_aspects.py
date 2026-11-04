"""NH07: certificate, platform registry and degree asks must survive separately."""

from __future__ import annotations

import json
from datetime import date

from campusguide.agents.coverage import CoverageChecker
from campusguide.agents.providers import FakeChatProvider
from campusguide.agents.question_units import decompose_question, requested_aspects
from campusguide.domain.models import Citation, Claim

OLD_QUESTION = "微专业完成后发什么证，学信网会备注吗，能授予学士学位吗？"
SYNTHETIC_QUESTION = "辅修专业完成后颁发哪种证，学信网会不会标注，可以授予学士学位吗？"
REQUIRED_ASPECTS = {"授予学位", "证书", "学信网备案"}


def _claim(claim_id: str, text: str) -> Claim:
    return Claim(
        claim_id=claim_id,
        text=text,
        citations=[
            Citation(
                article_id=f"micro-policy@v2024#{claim_id}",
                part_no=1,
                quote=text,
                quote_start=0,
                quote_end=len(text),
                doc_title="微专业管理办法",
                article_no="第三章",
                effective_from=date(2024, 9, 1),
                source_url="https://example.edu.cn/micro",
            )
        ],
    )


CERT_CLAIM = _claim("c1", "微专业完成后颁发结业证书。")
REGISTRY_CLAIM = _claim("c2", "微专业不在学信网备注。")
DEGREE_CLAIM = _claim("c3", "微专业不授予学士学位。")


def _audit(question: str, units, claims: list[Claim]):
    ids = [claim.claim_id for claim in claims]
    rows = [
        {
            "unit_id": unit.unit_id,
            "status": "answered",
            "claim_ids": ids,
            "missing_items": [],
        }
        for unit in units
    ]
    response = json.dumps(
        {"units": rows, "unanswered_questions": []}, ensure_ascii=False
    )
    return CoverageChecker(FakeChatProvider([response])).check(
        question, units, claims, [], []
    )


def _rows_by_aspect(units, audit) -> dict:
    return {
        unit.requested_aspects[0]: row
        for unit, row in zip(units, audit.units, strict=False)
    }


def _assert_three_asks(question: str) -> list:
    units = decompose_question(question)
    assert len(units) == 3
    assert {unit.requested_aspects[0] for unit in units} == REQUIRED_ASPECTS
    assert all(unit.requested_aspects == [unit.requested_aspects[0]] for unit in units)
    assert {unit.original_span for unit in units} == {question}
    return units


def test_old_nh07_question_keeps_all_three_asks() -> None:
    units = _assert_three_asks(OLD_QUESTION)
    assert [unit.requested_aspects[0] for unit in units] == [
        "授予学位",
        "证书",
        "学信网备案",
    ]


def test_synthetic_project_keeps_all_three_asks() -> None:
    _assert_three_asks(SYNTHETIC_QUESTION)


def test_complete_three_claims_cover_all_three_asks() -> None:
    units = _assert_three_asks(OLD_QUESTION)
    audit = _audit(
        OLD_QUESTION, units, [CERT_CLAIM, REGISTRY_CLAIM, DEGREE_CLAIM]
    )
    rows = _rows_by_aspect(units, audit)
    assert all(row["status"] == "answered" for row in audit.units)
    assert rows["证书"]["claim_ids"] == ["c1"]
    assert rows["学信网备案"]["claim_ids"] == ["c2"]
    assert rows["授予学位"]["claim_ids"] == ["c3"]
    assert audit.missing == []


def test_missing_certificate_claim_is_a_gap() -> None:
    units = _assert_three_asks(OLD_QUESTION)
    audit = _audit(OLD_QUESTION, units, [REGISTRY_CLAIM, DEGREE_CLAIM])
    rows = _rows_by_aspect(units, audit)
    assert rows["证书"]["status"] == "generation_missing"
    assert rows["学信网备案"]["status"] == "answered"
    assert rows["授予学位"]["status"] == "answered"
    assert "证书" in audit.missing


def test_missing_registry_claim_is_a_gap() -> None:
    units = _assert_three_asks(OLD_QUESTION)
    audit = _audit(OLD_QUESTION, units, [CERT_CLAIM, DEGREE_CLAIM])
    rows = _rows_by_aspect(units, audit)
    assert rows["证书"]["status"] == "answered"
    assert rows["学信网备案"]["status"] == "generation_missing"
    assert rows["授予学位"]["status"] == "answered"
    assert "学信网备案" in audit.missing


def test_missing_degree_claim_is_a_gap() -> None:
    units = _assert_three_asks(OLD_QUESTION)
    audit = _audit(OLD_QUESTION, units, [CERT_CLAIM, REGISTRY_CLAIM])
    rows = _rows_by_aspect(units, audit)
    assert rows["证书"]["status"] == "answered"
    assert rows["学信网备案"]["status"] == "answered"
    assert rows["授予学位"]["status"] == "generation_missing"
    assert any("学位" in item for item in audit.missing)


def test_uncertain_registry_wording_does_not_answer_registry_aspect() -> None:
    uncertain = _claim("c2", "学信网备注信息不详。")
    units = _assert_three_asks(OLD_QUESTION)
    audit = _audit(OLD_QUESTION, units, [CERT_CLAIM, uncertain, DEGREE_CLAIM])
    rows = _rows_by_aspect(units, audit)
    assert rows["证书"]["status"] == "answered"
    assert rows["学信网备案"]["status"] == "generation_missing"
    assert rows["授予学位"]["status"] == "answered"
    assert "学信网备案" in audit.missing


def test_submitted_certificate_material_is_not_a_certificate_ask() -> None:
    for question in (
        "申请奖学金需要提交哪些证书材料？",
        "申请时需提交证书复印件吗？",
    ):
        assert "证书" not in requested_aspects(question)


def test_platform_operation_background_is_not_a_registry_ask() -> None:
    for question in (
        "在学信网备案需要提交哪些材料？",
        "学信网备案流程是什么？",
        "学信网操作说明在哪里查看？",
    ):
        assert "学信网备案" not in requested_aspects(question)


def test_existing_certificate_question_contract_is_preserved() -> None:
    aspects = requested_aspects("微专业有什么区别，报名条件和完成后取得的证书是什么？")
    assert "证书" in aspects
    assert "学信网备案" not in aspects


def test_evidence_request_is_not_a_certificate_award_ask() -> None:
    question = "处分申诉时需要给什么证据？"
    assert "证书" not in requested_aspects(question)
    units = decompose_question(question)
    assert len(units) == 1
    assert "证书" not in units[0].requested_aspects


def test_real_certificate_award_questions_still_match() -> None:
    for question in (
        "微专业完成后发什么证？",
        "辅修专业完成后颁发哪种证书？",
        "完成以后会发证吗？",
    ):
        assert "证书" in requested_aspects(question)


def test_certificate_award_guard_keeps_submission_background() -> None:
    for question in (
        "申请奖学金需要提交哪些证书材料？",
        "申请时需提交证书复印件吗？",
    ):
        assert "证书" not in requested_aspects(question)
        assert all(
            "证书" not in unit.requested_aspects
            for unit in decompose_question(question)
        )
