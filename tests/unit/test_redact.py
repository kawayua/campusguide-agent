from __future__ import annotations

from campusguide.ingest.redact import redact_text


def test_redacts_mobile_id_and_student_id() -> None:
    text = "联系人：张老师 13800138000，身份证 440101199001011234，学号：2023123456。"
    result = redact_text(text)
    assert "13800138000" not in result.text
    assert "440101199001011234" not in result.text
    assert "2023123456" not in result.text
    assert "[手机号已脱敏]" in result.text
    assert "[身份证号已脱敏]" in result.text
    assert "[学号已脱敏]" in result.text
    assert result.counts["mobile"] == 1
    assert result.counts["id_card"] == 1
    assert result.counts["student_id"] == 1


def test_keeps_office_landline_and_scau_email() -> None:
    text = "咨询电话：020-85280000，邮箱：jwc@scau.edu.cn。"
    result = redact_text(text)
    assert "020-85280000" in result.text
    assert "jwc@scau.edu.cn" in result.text
    assert not result.counts


def test_redacts_personal_email_and_contact_name() -> None:
    text = "负责人：李四，邮箱 lisi@example.com。"
    result = redact_text(text)
    assert "李四" not in result.text
    assert "lisi@example.com" not in result.text
    assert "负责人：[姓名已脱敏]" in result.text
    assert result.counts["personal_email"] == 1
    assert result.counts["contact_name"] == 1


def test_redaction_is_idempotent() -> None:
    once = redact_text("电话 13800138000").text
    twice = redact_text(once).text
    assert once == twice
