from __future__ import annotations

import json
from pathlib import Path

import pytest

from campusguide.flows.calendar import load_calendar
from campusguide.flows.dsl import load_flows

ROOT = Path(__file__).resolve().parents[2]


def test_all_w5_flow_citations_exist_in_text_index() -> None:
    articles_path = ROOT / "data" / "processed" / "articles.jsonl"
    if not articles_path.exists():
        pytest.skip("需要先运行 make index 生成 data/processed/articles.jsonl")

    citations = {
        json.loads(line)["article_id"]
        for line in articles_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    }
    calendar_ids = {event.event_id for event in load_calendar(ROOT / "configs" / "calendar.yaml")}
    flows = load_flows(
        ROOT / "configs" / "flows",
        valid_citations=citations,
        calendar_event_ids=calendar_ids,
    )
    assert {flow.flow_id for flow in flows} == {
        "scholarship-application",
        "recommendation-eligibility",
        "enrollment-certificate",
    }


def test_w5_parsed_sources_contain_no_mobile_or_id() -> None:
    parsed_dir = ROOT / "data" / "processed" / "parsed"
    if not parsed_dir.exists():
        pytest.skip("需要先运行 make parse")

    import re

    doc_ids = [
        "national-aid-2026-info-merit",
        "national-aid-2026-info-aid",
        "national-aid-2026-agri-inspirational-publicity",
        "national-aid-2026-agri-grant-publicity",
        "comprehensive-evaluation-2026-info",
        "comprehensive-evaluation-2026-final-publicity",
        "recommendation-2027-notice",
        "recommendation-2027-publicity",
        "enrollment-certificate-guide",
        "enrollment-certificate-fee-note-2026",
        "enrollment-certificate-self-print-2024",
        "enrollment-certificate-grad-office",
    ]
    for doc_id in doc_ids:
        paths = list((parsed_dir / doc_id).glob("*.json"))
        if not paths:
            continue
        text = json.loads(paths[0].read_text(encoding="utf-8"))["text"]
        assert not re.search(r"(?<!\d)1[3-9]\d{9}(?!\d)", text), doc_id
        assert not re.search(r"(?<!\d)\d{17}[\dXx](?!\d)", text), doc_id
