"""Independent coverage audit over final verified claims and validated flow facts."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

from campusguide.agents.advisor import build_evidence_block
from campusguide.agents.plan import QuestionUnit
from campusguide.agents.providers import ChatProvider, ChatProviderError
from campusguide.agents.question_units import ASPECTS
from campusguide.domain.models import ChecklistItem, Claim, Evidence
from campusguide.observability.errors import safe_error_summary
from campusguide.verification.coverage_items import missing_course_items, required_course_items

_ASPECT_PATTERNS = dict(ASPECTS)

COVERAGE_PROMPT_VERSION = "coverage-2026-10-28"
SYSTEM_PROMPT = """你是答案完整性核对器，独立于答案生成器。检查已通过事实核对的最终结论与有效流程清单，是否完整回答原问题。
“没有涉及”“无法回答”“证据不足”等边界句不能算对应事实已答，不得因一条claim很长就忽略其中明确缺口。
不能把检索到证据等同于已经回答，不能相信生成器自报的unanswered_questions或unit_ids。
按照每个对象×版本×方面检查。列表应覆盖提供原文中用户所问范围的必要项目，比较应涵盖每个对象和方面。
宽泛制度题检查核心事项，不要求抄录所有相邻条款；问题只问哪些公示，不额外要求公示起止日。
核实“最新”须有集合扫描的日期选择依据；声明的收录范围不得扩展为真实世界所有活动。
可信同源标题/章节可解释学院、系和文件对象；不能补实质金额/期限/第一第二导师关系。
只把已验证claims或清单中实际提供的内容算作已答。每个answered单元必须列出支持它的claim_id或有效flow:step_id。
若证据足够但答案遗漏：generation_missing；没证据：missing_evidence；未通过引用/语义核对：verification_failed。
不得凭空增设用户没问的强制内容。必要且正确的澄清与有据的边界说明可以单独标出，不以拒答充当回答。
只输出JSON：{"units":[{"unit_id":"u1","status":"answered|missing_evidence|generation_missing|verification_failed|clarification_needed",
"claim_ids":["c1"],"missing_items":["具体遗漏的原问题方面或原文列表项"]}],"unanswered_questions":["原问题仍未覆盖的必要部分"]}。
所有unit必须返回一次，标识只能取输入中的值。没有真实支持标识不得标answered。"""


_LIST_ASPECTS = ("核心课程", "选修课程", "实践课程", "课程设置", "毕业设计")
_NUMBER_ASPECTS = ("毕业学分", "学制", "成绩记载", "截止时间")
_FLOW_ASPECTS = ("办理程序", "提交材料")
_COURSE_NAME = re.compile(r"[（(]?[\u4e00-\u9fffA-Za-z]{2,20}(?:课程|设计|论文|实习|实验|训练|实践)[）)]?")
_STEP_MARKERS = re.compile(
    r"首先|其次|然后|接着|最后|第一步|第二步|第[一二三四五六七八九十]步|"
    r"提交|携带|填写|登录|办理|领取|审核|公示|报名|申请"
)


_NON_ITEM = re.compile(
    r"设置|包括|开设|共有|均|分别|提供|涵盖|分为|如下|要求|应取得|学分|学时|等$|等等|以及|都有|均有|涉及|包含|相关"
)


def _course_items(text: str) -> list[str]:
    """列举项：课程名通常不以“课程”结尾（如“数据结构”“操作系统”）。

    仅靠 `_COURSE_NAME` 会漏掉大部分真实课程名，导致已答单元被判缺项；
    这里改为按分隔符切分后过滤掉句子骨架，再用数量判断是否是清单。
    """
    items: list[str] = []
    for piece in re.split(r"[、，,；;\n]", text):
        item = piece.strip(" 　.。：:（）()【】[]")
        item = re.sub(r"^[0-9一二三四五六七八九十]+[.、)）]\s*", "", item)
        if not (2 <= len(item) <= 30) or re.search(r"[0-9]", item):
            continue
        if _NON_ITEM.search(item) or re.search(r"是|为|的", item):
            continue
        items.append(item)
    return items


def _has_numbers(text: str) -> bool:
    return bool(re.search(r"\d+(?:\.\d+)?\s*(?:学分|学时|年|个|门|元|%|天|周|小时|分钟)", text))


_LIST_MIN = {
    "核心课程": 2,
    "选修课程": 2,
    "课程设置": 2,
    "实践课程": 1,
}


def _content_supports(aspect: str, text: str) -> bool:
    """What the claim actually says, not just which keyword it contains (A5)."""
    if aspect == "毕业设计":
        # 020：岗位要求是“6 学分/14 周/第 8 学期”，只写“毕业论文可作为实现途径”
        # 不算答出毕业设计要求。
        return bool(
            re.search(r"\d+(?:\.\d+)?\s*(?:学分|周|学期|字|人)", text)
            or re.search(r"选题|开题|中期检查|答辩|查重|撰写规范|评阅|成绩构成", text)
        )
    if aspect in _LIST_MIN:
        return len(_course_items(text)) >= _LIST_MIN[aspect]
    if aspect in _NUMBER_ASPECTS:
        return _has_numbers(text)
    if aspect in _FLOW_ASPECTS:
        return bool(_STEP_MARKERS.search(text))
    if aspect == "定义区别":
        return ("是指" in text or "定义为" in text or "指的是" in text) and len(text) >= 30
    return True


def _clean_missing(items: list[str], question: str) -> list[str]:
    """Drop wording echoes: the model often repeats the question or a stray fragment."""
    kept: list[str] = []
    squeezed_question = re.sub(r"[\s　、，,。；;：:？?（）()]", "", question or "")
    for item in items:
        text = item.strip()
        squeezed = re.sub(r"[\s　、，,。；;：:？?（）()]", "", text)
        if len(squeezed) < 4:
            continue
        if squeezed and (squeezed == squeezed_question or squeezed in squeezed_question):
            continue
        if text.lstrip().startswith(("）", ")", "、", "，", ",", "：")):
            continue
        kept.append(text)
    return list(dict.fromkeys(kept))


# 具体缺口：带数字/单位或明确的要求类实词。这类缺口说明“确实少答了一块”，
# 不能被反向核对吞掉（035 的学术不端、020 的毕设学分/周数）。
_CONCRETE_GAP = re.compile(
    r"\d|学分|学时|周|学期|年限|元|%|比例|分数线|名额"
    r"|具体|要求|材料|步骤|时间|名单|清单|规定|条件|流程|程序|标准|构成"
)


def split_missing(items: list[str], question: str) -> tuple[list[str], list[str]]:
    """返回 (主题回声, 具体缺口)。回声可以忽略，具体缺口必须保留。"""
    echoes: list[str] = []
    concrete: list[str] = []
    for text in _clean_missing(items, question):
        if _CONCRETE_GAP.search(text):
            concrete.append(text)
        else:
            echoes.append(text)
    return echoes, concrete


_CLAUSE_SPLIT = re.compile(r"[、，,]|以及|或者|及|和|与")
_STOP_BIGRAMS = {
    "如何",
    "规定",
    "要求",
    "哪些",
    "什么",
    "是否",
    "分别",
    "怎样",
    "怎么",
    "情况",
    "内容",
    "办法",
    "说明",
    "安排",
    "处理",
    "办理",
    "申请",
    "计算",
    "什么",
}


_META_CLAUSE = re.compile(r"分别|涉及|哪些|是否|多少|什么|怎样")
_DOC_CLAUSE = re.compile(r"管理办法|实施细则|评选办法|补充说明|通知|文件|方案|章程")
_DOC_HEAD = re.compile(r"^(?:华南农业大学|大学|学院|学校)")
# “纪律处分”“规定要求”这类只剩问法成分的碎片，不是需要单独覆盖的方面。
_GENERIC_CLAUSE = re.compile(
    r"^(?:纪律|处分|规定|要求|条件|程序|流程|材料|时间|方式|标准|内容|情况|办法|细则|名单|清单)+$"
)


def _question_clauses(span: str) -> list[str]:
    """把“整句问题当方面”的 fallback 单元切成并列子问题；切不动返回空表。

    只保留内容型子问题：像“分别涉及哪些规定和纪律处分”这类问法本身、
    以及“农学院……评选办法”这类文档名，都不作为必须覆盖的方面。
    """
    parts = [part.strip("？?。 ") for part in _CLAUSE_SPLIT.split(span)]
    parts = [
        part
        for part in parts
        if len(re.sub(r"[^\u4e00-\u9fff]", "", part)) >= 2
        and not _META_CLAUSE.search(part)
        and not _DOC_CLAUSE.search(part)
        and not (
            _DOC_HEAD.match(part)
            and len(re.sub(r"[^\u4e00-\u9fff]", "", part)) >= 10
        )
        and not _GENERIC_CLAUSE.match(re.sub(r"[^\u4e00-\u9fff]", "", part))
    ]
    return parts if len(parts) >= 2 else []


# 结论常用同义说法回答同一行为；两字子问题只按字面比对会误判为漏答。
_CLAUSE_ALIASES: dict[str, tuple[str, ...]] = {
    "替考": ("替考", "代考", "代替他人", "代替自己"),
    "作弊": ("作弊", "违纪", "舞弊"),
}


def _clause_covered(clause: str, text: str) -> bool:
    """子问题的实词双字组至少六成命中；只命中“学术”不算答出“学术不端”。"""
    chars = re.sub(r"[^\u4e00-\u9fff]", "", clause)
    aliases = _CLAUSE_ALIASES.get(chars)
    if aliases:
        return any(alias in text for alias in aliases)
    bigrams = [chars[i : i + 2] for i in range(max(0, len(chars) - 1))]
    bigrams = [bigram for bigram in bigrams if bigram not in _STOP_BIGRAMS]
    if not bigrams:
        return True
    hits = sum(1 for bigram in bigrams if bigram in text)
    return hits / len(bigrams) >= 0.6


def _clause_gaps(clauses: list[str], claims: list[Claim]) -> tuple[list[str], list[str]]:
    """返回 (未被任何结论覆盖的子问题, 参与覆盖的 claim_id)。"""
    uncovered: list[str] = []
    contributors: list[str] = []
    for clause in clauses:
        hit = False
        for claim in claims:
            if _clause_covered(clause, claim.text):
                hit = True
                if claim.claim_id not in contributors:
                    contributors.append(claim.claim_id)
        if not hit:
            uncovered.append(clause)
    return uncovered, contributors


def _is_fallback_unit(unit: QuestionUnit) -> bool:
    """decompose 没能识别出方面、把整句问题当成方面。"""
    aspects = [aspect for aspect in unit.requested_aspects if aspect]
    if not aspects:
        return False
    span = (unit.original_span or "").strip("？?。 ")
    return all(aspect.strip("？?。 ") == span for aspect in aspects)


def _matches_unit_subject(unit: QuestionUnit, claim: Claim) -> bool:
    haystack = f"{claim.text} " + " ".join(
        f"{citation.doc_title or ''} {citation.article_no or ''}"
        for citation in claim.citations
    )
    if unit.subject and unit.subject not in haystack:
        return False
    if unit.objects and not any(obj in haystack for obj in unit.objects if obj):
        return False
    return True


def _claim_supports_unit(unit: QuestionUnit, claim: Claim | None) -> bool:
    """R4/A5：一条结论必须绑定到该单元的对象和所问方面，且实际答出内容。

    比较题每个对象各占一个单元，因此“两个专业都有实践环节”这类泛称断言
    无法同时支撑两个单元；清单题要有课程名，数字题要有数值，流程题要有步骤。
    """
    if claim is None:
        return False
    citation_text = " ".join(
        f"{citation.doc_title or ''} {citation.article_no or ''}"
        for citation in claim.citations
    )
    haystack = f"{claim.text} {citation_text}"
    if unit.subject and unit.subject not in haystack:
        return False
    if unit.objects and not any(obj in haystack for obj in unit.objects if obj):
        return False
    aspects = [aspect for aspect in unit.requested_aspects if aspect]
    if not aspects:
        return True
    # 没有可识别方面时，decompose 会把整句问题当成方面；此时对象已匹配，
    # 一条通过核对的结论就覆盖了该问题，不再要求它重复问题原句。
    span = (unit.original_span or "").strip("？?。 ")
    if all(aspect.strip("？?。 ") == span for aspect in aspects):
        # 035：并列子问题要逐个覆盖；单条结论只答其中一问时不算覆盖整句。
        clauses = _question_clauses(span)
        if not clauses:
            return True
        return all(_clause_covered(clause, claim.text) for clause in clauses)
    for aspect in aspects:
        pattern = _ASPECT_PATTERNS.get(aspect)
        mentioned = bool(pattern and re.search(pattern, claim.text)) or aspect in claim.text
        if not mentioned and aspect == "定义区别":
            mentioned = "是指" in claim.text or "指的是" in claim.text
        if mentioned and _content_supports(aspect, claim.text):
            return True
        # 标题只解释对象，不提供清单内容；“标题+短正文”不能凑成已答。
    # An aspect may be carried by the cited heading only when it needs no content,
    # e.g. a section titled “课程设置” still must list courses to count as answered.
    return any(
        aspect in citation_text and _content_supports(aspect, claim.text) for aspect in aspects
    )


def _flow_supports_unit(unit: QuestionUnit, step_id: str, checklist: list[ChecklistItem]) -> bool:
    """A flow step must match the unit's object and ask, not just exist (A5)."""
    step = next((item for item in checklist if item.step_id == step_id), None)
    if step is None:
        return False
    haystack = " ".join(
        str(part)
        for part in (
            step.title,
            step.description,
            step.department,
            " ".join(step.materials or []),
        )
        if part
    )
    if unit.objects and not any(obj in haystack for obj in unit.objects if obj):
        return False
    if unit.subject and unit.subject not in haystack:
        return False
    aspects = [aspect for aspect in unit.requested_aspects if aspect]
    if not aspects:
        return True
    for aspect in aspects:
        pattern = _ASPECT_PATTERNS.get(aspect)
        if (pattern and re.search(pattern, haystack)) or aspect in haystack:
            return True
        if _content_supports(aspect, haystack):
            return True
    return False


