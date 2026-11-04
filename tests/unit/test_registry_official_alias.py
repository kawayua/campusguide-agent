"""Registry aspect recognizes the official 学信网 name without widening scope."""

from __future__ import annotations

import json
from datetime import date

from campusguide.agents.coverage import CoverageChecker
from campusguide.agents.providers import FakeChatProvider
from campusguide.agents.question_units import decompose_question
from campusguide.domain.models import Citation, Claim

QUESTION = "微专业完成后发什么证，学信网会备注吗，能授予学士学位吗？"
FORMAL = "中国高等教育学生信息网"
ALIASES = (
    FORMAL,
    f"{FORMAL}（学信网）",
    f"{FORMAL} (学信网)",
)


def _claim(claim_id: str, text: str, *, doc_title: str = "微专业管理办法") -> Claim:
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
REGISTRY_CLAIM = _claim("c2", f"微专业不在{ALIASES[1]}备注信息。")
DEGREE_CLAIM = _claim("c3", "微专业不授予学士学位。")


def _run(claims: list[Claim], *, selected_ids: tuple[str, ...] = ("c3",)):
    units = decompose_question(QUESTION)
    assert len(units) == 3
    response = {
        "units": [
            {
                "unit_id": unit.unit_id,
                "status": "answered",
                "claim_ids": list(selected_ids),
                "missing_items": [],
            }
            for unit in units
        ],
        "unanswered_questions": [],
    }
    checker = CoverageChecker(
        FakeChatProvider([json.dumps(response, ensure_ascii=False)])
    )
    return units, checker.check(QUESTION, units, claims, [], [])


def _rows(units, audit) -> dict[str, dict]:
    return {
        unit.requested_aspects[0]: row
        for unit, row in zip(units, audit.units, strict=False)
    }


def test_official_name_aliases_are_definite_for_negative_and_positive_claims() -> None:
    for alias in ALIASES:
        for text in (
            f"微专业不在{alias}备注信息。",
            f"原文不在{alias}备注信息。",
            f"微专业在{alias}备注信息。",
            f"微专业将在{alias}标注。",
        ):
            units, audit = _run([CERT_CLAIM, _claim("c2", text), DEGREE_CLAIM])
            rows = _rows(units, audit)
            assert rows["学信网备案"]["status"] == "answered"
            assert rows["学信网备案"]["claim_ids"] == ["c2"]


def test_uncertain_unrelated_and_heading_only_wording_does_not_count() -> None:
    for body in (
        "微专业中国高等教育学生信息网（学信网）备注状态未知。",
        "微专业中国高等教育学生信息网 (学信网) 备注信息不详。",
        "微专业中国高等教育学生信息网备注信息未公布。",
        "微专业不在阳光高考信息网备注信息。",
        "《中国高等教育学生信息网（学信网）会备注》",
    ):
        units, audit = _run([CERT_CLAIM, _claim("c2", body), DEGREE_CLAIM])
        rows = _rows(units, audit)
        assert rows["学信网备案"]["status"] == "generation_missing"
        assert rows["学信网备案"]["claim_ids"] == []

    heading = _claim(
        "c2",
        "微专业信息暂未纳入。",
        doc_title="中国高等教育学生信息网（学信网）备案说明",
    )
    units, audit = _run([CERT_CLAIM, heading, DEGREE_CLAIM])
    rows = _rows(units, audit)
    assert rows["学信网备案"]["status"] == "generation_missing"
    assert rows["学信网备案"]["claim_ids"] == []


def test_full_checker_recovers_verified_formal_registry_claim_from_bad_ids() -> None:
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
    assert all(row["status"] == "answered" for row in audit.units)
    assert rows["证书"]["claim_ids"] == ["c1"]
    assert rows["学信网备案"]["claim_ids"] == ["c2"]
    assert rows["授予学位"]["claim_ids"] == ["c3"]
    assert audit.missing == []


def test_deleting_or_uncertain_formal_registry_claim_is_missing() -> None:
    unknown = _claim(
        "c2",
        "微专业中国高等教育学生信息网（学信网）备注状态未知。",
    )
    for claims in (
        [CERT_CLAIM, DEGREE_CLAIM],
        [CERT_CLAIM, unknown, DEGREE_CLAIM],
    ):
        units, audit = _run(claims, selected_ids=("c3",))
        rows = _rows(units, audit)
        assert rows["证书"]["status"] == "answered"
        assert rows["学信网备案"]["status"] == "generation_missing"
        assert rows["学信网备案"]["claim_ids"] == []
        assert rows["授予学位"]["status"] == "answered"
        assert "学信网备案" in audit.missing
