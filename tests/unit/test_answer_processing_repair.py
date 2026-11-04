from __future__ import annotations

import json
from types import SimpleNamespace

from campusguide.agents.coverage import (
    CoverageChecker,
    _aspect_supported,
    _requested_properties,
    _requirement_union_binding,
    _submission_is_material_scaffold,
)
from campusguide.agents.graph import _progress_signature
from campusguide.agents.plan import QuestionUnit
from campusguide.agents.providers import ChatProviderError, ChatResponse
from campusguide.agents.question_units import requested_aspects
from campusguide.domain.enums import ArticleStatus, Audience
from campusguide.domain.models import Article, Claim, Evidence
from campusguide.retrieval.profile_expand import ProfileLexicon
from campusguide.verification.semantic import SemanticLabel, SemanticVerifier


def _claim(claim_id: str, text: str, quote: str | None = None, article: str = "rule@v1#第一条") -> Claim:
    return Claim.model_validate(
        {
            "claim_id": claim_id,
            "text": text,
            "citations": [
                {
                    "article_id": article,
                    "quote": quote or text,
                    "quote_start": 0,
                    "quote_end": len(quote or text),
                    "doc_title": "测试办法",
                    "article_no": "第一条",
                    "part_no": 1,
                    "effective_from": "2024-01-01",
                    "date_confidence": "high",
                    "source_url": "https://example.edu/rule",
                }
            ],
        }
    )


def _supported(
    aspect: str,
    text: str,
    *,
    span: str = "",
    citation: str = "",
) -> bool:
    return _aspect_supported(aspect, text, text, citation, None, original_span=span)


# ---------------------------------------------------------------- CR066 decomposition


def test_organization_enumeration_is_not_an_eligibility_ask() -> None:
    for question in (
        "数学与信息学院有哪些学生组织和产业学院？",
        "学院有哪些学生社团、机构和协会？",
        "学院有哪些学生组织可以申请活动经费？",
    ):
        assert "申请条件" not in requested_aspects(question)


def test_real_student_eligibility_still_decomposes() -> None:
    assert "申请条件" in requested_aspects("哪些学生可以申请国家奖学金？")
    assert "申请条件" in requested_aspects("国家奖学金有什么申请条件？")


def test_student_person_roles_keep_eligibility() -> None:
    for question in (
        "哪些学生干部可以申请奖学金？",
        "哪些学生党员可以申请奖学金？",
        "哪些学生团员可以申请奖学金？",
        "哪些学生代表可以申请奖学金？",
        "学生干部可以申请奖学金吗？",
    ):
        assert "申请条件" in requested_aspects(question)


def test_student_person_role_without_apply_ask_is_not_eligibility() -> None:
    assert "申请条件" not in requested_aspects("学生干部名单在哪里公布？")
    assert "申请条件" not in requested_aspects("学院有哪些学生组织可以申请活动经费？")


def test_mixed_organization_and_eligibility_keeps_both_asks() -> None:
    aspects = requested_aspects("学院有哪些学生组织，哪些学生可以申请加入？")
    assert "申请条件" in aspects
    aspects = requested_aspects("学院有哪些学生组织，如何申请加入，需要什么条件？")
    assert "申请条件" in aspects
    assert "办理程序" in aspects


# ---------------------------------------------------------------- CR012 procedures

QUESTION = "本科生如何申请勤工助学岗位，每周或每月工作时长有什么限制？"
PROCEDURE = (
    "学生参加勤工助学活动的程序包括："
    "（一）学生本人提出书面申请，学院勤工助学工作小组审核、登记；"
    "（二）中心根据岗位设置情况，发布用人信息；"
    "（三）学生填写《勤工助学申请表》并由学院签署意见。"
)


def test_answer_side_procedure_with_concrete_steps_is_supported() -> None:
    assert _supported("办理程序", PROCEDURE, span=QUESTION)


