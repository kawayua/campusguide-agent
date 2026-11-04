"""W6 混合 Planner：规则优先，LLM 只补充拆解/改写。

规则负责 guard、流程匹配、槽位、closed 和澄清；LLM 只在规则无法拆解
多问或有指代省略时调用。LLM 输出必须通过 Pydantic + 注册值校验，不合法
就退回规则结果，绝不让模型决定流程 ID、部门、时限或资格。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import date

from campusguide.agents.plan import Clarification, Plan, QueryPlanItem
from campusguide.agents.providers import ChatProvider, ChatProviderError
from campusguide.agents.question_units import decompose_question, query_profile
from campusguide.domain.enums import (
    ArticleStatus,
    Audience,
    Intent,
    OutcomeCode,
    VersionMode,
)
from campusguide.domain.models import FlowDefinition, SlotValue
from campusguide.domain.profiles import UserProfile
from campusguide.flows.catalog import FlowCatalog
from campusguide.flows.registry import FlowMatch, match_flows
from campusguide.flows.slots import extract_slots, validate_slot_value
from campusguide.qa.guard import MAX_QUERY_CHARS, check_guard, check_version_intent
from campusguide.retrieval.profile_expand import ProfileLexicon

PLANNER_PROMPT_VERSION = "w6b-planner-quality-2026-10-28"

_PROCEDURE_RE = re.compile(
    r"怎么申请|如何申请|怎么办理|如何办理|怎么办|怎么开|如何开|"
    r"办理流程|申请流程|办理步骤|申请步骤|操作流程|"
    r"需要什么材料|要交什么材料|提交什么材料|准备什么材料|"
    r"什么时候截止|截止时间|截止日期|什么时候开始|"
    r"去哪里办|在哪办|找哪个部门|哪个部门|如何准备|要准备"
)
_FACT_RE = re.compile(
    r"条件|资格|金额|标准|比例|多少|期限|几年|依据|规定|是什么|有哪些|"
    r"要求|能否|是否|可以|奖励|档次|范围|对象|区别|差异|不同|谁|哪|何时|发布|公示|新闻|消息"
)
_SWITCH_RE = re.compile(r"换(?:一个|个)?流程|改办|不办这个|另一个流程|退出流程|重新开始|取消")
_ANAPHORA_RE = re.compile(r"这个|那个|它|上述|该政策|该办法|此规定")
_CONNECTOR_RE = re.compile(r"(?:另外|还有|同时|顺便问|顺便|以及|分别问)")
_YEAR_RE = re.compile(r"(?<!\d)(20\d{2})(?!\d)")
_ENTITY_WORDS = (
    "国家奖学金",
    "国家励志奖学金",
    "国家助学金",
    "励志奖学金",
    "奖学金",
    "推免",
    "转专业",
    "在读证明",
    "培养方案",
    "本科生",
    "研究生",
)
_DECLINE_RE = re.compile(r"^(不想说|不想回答|跳过|不知道|不确定|不清楚|随便|不填)[。！!？?\s]*$")
_NO_CITATION_RE = re.compile(
    r"(?:不要|不用|别|无需|不需要).{0,4}引用"
    r"|直接告诉我.{0,12}(?:不要|不用|别).{0,4}引用"
    r"|(?:不列|不写|不附).{0,4}(?:引用|来源)"
)
_INVENT_DEADLINE_RE = re.compile(
    r"(?:编|随便|瞎|虚构|伪造).{0,8}(?:截止|日期|时间)"
    r"|(?:截止|日期).{0,8}(?:编|随便|瞎)"
)

_GUARD_INTENT = {
    OutcomeCode.PRIVACY_REQUEST: Intent.PRIVACY_REQUEST,
    OutcomeCode.OUT_OF_SCOPE: Intent.OUT_OF_SCOPE,
    OutcomeCode.INVALID_REQUEST: Intent.SMALLTALK,
    OutcomeCode.HISTORICAL_NOT_SUPPORTED: Intent.POLICY_QA,
    OutcomeCode.VERSION_MISMATCH: Intent.POLICY_QA,
}


@dataclass
class PlannerContext:
    message: str
    as_of: date
    session_id: str = ""
    turn_id: int = 1
    active_flow_id: str | None = None
    slots: dict[str, SlotValue] = field(default_factory=dict)
    declined_slots: list[str] = field(default_factory=list)
    asked_slots: list[str] = field(default_factory=list)
    pending_slot: str | None = None
    pending_flow_ids: list[str] = field(default_factory=list)
    clarify_count: int = 0
    audience: Audience | None = None
    profile: UserProfile | None = None
    pending_clarification: dict | None = None
    pending_question: str = ""


def _normalise_text(text: str) -> str:
    return re.sub(r"\s+", "", text).strip().lower()


def _split_subquestions(text: str) -> list[str]:
    """规则切分多问；保留原文子串，最多由调用方截断到 3 个。"""
    parts: list[str] = []
    for sentence in re.split(r"[？?；;]+", text):
        sentence = sentence.strip(" ，,。.、\n\t")
        if not sentence:
            continue
        comma_parts = [item.strip() for item in re.split(r"[，,]+", sentence) if item.strip()]
        if len(comma_parts) > 1 and any(
            _FACT_RE.search(item) or _PROCEDURE_RE.search(item) for item in comma_parts
        ):
            candidates = comma_parts
        else:
            candidates = [sentence]
        for candidate in candidates:
            chunks = [candidate]
            if _CONNECTOR_RE.search(candidate):
                split = [item.strip() for item in _CONNECTOR_RE.split(candidate) if item.strip()]
                if (
                    len(split) >= 2
                    and all(len(item) >= 3 for item in split)
                    and any(
                        _FACT_RE.search(item) or _PROCEDURE_RE.search(item) for item in split[1:]
                    )
                ):
                    chunks = split
            parts.extend(chunks)
    merged: list[str] = []
    context_prefix = ""
    for part in parts:
        if not re.search(
            r"谁|哪|什么|如何|怎样|怎么|何时|是否|能否|有何|吗|多少|要求|区别|不同|变化|规定", part
        ):
            context_prefix = f"{context_prefix} {part}".strip()
        else:
            merged.append(f"{context_prefix}，{part}" if context_prefix else part)
            context_prefix = ""
    if context_prefix:
        if merged:
            merged[-1] += "，" + context_prefix
        else:
            merged = [context_prefix]
    parts = merged
    deduped: list[str] = []
    seen: set[str] = set()
    for item in parts:
        key = _normalise_text(item)
        if not key or key in seen:
            continue
        seen.add(key)
        deduped.append(item)
    return deduped


def _cohort_filters(text: str) -> dict:
    # 与 query_profile 共用一套年度语义：届别/招生年度/学年不能落成 intake_year。
    return query_profile(text, ProfileLexicon({}, {}))


def _comparison_queries(text: str) -> list[str] | None:
    """拆分明确并列的奖助项目或培养届别，不让单值流程槽位吞掉对象。"""
    if not re.search(r"分别|区别|不同|差异|[和与]", text):
        return None
    # Explicit parallel policy objects share all requested aspects.
    concepts = list(re.finditer(r"微专业|辅修学士学位|辅修专业", text))
    if len({m.group() for m in concepts}) > 1:
        return [
            text[: concepts[0].start()] + m.group() + text[concepts[-1].end() :].replace("分别", "")
            for m in concepts
        ]
    # A list of examination behaviors needs separate evidence, not one broad query.
    if "考试" in text and re.search(r"分别|各自", text) and re.search(r"[，,]", text):
        head, tail = re.split(r"[，,]", text, maxsplit=1)
        actions = [p.strip() for p in re.split(r"、|或者|或是|以及", head) if p.strip()]
        if len(actions) > 1:
            return [
                (p if "考试" in p else "考试" + p) + "，" + tail.replace("分别", "")
                for p in actions
            ]
    awards = list(
        re.finditer(
            r"(?:20\d{2}(?:[—–-]20\d{2})?\s*学年)?(?:国家励志奖学金|国家奖学金|国家助学金)", text
        )
    )
    if (
        len(awards) > 1
        and len(set(m.group() for m in awards)) > 1
        and not re.search(r"[？?]", text[: awards[-1].end()])
    ):
        prefix, suffix = text[: awards[0].start()], text[awards[-1].end() :].replace("分别", "")
        return [f"{prefix}{m.group()}{suffix}" for m in awards]
    if re.search(r"培养方案|课程|学分|毕业", text):
        cohorts = list(re.finditer(r"20\d{2}\s*(?:版|级)", text))
        if len({m.group() for m in cohorts}) > 1:
            return [
                re.sub(
                    r"[和与、]\s*(?=培养|课程|毕业|的)",
                    "",
                    text[: cohorts[0].start()] + m.group() + text[cohorts[-1].end() :],
                )
                for m in cohorts
            ]
    return None


def _intent_flags(text: str) -> tuple[bool, bool]:
    return bool(_PROCEDURE_RE.search(text)), bool(_FACT_RE.search(text))


def _topic_prefix(text: str) -> str:
    """从原问题提取可用于补全子查询的主语片段。"""
    marker = re.search(
        r"怎么|如何|需要|有哪些|有什么|有多少|有何|是什么|是多少|哪些|什么时候|何时|能否|是否|可以|分别",
        text,
    )
    prefix = text[: marker.start()] if marker else text
    prefix = prefix.strip(" ，,。.、？?！!；;")
    prefix = re.sub(r"^(请问|我想问|我想|想问|帮我看看|帮我)", "", prefix).strip()
    return prefix[:80]


def _contextualize_queries(queries: list[str], original: str) -> list[str]:
    """给缺少主语的子查询补上原问题主语，避免“金额是多少”单独检索。"""
    topic = _topic_prefix(original)
    if not topic:
        return queries
    contextualized: list[str] = []
    for query in queries:
        has_topic = topic in query or query in topic or any(word in query for word in _ENTITY_WORDS)
        if has_topic or len(query) >= 24:
            contextualized.append(query)
        else:
            contextualized.append(f"{topic} {query}")
    return contextualized


# R2：提问者身份 ≠ 文档适用对象。
# “硕士复试包括哪些环节”“博士生导师选聘条件”里的“硕士/博士”是事项对象，
# 不能据此断定提问人是在读研究生，否则会把招生、选聘、学位授予类文件过滤掉。
_OBJECT_MARKERS = re.compile(
    r"招生|复试|报考|申请考核|推免|免试|录取|调剂|选聘|招聘|遴选|导师|资格|"
    r"面向|对象|名额|计划|目录|考试科目|学位授予|答辩|培养方案|"
    r"博士后|院长|主任|谁|负责|领导|单位"
)
_ASKER_MARKERS = re.compile(
    r"我是|本人是|我是一名|我是在读|作为一名?|我读|我们(?:本科生|研究生)"
)


def _audience_from_text(text: str) -> Audience | None:
    """提问者身份，仅在问题的角色是“本人适用规则”时返回。

    角色只作为事项对象（招生、选聘、学位授予等）出现时返回 None，
    由检索按内容相关性召回，而不是按角色硬过滤。
    """
    under = "本科生" in text or "本科" in text
    grad = "研究生" in text or "硕士" in text or "博士" in text
    if under and grad:
        return None
    if not under and not grad:
        return None
    explicit = bool(_ASKER_MARKERS.search(text))
    if _OBJECT_MARKERS.search(text) and not explicit:
        return None
    return Audience.UNDERGRADUATE if under else Audience.GRADUATE


def _answers_slot(definition, text: str) -> bool:
    """判断一条消息是否在回答 pending 槽位（避免“是多少”被当成 boolean=true）。"""
    stripped = text.strip()
    if definition.type == "boolean":
        if "处分" in stripped:
            return True
        return bool(
            re.match(
                r"^(是的|是|不是|没有|没|无|有|有过|受过|没受过|未受过|否)",
                stripped,
            )
        )
    if definition.type == "integer":
        return bool(re.search(r"大[一二三四五]|研[一二三]|[1-5]\s*年级|^[1-5]$", stripped))
    if definition.type == "date":
        return bool(_YEAR_RE.search(stripped))
    if definition.type == "enum":
        extraction = extract_slots(
            stripped,
            [definition],
            turn_id=0,
            pending_slot=definition.name,
        )
        return bool(extraction.values)
    return bool(stripped)


def _default_filters(audience: Audience | None) -> dict:
    filters: dict = {
        "status": [ArticleStatus.ACTIVE.value],
        "version_mode": VersionMode.CURRENT.value,
    }
    if audience is not None:
        filters["audience"] = [audience.value]
    return filters


class Planner:
    def __init__(
        self,
        catalog: FlowCatalog,
        *,
        provider: ChatProvider | None = None,
        llm_enabled: bool = True,
        max_queries: int = 3,
        max_clarify: int = 3,
        max_query_chars: int = MAX_QUERY_CHARS,
        known_doc_ids: set[str] | None = None,
        profile_lexicon: ProfileLexicon | None = None,
    ) -> None:
        self._catalog = catalog
        self._provider = provider
        self._llm_enabled = llm_enabled
        self._max_queries = min(max(1, max_queries), 3)
        self._max_clarify = max_clarify
        self._max_query_chars = max_query_chars
        self._known_doc_ids = set(known_doc_ids or [])
        self._profile_lexicon = profile_lexicon or ProfileLexicon({}, {})
        # R8：最近一次供应商故障（分类/尝试次数/是否可恢复），由 graph 写入 trace。
        self.last_provider_error: dict | None = None

    # ------------------------------------------------------------------ entry
    def plan(self, context: PlannerContext) -> Plan:
        text = context.message.strip()
        pending = context.pending_clarification or {}
        if pending.get("reason") == "ENTITY_UNKNOWN" and context.pending_question:
            original = re.sub(
                r"两个学院|这两个学院|这个办法|该办法|这项规定", text, context.pending_question
            )
            context.message = (
                original
                if original != context.pending_question
                else f"{text}：{context.pending_question}"
            )
            context.pending_clarification = None
        plan = self._plan(context)
        if not plan.is_guard:
            plan.question_units = decompose_question(context.message, self._profile_lexicon)
            for query in plan.queries:
                query.filters.update(query_profile(query.text, self._profile_lexicon))
                query.unit_ids = [
                    u.unit_id
                    for u in plan.question_units
                    if (not u.subject or u.subject in query.text)
                    and (u.cohort is None or str(u.cohort) in query.text)
                ]
            self._protect_unit_queries(plan, context.message)
        return plan

    def _protect_unit_queries(self, plan: Plan, text: str) -> None:
        """R3：为未覆盖的对象×方面补一条定向查询，先保名额再算总分。

        比较题的初检若只按原句拆分，预算会被先出现的对象占满；这里按
        “对象 + 方面”各补一条，最多补到 4 条，且优先补完全没有查询的单元。
        """
        if len(plan.question_units) < 2:
            return
        covered = {unit_id for query in plan.queries for unit_id in query.unit_ids}
        uncovered = [u for u in plan.question_units if u.unit_id not in covered]
        if not uncovered:
            return
        audience = plan.queries[0].filters.get("audience") if plan.queries else None
        budget = max(0, min(len(uncovered), 6 - len(plan.queries)))
        for unit in uncovered[:budget]:
            parts = [
                f"{unit.cohort}级" if unit.cohort else (unit.version or ""),
                unit.subject,
                "、".join(unit.requested_aspects),
                "培养方案" if re.search(r"培养方案|课程|学分", text) else "",
            ]
            query_text = " ".join(part for part in parts if part).strip()
            if not query_text:
                continue
            item = QueryPlanItem(
                query_id=f"unit_q{len(plan.queries) + 1}",
                text=query_text[:200],
                filters=dict(plan.queries[0].filters) if plan.queries else {},
                unit_ids=[unit.unit_id],
            )
            if audience is not None and "audience" not in item.filters:
                item.filters["audience"] = audience
            item.filters.update(query_profile(query_text, self._profile_lexicon))
            plan.queries.append(item)

    def _plan(self, context: PlannerContext) -> Plan:
        text = context.message.strip()
        pending = context.pending_clarification or {}
        if pending.get("reason") == "AUDIENCE_UNKNOWN":
            answer_audience = _audience_from_text(text)
            if answer_audience is None:
                return Plan(
                    intent=Intent.POLICY_QA,
                    needs_clarification=True,
                    clarification=Clarification(
                        reason="AUDIENCE_UNKNOWN",
                        question="你是本科生还是研究生？",
                        allowed_values=["本科生", "研究生"],
                        missing_slots=["level"],
                    ),
                    planner_source="rule",
                )
            context.audience = answer_audience
            if context.pending_question:
                text = f"{context.pending_question} {text}".strip()
                context.message = text
        guard = check_guard(text, max_chars=self._max_query_chars)
        if not guard.allowed:
            outcome = guard.outcome or OutcomeCode.INVALID_REQUEST
            return Plan(
                intent=_GUARD_INTENT.get(outcome, Intent.SMALLTALK),
                outcome=outcome,
                guard_message=guard.message,
                planner_source="rule",
            )
        version_intent = check_version_intent(text, context.profile)
        if version_intent is not None and not version_intent.allowed:
            outcome = version_intent.outcome or OutcomeCode.VERSION_MISMATCH
            return Plan(
                intent=Intent.POLICY_QA,
                outcome=outcome,
                guard_message=version_intent.message,
                planner_source="rule",
            )
        if _NO_CITATION_RE.search(text):
            return Plan(
                intent=Intent.POLICY_QA,
                outcome=OutcomeCode.UNVERIFIABLE,
                guard_message="本系统只输出可核对且有引用的答案，不能按要求省略引用。",
                planner_source="rule",
            )
        if _INVENT_DEADLINE_RE.search(text):
            return Plan(
                intent=Intent.POLICY_QA,
                outcome=OutcomeCode.UNVERIFIABLE,
                guard_message="不能编造截止日期；只能引用公开通知或流程中的 deadline_note。",
                planner_source="rule",
            )

        if not context.active_flow_id and not context.pending_question:
            if re.search(r"两个学院|这两个学院|这个办法|这个规定|该办法", text) and not re.search(
                r"农学院|数学与信息学院|《", text
            ):
                return Plan(
                    needs_clarification=True,
                    clarification=Clarification(
                        reason="ENTITY_UNKNOWN",
                        question="请说明具体是哪两个学院或哪一份办法，以便核对对应材料。",
                    ),
                )
            if (
                re.search(r"补考|重修", text)
                and re.search(r"不及格|成绩.*记载", text)
                and not re.search(r"本科|研究生|硕士|博士", text)
                and context.audience is None
            ):
                return Plan(
                    needs_clarification=True,
                    clarification=Clarification(
                        reason="AUDIENCE_UNKNOWN",
                        question="本科生和研究生的补考、重修规则不同，请说明要查询哪一类？",
                        allowed_values=["本科生", "研究生"],
                    ),
                )

        audience = (
            context.audience
            or _audience_from_text(text)
            or _audience_from_text(" ".join(str(item) for item in context.slots.values()))
        )

        flows = self._list_flows(context, audience)
        selection = self._select_pending_flow(text, context, flows)
        if selection is not None:
            return selection

        active = self._active_flow(context, flows, text)
        if active is not None:
            if self._is_topic_change(text, context, active):
                # 保留旧槽位但结束旧实例；槽位仍留在 state 供用户切换回来。
                return self._policy_plan(text, context, audience)
            plan = self._flow_plan(text, context, active, audience)
            if plan is not None:
                return plan
            # 流程求值失败时仍按新问题重新判断。
            plan = self._topic_change_plan(text, context, flows, audience)
            if plan is not None:
                return plan

        return self._route_new_topic(text, context, flows, audience)

    # --------------------------------------------------------------- helpers
    def _list_flows(
        self,
        context: PlannerContext,
        audience: Audience | None,
    ) -> list[FlowDefinition]:
        try:
            return self._catalog.list_flows(as_of=context.as_of, audience=audience)
        except Exception:
            return []

    def _active_flow(
        self,
        context: PlannerContext,
        flows: list[FlowDefinition],
        text: str,
    ) -> FlowDefinition | None:
        if not context.active_flow_id:
            return None
        if _SWITCH_RE.search(text):
            return None
        flow = next(
            (item for item in flows if item.flow_id == context.active_flow_id),
            None,
        )
        if flow is None:
            try:
                flow = self._catalog.get_flow(context.active_flow_id, as_of=context.as_of)
            except Exception:
                flow = None
        if flow is None:
            return None
        matches = match_flows(text, flows)
        if matches and matches[0].flow.flow_id != flow.flow_id:
            return None
        return flow

    def _select_pending_flow(
        self,
        text: str,
        context: PlannerContext,
        flows: list[FlowDefinition],
    ) -> Plan | None:
        if not context.pending_flow_ids:
            return None
        candidates = [flow for flow in flows if flow.flow_id in context.pending_flow_ids]
        selected: FlowDefinition | None = None
        if text in {"1", "一", "第一个"} and candidates:
            selected = candidates[0]
        elif text in {"2", "二", "第二个"} and len(candidates) > 1:
            selected = candidates[1]
        elif text in {"3", "三", "第三个"} and len(candidates) > 2:
            selected = candidates[2]
        else:
            exact = match_flows(text, candidates)
            if len(exact) == 1:
                selected = exact[0].flow
        if selected is None:
            return Plan(
                intent=Intent.PROCEDURE_GUIDANCE,
                needs_clarification=True,
                clarification=Clarification(
                    reason="FLOW_AMBIGUOUS",
                    question="请回复序号或流程名："
                    + "；".join(
                        f"{index + 1}. {flow.title}" for index, flow in enumerate(candidates)
                    ),
                    allowed_values=[flow.title for flow in candidates],
                ),
                pending_flow_ids=[flow.flow_id for flow in candidates],
                planner_source="rule",
            )
        return self._flow_plan(
            text,
            context,
            selected,
            context.audience,
            force_flow=True,
        )

    def _route_new_topic(
        self,
        text: str,
        context: PlannerContext,
        flows: list[FlowDefinition],
        audience: Audience | None,
    ) -> Plan:
        # 明确比较多个对象时属于知识咨询，不选择单个用户或单个办理槽位。
        if _comparison_queries(text) is not None or (
            ("本科" in text and re.search(r"研究生|硕士|博士", text))
            and re.search(r"分别|区别|不同|差异|各自", text)
        ):
            return self._maybe_llm(
                text, context, self._policy_plan(text, context, audience), flows, audience
            )
        matches = match_flows(text, flows)
        if matches and matches[0].matched == "奖学金":
            # 通用名只能承接未命名的奖学金办理；不能把社会奖项套成国家奖项。
            generic = re.search(r"(?:^|申请|办理|咨询|了解|的|关于)奖学金", text)
            if generic is None:
                return self._policy_plan(text, context, audience)
        if len(matches) > 1:
            candidates = [item.flow for item in matches]
            return Plan(
                intent=Intent.PROCEDURE_GUIDANCE,
                needs_clarification=True,
                clarification=Clarification(
                    reason="FLOW_AMBIGUOUS",
                    question="你要办理的是哪一项？请回复序号或流程名："
                    + "；".join(
                        f"{index + 1}. {flow.title}" for index, flow in enumerate(candidates)
                    ),
                    allowed_values=[flow.title for flow in candidates],
                ),
                pending_flow_ids=[flow.flow_id for flow in candidates],
                planner_source="rule",
            )
        if len(matches) == 1:
            flow = matches[0].flow
            procedure, fact = _intent_flags(text)
            if procedure or not fact:
                plan = self._flow_plan(text, context, flow, audience)
                if plan is not None:
                    return plan
            elif fact and not procedure:
                # “国家奖学金需要什么条件”是政策问答，不启动办理流程。
                plan = self._policy_plan(text, context, audience)
                return self._maybe_llm(text, context, plan, flows, audience)
        plan = self._policy_plan(text, context, audience)
        return self._maybe_llm(text, context, plan, flows, audience)

    def _is_topic_change(
        self,
        text: str,
        context: PlannerContext,
        flow: FlowDefinition,
    ) -> bool:
        """在流程中只问政策事实、且没有待填槽位 → 结束旧实例。"""
        procedure, fact = _intent_flags(text)
        if not fact or procedure:
            return False
        if not context.pending_slot:
            return True
        definition = next(
            (slot for slot in flow.slots if slot.name == context.pending_slot),
            None,
        )
        if definition is None:
            return True
        return not _answers_slot(definition, text)

    def _topic_change_plan(
        self,
        text: str,
        context: PlannerContext,
        flows: list[FlowDefinition],
        audience: Audience | None,
    ) -> Plan | None:
        procedure, fact = _intent_flags(text)
        if not fact or procedure:
            return None
        return self._policy_plan(text, context, audience)

    # ------------------------------------------------------------------ flow
    def _flow_plan(
        self,
        text: str,
        context: PlannerContext,
        flow: FlowDefinition,
        audience: Audience | None,
        *,
        force_flow: bool = False,
    ) -> Plan | None:
        extraction = extract_slots(
            text,
            flow.slots,
            turn_id=context.turn_id,
            existing=context.slots,
            pending_slot=context.pending_slot,
            allow_overwrite=True,
        )
        slots = dict(context.slots)
        slots.update(extraction.values)
        declined = list(context.declined_slots)
        for name in extraction.declined_slots:
            if name not in declined:
                declined.append(name)
        asked = list(context.asked_slots)
        pending = context.pending_slot
        if pending and (pending in extraction.values or pending in extraction.declined_slots):
            pending = None

        try:
            evaluation = self._catalog.evaluate(
                flow,
                slots,
                as_of=context.as_of,
                done_step_ids=set(),
                declined_slots=set(declined),
                asked_slots=set(asked),
            )
        except Exception as exc:
            return Plan(
                intent=Intent.PROCEDURE_GUIDANCE,
                flow_id=flow.flow_id,
                slots=slots,
                planner_source="rule",
                warnings=[f"流程求值失败：{type(exc).__name__}"],
            )

        procedure, fact = _intent_flags(text)
        queries: list[QueryPlanItem] = []
        intent = Intent.PROCEDURE_GUIDANCE
        if (procedure and fact) or (fact and not procedure and force_flow):
            subs = _split_subquestions(text)
            fact_queries = [
                item for item in subs if _FACT_RE.search(item) and not _PROCEDURE_RE.search(item)
            ]
            if not fact_queries:
                fact_queries = subs
            queries = self._query_items(_contextualize_queries(fact_queries, text), audience)
            if queries:
                intent = Intent.MIXED

        plan = Plan(
            intent=intent,
            flow_id=flow.flow_id,
            queries=queries,
            slots=slots,
            missing_slots=list(evaluation.missing_slots),
            flow_status=evaluation.status,
            planner_source="rule",
            declined_slots=declined,
            asked_slots=asked,
            pending_slot=pending,
        )
        if self._should_ask(
            context, evaluation, extraction_declined=bool(extraction.declined_slots)
        ):
            slot_name = (
                evaluation.next_slot.name
                if evaluation.next_slot
                else (evaluation.missing_slots[0] if evaluation.missing_slots else None)
            )
            definition = next(
                (slot for slot in flow.slots if slot.name == slot_name),
                evaluation.next_slot,
            )
            if definition is not None:
                plan.needs_clarification = True
                plan.clarification = Clarification(
                    reason="SLOT_MISSING",
                    question=definition.question,
                    allowed_values=list(definition.allowed_values or []),
                    missing_slots=list(evaluation.missing_slots),
                )
                plan.pending_slot = definition.name
                if definition.name not in plan.asked_slots:
                    plan.asked_slots.append(definition.name)
        return plan

    def _should_ask(
        self, context: PlannerContext, evaluation, *, extraction_declined: bool
    ) -> bool:
        if evaluation.status == "closed":
            return False
        if not evaluation.missing_slots:
            return False
        if context.clarify_count >= self._max_clarify:
            return False
        if extraction_declined:
            return False
        if context.pending_slot in context.declined_slots:
            return False
        return evaluation.next_slot is not None

    # ----------------------------------------------------------------- policy
    def _policy_plan(
        self,
        text: str,
        context: PlannerContext,
        audience: Audience | None,
    ) -> Plan:
        comparison = _comparison_queries(text)
        subs = comparison or _split_subquestions(text) or [text.strip()]
        if comparison is None:
            subs = _contextualize_queries(subs, text)
        dropped = subs[self._max_queries :]
        queries = self._query_items(subs[: self._max_queries], audience)
        return Plan(
            intent=Intent.POLICY_QA,
            queries=queries,
            slots={},
            planner_source="rule",
            dropped_queries=dropped,
            pending_slot=None,
        )

    def _query_items(
        self,
        texts: list[str],
        audience: Audience | None,
    ) -> list[QueryPlanItem]:
        filters = _default_filters(audience)
        items: list[QueryPlanItem] = []
        seen: set[str] = set()
        for text in texts:
            key = _normalise_text(text)
            if not key or key in seen:
                continue
            seen.add(key)
            items.append(
                QueryPlanItem(
                    query_id=f"q{len(items) + 1}",
                    text=text.strip(),
                    route="policy",
                    filters={**filters, **_cohort_filters(text)},
                )
            )
        return items

    # ------------------------------------------------------------------- llm
    def _maybe_llm(
        self,
        text: str,
        context: PlannerContext,
        rule_plan: Plan,
        flows: list[FlowDefinition],
        audience: Audience | None,
    ) -> Plan:
        if not self._llm_enabled or self._provider is None or not self._needs_llm(text, rule_plan):
            return rule_plan
        try:
            llm_plan = self._call_llm(text, context, rule_plan, flows, audience)
        except (ChatProviderError, ValueError, KeyError, TypeError):
            rule_plan.planner_source = "fallback"
            return rule_plan
        if llm_plan is None:
            rule_plan.planner_source = "fallback"
            return rule_plan
        if not rule_plan.flow_id:
            # LLM只补充知识查询，不能将已确认的事实问题改为办理流程。
            llm_plan.flow_id = None
            llm_plan.intent = Intent.POLICY_QA
        # 规则已匹配流程时，不允许 LLM 推翻流程。
        if rule_plan.flow_id and llm_plan.flow_id != rule_plan.flow_id:
            llm_plan.flow_id = rule_plan.flow_id
            llm_plan.intent = rule_plan.intent
        return llm_plan

    @staticmethod
    def _needs_llm(text: str, rule_plan: Plan) -> bool:
        marks = len(re.findall(r"[？?]", text))
        if marks >= 2 and len(rule_plan.queries) <= 1:
            return True
        if _ANAPHORA_RE.search(text) and len(rule_plan.queries) <= 1:
            return True
        return bool(re.search(r"分别|各自|以及", text) and len(rule_plan.queries) <= 1)

    def _call_llm(
        self,
        text: str,
        context: PlannerContext,
        rule_plan: Plan,
        flows: list[FlowDefinition],
        audience: Audience | None,
    ) -> Plan | None:
        assert self._provider is not None
        messages = build_planner_messages(
            text,
            context,
            rule_plan,
            flows,
            audience,
            max_queries=self._max_queries,
        )
        response = self._provider.complete(messages, temperature=0.0, max_tokens=800)
        usage = {
            "prompt_tokens": response.prompt_tokens,
            "completion_tokens": response.completion_tokens,
            "model_latency_ms": response.latency_ms,
        }
        rule_plan.model_usage = usage
        payload = _parse_json(response.content)
        if payload is None:
            repair = [
                *messages,
                {"role": "assistant", "content": response.content},
                {
                    "role": "user",
                    "content": (
                        "上面的输出不是合法 JSON。请只输出一个 JSON 对象，"
                        '结构为 {"intent":"policy_qa","flow_id":null,'
                        '"queries":[{"text":"...","route":"policy","filters":{}}]}。'
                    ),
                },
            ]
            response = self._provider.complete(repair, temperature=0.0, max_tokens=800)
            usage["prompt_tokens"] += response.prompt_tokens
            usage["completion_tokens"] += response.completion_tokens
            usage["model_latency_ms"] += response.latency_ms
            payload = _parse_json(response.content)
        if payload is None:
            return None
        plan = self._validate_llm_payload(
            payload,
            text,
            context,
            rule_plan,
            flows,
            audience,
        )
        if plan is not None:
            plan.model_usage = usage
        return plan

    def _validate_llm_payload(
        self,
        payload: dict,
        text: str,
        context: PlannerContext,
        rule_plan: Plan,
        flows: list[FlowDefinition],
        audience: Audience | None,
    ) -> Plan | None:
        raw_intent = str(payload.get("intent") or "").strip()
        try:
            intent = Intent(raw_intent)
        except ValueError:
            intent = rule_plan.intent
        if intent in {
            Intent.SMALLTALK,
            Intent.OUT_OF_SCOPE,
            Intent.PRIVACY_REQUEST,
        }:
            intent = rule_plan.intent

        known_flows = {flow.flow_id for flow in flows}
        flow_id = payload.get("flow_id")
        if not isinstance(flow_id, str) or flow_id not in known_flows:
            flow_id = rule_plan.flow_id

        dropped: list[str] = []
        queries: list[QueryPlanItem] = []
        raw_queries = payload.get("queries")
        if not isinstance(raw_queries, list):
            raw_queries = []
        for index, item in enumerate(raw_queries[: self._max_queries], start=1):
            if not isinstance(item, dict):
                continue
            query_text = item.get("text")
            if not isinstance(query_text, str) or not query_text.strip():
                continue
            query_text = query_text.strip()
            original_units = decompose_question(text, self._profile_lexicon)
            subjects = {u.subject for u in original_units if u.subject}
            years = {u.cohort for u in original_units if u.cohort}
            if len(subjects) == 1 and not any(subject in query_text for subject in subjects):
                query_text = next(iter(subjects)) + " " + query_text
            if len(years) == 1 and not any(str(y) in query_text for y in years):
                query_text = f"{next(iter(years))}级 " + query_text
            if len(query_text) > 200:
                continue
            if not _query_is_grounded(query_text, text):
                dropped.append(f"ungrounded:{query_text[:20]}")
                continue
            route = item.get("route") if item.get("route") in {"policy", "flow"} else "policy"
            filters = self._validate_filters(item.get("filters"), flows, dropped)
            filters.update(_cohort_filters(query_text))
            queries.append(
                QueryPlanItem(
                    query_id=f"q{index}",
                    text=query_text,
                    route=route,
                    filters=filters,
                )
            )
        if not queries:
            return None
        if intent is Intent.PROCEDURE_GUIDANCE and queries:
            intent = Intent.MIXED if flow_id else Intent.POLICY_QA
        plan = Plan(
            intent=intent,
            flow_id=flow_id,
            queries=queries,
            slots={},
            planner_source="llm",
            dropped_filters=dropped,
            warnings=["LLM Planner 改写查询；流程/过滤器已校验"],
        )
        if rule_plan.flow_id:
            plan.intent = rule_plan.intent
            if plan.intent is Intent.PROCEDURE_GUIDANCE:
                plan.queries = []
        return plan

    def _validate_filters(
        self,
        raw: object,
        flows: list[FlowDefinition],
        dropped: list[str],
    ) -> dict:
        filters = _default_filters(None)
        if not isinstance(raw, dict):
            return filters
        audience_raw = raw.get("audience")
        if isinstance(audience_raw, list):
            audiences: list[str] = []
            for item in audience_raw:
                try:
                    audiences.append(Audience(str(item)).value)
                except ValueError:
                    dropped.append(f"audience:{item}")
            if audiences:
                filters["audience"] = audiences
        status_raw = raw.get("status")
        if isinstance(status_raw, list):
            statuses: list[str] = []
            for item in status_raw:
                try:
                    statuses.append(ArticleStatus(str(item)).value)
                except ValueError:
                    dropped.append(f"status:{item}")
            if statuses:
                filters["status"] = statuses
        if "version_mode" in raw:
            try:
                filters["version_mode"] = VersionMode(str(raw["version_mode"])).value
            except ValueError:
                dropped.append(f"version_mode:{raw['version_mode']}")
        known_flows = {flow.flow_id for flow in flows}
        flow_ids = raw.get("flow_ids")
        if isinstance(flow_ids, list):
            allowed = [str(item) for item in flow_ids if str(item) in known_flows]
            if allowed:
                filters["flow_ids"] = allowed
            elif flow_ids:
                dropped.append("flow_ids:unknown")
        doc_ids = raw.get("doc_ids")
        if isinstance(doc_ids, list):
            allowed_docs = [str(item) for item in doc_ids if str(item) in self._known_doc_ids]
            if allowed_docs:
                filters["doc_ids"] = allowed_docs
            elif doc_ids:
                dropped.append("doc_ids:unknown")
        return filters


def _query_is_grounded(query: str, original: str) -> bool:
    """拒绝模型凭空加入年份/学校/奖项/学院等实体。"""
    query_norm = _normalise_text(query)
    original_norm = _normalise_text(original)
    if not query_norm:
        return False
    if query_norm in original_norm:
        return True
    if not set(_YEAR_RE.findall(query_norm)) <= set(_YEAR_RE.findall(original_norm)):
        return False
    for word in _ENTITY_WORDS:
        if word in query_norm and word not in original_norm:
            return False
    overlap = sum(1 for char in set(query_norm) if char in original_norm)
    return overlap / max(1, len(set(query_norm))) >= 0.5


def _parse_json(content: str) -> dict | None:
    text = content.strip()
    fenced = re.match(r"^```(?:json)?\s*(.*?)\s*```$", text, re.DOTALL)
    if fenced:
        text = fenced.group(1).strip()
    if not text.startswith("{"):
        start = text.find("{")
        end = text.rfind("}")
        if start < 0 or end <= start:
            return None
        text = text[start : end + 1]
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return None
    return payload if isinstance(payload, dict) else None


def build_planner_messages(
    text: str,
    context: PlannerContext,
    rule_plan: Plan,
    flows: list[FlowDefinition],
    audience: Audience | None,
    *,
    max_queries: int,
) -> list[dict[str, str]]:
    flow_lines = []
    for flow in flows:
        flow_lines.append(
            f"- {flow.flow_id}｜{flow.title}｜aliases={','.join(flow.aliases)}｜"
            f"slots={','.join(slot.name for slot in flow.slots)}"
        )
    system = (
        "你是高校制度问答的查询规划器。只输出 JSON，不回答问题。\n"
        "规则：1) 最多 3 个子查询；2) 不得加入用户原话没有的年份、学院、专业、"
        "奖项或政策名；3) flow_id 只能取给定列表；4) filters 只能使用 "
        "audience/status/version_mode/doc_ids/flow_ids；5) intent 只能是 "
        "policy_qa、procedure_guidance、mixed；6) 不确定时保持单查询。\n"
        '输出结构：{"intent":"policy_qa","flow_id":null,"queries":'
        '[{"text":"...","route":"policy","filters":{}}]}'
    )
    user = "\n".join(
        [
            f"用户问题：{text}",
            f"受众：{audience.value if audience else '未指定'}",
            f"当前流程：{context.active_flow_id or '无'}",
            f"规则初判：intent={rule_plan.intent.value}, "
            f"flow_id={rule_plan.flow_id or '无'}, "
            f"queries={[item.text for item in rule_plan.queries]}",
            f"最多子查询数：{max_queries}",
            "可用流程：",
            *(flow_lines or ["- 无"]),
            "只输出 JSON。",
        ]
    )
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]


def validate_llm_slot_values(
    raw_slots: object,
    flow: FlowDefinition,
    message: str,
    *,
    turn_id: int,
) -> dict[str, SlotValue]:
    """校验 LLM 提议的槽位：值必须在原话中有 span 且类型合法。"""
    if not isinstance(raw_slots, dict):
        return {}
    values: dict[str, SlotValue] = {}
    for definition in flow.slots:
        raw = raw_slots.get(definition.name)
        if raw is None:
            continue
        if isinstance(raw, str):
            value: object = raw
            if definition.type == "integer" and raw.isdigit():
                value = int(raw)
            elif definition.type == "boolean" and raw in {"true", "false"}:
                value = raw == "true"
        else:
            value = raw
        if not validate_slot_value(definition, value):
            continue
        if isinstance(value, str) and value not in message:
            continue
        values[definition.name] = SlotValue(
            name=definition.name,
            value=value,  # type: ignore[arg-type]
            source="user_explicit",
            turn_id=turn_id,
        )
    return values


__all__ = [
    "PLANNER_PROMPT_VERSION",
    "FlowMatch",
    "Planner",
    "PlannerContext",
    "build_planner_messages",
    "validate_llm_slot_values",
]
