"""Explicit requested properties and verified-claim coverage (synthetic only)."""

from __future__ import annotations

import json
from datetime import date

from campusguide.agents.coverage import (
    CoverageAudit,
    CoverageChecker,
    _claim_supports_unit,
    _property_supported,
    _question_clauses,
    _requested_properties,
    _scoped_content,
    _syntactic_course_terms,
)
from campusguide.agents.plan import QuestionUnit
from campusguide.agents.providers import FakeChatProvider
from campusguide.agents.question_units import decompose_question, requested_aspects
from campusguide.domain.models import Citation, Claim
from campusguide.retrieval.profile_expand import ProfileLexicon

SUBJECT = "明理学院"
PROGRAM = "数据治理微专业"
TITLE = "明理学院数据治理微专业管理办法"


def _claim(claim_id: str, text: str, *, title: str = TITLE) -> Claim:
    return Claim(
        claim_id=claim_id,
        text=text,
        citations=[
            Citation(
                article_id=f"{title}#1",
                quote=text,
                quote_start=0,
                quote_end=len(text),
                doc_title=title,
                article_no="第一条",
                effective_from=date(2026, 1, 1),
                source_url="https://example.edu.cn/x",
            )
        ],
    )


def _unit(
    question: str,
    aspects: list[str],
    *,
    subject: str = SUBJECT,
    objects: tuple[str, ...] = (PROGRAM,),
    version: str | None = None,
) -> QuestionUnit:
    return QuestionUnit(
        unit_id="u1",
        original_span=question,
        subject=subject,
        objects=list(objects),
        version=version,
        requested_aspects=aspects,
    )


def _audit(
    unit: QuestionUnit,
    claims: list[Claim],
    *,
    status: str = "answered",
    claim_ids: list[str] | None = None,
    missing: list[str] | None = None,
    unanswered: list[str] | None = None,
) -> CoverageAudit:
    verdict = {
        "units": [
            {
                "unit_id": unit.unit_id,
                "status": status,
                "claim_ids": (
                    [claim.claim_id for claim in claims]
                    if claim_ids is None
                    else claim_ids
                ),
                "missing_items": missing or [],
            }
        ],
        "unanswered_questions": unanswered or [],
    }
    provider = FakeChatProvider([json.dumps(verdict, ensure_ascii=False)])
    return CoverageChecker(provider).check(
        unit.original_span, [unit], claims, [], []
    )


def test_negative_payment_answer_covers_and_echo_is_rejected() -> None:
    question = f"{SUBJECT}{PROGRAM}是否需要缴费？"
    unit = _unit(question, ["费用"])
    claim = _claim("c1", f"{SUBJECT}{PROGRAM}不收费。")
    props = _requested_properties(question, (SUBJECT, PROGRAM))
    assert [prop.kind for prop in props] == ["payment"]
    assert _claim_supports_unit(unit, claim, properties=props)
    assert not _claim_supports_unit(
        unit,
        _claim("c2", f"{SUBJECT}{PROGRAM}是否收费尚不明确。"),
        properties=props,
    )
    audit = _audit(
        unit,
        [claim],
        status="generation_missing",
        claim_ids=[],
        missing=["未说明是否需要缴费"],
    )
    assert audit.units[0]["status"] == "answered"
    assert audit.units[0]["claim_ids"] == ["c1"]
    assert audit.missing == []


def test_numbered_list_omission_survives_covered_answer() -> None:
    question = f"{SUBJECT}{PROGRAM}是否需要缴费？"
    unit = _unit(question, ["费用"])
    claim = _claim("c1", f"{SUBJECT}{PROGRAM}不收费。")
    audit = _audit(
        unit,
        [claim],
        status="answered",
        claim_ids=["c1"],
        missing=["第3项未列出"],
    )
    assert audit.units[0]["status"] == "generation_missing"
    assert any("第3项" in item for item in audit.missing)


