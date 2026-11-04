"""W6 图 Retriever：多查询、合并去重、coverage、流程 citation 水合。

一个用户问题最多发 ``max_queries`` 次检索；按 ``(article_id, part_no)`` 合并，
每个子查询至少保留 1 条最高分证据，再取全局 top N。流程 YAML 的 citation
通过 ``policy-search.get_article`` 水合，避免 Advisor 引用流程依据时 H1 误判。
"""

from __future__ import annotations

import contextlib
import re
import time
from dataclasses import dataclass, field

from campusguide.agents.plan import Plan, QuestionUnit
from campusguide.domain.enums import (
    ArticleStatus,
    Audience,
    OutcomeCode,
    VersionMode,
)
from campusguide.domain.models import (
    Article,
    ArticlePublic,
    Evidence,
    PolicyFilters,
    VerificationIssue,
)
from campusguide.domain.profiles import UserProfile
from campusguide.domain.text import normalize_text
from campusguide.mcp.client import SearchClient, SearchResponse
from campusguide.mcp.schemas import (
    ErrorCode,
    SearchArticlesData,
    ToolError,
    search_request_from_filters,
)
from campusguide.observability.errors import safe_error_summary
from campusguide.retrieval.budget import evidence_cost
from campusguide.retrieval.profile_expand import ProfileLexicon
from campusguide.verification.scope import apply_scope

FLOW_CITATION_UNAVAILABLE = "FLOW_CITATION_UNAVAILABLE"
PARTIAL_RETRIEVAL = "PARTIAL_RETRIEVAL"
ORIGINAL_QUERY_FALLBACK_USED = "ORIGINAL_QUERY_FALLBACK_USED"
ORIGINAL_QUERY_FALLBACK_EMPTY = "ORIGINAL_QUERY_FALLBACK_EMPTY"
ORIGINAL_QUERY_FALLBACK_FAILED = "ORIGINAL_QUERY_FALLBACK_FAILED"
ORIGINAL_QUERY_FALLBACK_SKIPPED = "ORIGINAL_QUERY_FALLBACK_SKIPPED"
ORIGINAL_QUERY_FALLBACK_REJECTED = "ORIGINAL_QUERY_FALLBACK_REJECTED"


@dataclass
class RetrievalBundle:
    evidence: list[Evidence] = field(default_factory=list)
    coverage: dict[str, list[str]] = field(default_factory=dict)
    queries: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    issues: list[VerificationIssue] = field(default_factory=list)
    index_version: str = ""
    timing: dict[str, float] = field(default_factory=dict)
    partial: bool = False
    probe: dict = field(default_factory=dict)
    fallback: dict = field(default_factory=dict)
    reason_code: OutcomeCode | None = None
    flowed: bool = False

    @property
    def has_search_evidence(self) -> bool:
        return any(item.source == "search" for item in self.evidence)


def _article_from_public(public: ArticlePublic) -> Article:
    return Article.model_validate(public.model_dump())


def _evidence_from_data(data: SearchArticlesData, query_id: str) -> list[Evidence]:
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
                matched_queries=[query_id],
                source="search",
                structured=hit.structured,
                structured_rank=hit.structured_rank,
            )
        )
    return evidence


