from __future__ import annotations

import json
from datetime import date
from pathlib import Path

from campusguide.agents.advisor import Advisor
from campusguide.agents.providers import FakeChatProvider
from campusguide.domain.enums import OutcomeCode
from campusguide.domain.models import Evidence
from campusguide.observability.trace import TraceWriter
from campusguide.qa.service import QAService, QASettings
from campusguide.retrieval.service import RetrievalError, RetrievalResult
from tests.w2_helpers import make_article, make_evidence


def _claims_json(quote: str = "学习成绩优异", article_id: str = "demo-policy@v2021#第一条") -> str:
    return json.dumps(
        {
            "claims": [
                {
                    "claim_id": "c1",
                    "text": "申请奖学金需要学习成绩优异。",
                    "kind": "eligibility",
                    "citations": [{"article_id": article_id, "quote": quote}],
                }
            ]
        },
        ensure_ascii=False,
    )


class FakeRetriever:
    def __init__(self, result: RetrievalResult | Exception) -> None:
        self._result = result
        self.calls = 0

    def retrieve(self, query: str, *, filters=None) -> RetrievalResult:
        self.calls += 1
        if isinstance(self._result, Exception):
            raise self._result
        return self._result


def _service(
    *,
    retrieval: RetrievalResult | Exception,
    responses: list[str | Exception],
    trace_dir: Path,
) -> tuple[QAService, FakeRetriever]:
    retriever = FakeRetriever(retrieval)
    service = QAService(
        retriever=retriever,  # type: ignore[arg-type]
        advisor=Advisor(FakeChatProvider(responses)),
        settings=QASettings(min_dense_score=0.0),
        trace_writer=TraceWriter(trace_dir),
        today=date(2026, 10, 22),
    )
    return service, retriever


def _evidence() -> list:
    return [make_evidence(make_article(), score=0.9, rank=1)]


def test_service_answers_with_validated_citation_and_writes_trace(tmp_path: Path) -> None:
    service, _ = _service(
        retrieval=RetrievalResult(evidence=_evidence(), queries=["奖学金"]),
        responses=[_claims_json()],
        trace_dir=tmp_path / "traces",
    )
    result = service.ask("国家奖学金申请条件是什么？联系电话 13800138000")

    assert result.outcome is OutcomeCode.ANSWERED
    assert result.answer_markdown
    assert len(result.citations) == 1
    assert result.citations[0].article_id == "demo-policy@v2021#第一条"
    assert result.trace_id

    trace_file = next((tmp_path / "traces").glob("*.jsonl"))
    records = [json.loads(line) for line in trace_file.read_text("utf-8").splitlines()]
    assert records[-1]["outcome"] == "ANSWERED"
    assert "13800138000" not in records[-1]["question_redacted"]
    assert "手机号已脱敏" in records[-1]["question_redacted"]
    assert "13800138000" not in records[-1]["normalized_query"]
    assert all("13800138000" not in query for query in records[-1]["queries"])
    assert records[-1]["claims_kept"] == 1
    assert "evidence" not in records[-1]


def test_trace_redacts_retrieval_query(tmp_path: Path) -> None:
    article = make_article()
    evidence = [
        Evidence(
            article=article,
            dense_score=0.9,
            rank=1,
            retrieval_query="奖学金咨询 test@example.com 13800138000",
        )
    ]
    service, _ = _service(
        retrieval=RetrievalResult(evidence=evidence, queries=["奖学金咨询"]),
        responses=[_claims_json()],
        trace_dir=tmp_path / "traces",
    )
    result = service.ask("国家奖学金申请条件是什么？")
    assert result.outcome is OutcomeCode.ANSWERED

    trace_file = next((tmp_path / "traces").glob("*.jsonl"))
    record = json.loads(trace_file.read_text("utf-8").splitlines()[-1])
    logged = record["retrieval"][0]["retrieval_query"]
    assert "test@example.com" not in logged
    assert "13800138000" not in logged
    assert "[个人邮箱已脱敏]" in logged
    assert "手机号已脱敏" in logged


def test_service_returns_no_evidence(tmp_path: Path) -> None:
    service, _ = _service(
        retrieval=RetrievalResult(evidence=[], queries=["奶茶"]),
        responses=[],
        trace_dir=tmp_path,
    )
    result = service.ask("学校附近哪家奶茶店最好喝？")
    assert result.outcome is OutcomeCode.NO_EVIDENCE
    assert result.answer_markdown == ""


def test_service_returns_index_unavailable(tmp_path: Path) -> None:
    service, _ = _service(
        retrieval=RetrievalError("connection refused"),
        responses=[],
        trace_dir=tmp_path,
    )
    result = service.ask("国家奖学金申请条件是什么？")
    assert result.outcome is OutcomeCode.INDEX_UNAVAILABLE


def test_service_repairs_bad_json_then_answers(tmp_path: Path) -> None:
    service, _ = _service(
        retrieval=RetrievalResult(evidence=_evidence(), queries=["奖学金"]),
        responses=["not json", _claims_json()],
        trace_dir=tmp_path,
    )
    result = service.ask("国家奖学金申请条件是什么？")
    assert result.outcome is OutcomeCode.ANSWERED


def test_service_generation_failed_after_two_bad_outputs(tmp_path: Path) -> None:
    service, _ = _service(
        retrieval=RetrievalResult(evidence=_evidence(), queries=["奖学金"]),
        responses=["not json", "still not json"],
        trace_dir=tmp_path,
    )
    result = service.ask("国家奖学金申请条件是什么？")
    assert result.outcome is OutcomeCode.GENERATION_FAILED
    assert result.answer_markdown == ""


def test_service_unverifiable_when_quote_fabricated(tmp_path: Path) -> None:
    # W3 判定树：有 claim 但全部未通过核对 -> UNVERIFIABLE（零 claims 才是 NO_VALID_CLAIM）
    service, _ = _service(
        retrieval=RetrievalResult(evidence=_evidence(), queries=["奖学金"]),
        responses=[_claims_json(quote="原文中没有这句话")],
        trace_dir=tmp_path,
    )
    result = service.ask("国家奖学金申请条件是什么？")
    assert result.outcome is OutcomeCode.UNVERIFIABLE
    assert result.answer_markdown == ""
    assert result.dropped_citations[0]["reason"] == "QUOTE_MISMATCH"


def test_service_guard_short_circuits_before_retrieval(tmp_path: Path) -> None:
    service, retriever = _service(
        retrieval=RetrievalResult(evidence=_evidence(), queries=[]),
        responses=[],
        trace_dir=tmp_path,
    )
    result = service.ask("帮我查一下我的学号")
    assert result.outcome is OutcomeCode.PRIVACY_REQUEST
    assert retriever.calls == 0
