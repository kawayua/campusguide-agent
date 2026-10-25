"""W6 设计 13.2 的 5 个固定场景。"""

from __future__ import annotations

import json
from datetime import date

from tests.w6_helpers import (
    DEFAULT_AS_OF,
    FakeSearchClient,
    article,
    claims_json,
    make_runner,
    supported_json,
)


def quote() -> str:
    return article("national-aid-undergrad@v2021#第九条").text[:70]


def bad_claims(text: str) -> str:
    return json.dumps(
        {
            "claims": [
                {
                    "claim_id": "c1",
                    "text": text,
                    "kind": "eligibility",
                    "citations": [
                        {
                            "article_id": "national-aid-undergrad@v2021#第九条",
                            "quote": quote(),
                        }
                    ],
                }
            ]
        },
        ensure_ascii=False,
    )


def test_scenario_1_policy_qa_supported() -> None:
    runner = make_runner(
        advisor_responses=[claims_json("national-aid-undergrad@v2021#第九条", quote())],
        semantic_responses=[supported_json()],
    )
    result = runner.invoke("sc1", "申请国家奖学金需要满足哪些条件？", as_of=DEFAULT_AS_OF)
    assert result["outcome"]["code"] == "ANSWERED"
    assert result["verification_decision"].value == "pass"
    assert result["semantic"]["labels"]["c1"] == "supported"


def test_scenario_2_overgeneralization_is_revised() -> None:
    runner = make_runner(
        advisor_responses=[
            bad_claims("所有本科生都可以申请国家奖学金。"),
            bad_claims("国家奖学金要求二年级及以上。"),
        ],
        semantic_responses=[
            json.dumps(
                {"labels": [{"claim_id": "c1", "label": "contradicted", "detail": "范围过大"}]},
                ensure_ascii=False,
            ),
            supported_json(),
        ],
    )
    result = runner.invoke("sc2", "申请国家奖学金需要满足哪些条件？", as_of=DEFAULT_AS_OF)
    assert "所有本科生" not in result["final_answer"]
    assert result["outcome"]["code"] == "ANSWERED"


def test_scenario_3_multi_question_reports_missing_part() -> None:
    client = FakeSearchClient(
        by_query={
            "申请国家奖学金需要满足哪些条件": ["national-aid-undergrad@v2021#第九条"],
            "申请国家励志奖学金需要满足哪些条件": [],
        }
    )
    runner = make_runner(
        search_client=client,
        advisor_responses=[claims_json("national-aid-undergrad@v2021#第九条", quote())],
        semantic_responses=[supported_json()],
    )
    result = runner.invoke(
        "sc3",
        "申请国家奖学金需要满足哪些条件？申请国家励志奖学金需要满足哪些条件？",
        as_of=DEFAULT_AS_OF,
    )
    assert len(result["plan"].queries) == 2
    assert "国家励志奖学金" in result["final_answer"]
    assert "未检索到" in result["final_answer"]


def test_scenario_4_flow_open_and_closed() -> None:
    runner = make_runner(advisor_responses=[])
    closed = runner.invoke("sc4a", "数信学院大二申请国奖", as_of=date(2026, 10, 25))
    assert closed["closed"] is True
    assert closed["pending_slot"] is None
    assert "已截止" in closed["final_answer"]

    open_first = runner.invoke("sc4b", "数信学院大二申请国奖", as_of=date(2026, 9, 30))
    assert open_first["outcome"]["code"] == "CLARIFY"
    open_done = runner.invoke("sc4b", "没有处分", as_of=date(2026, 9, 30))
    assert open_done["flow_status"] == "open"
    assert open_done["closed"] is False


def test_scenario_5_clarify_then_decline() -> None:
    runner = make_runner(advisor_responses=[])
    first = runner.invoke("sc5", "奖学金怎么申请", as_of=date(2026, 9, 30))
    assert first["outcome"]["code"] == "CLARIFY"
    assert first["clarification"]["reason"] == "SLOT_MISSING"
    declined = runner.invoke("sc5", "跳过", as_of=date(2026, 9, 30))
    assert declined["outcome"]["code"] in {"ANSWERED", "PARTIAL_ANSWER"}
    assert "人工入口" in declined["final_answer"]