def test_negative_degree_and_submission_answers_are_coverage() -> None:
    degree_question = f"{SUBJECT}{PROGRAM}毕业是否授予学位？"
    degree = _requested_properties(degree_question, (SUBJECT, PROGRAM))
    assert [prop.kind for prop in degree] == ["degree"]
    assert _property_supported(degree[0], f"{SUBJECT}{PROGRAM}毕业不授予学位。")
    assert not _property_supported(
        degree[0], f"{SUBJECT}{PROGRAM}毕业是否授予学位尚不明确。"
    )

    submission_question = f"申请{PROGRAM}是否需要提交纸质材料？"
    submission = _requested_properties(submission_question, (PROGRAM,))
    assert [prop.kind for prop in submission] == ["submission"]
    assert _property_supported(
        submission[0], f"申请{PROGRAM}无需提交纸质材料。"
    )
    assert not _property_supported(
        submission[0], f"申请{PROGRAM}是否提交纸质材料尚未明确。"
    )


def test_duration_accepts_relative_semester_and_rejects_calendar_only() -> None:
    question = f"{PROGRAM}入学多久可以申请毕业？"
    props = _requested_properties(question, (PROGRAM,))
    assert [prop.kind for prop in props] == ["duration"]
    unit = _unit(question, ["学制"], subject="")
    assert _claim_supports_unit(
        unit,
        _claim("c1", f"{PROGRAM}入学满一学期后可以申请毕业。"),
        properties=props,
    )
    assert not _claim_supports_unit(
        unit,
        _claim("c2", f"{PROGRAM}于2026年9月入学。"),
        properties=props,
    )


def test_timing_accepts_relative_anchor_and_calendar_ask_requires_date() -> None:
    question = f"{PROGRAM}毕业实习的时间安排是什么？"
    props = _requested_properties(question, (PROGRAM,))
    assert [prop.kind for prop in props] == ["timing"]
    assert _property_supported(
        props[0], f"{PROGRAM}毕业实习安排在第五学期结束前完成。"
    )

    dated = f"{PROGRAM}毕业实习什么时候完成？请给出具体日期。"
    dated_props = _requested_properties(dated, (PROGRAM,))
    assert len(dated_props) == 1 and dated_props[0].calendar
    assert not _property_supported(
        dated_props[0], f"{PROGRAM}毕业实习在第五学期结束前完成。"
    )
    assert _property_supported(
        dated_props[0], f"{PROGRAM}毕业实习于2026年4月18日完成。"
    )


def test_calendar_value_does_not_transfer_to_another_action() -> None:
    question = f"{PROGRAM}毕业实习材料什么时候提交？请给出具体日期。"
    props = _requested_properties(question, (PROGRAM,))
    assert len(props) == 1 and props[0].calendar
    assert not _property_supported(
        props[0], f"{PROGRAM}毕业实习材料于2026年4月18日完成。"
    )
    assert _property_supported(
        props[0], f"{PROGRAM}毕业实习材料于2026年4月18日提交。"
    )


def test_bound_accepts_scoped_percentage_range() -> None:
    question = f"{SUBJECT}每年最多推荐多少比例的学生参加校际交流？"
    props = _requested_properties(question, (SUBJECT,))
    bounds = [prop for prop in props if prop.kind == "bound"]
    assert len(bounds) == 1 and bounds[0].direction == "max"
    assert _property_supported(
        bounds[0], f"{SUBJECT}每年最多推荐5%—15%的学生参加校际交流。"
    )
    assert not _property_supported(
        bounds[0], f"{SUBJECT}每年最多推荐5%—15%的教师参加校际交流。"
    )
    assert not _property_supported(
        bounds[0], f"{SUBJECT}每年最多推荐5%—15%的学生参加教学比赛。"
    )


def test_bound_min_and_max_hours_join_across_one_claim() -> None:
    question = "每学期最低多少学时、最高多少学时？"
    props = _requested_properties(question, ())
    assert sorted(prop.direction for prop in props if prop.kind == "bound") == [
        "max",
        "min",
    ]
    claim = "每学期最低20学时，最高40学时。"
    assert all(_property_supported(prop, claim) for prop in props)
    partial = "每学期最低20学时。"
    assert any(not _property_supported(prop, partial) for prop in props)


