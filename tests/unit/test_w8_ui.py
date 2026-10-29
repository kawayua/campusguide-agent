"""W8 UI 视图模型与浏览器会话存储的离线测试。"""

from __future__ import annotations

import json
from datetime import date

from campusguide.domain.models import ChecklistItem, Citation, Claim
from campusguide.ui import store as ui_store
from campusguide.ui import view_models as vm
from campusguide.ui.fake_backend import FakeBackend

URL = "https://xg.scau.edu.cn/2022/1018/c11387a327371/page.htm"


def _citation() -> Citation:
    return Citation(
        article_id="national-aid-undergrad@v2021#第九条",
        quote="（一）年级要求：二年级及以上年级。",
        quote_start=0,
        quote_end=18,
        doc_title="华南农业大学本科生国家奖助学金实施办法",
        article_no="第九条",
        part_no=1,
        effective_from=date(2021, 12, 31),
        date_confidence="low",
        source_url=URL,
    )


def _checklist_reply(*, na: bool = False):
    citation = _citation()
    items = [
        ChecklistItem(
            step_id="a",
            title="数信学院：国家奖学金申请",
            status="closed",
            description="在学院通知截止前提交材料。",
            materials=["审批表"],
            deadline="2026-10-10T09:00:00+08:00",
            department="学生工作办公室",
            citations=["national-aid-2026-info-merit@v2026#六、申请材料"],
            status_note="本批次截止：2026-10-10 09:00（Asia/Shanghai）",
        ),
        ChecklistItem(
            step_id="b",
            title="农学院：以学院通知为准",
            status="conditional",
            description="未检索到公开通知。",
            deadline_note="以学院当年通知为准",
            department="农学院学生工作办公室",
        ),
    ]
    if na:
        items.append(
            ChecklistItem(
                step_id="c",
                title="研究生：联系培养科",
                status="not_applicable",
                description="不适用于本科生。",
                deadline_note="以培养科确认为准",
                department="研究生院培养科",
            )
        )
    return {
        "text": "办理路径\n\n---\n演示系统不构成官方答复，具体以学校官网为准。",
        "outcome": "ANSWERED",
        "decision": "pass",
        "closed": True,
        "status": "closed",
        "flow_id": "scholarship-application",
        "checklist": [item.model_dump(mode="json") for item in items],
        "claims": [Claim(claim_id="c1", text="年级要求为二年级及以上。", citations=[citation])],
        "citations": [citation.model_dump(mode="json")],
        "trace": [
            {"node": "planner", "duration_ms": 10, "plan": {"intent": "policy_qa"}},
            {"node": "finalize", "duration_ms": 5, "outcome": "ANSWERED"},
        ],
        "handoff": {"department": "学生工作部", "url": "https://xg.scau.edu.cn/"},
    }


def test_outcome_badge_maps_codes_and_closed() -> None:
    html = vm.outcome_badge_html("ANSWERED", closed=True)
    assert "已核实回答" in html
    assert "批次已截止" in html
    assert "w8-badge-success" in html
    partial = vm.outcome_badge_html("PARTIAL_ANSWER")
    assert "部分回答" in partial
    assert "w8-badge-warn" in partial


def test_checklist_hides_not_applicable_until_requested() -> None:
    reply = _checklist_reply(na=True)
    default = vm.checklist_html(reply)
    assert "研究生：联系培养科" not in default
    assert "不适用 1（默认隐藏）" in default
    shown = vm.checklist_html(reply, show_not_applicable=True)
    assert "研究生：联系培养科" in shown
    assert "不适用 1（已显示）" in shown


def test_closed_deadline_not_duplicated_and_na_filter() -> None:
    html = vm.checklist_html(_checklist_reply())
    assert html.count("2026-10-10 09:00") == 1
    assert "本批次截止" in html
    filtered = vm.checklist_html(_checklist_reply(), status_filter="conditional")
    assert "农学院：以学院通知为准" in filtered
    assert "数信学院：国家奖学金申请" not in filtered


