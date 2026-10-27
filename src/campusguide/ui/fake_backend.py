"""无模型 fake backend：给 UI 契约测试和本地设计预览使用。"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import date
from typing import Any

from campusguide.conversation.service import ConversationReply
from campusguide.domain.models import ChecklistItem, Citation, Claim
from campusguide.ui.backends import ProgressEvent

URL = "https://xg.scau.edu.cn/2022/1018/c11387a327371/page.htm"


def _citation(article_no: str, quote: str) -> Citation:
    return Citation(
        article_id=f"national-aid-undergrad@v2021#{article_no}",
        quote=quote,
        quote_start=0,
        quote_end=len(quote),
        doc_title="华南农业大学本科生国家奖助学金实施办法",
        article_no=article_no,
        part_no=1,
        effective_from=date(2021, 12, 31),
        date_confidence="low",
        source_url=URL,
    )


def _policy_reply() -> ConversationReply:
    citation = _citation("第九条", "（一）年级要求：二年级及以上年级。")
    claim = Claim(
        claim_id="c1",
        text="申请国家奖学金的年级要求为二年级及以上年级。",
        citations=[citation],
    )
    text = (
        "**结论要点：**\n\n"
        "1. 申请国家奖学金的年级要求为二年级及以上年级。 "
        "[华南农业大学本科生国家奖助学金实施办法 第九条]\n\n"
        "**依据：**\n\n"
        "- [华南农业大学本科生国家奖助学金实施办法 第九条]"
        f"({URL})\n> （一）年级要求：二年级及以上年级。\n"
        "> 日期为文号年份/月份锚点，非精确发布日。\n\n---\n"
        "演示系统不构成官方答复，具体以学校官网为准。"
    )
    return ConversationReply(
        text=text,
        outcome="ANSWERED",
        decision="pass",
        claims=[claim],
        citations=[citation.model_dump(mode="json")],
        trace=[
            {
                "node": "planner",
                "duration_ms": 120,
                "plan": {
                    "intent": "policy_qa",
                    "flow_id": None,
                    "queries": ["国家奖学金 申请条件"],
                    "planner_source": "rule",
                    "warnings": [],
                },
            },
            {
                "node": "retriever",
                "duration_ms": 350,
                "queries": ["国家奖学金 申请条件"],
                "evidence": 3,
                "coverage": {"q1": ["ev1"]},
                "index_version": "fake-index",
            },
            {"node": "advisor", "duration_ms": 800, "mode": "generate", "claims": 1},
            {"node": "verifier", "duration_ms": 300, "decision": "pass"},
            {"node": "finalize", "duration_ms": 10, "outcome": "ANSWERED"},
        ],
    )


def _closed_reply() -> ConversationReply:
    checklist = [
        ChecklistItem(
            step_id="info-merit-apply",
            title="数信学院：国家奖学金申请",
            status="closed",
            description="在学院通知截止前提交国家奖学金申请材料；学院评审后按规定公示。",
            materials=["国家奖学金申请审批表", "学习成绩与综合测评证明"],
            deadline="2026-10-10T09:00:00+08:00",
            department="数学与信息学院、软件学院学生工作办公室",
            citations=[
                "national-aid-2026-info-merit@v2026#六、申请材料",
                "national-aid-undergrad@v2021#第九条",
            ],
            status_note="本批次截止：2026-10-10 09:00（Asia/Shanghai）",
        ),
        ChecklistItem(
            step_id="agri-merit-check",
            title="农学院：国家奖学金（以学院通知为准）",
            status="conditional",
            description="未检索到农学院 2026 年本科生国家奖学金公开通知；以学院通知为准。",
            materials=["国家奖学金申请审批表"],
            deadline_note="未检索到农学院 2026 年公开通知；以学院当年通知为准",
            department="农学院学生工作办公室",
            citations=["national-aid-undergrad@v2021#第九条"],
            status_note="需要补充学院信息后才能判断",
        ),
    ]
    handoff = {
        "department": "学生工作部资助中心 / 学院学生工作办公室",
        "url": "https://xg.scau.edu.cn/jxj/list.htm",
        "note": "具体名额、材料和批次以学校/学院当年通知为准。",
    }
    text = (
        "本科生国家奖学金申请（状态：已截止）\n"
        "批次：2026 年本科生国家奖助学金批次\n"
        "- [已截止] 数信学院：国家奖学金申请\n"
        "  本批次截止：2026-10-10 09:00（Asia/Shanghai）\n"
        "- [待确认] 农学院：国家奖学金（以学院通知为准）\n\n"
        "下一批次的时间、名额和材料以学校/学院当年通知为准；本流程不推断下一年日期。\n"
        f"人工入口：{handoff['department']}\n{handoff['url']}\n\n---\n"
        "演示系统不构成官方答复，具体以学校官网为准。"
    )
    return ConversationReply(
        text=text,
        flow_id="scholarship-application",
        status="closed",
        checklist=checklist,
        closed=True,
        outcome="ANSWERED",
        handoff=handoff,
        trace=[
            {
                "node": "planner",
                "duration_ms": 80,
                "plan": {
                    "intent": "procedure_guidance",
                    "flow_id": "scholarship-application",
                    "queries": [],
                    "planner_source": "rule",
                },
            },
            {"node": "flow", "duration_ms": 20, "status": "closed", "checklist": 2},
            {"node": "finalize", "duration_ms": 10, "outcome": "ANSWERED"},
        ],
    )


def _certificate_clarify(question: str, values: list[str], missing: list[str]) -> ConversationReply:
    return ConversationReply(
        text=f"{question}\n可以直接回答；不想说也可以说“跳过”。",
        flow_id="enrollment-certificate",
        status="unknown",
        outcome="CLARIFY",
        missing_slots=missing,
        clarification={
            "reason": "SLOT_MISSING",
            "question": question,
            "allowed_values": values,
            "missing_slots": missing,
        },
        trace=[
            {
                "node": "planner",
                "duration_ms": 40,
                "plan": {
                    "intent": "procedure_guidance",
                    "flow_id": "enrollment-certificate",
                    "queries": [],
                    "planner_source": "rule",
                },
            },
            {"node": "finalize", "duration_ms": 5, "outcome": "CLARIFY"},
        ],
    )


def _certificate_final(*, partial: bool = False) -> ConversationReply:
    checklist = [
        ChecklistItem(
            step_id="ug-self-print",
            title="本科生：自助打印在读证明",
            status="conditional",
            description="在校生可用自助打印机；打印前核对姓名、学号、专业等信息。",
            materials=["身份证", "校园卡"],
            deadline_note="自助打印机 24 小时开放；行政楼一楼大厅等",
            department="本科生院教学运行科",
            citations=[
                "enrollment-certificate-guide@v2026#五、自助打印",
                "enrollment-certificate-self-print-2024@v2024#六、打印点",
            ],
            status_note="补充用途后可判断",
        ),
        ChecklistItem(
            step_id="ug-fee",
            title="本科生：费用与领取",
            status="available",
            description="在校生 5 份内免费，超出部分每份 5 元。",
            materials=["现金（超出免费份数时）"],
            deadline_note="以窗口/自助打印现场为准",
            department="本科生院教学运行科",
            citations=["enrollment-certificate-fee-note-2026@v2026#六、收费"],
        ),
    ]
    handoff = {
        "department": "本科生院教学运行科 / 研究生院培养科",
        "url": "https://jwc.scau.edu.cn/hnnydxywcjdwxlxwzmjqtzm/list.htm",
        "note": "本科生：行政楼 305；研究生：培养科行政楼 309。",
    }
    text = (
        "已按“本科生 · 在校生”整理 2 个可见步骤。"
        "\n- [待确认] 本科生：自助打印在读证明\n- [可办理] 本科生：费用与领取\n\n"
        f"人工入口：{handoff['department']}\n\n---\n"
        "演示系统不构成官方答复，具体以学校官网为准。"
    )
    return ConversationReply(
        text=text,
        flow_id="enrollment-certificate",
        status="unknown",
        checklist=checklist,
        missing_slots=["purpose", "delivery"],
        outcome="PARTIAL_ANSWER" if partial else "ANSWERED",
        handoff=handoff,
        trace=[
            {
                "node": "planner",
                "duration_ms": 50,
                "plan": {
                    "intent": "procedure_guidance",
                    "flow_id": "enrollment-certificate",
                    "queries": [],
                    "planner_source": "rule",
                },
            },
            {"node": "flow", "duration_ms": 20, "status": "unknown", "checklist": 2},
            {"node": "finalize", "duration_ms": 5, "outcome": "ANSWERED"},
        ],
    )


def _privacy_reply() -> ConversationReply:
    handoff = {
        "department": "学校相关负责部门",
        "url": "https://www.scau.edu.cn/",
        "note": "请通过学校教务系统或相关部门查询。",
    }
    return ConversationReply(
        text=(
            "本系统不查询个人学号、成绩、排名等个人信息。\n"
            "请通过学校教务系统或相关部门办理。\n\n"
            f"下一步：{handoff['department']}\n{handoff['url']}\n\n---\n"
            "演示系统不构成官方答复，具体以学校官网为准。"
        ),
        outcome="PRIVACY_REQUEST",
        handoff=handoff,
        trace=[
            {
                "node": "planner",
                "duration_ms": 30,
                "plan": {
                    "intent": "privacy_request",
                    "queries": [],
                    "planner_source": "rule",
                },
            },
            {"node": "finalize", "duration_ms": 5, "outcome": "PRIVACY_REQUEST"},
        ],
    )


def _progress_for(reply: ConversationReply) -> list[ProgressEvent]:
    code = reply.outcome
    if code == "CLARIFY":
        return [ProgressEvent("planner", "规划"), ProgressEvent("finalize", "整理")]
    if reply.flow_id:
        return [
            ProgressEvent("planner", "规划"),
            ProgressEvent("flow", "流程"),
            ProgressEvent("finalize", "整理"),
        ]
    if code == "ANSWERED":
        return [
            ProgressEvent("planner", "规划"),
            ProgressEvent("retriever", "检索"),
            ProgressEvent("advisor", "生成"),
            ProgressEvent("verifier", "核对"),
            ProgressEvent("finalize", "整理"),
        ]
    return [ProgressEvent("planner", "规划"), ProgressEvent("finalize", "整理")]


class FakeBackend:
    """按关键词走固定脚本，不读模型、不读网络、不读 Qdrant。"""

    profile = "ultraspeed"

    def __init__(self) -> None:
        self._stages: dict[str, str] = {}

    def handle(self, thread_id: str, message: str) -> ConversationReply:
        text = str(message or "").strip()
        compact = text.replace(" ", "")
        stage = self._stages.get(thread_id)
        if text == "跳过":
            self._stages.pop(thread_id, None)
            return _certificate_final(partial=True)
        if stage == "cert-level":
            self._stages[thread_id] = "cert-status"
            return _certificate_clarify(
                "你目前是在校生还是已经毕业？",
                ["enrolled", "graduated"],
                ["enrollment_status", "purpose", "delivery"],
            )
        if stage == "cert-status":
            self._stages.pop(thread_id, None)
            return _certificate_final(partial=text == "跳过")
        if "在读证明" in compact or "在校证明" in compact:
            self._stages[thread_id] = "cert-level"
            return _certificate_clarify(
                "你是本科生还是研究生？",
                ["undergraduate", "graduate"],
                ["level", "enrollment_status", "purpose", "delivery"],
            )
        if any(key in compact for key in ("绩点", "排名", "学号", "成绩单", "身份证")):
            return _privacy_reply()
        if any(key in compact for key in ("国奖", "奖学金", "助学金")):
            return _closed_reply()
        return _policy_reply()

    def stream(self, thread_id: str, message: str) -> Iterator[ProgressEvent | ConversationReply]:
        reply = self.handle(thread_id, message)
        yield from _progress_for(reply)
        yield reply

    def reset(self, thread_id: str) -> None:
        self._stages.pop(thread_id, None)

    def metadata(self) -> dict[str, Any]:
        return {
            "model_profile": "ultraspeed",
            "models": {
                "planner": "fake",
                "advisor": "fake",
                "semantic": "fake",
            },
            "prompt_versions": {
                "planner": "w6b-planner-2026-10-25",
                "advisor": "w6b-advisor-2026-10-25",
                "semantic": "w6b-semantic-2026-10-25",
            },
            "index_version": "fake-index",
            "graph_version": "w6-2026-10-25",
        }

    def close(self) -> None:
        return None
