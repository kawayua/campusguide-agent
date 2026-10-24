"""W4 单轮问答线性编排。

guard -> MCP search -> （空结果）废止探针 -> scope -> advisor
      -> deterministic verifier -> finalize -> trace

W3 的核对逻辑不变；W4 只把检索换成 ``SearchClient``（生产为 policy-search
MCP，测试为 fake/legacy adapter），并增加 profile 与 index_version 校验。
"""

from __future__ import annotations

import contextlib
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
    Article,
    ArticlePublic,
    Citation,
    Claim,
    Evidence,
    PolicyFilters,
    VerificationIssue,
    VerificationResult,
)
from campusguide.domain.profiles import UserProfile
from campusguide.mcp.client import SearchClient, SearchResponse
from campusguide.mcp.schemas import (
    ErrorCode,
    SearchArticlesData,
    SearchHit,
    ToolError,
    policy_filters_from_request,
    search_request_from_filters,
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
    OutcomeCode.TOOL_UNAVAILABLE: (
        "检索工具不可用（MCP server 未启动、超时或重排模型不可用）。系统不会在无检索证据时作答。"
    ),
}


class _RetrieverSearchClient:
    """测试/离线兼容：把旧 ``DenseRetriever`` 包装成 SearchClient。

    生产路径不使用该类；QAService 本身仍只依赖 SearchClient 协议。
    """

    def __init__(self, retriever: Any) -> None:
        self._retriever = retriever

    def warmup(self) -> None:
        warmup = getattr(self._retriever, "warmup", None)
        if warmup is not None:
            warmup()

    def search_articles(
        self,
        request: Any,
        *,
        timeout: float | None = None,
    ) -> SearchResponse:
        from campusguide.retrieval.service import RetrievalError

        filters = policy_filters_from_request(request)
        try:
            result: Any = self._retriever.retrieve(
                request.query,
                filters=filters,
            )
        except RetrievalError as exc:
            raise ToolError(ErrorCode.INDEX_UNAVAILABLE, str(exc)) from exc
        hits = [
            SearchHit(
                article_id=item.article.article_id,
                part_no=item.article.part_no,
                block_type=item.article.block_type.value,
                article=ArticlePublic.from_article(item.article),
                dense_score=item.dense_score,
                sparse_score=item.sparse_score,
                rrf_score=item.rrf_score,
                rerank_score=item.rerank_score,
                matched_spans=list(item.matched_spans),
                retrieval_query=item.retrieval_query,
                rank=item.rank,
            )
            for item in result.evidence
        ]
        data = SearchArticlesData(
            query=request.query,
            queries=list(result.queries),
            articles=hits,
            index_version="",
            candidates=result.candidates,
            warnings=list(result.notes),
        )
        return SearchResponse(data=data, server="legacy-retriever", latency_ms=0)

    def get_article(self, article_id: str, part_no: int | None = None) -> Any:
        raise NotImplementedError("legacy retriever 不支持 get_article")


def _evidence_from_hits(data: SearchArticlesData) -> list[Evidence]:
    evidence: list[Evidence] = []
    for hit in data.articles:
        evidence.append(
            Evidence(
                article=_article_from_public(hit.article),
                dense_score=hit.dense_score,
                sparse_score=hit.sparse_score,
                rrf_score=hit.rrf_score,
                rerank_score=hit.rerank_score,
                matched_spans=list(hit.matched_spans),
                retrieval_query=hit.retrieval_query,
                rank=hit.rank,
            )
        )
    return evidence


def _article_from_public(public: ArticlePublic) -> Article:
    return Article.model_validate(public.model_dump())