def test_generic_or_non_procedural_wording_is_rejected() -> None:
    assert not _supported("办理程序", "办理程序按规定办理。", span=QUESTION)
    assert not _supported("办理程序", "程序是按规定办理。", span=QUESTION)
    assert not _supported("办理程序", "程序为：学生5月1日前申报，缴纳报名费。", span=QUESTION)
    assert not _supported(
        "办理程序",
        "程序包括：（一）5月1日截止；（二）缴纳报名费。",
        span=QUESTION,
    )


def test_procedure_for_another_event_is_rejected() -> None:
    other = "奖学金评定程序包括：（一）学生提交申请；（二）学院审核并公示。"
    assert not _supported("办理程序", other, span=QUESTION)


def test_existing_ask_wording_still_counts_when_topic_matches() -> None:
    claim = "如何申请：学生提交申请表，学院审核。"
    assert _supported("办理程序", claim, span=QUESTION, citation="勤工助学管理办法 第二十七条")


_STEPS = (
    "学生本人提出书面申请，学院勤工助学工作小组审核、登记",
    "中心根据岗位设置情况，发布用人信息",
    "学生填写《勤工助学申请表》并由学院签署意见",
    "中心组织面试",
    "学生与用人单位签订协议",
    "中心发放上岗通知",
    "学生上岗工作",
    "中心进行考核",
)


def _procedure_body(steps: tuple[str, ...] = _STEPS) -> str:
    markers = "一二三四五六七八"
    return (
        "学生参加勤工助学活动的程序包括："
        + "；".join(f"（{markers[index]}）{step}" for index, step in enumerate(steps))
        + "。"
    )


def _rule_article(text: str) -> Article:
    return Article(
        article_id="rule@v1#第一条",
        doc_id="rule",
        version_id="v1",
        doc_title="测试办法",
        article_no="第一条",
        article_index=1,
        text=text,
        text_normalized=text,
        audience=[Audience.UNDERGRADUATE],
        status=ArticleStatus.ACTIVE,
        effective_from="2024-01-01",
        source_url="https://example.edu/rule",
        published_at="2024-01-01",
        issuing_department="学生处",
        source_sha256="0" * 64,
    )


def _cr012_unit(
    *, version: str | None = None, class_type: str | None = None, span: str = QUESTION
) -> QuestionUnit:
    return QuestionUnit(
        unit_id="u1",
        original_span=span,
        audience_scope=["本科"],
        version=version,
        class_type=class_type,
        requested_aspects=["办理程序"],
    )


def _run_coverage(unit, claims, evidence, missing, *, lexicon=None):
    row = {
        "unit_id": unit.unit_id,
        "status": "generation_missing",
        "claim_ids": [],
        "missing_items": list(missing),
    }
    response = ChatResponse(
        content=json.dumps(
            {"units": [row], "unanswered_questions": []}, ensure_ascii=False
        )
    )
    provider = _ScriptedProvider([response])
    audit = CoverageChecker(provider, profile_lexicon=lexicon).check(
        unit.original_span, [unit], claims, evidence, []
    )
    return audit, provider


def test_generic_procedure_gap_recovers_only_with_every_source_step() -> None:
    body = _procedure_body()
    evidence = [Evidence(article=_rule_article(body))]
    claim = _claim("c1", body, quote=body)
    audit, provider = _run_coverage(
        _cr012_unit(), [claim], evidence, ["办理程序"]
    )
    row = audit.units[0]
    assert row["status"] == "answered"
    assert row["claim_ids"] == ["c1"]
    assert row["missing_items"] == []
    assert len(provider.calls) == 1


def test_procedure_gap_stays_when_later_source_steps_are_omitted() -> None:
    body = _procedure_body()
    evidence = [Evidence(article=_rule_article(body))]
    partial = _procedure_body(_STEPS[:2])
    claim = _claim("c1", partial, quote=partial)
    audit, _ = _run_coverage(_cr012_unit(), [claim], evidence, ["办理程序"])
    row = audit.units[0]
    assert row["status"] == "generation_missing"
    assert row["claim_ids"] == []
    assert row["missing_items"] == ["办理程序"]


