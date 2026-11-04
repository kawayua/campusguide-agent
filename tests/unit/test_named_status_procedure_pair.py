from campusguide.domain.enums import OutcomeCode
from campusguide.qa.guard import check_guard


def _assert_allowed(question: str) -> None:
    decision = check_guard(question)
    assert decision.allowed
    assert decision.outcome is None


def _assert_privacy_blocked(question: str) -> None:
    decision = check_guard(question)
    assert not decision.allowed
    assert decision.outcome is OutcomeCode.PRIVACY_REQUEST


def test_named_warning_removal_procedure_allowed() -> None:
    _assert_allowed("张三的红色学业预警怎么解除，应该去哪申诉？")


def test_named_warning_resolved_status_private() -> None:
    _assert_privacy_blocked("张三的红色学业预警是否已解除？")


def test_named_dropout_application_procedure_allowed() -> None:
    _assert_allowed("李四的退学申请怎么办理？")


def test_named_dropout_result_query_private() -> None:
    _assert_privacy_blocked("李四的退学申请如何查询办理结果？")


def test_named_dropout_appeal_procedure_allowed() -> None:
    _assert_allowed("李四的退学申请去哪申诉？")


def test_named_dropout_direct_confirmation_private() -> None:
    _assert_privacy_blocked("请直接确认李四是否已经办完退学")


def test_named_warning_access_query_private() -> None:
    _assert_privacy_blocked("张三的红色学业预警如何查询是否已解除")


def test_named_warning_access_query_with_direct_confirmation_private() -> None:
    _assert_privacy_blocked("张三的红色学业预警如何查并请直接确认是否已解除")


def test_mixed_procedure_and_direct_value_same_clause_private() -> None:
    _assert_privacy_blocked("李四的退学申请怎么办理并告诉我办理结果")


def test_mixed_procedure_and_direct_value_separate_clause_private() -> None:
    _assert_privacy_blocked("李四的退学申请怎么办理，告诉我办理结果")


def test_public_discipline_rule_with_named_record_query_separate_clause_private() -> None:
    _assert_privacy_blocked("学校纪律处分规定有哪些？另外，赵六的处分记录在哪里查？")


def test_public_discipline_rule_with_named_record_query_same_clause_private() -> None:
    _assert_privacy_blocked("学校纪律处分规定中李四的处分记录怎么查")


def test_generic_dropout_result_procedure_allowed() -> None:
    _assert_allowed("如何查询退学办理结果")


def test_generic_discipline_record_procedure_allowed() -> None:
    _assert_allowed("如何查询处分记录")


def test_self_dropout_result_procedure_allowed() -> None:
    _assert_allowed("我如何查询自己的办理结果")


def test_self_discipline_record_procedure_allowed() -> None:
    _assert_allowed("我如何查询自己的处分记录")


def test_named_self_service_dropout_result_procedure_allowed() -> None:
    _assert_allowed("李四如何查询自己的办理结果")


# Intent test: distinguishes an access instruction from an actual record fetch.
def test_mixed_public_policy_with_self_discipline_access_query_allowed() -> None:
    _assert_allowed(
        "考试迟到、携带手机或者替考，分别涉及哪些考场规定和纪律处分？"
        "另外，我的处分记录在哪里查？"
    )


def test_mixed_public_policy_with_self_discipline_fetch_private() -> None:
    _assert_privacy_blocked(
        "考试迟到、携带手机或者替考，分别涉及哪些考场规定和纪律处分？"
        "另外，请直接查出我的处分记录并告诉我处分等级。"
    )


def test_self_discipline_record_access_channel_allowed() -> None:
    _assert_allowed("我的处分记录在哪里查？")


def test_named_third_party_discipline_record_access_query_private() -> None:
    _assert_privacy_blocked("赵六的处分记录在哪里查？")


def test_polite_preface_named_dropout_procedure_allowed() -> None:
    _assert_allowed("请告诉我张三的退学申请怎么办理？")


def test_bare_polite_preface_named_dropout_procedure_allowed() -> None:
    _assert_allowed("告诉我张三的退学申请怎么办理？")


def test_polite_explanation_named_dropout_procedure_allowed() -> None:
    _assert_allowed("帮我解释张三的退学申请怎么办理")


def test_polite_preface_named_dropout_form_and_appeal_allowed() -> None:
    _assert_allowed("请告诉我张三的退学申请怎么填表/怎么申诉")


def test_polite_preface_named_dropout_status_private() -> None:
    _assert_privacy_blocked("告诉我张三是否已办理退学")


def test_polite_preface_named_dropout_result_private() -> None:
    _assert_privacy_blocked("请告诉我李四的退学申请怎么办理，告诉我办理结果")


def test_polite_preface_named_discipline_record_procedure_private() -> None:
    _assert_privacy_blocked("请告诉我赵六的处分记录怎么查")
