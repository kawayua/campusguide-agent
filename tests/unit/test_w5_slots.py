from __future__ import annotations

from datetime import date

from campusguide.domain.models import SlotDefinition
from campusguide.flows.slots import extract_slots, validate_slot_value


def slot(name: str, type_: str, *, allowed=None, aliases=None, validation=None, required=True):
    return SlotDefinition(
        name=name,
        type=type_,
        required=required,
        question="q",
        why_needed="w",
        allowed_values=allowed,
        aliases=aliases,
        validation=validation,
        priority=10,
    )


COLLEGE = slot(
    "college",
    "enum",
    allowed=["info", "agri", "other"],
    aliases={
        "info": ["数学与信息学院", "数信学院", "数信", "软件学院"],
        "agri": ["农学院"],
        "other": ["其他学院", "其他"],
    },
)
GRADE = slot("grade", "integer", validation={"min": 1, "max": 5})
RECORD = slot("has_record", "boolean")
PURPOSE = slot(
    "purpose",
    "enum",
    allowed=["study", "employment", "overseas", "other"],
    aliases={
        "study": ["升学", "考研", "保研"],
        "employment": ["就业", "找工作"],
        "overseas": ["出国", "留学"],
        "other": ["其他"],
    },
)


def extract(message: str, *, pending: str | None = None, existing=None, defs=None):
    return extract_slots(
        message,
        defs or [COLLEGE, GRADE, RECORD, PURPOSE],
        turn_id=1,
        existing=existing or {},
        pending_slot=pending,
    )


def test_extract_multiple_slots_in_one_message() -> None:
    result = extract("我是数信学院大二，没受过处分，想申请国家奖学金")
    assert result.values["college"].value == "info"
    assert result.values["grade"].value == 2
    assert result.values["has_record"].value is False


def test_enum_aliases_and_longest_match() -> None:
    result = extract("数学与信息学院、软件学院，大三")
    assert result.values["college"].value == "info"
    result = extract("我是农学院的")
    assert result.values["college"].value == "agri"


def test_pending_boolean_disambiguates() -> None:
    assert extract("没有", pending="has_record").values["has_record"].value is False
    assert extract("有", pending="has_record").values["has_record"].value is True
    assert extract("不确定", pending="has_record").declined_slots == ["has_record"]


def test_do_not_guess_from_ambiguous_text() -> None:
    assert "has_record" not in extract("我表现还可以").values
    assert "grade" not in extract("2026 年").values
    assert "college" not in extract("华农").values


def test_grade_range_and_chinese_terms() -> None:
    assert extract("大四").values["grade"].value == 4
    assert extract("研二").values["grade"].value == 2
    assert "grade" not in extract("大六").values


def test_date_and_string_pending() -> None:
    d = slot("when", "date")
    result = extract_slots("2026年10月25日", [d], turn_id=2, pending_slot="when")
    assert result.values["when"].value == date(2026, 10, 25)
    s = slot("note", "string")
    result = extract_slots("需要密封", [s], turn_id=2, pending_slot="note")
    assert result.values["note"].value == "需要密封"


def test_overwrite_and_no_overwrite() -> None:
    existing = {"grade": type("V", (), {"value": 2})()}
    result = extract("大三", existing=existing)
    assert result.values["grade"].value == 3
    result = extract("大三", existing=existing)
    result = extract_slots("大三", [GRADE], turn_id=2, existing=existing, allow_overwrite=False)
    assert not result.values


def test_validate_slot_value() -> None:
    assert validate_slot_value(GRADE, 3)
    assert not validate_slot_value(GRADE, 6)
    assert validate_slot_value(RECORD, False)
    assert not validate_slot_value(RECORD, "否")
    assert validate_slot_value(COLLEGE, "info")
    assert not validate_slot_value(COLLEGE, "数学与信息学院")
    assert validate_slot_value(PURPOSE, "overseas")
    assert not validate_slot_value(PURPOSE, "出国")
