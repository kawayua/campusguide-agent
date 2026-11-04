"""用户所问年度/学年的确定性适用性核对。

``as_of`` 只表示系统在何时可用哪些材料；它不能证明材料适用于用户点名的
未来年度。本模块只复用原文标题、正文适用语句和已验证的 audience_detail
scope，不从发布日期、版本号、截止日期或随机数字推断目标年度。
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from datetime import date
from typing import Literal

from campusguide.domain.enums import (
    FactKind,
    IssueScope,
    IssueSeverity,
    OutcomeCode,
    ScopeStatus,
)
from campusguide.domain.models import Article, Citation, Claim, Evidence, VerificationIssue
from campusguide.verification.facts import check_fact_coverage

TargetKind = Literal["calendar_year", "academic_year"]

_ACADEMIC_RANGE_RE = re.compile(
    r"(?<!\d)(20\d{2})\s*[-—–至]\s*(20\d{2}|\d{2})\s*(?:学年|学年度)(?!\d)"
)
_ACADEMIC_SINGLE_RE = re.compile(r"(?<![\d\-—–])(20\d{2})\s*(?:学年|学年度)(?!\d)")
_YEAR_RE = re.compile(
    r"(?<![\d\-—–])(?=20\d{2}(?!\d))"
    r"20\d{2}\s*年"
    r"(?!\s*(?:级|届|版|入学|毕业|应届))"
)
_REQUEST_REJECT_AFTER = re.compile(
    r"^\s*(?:\d{1,2}\s*月(?:\d{1,2}\s*日)?[，,、\s]*)?"
    r"(?:修订|印发|发布|公布|公示|公告|出台|通过|编制|更新)"
)
_RELATIVE_YEAR_RE = re.compile(r"明年")
_RELATIVE_ACADEMIC_RE = re.compile(r"下一?学年|下个学年")

_BODY_AFTER_RE = re.compile(
    r"(?:适用于|适用|执行|施行|实施|收费标准|收费|标准|价格为|费用为|费为|按)"
    r"[^。；\n]{0,24}?"
    r"(?<![\d\-—–])(20\d{2})(?:\s*[-—–至]\s*(20\d{2}|\d{2}))?"
    r"\s*(学年|学年度|年度|年)"
)
_BODY_BEFORE_RE = re.compile(
    r"(?<![\d\-—–])(20\d{2})(?:\s*[-—–至]\s*(20\d{2}|\d{2}))?"
    r"\s*(学年|学年度|年度|年)"
    r"[^。；\n]{0,12}?(?:起|开始)?"
    r"(?:执行|施行|实施|适用|收费|标准|价格为|费用为|费为|按)"
)
_BODY_REJECT_AFTER = re.compile(
    r"^\s*年?\s*(?:"
    r"(?:\d{1,2}\s*月(?:\d{1,2}\s*日)?[，,、\s]*)?"
    r"(?:修订|印发|发布|公布|公示|公告|通知|出台|通过|编制|更新)"
    r"|(?:\d{1,2}\s*月(?:\d{1,2}\s*日)?[，,、\s]*)?"
    r"(?:截止|申请|报名|提交|受理|评审|考试|笔试|面试|开放|关闭|结束)"
    r")"
)
_TITLE_REJECT_AFTER = re.compile(
    r"^\s*年?\s*(?:"
    r"(?:\d{1,2}\s*月(?:\d{1,2}\s*日)?[，,、\s]*)?"
    r"(?:修订|印发|发布|公布|公示|公告|通知|出台|通过|编制|更新)"
    r"|(?:\d{1,2}\s*月(?:\d{1,2}\s*日)?[，,、\s]*)?"
    r"(?:截止|申请|报名|提交|受理|评审|考试|笔试|面试|开放|关闭|结束)"
    r")"
)
_GENERIC_BUSINESS = frozenset(
    {
        "学校",
        "我校",
        "学生",
        "申请",
        "办理",
        "通知",
        "规定",
        "办法",
        "标准",
        "费用",
        "收费",
        "多少",
        "每份",
        "一份",
        "请问",
        "相关",
        "具体",
        "情况",
        "要求",
        "是否",
        "可以",
        "如何",
        "怎么",
    }
)
_BUSINESS_NOISE = (
    "请问",
    "我想问",
    "帮我",
    "学校",
    "我校",
    "每份",
    "多少元",
    "是多少",
    "是什么",
    "有哪些",
    "怎么",
    "如何",
    "是否",
    "可以",
    "需要",
    "标准",
    "规定",
    "通知",
    "政策",
    "费用",
    "收费",
    "价格",
    "金额",
)
_CLAUSE_BREAKS = "。；;\n，,"


@dataclass(frozen=True)
class RequestTarget:
    kind: TargetKind
    start_year: int
    end_year: int | None = None
    raw: str = ""
    relative: bool = False
    future: bool = False

    @property
    def key(self) -> tuple[TargetKind, int, int | None]:
        return (self.kind, self.start_year, self.end_year)

    def label(self) -> str:
        if self.kind == "academic_year":
            end = self.end_year or self.start_year + 1
            return f"{self.start_year}-{end}学年"
        return f"{self.start_year}年"


@dataclass
class RequestedScope:
    question: str
    as_of: date
    targets: list[RequestTarget] = field(default_factory=list)

    @property
    def future_targets(self) -> list[RequestTarget]:
        return [target for target in self.targets if target.future]

    @property
    def has_future(self) -> bool:
        return bool(self.future_targets)

    def missing_future_targets(self) -> list[RequestTarget]:
        return list(self.future_targets)


@dataclass(frozen=True)
class Applicability:
    status: Literal["matched", "mismatch", "unknown"]
    detail: str = ""
    matched_source: str = ""


@dataclass
class RequestFilterResult:
    evidence: list[Evidence] = field(default_factory=list)
    issues: list[VerificationIssue] = field(default_factory=list)
    reason_code: OutcomeCode | None = None
    dropped: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class _TimeScope:
    kind: TargetKind
    start_year: int
    end_year: int | None = None
    text: str = ""
    context: str = ""
    source: Literal["title", "body", "cycle"] = "body"


def _normalized(text: str) -> str:
    return unicodedata.normalize("NFKC", text or "")


def _compact(text: str) -> str:
    return re.sub(r"\s+", "", _normalized(text))


def _target_from_year(
    year: int,
    *,
    kind: TargetKind,
    end_year: int | None,
    raw: str,
    as_of: date,
    relative: bool = False,
) -> RequestTarget:
    if kind == "academic_year" and end_year is None:
        end_year = year + 1
    future = year > (as_of.year if kind == "calendar_year" else _academic_start(as_of))
    return RequestTarget(
        kind=kind,
        start_year=year,
        end_year=end_year,
        raw=raw,
        relative=relative,
        future=future,
    )


def _academic_start(as_of: date) -> int:
    return as_of.year if as_of.month >= 9 else as_of.year - 1


def _resolve_end_year(start: int, end_text: str) -> int:
    if len(end_text) == 4:
        return int(end_text)
    end = (start // 100) * 100 + int(end_text)
    return end + 100 if end < start else end


def _request_year_rejected(text: str, match_end: int) -> bool:
    return bool(_REQUEST_REJECT_AFTER.match(text[match_end : match_end + 12]))


def _target_relation(
    target: RequestTarget,
    as_of: date,
) -> Literal["past", "current", "future"]:
    reference = as_of.year if target.kind == "calendar_year" else _academic_start(as_of)
    if target.start_year > reference:
        return "future"
    if target.start_year < reference:
        return "past"
    return "current"


def _append_target(targets: list[RequestTarget], target: RequestTarget) -> None:
    if target.key not in {item.key for item in targets}:
        targets.append(target)


def parse_requested_scope(question: str, as_of: date | None = None) -> RequestedScope:
    """只解析用户明确点名的日历年/学年；届别、年级、版本、学制不在此列。"""
    as_of = as_of or date.today()
    raw_text = question or ""
    text = _normalized(raw_text)
    scope = RequestedScope(question=raw_text, as_of=as_of)

    academic_spans: list[tuple[int, int]] = []
    for match in _ACADEMIC_RANGE_RE.finditer(text):
        start = int(match.group(1))
        end = _resolve_end_year(start, match.group(2))
        academic_spans.append((match.start(), match.end()))
        if _request_year_rejected(text, match.end()):
            continue
        _append_target(
            scope.targets,
            _target_from_year(
                start,
                kind="academic_year",
                end_year=end,
                raw=match.group(0),
                as_of=as_of,
            ),
        )
    for match in _ACADEMIC_SINGLE_RE.finditer(text):
        if any(start <= match.start() < end for start, end in academic_spans):
            continue
        academic_spans.append((match.start(), match.end()))
        if _request_year_rejected(text, match.end()):
            continue
        _append_target(
            scope.targets,
            _target_from_year(
                int(match.group(1)),
                kind="academic_year",
                end_year=None,
                raw=match.group(0),
                as_of=as_of,
            ),
        )

    for match in _YEAR_RE.finditer(text):
        if any(start <= match.start() < end for start, end in academic_spans):
            continue
        if _request_year_rejected(text, match.end()):
            continue
        year_text = match.group(0)
        year = int(re.search(r"20\d{2}", year_text).group(0))
        _append_target(
            scope.targets,
            _target_from_year(
                year,
                kind="calendar_year",
                end_year=None,
                raw=year_text,
                as_of=as_of,
            ),
        )

    if _RELATIVE_YEAR_RE.search(text):
        _append_target(
            scope.targets,
            _target_from_year(
                as_of.year + 1,
                kind="calendar_year",
                end_year=None,
                raw="明年",
                as_of=as_of,
                relative=True,
            ),
        )
    relative = _RELATIVE_ACADEMIC_RE.search(text)
    if relative:
        _append_target(
            scope.targets,
            _target_from_year(
                _academic_start(as_of) + 1,
                kind="academic_year",
                end_year=_academic_start(as_of) + 2,
                raw=relative.group(0),
                as_of=as_of,
                relative=True,
            ),
        )
    return scope


def _parse_time_scope(
    year_text: str,
    start: int,
    end_text: str | None,
    suffix: str,
    *,
    context: str = "",
    source: Literal["title", "body", "cycle"] = "body",
) -> _TimeScope:
    kind: TargetKind = "academic_year" if suffix in {"学年", "学年度"} else "calendar_year"
    end_year = (
        _resolve_end_year(start, end_text)
        if end_text
        else (start + 1 if kind == "academic_year" else None)
    )
    return _TimeScope(
        kind=kind,
        start_year=start,
        end_year=end_year,
        text=year_text,
        context=context,
        source=source,
    )


def _title_scopes(title: str) -> list[_TimeScope]:
    title = _normalized(title)
    scopes: list[_TimeScope] = []
    consumed: list[tuple[int, int]] = []
    for match in _ACADEMIC_RANGE_RE.finditer(title):
        consumed.append((match.start(), match.end()))
        start = int(match.group(1))
        scopes.append(
            _TimeScope(
                kind="academic_year",
                start_year=start,
                end_year=_resolve_end_year(start, match.group(2)),
                text=match.group(0),
                context=title,
                source="title",
            )
        )
    for match in _ACADEMIC_SINGLE_RE.finditer(title):
        if any(start <= match.start() < end for start, end in consumed):
            continue
        consumed.append((match.start(), match.end()))
        scopes.append(
            _TimeScope(
                kind="academic_year",
                start_year=int(match.group(1)),
                end_year=int(match.group(1)) + 1,
                text=match.group(0),
                context=title,
                source="title",
            )
        )
    for match in _YEAR_RE.finditer(title):
        if any(start <= match.start() < end for start, end in consumed):
            continue
        after = title[match.end() : match.end() + 8]
        if _TITLE_REJECT_AFTER.match(after):
            continue
        year = int(re.search(r"20\d{2}", match.group(0)).group(0))
        scopes.append(
            _TimeScope(
                kind="calendar_year",
                start_year=year,
                end_year=None,
                text=match.group(0),
                context=title,
                source="title",
            )
        )
    return scopes


def _clause_context(text: str, start: int, end: int) -> str:
    left = max([text.rfind(char, 0, start) for char in _CLAUSE_BREAKS] + [-1]) + 1
    right_candidates = [
        index for char in _CLAUSE_BREAKS if (index := text.find(char, end)) >= 0
    ]
    right = min(right_candidates) if right_candidates else len(text)
    context = text[left:right].strip()
    head = context.rstrip()
    if head.endswith(("：", ":")) or head.endswith("如下"):
        next_start = right + 1
        while next_start < len(text) and text[next_start].isspace():
            next_start += 1
        next_candidates = [
            index
            for char in _CLAUSE_BREAKS
            if (index := text.find(char, next_start)) >= 0
        ]
        next_end = min(next_candidates) if next_candidates else len(text)
        context = (context + " " + text[next_start:next_end].strip()).strip()
    return context


def _body_scopes(text: str) -> list[_TimeScope]:
    text = _normalized(text)
    scopes: list[_TimeScope] = []
    seen: set[tuple[TargetKind, int, int | None, str]] = set()
    for pattern in (_BODY_AFTER_RE, _BODY_BEFORE_RE):
        for match in pattern.finditer(text):
            after_year = text[match.start(1) + len(match.group(1)) :]
            if _BODY_REJECT_AFTER.match(after_year):
                continue
            start = int(match.group(1))
            end_text = match.group(2)
            suffix = match.group(3)
            context = _clause_context(text, match.start(), match.end())
            item = _parse_time_scope(
                match.group(0),
                start,
                end_text,
                suffix,
                context=context,
            )
            if item.kind == "calendar_year" and end_text:
                continue
            key = (item.kind, item.start_year, item.end_year, item.context)
            if key not in seen:
                seen.add(key)
                scopes.append(item)
    return scopes


def _cycle_scope(article: Article) -> tuple[_TimeScope | None, bool]:
    detail = article.audience_detail
    if detail is None or not detail.cycle:
        return None, False
    start_text, _, end_text = detail.cycle.partition("-")
    if not start_text.isdigit():
        return None, False
    start = int(start_text)
    end = int(end_text) if end_text.isdigit() else start + 1
    scope = _TimeScope(
        kind="academic_year",
        start_year=start,
        end_year=end,
        text=f"audience_detail.cycle={detail.cycle}",
        source="cycle",
    )
    trusted = (
        article.metadata_source_status == "verified"
        and detail.scope_status is ScopeStatus.EXACT
        and bool((detail.scope_evidence or "").strip())
    )
    return scope, trusted


def _matches(scope: _TimeScope, target: RequestTarget) -> bool:
    return (
        scope.kind == target.kind
        and scope.start_year == target.start_year
        and scope.end_year == target.end_year
    )


def _business_text(text: str) -> str:
    cleaned = _ACADEMIC_RANGE_RE.sub("", _normalized(text))
    cleaned = _ACADEMIC_SINGLE_RE.sub("", cleaned)
    cleaned = _YEAR_RE.sub("", cleaned)
    cleaned = _RELATIVE_ACADEMIC_RE.sub("", cleaned)
    cleaned = _RELATIVE_YEAR_RE.sub("", cleaned)
    cleaned = re.sub(r"[^\u4e00-\u9fa5A-Za-z0-9]+", "", cleaned)
    for word in _BUSINESS_NOISE:
        cleaned = cleaned.replace(word, "")
    return cleaned


def _business_matches(question: str, context: str) -> bool:
    asked = _business_text(question)
    if len(asked) < 2:
        return True
    haystack = _normalized(context)
    for size in (4, 3):
        for index in range(0, len(asked) - size + 1):
            if asked[index : index + size] in haystack:
                return True
    for index in range(0, len(asked) - 1):
        gram = asked[index : index + 2]
        if gram in _GENERIC_BUSINESS:
            continue
        if gram in haystack:
            return True
    return False


def _scope_business_ok(scope: _TimeScope, question: str, article: Article) -> bool:
    if not question:
        return True
    context = (article.doc_title or "") if scope.source in {"title", "cycle"} else scope.context
    return _business_matches(question, context)


def _scope_covers_quote(scope: _TimeScope, quote: str) -> bool:
    context = _compact(scope.context)
    quoted = _compact(quote)
    if not context or not quoted:
        return False
    return quoted in context or context in quoted


def source_applicability(
    article: Article,
    target: RequestTarget,
    *,
    question: str = "",
    as_of: date | None = None,
) -> Applicability:
    """判断当前可用的单一来源是否明确适用于目标年度/学年。"""
    if as_of is not None and article.published_at > as_of:
        return Applicability(
            status="unknown",
            detail="来源发布日期晚于系统当前可用日期，不能证明当前已知标准",
        )

    title_scopes = _title_scopes(article.doc_title or "")
    body_scopes = _body_scopes(article.text or "")
    cycle_scope, cycle_trusted = _cycle_scope(article)
    explicit = [*title_scopes, *body_scopes]
    if cycle_scope is not None and cycle_trusted:
        explicit.append(cycle_scope)

    # 年度只有在自己的标题/局部条款中同时命中同一业务时才是适用性依据；
    # 同一文章其他段落提到的年份不能借给本条业务。
    candidates = [
        scope for scope in explicit if _scope_business_ok(scope, question, article)
    ]
    matches = [scope for scope in candidates if _matches(scope, target)]
    conflicts = [
        scope
        for scope in candidates
        if scope.kind == target.kind and not _matches(scope, target)
    ]
    if cycle_scope is not None and not cycle_trusted and not _matches(cycle_scope, target):
        conflicts.append(cycle_scope)

    if matches:
        return Applicability(
            status="matched",
            detail="来源明确适用于目标年度/学年：" + "、".join(scope.text for scope in matches[:3]),
            matched_source=matches[0].text,
        )
    if conflicts:
        detail = "来源含冲突的明示年度/学年：" + "、".join(
            scope.text for scope in conflicts[:3]
        )
        return Applicability(status="mismatch", detail=detail)
    return Applicability(
        status="unknown",
        detail="来源标题/正文未明确写出适用于该目标年度/学年",
    )


def _evidence_issue(evidence: Evidence, detail: str) -> VerificationIssue:
    return VerificationIssue(
        code="REQUESTED_SCOPE_MISMATCH",
        scope=IssueScope.EVIDENCE,
        severity=IssueSeverity.WARNING,
        article_id=evidence.article.article_id,
        part_no=evidence.article.part_no,
        detail=detail,
    )


def filter_evidence_for_request(
    evidence: list[Evidence],
    scope: RequestedScope,
) -> RequestFilterResult:
    """生成前过滤无法支撑所问年度/学年的证据。

    未来目标仍要求标题/正文给出正面明示且主题匹配；当前目标可接受无年份的
    现行通用政策（unknown），但不得混入明确写了其他年度的同主题来源；历史
    目标必须由明示历史年度来源支撑，不能拿当前未标年份的价格替代。若混问中
    的未来目标完全无依据，整批证据清空，避免只拿当前年度答案冒充完整回答。
    """
    if not scope.targets:
        return RequestFilterResult(evidence=list(evidence))

    future_targets = scope.future_targets
    kept: list[Evidence] = []
    issues: list[VerificationIssue] = []
    dropped: list[str] = []
    future_supported = False

    for item in evidence:
        if item.article.published_at > scope.as_of:
            dropped.append(item.article.article_id)
            issues.append(
                _evidence_issue(
                    item,
                    "来源发布日期晚于系统当前可用日期，不能证明当前已知标准",
                )
            )
            continue
        apps = [
            (
                target,
                source_applicability(
                    item.article,
                    target,
                    question=scope.question,
                    as_of=scope.as_of,
                ),
            )
            for target in scope.targets
        ]
        if any(app.status == "matched" for target, app in apps if target.future):
            kept.append(item)
            future_supported = True
            continue
        non_future_ok = any(
            app.status == "matched"
            or (
                app.status == "unknown"
                and _target_relation(target, scope.as_of) == "current"
            )
            for target, app in apps
            if not target.future
        )
        if non_future_ok:
            kept.append(item)
            continue
        dropped.append(item.article.article_id)
        detail = "; ".join(f"{target.label()}: {app.detail}" for target, app in apps)
        issues.append(_evidence_issue(item, detail))

    if future_targets and not future_supported:
        issues.append(
            VerificationIssue(
                code="REQUESTED_SCOPE_MISMATCH",
                scope=IssueScope.EVIDENCE,
                severity=IssueSeverity.WARNING,
                detail=requested_scope_message(scope),
            )
        )
        return RequestFilterResult(
            evidence=[],
            issues=issues,
            reason_code=OutcomeCode.VERSION_MISMATCH,
            dropped=dropped,
        )
    if evidence and not kept:
        issues.append(
            VerificationIssue(
                code="REQUESTED_SCOPE_MISMATCH",
                scope=IssueScope.EVIDENCE,
                severity=IssueSeverity.WARNING,
                detail=requested_scope_message(scope),
            )
        )
        return RequestFilterResult(
            evidence=[],
            issues=issues,
            reason_code=OutcomeCode.VERSION_MISMATCH,
            dropped=dropped,
        )
    return RequestFilterResult(evidence=kept, issues=issues, dropped=dropped)


def _claim_targets(claim: Claim, scope: RequestedScope) -> list[RequestTarget]:
    return parse_requested_scope(claim.text, scope.as_of).targets


def _citation_proof_texts(
    citation: Citation,
    article: Article,
    target: RequestTarget,
    scope: RequestedScope,
) -> list[str]:
    applicability = source_applicability(
        article,
        target,
        question=scope.question,
        as_of=scope.as_of,
    )
    quote = citation.quote or ""
    if applicability.status == "unknown" and _target_relation(target, scope.as_of) == "current":
        return [quote] if quote.strip() else []
    if applicability.status != "matched":
        return []

    quote_scopes = _body_scopes(quote)
    if quote_scopes:
        matching = [
            item
            for item in quote_scopes
            if _matches(item, target)
            and _scope_business_ok(item, scope.question, article)
        ]
        return list(dict.fromkeys(item.context for item in matching if item.context))

    title_matches = [
        item
        for item in _title_scopes(article.doc_title or "")
        if _matches(item, target) and _scope_business_ok(item, scope.question, article)
    ]
    if title_matches:
        return [quote] if quote.strip() else []

    body_matches = [
        item
        for item in _body_scopes(article.text or "")
        if _matches(item, target)
        and _scope_business_ok(item, scope.question, article)
        and _scope_covers_quote(item, quote)
    ]
    return list(dict.fromkeys(item.context for item in body_matches if item.context))


def _claim_supports_target(
    claim: Claim,
    target: RequestTarget,
    scope: RequestedScope,
    evidence: list[Evidence],
) -> bool:
    by_key = {item.key: item.article for item in evidence}
    claim_targets = _claim_targets(claim, scope)
    if claim_targets and target.key not in {item.key for item in claim_targets}:
        return False
    proof_texts: list[str] = []
    for citation in claim.citations:
        article = by_key.get((citation.article_id, citation.part_no))
        if article is None:
            continue
        proof_texts.extend(_citation_proof_texts(citation, article, target, scope))
    if not proof_texts:
        return False
    coverage = check_fact_coverage(claim.text, list(dict.fromkeys(proof_texts)))
    scope_years = {f"{target.start_year:04d}"}
    if target.end_year is not None:
        scope_years.add(f"{target.end_year:04d}")
    uncovered = [
        fact
        for fact in coverage.uncovered
        if not (fact.kind is FactKind.DATE and fact.canonical in scope_years)
    ]
    return not uncovered


def claim_scope_issues(
    claim: Claim,
    scope: RequestedScope,
    evidence: list[Evidence],
) -> list[VerificationIssue]:
    """按实际被引来源核对 claim，而不是看本轮是否碰巧检索到正确年度材料。"""
    if not scope.targets:
        return []
    requested_keys = {target.key for target in scope.targets}
    claim_targets = _claim_targets(claim, scope)
    mismatched = [target for target in claim_targets if target.key not in requested_keys]
    if mismatched:
        return [
            VerificationIssue(
                code="REQUESTED_SCOPE_MISMATCH",
                scope=IssueScope.CLAIM,
                severity=IssueSeverity.BLOCKING,
                claim_id=claim.claim_id,
                detail=(
                    "结论写出的目标年度与问题不一致："
                    + "、".join(target.label() for target in mismatched)
                ),
            )
        ]

    if claim_targets:
        targets = claim_targets
    elif scope.has_future:
        targets = scope.future_targets
    elif any(
        _claim_supports_target(claim, target, scope, evidence)
        for target in scope.targets
    ):
        return []
    else:
        targets = scope.targets

    missing = [
        target
        for target in targets
        if not _claim_supports_target(claim, target, scope, evidence)
    ]
    if not missing:
        return []
    return [
        VerificationIssue(
            code="REQUESTED_SCOPE_MISMATCH",
            scope=IssueScope.CLAIM,
            severity=IssueSeverity.BLOCKING,
            claim_id=claim.claim_id,
            detail=(
                "引用来源未明确适用于所问目标年度/学年："
                + "、".join(target.label() for target in missing)
            ),
        )
    ]


def missing_future_targets(
    claims: list[Claim],
    scope: RequestedScope,
    evidence: list[Evidence],
) -> list[RequestTarget]:
    if not scope.targets:
        return []
    return [
        target
        for target in scope.targets
        if not any(_claim_supports_target(claim, target, scope, evidence) for claim in claims)
    ]


def requested_scope_message(scope: RequestedScope) -> str:
    required = scope.future_targets or scope.targets
    labels = "、".join(target.label() for target in required)
    if not labels:
        labels = "所问年度"
    return (
        f"未能确认 {labels} 的适用依据：当前可检索材料没有明确写明适用于该目标年度/学年，"
        "系统不会用当前学年或未标注年份的标准替代。请提供该年度的正式通知/标准，"
        "或联系负责部门确认。"
    )


__all__ = [
    "Applicability",
    "RequestFilterResult",
    "RequestTarget",
    "RequestedScope",
    "claim_scope_issues",
    "filter_evidence_for_request",
    "missing_future_targets",
    "parse_requested_scope",
    "requested_scope_message",
    "source_applicability",
]