def test_selected_and_unselected_join_across_verified_claims() -> None:
    question = f"{SUBJECT}本科培养方案中已选修和未选修的课程分别有哪些？"
    unit = _unit(question, ["选修课程"], objects=())
    chosen = _claim("c1", f"{SUBJECT}本科培养方案中张三已选修《运筹学与优化》。")
    skipped = _claim("c2", f"{SUBJECT}本科培养方案中李四未选修《数据分析与决策》。")
    props = _requested_properties(question, (SUBJECT,))
    assert sorted(prop.polarity for prop in props) == ["selected", "unselected"]
    audit = _audit(unit, [chosen, skipped])
    assert audit.units[0]["status"] == "answered"
    assert audit.units[0]["claim_ids"] == ["c1", "c2"]

    partial = _audit(unit, [chosen])
    assert partial.units[0]["status"] == "generation_missing"
    assert any("未选修" in item for item in partial.missing)


def test_registered_and_quoted_conjunctions_stay_whole() -> None:
    question = "供应链管理与优化和智能物流的时间安排分别是什么？"
    props = _requested_properties(
        question, ("供应链管理与优化", "智能物流")
    )
    assert {prop.core for prop in props if prop.kind == "timing"} == {
        "供应链管理与优化",
        "智能物流",
    }
    clauses, topics = _question_clauses(
        "《运筹学与优化》和《数据分析与决策》分别有哪些考核方式？",
        protected=("运筹学与优化", "数据分析与决策"),
    )
    assert topics == []
    assert clauses == ["《运筹学与优化》", "《数据分析与决策》"]


def test_context_words_do_not_become_required_aspects() -> None:
    assert "负责人" not in requested_aspects(
        "在班主任指导下完成毕业实习的要求有哪些？"
    )
    assert "毕业学分" not in requested_aspects(
        "申请明理奖学金要求总学分不低于多少？"
    )
    assert "提交材料" not in requested_aspects(
        "材料科学与工程专业有哪些核心课程？"
    )
    assert "证书" not in requested_aspects(
        "提交英语等级证书复印件有哪些要求？"
    )


def test_timing_and_selection_asks_do_not_invent_content_aspects() -> None:
    timing = requested_aspects("毕业论文什么时候提交？")
    assert "毕业设计" not in timing
    assert "时间安排" in timing
    selection = requested_aspects("选修课程是否需要缴费？")
    assert "选修课程" not in selection
    assert "费用" in selection


def test_shared_version_range_is_one_unit() -> None:
    units = decompose_question("2025—2026级课程设置有哪些？")
    assert len(units) == 1
    assert units[0].version == "2025-2026"
    assert units[0].cohort is None

    lexicon = ProfileLexicon({}, {"m1": ("供应链管理与优化",)})
    named = decompose_question(
        "2025—2026级供应链管理与优化专业的课程设置有哪些？", lexicon
    )
    assert len(named) == 1
    assert {unit.version for unit in named} == {"2025-2026"}

    listed = decompose_question(
        "2025、2026级供应链管理与优化专业的课程设置有哪些？", lexicon
    )
    assert {unit.version for unit in listed} == {"2025", "2026"}


_SHARED_SCOPE_ASPECTS = ["学分", "开课学期", "课程性质"]
_SHARED_SCOPE_QUESTION = (
    "在2031—2032级示例专业培养方案中，"
    "课程90000000000001示例课程甲的学分、开课学期和课程性质分别是什么？"
)
_SHARED_SCOPE_CLAIM = (
    "在2031—2032级示例专业培养方案中，"
    "课程90000000000001示例课程甲的学分为4，开课学期为夏，课程性质为任选。"
)


