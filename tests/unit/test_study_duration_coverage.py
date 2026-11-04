import dataclasses

import pytest

from campusguide.agents.coverage_study_duration import (
    StudyDurationField,
    study_duration_fields,
    study_duration_supported,
)

QUESTION_A = (
    "三年制与六年制专业通常最长可以学习几年，"
    "休学创业时最长年限分别是多少？"
)
ANSWER_A = (
    "三年制专业最长7年（休学创业8年），"
    "六年制专业最长9年（创业10年）"
)
QUESTION_B = "全日制、非全日制硕士的学制和最长学习年限分别几年？"
ANSWER_B = (
    "全日制学制3年，非全日制学制3年；"
    "全日制最长5年，非全日制最长5年"
)


def _by_key(question):
    fields = study_duration_fields(question)
    return {(field.condition, field.kind): field for field in fields}


def _field(question, condition, kind):
    return _by_key(question)[(condition, kind)]


def _all_supported(fields, answer):
    return all(study_duration_supported(field, answer) for field in fields)


def _any_supported(fields, answer):
    return any(study_duration_supported(field, answer) for field in fields)


def test_year_system_fields_cover_each_condition_and_kind():
    fields = study_duration_fields(QUESTION_A)
    assert {(field.condition, field.kind) for field in fields} == {
        ("三年制", "ordinary_max"),
        ("三年制", "startup_max"),
        ("六年制", "ordinary_max"),
        ("六年制", "startup_max"),
    }
    assert len(fields) == 4
    assert _all_supported(fields, ANSWER_A)


def test_arabic_year_systems_are_parsed():
    question = "3年制与6年制专业最长年限分别是多少？"
    fields = study_duration_fields(question)
    assert {(field.condition, field.kind) for field in fields} == {
        ("3年制", "ordinary_max"),
        ("6年制", "ordinary_max"),
    }
    answer = "3年制专业最长7年，6年制专业最长9年"
    assert _all_supported(fields, answer)


def test_startup_parenthesis_inherits_nearest_preceding_condition():
    answer = "三年制专业最长7年，六年制专业最长9年（休学创业10年）"
    assert study_duration_supported(
        _field(QUESTION_A, "三年制", "ordinary_max"), answer
    )
    assert not study_duration_supported(
        _field(QUESTION_A, "三年制", "startup_max"), answer
    )
    assert study_duration_supported(
        _field(QUESTION_A, "六年制", "ordinary_max"), answer
    )
    assert study_duration_supported(
        _field(QUESTION_A, "六年制", "startup_max"), answer
    )


def test_missing_startup_branch_fails_only_that_field():
    answer = "三年制专业最长7年（休学创业8年），六年制专业最长9年"
    assert study_duration_supported(
        _field(QUESTION_A, "三年制", "startup_max"), answer
    )
    assert not study_duration_supported(
        _field(QUESTION_A, "六年制", "startup_max"), answer
    )
    assert study_duration_supported(
        _field(QUESTION_A, "三年制", "ordinary_max"), answer
    )
    assert study_duration_supported(
        _field(QUESTION_A, "六年制", "ordinary_max"), answer
    )


def test_startup_values_do_not_satisfy_ordinary_maximum():
    answer = "三年制专业（休学创业8年），六年制专业（创业10年）"
    for field in study_duration_fields(QUESTION_A):
        if field.kind == "ordinary_max":
            assert not study_duration_supported(field, answer)
        else:
            assert study_duration_supported(field, answer)


def test_later_branch_can_inherit_shared_maximum_topic():
    answer = "三年制专业最长7年（休学创业8年），六年制专业9年（创业10年）"
    assert _all_supported(study_duration_fields(QUESTION_A), answer)
    prefixed = "最长学习年限：三年制专业7年（休学创业8年），六年制专业9年（创业10年）"
    assert _all_supported(study_duration_fields(QUESTION_A), prefixed)


