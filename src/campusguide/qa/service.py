"""W2 单轮问答线性编排。

guard -> normalize/alias -> filters -> dense retrieve -> evidence
      -> advisor claims -> validator -> finalize -> trace
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from typing import Any, ClassVar

from campusguide.agents.advisor import Advisor, AdvisorError, AdvisorResult
from campusguide.agents.providers import ChatProviderError
from campusguide.config import RetrievalConfig
from campusguide.domain.enums import (
    Audience,
    BlockType,
    OutcomeCode,
    VerificationDecision,
)
from campusguide.domain.models import Citation, Claim, Evidence, PolicyFilters
from campusguide.observability.redact import redact_sensitive
from campusguide.observability.trace import TraceWriter
from campusguide.qa.finalize import DISCLAIMER, FinalizeResult, finalize
from campusguide.qa.guard import MAX_QUERY_CHARS, check_guard
from campusguide.retrieval.normalize import normalize_query
from campusguide.retrieval.service import DenseRetriever, RetrievalError
from campusguide.verification.claims import ClaimValidator
from campusguide.verification.issues import VerificationIssue, VerificationResult
from campusguide.verification.verifier import DeterministicVerifier

_MESSAGES = {
    OutcomeCode.NO_EVIDENCE: (
        "没有检索到可引用的现行条款。请换一种说法，或补充具体事项（如“国家奖学金申请条件”）。"
    ),
    OutcomeCode.NO_VALID_CLAIM: (
        "检索到了材料，但模型给出的结论无法通过引用校验，因此不作答。请稍后重试或换一种问法。"
    ),
    OutcomeCode.GENERATION_FAILED: (
        "生成服务暂时不可用或输出不符合契约。请检查 LLM 配置后重试；系统不会在无引用情况下作答。"
    ),
    OutcomeCode.INDEX_UNAVAILABLE: (
        "向量索引不可用（Qdrant 未启动或索引未构建）。请先启动 Qdrant 并运行 build_vectors.py。"
    ),
    OutcomeCode.ONLY_REPEALED: (
        "该问题只命中已废止的文件版本，现行依据暂缺。请关注学校官网的文件更新，"
        "或直接咨询相关职能部门。"
    ),
    OutcomeCode.VERSION_CONFLICT: (
        "同一文件检索到多个现行版本且内容可能冲突，暂不作答。请稍后重试或查看学校官网原文。"
    ),
    OutcomeCode.VERSION_MISMATCH: (
        "检索到的条款不在现行有效期内，暂不作答。请确认事项的时间范围后重试。"
    ),
    OutcomeCode.UNVERIFIABLE: (
        "模型给出的结论均未通过引用核对，因此不作答。请换一种问法，或直接查看学校官网原文。"
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

    @classmethod
    def from_config(cls, config: RetrievalConfig) -> QASettings:
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
    partial: bool = False
    issues: list[VerificationIssue] = field(default_factory=list)
    verification: VerificationResult | None = None
    draft_claims: list[dict[str, Any]] = field(default_factory=list)
    message: str = ""
    duration_ms: int = 0
    probe_ms: int = 0
    llm_model: str = ""
    retrieval_scores: list[dict[str, Any]] = field(default_factory=list)

    @property
    def answered(self) -> bool:
        # PARTIAL_ANSWER 仍算“有答案”，但不计入“完整回答”指标
        return self.outcome in {OutcomeCode.ANSWERED, OutcomeCode.PARTIAL_ANSWER}


class QAService:
    def __init__(
        self,
        *,
        retriever: DenseRetriever,
        advisor: Advisor,
        settings: QASettings | None = None,
        validator: ClaimValidator | None = None,
        verifier: DeterministicVerifier | None = None,
        trace_writer: TraceWriter | None = None,
        today: date | None = None,
    ) -> None:
        self._retriever = retriever
        self._advisor = advisor
        self._settings = settings or QASettings()
        self._verifier = verifier or DeterministicVerifier(validator=validator)
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
        filters = PolicyFilters(
            block_types=list(block_types or self._settings.block_types),
            as_of=self._today or date.today(),
            audience=[audience] if audience else None,
        )
        trace_extra: dict[str, Any] = {
            "normalized_query": redact_sensitive(normalized),
            "filters": filters.model_dump(mode="json"),
        }

        guard = check_guard(question, max_chars=self._settings.max_query_chars)
        if not guard.allowed:
            return self._finish(
                trace_id=trace_id,
                session_id=session_id,
                started=started,
                started_at=started_at,
                question=question,
                trace_extra=trace_extra,
                result=QAResult(
                    trace_id=trace_id,
                    outcome=guard.outcome or OutcomeCode.INVALID_REQUEST,
                    message=guard.message,
                ),
            )

        try:
            retrieval = self._retriever.retrieve(question, filters=filters)
        except RetrievalError as exc:
            trace_extra["error"] = str(exc)[:300]
            return self._finish(
                trace_id=trace_id,
                session_id=session_id,
                started=started,
                started_at=started_at,
                question=question,
                trace_extra=trace_extra,
                result=QAResult(
                    trace_id=trace_id,
                    outcome=OutcomeCode.INDEX_UNAVAILABLE,
                    message=_MESSAGES[OutcomeCode.INDEX_UNAVAILABLE],
                ),
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
            probe_hit = False
            probe_latency_ms = 0
            probe = getattr(self._retriever, "probe_repealed", None)
            if probe is not None:
                probe_started = time.perf_counter()
                try:
                    probe_hit = bool(probe(question, filters=filters))
                except Exception:  # 探针失败按无命中处理
                    probe_hit = False
                probe_latency_ms = int((time.perf_counter() - probe_started) * 1000)
            trace_extra["repealed_probe"] = {"hit": probe_hit, "latency_ms": probe_latency_ms}
            outcome = OutcomeCode.ONLY_REPEALED if probe_hit else OutcomeCode.NO_EVIDENCE
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
                    queries=retrieval.queries,
                    message=_MESSAGES[outcome],
                    probe_ms=probe_latency_ms,
                    retrieval_scores=trace_extra["retrieval"],
                ),
            )

        as_of = filters.as_of
        scope = self._verifier.check_scope(evidence, audience, as_of)
        if scope.abstain:
            trace_extra["verification"] = {
                "decision": VerificationDecision.ABSTAIN.value,
                "issues": [issue.model_dump(mode="json") for issue in scope.issues],
                "dropped_claim_ids": [],
                "partial": False,
            }
            return self._finish(
                trace_id=trace_id,
                session_id=session_id,
                started=started,
                started_at=started_at,
                question=question,
                trace_extra=trace_extra,
                result=QAResult(
                    trace_id=trace_id,
                    outcome=scope.abstain_reason or OutcomeCode.UNVERIFIABLE,
                    queries=retrieval.queries,
                    evidence=evidence,
                    issues=scope.issues,
                    message=_MESSAGES[scope.abstain_reason or OutcomeCode.UNVERIFIABLE],
                    retrieval_scores=trace_extra["retrieval"],
                ),
            )

        try:
            advisor_result = self._advisor.generate(question, scope.evidence, audience)
        except (ChatProviderError, AdvisorError) as exc:
            trace_extra["error"] = str(exc)[:300]
            return self._finish(
                trace_id=trace_id,
                session_id=session_id,
                started=started,
                started_at=started_at,
                question=question,
                trace_extra=trace_extra,
                result=QAResult(
                    trace_id=trace_id,
                    outcome=OutcomeCode.GENERATION_FAILED,
                    queries=retrieval.queries,
                    evidence=scope.evidence,
                    message=_MESSAGES[OutcomeCode.GENERATION_FAILED],
                ),
            )

        trace_extra["llm"] = self._llm_trace(advisor_result)
        verification = self._verifier.verify(advisor_result.drafts, scope.evidence, audience, as_of)
        merged_issues = [*scope.issues, *verification.issues]
        trace_extra["claims_total"] = len(advisor_result.drafts)
        trace_extra["claims_kept"] = len(verification.claims)
        trace_extra["verification"] = {
            "decision": verification.decision.value,
            "issues": [issue.model_dump(mode="json") for issue in verification.issues],
            "dropped_claim_ids": verification.dropped_claim_ids,
            "partial": verification.partial,
        }
        dropped_claims = self._claim_drops(merged_issues)
        dropped_citations = self._citation_drops(merged_issues)
        draft_claims = [
            {
                "claim_id": draft.claim_id or "",
                "text": draft.text,
                "citations": [
                    {"article_id": citation.article_id, "quote": citation.quote}
                    for citation in draft.citations
                ],
            }
            for draft in advisor_result.drafts
        ]

        if verification.decision is VerificationDecision.ABSTAIN:
            outcome = verification.reason_code or OutcomeCode.UNVERIFIABLE
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
                    queries=retrieval.queries,
                    evidence=scope.evidence,
                    dropped_claims=dropped_claims,
                    dropped_citations=dropped_citations,
                    issues=merged_issues,
                    verification=verification,
                    draft_claims=draft_claims,
                    message=_MESSAGES.get(outcome, ""),
                    retrieval_scores=trace_extra["retrieval"],
                ),
            )

        partial = verification.decision is VerificationDecision.PARTIAL
        finished: FinalizeResult = finalize(verification.claims, partial=partial)
        outcome = OutcomeCode.PARTIAL_ANSWER if partial else OutcomeCode.ANSWERED
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
                dropped_claims=dropped_claims,
                dropped_citations=dropped_citations,
                partial=partial,
                issues=merged_issues,
                verification=verification,
                draft_claims=draft_claims,
                llm_model=advisor_result.responses[-1].model if advisor_result.responses else "",
                retrieval_scores=trace_extra["retrieval"],
            ),
        )

    _CITATION_DROP_CODES: ClassVar[set[str]] = {
        "QUOTE_TOO_SHORT",
        "QUOTE_ELLIPSIS",
        "QUOTE_MISMATCH",
        "CITATION_NOT_IN_EVIDENCE",
    }
    _CLAIM_DROP_CODES: ClassVar[set[str]] = {
        "MISSING_CITATION",
        "EMPTY_CLAIM_TEXT",
        "DUPLICATE_CLAIM_ID",
        "TOO_MANY_CLAIMS",
        "NUMBER_NOT_SUPPORTED",
        "PRIVACY_VIOLATION",
    }

    @staticmethod
    def _claim_drops(issues: list[VerificationIssue]) -> list[dict[str, str]]:
        return [
            {"claim_id": issue.claim_id or "", "reason": issue.code, "detail": issue.detail}
            for issue in issues
            if issue.code in QAService._CLAIM_DROP_CODES
        ]

    @staticmethod
    def _citation_drops(issues: list[VerificationIssue]) -> list[dict[str, str]]:
        return [
            {
                "claim_id": issue.claim_id or "",
                "article_id": issue.article_id or "",
                "reason": issue.code,
            }
            for issue in issues
            if issue.code in QAService._CITATION_DROP_CODES
        ]

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
        payload = {
            "trace_id": trace_id,
            "session_id": session_id,
            "started_at": started_at.isoformat(timespec="seconds"),
            "duration_ms": result.duration_ms,
            "question_redacted": redact_sensitive(question),
            "outcome": result.outcome.value,
            **trace_extra,
        }
        if result.outcome is not OutcomeCode.ANSWERED:
            payload["message"] = result.message
        if self._trace_writer is not None:
            self._trace_writer.write(payload)
        result.message = result.message or ("" if result.answered else DISCLAIMER)
        return result