def test_shared_version_range_lets_following_comma_clauses_inherit_scope() -> None:
    claim = _claim("c1", _SHARED_SCOPE_CLAIM)
    for version in ("2031-2032", "2031"):
        unit = _unit(
            _SHARED_SCOPE_QUESTION,
            _SHARED_SCOPE_ASPECTS,
            subject="",
            objects=(),
            version=version,
        )
        scoped = _scoped_content(unit, claim, claim.text, None)
        assert "学分为4" in scoped
        assert "开课学期为夏" in scoped
        assert "课程性质为任选" in scoped
        assert _claim_supports_unit(unit, claim)

    cohort = _unit(
        _SHARED_SCOPE_QUESTION,
        _SHARED_SCOPE_ASPECTS,
        subject="",
        objects=(),
        version="2031",
    )
    to_claim = _claim(
        "c2",
        "在2031至2032级示例专业培养方案中，学分为4，开课学期为夏，课程性质为任选。",
    )
    scoped = _scoped_content(cohort, to_claim, to_claim.text, None)
    assert "学分为4" in scoped
    assert "开课学期为夏" in scoped
    assert "课程性质为任选" in scoped
    assert _claim_supports_unit(cohort, to_claim)


def test_separate_version_declarations_do_not_borrow_a_shared_range() -> None:
    mixed = _claim(
        "c1",
        "2031级示例专业培养方案中学分为4，2032级示例专业培养方案中学分为5，"
        "开课学期为夏。",
    )
    cohort = _unit(
        _SHARED_SCOPE_QUESTION,
        _SHARED_SCOPE_ASPECTS,
        subject="",
        objects=(),
        version="2031",
    )
    scoped = _scoped_content(cohort, mixed, mixed.text, None)
    assert "学分为4" in scoped
    assert "学分为5" not in scoped
    assert "开课学期" not in scoped

    extra_year = _claim(
        "c2",
        "在2031—2032级示例专业培养方案中，学分为4，"
        "2033级示例专业培养方案中学分为5，开课学期为夏。",
    )
    shared = _unit(
        _SHARED_SCOPE_QUESTION,
        _SHARED_SCOPE_ASPECTS,
        subject="",
        objects=(),
        version="2031-2032",
    )
    scoped = _scoped_content(shared, extra_year, extra_year.text, None)
    assert "学分为5" not in scoped
    assert "开课学期" not in scoped
    assert "学分为4" not in scoped


# ---------------------------------------------------------- syntactic course title
_COURSE_QUESTION = (
    "某届数据管理专业课程12345678901234信息检索与分析的学分、开课学期和课程性质是什么？"
)
_COURSE_CLAIM = (
    "信息检索与分析的学分为3学分，开课学期为第5学期，课程性质为专业选修课。"
)


def test_syntactic_course_title_survives_conjunctions_without_quotes() -> None:
    terms = _syntactic_course_terms(_COURSE_QUESTION)
    assert terms == ("信息检索与分析",)
    clauses, topics = _question_clauses(_COURSE_QUESTION, terms)
    assert topics == []
    assert clauses == ["信息检索与分析的学分", "开课学期", "课程性质"]
    assert all("某届" not in clause for clause in clauses)
    assert all("12345678901234" not in clause for clause in clauses)


def test_course_title_scope_prefix_is_not_a_separate_answer() -> None:
    unit = _unit(_COURSE_QUESTION, [_COURSE_QUESTION], subject="", objects=())
    audit = _audit(
        unit,
        [_claim("c1", _COURSE_CLAIM)],
        status="generation_missing",
        claim_ids=[],
    )
    assert audit.units[0]["status"] == "answered"
    assert audit.units[0]["claim_ids"] == ["c1"]
    assert audit.missing == []

    partial = _claim("c1", "信息检索与分析的学分为3学分，开课学期为第5学期。")
    audit = _audit(unit, [partial], status="answered", claim_ids=["c1"])
    assert audit.units[0]["status"] == "generation_missing"
    assert any("课程性质" in item for item in audit.missing)


# --------------------------------------------------- eligibility credits vs ratio
def test_credit_ratio_eligibility_does_not_require_graduation_credits() -> None:
    for question in (
        "本科修满培养方案总学分的多少比例时就不允许转专业",
        "本科修满培养方案总学分的80%时就不允许转专业",
    ):
        assert "毕业学分" not in requested_aspects(question)
    assert "毕业学分" in requested_aspects("本科毕业学分要求是多少？")