def _filters_for_query(base: PolicyFilters, query_filters: dict) -> PolicyFilters:
    updates: dict = {}
    audience_raw = query_filters.get("audience")
    if isinstance(audience_raw, list) and audience_raw:
        with contextlib.suppress(ValueError):
            updates["audience"] = [Audience(str(item)) for item in audience_raw]
    status_raw = query_filters.get("status")
    if isinstance(status_raw, list) and status_raw:
        with contextlib.suppress(ValueError):
            updates["status"] = [ArticleStatus(str(item)) for item in status_raw]
    version_raw = query_filters.get("version_mode")
    if isinstance(version_raw, str):
        with contextlib.suppress(ValueError):
            updates["version_mode"] = VersionMode(version_raw)
    profile = query_filters.get("profile")
    if isinstance(profile, dict):
        base_profile = base.profile.model_dump() if base.profile is not None else {}
        # Query objects can differ from the speaker's college/major. Do not
        # combine a newly named major with an unrelated inherited college.
        if profile.get("majors") and "colleges" not in profile:
            base_profile["colleges"] = []
        elif profile.get("colleges") and "majors" not in profile:
            base_profile["majors"] = []
        updates["profile"] = UserProfile.model_validate({**base_profile, **profile})
    doc_ids = query_filters.get("doc_ids")
    if isinstance(doc_ids, list) and doc_ids:
        updates["doc_ids"] = [str(item) for item in doc_ids]
    flow_ids = query_filters.get("flow_ids")
    if isinstance(flow_ids, list) and flow_ids:
        updates["flow_ids"] = [str(item) for item in flow_ids]
    return base.model_copy(update=updates)


def _coverage_id(evidence: Evidence) -> str:
    article = evidence.article
    if article.part_no > 1:
        return f"{article.article_id}#p{article.part_no}"
    return article.article_id


def _normalized_query(text: str) -> str:
    return normalize_text(text or "").casefold()


_YEAR_SCOPE = re.compile(r"((?:20\d{2}\s*(?:[、,，和与及—–-]\s*)?)+)\s*(?:版|级)")
_RAW_YEAR = re.compile(r"20\d{2}")


def _years_in(text: str) -> set[int]:
    years: set[int] = set()
    for match in _YEAR_SCOPE.finditer(text or ""):
        years.update(int(year) for year in _RAW_YEAR.findall(match.group(1)))
    return years


def _unit_version_years(unit: QuestionUnit) -> set[int]:
    """Plan version only; intake cohort must not be treated as a plan year."""
    if not unit.version:
        return set()
    years = _years_in(unit.version)
    if not years:
        years = {int(year) for year in _RAW_YEAR.findall(unit.version)}
    return years


def _article_version_years(article: Article) -> set[int]:
    years = {int(year) for year in _RAW_YEAR.findall(article.version_id or "")}
    return years or _years_in(article.doc_title)


def _class_scope_ok(
    unit: QuestionUnit,
    context: str,
    class_terms: dict[str, tuple[str, ...]] | None,
) -> bool:
    if not unit.class_type or not class_terms:
        return True
    own = set(class_terms.get(unit.class_type, ()))
    if not own:
        return True
    if any(term in context for term in own):
        return True
    other = {
        term
        for class_id, terms in class_terms.items()
        if class_id != unit.class_type
        for term in terms
    }
    if any(term in context for term in other):
        return False
    return "普通" in " ".join(own)


@dataclass
class _Candidate:
    evidence: Evidence
    queries: set[str]


@dataclass
class _OriginalFallback:
    diagnostics: dict
    target_query_ids: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    hits: list[Evidence] = field(default_factory=list)
    data_warnings: list[str] = field(default_factory=list)
    index_version: str = ""
    error: ToolError | None = None


def _skip_original_fallback(reason: str, target_ids: list[str]) -> _OriginalFallback:
    return _OriginalFallback(
        diagnostics={
            "requested": True,
            "attempted": False,
            "used": False,
            "reason": reason,
            "source_query": "",
            "target_query_ids": list(target_ids),
            "hits": 0,
            "latency_ms": 0.0,
            "index_version": "",
            "error_code": "",
        },
        target_query_ids=list(target_ids),
        warnings=[f"{ORIGINAL_QUERY_FALLBACK_SKIPPED}:{reason}"],
    )


