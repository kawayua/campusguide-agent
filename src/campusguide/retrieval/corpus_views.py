"""Rebuildable source views for exact rosters, curriculum sections and dated collections.

Every result is an original Article, with unchanged body and offsets. This module
never imports evaluation data. It supplements recall; policy/scope gates still run.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from campusguide.agents.question_units import (
    cohort_years,
    mentioned_class_types,
    mentioned_level,
    mentioned_terms,
)
from campusguide.domain.models import Article
from campusguide.retrieval.profile_expand import ProfileLexicon
from campusguide.retrieval.sparse import tokenize

_NEWS = re.compile(r"(?m)^(20\d{2}-\d{2}-\d{2})[｜|]([^\n]+)[｜|](https?://\S+)\s*$")
# 本科 6 位、研究生 14 位课程代码；课程行以代码开头。
_COURSE_CODE_RE = re.compile(r"(?<!\d)(?:\d{14}|\d{6})(?!\d)")
_COURSE_ROW_RE = re.compile(r"(?<!\d)(?:\d{14}|\d{6})(?!\d)\s*[|｜]")
_STRUCTURED_ROW_KINDS = frozenset({"table_row", "matrix_row"})
_ROLES = {"教授", "副教授", "讲师", "博士后", "高级实验师", "实验师", "助理实验师"}
_STOP = {
    "什么",
    "哪些",
    "分别",
    "如何",
    "可以",
    "需要",
    "要求",
    "学生",
    "学校",
    "专业",
    "学院",
    "进行",
    "是否",
    "对于",
    "来讲",
    "我们",
    "情况",
    "规定",
    "华南",
    "农业大学",
    "还有",
    "有关",
    "多少",
}
_SECTION_PURPOSES = (
    (r"核心课", r"专业核心课程|核心课程"),
    (r"培养目标", r"培养目标"),
    (r"毕业要求", r"毕业要求"),
    (r"学制|授予|什么学位", r"学制|学位"),
    (r"毕业.*学分|总学分|学分.*分配", r"课程结构|毕业要求"),
    (
        r"实践|毕业设计|毕业论文|选修|科目|课程安排|课程设置",
        r"进程表|教学计划|课程设置|实践|课程结构",
    ),
    (r"研究方向", r"研究方向"),
)


def compact(text: str) -> str:
    return re.sub(r"\s+", "", text).lower()


_OTHER_LEVEL = {
    "undergraduate": re.compile(r"研究生|硕士|博士|学硕|专硕"),
    "graduate": re.compile(r"本科|学士|专科"),
}


def level_conflict(title: str, level: str | None) -> bool:
    """The document is a curriculum for the other education level."""
    pattern = _OTHER_LEVEL.get(level or "")
    return bool(pattern and pattern.search(title))


def is_supplement(title: str) -> bool:
    """分流/补充/修订说明不是完整培养方案。"""
    return bool(re.search(r"补充|分流|调整|修订|说明", title))


def roster_names(text: str) -> dict[str, tuple[int, int]]:
    """Join only isolated OCR name tokens; never join two complete names."""
    tokens = list(re.finditer(r"[\u3400-\u9fff]{1,5}", text))
    names: dict[str, tuple[int, int]] = {}
    i = 0
    while i < len(tokens):
        token = tokens[i]
        word = token.group()
        if word in _ROLES:
            i += 1
            continue
        if len(word) == 1 and i + 1 < len(tokens):
            nxt = tokens[i + 1]
            gap = text[token.end() : nxt.start()]
            if gap and gap.isspace() and len(gap) == 1 and len(nxt.group()) <= 2:
                word += nxt.group()
                names[word] = (token.start(), nxt.end())
                i += 2
                continue
        if 2 <= len(word) <= 4:
            names[word] = token.span()
        i += 1
    return names


@dataclass(frozen=True)
class NewsEntry:
    article: Article
    published_on: date
    title: str
    original_url: str
    start: int
    end: int


@dataclass
class SourceMatches:
    articles: list[Article] = field(default_factory=list)
    exact_keys: set[tuple[str, int]] = field(default_factory=set)
    warnings: list[str] = field(default_factory=list)
    collection_query: bool = False


class CorpusViews:
    def __init__(self, articles: list[Article], lexicon: ProfileLexicon, *, source_hash: str = ""):
        self.articles = articles
        # 表格父块只用于 article_by_id 水合，不参与结构化召回、候选上限或精确命中。
        self._searchable = [article for article in articles if article.is_indexable]
        self.lexicon = lexicon
        self.source_hash = source_hash
        self.news: dict[str, list[NewsEntry]] = {}
        self.headers: dict[str, Article] = {}
        self.rosters: list[tuple[Article, dict[str, tuple[int, int]]]] = []
        self._texts = {(a.article_id, a.part_no): compact(a.text) for a in articles}
        for article in self._searchable:
            match = _NEWS.fullmatch(article.text.strip())
            if match:
                self.news.setdefault(article.doc_id, []).append(
                    NewsEntry(
                        article,
                        date.fromisoformat(match[1]),
                        match[2].strip(),
                        match[3],
                        match.start(),
                        match.end(),
                    )
                )
            elif article.article_index == 1:
                self.headers[article.doc_id] = article
            if re.search(r"教师|师资", article.article_no) and len(article.text) < 12000:
                self.rosters.append((article, roster_names(article.text)))

    def article_by_id(self, article_id: str, part_no: int | None = None) -> Article | None:
        """按 article_id 读取文章，包括不进入向量索引的表格父块。"""
        matches = [
            article
            for article in self.articles
            if article.article_id == article_id
            and (part_no is None or article.part_no == part_no)
        ]
        if not matches:
            return None
        return min(matches, key=lambda article: article.part_no)

    @classmethod
    def load(cls, path: Path, lexicon: ProfileLexicon) -> CorpusViews:
        raw = path.read_bytes()
        view = cls(
            [Article.model_validate_json(line) for line in raw.splitlines() if line.strip()],
            lexicon,
            source_hash=hashlib.sha256(raw).hexdigest(),
        )
        view._source_path = path
        stat = path.stat()
        view._source_stamp = (stat.st_mtime_ns, stat.st_size)
        return view

    def search(self, query: str, *, limit: int = 16) -> SourceMatches:
        path = getattr(self, "_source_path", None)
        if path is not None:
            stat = path.stat()
            if (stat.st_mtime_ns, stat.st_size) != self._source_stamp:
                refreshed = self.load(path, self.lexicon)
                self.__dict__.update(refreshed.__dict__)
        result = SourceMatches(warnings=[f"SOURCE_VIEW_HASH:{self.source_hash}"])
        nq = compact(query)
        colleges = mentioned_terms(query, self.lexicon.college_terms)
        majors = mentioned_terms(query, self.lexicon.major_terms)
        college_terms = [self.lexicon.college_terms[key] for key, _ in colleges]
        major_terms = [self.lexicon.major_terms[key] for key, _ in majors]
        years = {str(y) for y in cohort_years(query)}
        is_plan = bool(re.search(r"培养|课程|学分|学制|选修|科目|毕业", query))
        # R3：层次与班型属于对象本身的一部分，本科表不能回答研究生实践，
        # 分流补充方案也不能替代创新班完整方案。
        level = mentioned_level(query)
        class_terms = [name for _, name in mentioned_class_types(query, self.lexicon)]
        asks_supplement = bool(re.search(r"补充|分流|调整|修订", query))

        def college_matches(article: Article) -> bool:
            if not college_terms:
                return True
            if article.audience_detail and set(article.audience_detail.colleges) & {
                key for key, _ in colleges
            }:
                return True
            return any(
                any(term in article.doc_title for term in terms if len(term) >= 3)
                for terms in college_terms
            )

        # Dated collections are traversed in full, independently of semantic top-K.
        temporal = bool(re.search(r"最近|最新|20\d{2}年\d{1,2}月", query))
        category = (
            "学术"
            if re.search(r"学术报告|学术动态|学术会议", query)
            else "讲座"
            if re.search(r"讲座|论坛", query)
            else "通知"
            if re.search(r"通知", query)
            else "新闻"
            if re.search(r"新闻", query)
            else "科研"
            if re.search(r"科研动态|科研进展", query)
            else ""
        )
        collection_docs = [
            doc
            for doc, rows in self.news.items()
            if college_matches(rows[0].article)
            and category
            and category in rows[0].article.doc_title
        ]
        if temporal and collection_docs:
            result.collection_query = True
            for doc in collection_docs:
                entries = self.news[doc]
                matches = list(entries)
                month = re.search(r"(20\d{2})年(\d{1,2})月", query)
                if month:
                    matches = [
                        e
                        for e in matches
                        if (e.published_on.year, e.published_on.month)
                        == (int(month[1]), int(month[2]))
                    ]
                days = re.findall(r"(?:月|和|与)(\d{1,2})日", query)
                if days:
                    matches = [
                        e for e in matches if e.published_on.day in {int(day) for day in days}
                    ]
                if re.search(r"最近|最新", query) and matches:
                    latest = max(e.published_on for e in matches)
                    matches = [e for e in matches if e.published_on == latest]
                matches.sort(key=lambda e: (e.published_on, e.title), reverse=True)
                header = self.headers.get(doc)
                if header:
                    result.articles.append(header)
                    result.exact_keys.add((header.article_id, header.part_no))
                for entry in matches:
                    result.articles.append(entry.article)
                    result.exact_keys.add((entry.article.article_id, entry.article.part_no))
                result.warnings.append(
                    f"COLLECTION_SCAN:{doc}:scanned={len(entries)}:matched={len(matches)}"
                )
            # A bounded output cannot assert collection completeness after truncation.
            if len(result.articles) > limit:
                result.warnings.append(
                    "COLLECTION_TRUNCATED:仅返回本轮预算内条目，不得宣称完整枚举"
                )
            result.articles = result.articles[:limit]
            return result
        # Exact roster lookup uses known entry boundaries plus the original heading.
        if re.search(r"老师|教师|哪个系|属于|分别.*系", query):
            for article, names in self.rosters:
                if not college_matches(article):
                    continue
                matching = [
                    name
                    for name in names
                    if re.search(
                        r"(?:^|[、，,]|和|与|及|老师|教师|请问|知道|查询|关于|的)"
                        + re.escape(name)
                        + r"(?=$|[、，,?？]|和|与|及|是|的|属|在|分|任|教授|老师|教师)",
                        nq,
                    )
                ]
                if matching:
                    result.articles.append(article)
                    result.exact_keys.add((article.article_id, article.part_no))
                    result.warnings.append(
                        f"ROSTER_LOOKUP:{article.article_id}:names={','.join(matching)}"
                    )
        # Supplement RRF with lexical title/heading/body relevance. This path still
        # requires the configured rerank threshold, except exact curriculum headings.
        terms = [t for t in set(tokenize(query)) if len(t) >= 2 and t not in _STOP]
        query_codes = set(_COURSE_CODE_RE.findall(query))
        # 同一文档/章节在候选循环里重复数百次；缓存只存活于本次 search，
        # key 用未归一化的原字符串，不跨 search/lexicon 复用。
        major_cache: dict[str, list[tuple[str, str]]] = {}

        def major_hits(text: str) -> list[tuple[str, str]]:
            if text not in major_cache:
                major_cache[text] = mentioned_terms(text, self.lexicon.major_terms)
            return major_cache[text]

        ranked: list[tuple[float, Article, bool]] = []
        for article in self._searchable:
            title = compact(article.doc_title)
            heading = compact(article.article_no)
            body = self._texts[(article.article_id, article.part_no)]
            chapter_title = " ".join(article.chapter_path)
            chapter_majors = major_hits(chapter_title)
            plan_title = (
                compact(chapter_title)
                if chapter_majors and len(major_hits(article.doc_title)) > 1
                else title
            )
            exact_plan = (
                is_plan
                and major_terms
                and any(
                    re.sub(r"[（()）]", "", compact(name)) in re.sub(r"[（()）]", "", plan_title)
                    for _, name in majors
                )
            )
            if (
                is_plan
                and major_terms
                and not exact_plan
                and re.search(r"培养方案", article.doc_title)
            ):
                continue
            if (
                is_plan
                and len(years) == 1
                and re.search(r"培养方案", article.doc_title)
                and next(iter(years)) not in article.version_id
            ):
                continue
            joined_title = article.doc_title + " " + " ".join(article.chapter_path)
            if (
                is_plan
                and level is not None
                and re.search(r"培养方案|教学计划", article.doc_title)
                and level_conflict(article.doc_title, level)
            ):
                continue
            if (
                is_plan
                and class_terms
                and re.search(r"培养方案", article.doc_title)
                and re.search(r"创新班|丁颖|实验班|国际班|普通班|方向班", joined_title)
                and not any(term in joined_title for term in class_terms)
            ):
                continue
            score = sum(
                (2.0 if term in title else 0)
                + (1.8 if term in heading else 0)
                + min(body.count(term), 3) * 0.25
                for term in terms
            )
            if is_plan and is_supplement(article.doc_title) and not asks_supplement:
                score -= 12
            direct = False
            if exact_plan:
                score += 5
                for intent, section in _SECTION_PURPOSES:
                    if re.search(intent, query) and re.search(section, article.article_no):
                        score += 7
                        direct = True
                if (
                    re.search(r"毕业设计|毕业论文", query)
                    and re.search(r"毕业(?:论文\s*[/／]?\s*设计|论文|设计)", body)
                    and re.search(r"进程表|教学计划", article.article_no)
                ):
                    score += 24
                    # Prefer an actual coded course row over a matrix mentioning
                    # the same course or a flattened PDF transcription.
                    if _COURSE_ROW_RE.search(article.text):
                        score += 10
                if re.search(r"实践|毕业设计|毕业论文", query) and re.search(
                    r"进程表|教学计划", article.article_no
                ):
                    if re.search(r"实验教学|通用技能实践|专业技能实践|集中实践", article.text):
                        score += 12
                    elif re.search(r"毕业实习|毕业论文|课程设计|实验", article.text):
                        score += 5
                    else:
                        score -= 8
                if "矩阵" in article.article_no and "矩阵" not in query:
                    score -= 15
                    direct = False
                module = re.search(r"([\u3400-\u9fff]{2,8})(?:选修组|方向)", query)
                if (
                    module
                    and module[1] in body
                    and re.search(r"进程表|教学计划", article.article_no)
                ):
                    score += 16
                    direct = True
            if query_codes and any(
                re.search(rf"(?<!\d){re.escape(code)}\s*[|｜]", article.text)
                for code in query_codes
            ):
                # 精确课程代码（含 14 位研究生代码）是实体命中，不再依赖分词。
                score += 30
                direct = True
            if colleges and college_matches(article):
                score += 1
            if is_supplement(article.doc_title) and not asks_supplement:
                direct = False
            if score > 0:
                ranked.append((score, article, bool(direct)))
        ranked.sort(key=lambda row: row[0], reverse=True)
        # Reserve one original fragment per document and requested section before
        # filling by lexical score. A long timetable must not evict the short
        # core-course/goal section, or every section of the comparison's other plan.
        protected: list[tuple[float, Article, bool]] = []
        seen_facets: set[tuple[str, str]] = set()
        intents = [
            (intent, section) for intent, section in _SECTION_PURPOSES if re.search(intent, query)
        ]
        for row in ranked:
            _, article, direct = row
            if not direct:
                continue
            for intent, section in intents:
                facet = (article.doc_id, intent)
                if re.search(section, article.article_no) and facet not in seen_facets:
                    protected.append(row)
                    seen_facets.add(facet)
        ordered = []
        ordered_keys = set()
        for row in [*protected, *ranked]:
            key = (row[1].article_id, row[1].part_no)
            if key not in ordered_keys:
                ordered.append(row)
                ordered_keys.add(key)
        existing = {(a.article_id, a.part_no) for a in result.articles}
        for _, article, direct in ordered[: max(0, limit - len(existing))]:
            key = (article.article_id, article.part_no)
            if key not in existing:
                result.articles.append(article)
                existing.add(key)
            if direct:
                result.exact_keys.add(key)
        # Child rows can start two chunks after their table header. Supply the
        # nearest original header as separate evidence; never fabricate a joined
        # quote or alter stored offsets. This works without rebuilding the index.
        expanded = []
        expanded_keys = set()
        for article in result.articles:
            key = (article.article_id, article.part_no)
            if key not in expanded_keys:
                expanded.append(article)
                expanded_keys.add(key)
            # 结构化行自带表头/列路径，无需再补父表或旧表头片段。
            if article.block_kind in _STRUCTURED_ROW_KINDS or article.table_id:
                continue
            if not _COURSE_ROW_RE.search(article.text):
                continue
            headers = [
                a
                for a in self.articles
                if a.article_id == article.article_id
                and a.part_no <= article.part_no
                and all(word in a.text for word in ("课程名称", "学分", "学时"))
                and re.search(r"[|｜]", a.text)
            ]
            if not headers:
                continue
            header = max(headers, key=lambda a: a.part_no)
            header_key = (header.article_id, header.part_no)
            if header_key not in expanded_keys:
                expanded.append(header)
                expanded_keys.add(header_key)
                if key in result.exact_keys:
                    result.exact_keys.add(header_key)
                result.warnings.append(
                    f"TABLE_HEADER_CONTEXT:{article.article_id}:p{article.part_no}:header=p{header.part_no}"
                )
        result.articles = expanded[:limit]
        result.exact_keys.intersection_update({(a.article_id, a.part_no) for a in result.articles})
        return result
