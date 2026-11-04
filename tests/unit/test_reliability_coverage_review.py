"""Acceptance tests for bound-cue dimensionality in study-duration coverage."""

from __future__ import annotations

import json
from datetime import date

import pytest

from campusguide.agents.advisor import CitationDraft, ClaimDraft
from campusguide.agents.coverage import CoverageChecker, _requested_properties
from campusguide.agents.coverage_study_duration import (
    BASIC,
    MAXIMUM,
    ORDINARY_MAX,
    STARTUP_MAX,
    StudyDurationField,
    study_duration_fields,
    study_duration_supported,
)
from campusguide.agents.plan import QuestionUnit
from campusguide.agents.providers import ChatResponse
from campusguide.domain.enums import ArticleStatus, Audience
from campusguide.domain.models import Article, Citation, Claim, Evidence
from campusguide.verification.claims import ClaimValidator

VD03_QUESTION = (
    "第一主修为四年制的双学士项目，第二主修为农林理工类与文科管理类时，"
    "总学分上限分别是多少？"
)
SCIENCE_CLAIM_TEXT = (
    "第一主修专业为四年制的双学士学位项目，第二主修专业为农林类、理工类专业的，"
    "培养方案总学分原则上不超过185学分。"
)
ARTS_CLAIM_TEXT = (
    "第一主修专业为四年制的双学士学位项目，第二主修专业为文科类专业（含管理类专业）的，"
    "培养方案总学分原则上不超过175学分。"
)


class _FakeProvider:
    def __init__(self, content: str):
        self._content = content

    def complete(
        self,
        messages: list[dict[str, str]],
        *,
        temperature: float = 0.2,
        max_tokens: int = 1200,
    ) -> ChatResponse:
        return ChatResponse(content=self._content)


def _unit(question: str) -> QuestionUnit:
    return QuestionUnit(
        unit_id="u1",
        original_span=question,
        requested_aspects=[question],
    )


def _claim(claim_id: str, text: str) -> Claim:
    return Claim(
        claim_id=claim_id,
        text=text,
        citations=[
            Citation(
                article_id="dual-degree#7",
                quote=text,
                quote_start=0,
                quote_end=len(text),
                doc_title="华南农业大学双学士学位复合型人才培养项目管理办法",
                article_no="第七条",
                effective_from=date(2025, 10, 7),
                source_url="https://example.edu/dual-degree.pdf",
            )
        ],
    )


def _scoped_claims() -> list[Claim]:
    return [
        _claim("c-science", SCIENCE_CLAIM_TEXT),
        _claim("c-arts", ARTS_CLAIM_TEXT),
    ]


def _check(question: str, claims: list[Claim]) -> dict:
    payload = {
        "units": [
            {
                "unit_id": "u1",
                "status": "answered",
                "claim_ids": [claim.claim_id for claim in claims],
                "missing_items": [],
            }
        ],
        "unanswered_questions": [],
    }
    checker = CoverageChecker(
        _FakeProvider(json.dumps(payload, ensure_ascii=False))
    )
    audit = checker.check(question, [_unit(question)], claims, [], [])
    assert not audit.unavailable, audit.error
    return audit.units[0]


def _article() -> Article:
    text = SCIENCE_CLAIM_TEXT + ARTS_CLAIM_TEXT
    return Article(
        article_id="dual-degree#7",
        doc_id="dual-degree",
        version_id="v2025",
        doc_title="华南农业大学双学士学位复合型人才培养项目管理办法",
        article_no="第七条",
        article_index=7,
        text=text,
        text_normalized=text,
        audience=[Audience.UNDERGRADUATE],
        status=ArticleStatus.ACTIVE,
        effective_from=date(2025, 10, 7),
        published_at=date(2025, 10, 7),
        issuing_department="教务处",
        source_url="https://example.edu/dual-degree.pdf",
        source_sha256="0" * 64,
    )


def _draft(claim_id: str, text: str, quote: str) -> ClaimDraft:
    return ClaimDraft(
        claim_id=claim_id,
        text=text,
        kind="eligibility",
        citations=[CitationDraft(article_id="dual-degree#7", quote=quote)],
    )


def test_credit_bound_background_does_not_invent_study_duration() -> None:
    assert study_duration_fields(VD03_QUESTION) == []

    properties = _requested_properties(VD03_QUESTION)
    assert not [prop for prop in properties if prop.kind == "study_duration"]
    bounds = [prop for prop in properties if prop.kind == "bound"]
    assert [(prop.measure, prop.direction) for prop in bounds] == [("学分", "max")]


def test_real_four_year_maximum_stays_required_and_enforced() -> None:
    question = "第一主修专业为四年制的双学士学位项目，最长学习年限是多少年？"
    assert study_duration_fields(question) == [
        StudyDurationField("四年制", ORDINARY_MAX, "四年制最长学习年限")
    ]
    field = StudyDurationField("四年制", ORDINARY_MAX, "四年制最长学习年限")
    assert study_duration_supported(field, "四年制项目最长学习年限为8年。")
    assert not study_duration_supported(field, "四年制项目总学分上限为185学分。")