# ------------------------------------------------------ scoped duration/threshold
_THRESHOLD_QUESTION = "示例事项应在开学几周内备案？"
_DURATION_TRIAD = "学生休学一般以多久为一期、累计最多多久、创业休学最多多久？"


def _duration_props(question: str, protected: tuple[str, ...] = ()):
    return [
        prop
        for prop in _requested_properties(question, protected)
        if prop.kind == "duration"
    ]


def test_threshold_duration_binds_scope_and_action() -> None:
    props = _duration_props(_THRESHOLD_QUESTION)
    assert len(props) == 1
    assert props[0].core == "开学"
    assert props[0].action == "备案"
    assert _property_supported(props[0], "开学3周内备案。")
    assert not _property_supported(props[0], "放假3周内备案。")
    assert not _property_supported(props[0], "开学3周内公布。")


def test_threshold_fallback_question_words_are_not_substantive_gaps() -> None:
    question = "示例事项又应在开学几周内备案？"
    unit = _unit(question, [question], subject="", objects=())
    audit = _audit(
        unit,
        [_claim("c1", "开学3周内备案。")],
        status="generation_missing",
        claim_ids=[],
    )
    assert audit.units[0]["status"] == "answered"
    assert audit.units[0]["claim_ids"] == ["c1"]
    assert audit.missing == []


def test_eligibility_duration_accepts_answer_without_repeating_action() -> None:
    question = "入学多久才可以转专业？"
    props = _duration_props(question)
    assert len(props) == 1
    assert props[0].core == "入学"
    assert props[0].action == "转专业"
    assert _property_supported(props[0], "入学满一学期。")
    assert not _property_supported(props[0], "毕业满一学期。")
    assert not _property_supported(props[0], "转专业满一学期。")
    assert not _property_supported(props[0], "入学满一学期后申请休学。")

    unit = _unit(question, [question], subject="", objects=())
    audit = _audit(
        unit,
        [_claim("c1", "入学满一学期。")],
        status="generation_missing",
        claim_ids=[],
    )
    assert audit.units[0]["status"] == "answered"
    assert audit.missing == []


def test_duration_anchor_keeps_cohort_and_drops_action_context() -> None:
    compound = _duration_props("申请转专业需要入学多久？")
    assert len(compound) == 1
    assert compound[0].core == "入学"
    assert _property_supported(compound[0], "入学已满一学期。")
    assert not _property_supported(compound[0], "毕业满一学期。")

    modal = _duration_props("入学多久就可以转专业？")
    assert len(modal) == 1
    assert modal[0].core == "入学"
    assert modal[0].action == "转专业"

    cohort = _duration_props("本科生入学多久可以转专业？")
    assert len(cohort) == 1
    assert cohort[0].core == "本科生入学"
    assert _property_supported(cohort[0], "本科生入学满一学期。")
    assert not _property_supported(cohort[0], "研究生入学满一学期。")

    scoped = _duration_props(f"{PROGRAM}入学多久可以申请毕业？", (PROGRAM,))
    assert len(scoped) == 1
    assert scoped[0].core == f"{PROGRAM}入学"
    assert _property_supported(scoped[0], f"{PROGRAM}入学满一学期。")
    assert not _property_supported(scoped[0], "信息管理微专业入学满一学期。")


def test_scoped_durations_stay_distinct_and_reject_sibling_values() -> None:
    props = _duration_props(_DURATION_TRIAD)
    assert [prop.core for prop in props] == ["休学", "累计", "创业休学"]
    per_period, cumulative, entrepreneurial = props
    assert _property_supported(per_period, "学生休学一般以一年为一期。")
    assert _property_supported(cumulative, "累计最多两年。")
    assert _property_supported(entrepreneurial, "创业休学最多四年。")
    assert not _property_supported(per_period, "学生休学累计最多两年。")
    assert not _property_supported(cumulative, "创业休学最多四年。")
    assert not _property_supported(entrepreneurial, "累计最多两年。")


