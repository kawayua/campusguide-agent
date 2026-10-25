from __future__ import annotations

from datetime import date
from pathlib import Path

from campusguide.config import HandoffEntry, load_handoff_config
from campusguide.domain.enums import BlockType
from campusguide.domain.models import ChecklistItem, Citation, Claim
from campusguide.handoff import resolve_handoff
from campusguide.qa.finalize import (
    DISCLAIMER,
    SEMANTIC_UNCHECKED_NOTICE,
    finalize,
    finalize_abstain,
    finalize_flow,
    finalize_mixed,
)
from tests.w6_helpers import article, catalog

ROOT = Path(__file__).resolve().parents[2]


def claim() -> Claim:
    item = article("national-aid-undergrad@v2021#第九条")
    return Claim(
        claim_id="c1",
        text="国家奖学金要求二年级及以上。",
        citations=[
            Citation(
                article_id=item.article_id,
                quote=item.text[:60],
                quote_start=0,
                quote_end=60,
                doc_title=item.doc_title,
                article_no=item.article_no,
                part_no=item.part_no,
                block_type=BlockType.ARTICLE,
                effective_from=item.effective_from,
                date_confidence=item.date_confidence,
                source_url=item.source_url,
            )
        ],
    )


def checklist() -> list[ChecklistItem]:
    return [
        ChecklistItem(
            step_id="s1",
            title="提交申请",
            status="available",
            description="在截止前提交",
            materials=["申请表"],
            deadline="2026-10-10",
            department="学院学工办",
            citations=["national-aid-undergrad@v2021#第九条"],
        )
    ]


def flow():
    return catalog().get_flow("scholarship-application", as_of=date(2026, 9, 30))


def test_policy_finalize_semantic_unchecked_notice() -> None:
    result = finalize([claim()], semantic_unchecked=True)
    assert result.answer_markdown.startswith(f"> {SEMANTIC_UNCHECKED_NOTICE}")
    assert "结论要点" in result.answer_markdown
    assert result.citations


def test_flow_finalize_renders_checklist_and_handoff() -> None:
    handoff = resolve_handoff(
        load_handoff_config(ROOT / "configs" / "handoff.yaml"),
        flow=flow(),
    )
    text = finalize_flow(
        flow(),
        checklist(),
        status="open",
        handoff=handoff,
    )
    assert "本科生国家奖学金申请" in text
    assert "提交申请" in text
    assert "申请表" in text
    assert "学院学工办" in text
    assert "人工入口" in text
    assert DISCLAIMER in text


def test_flow_closed_adds_next_batch_note() -> None:
    handoff = resolve_handoff(None, flow=flow())
    text = finalize_flow(flow(), checklist(), status="closed", handoff=handoff, closed=True)
    assert "下一批次" in text


def test_mixed_has_both_sections_and_one_disclaimer() -> None:
    handoff = resolve_handoff(None, flow=flow())
    result = finalize_mixed(
        [claim()],
        flow=flow(),
        checklist=checklist(),
        status="open",
        handoff=handoff,
        semantic_unchecked=True,
    )
    text = result.answer_markdown
    assert "**办理路径" in text
    assert "**政策说明" in text
    assert "提交申请" in text
    assert "国家奖学金要求二年级及以上" in text
    assert text.count(DISCLAIMER) == 1
    assert text.startswith(f"> {SEMANTIC_UNCHECKED_NOTICE}")


def test_abstain_has_reason_missing_and_handoff() -> None:
    handoff = resolve_handoff(None, reason="NO_EVIDENCE")
    text = finalize_abstain(
        message="没有检索到可引用的现行条款。",
        handoff=handoff,
        missing_slots=["college"],
        warnings=["子问题“金额是多少”未检索到可引用的依据，未作答。"],
    )
    assert "暂时无法作答" in text
    assert "college" in text
    assert "学校相关负责部门" in text
    assert "金额是多少" in text
    assert DISCLAIMER in text


def test_handoff_priority_flow_then_config_then_default() -> None:
    config = load_handoff_config(ROOT / "configs" / "handoff.yaml")
    with_flow = resolve_handoff(config, flow=flow())
    assert with_flow.source == "flow"
    assert "学生工作部资助中心" in with_flow.department
    no_flow = resolve_handoff(config, reason="NO_EVIDENCE")
    assert no_flow.source == "config:reason"
    assert no_flow.department == "学校相关负责部门"
    default = resolve_handoff(config)
    assert default.source == "config:default"
    assert default.department == "学校相关负责部门"


def test_handoff_entry_url_is_string_not_invented() -> None:
    entry = HandoffEntry(department="某部门", url="https://example.edu/x")
    assert entry.url.startswith("https://")