def test_credit_and_duration_upper_bounds_remain_independent() -> None:
    question = (
        "第一主修专业为四年制的双学士学位项目，"
        "总学分与最长学习年限的上限分别是多少？"
    )
    fields = study_duration_fields(question)
    assert [(field.condition, field.kind) for field in fields] == [
        ("四年制", ORDINARY_MAX)
    ]
    assert not study_duration_supported(
        fields[0], "四年制项目总学分上限为185学分。"
    )

    properties = _requested_properties(question)
    study = [prop for prop in properties if prop.kind == "study_duration"]
    bounds = [prop for prop in properties if prop.kind == "bound"]
    assert [(prop.core, prop.action) for prop in study] == [
        ("四年制", ORDINARY_MAX)
    ]
    assert [(prop.measure, prop.direction) for prop in bounds] == [("学分", "max")]


@pytest.mark.parametrize(
    "question",
    [
        "四年制课程的学费上限是多少？",
        "四年制课程的学分上限是多少？",
        "四年制课程的人数上限是多少？",
        "四年制课程最多多少人？",
        "四年制课程最长多少学分？",
        "三年制项目每学期学费最多多少元？",
        "四年制课程的上限是多少学分？",
        "四年制项目上限是多少元？",
        "四年制课程上限是多少人？",
    ],
)
def test_other_measure_bounds_do_not_create_study_duration(
    question: str,
) -> None:
    assert study_duration_fields(question) == []


@pytest.mark.parametrize(
    ("question", "condition"),
    [
        ("三年制专业最长学习年限是多少年？", "三年制"),
        ("五年制项目最长年限为几年？", "五年制"),
        ("两年制课程最长学习时间是多少？", "两年制"),
        ("四年制项目的上限是多少年？", "四年制"),
    ],
)
def test_year_system_duration_asks_still_create_fields(
    question: str, condition: str
) -> None:
    assert [(field.condition, field.kind) for field in study_duration_fields(question)] == [
        (condition, ORDINARY_MAX)
    ]


@pytest.mark.parametrize(
    "question",
    [
        "四年制项目的学费上限和最长学习年限分别是多少？",
        "四年制项目总学分上限与最长学习年限分别是多少？",
        "四年制项目的最长学习年限和学费上限分别是多少？",
    ],
)
def test_mixed_measure_questions_keep_the_duration_field(
    question: str,
) -> None:
    assert [(field.condition, field.kind) for field in study_duration_fields(question)] == [
        ("四年制", ORDINARY_MAX)
    ]


def test_startup_only_duration_ask_is_not_contaminated_by_credit_bound() -> None:
    question = (
        "三年制专业休学创业时最长年限是多少年，总学分上限是多少？"
    )
    assert [(field.condition, field.kind) for field in study_duration_fields(question)] == [
        ("三年制", STARTUP_MAX)
    ]


def test_mode_basic_and_maximum_branches_are_unchanged() -> None:
    question = "全日制、非全日制硕士学制和最长学习年限分别几年？"
    assert {
        (field.condition, field.kind) for field in study_duration_fields(question)
    } == {
        ("全日制", BASIC),
        ("全日制", MAXIMUM),
        ("非全日制", BASIC),
        ("非全日制", MAXIMUM),
    }


def test_full_checker_accepts_two_correctly_scoped_credit_answers() -> None:
    row = _check(VD03_QUESTION, _scoped_claims())
    assert row["status"] == "answered"
    assert set(row["claim_ids"]) == {"c-science", "c-arts"}
    assert row["missing_items"] == []


@pytest.mark.parametrize(
    ("removed_id", "missing_item"),
    [
        ("c-arts", "文科管理类时"),
        ("c-science", "第二主修为农林理工类"),
    ],
)
def test_deleting_either_scope_stays_generation_missing(
    removed_id: str, missing_item: str
) -> None:
    claims = [claim for claim in _scoped_claims() if claim.claim_id != removed_id]
    row = _check(VD03_QUESTION, claims)
    assert row["status"] == "generation_missing"
    assert row["claim_ids"] == []
    assert missing_item in row["missing_items"]


def test_verifier_keeps_two_correctly_scoped_credit_answers() -> None:
    check = ClaimValidator().check(
        [
            _draft("c-science", SCIENCE_CLAIM_TEXT, SCIENCE_CLAIM_TEXT),
            _draft("c-arts", ARTS_CLAIM_TEXT, ARTS_CLAIM_TEXT),
        ],
        [Evidence(article=_article())],
    )
    assert check.dropped_claim_ids == []
    assert [claim.claim_id for claim in check.claims] == ["c-science", "c-arts"]


@pytest.mark.parametrize(
    ("claim_id", "text", "quote", "uncovered_fact"),
    [
        (
            "c-science-swapped",
            SCIENCE_CLAIM_TEXT.replace("185学分", "175学分"),
            SCIENCE_CLAIM_TEXT,
            "175学分",
        ),
        (
            "c-arts-swapped",
            ARTS_CLAIM_TEXT.replace("175学分", "185学分"),
            ARTS_CLAIM_TEXT,
            "185学分",
        ),
    ],
)
def test_verifier_rejects_swapped_scope_values(
    claim_id: str, text: str, quote: str, uncovered_fact: str
) -> None:
    check = ClaimValidator().check(
        [_draft(claim_id, text, quote)],
        [Evidence(article=_article())],
    )
    assert check.claims == []
    assert check.dropped_claim_ids == [claim_id]
    issues = [issue for issue in check.issues if issue.code == "NUMBER_NOT_SUPPORTED"]
    assert issues
    assert uncovered_fact in issues[0].facts