@dataclass
class CoverageAudit:
    units: list[dict] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)
    unavailable: bool = False
    error: str = ""
    # R8：服务故障分类与尝试次数，进入 trace 而非写成“缺资料”。
    error_kind: str = ""
    error_attempts: int | None = None
    prompt_tokens: int = 0
    completion_tokens: int = 0
    latency_ms: int = 0


class CoverageChecker:
    def __init__(self, provider: ChatProvider):
        self._provider = provider

    def check(
        self,
        question: str,
        units: list[QuestionUnit],
        claims: list[Claim],
        evidence: list[Evidence],
        checklist: list[ChecklistItem],
        *,
        retrieval_warnings: list[str] | None = None,
    ) -> CoverageAudit:
        if not units:
            return CoverageAudit()
        # Only hydrated original citations may support checklist coverage.
        evidence_ids = {e.article.article_id for e in evidence}
        valid_flow = [
            item
            for item in checklist
            if item.citations
            and set(item.citations) <= evidence_ids
            and item.status != "not_applicable"
        ]
        known_claims = {claim.claim_id for claim in claims}
        known_claims.update("flow:" + item.step_id for item in valid_flow)
        payload = {
            "question": question,
            "units": [
                {
                    **u.model_dump(mode="json"),
                    "required_source_items": required_course_items(u, evidence),
                }
                for u in units
            ],
            "verified_claims": [c.model_dump(mode="json") for c in claims],
            "verified_flow_checklist": [
                {**c.model_dump(mode="json"), "claim_id": "flow:" + c.step_id} for c in valid_flow
            ],
            "retrieval_diagnostics": [
                w
                for w in (retrieval_warnings or [])
                if w.startswith(("COLLECTION_", "SOURCE_VIEW_"))
            ],
        }
        messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {
                "role": "user",
                "content": json.dumps(payload, ensure_ascii=False)
                + "\n本轮原始证据（只用于判断遗漏，不算已答）：\n"
                + build_evidence_block(evidence),
            },
        ]
        audit = CoverageAudit()
        try:
            response = self._provider.complete(messages, temperature=0, max_tokens=2400)
            audit.prompt_tokens = response.prompt_tokens
            audit.completion_tokens = response.completion_tokens
            audit.latency_ms = response.latency_ms
            text = (
                response.content.strip()
                .removeprefix("```json")
                .removeprefix("```")
                .removesuffix("```")
                .strip()
            )
            raw = json.loads(text)
            rows = raw["units"]
            if not isinstance(rows, list):
                raise ValueError("coverage units must be a list")
            by_id = {r["unit_id"]: r for r in rows if isinstance(r, dict)}
            if set(by_id) != {u.unit_id for u in units} or len(by_id) != len(rows):
                raise ValueError("coverage unit identifiers mismatch")
            statuses = {
                "answered",
                "missing_evidence",
                "generation_missing",
                "verification_failed",
                "clarification_needed",
            }
            for unit in units:
                row = by_id[unit.unit_id]
                status = row.get("status")
                if status not in statuses:
                    raise ValueError("unknown coverage status")
                ids = row.get("claim_ids", [])
                if not isinstance(ids, list) or set(ids) - known_claims:
                    raise ValueError("coverage references unverified claims")
                missing = row.get("missing_items", [])
                if not isinstance(missing, list) or any(not isinstance(m, str) for m in missing):
                    raise ValueError("invalid missing items")
                # Never accept answered without actual final support, even if the model says so.
                deterministic_missing = missing_course_items(
                    unit, evidence, "\n".join(c.text for c in claims)
                )
                model_missing = _clean_missing(missing, question)
                concrete_gaps = split_missing(missing, question)[1]
                missing = list(dict.fromkeys([*model_missing, *deterministic_missing]))
                # 035：整句问题当方面时，必须每个并列子问题都有结论覆盖；
                # 只答“科研成果加分”不能把“学术不端”一起算作已答。
                span = (unit.original_span or "").strip("？?。 ")
                clauses = _question_clauses(span) if _is_fallback_unit(unit) else []
                clause_gaps, clause_ids = _clause_gaps(clauses, claims) if clauses else ([], [])
                # A5 反向核对：模型常把已经答出的单元判成缺项并回显问题本身。
                # 只要有一条通过核对的结论实际覆盖了该单元对象与方面，且原文清单
                # 没有确定性漏项、模型也没有点名具体缺口，就按已答处理并写回真实
                # claim_ids；泛称断言与漏列清单项仍会被内容检查挡住。
                if (
                    status != "answered"
                    and not unit.ambiguity
                    and not deterministic_missing
                    and not concrete_gaps
                    and not clause_gaps
                ):
                    if clauses:
                        recovered = clause_ids
                    else:
                        recovered = [
                            claim.claim_id
                            for claim in claims
                            if _claim_supports_unit(unit, claim)
                        ]
                    if recovered:
                        status = "answered"
                        ids = recovered
                        missing = []
                if status == "answered" and clauses and clause_gaps:
                    status = "generation_missing"
                    missing = list(dict.fromkeys([*missing, *clause_gaps]))
                if status == "answered" and (not ids or missing or unit.ambiguity):
                    status = "generation_missing"
                if status == "answered" and ids:
                    claims_by_id = {claim.claim_id: claim for claim in claims}
                    bound = [
                        claim_id
                        for claim_id in ids
                        if (
                            _flow_supports_unit(unit, claim_id[len("flow:") :], valid_flow)
                            if claim_id.startswith("flow:")
                            else _claim_supports_unit(unit, claims_by_id.get(claim_id))
                        )
                    ]
                    ids = bound
                    if not bound:
                        status = "generation_missing"
                        missing = list(
                            dict.fromkeys(
                                [
                                    *missing,
                                    f"{unit.subject} {'、'.join(unit.requested_aspects)}".strip(),
                                ]
                            )
                        )
                        ids = []
                if status != "answered":
                    audit.missing.extend(
                        missing or [f"{unit.subject} {'、'.join(unit.requested_aspects)}".strip()]
                    )
                audit.units.append(
                    {
                        "unit_id": unit.unit_id,
                        "status": status,
                        "claim_ids": ids,
                        "missing_items": missing,
                    }
                )
            unanswered = raw.get("unanswered_questions", [])
            if not isinstance(unanswered, list) or any(not isinstance(m, str) for m in unanswered):
                raise ValueError("invalid unanswered questions")
            audit.missing = list(dict.fromkeys([*audit.missing, *unanswered]))[:12]
            if (
                unanswered
                and audit.units
                and all(row["status"] == "answered" for row in audit.units)
            ):
                audit.units[0]["status"] = "generation_missing"
                audit.units[0]["missing_items"] = list(
                    dict.fromkeys([*audit.units[0]["missing_items"], *unanswered])
                )
        except (ChatProviderError, ValueError, KeyError, TypeError) as exc:
            audit.unavailable = True
            audit.error = safe_error_summary(exc)
            audit.error_kind = exc.kind if isinstance(exc, ChatProviderError) else "internal"
            audit.error_attempts = exc.attempts if isinstance(exc, ChatProviderError) else None
            audit.missing = ["本轮未能完成答案完整性核对"]
            audit.units = [
                {
                    "unit_id": u.unit_id,
                    "status": "verification_failed",
                    "claim_ids": [],
                    "missing_items": ["完整性核对服务不可用"],
                }
                for u in units
            ]
        return audit