def test_unknown_quote_echo_and_calendar_are_not_supported():
    fields = study_duration_fields(QUESTION_A)
    assert not _any_supported(fields, "暂未公布")
    assert not _any_supported(fields, QUESTION_A)
    assert not _any_supported(fields, "“" + ANSWER_A + "”")
    assert not _any_supported(fields, "三年制专业最长7年（休学创业8年）吗？")
    calendar = "三年制专业最长2026年（休学创业8年），六年制专业最长9年（创业10年）"
    assert not study_duration_supported(
        _field(QUESTION_A, "三年制", "ordinary_max"), calendar
    )
    assert study_duration_supported(
        _field(QUESTION_A, "三年制", "startup_max"), calendar
    )


def test_outer_question_echo_is_branch_local():
    answer = "三年制专业最长7年（休学创业8年）吗？六年制专业最长9年（创业10年）"
    assert not study_duration_supported(
        _field(QUESTION_A, "三年制", "ordinary_max"), answer
    )
    assert not study_duration_supported(
        _field(QUESTION_A, "三年制", "startup_max"), answer
    )
    assert study_duration_supported(
        _field(QUESTION_A, "六年制", "ordinary_max"), answer
    )
    assert study_duration_supported(
        _field(QUESTION_A, "六年制", "startup_max"), answer
    )
    localized = "三年制专业最长7年（休学创业是否8年？）"
    assert study_duration_supported(
        _field(QUESTION_A, "三年制", "ordinary_max"), localized
    )
    assert not study_duration_supported(
        _field(QUESTION_A, "三年制", "startup_max"), localized
    )


def test_condition_years_and_grade_tokens_are_not_answer_values():
    field = _field(QUESTION_A, "三年制", "ordinary_max")
    assert not study_duration_supported(field, "仅三年制专业，最长学习年限尚未公布")
    assert not study_duration_supported(field, "三年制专业最长3年级")


def test_study_duration_field_is_immutable():
    field = StudyDurationField("三年制", "ordinary_max", "最长")
    with pytest.raises(dataclasses.FrozenInstanceError):
        field.condition = "六年制"


def test_mode_fields_cover_basic_and_maximum():
    fields = study_duration_fields(QUESTION_B)
    assert {(field.condition, field.kind) for field in fields} == {
        ("全日制", "basic"),
        ("全日制", "maximum"),
        ("非全日制", "basic"),
        ("非全日制", "maximum"),
    }
    assert len(fields) == 4
    assert _all_supported(fields, ANSWER_B)


def test_missing_parttime_maximum_does_not_borrow_fulltime_value():
    answer = "全日制学制3年，非全日制学制3年；全日制最长5年"
    assert study_duration_supported(
        _field(QUESTION_B, "全日制", "maximum"), answer
    )
    assert not study_duration_supported(
        _field(QUESTION_B, "非全日制", "maximum"), answer
    )
    assert study_duration_supported(
        _field(QUESTION_B, "非全日制", "basic"), answer
    )
    inverse = "全日制学制3年，非全日制学制3年；非全日制最长5年"
    assert not study_duration_supported(
        _field(QUESTION_B, "全日制", "maximum"), inverse
    )
    assert study_duration_supported(
        _field(QUESTION_B, "非全日制", "maximum"), inverse
    )


def test_parttime_mode_is_not_matched_as_fulltime():
    answer = "非全日制学制3年，非全日制最长5年"
    assert not study_duration_supported(
        _field(QUESTION_B, "全日制", "basic"), answer
    )
    assert not study_duration_supported(
        _field(QUESTION_B, "全日制", "maximum"), answer
    )
    assert study_duration_supported(
        _field(QUESTION_B, "非全日制", "basic"), answer
    )
    assert study_duration_supported(
        _field(QUESTION_B, "非全日制", "maximum"), answer
    )


def test_shared_assertion_covers_both_modes_when_marked():
    answer = "全日制和非全日制学制均为3年，最长学习年限均为5年"
    assert _all_supported(study_duration_fields(QUESTION_B), answer)
    duplicated = "全日制和非全日制学制都是3年，最长学习年限都是5年"
    assert _all_supported(study_duration_fields(QUESTION_B), duplicated)