def test_multi_claim_scoped_durations_join_but_partial_stays_missing() -> None:
    unit = _unit(_DURATION_TRIAD, [_DURATION_TRIAD], subject="", objects=())
    claims = [
        _claim("c1", "学生休学一般以一年为一期。"),
        _claim("c2", "累计最多两年。"),
        _claim("c3", "创业休学最多四年。"),
    ]
    audit = _audit(unit, claims, status="generation_missing", claim_ids=[])
    assert audit.units[0]["status"] == "answered"
    assert audit.units[0]["claim_ids"] == ["c1", "c2", "c3"]
    assert audit.missing == []

    partial = _audit(unit, [claims[1]], status="generation_missing", claim_ids=[])
    assert partial.units[0]["status"] == "generation_missing"
    assert any("学生休学" in item for item in partial.missing)
    assert any("创业休学" in item for item in partial.missing)


def test_property_echo_drops_surface_words_but_keeps_the_other_action() -> None:
    question = "示例事项又应在开学几周内备案，逾期如何处理？"
    unit = _unit(question, [question], subject="", objects=())
    only_duration = _audit(
        unit,
        [_claim("c1", "开学3周内备案。")],
        status="generation_missing",
        claim_ids=[],
    )
    assert only_duration.units[0]["status"] == "generation_missing"
    assert any("逾期" in item for item in only_duration.missing)

    both = _audit(
        unit,
        [_claim("c1", "开学3周内备案，逾期未备案的按规定处理。")],
        status="generation_missing",
        claim_ids=[],
    )
    assert both.units[0]["status"] == "answered"
    assert both.units[0]["claim_ids"] == ["c1"]


# --------------------------------------------------------- ratio lower/upper bound
_RATIO_BOUND_QUESTION = "接收人数相对于基准人数的原则下限和上限分别是多少？"
_RATIO_BOUND_CLAIM = "接收人数原则上为基准人数的20%—30%。"


def test_ratio_bound_accepts_one_scoped_percentage_range() -> None:
    bounds = [
        prop for prop in _requested_properties(_RATIO_BOUND_QUESTION) if prop.kind == "bound"
    ]
    assert sorted(prop.direction for prop in bounds) == ["max", "min"]
    assert all(prop.measure == "比例" for prop in bounds)
    assert all(_property_supported(prop, _RATIO_BOUND_CLAIM) for prop in bounds)
    assert not any(
        _property_supported(prop, "教师人数相对于基准人数的20%—30%。")
        for prop in bounds
    )
    absolute = "接收人数原则上为基准人数的20人。"
    assert all(not _property_supported(prop, absolute) for prop in bounds)


def test_ratio_bound_fallback_question_is_answered_by_the_range() -> None:
    unit = _unit(
        _RATIO_BOUND_QUESTION, [_RATIO_BOUND_QUESTION], subject="", objects=()
    )
    audit = _audit(
        unit,
        [_claim("c1", _RATIO_BOUND_CLAIM)],
        status="generation_missing",
        claim_ids=[],
    )
    assert audit.units[0]["status"] == "answered"
    assert audit.units[0]["claim_ids"] == ["c1"]


# ------------------------------------------------------ global unanswered echoes
def test_global_question_echo_does_not_override_proven_property_answer() -> None:
    question = f"{SUBJECT}{PROGRAM}是否需要缴费？"
    unit = _unit(question, ["费用"])
    claim = _claim("c1", f"{SUBJECT}{PROGRAM}不收费。")
    audit = _audit(unit, [claim], unanswered=[question])
    assert audit.units[0]["status"] == "answered"
    assert audit.units[0]["claim_ids"] == ["c1"]
    assert audit.missing == []


def test_global_property_echo_is_redundant_but_list_omission_survives() -> None:
    question = "示例事项应在开学几周内备案？"
    unit = _unit(question, ["学制"], subject="", objects=())
    claim = _claim("c1", "开学3周内备案。")
    redundant = _audit(unit, [claim], unanswered=["开学几周"])
    assert redundant.units[0]["status"] == "answered"
    assert redundant.missing == []

    omission = _audit(unit, [claim], unanswered=["第3项未列出"])
    assert omission.units[0]["status"] == "generation_missing"
    assert any("第3项" in item for item in omission.missing)
