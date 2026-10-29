"""R4 guard cases: a claim must be bound to the asked object and aspect.

Report cases 005/016/027/045/049/070/075/080/083. The negative case required by
the report: one generic assertion must not be counted as covering every unit.
"""

from __future__ import annotations

import json

import pytest

from campusguide.agents.coverage import CoverageChecker
from campusguide.agents.plan import QuestionUnit
from campusguide.agents.providers import FakeChatProvider
from campusguide.verification.claims import ClaimValidator
from campusguide.agents.advisor import CitationDraft, ClaimDraft
from tests.w2_helpers import make_article, make_evidence

AGRO_TEXT = "农学专业实践环节包括专业实践、毕业实习，共12学分。"
CS_TEXT = "计算机科学与技术专业实践环节包括课程设计、毕业设计，共16学分。"


def _claim(article, text: str):
    return (
        ClaimValidator()
        .check(
            [ClaimDraft(text=text, citations=[CitationDraft(article_id=article.article_id, quote=article.text)])],
            [make_evidence(article)],
        )
        .claims[0]
    )


def _unit(unit_id: str, subject: str, aspect: str = "实践课程") -> QuestionUnit:
    return QuestionUnit(
        unit_id=unit_id,
        original_span=f"{subject}专业的{aspect}有哪些？",
        subject=subject,
        objects=[subject],
        requested_aspects=[aspect],
    )


def _checker(unit_ids: list[str], claim_ids: list[str]) -> CoverageChecker:
    rows = [
        {"unit_id": unit_id, "status": "answered", "claim_ids": claim_ids, "missing_items": []}
        for unit_id in unit_ids
    ]
    content = json.dumps({"units": rows, "unanswered_questions": []}, ensure_ascii=False)
    return CoverageChecker(FakeChatProvider([content]))


def test_generic_claim_does_not_cover_two_objects() -> None:
    agro = make_article("agro-plan@v2024#实践", doc_title="农学专业培养方案", article_no="实践环节", text=AGRO_TEXT)
    cs = make_article("cs-plan@v2024#实践", doc_title="计算机科学与技术专业培养方案", article_no="实践环节", text=CS_TEXT)
    evidence = [make_evidence(agro), make_evidence(cs)]
    generic = (
        ClaimValidator()
        .check(
            [
                ClaimDraft(
                    text="两个专业都有实践环节。",
                    citations=[
                        CitationDraft(article_id=agro.article_id, quote=agro.text),
                    ],
                )
            ],
            evidence,
        )
        .claims[0]
    )
    audit = _checker(["u1", "u2", "u3"], [generic.claim_id]).check(
        "农学、计算机科学与技术、统计学专业的实践环节有哪些？",
        [
            _unit("u1", "农学"),
            _unit("u2", "计算机科学与技术"),
            _unit("u3", "统计学"),
        ],
        [generic],
        evidence,
        [],
    )
    statuses = {row["unit_id"]: row["status"] for row in audit.units}
    # 只有被该结论真正提到的对象算覆盖，其余单元必须暴露缺口。
    # 报告 A5：泛称“两个专业都有实践环节”没有课程清单，不算任何单元已答。
    assert statuses["u1"] == "generation_missing"
    assert statuses["u2"] == "generation_missing"
    assert statuses["u3"] == "generation_missing"


def test_bound_claim_is_accepted() -> None:
    agro = make_article("agro-plan@v2024#实践", doc_title="农学专业培养方案", article_no="实践环节", text=AGRO_TEXT)
    evidence = [make_evidence(agro)]
    claim = _claim(agro, "农学专业实践环节包括专业实践、毕业实习，共12学分。")
    audit = _checker(["u1"], [claim.claim_id]).check(
        "农学专业的实践环节有哪些？", [_unit("u1", "农学")], [claim], evidence, []
    )
    assert audit.units[0]["status"] == "answered"


def test_aspect_mismatch_is_a_gap() -> None:
    agro = make_article("agro-plan@v2024#实践", doc_title="农学专业培养方案", article_no="实践环节", text=AGRO_TEXT)
    evidence = [make_evidence(agro)]
    claim = _claim(agro, "农学专业实践环节包括专业实践、毕业实习，共12学分。")
    audit = _checker(["u1"], [claim.claim_id]).check(
        "农学专业的研究方向有哪些？", [_unit("u1", "农学", "研究方向")], [claim], evidence, []
    )
    assert audit.units[0]["status"] == "generation_missing"


def test_flow_step_counts_as_support() -> None:
    from campusguide.domain.models import ChecklistItem

    agro = make_article("agro-plan@v2024#实践", doc_title="农学专业培养方案", article_no="实践环节", text=AGRO_TEXT)
    evidence = [make_evidence(agro)]
    step = ChecklistItem(
        step_id="s1",
        title="提交申请",
        description="向学院提交实践申请",
        department="农学院",
        citations=[agro.article_id],
        status="done",
    )
    audit = _checker(["u1"], ["flow:s1"]).check(
        "农学专业实践环节如何办理？",
        [_unit("u1", "农学", "办理程序")],
        [],
        evidence,
        [step],
    )
    assert audit.units[0]["status"] == "answered"