"""W3 QAService 集成测试：PARTIAL_ANSWER / UNVERIFIABLE / 废止探针 / trace。"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

from campusguide.agents.advisor import Advisor
from campusguide.agents.providers import FakeChatProvider
from campusguide.domain.enums import ArticleStatus, Audience, OutcomeCode
from campusguide.observability.trace import TraceWriter
from campusguide.qa.service import QAService, QASettings
from campusguide.retrieval.service import RetrievalResult
from tests.w2_helpers import make_article, make_evidence

TODAY = date(2026, 10, 23)


def _claims_json(claims: list[dict]) -> str:
    return json.dumps({"claims": claims}, ensure_ascii=False)


def _valid_claim(claim_id: str = "c1") -> dict:
    return {
        "claim_id": claim_id,
        "text": "申请奖学金应当学习成绩优异，品行端正。",
        "kind": "eligibility",
        "citations": [
            {"article_id": "demo-policy@v2021#第一条", "quote": "申请奖学金应当学习成绩优异"}
        ],
    }


def _number_claim(claim_id: str = "c2") -> dict:
    return {
        "claim_id": claim_id,
        "text": "奖励标准为每人8000元。",
        "kind": "other",
        "citations": [
            {"article_id": "demo-policy@v2021#第一条", "quote": "申请奖学金应当学习成绩优异"}
        ],
    }


class FakeRetriever:
    """可配置 retrieve / probe_repealed 的假检索器。"""

    def __init__(
        self,
        result: RetrievalResult,
        *,
        probe_result: list | None = None,
    ) -> None:
        self._result = result
        self._probe_result = probe_result
        self.probe_calls = 0

    def retrieve(self, query: str, *, filters=None) -> RetrievalResult:
        return self._result

    def probe_repealed(self, query: str, *, filters=None) -> list:
        self.probe_calls += 1
        return self._probe_result or []


def _service(
    *,
    retriever: FakeRetriever,
    responses: list[str],
    trace_dir: Path,
) -> QAService:
    return QAService(
        retriever=retriever,  # type: ignore[arg-type]
        advisor=Advisor(FakeChatProvider(responses)),
        settings=QASettings(min_dense_score=0.0),
        trace_writer=TraceWriter(trace_dir),
        today=TODAY,
    )


def _evidence(*articles) -> list:
    return [
        make_evidence(article, score=0.9, rank=rank)
        for rank, article in enumerate(articles or [make_article()], start=1)
    ]


def _read_traces(trace_dir: Path) -> list[dict]:
    trace_file = next(trace_dir.glob("*.jsonl"))
    return [json.loads(line) for line in trace_file.read_text("utf-8").splitlines()]


def test_partial_answer_renders_only_verified_claims(tmp_path: Path) -> None:
    retriever = FakeRetriever(RetrievalResult(evidence=_evidence(), queries=["奖学金"]))
    service = _service(
        retriever=retriever,
        responses=[_claims_json([_valid_claim(), _number_claim()])],
        trace_dir=tmp_path / "traces",
    )
    result = service.ask("国家奖学金申请条件是什么？")

    assert result.outcome is OutcomeCode.PARTIAL_ANSWER
    assert result.partial is True
    assert result.answered is True
    assert [claim.claim_id for claim in result.claims] == ["c1"]
    assert "8000元" not in result.answer_markdown
    assert "部分结论未通过引用核对" in result.answer_markdown
    assert any(issue.code == "NUMBER_NOT_SUPPORTED" for issue in result.issues)

    trace = _read_traces(tmp_path / "traces")[-1]
    assert trace["verification"]["decision"] == "partial"
    assert trace["verification"]["partial"] is True
    assert trace["verification"]["dropped_claim_ids"] == ["c2"]


def test_all_claims_failed_yields_unverifiable_without_answer(tmp_path: Path) -> None:
    retriever = FakeRetriever(RetrievalResult(evidence=_evidence(), queries=["奖学金"]))
    service = _service(
        retriever=retriever,
        responses=[_claims_json([_number_claim()])],
        trace_dir=tmp_path / "traces",
    )
    result = service.ask("国家奖学金的奖励标准是多少？")

    assert result.outcome is OutcomeCode.UNVERIFIABLE
    assert result.answer_markdown == ""
    assert result.message  # 拒答解释
    assert not result.answered

    trace = _read_traces(tmp_path / "traces")[-1]
    assert trace["verification"]["decision"] == "abstain"
    assert trace["verification"]["issues"][0]["code"] == "NUMBER_NOT_SUPPORTED"


def test_only_repealed_probe_hits(tmp_path: Path) -> None:
    repealed = make_article(
        article_id="old-policy@v2019#第一条",
        status=ArticleStatus.REPEALED,
    )
    retriever = FakeRetriever(
        RetrievalResult(evidence=[], queries=["旧办法"]),
        probe_result=[make_evidence(repealed)],
    )
    service = _service(
        retriever=retriever,
        responses=[],
        trace_dir=tmp_path / "traces",
    )
    result = service.ask("旧的国家奖学金办法怎么规定？")

    assert result.outcome is OutcomeCode.ONLY_REPEALED
    assert retriever.probe_calls == 1
    assert result.answer_markdown == ""

    trace = _read_traces(tmp_path / "traces")[-1]
    assert trace["repealed_probe"]["hit"] is True
    assert "latency_ms" in trace["repealed_probe"]


def test_empty_evidence_without_probe_hit_is_no_evidence(tmp_path: Path) -> None:
    retriever = FakeRetriever(
        RetrievalResult(evidence=[], queries=["无关"]),
        probe_result=[],
    )
    service = _service(
        retriever=retriever,
        responses=[],
        trace_dir=tmp_path / "traces",
    )
    result = service.ask("完全无关的问题")
    assert result.outcome is OutcomeCode.NO_EVIDENCE
    assert retriever.probe_calls == 1


def test_version_conflict_abstains(tmp_path: Path) -> None:
    v1 = make_article(version_id="v2021")
    v2 = make_article(version_id="v2025")
    retriever = FakeRetriever(
        RetrievalResult(evidence=_evidence(v1, v2), queries=["奖学金"]),
        probe_result=[],
    )
    service = _service(
        retriever=retriever,
        responses=[],
        trace_dir=tmp_path / "traces",
    )
    result = service.ask("国家奖学金申请条件是什么？")
    assert result.outcome is OutcomeCode.VERSION_CONFLICT


def test_audience_mismatch_drops_bad_evidence_but_answers(tmp_path: Path) -> None:
    undergrad = make_article()
    faculty = make_article(article_id="faculty@v2021#第一条", audience=[Audience.FACULTY])
    retriever = FakeRetriever(
        RetrievalResult(evidence=_evidence(undergrad, faculty), queries=["奖学金"]),
        probe_result=[],
    )
    service = _service(
        retriever=retriever,
        responses=[_claims_json([_valid_claim()])],
        trace_dir=tmp_path / "traces",
    )
    result = service.ask("国家奖学金申请条件是什么？", audience=Audience.UNDERGRADUATE)

    assert result.outcome is OutcomeCode.ANSWERED
    assert any(issue.code == "AUDIENCE_MISMATCH" for issue in result.issues)
    # faculty 证据不应进入 advisor 输入：只传给 advisor 的 evidence 是 scope 过滤后的
    assert all(item.article.audience != [Audience.FACULTY] for item in result.evidence)


def test_trace_has_no_full_article_text(tmp_path: Path) -> None:
    retriever = FakeRetriever(RetrievalResult(evidence=_evidence(), queries=["奖学金"]))
    service = _service(
        retriever=retriever,
        responses=[_claims_json([_valid_claim()])],
        trace_dir=tmp_path / "traces",
    )
    service.ask("国家奖学金申请条件是什么？")
    trace = _read_traces(tmp_path / "traces")[-1]
    serialized = json.dumps(trace, ensure_ascii=False)
    assert "申请奖学金应当学习成绩优异，品行端正。" not in serialized
    assert "verification" in trace