def test_procedure_gap_stays_when_only_a_middle_source_step_is_omitted() -> None:
    body = _procedure_body()
    evidence = [Evidence(article=_rule_article(body))]
    partial = _procedure_body((*_STEPS[:2], *_STEPS[3:]))
    claim = _claim("c1", partial, quote=partial)
    audit, _ = _run_coverage(_cr012_unit(), [claim], evidence, ["办理程序"])
    row = audit.units[0]
    assert row["status"] == "generation_missing"
    assert row["claim_ids"] == []
    assert row["missing_items"] == ["办理程序"]


def test_procedure_gap_stays_for_wrong_event_scope_or_no_facts() -> None:
    body = _procedure_body()
    evidence = [Evidence(article=_rule_article(body))]
    wrong_event = _claim(
        "c1", "奖学金评定程序包括：（一）学生提交申请；（二）学院审核并公示。"
    )
    audit, _ = _run_coverage(_cr012_unit(), [wrong_event], evidence, ["办理程序"])
    assert audit.units[0]["status"] == "generation_missing"

    wrong_version = _claim("c1", "2023级" + body, quote="2023级" + body)
    audit, _ = _run_coverage(
        _cr012_unit(version="2024"), [wrong_version], evidence, ["办理程序"]
    )
    assert audit.units[0]["status"] == "generation_missing"

    lexicon = ProfileLexicon(
        {}, {}, {"innovation": ("创新班",), "regular": ("普通班",)}
    )
    wrong_class = _claim("c1", "普通班" + body, quote="普通班" + body)
    audit, _ = _run_coverage(
        _cr012_unit(
            class_type="innovation",
            span="本科生创新班如何申请勤工助学岗位，工作时长有什么限制？",
        ),
        [wrong_class],
        evidence,
        ["办理程序"],
        lexicon=lexicon,
    )
    assert audit.units[0]["status"] == "generation_missing"

    no_facts = _claim("c1", "勤工助学申请按规定办理。")
    audit, _ = _run_coverage(_cr012_unit(), [no_facts], evidence, ["办理程序"])
    row = audit.units[0]
    assert row["status"] == "generation_missing"
    assert row["claim_ids"] == []


def test_procedure_gap_recovery_keeps_other_reported_gap() -> None:
    body = _procedure_body()
    evidence = [Evidence(article=_rule_article(body))]
    claim = _claim("c1", body, quote=body)
    audit, _ = _run_coverage(
        _cr012_unit(), [claim], evidence, ["办理程序", "工作时长限制"]
    )
    row = audit.units[0]
    assert row["status"] == "generation_missing"
    assert row["claim_ids"] == []
    assert row["missing_items"] == ["工作时长限制"]


# ---------------------------------------------------------------- CR096 materials


def test_material_review_and_ordinal_screening_are_not_a_material_list() -> None:
    claim = "资格审查：根据考生提交的材料进行审核，初选、复选。"
    assert not _supported("提交材料", claim)
    assert not _supported("提交材料", "对考生提交的身份证、成绩单进行审核。")


def test_named_materials_and_concrete_properties_are_supported() -> None:
    assert _supported("提交材料", "需提交身份证、成绩单和申请表。")
    assert _supported("提交材料", "材料要求：原件及复印件，一式两份。")
    assert _supported("提交材料", "需提交身份证。")


def test_submission_route_time_and_generic_boundary_are_not_a_list() -> None:
    assert not _supported("提交材料", "提交材料至教务处，5月1日前完成。")
    assert not _supported("提交材料", "电子版材料发送至邮箱。")
    assert not _supported("提交材料", "材料以通知为准。")


def test_general_submission_route_answers_a_general_requirement() -> None:
    answer = (
        "考生应于2026年3月11日至13日向学校研招办提交报考材料，"
        "具体清单依2026年招生简章执行。"
    )
    assert _supported(
        "提交材料", answer, span="博士报名的材料提交有哪些要求？"
    )


