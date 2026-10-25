from __future__ import annotations

from datetime import date

import pytest

from campusguide.domain.models import SlotDefinition
from campusguide.flows.predicates import (
    PredicateError,
    Truth,
    evaluate_bool,
    evaluate_predicate,
    parse_predicate,
    slot_names,
    validate_predicate,
)


def slot(name: str, type_: str, **kwargs) -> SlotDefinition:
    return SlotDefinition(
        name=name,
        type=type_,
        required=kwargs.pop("required", True),
        question="q",
        why_needed="w",
        allowed_values=kwargs.pop("allowed_values", None),
        validation=kwargs.pop("validation", None),
        priority=kwargs.pop("priority", 100),
    )


SLOTS = {
    "grade": slot("grade", "integer"),
    "college": slot("college", "enum", allowed_values=["info", "agri"]),
    "has_record": slot("has_record", "boolean"),
    "purpose": slot("purpose", "enum", allowed_values=["升学", "就业"]),
}


def ev(expr: str, values: dict | None = None, today: date = date(2026, 10, 25)) -> Truth:
    ast = validate_predicate(expr, SLOTS)
    return evaluate_predicate(ast, values or {}, today=today)


def test_truth_table_and_or_not() -> None:
    assert ev("slots.grade >= 2", {"grade": 3}) is Truth.TRUE
    assert ev("slots.grade >= 2", {"grade": 1}) is Truth.FALSE
    assert ev("slots.grade >= 2", {}) is Truth.UNKNOWN
    assert ev("not slots.grade >= 2", {"grade": 1}) is Truth.TRUE
    assert ev("not slots.grade >= 2", {}) is Truth.UNKNOWN
    assert ev("slots.grade >= 2 and slots.has_record == false", {"grade": 3}) is Truth.UNKNOWN
    assert (
        ev("slots.grade >= 2 and slots.has_record == false", {"grade": 3, "has_record": False})
        is Truth.TRUE
    )
    assert ev("slots.grade >= 2 or slots.has_record == false", {"has_record": False}) is Truth.TRUE
    assert ev("slots.grade > 2 and slots.has_record == false", {"grade": 1}) is Truth.FALSE


def test_in_and_enum() -> None:
    assert ev('slots.college in ["info", "agri"]', {"college": "info"}) is Truth.TRUE
    assert ev('slots.college in ["info", "agri"]', {"college": "other"}) is Truth.FALSE
    assert ev('slots.purpose in ["升学", "就业"]', {}) is Truth.UNKNOWN


def test_date_comparison_and_today() -> None:
    assert ev("today >= 2026-10-01", today=date(2026, 10, 25)) is Truth.TRUE
    assert ev("today > 2026-10-25", today=date(2026, 10, 25)) is Truth.FALSE


def test_type_errors_are_load_time() -> None:
    with pytest.raises(PredicateError, match="类型不匹配"):
        validate_predicate('slots.grade == "3"', SLOTS)
    with pytest.raises(PredicateError, match="类型不匹配"):
        validate_predicate("slots.college == 1", SLOTS)
    with pytest.raises(PredicateError, match="只支持"):
        validate_predicate('slots.college > "a"', SLOTS)
    with pytest.raises(PredicateError, match="未定义槽位"):
        validate_predicate("slots.missing == 1", SLOTS)
    with pytest.raises(PredicateError, match="未定义常量"):
        validate_predicate("constants.unknown == 1", SLOTS)


def test_syntax_errors() -> None:
    for expression in [
        "",
        "slots.grade",
        "slots.grade ==",
        "slots.grade == 1 and",
        "(slots.grade == 1",
        "slots.college in []",
    ]:
        with pytest.raises(PredicateError):
            parse_predicate(expression)


def test_slot_names_and_bool_helper() -> None:
    ast = validate_predicate("slots.grade >= 2 and slots.has_record == false", SLOTS)
    assert slot_names(ast) == {"grade", "has_record"}
    assert evaluate_bool(ast, {"grade": 2, "has_record": False}, today=date(2026, 10, 25)) is True
    assert evaluate_bool(ast, {"grade": 2}, today=date(2026, 10, 25)) is None


def test_no_eval_attribute_access_or_calls() -> None:
    for expression in [
        "__import__('os').system('echo x') == 0",
        "slots.grade.__class__ == 1",
        "len(slots.grade) > 1",
        "slots.grade + 1 == 2",
    ]:
        with pytest.raises(PredicateError):
            parse_predicate(expression)