@dataclass
class QASettings:
    max_query_chars: int = MAX_QUERY_CHARS
    top_k: int = 30
    rerank_top_n: int = 8
    max_evidence: int = 8
    max_chars_per_evidence: int = 800
    min_dense_score: float = 0.55
    token_budget: int = 4000
    block_types: list[BlockType] = field(default_factory=lambda: [BlockType.ARTICLE])
    min_quote_chars: int = 6
    max_claims: int = 5
    enable_repealed_probe: bool = True
    expected_index_version: str = ""

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
            top_k=config.recall_top_k,
            rerank_top_n=config.rerank_top_n,
            max_evidence=config.max_articles,
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
    index_version: str = ""
    mcp: dict[str, Any] = field(default_factory=dict)
    retrieval_timing: dict[str, float] = field(default_factory=dict)
    retrieval_warnings: list[str] = field(default_factory=list)
    version_metadata: dict[str, Any] = field(default_factory=dict)
    quote_repair: dict[str, Any] = field(default_factory=dict)

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
        search_client: SearchClient | None = None,
        retriever: Any | None = None,
        advisor: Advisor,
        settings: QASettings | None = None,
        validator: ClaimValidator | None = None,
        verifier: Verifier | None = None,
        trace_writer: TraceWriter | None = None,
        meta_client: Any | None = None,
        today: date | None = None,
    ) -> None:
        if search_client is None:
            if retriever is None:
                raise ValueError("必须提供 search_client（生产）或 retriever（测试/离线）")
            search_client = _RetrieverSearchClient(retriever)
        self._search_client = search_client
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
        self.meta_client = meta_client

    def warmup(self) -> None:
        """预加载检索模型 / 启动 MCP server；不产生 trace，也不调用 LLM。"""
        warmup = getattr(self._search_client, "warmup", None)
        if callable(warmup):
            warmup()
        else:
            start = getattr(self._search_client, "start", None)
            if callable(start):
                start()

    def close(self) -> None:
        for client in (self._search_client, self.meta_client):
            close = getattr(client, "close", None)
            if callable(close):
                with contextlib.suppress(Exception):
                    close()

    def ask(
        self,
        question: str,
        *,
        audience: Audience | None = None,
        profile: UserProfile | None = None,
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
            profile=profile,
        )
        trace_extra: dict[str, Any] = {
            "normalized_query": redact_sensitive(normalized),
            "filters": filters.model_dump(mode="json"),
            "index_version": self._settings.expected_index_version,
            "mcp": {},
            "retrieval_timing": {},
            "retrieval_warnings": [],
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
            response = self._search(question, filters)
        except ToolError as exc:
            trace_extra["error"] = exc.message[:300]
            trace_extra["mcp"] = {
                "error_code": exc.code.value,
                "retryable": exc.retryable,
                "details": exc.details,
            }
            outcome = self._outcome_for_tool_error(exc)
            return self._finish_abstain(
                trace_id=trace_id,
                session_id=session_id,
                started=started,
                started_at=started_at,
                question=question,
                trace_extra=trace_extra,
                outcome=outcome,
                message=_MESSAGES.get(outcome, DISCLAIMER),
                issues=[],
                scope_issues=[],
            )

        data = response.data
        trace_extra["mcp"] = {
            "server": response.server,
            "tool": response.tool,
            "latency_ms": response.latency_ms,
            "retries": response.retries,
            "error_code": response.error_code,
        }
        trace_extra["index_version"] = data.index_version
        trace_extra["retrieval_timing"] = dict(data.timing)
        trace_extra["retrieval_warnings"] = list(data.warnings)
        expected = self._settings.expected_index_version
        if expected and data.index_version != expected:
            trace_extra["error"] = (
                f"index_version 不一致：server={data.index_version!r}, local={expected!r}"
            )
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

        evidence = _evidence_from_hits(data)
        trace_extra["queries"] = [redact_sensitive(query) for query in data.queries]
        trace_extra["retrieval"] = [
            {
                "article_id": item.article.article_id,
                "part_no": item.article.part_no,
                "score": round(item.score, 4),
                "dense_score": item.dense_score,
                "sparse_score": item.sparse_score,
                "rrf_score": item.rrf_score,
                "rerank_score": item.rerank_score,
                "block_type": item.article.block_type.value,
                "rank": item.rank,
                "retrieval_query": redact_sensitive(item.retrieval_query),
            }
            for item in evidence
        ]
        trace_extra["candidates"] = data.candidates

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
                queries=data.queries,
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
                queries=data.queries,
            )

        trace_extra["version_metadata"] = self._version_metadata(scope.evidence)

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
                queries=data.queries,
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
                    queries=data.queries,
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
                queries=data.queries,
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

    def _version_metadata(self, evidence: list[Evidence]) -> dict[str, Any]:
        """通过 policy-meta 读取最新版本的元数据，只进入 trace，不参与核对。"""
        if self.meta_client is None or not evidence:
            return {}
        doc_ids = list(dict.fromkeys(item.article.doc_id for item in evidence))[:3]
        metadata: dict[str, Any] = {}
        for doc_id in doc_ids:
            try:
                versions = self.meta_client.get_document_versions(doc_id)
            except ToolError as exc:
                metadata[doc_id] = {"error_code": exc.code.value}
                continue
            except Exception as exc:
                metadata[doc_id] = {"error": type(exc).__name__}
                continue
            items = versions.get("versions") or []
            metadata[doc_id] = {
                "version_count": len(items),
                "versions": [
                    {
                        "version_id": item.get("version_id"),
                        "status": item.get("status"),
                        "effective_from": item.get("effective_from"),
                        "effective_to": item.get("effective_to"),
                        "date_confidence": item.get("date_confidence"),
                    }
                    for item in items
                ],
            }
        return metadata

    def _search(
        self,
        question: str,
        filters: PolicyFilters,
        *,
        rerank_top_n: int | None = None,
    ) -> SearchResponse:
        request = search_request_from_filters(
            question,
            filters,
            top_k=self._settings.top_k,
            rerank_top_n=(
                rerank_top_n if rerank_top_n is not None else self._settings.rerank_top_n
            ),
        )
        return self._search_client.search_articles(request)

    @staticmethod
    def _outcome_for_tool_error(exc: ToolError) -> OutcomeCode:
        if exc.code is ErrorCode.CONFLICT:
            return OutcomeCode.VERSION_CONFLICT
        if exc.code is ErrorCode.INDEX_UNAVAILABLE:
            return OutcomeCode.INDEX_UNAVAILABLE
        if exc.code in {ErrorCode.INVALID_FILTER, ErrorCode.INVALID_ARGUMENT}:
            return OutcomeCode.INVALID_REQUEST
        return OutcomeCode.TOOL_UNAVAILABLE

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
            response = self._search(question, probe_filters, rerank_top_n=1)
        except ToolError as exc:
            return {
                "hit": False,
                "latency_ms": int((time.perf_counter() - started) * 1000),
                "error": exc.message[:300],
                "error_code": exc.code.value,
                "article_ids": [],
            }
        article_ids = [hit.article_id for hit in response.data.articles[:5]]
        return {
            "hit": bool(response.data.articles),
            "latency_ms": int((time.perf_counter() - started) * 1000),
            "article_ids": article_ids,
            "count": len(response.data.articles),
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
            "quote_repair_attempted": result.quote_repair_attempted,
            "quote_repaired": result.quote_repaired,
            "quote_repair_remaining": list(result.quote_repair_remaining),
            "quote_repair_error": result.quote_repair_error,
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
        result.index_version = result.index_version or str(trace_extra.get("index_version", ""))
        result.mcp = result.mcp or dict(trace_extra.get("mcp") or {})
        result.retrieval_timing = result.retrieval_timing or dict(
            trace_extra.get("retrieval_timing") or {}
        )
        result.retrieval_warnings = result.retrieval_warnings or list(
            trace_extra.get("retrieval_warnings") or []
        )
        result.version_metadata = result.version_metadata or dict(
            trace_extra.get("version_metadata") or {}
        )
        result.quote_repair = result.quote_repair or {
            key: (trace_extra.get("llm") or {}).get(key)
            for key in (
                "quote_repair_attempted",
                "quote_repaired",
                "quote_repair_remaining",
                "quote_repair_error",
            )
            if key in (trace_extra.get("llm") or {})
        }
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