def test_general_route_cannot_answer_an_explicit_material_list() -> None:
    answer = (
        "考生应于2026年3月11日至13日向学校研招办提交报考材料，"
        "具体清单依2026年招生简章执行。"
    )
    assert not _supported(
        "提交材料", answer, span="博士报名需要提交哪些材料？"
    )


def test_review_only_materials_stay_missing_even_for_general_ask() -> None:
    claim = "根据考生提交的评审材料，学院进行初选、复选。"
    assert not _supported(
        "提交材料", claim, span="博士报名的材料提交有哪些要求？"
    )


def test_explicit_bounded_no_material_answer_is_supported() -> None:
    assert _supported("提交材料", "无需提交任何材料。")


def test_explicit_material_list_is_not_a_submission_yes_no_property() -> None:
    span = "申请时须提交哪些材料？"
    assert _submission_is_material_scaffold(span)
    assert all(
        prop.kind != "submission" for prop in _requested_properties(span)
    )
    assert _supported("提交材料", "申请人须提交身份证复印件和成绩单。", span=span)
    assert not _supported("提交材料", "申请人须提交材料。", span=span)
    assert not _submission_is_material_scaffold("是否需要提交材料？")
    assert not _submission_is_material_scaffold("是否必须提交材料？")


def test_multi_ask_union_reports_genuine_material_gap() -> None:
    unit = QuestionUnit(
        unit_id="u1",
        original_span="招生需要什么资格、提交哪些材料？",
        requested_aspects=["申请条件", "提交材料"],
    )
    eligibility = _claim("c1", "申请条件：申请者须为全日制本科生。")
    material = _claim("c2", "需提交身份证、成绩单。")
    contributors, missing = _requirement_union_binding(unit, [eligibility], properties=[])
    assert contributors == ["c1"]
    assert missing == ["提交材料"]
    contributors, missing = _requirement_union_binding(
        unit, [eligibility, material], properties=[]
    )
    assert set(contributors) == {"c1", "c2"}
    assert missing == []


# ---------------------------------------------------------------- CR016 semantic recheck


class _ScriptedProvider:
    def __init__(self, responses: list[ChatResponse | Exception]) -> None:
        self.responses = list(responses)
        self.calls: list[list[dict[str, str]]] = []

    def complete(self, messages, *, temperature: float = 0.2, max_tokens: int = 1200):
        self.calls.append(messages)
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def _payload(*labels: tuple[str, str, str]) -> str:
    return json.dumps(
        {
            "labels": [
                {"claim_id": claim_id, "label": label, "detail": detail}
                for claim_id, label, detail in labels
            ]
        },
        ensure_ascii=False,
    )


def _semantic_claims() -> tuple[Claim, Claim]:
    first = _claim("c1", "校外导师担任第一导师。", quote="校外导师担任第一导师。", article="a#第二条")
    second = _claim(
        "c2",
        "联合培养时校外导师担任第一导师，校内导师担任第二导师。",
        quote="联合培养的研究生，校外导师担任第一导师，校内导师担任第二导师。",
        article="b#第二条",
    )
    return first, second


INCONSISTENT = (
    "前面先说与原文不一致，但重新核对后原文与claim表述相同，"
    "各项均一致，故应为supported。"
) * 6