class GraphRetriever:
    def __init__(
        self,
        search_client: SearchClient,
        *,
        top_k: int = 30,
        rerank_top_n: int = 8,
        max_evidence: int = 8,
        max_per_doc: int = 3,
        token_budget: int = 4000,
        expected_index_version: str = "",
        enable_repealed_probe: bool = True,
        profile_lexicon: ProfileLexicon | None = None,
    ) -> None:
        self._search_client = search_client
        self._top_k = top_k
        self._rerank_top_n = rerank_top_n
        self._max_evidence = max(1, max_evidence)
        self._max_per_doc = max(1, max_per_doc)
        self._token_budget = max(500, token_budget)
        self._expected_index_version = expected_index_version
        self._enable_repealed_probe = enable_repealed_probe
        self._class_terms = profile_lexicon.class_terms if profile_lexicon else None

    def warmup(self) -> None:
        warmup = getattr(self._search_client, "warmup", None)
        if callable(warmup):
            warmup()

    @staticmethod
    def _merge_hits(
        candidates: dict[tuple[str, int], _Candidate],
        query_scores: dict[str, dict[tuple[str, int], float]],
        hits: list[Evidence],
        *,
        score_id: str,
        provenance: list[str],
    ) -> None:
        if not hits:
            return
        scores = query_scores.setdefault(score_id, {})
        query_ids = set(provenance)
        for hit in hits:
            hit.matched_queries = sorted(set(hit.matched_queries) | query_ids)
            key = hit.key
            score = hit.score
            scores[key] = max(scores.get(key, score), score)
            existing = candidates.get(key)
            if existing is None:
                candidates[key] = _Candidate(evidence=hit, queries=set(query_ids))
                continue
            # Structure provenance is query-independent: a higher semantic
            # score from another query must not erase a structural hit.
            structured = existing.evidence.structured or hit.structured
            ranks = [
                rank
                for rank in (
                    existing.evidence.structured_rank,
                    hit.structured_rank,
                )
                if rank is not None
            ]
            structured_rank = min(ranks) if ranks else None
            matched_queries = sorted(set(existing.evidence.matched_queries) | query_ids)
            if score > existing.evidence.score:
                merged = hit.model_copy(
                    update={
                        "matched_queries": matched_queries,
                        "structured": structured,
                        "structured_rank": structured_rank,
                    }
                )
                candidates[key] = _Candidate(
                    evidence=merged, queries=existing.queries | query_ids
                )
            else:
                existing.queries.update(query_ids)
                existing.evidence.matched_queries = matched_queries
                existing.evidence.structured = structured
                existing.evidence.structured_rank = structured_rank

    def _fallback_to_original(
        self,
        plan: Plan,
        base_filters: PolicyFilters,
        search_queries: list[str] | None,
        *,
        empty_query_ids: list[str],
        failures: list[tuple[str, ToolError]],
        has_candidates: bool,
    ) -> _OriginalFallback | None:
        if not search_queries or has_candidates:
            return None
        if failures:
            return _skip_original_fallback("PRIMARY_FAILURE", empty_query_ids)
        if not empty_query_ids:
            return None

        filters_by_query = {
            item.query_id: _filters_for_query(base_filters, item.filters)
            for item in plan.queries
        }
        contexts = list(filters_by_query.values())
        if any(context != contexts[0] for context in contexts[1:]):
            return _skip_original_fallback("AMBIGUOUS_FILTERS", empty_query_ids)

        primary_queries = {_normalized_query(item.text) for item in plan.queries}
        original = next(
            (
                query
                for query in search_queries
                if _normalized_query(query)
                and _normalized_query(query) not in primary_queries
            ),
            None,
        )
        if original is None:
            reason = (
                "EMPTY_ORIGINAL"
                if all(not _normalized_query(query) for query in search_queries)
                else "IDENTICAL_QUERY"
            )
            return _skip_original_fallback(reason, empty_query_ids)

        target_ids = list(
            dict.fromkeys(
                query_id
                for query_id in empty_query_ids
                if query_id in filters_by_query
            )
        )
        if not target_ids:
            return _skip_original_fallback("NO_TARGET_QUERY", [])

        started = time.perf_counter()
        try:
            response = self._search_with_retry(original, filters_by_query[target_ids[0]])
        except ToolError as exc:
            latency = round((time.perf_counter() - started) * 1000, 1)
            return _OriginalFallback(
                diagnostics={
                    "requested": True,
                    "attempted": True,
                    "used": False,
                    "reason": "ERROR",
                    "source_query": original,
                    "target_query_ids": list(target_ids),
                    "hits": 0,
                    "latency_ms": latency,
                    "index_version": "",
                    "error_code": exc.code.value,
                },
                target_query_ids=list(target_ids),
                warnings=[
                    f"{ORIGINAL_QUERY_FALLBACK_FAILED}:{exc.code.value}:"
                    f"{safe_error_summary(exc)}"
                ],
                error=exc,
            )

        latency = round((time.perf_counter() - started) * 1000, 1)
        data = response.data
        hits = _evidence_from_data(data, target_ids[0])
        for hit in hits:
            hit.matched_queries = sorted(set(target_ids))
        used = bool(hits)
        return _OriginalFallback(
            diagnostics={
                "requested": True,
                "attempted": True,
                "used": used,
                "reason": "USED" if used else "EMPTY_RESULT",
                "source_query": original,
                "target_query_ids": list(target_ids),
                "hits": len(hits),
                "latency_ms": latency,
                "index_version": data.index_version,
                "error_code": "",
            },
            target_query_ids=list(target_ids),
            warnings=[
                (
                    f"{ORIGINAL_QUERY_FALLBACK_USED}:hits={len(hits)}:"
                    f"latency_ms={latency}"
                    if used
                    else f"{ORIGINAL_QUERY_FALLBACK_EMPTY}:latency_ms={latency}"
                )
            ],
            hits=hits,
            data_warnings=list(data.warnings),
            index_version=data.index_version,
        )

    def retrieve(
        self,
        plan: Plan,
        base_filters: PolicyFilters,
        *,
        flow_citations: list[str] | None = None,
        search_queries: list[str] | None = None,
    ) -> RetrievalBundle:
        bundle = RetrievalBundle(queries=[item.text for item in plan.queries])
        candidates: dict[tuple[str, int], _Candidate] = {}
        query_scores: dict[str, dict[tuple[str, int], float]] = {}
        data_warnings: list[str] = []
        index_versions: set[str] = set()
        failures: list[tuple[str, ToolError]] = []
        empty_queries: list[str] = []
        timing: dict[str, float] = {}

        if not plan.queries:
            return bundle

        for item in plan.queries:
            started = time.perf_counter()
            filters = _filters_for_query(base_filters, item.filters)
            try:
                response = self._search_with_retry(item.text, filters)
            except ToolError as exc:
                failures.append((item.query_id, exc))
                bundle.warnings.append(
                    f"RETRIEVAL_ERROR:{item.query_id}:{exc.code.value}:{safe_error_summary(exc)}"
                )
                bundle.coverage[item.query_id] = []
                timing[item.query_id] = round((time.perf_counter() - started) * 1000, 1)
                continue
            timing[item.query_id] = round((time.perf_counter() - started) * 1000, 1)
            data = response.data
            if data.index_version:
                index_versions.add(data.index_version)
            data_warnings.extend(data.warnings)
            hits = _evidence_from_data(data, item.query_id)
            if not hits:
                empty_queries.append(item.query_id)
                bundle.coverage[item.query_id] = []
                continue
            self._merge_hits(
                candidates,
                query_scores,
                hits,
                score_id=item.query_id,
                provenance=[item.query_id],
            )

        def check_index() -> bool:
            if not index_versions:
                return True
            versions = sorted(index_versions)
            bundle.index_version = versions[0]
            # Any disagreement between responses is fatal, even when the
            # lexicographically first value happens to match the manifest.
            conflict = len(versions) > 1
            expected_mismatch = bool(self._expected_index_version) and (
                versions[0] != self._expected_index_version
            )
            if not conflict and not expected_mismatch:
                return True
            bundle.reason_code = OutcomeCode.INDEX_UNAVAILABLE
            warning = (
                f"index_version 不一致：server={versions!r}"
                if conflict
                else f"index_version 不一致：server={versions[0]!r}"
            )
            if self._expected_index_version:
                warning += f", local={self._expected_index_version!r}"
            bundle.warnings.append(warning)
            bundle.evidence = []
            return False

        bundle.timing = timing
        bundle.warnings.extend(data_warnings)
        if not check_index():
            return bundle

        fallback_outcome = self._fallback_to_original(
            plan,
            base_filters,
            search_queries,
            empty_query_ids=empty_queries,
            failures=failures,
            has_candidates=bool(candidates),
        )
        fallback_error: ToolError | None = None
        if fallback_outcome is not None:
            bundle.fallback = fallback_outcome.diagnostics
            bundle.warnings.extend(fallback_outcome.warnings)
            bundle.warnings.extend(fallback_outcome.data_warnings)
            if fallback_outcome.index_version:
                index_versions.add(fallback_outcome.index_version)
            if fallback_outcome.hits:
                self._merge_hits(
                    candidates,
                    query_scores,
                    fallback_outcome.hits,
                    score_id=fallback_outcome.target_query_ids[0],
                    provenance=fallback_outcome.target_query_ids,
                )
            fallback_error = fallback_outcome.error
            if not check_index():
                bundle.fallback.update(
                    {"attempted": True, "used": False, "reason": "INDEX_MISMATCH"}
                )
                bundle.warnings.append(
                    f"{ORIGINAL_QUERY_FALLBACK_REJECTED}:INDEX_VERSION"
                )
                return bundle

        if failures and len(failures) == len(plan.queries) and not candidates:
            bundle.reason_code = self._error_outcome(failures[0][1])
            bundle.warnings.extend(f"{query_id}:{exc.code.value}" for query_id, exc in failures)
            return bundle
        if not candidates:
            probe = self._probe(plan, base_filters)
            bundle.probe = probe
            bundle.reason_code = (
                OutcomeCode.ONLY_REPEALED if probe.get("hit") else OutcomeCode.NO_EVIDENCE
            )
            if fallback_error is not None:
                bundle.reason_code = self._error_outcome(fallback_error)
            if failures:
                bundle.reason_code = self._error_outcome(failures[0][1])
            bundle.partial = bool(failures)
            return bundle
        recovered_ids = (
            set(bundle.fallback.get("target_query_ids") or [])
            if bundle.fallback.get("used")
            else set()
        )
        if failures or any(query_id not in recovered_ids for query_id in empty_queries):
            bundle.partial = True
            bundle.warnings.append(PARTIAL_RETRIEVAL)
            for query_id, exc in failures:
                bundle.warnings.append(f"{query_id}:{exc.code.value}")

        selected = self._select(candidates, query_scores, plan=plan, diagnostics=bundle.warnings)

        # 流程 citation 水合：不依赖检索命中，仍参与 H1/H3/H5/H6。
        hydrated, hydration_warnings = self._hydrate(flow_citations or [], selected)
        bundle.warnings.extend(hydration_warnings)
        selected.extend(hydrated)
        bundle.flowed = bool(hydrated)

        if selected:
            scope = apply_scope(
                selected,
                audience=self._audience(base_filters),
                as_of=base_filters.as_of,
                profile=base_filters.profile,
                academic_year=base_filters.academic_year,
                admission_year=base_filters.admission_year,
                graduating_year=base_filters.graduating_year,
            )
            bundle.evidence = scope.evidence
            bundle.issues = scope.issues
            if not scope.evidence:
                bundle.reason_code = scope.reason_code or OutcomeCode.UNVERIFIABLE
            elif scope.reason_code is not None:
                bundle.reason_code = scope.reason_code
        for item in bundle.evidence:
            for query_id in item.matched_queries:
                ids = bundle.coverage.setdefault(query_id, [])
                cov_id = _coverage_id(item)
                if cov_id not in ids:
                    ids.append(cov_id)
        return bundle

    # ------------------------------------------------------------------ search
    def _search_with_retry(self, query: str, filters: PolicyFilters) -> SearchResponse:
        request = search_request_from_filters(
            query,
            filters,
            top_k=self._top_k,
            rerank_top_n=self._rerank_top_n,
        )
        try:
            return self._search_client.search_articles(request)
        except ToolError as exc:
            if not exc.retryable and exc.code not in {
                ErrorCode.TOOL_UNAVAILABLE,
                ErrorCode.RATE_LIMITED,
                ErrorCode.RERANK_UNAVAILABLE,
            }:
                raise
            return self._search_client.search_articles(request)

    @staticmethod
    def _error_outcome(exc: ToolError) -> OutcomeCode:
        if exc.code is ErrorCode.INDEX_UNAVAILABLE:
            return OutcomeCode.INDEX_UNAVAILABLE
        if exc.code is ErrorCode.CONFLICT:
            return OutcomeCode.VERSION_CONFLICT
        return OutcomeCode.TOOL_UNAVAILABLE

    def _probe(self, plan: Plan, base_filters: PolicyFilters) -> dict:
        if not self._enable_repealed_probe or not plan.queries:
            return {"hit": False, "latency_ms": 0, "article_ids": []}
        filters = base_filters.model_copy(
            update={
                "status": [ArticleStatus.REPEALED],
                "ignore_effective_dates": True,
            }
        )
        started = time.perf_counter()
        try:
            response = self._search_client.search_articles(
                search_request_from_filters(
                    plan.queries[0].text,
                    filters,
                    top_k=self._top_k,
                    rerank_top_n=1,
                )
            )
        except ToolError as exc:
            return {
                "hit": False,
                "latency_ms": int((time.perf_counter() - started) * 1000),
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

    # --------------------------------------------------------------- selection
    @staticmethod
    def _selection_key(evidence: Evidence) -> tuple[int, int, float]:
        """Structure is a selection signal; the real score stays untouched."""
        return (
            1 if evidence.structured else 0,
            -(evidence.structured_rank or 0),
            evidence.score,
        )

    def _select(
        self,
        candidates: dict[tuple[str, int], _Candidate],
        query_scores: dict[str, dict[tuple[str, int], float]],
        *,
        plan: Plan | None = None,
        diagnostics: list[str] | None = None,
    ) -> list[Evidence]:
        selected_keys: list[tuple[str, int]] = []
        unit_keys: set[tuple[str, int]] = set()

        def add(key: tuple[str, int]) -> None:
            if key not in selected_keys:
                selected_keys.append(key)

        # Protect a direct section for each requested object/aspect before global fill.
        # R3：比较题按“对象×方面”各留名额，避免一个对象占满预算后另一个对象无证据。
        per_unit = 2 if len(plan.question_units if plan else []) > 1 else 1
        for unit in plan.question_units if plan else []:
            scored = sorted(
                (
                    (self._unit_priority(unit, c.evidence), self._selection_key(c.evidence), key)
                    for key, c in candidates.items()
                ),
                key=lambda row: (row[0], row[1]),
                reverse=True,
            )
            for taken, (priority, _order, key) in enumerate(scored):
                if priority < 1 or taken >= per_unit:
                    break
                add(key)
                unit_keys.add(key)
        # R3：受保护的对象×方面名额必须全部进入最终集合，不能被总上限截断。
        effective_max = max(self._max_evidence, len(unit_keys) + len(query_scores))
        # 每个子查询至少保留一条最高分证据。
        for scores in query_scores.values():
            if not scores:
                continue
            top = max(scores, key=lambda key: scores[key])
            add(top)

        for key in sorted(
            candidates,
            key=lambda item: self._selection_key(candidates[item].evidence),
            reverse=True,
        ):
            add(key)
            if len(selected_keys) >= effective_max + len(query_scores):
                break

        structured_keys = {
            key for key, candidate in candidates.items() if candidate.evidence.structured
        }
        must_keep = (
            unit_keys
            | structured_keys
            | {
                max(scores, key=lambda key: scores[key])
                for scores in query_scores.values()
                if scores
            }
        )
        doc_counts: dict[str, int] = {}
        kept: list[Evidence] = []
        for key in selected_keys:
            evidence = candidates[key].evidence
            doc_id = evidence.article.doc_id
            if doc_counts.get(doc_id, 0) >= self._max_per_doc and key not in must_keep:
                if diagnostics is not None:
                    diagnostics.append(
                        f"GRAPH_DOC_CAP:{evidence.article.article_id}:p{evidence.article.part_no}"
                    )
                continue
            doc_counts[doc_id] = doc_counts.get(doc_id, 0) + 1
            kept.append(evidence)
            if len(kept) >= effective_max:
                break

        # token budget：只在超出预算时丢弃最低分的非关键证据。
        kept = self._cap_tokens(kept, protected_keys=unit_keys, diagnostics=diagnostics)
        kept.sort(key=self._selection_key, reverse=True)
        for index, item in enumerate(kept, start=1):
            item.rank = index
        return kept

    def _unit_priority(self, unit: QuestionUnit, evidence: Evidence) -> float:
        article = evidence.article
        context = f"{article.doc_title} {' '.join(article.chapter_path)} {article.article_no}"
        detail = article.audience_detail
        asked_versions = _unit_version_years(unit)
        source_versions = _article_version_years(article)
        if asked_versions and source_versions and not (asked_versions & source_versions):
            return -1
        if (
            unit.cohort
            and detail
            and detail.intake_years
            and unit.cohort not in detail.intake_years
        ):
            return -1
        if unit.class_type and self._class_terms:
            from campusguide.domain.source_context import trusted_context

            metadata = trusted_context(article)
            class_context = " ".join(
                [context, *metadata["colleges"], *metadata["majors"]]
            )
            if not _class_scope_ok(unit, class_context, self._class_terms):
                return -1
        elif (
            not unit.class_type
            and bool(unit.subject)
            and "普通" in unit.original_span
            and not re.search(r"创新班|实验班|丁颖", unit.subject)
            and re.search(r"创新班|实验班|丁颖", context)
        ):
            # Compatibility for legacy units that predate class_type.
            return -1
        score = evidence.score
        if unit.subject:
            if unit.subject in context:
                score += 1
            elif detail and (detail.colleges or detail.majors):
                from campusguide.domain.source_context import trusted_context

                metadata = trusted_context(article)
                if any(
                    unit.subject in label for label in [*metadata["colleges"], *metadata["majors"]]
                ):
                    score += 1
                else:
                    return -1
        patterns = {
            "核心课程": r"核心课程",
            "培养目标": r"培养目标",
            "毕业学分": r"课程结构|毕业要求",
            "学制": r"学制",
            "授予学位": r"学位",
            "毕业要求": r"毕业要求",
            "课程设置": r"课程设置|进程表",
            "实践课程": r"集中实践|实践教学|进程表|教学计划",
            "实践基地": r"国家级|省级|校级|院级|实践基地",
            "时间安排": r"时间安排|日程|工作安排",
            "选修课程": r"进程表|教学计划|课程设置",
            "毕业设计": r"进程表|毕业论文|毕业设计",
            "研究方向": r"研究方向",
            "师资归属": r"教师|师资",
            "负责人": r"委员会|现任领导",
        }
        for aspect in unit.requested_aspects:
            pattern = patterns.get(aspect)
            if pattern and re.search(pattern, article.article_no):
                score += 3
            if aspect == "毕业设计" and re.search(
                r"毕业(?:论文\s*[/／]?\s*设计|论文|设计)", article.text
            ):
                score += 5
                if re.search(r"\d{6}\s*[|｜]", article.text):
                    score += 2
            elif aspect == "实践课程" and re.search(
                r"集中实践|毕业实习|毕业论文/设计|毕业设计", article.text
            ):
                score += 2
        if "矩阵" in article.article_no and "矩阵" not in unit.original_span:
            score -= 4
        return score

    def _cap_tokens(
        self,
        evidence: list[Evidence],
        *,
        protected_keys: set | None = None,
        diagnostics: list[str] | None = None,
    ) -> list[Evidence]:
        if not evidence:
            return evidence
        total = sum(evidence_cost(item.article) for item in evidence)
        budget_chars = self._token_budget
        if total <= budget_chars:
            return evidence
        protected: set[int] = set()
        seen_queries: set[str] = set()
        for item in evidence:
            if (
                item.source == "flow"
                or item.structured
                or item.key in (protected_keys or set())
            ):
                protected.add(id(item))
                continue
            for query_id in item.matched_queries:
                if query_id not in seen_queries:
                    seen_queries.add(query_id)
                    protected.add(id(item))
        kept = list(evidence)
        for item in sorted(evidence, key=lambda entry: entry.score):
            if total <= budget_chars:
                break
            if id(item) in protected:
                continue
            kept.remove(item)
            total -= evidence_cost(item.article)
            if diagnostics is not None:
                diagnostics.append(
                    f"GRAPH_TOKEN_BUDGET:{item.article.article_id}:p{item.article.part_no}"
                )
        if total > budget_chars and diagnostics is not None:
            diagnostics.append(f"PROTECTED_EVIDENCE_BUDGET:estimated={total}:budget={budget_chars}")
        return kept or evidence[: self._max_evidence]

    # --------------------------------------------------------------- hydration
    def _hydrate(
        self,
        citations: list[str],
        selected: list[Evidence],
    ) -> tuple[list[Evidence], list[str]]:
        warnings: list[str] = []
        existing_ids = {item.article.article_id for item in selected}
        hydrated: list[Evidence] = []
        for citation in dict.fromkeys(citations):
            article_id = citation
            if article_id in existing_ids:
                continue
            try:
                payload = self._search_client.get_article(article_id)
            except Exception:
                warnings.append(f"{FLOW_CITATION_UNAVAILABLE}:{article_id}")
                continue
            if payload is None:
                warnings.append(f"{FLOW_CITATION_UNAVAILABLE}:{article_id}")
                continue
            public = getattr(payload, "article", None)
            if public is None:
                warnings.append(f"{FLOW_CITATION_UNAVAILABLE}:{article_id}")
                continue
            try:
                article = _article_from_public(public)
            except Exception:
                warnings.append(f"{FLOW_CITATION_UNAVAILABLE}:{article_id}")
                continue
            hydrated.append(
                Evidence(
                    article=article,
                    rerank_score=0.0,
                    rank=len(selected) + len(hydrated) + 1,
                    source="flow",
                )
            )
            existing_ids.add(article.article_id)
        return hydrated, warnings

    @staticmethod
    def _audience(filters: PolicyFilters) -> Audience | None:
        if not filters.audience:
            return None
        return filters.audience[0]


__all__ = [
    "FLOW_CITATION_UNAVAILABLE",
    "ORIGINAL_QUERY_FALLBACK_EMPTY",
    "ORIGINAL_QUERY_FALLBACK_FAILED",
    "ORIGINAL_QUERY_FALLBACK_REJECTED",
    "ORIGINAL_QUERY_FALLBACK_SKIPPED",
    "ORIGINAL_QUERY_FALLBACK_USED",
    "PARTIAL_RETRIEVAL",
    "GraphRetriever",
    "RetrievalBundle",
]
