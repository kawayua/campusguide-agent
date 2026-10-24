"""W3 单轮问答线性编排。

guard -> retrieval -> （空结果）废止探针 -> scope -> advisor
      -> deterministic verifier -> finalize -> trace

W3 不修改 W2 的检索/生成主路径，只在其后增加 scope 与 claim 核对层。
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from typing import Any

from campusguide.agents.advisor import Advisor, AdvisorError, AdvisorResult
from campusguide.agents.providers import ChatProviderError
from campusguide.config import AbstentionConfig, RetrievalConfig
from campusguide.domain.enums import (
    ArticleStatus,
    Audience,
    BlockType,
    OutcomeCode,
    VerificationDecision,
)
from campusguide.domain.models import (
    Citation,
    Claim,
    Evidence,
    PolicyFilters,
    VerificationIssue,
    VerificationResult,
)
from campusguide.observability.redact import redact_sensitive
from campusguide.observability.trace import TraceWriter
from campusguide.qa.finalize import (
    DISCLAIMER,
    PARTIAL_NOTICE,
    FinalizeResult,
    finalize,
)
from campusguide.qa.guard import MAX_QUERY_CHARS, check_guard
from campusguide.retrieval.normalize import normalize_query
from campusguide.retrieval.service import DenseRetriever, RetrievalError
from campusguide.verification.claims import ClaimValidator
from campusguide.verification.scope import ScopeResult, apply_scope
from campusguide.verification.verifier import Verifier, VerifyReport

_MESSAGES = {
    OutcomeCode.NO_EVIDENCE: (
        "没有检索到可引用的现行条款。请换一种说法，或补充具体事项（如“国家奖学金申请条件”）。"
    ),
    OutcomeCode.ONLY_REPEALED: (
        "只检索到已废止版本，W3 不对废止材料作答。请查询现行版本，或联系负责部门确认。"
    ),
    OutcomeCode.UNVERIFIABLE: (
        "检索到的材料未能通过引用与事实核对，因此不作答。请换一种问法，或直接查看学校官网原文。"
    ),
    OutcomeCode.VERSION_MISMATCH: (
        "检索到的材料不在当前有效期内，因此不作答。请确认问题对应的现行版本。"
    ),
    OutcomeCode.VERSION_CONFLICT: (
        "检索到同一条款的多个并存版本，无法确定适用版本，因此不作答。请提供适用年度或届别。"
    ),
    OutcomeCode.NO_VALID_CLAIM: (
        "检索到了材料，但模型没有给出可核对的结论，因此不作答。请稍后重试或换一种问法。"
    ),
    OutcomeCode.GENERATION_FAILED: (
        "生成服务暂时不可用或输出不符合契约。请检查 LLM 配置后重试；系统不会在无引用情况下作答。"
    ),
    OutcomeCode.INDEX_UNAVAILABLE: (
        "向量索引不可用（Qdrant 未启动或索引未构建）。请先启动 Qdrant 并运行 build_vectors.py。"
    ),
}


@dataclass
class QASettings:
    max_query_chars: int = MAX_QUERY_CHARS
    top_k: int = 20
    max_evidence: int = 8
    max_chars_per_evidence: int = 800
    min_dense_score: float = 0.55
    token_budget: int = 4000
    block_types: list[BlockType] = field(default_factory=lambda: [BlockType.ARTICLE])
    min_quote_chars: int = 6
    max_claims: int = 5
    enable_repealed_probe: bool = True

    @classmethod
    def from_config(
        cls,
        config: RetrievalConfig,
        abstention: AbstentionConfig | None = None,
    ) -> QASettings:
        block_types = (
            [BlockType.ARTICLE, BlockType.SECTION]
            if config.include_sections_default
            else [BlockType.ARTICLE]
        )
        return cls(
            top_k=config.query_top_k,
            max_evidence=config.max_evidence,
            max_chars_per_evidence=config.max_chars_per_evidence,
            min_dense_score=config.min_dense_score,
            token_budget=config.token_budget,
            block_types=block_types,
            min_quote_chars=abstention.min_quote_chars if abstention else 6,
            max_claims=abstention.max_claims if abstention else 5,
            enable_repealed_probe=(abstention.enable_repealed_probe if abstention else True),
        )


@dataclass
class QAResult:
    trace_id: str
    outcome: OutcomeCode
    answer_markdown: str = ""
    claims: list[Claim] = field(default_factory=list)
    citations: list[Citation] = field(default_factory=list)
    evidence: list[Evidence] = field(default_factory=list)
    queries: list[str] = field(default_factory=list)
    dropped_claims: list[dict[str, str]] = field(default_factory=list)
    dropped_citations: list[dict[str, str]] = field(default_factory=list)
    message: str = ""
    duration_ms: int = 0
    llm_model: str = ""
    retrieval_scores: list[dict[str, Any]] = field(default_factory=list)
    decision: VerificationDecision | None = None
    issues: list[VerificationIssue] = field(default_factory=list)
    scope_issues: list[VerificationIssue] = field(default_factory=list)
    dropped_claim_ids: list[str] = field(default_factory=list)
    partial: bool = False
    probe_ms: int = 0
    probe: dict[str, Any] = field(default_factory=dict)
    drafts: list[dict[str, Any]] = field(default_factory=list)
    rendered_claim_ids: list[str] = field(default_factory=list)

    @property
    def answered(self) -> bool:
        return self.outcome is OutcomeCode.ANSWERED

    @property
    def renders_answer(self) -> bool:
        return self.outcome in {OutcomeCode.ANSWERED, OutcomeCode.PARTIAL_ANSWER}

    @property
    def qa_ms(self) -> int:
        return max(0, self.duration_ms - self.probe_ms)


def _issue_payload(issue: VerificationIssue) -> dict[str, Any]:
    return issue.model_dump(mode="json", exclude_none=True)


def _verification_payload(
    result: VerificationResult,
    scope_issues: list[VerificationIssue],
) -> dict[str, Any]:
    return {
        "decision": result.decision.value,
        "issues": [_issue_payload(issue) for issue in result.issues],
        "scope_issues": [_issue_payload(issue) for issue in scope_issues],
        "dropped_claim_ids": list(result.dropped_claim_ids),
        "partial": result.decision is VerificationDecision.PARTIAL,
        "reason_code": result.reason_code.value if result.reason_code else None,
    }


def _record_payload(record: dict[str, Any]) -> dict[str, Any]:
    payload = dict(record)
    payload["issues"] = [_issue_payload(issue) for issue in record.get("issues", [])]
    return payload


class QAService:
    def __init__(
        self,
        *,
        retriever: DenseRetriever,
        advisor: Advisor,
        settings: QASettings | None = None,
        validator: ClaimValidator | None = None,
        verifier: Verifier | None = None,
        trace_writer: TraceWriter | None = None,
        today: date | None = None,
    ) -> None:
        self._retriever = retriever
        self._advisor = advisor
        self._settings = settings or QASettings()
        if verifier is None:
            validator = validator or ClaimValidator(
                min_quote_chars=self._settings.min_quote_chars,
                max_claims=self._settings.max_claims,
            )
            verifier = Verifier(validator)
        self._verifier = verifier
        self._trace_writer = trace_writer
        self._today = today

    def warmup(self) -> None:
        """预加载检索模型；不产生 trace，也不调用 LLM。"""
        self._retriever.warmup()

    def ask(
        self,
        question: str,
        *,
        audience: Audience | None = None,
        session_id: str | None = None,
        block_types: list[BlockType] | None = None,
    ) -> QAResult:
        started = time.perf_counter()
        trace_id = str(uuid.uuid4())
        session_id = session_id or str(uuid.uuid4())
        started_at = datetime.now(UTC)
        normalized = normalize_query(question)
        retrieval_audience = None if audience in (None, Audience.ALL) else audience
        filters = PolicyFilters(
            block_types=list(block_types or self._settings.block_types),
            as_of=self._today or date.today(),
            audience=[retrieval_audience] if retrieval_audience else None,
        )
        trace_extra: dict[str, Any] = {
            "normalized_query": redact_sensitive(normalized),
            "filters": filters.model_dump(mode="json"),
            "repealed_probe": {"hit": False, "latency_ms": 0, "article_ids": []},
        }

        guard = check_guard(question, max_chars=self._settings.max_query_chars)
        if not guard.allowed:
            return self._finish_abstain(
                trace_id=trace_id,
                session_id=session_id,
                started=started,
                started_at=started_at,
                question=question,
                trace_extra=trace_extra,
                outcome=guard.outcome or OutcomeCode.INVALID_REQUEST,
                message=guard.message,
                issues=[],
                scope_issues=[],
            )

        try:
            retrieval = self._retriever.retrieve(question, filters=filters)
        except RetrievalError as exc:
            trace_extra["error"] = str(exc)[:300]
            return self._finish_abstain(
                trace_id=trace_id,
                session_id=session_id,
                started=started,
                started_at=started_at,
                question=question,
                trace_extra=trace_extra,
                outcome=OutcomeCode.INDEX_UNAVAILABLE,
                message=_MESSAGES[OutcomeCode.INDEX_UNAVAILABLE],
                issues=[],
                scope_issues=[],
            )

        evidence = retrieval.evidence
        trace_extra["queries"] = [redact_sensitive(query) for query in retrieval.queries]
        trace_extra["retrieval"] = [
            {
                "article_id": item.article.article_id,
                "part_no": item.article.part_no,
                "score": round(item.score, 4),
                "block_type": item.article.block_type.value,
                "rank": item.rank,
                "retrieval_query": redact_sensitive(item.retrieval_query),
            }
            for item in evidence
        ]
        trace_extra["candidates"] = retrieval.candidates

        if not evidence:
            probe = self._probe(question, filters)
            trace_extra["repealed_probe"] = probe
            outcome = OutcomeCode.ONLY_REPEALED if probe["hit"] else OutcomeCode.NO_EVIDENCE
            return self._finish_abstain(
                trace_id=trace_id,
                session_id=session_id,
                started=started,
                started_at=started_at,
                question=question,
                trace_extra=trace_extra,
                outcome=outcome,
                message=_MESSAGES[outcome],
                issues=[],
                scope_issues=[],
                queries=retrieval.queries,
                probe_ms=probe["latency_ms"],
            )

        scope: ScopeResult = apply_scope(
            evidence,
            audience=audience,
            as_of=filters.as_of,
        )
        if not scope.evidence:
            outcome = scope.reason_code or OutcomeCode.UNVERIFIABLE
            return self._finish_abstain(
                trace_id=trace_id,
                session_id=session_id,
                started=started,
                started_at=started_at,
                question=question,
                trace_extra=trace_extra,
                outcome=outcome,
                message=_MESSAGES.get(outcome, DISCLAIMER),
                issues=scope.issues,
                scope_issues=scope.issues,
                queries=retrieval.queries,
            )

        try:
            advisor_result = self._advisor.generate(question, scope.evidence, audience)
        except (ChatProviderError, AdvisorError) as exc:
            trace_extra["error"] = str(exc)[:300]
            return self._finish_abstain(
                trace_id=trace_id,
                session_id=session_id,
                started=started,
                started_at=started_at,
                question=question,
                trace_extra=trace_extra,
                outcome=OutcomeCode.GENERATION_FAILED,
                message=_MESSAGES[OutcomeCode.GENERATION_FAILED],
                issues=scope.issues,
                scope_issues=scope.issues,
                queries=retrieval.queries,
                evidence=scope.evidence,
            )

        trace_extra["llm"] = self._llm_trace(advisor_result)
        report: VerifyReport = self._verifier.verify(
            advisor_result.drafts,
            scope.evidence,
            scope_issues=scope.issues,
        )
        verification = report.result
        trace_extra["claims_total"] = len(advisor_result.drafts)
        trace_extra["claims_kept"] = len(verification.claims)
        trace_extra["dropped_claims"] = report.check.dropped if report.check is not None else []
        trace_extra["dropped_citations"] = (
            report.check.dropped_citations if report.check is not None else []
        )
        trace_extra["verification"] = _verification_payload(verification, scope.issues)

        outcome = self._outcome_for(verification)
        if verification.decision is VerificationDecision.ABSTAIN:
            return self._finish(
                trace_id=trace_id,
                session_id=session_id,
                started=started,
                started_at=started_at,
                question=question,
                trace_extra=trace_extra,
                result=QAResult(
                    trace_id=trace_id,
                    outcome=outcome,
                    evidence=scope.evidence,
                    queries=retrieval.queries,
                    dropped_claims=trace_extra["dropped_claims"],
                    dropped_citations=trace_extra["dropped_citations"],
                    message=_MESSAGES.get(outcome, DISCLAIMER),
                    llm_model=(
                        advisor_result.responses[-1].model if advisor_result.responses else ""
                    ),
                    retrieval_scores=trace_extra["retrieval"],
                    decision=verification.decision,
                    issues=verification.issues,
                    scope_issues=scope.issues,
                    dropped_claim_ids=verification.dropped_claim_ids,
                    drafts=[_record_payload(record) for record in report.records],
                ),
            )

        partial = verification.decision is VerificationDecision.PARTIAL
        finished: FinalizeResult = finalize(
            verification.claims,
            partial=partial,
            dropped_count=len(verification.dropped_claim_ids),
        )
        return self._finish(
            trace_id=trace_id,
            session_id=session_id,
            started=started,
            started_at=started_at,
            question=question,
            trace_extra=trace_extra,
            result=QAResult(
                trace_id=trace_id,
                outcome=outcome,
                answer_markdown=finished.answer_markdown,
                claims=verification.claims,
                citations=finished.citations,
                evidence=scope.evidence,
                queries=retrieval.queries,
                dropped_claims=trace_extra["dropped_claims"],
                dropped_citations=trace_extra["dropped_citations"],
                message=PARTIAL_NOTICE if partial else "",
                llm_model=(advisor_result.responses[-1].model if advisor_result.responses else ""),
                retrieval_scores=trace_extra["retrieval"],
                decision=verification.decision,
                issues=verification.issues,
                scope_issues=scope.issues,
                dropped_claim_ids=verification.dropped_claim_ids,
                partial=partial,
                drafts=[_record_payload(record) for record in report.records],
                rendered_claim_ids=[claim.claim_id for claim in verification.claims],
            ),
        )

    def _probe(self, question: str, filters: PolicyFilters) -> dict[str, Any]:
        if not self._settings.enable_repealed_probe:
            return {"hit": False, "latency_ms": 0, "article_ids": []}
        probe_filters = filters.model_copy(
            update={
                "status": [ArticleStatus.REPEALED],
                "ignore_effective_dates": True,
            }
        )
        started = time.perf_counter()
        try:
            probe = self._retriever.retrieve(question, filters=probe_filters)
        except RetrievalError as exc:
            return {
                "hit": False,
                "latency_ms": int((time.perf_counter() - started) * 1000),
                "error": str(exc)[:300],
                "article_ids": [],
            }
        article_ids = [item.article.article_id for item in probe.evidence[:5]]
        return {
            "hit": bool(probe.evidence),
            "latency_ms": int((time.perf_counter() - started) * 1000),
            "article_ids": article_ids,
            "count": len(probe.evidence),
        }

    @staticmethod
    def _outcome_for(result: VerificationResult) -> OutcomeCode:
        if result.decision is VerificationDecision.PASS:
            return OutcomeCode.ANSWERED
        if result.decision is VerificationDecision.PARTIAL:
            return OutcomeCode.PARTIAL_ANSWER
        return result.reason_code or OutcomeCode.UNVERIFIABLE

    def _finish_abstain(
        self,
        *,
        trace_id: str,
        session_id: str,
        started: float,
        started_at: datetime,
        question: str,
        trace_extra: dict[str, Any],
        outcome: OutcomeCode,
        message: str,
        issues: list[VerificationIssue],
        scope_issues: list[VerificationIssue],
        queries: list[str] | None = None,
        evidence: list[Evidence] | None = None,
        probe_ms: int = 0,
    ) -> QAResult:
        verification = VerificationResult(
            decision=VerificationDecision.ABSTAIN,
            claims=[],
            issues=list(issues),
            dropped_claim_ids=[],
            reason_code=outcome,
        )
        trace_extra["verification"] = _verification_payload(verification, scope_issues)
        return self._finish(
            trace_id=trace_id,
            session_id=session_id,
            started=started,
            started_at=started_at,
            question=question,
            trace_extra=trace_extra,
            result=QAResult(
                trace_id=trace_id,
                outcome=outcome,
                evidence=evidence or [],
                queries=queries or [],
                message=message,
                decision=verification.decision,
                issues=verification.issues,
                scope_issues=scope_issues,
                probe_ms=probe_ms,
            ),
        )

    def _llm_trace(self, result: AdvisorResult) -> dict[str, Any]:
        return {
            "model": result.responses[-1].model if result.responses else "",
            "latency_ms": sum(response.latency_ms for response in result.responses),
            "prompt_tokens": sum(response.prompt_tokens for response in result.responses),
            "completion_tokens": sum(response.completion_tokens for response in result.responses),
            "repaired": result.repaired,
        }

    def _finish(
        self,
        *,
        trace_id: str,
        session_id: str,
        started: float,
        started_at: datetime,
        question: str,
        trace_extra: dict[str, Any],
        result: QAResult,
    ) -> QAResult:
        result.duration_ms = int((time.perf_counter() - started) * 1000)
        if not result.probe:
            result.probe = dict(trace_extra.get("repealed_probe", {}))
        payload = {
            "trace_id": trace_id,
            "session_id": session_id,
            "started_at": started_at.isoformat(timespec="seconds"),
            "duration_ms": result.duration_ms,
            "question_redacted": redact_sensitive(question),
            "outcome": result.outcome.value,
            "probe_ms": result.probe_ms,
            "qa_ms": result.qa_ms,
            **trace_extra,
        }
        if result.outcome is not OutcomeCode.ANSWERED:
            payload["message"] = result.message
        if self._trace_writer is not None:
            self._trace_writer.write(payload)
        if not result.message and not result.renders_answer:
            result.message = DISCLAIMER
        return result