def test_negative_label_with_self_inconsistent_detail_is_rechecked() -> None:
    first, second = _semantic_claims()
    primary = ChatResponse(
        content=_payload(
            ("c1", "supported", "一致。"),
            ("c2", "contradicted", INCONSISTENT),
        ),
        model="primary-model",
        prompt_tokens=10,
        completion_tokens=5,
    )
    recheck = ChatResponse(
        content=_payload(("c2", "supported", "两处表述一致。")),
        model="recheck-model",
        prompt_tokens=7,
        completion_tokens=3,
    )
    provider = _ScriptedProvider([primary, recheck])
    report = SemanticVerifier(provider).check([first, second], [])
    assert len(provider.calls) == 2
    assert report.labels["c1"] is SemanticLabel.SUPPORTED
    assert report.labels["c2"] is SemanticLabel.SUPPORTED
    assert report.checked == 3
    assert report.prompt_tokens == 17
    assert report.completion_tokens == 8
    assert report.model == "recheck-model"
    assert report.details["c2"].startswith("定向复核：supported")
    recheck_text = json.dumps(provider.calls[1], ensure_ascii=False)
    assert "a#第二条" not in recheck_text
    assert "校外导师担任第一导师。" not in recheck_text


def test_ordinary_negative_label_does_not_trigger_recheck() -> None:
    first, _ = _semantic_claims()
    primary = ChatResponse(
        content=_payload(("c1", "contradicted", "原文数字是30分钟，claim写成60分钟，不一致。")),
        model="primary-model",
    )
    provider = _ScriptedProvider([primary])
    report = SemanticVerifier(provider).check([first], [])
    assert len(provider.calls) == 1
    assert report.labels["c1"] is SemanticLabel.CONTRADICTED
    assert report.checked == 1


def test_negated_consistency_wording_does_not_trigger_recheck() -> None:
    first, _ = _semantic_claims()
    primary = ChatResponse(
        content=_payload(("c1", "contradicted", "claim与原文并非一致，不能判supported。")),
        model="primary-model",
    )
    provider = _ScriptedProvider([primary])
    report = SemanticVerifier(provider).check([first], [])
    assert len(provider.calls) == 1
    assert report.labels["c1"] is SemanticLabel.CONTRADICTED


def test_recheck_must_return_explicit_supported_for_the_exact_claim() -> None:
    first, second = _semantic_claims()
    primary = ChatResponse(
        content=_payload(("c1", "contradicted", INCONSISTENT)),
        model="primary-model",
    )
    recheck = ChatResponse(
        content=_payload(("c1", "contradicted", "原文与claim的导师顺序相反。")),
        model="recheck-model",
    )
    provider = _ScriptedProvider([primary, recheck])
    report = SemanticVerifier(provider).check([first, second], [])
    assert report.labels["c1"] is SemanticLabel.CONTRADICTED
    assert report.labels["c2"] is SemanticLabel.INSUFFICIENT  # primary did not return c2


def test_duplicate_repair_labels_are_rejected() -> None:
    first, _ = _semantic_claims()
    primary = ChatResponse(
        content=_payload(("c1", "contradicted", INCONSISTENT)),
        model="primary-model",
    )
    duplicate = json.dumps(
        {
            "labels": [
                {"claim_id": "c1", "label": "supported", "detail": "一致。"},
                {"claim_id": "c1", "label": "supported", "detail": "一致。"},
            ]
        },
        ensure_ascii=False,
    )
    provider = _ScriptedProvider([primary, ChatResponse(content=duplicate)])
    report = SemanticVerifier(provider).check([first], [])
    assert report.labels["c1"] is SemanticLabel.CONTRADICTED
    assert "无效" in report.details["c1"]


def test_repair_with_unknown_extra_id_is_rejected() -> None:
    first, second = _semantic_claims()
    primary = ChatResponse(
        content=_payload(
            ("c1", "contradicted", INCONSISTENT),
            ("c2", "supported", "一致。"),
        ),
        model="primary-model",
    )
    repair = ChatResponse(
        content=_payload(
            ("c1", "supported", "一致。"),
            ("c_unknown", "supported", "一致。"),
        ),
        model="recheck-model",
    )
    provider = _ScriptedProvider([primary, repair])
    report = SemanticVerifier(provider).check([first, second], [])
    assert report.labels["c1"] is SemanticLabel.CONTRADICTED
    assert report.labels["c2"] is SemanticLabel.SUPPORTED
    assert "无效" in report.details["c1"]