def test_citations_separate_claim_quote_and_flow_clause() -> None:
    html = vm.citations_html(_checklist_reply())
    assert "结论到依据" in html
    assert "政策结论引用" in html
    assert "流程依据" in html
    assert "日期口径待核" in html
    assert "无逐字 quote" in html
    assert "national-aid-2026-info-merit" in html


def test_clarify_options_use_chinese_labels() -> None:
    reply = FakeBackend().handle("t1", "我要开在读证明")
    options = vm.clarify_options(reply)
    assert options == [("本科生", "undergraduate"), ("研究生", "graduate")]
    prompt = vm.clarify_prompt_html(reply)
    assert "你是本科生还是研究生" in prompt
    assert "还缺" in prompt


def test_debug_redacts_sensitive_values() -> None:
    reply = _checklist_reply()
    reply["trace"].append(
        {
            "node": "retriever",
            "duration_ms": 3,
            "queries": ["test"],
            "api_key": "sk-should-not-appear",
            "text_raw_ref": "raw-secret",
        }
    )
    html = vm.debug_html(
        reply,
        metadata={"model_profile": "ultraspeed", "index_version": "idx"},
        trace_id="trace-1",
    )
    assert "sk-should-not-appear" not in html
    assert "raw-secret" not in html
    assert "trace-1" in html
    assert "Planner" in html


def test_store_escapes_user_html_and_tracks_preview() -> None:
    _store, session = ui_store.ensure_current(ui_store.empty_store())
    ui_store.append_user_message(session, "<script>alert(1)</script>")
    assert "&lt;script&gt;" in session["messages"][0]["content"]
    assert "<script>" not in session["messages"][0]["content"]
    reply = FakeBackend().handle("t2", "国家奖学金怎么申请")
    _store2, session2 = ui_store.ensure_current(ui_store.empty_store())
    ui_store.append_user_message(session2, "国家奖学金怎么申请")
    ui_store.apply_reply(session2, reply, "<div>assistant</div>", trace_id="trace-2")
    assert session2["title"] == "国家奖学金怎么申请"
    assert session2["outcome"] == "ANSWERED"
    assert "2 步" in session2["preview"]
    assert session2["active_tab"] == "checklist"
    assert session2["messages"][-1]["role"] == "assistant"


def test_store_round_trip_and_invalid_input() -> None:
    store, _ = ui_store.ensure_current(ui_store.empty_store())
    encoded = json.dumps(store, ensure_ascii=False)
    restored = ui_store.normalize_store(json.loads(encoded))
    assert restored["current_id"] == store["current_id"]
    assert ui_store.session_choices(restored)
    assert "\n" in ui_store.session_choices(restored)[0][0]
    assert ui_store.normalize_store({"bad": "shape"}) == ui_store.empty_store()
    store["ui"] = {"status_filter": "closed", "show_na": True}
    store["requests"] = [1.0, 2.0]
    normalized = ui_store.normalize_store(store)
    assert normalized["ui"] == {"status_filter": "closed", "show_na": True}
    assert normalized["requests"] == [1.0, 2.0]


def test_progress_dots_mark_completed_active_and_pending() -> None:
    initial = vm.progress_html("正在规划与核对", nodes=[])
    assert initial.count('<li class="w8-progress-step') == 5
    assert initial.count("w8-progress-step active") == 1
    assert initial.count("w8-progress-step pending") == 4
    assert initial.count("w8-progress-step done") == 0

    started = vm.progress_html("正在检索", nodes=["planner", "retriever"])
    assert started.count("w8-progress-step done") == 1
    assert started.count("w8-progress-step active") == 1
    assert "检索" in started
    assert 'aria-current="step"' in started

    flow = vm.progress_html("流程", nodes=["planner", "flow", "advisor"])
    assert flow.count("w8-progress-step done") == 2
    assert "流程" in flow
    assert "生成" in flow


def test_plain_status_message_does_not_render_stepper() -> None:
    html = vm.progress_html("服务暂时不可用 < 请重试")
    assert "w8-status-message" in html
    assert "w8-progress-dots" not in html
    assert "&lt;" in html
