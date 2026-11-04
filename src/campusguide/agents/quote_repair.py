"""W6-A2：citation quote 连续性与范围/数字覆盖的生成侧修复。

这里只做“检测 + 一次重写重试”，不改变 W3 的确定性核对：
- 不修改 Verifier / ClaimValidator；
- 修复失败时保留原 draft，交给 Verifier 丢弃；
- 不把模型重写当成新的核对依据。

修复调用与 revise 一样只接受 ``citation {quote_id}``：
服务器按登记窗口取回原文连续子串，模型重抄的文本永远不作为事实来源。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

from campusguide.domain.models import Evidence, Fact
from campusguide.domain.text import normalize_text
from campusguide.retrieval.source_spans import SourceSpan, source_spans
from campusguide.verification.facts import (
    check_evidence_fact_coverage,
    check_fact_coverage,
    extract_facts_detailed,
)
from campusguide.verification.quotes import locate_quote

MIN_QUOTE_CHARS = 6
SCOPED_SPAN_LABEL = "上一轮引用原文片段"
MAX_ADDITIONAL_CLAIM_CANDIDATES = 8
MAX_PROMPT_QUOTE_CHARS = 180
MAX_PROMPT_OTHER_CANDIDATES = 12
_ANCHOR_WINDOW = 24
_MIN_ANCHOR_CHARS = 2
_MIN_ENTITY_CHARS = 2
_MIN_SUFFIX_CHARS = 3
_ANCHOR_SENTINEL = "\u0000"
_ANCHOR_BREAKS = frozenset("，,。；;：:、.．\n\r\t（）()【】[]《》〈〉“”\"'‘’ \u3000")
_ENTITY_BREAKS = _ANCHOR_BREAKS | frozenset("-—–－−~～〜/%…")
_CLAUSE_BREAKS = frozenset("，,。；;：:、\n\r")
_SENTENCE_BREAKS = frozenset("。；;\n\r")
_GENERIC_ANCHORS = frozenset(
    {
        "要求",
        "规定",
        "需",
        "需要",
        "应当",
        "应该",
        "必须",
        "须",
        "应",
        "可以",
        "可",
        "共计",
        "合计",
        "总计",
        "至少",
        "最多",
        "不超过",
        "不低于",
        "不少于",
        "不多于",
        "以上",
        "以下",
        "其中",
        "以及",
        "并且",
        "而且",
        "同时",
        "关于",
        "对于",
        "方面",
        "情况",
        "标准",
        "条件",
        "为",
        "是",
        "的",
        "了",
        "有",
        "按",
        "按照",
        "每",
        "共",
        "计",
        "达",
        "达到",
        "完成",
        "修满",
        "修读",
        "获得",
        "取得",
        "在",
        "于",
        "前",
        "后",
        "之前",
        "之后",
        "结束",
        "其他",
        "其余",
        "金额",
        "数额",
        "日期",
        "时间",
        "期限",
        "和",
        "与",
        "及",
        "或",
        "等",
        "等等",
        "相关",
        "有关",
        "包括",
        "包含",
        "学生",
        "学分",
        "学时",
        "学期",
        "学年",
        "工作日",
        "自然日",
        "小时",
    }
)
_GENERIC_ANCHOR_RE = re.compile(
    "|".join(re.escape(token) for token in sorted(_GENERIC_ANCHORS, key=len, reverse=True))
)
_ENTITY_META_PATTERNS = (
    re.compile(
        r"(?:19|20)\d{2}(?:-+|—+|–+|－+|−+|~+|～+|〜+|至|到)"
        r"(?:19|20)\d{2}(?:学?年)?[级届]?"
    ),
    re.compile(r"(?:19|20)\d{2}(?:学?年)?[级届]?"),
    re.compile(r"第[一二三四五六七八九十百\d]+个?学期"),
    re.compile(r"[一二三四五六七八九十百\d]+个?学期"),
    re.compile(r"非全日制|全日制"),
    re.compile(r"本科生?|研究生|硕士生?|博士生?|专科生?"),
    re.compile(r"本(?:学)?年|当年|该学年|入学|毕业|应届|往届"),
)
_SCOPED_SPAN_RE = re.compile(r"^source_(\d+)_prev_(\d+)$")


@dataclass(frozen=True)
class QuoteProblem:
    index: int
    claim_id: str
    reasons: tuple[str, ...]


@dataclass
class QuoteRepairResult:
    drafts: list[Any]
    changed: bool = False
    remaining: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class RecommendedSpan:
    """An existing registered window that carries a missing typed fact.

    ``span_id`` is the canonical registry ID; the quote is never rewritten or
    composed from several windows.
    """

    span_id: str
    supported_facts: tuple[str, ...] = ()


@dataclass
class ClaimCandidates:
    """Repair catalog for one claim: legal IDs plus typed-fact recommendations."""

    span_ids: list[str] = field(default_factory=list)
    original_span_ids: list[str] = field(default_factory=list)
    recommended: list[RecommendedSpan] = field(default_factory=list)
    missing_facts: list[str] = field(default_factory=list)

    @property
    def recommended_span_ids(self) -> list[str]:
        return [item.span_id for item in self.recommended]


@dataclass(frozen=True)
class _EntityProbe:
    text: str
    fragment: str
    distance: int


@dataclass(frozen=True)
class _EntityContext:
    text: str
    distance: int
    context_len: int
    occurrence: int


@dataclass(frozen=True)
class _EntityMatch:
    probe: str
    fragment: str
    distance: int
    context_len: int

    @property
    def probe_len(self) -> int:
        return len(self.probe)

    @property
    def exact_fragment(self) -> bool:
        return self.probe == self.fragment


def is_scoped_span_id(span_id: str) -> bool:
    return _SCOPED_SPAN_RE.fullmatch(span_id) is not None


def _has_cjk(text: str) -> bool:
    return any("\u4e00" <= char <= "\u9fff" for char in text)


def _fact_pattern(raw: str) -> re.Pattern[str] | None:
    compact = re.sub(r"\s+", "", raw)
    if not compact:
        return None
    return re.compile(r"\s*".join(re.escape(char) for char in compact))


def _claim_anchor(text: str, position: int) -> str:
    window = re.sub(r"\s+", "", text[max(0, position - _ANCHOR_WINDOW) : position])
    cut = max(
        (index for index, char in enumerate(window) if char in _ANCHOR_BREAKS),
        default=-1,
    )
    return window[cut + 1 :]


def _strip_generic_anchor(anchor: str) -> str:
    """Remove boilerplate words so only a business phrase can anchor a match."""
    for token in sorted(_GENERIC_ANCHORS, key=len, reverse=True):
        anchor = anchor.replace(token, _ANCHOR_SENTINEL)
    return anchor


def _common_anchor(anchor: str, context: str) -> str:
    """Longest non-generic business phrase shared by claim and candidate context."""
    context = re.sub(r"\s+", "", context)
    best = ""
    for start in range(len(anchor)):
        if len(anchor) - start <= len(best):
            break
        for end in range(len(anchor), start + 1, -1):
            size = end - start
            if size <= len(best):
                break
            piece = anchor[start:end]
            if (
                _ANCHOR_SENTINEL in piece
                or piece in _GENERIC_ANCHORS
                or not _has_cjk(piece)
            ):
                continue
            if piece in context:
                best = piece
                break
    return best


def _entity_char(char: str) -> bool:
    return (
        "\u4e00" <= char <= "\u9fff"
        or (char.isascii() and char.isalnum())
        or "\u2160" <= char <= "\u217f"
    )


def _all_fact_spans(text: str) -> list[tuple[int, int]]:
    spans: list[tuple[int, int]] = []
    seen: set[tuple[int, int]] = set()
    for item in extract_facts_detailed(text).facts:
        pattern = _fact_pattern(item.raw)
        if pattern is None:
            continue
        for match in pattern.finditer(text):
            key = (match.start(), match.end())
            if key not in seen:
                seen.add(key)
                spans.append(key)
    return spans


def _bounded_span(text: str, position: int, breaks: frozenset[str]) -> tuple[int, int]:
    start = 0
    for index in range(position - 1, -1, -1):
        if text[index] in breaks:
            start = index + 1
            break
    end = len(text)
    for index in range(position, len(text)):
        if text[index] in breaks:
            end = index
            break
    return start, end


def _distance_to_fact(
    start: int, end: int, fact_start: int, fact_end: int
) -> int:
    if end <= fact_start:
        return fact_start - end
    if fact_end <= start:
        return start - fact_end
    return 0


def _entity_fragments(
    text: str,
    *,
    start: int,
    end: int,
    fact_spans: list[tuple[int, int]],
) -> list[tuple[str, int, int]]:
    """Business-phrase runs around a fact, with metadata/generic tokens masked.

    Suffixes of each run (not arbitrary prefixes) become entity probes, so a
    modifier such as a cohort can be ignored while the head noun still anchors.
    """
    start = max(0, start)
    end = min(len(text), end)
    if start >= end:
        return []
    segment = text[start:end]
    keep = [True] * len(segment)

    def mask(span_start: int, span_end: int) -> None:
        for index in range(max(0, span_start), min(len(segment), span_end)):
            keep[index] = False

    for fact_start, fact_end in fact_spans:
        mask(fact_start - start, fact_end - start)
    for pattern in _ENTITY_META_PATTERNS:
        for match in pattern.finditer(segment):
            mask(match.start(), match.end())
    for match in _GENERIC_ANCHOR_RE.finditer(segment):
        mask(match.start(), match.end())
    for index, char in enumerate(segment):
        if char in _ENTITY_BREAKS or not _entity_char(char):
            keep[index] = False

    fragments: list[tuple[str, int, int]] = []
    cursor = 0
    while cursor < len(segment):
        if not keep[cursor]:
            cursor += 1
            continue
        run_start = cursor
        while cursor < len(segment) and keep[cursor]:
            cursor += 1
        fragment = segment[run_start:cursor]
        if len(fragment) >= _MIN_ENTITY_CHARS and _has_cjk(fragment):
            fragments.append((fragment, start + run_start, start + cursor))
    return fragments


def _suffix_probes(fragment: str) -> list[str]:
    """Entity heads: every suffix of >=3 chars, or the whole 2-char fragment.

    The 3-char floor rejects accidental shared phrases such as ``实践`` inside
    ``专业实践`` vs ``社会主义理论与实践`` while still allowing a real two-char
    business entity.
    """
    if len(fragment) < _MIN_ENTITY_CHARS:
        return []
    if len(fragment) == _MIN_ENTITY_CHARS:
        return [fragment]
    probes: list[str] = []
    for index in range(0, len(fragment) - _MIN_SUFFIX_CHARS + 1):
        probe = fragment[index:]
        if probe not in probes:
            probes.append(probe)
    return probes


def _probe_matches_entity(probe: str, entity: str) -> bool:
    if not probe or not entity or not _has_cjk(probe):
        return False
    if len(probe) >= _MIN_SUFFIX_CHARS:
        return probe in entity
    return len(probe) == _MIN_ENTITY_CHARS and entity.startswith(probe)


def _claim_entity_probes(claim_text: str, fact: Fact) -> list[_EntityProbe]:
    text = normalize_text(claim_text)
    pattern = _fact_pattern(fact.raw)
    if pattern is None:
        return []
    matches = list(pattern.finditer(text))
    if not matches:
        return []
    fact_spans = _all_fact_spans(text)
    probes: list[_EntityProbe] = []
    seen: set[tuple[str, int]] = set()
    for match in matches:
        clause_start, clause_end = _bounded_span(text, match.start(), _CLAUSE_BREAKS)
        fragments = _entity_fragments(
            text, start=clause_start, end=clause_end, fact_spans=fact_spans
        )
        if not fragments:
            sentence_start, sentence_end = _bounded_span(
                text, match.start(), _SENTENCE_BREAKS
            )
            fragments = _entity_fragments(
                text, start=sentence_start, end=sentence_end, fact_spans=fact_spans
            )
        if not fragments:
            continue
        ranked = sorted(
            (
                (
                    _distance_to_fact(start, end, match.start(), match.end()),
                    start,
                    fragment,
                )
                for fragment, start, end in fragments
            ),
            key=lambda item: (item[0], -item[1]),
        )
        nearest = ranked[0][0]
        for distance, _, fragment in ranked:
            if distance != nearest:
                continue
            for probe_text in _suffix_probes(fragment):
                key = (probe_text, distance)
                if key in seen:
                    continue
                seen.add(key)
                probes.append(_EntityProbe(probe_text, fragment, distance))
    return probes


def _candidate_entities(quote: str, fact: Fact) -> list[_EntityContext]:
    text = normalize_text(quote)
    pattern = _fact_pattern(fact.raw)
    if pattern is None:
        return []
    matches = list(pattern.finditer(text))
    if not matches:
        return []
    fact_spans = _all_fact_spans(text)
    entities: list[_EntityContext] = []
    seen: set[tuple[int, int, int, str]] = set()
    for occurrence, match in enumerate(matches):
        start, end = _bounded_span(text, match.start(), _SENTENCE_BREAKS)
        fragments = _entity_fragments(
            text, start=start, end=end, fact_spans=fact_spans
        )
        context_len = end - start
        for fragment, fragment_start, fragment_end in fragments:
            key = (occurrence, fragment_start, fragment_end, fragment)
            if key in seen:
                continue
            seen.add(key)
            entities.append(
                _EntityContext(
                    text=fragment,
                    distance=_distance_to_fact(
                        fragment_start, fragment_end, match.start(), match.end()
                    ),
                    context_len=context_len,
                    occurrence=occurrence,
                )
            )
    return entities


def _fact_entity_support(
    quote: str, claim_text: str, fact: Fact
) -> _EntityMatch | None:
    """Literal typed fact plus the nearest same-sentence business entity.

    Only the registered window's own text is examined. A nearer unrelated
    entity blocks a farther match, so a different clause cannot lend its
    amount/category to this fact.
    """
    if fact.canonical not in {
        item.canonical for item in extract_facts_detailed(quote).facts
    }:
        return None
    probes = _claim_entity_probes(claim_text, fact)
    if not probes:
        return None
    entities = _candidate_entities(quote, fact)
    if not entities:
        return None
    by_occurrence: dict[int, list[_EntityContext]] = {}
    for entity in entities:
        by_occurrence.setdefault(entity.occurrence, []).append(entity)
    best: _EntityMatch | None = None
    for nearby in by_occurrence.values():
        nearest = min(entity.distance for entity in nearby)
        for probe in probes:
            for entity in nearby:
                if entity.distance != nearest:
                    continue
                if not _probe_matches_entity(probe.text, entity.text):
                    continue
                match = _EntityMatch(
                    probe=probe.text,
                    fragment=probe.fragment,
                    distance=entity.distance,
                    context_len=entity.context_len,
                )
                if best is None or (
                    match.probe_len,
                    match.exact_fragment,
                    -match.distance,
                    -match.context_len,
                ) > (
                    best.probe_len,
                    best.exact_fragment,
                    -best.distance,
                    -best.context_len,
                ):
                    best = match
    return best


def _span_supports_fact_with_anchor(
    quote: str, claim_text: str, fact: Fact
) -> bool:
    """True only for a literal typed value with a matching business entity."""
    return _fact_entity_support(quote, claim_text, fact) is not None


def _missing_facts(draft: Any, evidence: list[Evidence]) -> list[Fact]:
    """Facts the original locatable citations do not cover.

    Mirrors ``find_quote_problems``: only citation quotes that resolve to the
    evidence are trusted, and metadata-only years keep their existing rules.
    """
    text = getattr(draft, "text", "") or ""
    if not text:
        return []
    by_article: dict[str, list[Evidence]] = {}
    for item in evidence:
        by_article.setdefault(item.article.article_id, []).append(item)
    quotes: list[str] = []
    articles: list[Any] = []
    for citation in getattr(draft, "citations", []):
        quote = getattr(citation, "quote", "") or ""
        article_id = getattr(citation, "article_id", "") or ""
        if not quote:
            continue
        for item in by_article.get(article_id, []):
            if locate_quote(item.article.text, quote):
                quotes.append(quote)
                articles.append(item.article)
    return check_evidence_fact_coverage(text, quotes, articles).uncovered


@dataclass
class SpanRegistry:
    """Registered source windows plus immutable scoped spans for old citations."""

    evidence: list[Evidence]
    spans: dict[str, SourceSpan]
    _scoped_counts: dict[int, int] = field(default_factory=dict)

    @classmethod
    def build(cls, evidence: list[Evidence]) -> SpanRegistry:
        return cls(evidence=list(evidence), spans=dict(source_spans(evidence)))

    def ids_for_quote(
        self, article_id: str, quote: str, *, register: bool = False
    ) -> list[str]:
        """Return exact registered IDs; optionally register the literal source slice.

        A scoped span is created only from ``article.text[start:end]`` and keeps the
        evidence part identity. The model's supplied quote is used solely to locate
        the slice; it never becomes the registered text.
        """
        quote = (quote or "").strip()
        if not article_id or not quote:
            return []
        exact = [
            span_id
            for span_id, span in self.spans.items()
            if span.article_id == article_id and span.quote == quote
        ]
        if exact:
            return exact
        if not register:
            return []
        registered: list[str] = []
        for index, item in enumerate(self.evidence, 1):
            article = item.article
            if article.article_id != article_id:
                continue
            located = locate_quote(article.text, quote)
            if located is None:
                continue
            existing = self._span_id_for_slice(
                article.article_id, article.part_no, located.start, located.end
            )
            span_id = existing or self._register_scope(index, item, located.start, located.end)
            if span_id not in registered:
                registered.append(span_id)
        return registered

    def ensure_quote_span(self, article_id: str, quote: str) -> str | None:
        ids = self.ids_for_quote(article_id, quote, register=True)
        return ids[0] if ids else None

    def candidates_for_claims(
        self, drafts: list[Any], problems: list[QuoteProblem]
    ) -> dict[str, ClaimCandidates]:
        """Repair catalog per failing claim.

        Original citation windows remain legal. Recommended windows are existing
        same-doc/version windows that literally contain a missing typed fact and
        whose nearest same-sentence entity matches the claim's business target.
        No span is synthesized, concatenated or remapped here.
        """
        result: dict[str, ClaimCandidates] = {}
        evidence_article_ids = {item.article.article_id for item in self.evidence}
        for problem in problems:
            if problem.index >= len(drafts):
                continue
            draft = drafts[problem.index]
            article_ids = {
                citation.article_id for citation in draft.citations if citation.article_id
            }
            source_article_ids: list[str] = []
            original_ids: list[str] = []
            for citation in draft.citations:
                quote_id = matching_span_id(citation, self.spans)
                if quote_id:
                    if quote_id not in original_ids:
                        original_ids.append(quote_id)
                    source_article_ids.append(self.spans[quote_id].article_id)
                for span_id in self.ids_for_quote(
                    citation.article_id, citation.quote, register=True
                ):
                    if span_id not in original_ids:
                        original_ids.append(span_id)
                    source_article_ids.append(self.spans[span_id].article_id)
                if citation.article_id in evidence_article_ids:
                    source_article_ids.append(citation.article_id)
            other_ids: list[str] = []
            for span_id, span in self.spans.items():
                if article_ids and span.article_id not in article_ids:
                    continue
                if span_id in original_ids or span_id in other_ids:
                    continue
                other_ids.append(span_id)
            source_articles = set(source_article_ids)
            families = {
                (item.article.doc_id, item.article.version_id)
                for item in self.evidence
                if item.article.article_id in source_articles
            }
            missing = _missing_facts(draft, self.evidence)
            recommended = self._rank_recommended(
                getattr(draft, "text", "") or "",
                missing,
                families,
            )
            recommended_ids = [item.span_id for item in recommended]
            span_ids: list[str] = []
            for span_id in [*recommended_ids, *original_ids, *other_ids]:
                if span_id not in span_ids:
                    span_ids.append(span_id)
            result[problem.claim_id] = ClaimCandidates(
                span_ids=span_ids,
                original_span_ids=original_ids,
                recommended=recommended,
                missing_facts=[fact.canonical for fact in missing],
            )
        return result

    def ids_for_claims(
        self, drafts: list[Any], problems: list[QuoteProblem]
    ) -> dict[str, list[str]]:
        """Legacy view: original windows first, then bounded recommendations.

        ``candidates_for_claims`` remains the recommended-first view used by the
        repair prompt; this compatibility view preserves the older contract.
        """
        legacy: dict[str, list[str]] = {}
        for claim_id, candidates in self.candidates_for_claims(drafts, problems).items():
            ordered: list[str] = []
            for span_id in [
                *candidates.original_span_ids,
                *candidates.recommended_span_ids,
            ]:
                if span_id not in ordered:
                    ordered.append(span_id)
            legacy[claim_id] = ordered
        return legacy

    def _rank_recommended(
        self,
        claim_text: str,
        missing_facts: list[Fact],
        families: set[tuple[str, str]],
        *,
        exclude_ids: set[str] | None = None,
    ) -> list[RecommendedSpan]:
        if not claim_text or not missing_facts or not families:
            return []
        excluded = exclude_ids or set()
        ranked: list[tuple[tuple[Any, ...], str, tuple[str, ...]]] = []
        for order, (span_id, span) in enumerate(self.spans.items()):
            if span_id in excluded or not self._in_families(span.article_id, families):
                continue
            supported: list[str] = []
            matches: list[_EntityMatch] = []
            for fact in missing_facts:
                match = _fact_entity_support(span.quote, claim_text, fact)
                if match is None:
                    continue
                if fact.canonical not in supported:
                    supported.append(fact.canonical)
                matches.append(match)
            if not supported:
                continue
            score = (
                -len(supported),
                -max(match.probe_len for match in matches),
                -int(any(match.exact_fragment for match in matches)),
                max(match.context_len for match in matches),
                min(match.distance for match in matches),
                len(normalize_text(span.quote)),
                int(span_id.endswith("_body")),
                order,
            )
            ranked.append((score, span_id, tuple(supported)))
        ranked.sort(key=lambda item: item[0])
        return [
            RecommendedSpan(span_id=span_id, supported_facts=supported)
            for _, span_id, supported in ranked[:MAX_ADDITIONAL_CLAIM_CANDIDATES]
        ]

    def _additional_candidates(
        self,
        draft: Any,
        offered_ids: list[str],
        source_article_ids: list[str],
    ) -> list[str]:
        """Compatibility wrapper for the bounded recommendation search."""
        text = getattr(draft, "text", "") or ""
        if not text or not source_article_ids:
            return []
        source_articles = set(source_article_ids)
        families = {
            (item.article.doc_id, item.article.version_id)
            for item in self.evidence
            if item.article.article_id in source_articles
        }
        if not families:
            return []
        uncovered = check_fact_coverage(
            text,
            [
                self.spans[span_id].quote
                for span_id in offered_ids
                if span_id in self.spans
            ],
        ).uncovered
        if not uncovered:
            return []
        return [
            item.span_id
            for item in self._rank_recommended(
                text,
                uncovered,
                families,
                exclude_ids=set(offered_ids),
            )
        ]

    def _in_families(
        self, article_id: str, families: set[tuple[str, str]]
    ) -> bool:
        return any(
            (item.article.doc_id, item.article.version_id) in families
            for item in self.evidence
            if item.article.article_id == article_id
        )

    def _span_id_for_slice(
        self, article_id: str, part_no: int, start: int, end: int
    ) -> str | None:
        for span_id, span in self.spans.items():
            if (
                span.article_id == article_id
                and span.part_no == part_no
                and span.start == start
                and span.end == end
            ):
                return span_id
        return None

    def _register_scope(
        self, index: int, item: Evidence, start: int, end: int
    ) -> str:
        article = item.article
        counter = self._scoped_counts.get(index, 0)
        while True:
            counter += 1
            span_id = f"source_{index}_prev_{counter}"
            if span_id not in self.spans:
                break
        self._scoped_counts[index] = counter
        self.spans[span_id] = SourceSpan(
            span_id=span_id,
            article_id=article.article_id,
            part_no=article.part_no,
            start=start,
            end=end,
            quote=article.text[start:end],
            label=SCOPED_SPAN_LABEL,
        )
        return span_id


_NEGATION_RE = re.compile(r"不得|不予|不能|不可|不允许|无须|无需|不再|未|没有|不")
_SCOPE_MARKERS = (
    re.compile(r"非全日制|全日制"),
    re.compile(r"本科生?|研究生|硕士生?|博士生?|专科生?"),
    re.compile(r"(?:19|20)\d{2}\s*(?:-+|—+|–+|－+|−+|~+|～+|〜+|至|到)\s*(?:19|20)\d{2}\s*(?:学?年)?[级届]?"),
    re.compile(r"(?:19|20)\d{2}\s*(?:学?年)?[级届]"),
)


_SCOPE_RANGE_SEPARATOR_RE = re.compile(r"[—–－−~～〜至到-]+")


def _canonical_scope_token(token: str) -> str:
    """Normalize separators only; year endpoints and cohort markers stay."""
    compact = re.sub(r"\s+", "", normalize_text(token))
    return _SCOPE_RANGE_SEPARATOR_RE.sub("-", compact)


def _span_scope_tokens(text: str) -> set[str]:
    tokens: set[str] = set()
    normalized = normalize_text(text or "")
    for pattern in _SCOPE_MARKERS:
        for match in pattern.finditer(normalized):
            tokens.add(_canonical_scope_token(match.group(0)))
    return tokens


def _scope_compatible(span_quote: str, claim_text: str) -> bool:
    """Reject a compact candidate that adds a scope the claim does not carry."""
    claim_scope = _span_scope_tokens(claim_text)
    span_scope = _span_scope_tokens(span_quote)
    if not span_scope:
        return True
    return span_scope <= claim_scope


def _scope_contradicts(span_quote: str, claim_text: str) -> bool:
    """Reject a full body that explicitly names a different mode/year/degree.

    A body naturally mentions unrelated cohorts in other sections, so subset
    matching is too strict; an explicit disjoint mode/year/degree is still a
    contradiction and must fail closed.
    """
    claim_tokens = _span_scope_tokens(claim_text)
    span_tokens = _span_scope_tokens(span_quote)
    if not span_tokens:
        return False
    modes = {"全日制", "非全日制"}
    claim_modes = claim_tokens & modes
    span_modes = span_tokens & modes
    if span_modes and claim_modes and not span_modes & claim_modes:
        return True
    years = re.compile(r"(?:19|20)\d{2}")
    claim_years = {
        year for token in claim_tokens for year in years.findall(token)
    }
    span_years = {
        year for token in span_tokens for year in years.findall(token)
    }
    if span_years and claim_years and not span_years & claim_years:
        return True
    degrees = re.compile(r"本科生?|研究生|硕士生?|博士生?|专科生?")
    claim_degrees = {
        degree for token in claim_tokens for degree in degrees.findall(token)
    }
    span_degrees = {
        degree for token in span_tokens for degree in degrees.findall(token)
    }
    return bool(span_degrees and claim_degrees and not span_degrees & claim_degrees)


def _fact_negation_states(text: str, fact: Fact) -> set[bool]:
    """Negation only counts in the clause that actually carries this fact."""
    normalized = normalize_text(text)
    pattern = _fact_pattern(fact.raw)
    if pattern is None:
        return set()
    states: set[bool] = set()
    for match in pattern.finditer(normalized):
        start, end = _bounded_span(normalized, match.start(), _CLAUSE_BREAKS)
        states.add(_NEGATION_RE.search(normalized[start:end]) is not None)
    return states


def _strip_scope_markers(text: str) -> str:
    cleaned = text or ""
    for pattern in _SCOPE_MARKERS:
        cleaned = pattern.sub("", cleaned)
    return cleaned


def _fact_occurrences(
    text: str, fact: Fact, breaks: frozenset[str]
) -> list[tuple[set[str], bool, list[str]]]:
    """Per-occurrence clause scope, negation and nearest business entities."""
    normalized = normalize_text(text)
    pattern = _fact_pattern(fact.raw)
    if pattern is None:
        return []
    fact_spans = _all_fact_spans(normalized)
    occurrences: list[tuple[set[str], bool, list[str]]] = []
    for match in pattern.finditer(normalized):
        start, end = _bounded_span(normalized, match.start(), breaks)
        fragments = _entity_fragments(
            normalized, start=start, end=end, fact_spans=fact_spans
        )
        if not fragments and breaks == _CLAUSE_BREAKS:
            start, end = _bounded_span(
                normalized, match.start(), _SENTENCE_BREAKS
            )
            fragments = _entity_fragments(
                normalized, start=start, end=end, fact_spans=fact_spans
            )
        distances = [
            (
                _distance_to_fact(
                    fragment_start, fragment_end, match.start(), match.end()
                ),
                fragment,
            )
            for fragment, fragment_start, fragment_end in fragments
        ]
        nearest = min((distance for distance, _ in distances), default=0)
        entities = list(
            dict.fromkeys(
                fragment
                for distance, fragment in distances
                if distance == nearest
            )
        )
        clause = normalized[start:end]
        occurrences.append(
            (
                _span_scope_tokens(clause),
                _NEGATION_RE.search(clause) is not None,
                entities,
            )
        )
    return occurrences


def _entity_head_match(left: str, right: str, *, minimum: int = 3) -> bool:
    """Business heads must agree on any meaningful qualifier prefix.

    A generic source head (``专业实践``) may support a narrower claim
    (``集中专业实践``), but a source-only prefix must not broaden a generic
    claim. Two different non-empty prefixes (``创新班``/``普通班``,
    ``本省``/``外省``) also fail closed; mode/year/degree markers are compared
    separately at the fact occurrence.
    """
    if len(left) < minimum or len(right) < minimum:
        return False
    if left == right:
        return True
    size = 0
    while (
        size < len(left)
        and size < len(right)
        and left[-1 - size] == right[-1 - size]
    ):
        size += 1
    if size < 4:
        return False
    left_prefix = _strip_scope_markers(left[:-size])
    right_prefix = _strip_scope_markers(right[:-size])
    if left_prefix == right_prefix:
        return True
    return bool(left_prefix) and not right_prefix


def _strict_entity_support(
    quote: str,
    claim_text: str,
    fact: Fact,
    *,
    shared_scope: set[str] | None = None,
) -> bool:
    """Occurrence-bound typed literal, scope, negation and business head.

    Every claim occurrence of this typed fact needs its own compatible source
    occurrence in the window (or shared scope). One matching pair cannot lend
    its typed value to a second object or mode. A qualifier, negation or entity
    from another clause cannot rescue an occurrence.
    """
    if fact.canonical not in {
        item.canonical for item in extract_facts_detailed(quote).facts
    }:
        return False
    claim_occurrences = _fact_occurrences(claim_text, fact, _CLAUSE_BREAKS)
    quote_occurrences = _fact_occurrences(quote, fact, _CLAUSE_BREAKS)
    claim_slots = [
        occurrence for occurrence in claim_occurrences if occurrence[2]
    ]
    quote_slots = [
        occurrence for occurrence in quote_occurrences if occurrence[2]
    ]
    if not claim_slots or len(quote_slots) < len(claim_slots):
        return False
    shared = shared_scope or set()

    def compatible(
        claim_occ: tuple[set[str], bool, list[str]],
        quote_occ: tuple[set[str], bool, list[str]],
    ) -> bool:
        claim_tokens, claim_negated, claim_entities = claim_occ
        quote_tokens, quote_negated, quote_entities = quote_occ
        if quote_negated != claim_negated:
            return False
        if not quote_tokens <= claim_tokens:
            return False
        if not claim_tokens <= quote_tokens | shared:
            return False
        return any(
            _entity_head_match(claim, candidate)
            for claim in claim_entities
            for candidate in quote_entities
        )

    adjacency = [
        [
            quote_index
            for quote_index, quote_occ in enumerate(quote_slots)
            if compatible(claim_occ, quote_occ)
        ]
        for claim_occ in claim_slots
    ]
    matched_quote: list[int] = [-1] * len(quote_slots)

    def augment(claim_index: int, seen: list[bool]) -> bool:
        for quote_index in adjacency[claim_index]:
            if seen[quote_index]:
                continue
            seen[quote_index] = True
            if matched_quote[quote_index] == -1 or augment(
                matched_quote[quote_index], seen
            ):
                matched_quote[quote_index] = claim_index
                return True
        return False

    for claim_index in range(len(claim_slots)):
        if not augment(claim_index, [False] * len(quote_slots)):
            return False
    return True


def _scope_metadata(article: Any) -> str:
    return " ".join(
        str(value)
        for value in (
            getattr(article, "doc_title", "") or "",
            getattr(article, "article_no", "") or "",
            getattr(article, "version_id", "") or "",
        )
        if value
    )


def _span_fact_support(
    span: SourceSpan,
    claim_text: str,
    missing_facts: list[Fact],
    evidence_by_article: dict[str, Evidence],
    *,
    shared_scope: set[str] | None = None,
) -> tuple[str, ...]:
    """Facts provable by this exact registered span, with no bare-cell trust.

    Reliable structured rows go through ``check_evidence_fact_coverage`` so the
    existing same-row/field/quoted-cell checks still decide. Flat/legacy spans
    only qualify when the span itself literally carries the typed fact and, for
    that exact occurrence, the local clause agrees on scope, negation and
    business entity; claim scope may also be grounded in the already-cited
    same-family/version section.
    """
    if span.structured:
        if not (
            span.curriculum_row
            and span.header_reliable
            and not span.structure_problems
            and span.column_values
        ):
            return ()
        item = evidence_by_article.get(span.article_id)
        if item is None:
            return ()
        coverage = check_evidence_fact_coverage(claim_text, [span.quote], [item.article])
        missing_canonicals = {fact.canonical for fact in missing_facts}
        return tuple(
            fact.canonical
            for fact in coverage.covered
            if fact.canonical in missing_canonicals
        )
    span_facts = {fact.canonical for fact in extract_facts_detailed(span.quote).facts}
    if not {fact.canonical for fact in missing_facts} & span_facts:
        return ()
    context_scope = set(shared_scope or ())
    item = evidence_by_article.get(span.article_id)
    if item is not None:
        context_scope |= _span_scope_tokens(_scope_metadata(item.article))
    supported: list[str] = []
    for fact in missing_facts:
        if fact.canonical not in span_facts:
            continue
        if not _strict_entity_support(
            span.quote, claim_text, fact, shared_scope=context_scope
        ):
            continue
        supported.append(fact.canonical)
    return tuple(supported)


def _table_fact_gap(draft: Any, missing_facts: list[Fact]) -> bool:
    """Whether a table citation carries the bare number but not the typed fact.

    This is the bounded entry point for table convergence: a non-table quote
    without the number is a general citation-selection problem and stays on the
    model repair path.
    """
    for citation in getattr(draft, "citations", []):
        quote = getattr(citation, "quote", "") or ""
        if "|" not in quote and "｜" not in quote:
            continue
        for fact in missing_facts:
            if fact.canonical in quote:
                continue
            number = re.match(r"\d+(?:\.\d+)?", fact.canonical)
            if number is None:
                continue
            if re.search(
                rf"(?<![\d.]){re.escape(number.group(0))}(?![\d.])", quote
            ):
                return True
    return False


def _repair_citation(template: Any, span: SourceSpan) -> Any | None:
    """Copy the registered span into a new citation of the same draft type."""
    updates = {
        "article_id": span.article_id,
        "quote": span.quote,
        "quote_id": span.span_id,
        "trusted_part_no": span.part_no,
        "trusted_start": span.start,
        "trusted_end": span.end,
    }
    if hasattr(template, "model_copy"):
        return template.model_copy(update=updates)
    if hasattr(template, "__dataclass_fields__"):
        import dataclasses

        return dataclasses.replace(template, **updates)
    return None


def deterministic_quote_repair(
    drafts: list[Any],
    problems: list[QuoteProblem],
    evidence: list[Evidence],
    *,
    registry: SpanRegistry | None = None,
) -> QuoteRepairResult:
    """Add one proven existing window for a missing typed table fact.

    The entry gate is a cited table-shaped quote that literally carries the bare
    number but not the typed fact (e.g. a wide table cell ``6`` without
    ``6学分``); other missing-fact cases stay on the model repair path.

    This is deliberately additive: the original citations stay in place, so a
    deadline/condition citation is never lost when only the numeric fact needs
    a typed source. A claim is rewritten only when the combined citation set
    covers every missing fact and the candidate window comes from the same
    ``(doc_id, version_id)`` family as the cited evidence. If no single safe
    combination exists, the draft is returned unchanged for the model repair
    path.
    """
    registry = registry or SpanRegistry.build(evidence)
    failing = {problem.index: problem for problem in problems}
    result = list(drafts)
    changed = False
    for index, draft in enumerate(drafts):
        problem = failing.get(index)
        if problem is None or index >= len(result):
            continue
        if any("未被 quote 覆盖" not in reason for reason in problem.reasons):
            # Contract failures (empty/ellipsis/non-contiguous quote) are not
            # repairable by adding a second citation; keep the model path.
            continue
        missing = _missing_facts(draft, evidence)
        if not missing:
            continue
        if not _table_fact_gap(draft, missing):
            # A missing typed fact without a table-shaped citation carrying the
            # bare number is a general citation-selection problem; keep the
            # model ID-only repair path for it.
            continue
        source_article_ids = {
            citation.article_id
            for citation in getattr(draft, "citations", [])
            if getattr(citation, "article_id", "")
        }
        families = {
            (item.article.doc_id, item.article.version_id)
            for item in evidence
            if item.article.article_id in source_article_ids
        }
        if len(families) != 1:
            # A claim already mixing source families/versions is not safe to
            # repair deterministically; leave it to the model/verifier.
            continue
        evidence_by_article = {
            item.article.article_id: item for item in evidence
        }
        shared_scope: set[str] = set()
        for citation in getattr(draft, "citations", []):
            item = evidence_by_article.get(
                getattr(citation, "article_id", "") or ""
            )
            if item is None:
                continue
            shared_scope |= _span_scope_tokens(item.article.text)
            shared_scope |= _span_scope_tokens(_scope_metadata(item.article))
        candidates: list[tuple[tuple[Any, ...], SourceSpan, tuple[str, ...]]] = []
        for order, span in enumerate(registry.spans.values()):
            if not registry._in_families(span.article_id, families):
                continue
            if span.article_id not in evidence_by_article:
                continue
            supported = _span_fact_support(
                span,
                getattr(draft, "text", "") or "",
                missing,
                evidence_by_article,
                shared_scope=shared_scope,
            )
            if not supported:
                continue
            score = (
                len(normalize_text(span.quote)),
                int(span.span_id.endswith("_body")),
                not span.structured,
                order,
            )
            candidates.append((score, span, supported))
        if not candidates:
            continue
        candidates.sort(key=lambda item: item[0])
        uncovered = [
            fact.canonical for fact in missing
            if not any(fact.canonical in supported for _, _, supported in candidates)
        ]
        if uncovered:
            continue
        # Prefer compact typed windows; still add at most one window per fact
        # group to keep the change auditable.
        chosen: list[SourceSpan] = []
        remaining = {fact.canonical for fact in missing}
        for _score, span, supported in candidates:
            if not remaining:
                break
            hit = remaining & set(supported)
            if not hit:
                continue
            chosen.append(span)
            remaining -= hit
        if remaining:
            continue
        citations = list(getattr(draft, "citations", []))
        existing = {
            (getattr(citation, "article_id", ""), getattr(citation, "quote", ""))
            for citation in citations
        }
        for span in chosen:
            if (span.article_id, span.quote) in existing:
                continue
            citation = _repair_citation(citations[0], span) if citations else None
            if citation is None:
                continue
            citations.append(citation)
            existing.add((span.article_id, span.quote))
        if len(citations) == len(getattr(draft, "citations", [])):
            continue
        updated = draft
        if hasattr(updated, "model_copy"):
            updated = updated.model_copy(update={"citations": citations})
        elif hasattr(updated, "__dataclass_fields__"):
            import dataclasses

            updated = dataclasses.replace(updated, citations=citations)
        else:
            continue
        result[index] = updated
        changed = True
    return QuoteRepairResult(
        drafts=result,
        changed=changed,
        remaining=remaining_problem_ids(result, evidence) if changed else [],
    )


def matching_span_id(citation: Any, spans: dict[str, SourceSpan]) -> str:
    """Only reuse a supplied ID when it still resolves to the same citation."""
    span_id = getattr(citation, "quote_id", "")
    if not span_id or span_id not in spans:
        return ""
    span = spans[span_id]
    article_id = getattr(citation, "article_id", "")
    quote = getattr(citation, "quote", "")
    if article_id and article_id != span.article_id:
        return ""
    if quote and quote != span.quote:
        return ""
    return span_id


def _claim_label(draft: Any, index: int) -> str:
    return draft.claim_id or f"c{index + 1}"


def _citation_problems(draft: Any, by_article: dict[str, list[Evidence]]) -> list[str]:
    reasons: list[str] = []
    for citation in draft.citations:
        quote = (citation.quote or "").strip()
        article_id = citation.article_id or ""
        if not quote:
            reasons.append(f"{article_id or '空 article_id'}：quote 为空")
            continue
        if "…" in quote or "..." in quote:
            reasons.append(f"{article_id}：quote 含省略号")
        if len("".join(quote.split())) < MIN_QUOTE_CHARS:
            reasons.append(f"{article_id}：quote 不足 {MIN_QUOTE_CHARS} 字")
        candidates = by_article.get(article_id, [])
        if not candidates:
            reasons.append(f"{article_id}：article_id 不在本轮证据")
            continue
        if not any(locate_quote(item.article.text, quote) for item in candidates):
            reasons.append(f"{article_id}：quote 不是证据正文的连续子串")
    return reasons


def find_quote_problems(drafts: list[Any], evidence: list[Evidence]) -> list[QuoteProblem]:
    by_article: dict[str, list[Evidence]] = {}
    for item in evidence:
        by_article.setdefault(item.article.article_id, []).append(item)

    problems: list[QuoteProblem] = []
    for index, draft in enumerate(drafts):
        reasons = _citation_problems(draft, by_article)
        if draft.text and draft.citations:
            locatable_quotes = [
                citation.quote
                for citation in draft.citations
                if citation.quote
                and any(
                    locate_quote(item.article.text, citation.quote)
                    for item in by_article.get(citation.article_id, [])
                )
            ]
            if locatable_quotes:
                coverage = check_evidence_fact_coverage(
                    draft.text,
                    locatable_quotes,
                    [
                        item.article
                        for citation in draft.citations
                        for item in by_article.get(citation.article_id, [])
                        if locate_quote(item.article.text, citation.quote)
                    ],
                )
                if coverage.uncovered:
                    facts = "、".join(fact.canonical for fact in coverage.uncovered)
                    reasons.append(f"claim 的数字/期限未被 quote 覆盖：{facts}")
        if reasons:
            problems.append(
                QuoteProblem(
                    index=index,
                    claim_id=_claim_label(draft, index),
                    reasons=tuple(reasons),
                )
            )
    return problems


def _quote_excerpt(
    quote: str,
    facts: tuple[str, ...] = (),
    *,
    limit: int = MAX_PROMPT_QUOTE_CHARS,
) -> str:
    """Bounded literal excerpt; never used as a citation source."""
    if len(quote) <= limit:
        return quote
    positions: list[int] = []
    for canonical in facts:
        pattern = _fact_pattern(canonical)
        if pattern is None:
            continue
        positions.extend(match.start() for match in pattern.finditer(quote))
    if positions:
        center = min(positions)
        start = max(0, center - limit // 2)
        end = min(len(quote), start + limit)
        start = max(0, end - limit)
    else:
        start, end = 0, limit
    prefix = "…" if start > 0 else ""
    suffix = "…" if end < len(quote) else ""
    return f"{prefix}{quote[start:end]}{suffix}"


def _span_catalog_entry(
    span_id: str,
    spans: dict[str, SourceSpan] | None,
    *,
    supported_facts: tuple[str, ...] = (),
    include_identity: bool = False,
    include_quote: bool = False,
) -> str:
    span = spans.get(span_id) if spans else None
    if span is None:
        return span_id
    details = [span.label or "原文窗口"]
    if include_identity:
        details.append(f"article_id={span.article_id}")
        details.append(f"part_no={span.part_no}")
        if span.table_id and span.row_id:
            details.append(f"table_id={span.table_id}")
            details.append(f"row_id={span.row_id}")
    if is_scoped_span_id(span_id):
        if not include_identity:
            details.append(f"part_no={span.part_no}")
        details.append(f"原文偏移={span.start}-{span.end}")
        if not include_quote:
            details.append(f"片段={json.dumps(span.quote, ensure_ascii=False)}")
    if supported_facts:
        details.append(f"支持={'、'.join(supported_facts)}")
    if include_quote and span.quote:
        details.append(
            f"原文={json.dumps(_quote_excerpt(span.quote, supported_facts), ensure_ascii=False)}"
        )
    return f"{span_id}（{'；'.join(details)}）"


def _candidate_catalog_lines(
    claim_id: str,
    candidates: ClaimCandidates,
    spans: dict[str, SourceSpan] | None,
) -> list[str]:
    lines = [f"- {claim_id}："]
    if candidates.missing_facts:
        lines.append(f"  缺少的类型化事实：{'、'.join(candidates.missing_facts)}")
    recommended_ids = set(candidates.recommended_span_ids)
    original_ids = [
        span_id
        for span_id in candidates.original_span_ids
        if span_id not in recommended_ids
    ]
    original_set = set(original_ids)
    other_ids = [
        span_id
        for span_id in candidates.span_ids
        if span_id not in recommended_ids and span_id not in original_set
    ]
    if candidates.recommended:
        lines.append("  [推荐] 已核实含缺少事实且业务对象匹配的现有窗口（按优先级）：")
        for item in candidates.recommended:
            lines.append(
                "  - "
                + _span_catalog_entry(
                    item.span_id,
                    spans,
                    supported_facts=item.supported_facts,
                    include_identity=True,
                    include_quote=True,
                )
            )
    elif candidates.missing_facts:
        lines.append(
            "  [推荐] 无：没有找到与 claim 业务对象一致、且含缺少类型化事实的现有窗口。"
            "请删除该 claim；不得把裸数字当作带单位事实，不得拼接不同窗口。"
        )
    if original_ids:
        lines.append("  [上一轮引用] 仍可继续使用，但不覆盖上述缺少事实：")
        for span_id in original_ids:
            lines.append(
                "  - "
                + _span_catalog_entry(
                    span_id,
                    spans,
                    include_identity=True,
                    include_quote=True,
                )
            )
    if other_ids:
        shown = other_ids[:MAX_PROMPT_OTHER_CANDIDATES]
        lines.append("  [其他可用窗口]：")
        for span_id in shown:
            lines.append("  - " + _span_catalog_entry(span_id, spans))
        if len(other_ids) > len(shown):
            lines.append(f"  （其余 {len(other_ids) - len(shown)} 个窗口已省略）")
    if not candidates.span_ids:
        lines.append("  无可用窗口，请删除该 claim")
    if candidates.recommended and candidates.missing_facts:
        lines.append(
            "  推荐窗口按覆盖缺少事实数量、业务对象匹配度和窗口紧凑度排序；"
            "一条 claim 可用多个 citation，请组合覆盖全部缺少事实。"
        )
    return lines


def build_quote_id_catalog(
    evidence: list[Evidence],
    spans: dict[str, SourceSpan] | None = None,
) -> str:
    """List the exact window IDs a prompt may use, grouped by source identity.

    The caller must pass the same evidence/spans that the parser will use; IDs
    are positional (``source_<evidence index>_*``) and must never be remapped.
    """
    spans = source_spans(evidence) if spans is None else spans
    lines: list[str] = []
    for index, item in enumerate(evidence, 1):
        entries: list[str] = []
        for span_id, span in spans.items():
            if not span_id.startswith(f"source_{index}_"):
                continue
            details = [span.label or "原文窗口"]
            if not span_id.endswith("_body"):
                details.append(f"原文偏移={span.start}-{span.end}")
                if span.quote:
                    details.append(f"原文片段={json.dumps(span.quote, ensure_ascii=False)}")
            entries.append(f"{span_id}（{'；'.join(details)}）")
        if entries:
            lines.append(
                f"- 证据{index}：article_id={item.article.article_id}；"
                f"part_no={item.article.part_no}；" + "；".join(entries)
            )
    return "\n".join(lines)


def build_repair_messages(
    base_messages: list[dict[str, str]],
    assistant_content: str,
    drafts: list[Any],
    problems: list[QuoteProblem],
    *,
    spans: dict[str, SourceSpan] | None = None,
    span_ids_by_claim: dict[str, list[str]] | None = None,
    contract_error: str = "",
    candidates_by_claim: dict[str, ClaimCandidates] | None = None,
) -> list[dict[str, str]]:
    lines = [
        "上面的 claims JSON 中有 citation 不符合输出契约。请只重写下列失败 claim 的 citations，"
        "不要解释，不要输出 Markdown 代码块。",
        "严格引用契约：",
        '1. 每个 citation 必须且只能是一个对象 {"quote_id":"..."}；系统按ID取回原文连续子串。'
        "不得输出 article_id，不得输出 quote 或任何重抄正文。",
        "2. quote_id 必须来自上方证据窗口ID或下方“可用 quote_id”；未知ID、重复ID 会使整次修复失败。",
        "3. 引用必须在原文中连续；不得用省略号，不得跳过中间内容，不得把不相邻的句子拼接。",
        "4. 数字、金额、期限、角色必须由所引窗口正文支持；仅适用主体和入学届别可由可信元数据支持。",
        "5. 无法用现有窗口ID支持时，删除该 claim，不要保留无法定位的 quote。",
        '6. 只输出失败 claim；claim_id 必须来自失败项且不得重复。结构仍为 {"claims": [...]}。',
        "7. 推荐窗口只是已有原文候选；最终引用仍须由该登记窗口原文覆盖 claim 的数字/期限，"
        "不得把裸数字、表头或其他行推断成本条事实。",
    ]
    if contract_error:
        lines.extend(["", f"上一次修复未通过严格引用契约：{contract_error}"])
    lines.extend(["", "失败项："])
    for problem in problems:
        lines.append(f"- {problem.claim_id}：{'；'.join(problem.reasons)}")
    if candidates_by_claim:
        lines.extend(["", "可用 quote_id（只能从中选择，不得改写）："])
        for claim_id, candidates in candidates_by_claim.items():
            lines.extend(_candidate_catalog_lines(claim_id, candidates, spans))
    elif span_ids_by_claim:
        lines.extend(["", "可用 quote_id（只能从中选择，不得改写）："])
        for claim_id, span_ids in span_ids_by_claim.items():
            if not span_ids:
                lines.append(f"- {claim_id}：无可用窗口，请删除该 claim")
                continue
            entries = "；".join(_span_catalog_entry(span_id, spans) for span_id in span_ids)
            lines.append(f"- {claim_id}：{entries}")
    lines.extend(
        [
            "",
            "上一轮输出已省略，避免模型复述旧 quote；请依据证据窗口ID重写。"
            f"（原输出 {len(assistant_content)} 字）",
        ]
    )
    return [*base_messages, {"role": "user", "content": "\n".join(lines)}]


def merge_repairs(
    original: list[Any],
    repaired: list[Any],
    problems: list[QuoteProblem],
) -> QuoteRepairResult:
    failing = {problem.index: problem for problem in problems}
    if not repaired:
        # 模型明确返回空 claims：按“无法修复则丢弃”处理失败项，保留其他 claim。
        if not failing:
            return QuoteRepairResult(drafts=list(original), changed=False)
        merged = [draft for index, draft in enumerate(original) if index not in failing]
        return QuoteRepairResult(drafts=merged, changed=True)
    by_id = {item.claim_id: item for item in repaired if item.claim_id}
    same_length = len(repaired) == len(original)

    merged: list[Any] = []
    changed = False
    for index, draft in enumerate(original):
        problem = failing.get(index)
        if problem is None:
            merged.append(draft)
            continue
        replacement = by_id.get(draft.claim_id) if draft.claim_id else None
        if replacement is None and same_length:
            replacement = repaired[index]
        if replacement is None:
            # 修复输出删除了该 claim：按契约丢弃。
            changed = True
            continue
        merged.append(replacement)
        changed = True
    return QuoteRepairResult(drafts=merged, changed=changed)


def remaining_problem_ids(drafts: list[Any], evidence: list[Evidence]) -> list[str]:
    return [problem.claim_id for problem in find_quote_problems(drafts, evidence)]