def test_repair_missing_a_suspect_id_is_rejected_as_a_whole() -> None:
    first, second = _semantic_claims()
    primary = ChatResponse(
        content=_payload(
            ("c1", "contradicted", INCONSISTENT),
            ("c2", "contradicted", INCONSISTENT),
        ),
        model="primary-model",
    )
    repair = ChatResponse(
        content=_payload(("c1", "supported", "一致。")),
        model="recheck-model",
    )
    provider = _ScriptedProvider([primary, repair])
    report = SemanticVerifier(provider).check([first, second], [])
    assert report.labels["c1"] is SemanticLabel.CONTRADICTED
    assert report.labels["c2"] is SemanticLabel.CONTRADICTED
    assert "无效" in report.details["c1"]
    assert "无效" in report.details["c2"]


def test_negated_consistency_hints_do_not_trigger_recheck() -> None:
    first, _ = _semantic_claims()
    for detail in (
        "原文是30分钟，claim写成60分钟，并非各项均一致。",
        "原文是30分钟，claim写成60分钟，不是与claim一致。",
        "原文是30分钟，claim写成60分钟，不能认定各项均一致。",
    ):
        primary = ChatResponse(
            content=_payload(("c1", "contradicted", detail)),
            model="primary-model",
        )
        provider = _ScriptedProvider([primary])
        report = SemanticVerifier(provider).check([first], [])
        assert len(provider.calls) == 1
        assert report.labels["c1"] is SemanticLabel.CONTRADICTED


def test_later_explicit_correction_still_triggers_recheck() -> None:
    first, _ = _semantic_claims()
    detail = (
        "原文与claim不一致，但经复核原文与claim表述相同，"
        "各项均一致，故应为supported。"
    )
    primary = ChatResponse(
        content=_payload(("c1", "contradicted", detail)),
        model="primary-model",
    )
    repair = ChatResponse(
        content=_payload(("c1", "supported", "复核后一致。")),
        model="recheck-model",
    )
    provider = _ScriptedProvider([primary, repair])
    report = SemanticVerifier(provider).check([first], [])
    assert len(provider.calls) == 2
    assert report.labels["c1"] is SemanticLabel.SUPPORTED


def test_recheck_provider_failure_keeps_original_negative() -> None:
    first, _ = _semantic_claims()
    primary = ChatResponse(
        content=_payload(("c1", "contradicted", INCONSISTENT)),
        model="primary-model",
    )
    provider = _ScriptedProvider([primary, ChatProviderError("timeout")])
    report = SemanticVerifier(provider).check([first], [])
    assert report.labels["c1"] is SemanticLabel.CONTRADICTED
    assert report.checked == 2
    assert "定向复核失败" in report.details["c1"]


# ---------------------------------------------------------------- progress signature


def test_progress_signature_ignores_new_claim_id() -> None:
    first = _claim("c1", "需提交身份证。", quote="需提交身份证。")
    duplicate = first.model_copy(update={"claim_id": "supplement_1_1"})
    assert _progress_signature([], [], [first]) == _progress_signature([], [], [duplicate])


def test_progress_signature_tracks_new_facts_numbers_negation_and_quotes() -> None:
    base = _claim("c1", "2024级需提交身份证。", quote="2024级需提交身份证。")
    base_signature = _progress_signature([], [], [base])
    for changed in (
        _claim("c2", "2024级需提交身份证和成绩单。", quote="2024级需提交身份证。"),
        _claim("c3", "2025级需提交身份证。", quote="2025级需提交身份证。"),
        _claim("c4", "2024级无需提交身份证。", quote="2024级无需提交身份证。"),
        _claim("c5", "2024级需提交身份证。", quote="2024级须提交身份证。"),
    ):
        assert _progress_signature([], [], [changed]) != base_signature
    evidence = SimpleNamespace(article=SimpleNamespace(article_id="rule", part_no=1))
    assert _progress_signature([], [evidence], [base]) != base_signature
