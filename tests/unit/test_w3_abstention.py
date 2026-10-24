"""W3 端到端拒答/部分作答：QAService、trace、探针与 scope。"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

from qdrant_client.models import FieldCondition, Filter

from campusguide.agents.advisor import Advisor
from campusguide.agents.providers import FakeChatProvider
from campusguide.domain.enums import (
    ArticleStatus,
    Audience,
    OutcomeCode,
    VerificationDecision,
)
from campusguide.domain.models import Evidence, PolicyFilters
from campusguide.observability.trace import TraceWriter
from campusguide.qa.finalize import PARTIAL_NOTICE
from campusguide.qa.service import QAService, QASettings
from campusguide.retrieval.filters import build_qdrant_filter
from campusguide.retrieval.service import RetrievalError, RetrievalResult
from tests.w2_helpers import make_article, make_evidence

ARTICLE_ID = "demo-policy@v2021#第一条"
ARTICLE_TEXT = (
    "申请奖学金应当学习成绩优异，品行端正。奖励10000元。奖励20000元。联系电话 020-85281234。"
)


class StubRetriever:
    def __init__(
        self,
        *,
        active: list[Evidence] | None = None,
        repealed: list[Evidence] | None = None,
        probe_error: Exception | None = None,
    ) -> None:
        self.active = active or []
        self.repealed = repealed or []
        self.probe_error = probe_error
        self.calls: list[PolicyFilters | None] = []

    def retrieve(
        self,
        query: str,
        *,
        filters: PolicyFilters | None = None,
    ) -> RetrievalResult:
        self.calls.append(filters)
        if filters is not None and filters.status == [ArticleStatus.REPEALED]:
            if self.probe_error is not None:
                raise self.probe_error
            return RetrievalResult(evidence=list(self.repealed), queries=[query])
        return RetrievalResult(evidence=list(self.active), queries=[query])


def _claims_json(*claims: tuple[str, str, list[str]]) -> str:
    payload = {
        "claims": [
            {
                "claim_id": claim_id,
                "text": text,
                "kind": "eligibility",
                "citations": [{"article_id": ARTICLE_ID, "quote": quote} for quote in quotes],
            }
            for claim_id, text, quotes in claims
        ]
    }
    return json.dumps(payload, ensure_ascii=False)


def _active_evidence(**kwargs) -> Evidence:
    article = make_article(article_id=ARTICLE_ID, text=ARTICLE_TEXT, **kwargs)
    return make_evidence(article)


def _service(
    *,
    retriever: StubRetriever,
    responses: list[str | Exception],
    trace_dir: Path,
) -> tuple[QAService, FakeChatProvider]:
    provider = FakeChatProvider(responses)
    service = QAService(
        retriever=retriever,  # type: ignore[arg-type]
        advisor=Advisor(provider),
        settings=QASettings(min_dense_score=0.0),
        trace_writer=TraceWriter(trace_dir),
        today=date(2026, 10, 23),
    )
    return service, provider


def _last_trace(directory: Path) -> dict:
    trace_file = next(directory.glob("*.jsonl"))
    return json.loads(trace_file.read_text("utf-8").splitlines()[-1])


def test_partial_renders_only_verified_claim_and_fixed_notice(tmp_path: Path) -> None:
    retriever = StubRetriever(active=[_active_evidence()])
    service, _ = _service(
        retriever=retriever,
        responses=[
            _claims_json(
                ("c1", "申请奖学金需要学习成绩优异。", ["学习成绩优异，品行端正"]),
                ("c2", "另有一条结论。", ["原文中没有这句话"]),
            )
        ],
        trace_dir=tmp_path / "traces",
    )
    result = service.ask("国家奖学金申请条件是什么？", audience=Audience.UNDERGRADUATE)

    assert result.outcome is OutcomeCode.PARTIAL_ANSWER
    assert result.decision is VerificationDecision.PARTIAL
    assert result.partial is True
    assert result.answered is False
    assert result.renders_answer is True
    assert result.dropped_claim_ids == ["c2"]
    assert result.rendered_claim_ids == ["c1"]
    assert PARTIAL_NOTICE in result.answer_markdown
    assert "已省略 1 条结论。" in result.answer_markdown
    assert "申请奖学金需要学习成绩优异。" in result.answer_markdown
    assert "另有一条结论。" not in result.answer_markdown
    assert len(result.citations) == 1

    trace = _last_trace(tmp_path / "traces")
    assert trace["outcome"] == "PARTIAL_ANSWER"
    assert trace["verification"]["decision"] == "partial"
    assert trace["verification"]["partial"] is True
    assert trace["verification"]["dropped_claim_ids"] == ["c2"]
    assert trace["verification"]["scope_issues"] == []
    assert {"QUOTE_MISMATCH", "NO_VALID_CITATION"} <= {
        issue["code"] for issue in trace["verification"]["issues"]
    }
    assert trace["claims_total"] == 2
    assert trace["claims_kept"] == 1


def test_all_claims_dropped_is_unverifiable(tmp_path: Path) -> None:
    retriever = StubRetriever(active=[_active_evidence()])
    service, _ = _service(
        retriever=retriever,
        responses=[_claims_json(("c1", "另有一条结论。", ["原文中没有这句话"]))],
        trace_dir=tmp_path / "traces",
    )
    result = service.ask("国家奖学金申请条件是什么？")

    assert result.outcome is OutcomeCode.UNVERIFIABLE
    assert result.decision is VerificationDecision.ABSTAIN
    assert result.answer_markdown == ""
    assert result.message
    assert result.dropped_claim_ids == ["c1"]
    assert result.partial is False

    trace = _last_trace(tmp_path / "traces")
    assert trace["verification"]["decision"] == "abstain"
    assert trace["verification"]["reason_code"] == "UNVERIFIABLE"


def test_zero_drafts_is_no_valid_claim(tmp_path: Path) -> None:
    retriever = StubRetriever(active=[_active_evidence()])
    service, _ = _service(
        retriever=retriever,
        responses=['{"claims": []}'],
        trace_dir=tmp_path / "traces",
    )
    result = service.ask("国家奖学金申请条件是什么？")

    assert result.outcome is OutcomeCode.NO_VALID_CLAIM
    assert result.decision is VerificationDecision.ABSTAIN
    assert result.answer_markdown == ""
    assert result.drafts == []
    assert "AUDIENCE_UNDETERMINED" in {issue.code for issue in result.scope_issues}

    trace = _last_trace(tmp_path / "traces")
    assert trace["verification"]["reason_code"] == "NO_VALID_CLAIM"
    assert "AUDIENCE_UNDETERMINED" in {
        issue["code"] for issue in trace["verification"]["scope_issues"]
    }


def test_repealed_probe_hit_returns_only_repealed_without_llm(tmp_path: Path) -> None:
    repealed = make_evidence(
        make_article(article_id=ARTICLE_ID, text=ARTICLE_TEXT, status=ArticleStatus.REPEALED)
    )
    retriever = StubRetriever(active=[], repealed=[repealed])
    service, provider = _service(
        retriever=retriever,
        responses=[],
        trace_dir=tmp_path / "traces",
    )
    result = service.ask("国家奖学金申请条件是什么？")

    assert result.outcome is OutcomeCode.ONLY_REPEALED
    assert result.decision is VerificationDecision.ABSTAIN
    assert result.answer_markdown == ""
    assert provider.calls == []
    assert len(retriever.calls) == 2
    probe_filters = retriever.calls[1]
    assert probe_filters is not None
    assert probe_filters.status == [ArticleStatus.REPEALED]
    assert probe_filters.ignore_effective_dates is True
    assert result.probe_ms >= 0
    assert result.qa_ms == result.duration_ms - result.probe_ms

    trace = _last_trace(tmp_path / "traces")
    assert trace["repealed_probe"]["hit"] is True
    assert trace["repealed_probe"]["latency_ms"] == result.probe_ms
    assert trace["verification"]["reason_code"] == "ONLY_REPEALED"


def test_repealed_probe_miss_returns_no_evidence(tmp_path: Path) -> None:
    retriever = StubRetriever(active=[], repealed=[])
    service, provider = _service(
        retriever=retriever,
        responses=[],
        trace_dir=tmp_path / "traces",
    )
    result = service.ask("学校附近哪家奶茶店最好喝？")

    assert result.outcome is OutcomeCode.NO_EVIDENCE
    assert provider.calls == []
    assert len(retriever.calls) == 2
    assert result.probe_ms >= 0

    trace = _last_trace(tmp_path / "traces")
    assert trace["repealed_probe"]["hit"] is False


def test_repealed_probe_error_is_treated_as_miss(tmp_path: Path) -> None:
    retriever = StubRetriever(active=[], probe_error=RetrievalError("probe down"))
    service, _ = _service(
        retriever=retriever,
        responses=[],
        trace_dir=tmp_path / "traces",
    )
    result = service.ask("学校附近哪家奶茶店最好喝？")

    assert result.outcome is OutcomeCode.NO_EVIDENCE
    trace = _last_trace(tmp_path / "traces")
    assert trace["repealed_probe"]["hit"] is False
    assert "probe down" in trace["repealed_probe"]["error"]


def test_scope_version_mismatch_abstains_without_llm(tmp_path: Path) -> None:
    retriever = StubRetriever(
        active=[_active_evidence(status=ArticleStatus.REPEALED)],
    )
    service, provider = _service(
        retriever=retriever,
        responses=[],
        trace_dir=tmp_path / "traces",
    )
    result = service.ask("国家奖学金申请条件是什么？")

    assert result.outcome is OutcomeCode.VERSION_MISMATCH
    assert result.decision is VerificationDecision.ABSTAIN
    assert provider.calls == []
    assert result.evidence == []
    assert "VERSION_MISMATCH" in {issue.code for issue in result.scope_issues}

    trace = _last_trace(tmp_path / "traces")
    assert trace["verification"]["reason_code"] == "VERSION_MISMATCH"
    assert "VERSION_MISMATCH" in {issue["code"] for issue in trace["verification"]["scope_issues"]}


def test_scope_audience_mismatch_abstains_without_llm(tmp_path: Path) -> None:
    retriever = StubRetriever(active=[_active_evidence(audience=[Audience.GRADUATE])])
    service, provider = _service(
        retriever=retriever,
        responses=[],
        trace_dir=tmp_path / "traces",
    )
    result = service.ask("国家奖学金申请条件是什么？", audience=Audience.UNDERGRADUATE)

    assert result.outcome is OutcomeCode.UNVERIFIABLE
    assert result.decision is VerificationDecision.ABSTAIN
    assert provider.calls == []
    assert "AUDIENCE_MISMATCH" in {issue.code for issue in result.scope_issues}


def test_scope_warning_keeps_answer_and_is_recorded(tmp_path: Path) -> None:
    retriever = StubRetriever(active=[_active_evidence()])
    service, _ = _service(
        retriever=retriever,
        responses=[
            _claims_json(("c1", "申请奖学金需要学习成绩优异。", ["学习成绩优异，品行端正"]))
        ],
        trace_dir=tmp_path / "traces",
    )
    result = service.ask("国家奖学金申请条件是什么？")

    assert result.outcome is OutcomeCode.ANSWERED
    assert result.decision is VerificationDecision.PASS
    assert result.partial is False
    assert "AUDIENCE_UNDETERMINED" in {issue.code for issue in result.issues}
    assert "AUDIENCE_UNDETERMINED" in {issue.code for issue in result.scope_issues}

    trace = _last_trace(tmp_path / "traces")
    assert trace["verification"]["decision"] == "pass"
    assert trace["verification"]["partial"] is False
    assert trace["verification"]["reason_code"] is None
    assert "AUDIENCE_UNDETERMINED" in {issue["code"] for issue in trace["verification"]["issues"]}


def test_privacy_claim_is_dropped_and_not_logged(tmp_path: Path) -> None:
    retriever = StubRetriever(active=[_active_evidence()])
    service, _ = _service(
        retriever=retriever,
        responses=[_claims_json(("c1", "联系电话 13800138000。", ["学习成绩优异，品行端正"]))],
        trace_dir=tmp_path / "traces",
    )
    result = service.ask("国家奖学金申请条件是什么？")

    assert result.outcome is OutcomeCode.UNVERIFIABLE
    assert "PRIVACY_VIOLATION" in {issue.code for issue in result.issues}
    raw_trace = next((tmp_path / "traces").glob("*.jsonl")).read_text("utf-8")
    assert "13800138000" not in raw_trace


def test_fact_mismatch_produces_partial_answer(tmp_path: Path) -> None:
    retriever = StubRetriever(active=[_active_evidence()])
    service, _ = _service(
        retriever=retriever,
        responses=[
            _claims_json(
                ("c1", "申请奖学金需要学习成绩优异。", ["学习成绩优异，品行端正"]),
                ("c2", "奖励20000元。", ["奖励10000元"]),
            )
        ],
        trace_dir=tmp_path / "traces",
    )
    result = service.ask("国家奖学金奖励标准是多少？")

    assert result.outcome is OutcomeCode.PARTIAL_ANSWER
    assert result.dropped_claim_ids == ["c2"]
    issue = next(issue for issue in result.issues if issue.code == "NUMBER_NOT_SUPPORTED")
    assert issue.facts == ["20000元"]


def test_filters_can_ignore_effective_dates_for_repealed_probe() -> None:
    result = build_qdrant_filter(
        PolicyFilters(
            status=[ArticleStatus.REPEALED],
            as_of=date(2026, 10, 23),
            ignore_effective_dates=True,
        )
    )

    keys: set[str] = set()
    for condition in result.must:
        if isinstance(condition, FieldCondition):
            keys.add(condition.key)
        elif isinstance(condition, Filter):
            for item in condition.should or []:
                if isinstance(item, FieldCondition):
                    keys.add(item.key)
    assert "status" in keys
    assert "effective_from" not in keys
    assert "effective_to" not in keys