def test_unmarked_mixture_does_not_inherit_across_modes():
    answer = "全日制学制3年，非全日制学制3年；最长学习年限均为5年"
    assert study_duration_supported(_field(QUESTION_B, "全日制", "basic"), answer)
    assert study_duration_supported(_field(QUESTION_B, "非全日制", "basic"), answer)
    assert not study_duration_supported(_field(QUESTION_B, "全日制", "maximum"), answer)
    assert not study_duration_supported(
        _field(QUESTION_B, "非全日制", "maximum"), answer
    )


def test_basic_without_maximum_does_not_fill_maximum():
    answer = "全日制和非全日制学制均为3年"
    assert study_duration_supported(_field(QUESTION_B, "全日制", "basic"), answer)
    assert study_duration_supported(_field(QUESTION_B, "非全日制", "basic"), answer)
    assert not study_duration_supported(_field(QUESTION_B, "全日制", "maximum"), answer)
    assert not study_duration_supported(
        _field(QUESTION_B, "非全日制", "maximum"), answer
    )


def test_question_with_one_property_returns_only_those_fields():
    question = "全日制、非全日制硕士的学制分别几年？"
    fields = study_duration_fields(question)
    assert {(field.condition, field.kind) for field in fields} == {
        ("全日制", "basic"),
        ("非全日制", "basic"),
    }
    answer = "全日制学制3年，非全日制学制3年"
    assert _all_supported(fields, answer)
    max_question = "全日制、非全日制硕士的最长学习年限分别几年？"
    max_fields = study_duration_fields(max_question)
    assert {(field.condition, field.kind) for field in max_fields} == {
        ("全日制", "maximum"),
        ("非全日制", "maximum"),
    }
    max_answer = "全日制最长5年，非全日制最长5年"
    assert _all_supported(max_fields, max_answer)


def test_unsupported_questions_return_no_fields():
    assert study_duration_fields("今天天气怎么样？") == []
    assert study_duration_fields("全日制和非全日制有什么区别？") == []
    assert study_duration_fields("四年制和五年制专业是什么？") == []


def test_uncertain_mode_value_does_not_assert_or_drop_other_branch():
    answer = "全日制学制是否为3年尚不明确；非全日制学制4年"
    assert not study_duration_supported(
        _field(QUESTION_B, "全日制", "basic"), answer
    )
    assert study_duration_supported(
        _field(QUESTION_B, "非全日制", "basic"), answer
    )
    questioned = "全日制学制3年吗；非全日制学制4年"
    assert not study_duration_supported(
        _field(QUESTION_B, "全日制", "basic"), questioned
    )
    assert study_duration_supported(
        _field(QUESTION_B, "非全日制", "basic"), questioned
    )


def test_uncertain_year_system_values_are_not_proof():
    answer = (
        "三年制专业最长是否为7年尚不明确（休学创业8年尚不明确），"
        "六年制专业最长9年（创业10年）"
    )
    assert not study_duration_supported(
        _field(QUESTION_A, "三年制", "ordinary_max"), answer
    )
    assert not study_duration_supported(
        _field(QUESTION_A, "三年制", "startup_max"), answer
    )
    assert study_duration_supported(
        _field(QUESTION_A, "六年制", "ordinary_max"), answer
    )
    assert study_duration_supported(
        _field(QUESTION_A, "六年制", "startup_max"), answer
    )


def test_ordinary_scope_with_suspension_does_not_require_startup():
    question = "四年制与五年制专业的最长学习年限含休学是多少？"
    fields = study_duration_fields(question)
    assert {(field.condition, field.kind) for field in fields} == {
        ("四年制", "ordinary_max"),
        ("五年制", "ordinary_max"),
    }
    assert len(fields) == 2
    answer = "四年制专业最长6年（含休学），五年制专业最长7年（含休学）"
    assert _all_supported(fields, answer)


def test_startup_only_question_does_not_invent_ordinary_fields():
    question = "三年制与六年制专业休学创业时最长年限分别是多少？"
    fields = study_duration_fields(question)
    assert {(field.condition, field.kind) for field in fields} == {
        ("三年制", "startup_max"),
        ("六年制", "startup_max"),
    }
    answer = "三年制专业（休学创业8年），六年制专业（创业10年）"
    assert _all_supported(fields, answer)


def test_reversed_startup_phrase_still_asks_both_kinds():
    question = (
        "三年制与六年制专业通常最长可以学习几年，"
        "创业休学时最长年限分别是多少？"
    )
    fields = study_duration_fields(question)
    assert {(field.condition, field.kind) for field in fields} == {
        ("三年制", "ordinary_max"),
        ("三年制", "startup_max"),
        ("六年制", "ordinary_max"),
        ("六年制", "startup_max"),
    }


def test_field_cues_include_condition_and_kind():
    assert _field(QUESTION_B, "全日制", "basic").cue == "全日制学制"
    assert (
        _field(QUESTION_B, "非全日制", "maximum").cue
        == "非全日制最长学习年限"
    )
    ordinary_question = "四年制与五年制专业的最长学习年限含休学是多少？"
    assert (
        _field(ordinary_question, "四年制", "ordinary_max").cue
        == "四年制最长学习年限"
    )
    assert (
        _field(QUESTION_A, "三年制", "startup_max").cue
        == "三年制创业最长学习年限"
    )


def test_procedure_background_does_not_manufacture_duration_fields():
    assert study_duration_fields(
        "全日制和非全日制学生在学制内如何申请成绩单？"
    ) == []
    assert study_duration_fields(
        "三年制与六年制学生在最长学习年限内如何补办证明？"
    ) == []
    assert study_duration_fields(
        "全日制和非全日制两种方式的学生在学制期间申请奖学金的步骤是什么？"
    ) == []
    assert study_duration_fields(
        "两种方式的学生在学制期间申请奖学金的步骤是什么"
    ) == []


def test_procedure_background_with_explicit_duration_ask_keeps_fields():
    transcript = (
        "全日制和非全日制学生在学制内如何申请成绩单？"
        "两种学制分别是几年？"
    )
    assert {
        (field.condition, field.kind)
        for field in study_duration_fields(transcript)
    } == {
        ("全日制", "basic"),
        ("非全日制", "basic"),
    }
    certificate = (
        "三年制与六年制学生在最长学习年限内如何补办证明？"
        "最长年限各是多少？"
    )
    assert {
        (field.condition, field.kind)
        for field in study_duration_fields(certificate)
    } == {
        ("三年制", "ordinary_max"),
        ("六年制", "ordinary_max"),
    }
    scholarship = (
        "全日制和非全日制两种方式的学生在学制期间申请奖学金的步骤是什么？"
        "学制分别是几年，最长学习年限分别是多少？"
    )
    assert {
        (field.condition, field.kind)
        for field in study_duration_fields(scholarship)
    } == {
        ("全日制", "basic"),
        ("全日制", "maximum"),
        ("非全日制", "basic"),
        ("非全日制", "maximum"),
    }


def test_explicit_duration_ask_cues_work_without_question_marks():
    for question in (
        "全日制和非全日制学制分别是几年",
        "全日制和非全日制学制分别是多少年",
        "全日制和非全日制学制分别要读多久",
        "全日制和非全日制学制多长",
        "全日制和非全日制最长年限各是多少",
        "全日制和非全日制学制和学习年限分别是多少",
    ):
        assert study_duration_fields(question), question


def test_study_system_keywords_and_modes_alone_are_not_enough():
    for question in (
        "全日制和非全日制学制的申请流程",
        "全日制和非全日制学制和最长学习年限的规定",
        "全日制和非全日制学生在学制期间的成绩单办理",
    ):
        assert study_duration_fields(question) == []


def test_bare_study_system_comparison_prompts_are_preserved():
    for question in (
        "全日制和非全日制学制对比",
        "全日制和非全日制学制比较",
        "全日制和非全日制学制的区别",
        "全日制和非全日制学制哪个更长",
    ):
        assert {
            (field.condition, field.kind)
            for field in study_duration_fields(question)
        } == {
            ("全日制", "basic"),
            ("非全日制", "basic"),
        }
