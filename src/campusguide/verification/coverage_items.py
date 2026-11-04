"""Deterministic required list items from explicit source lists and course rows."""

from __future__ import annotations

import re
from dataclasses import dataclass

from campusguide.agents.plan import QuestionUnit
from campusguide.domain.enums import Audience
from campusguide.domain.models import Article, Evidence
from campusguide.domain.source_context import trusted_context
from campusguide.domain.text import normalize_text
from campusguide.retrieval.source_spans import (
    StructuredTableRow,
    source_spans,
    structured_table_row,
)

_LIST_SPLIT = re.compile(r"[、，,；;。]")
_ITEM_NOISE = re.compile(
    r"最为核心|最核心|门左右|门以上|门以内|不少于\d+门|\d+\s*[至到~-]\s*\d+\s*门|等$|^重要$|^核心$|^主要$|^基本$"
)
_YEAR_SCOPE = re.compile(r"((?:20\d{2}\s*(?:[、,，和与及—–-]\s*)?)+)\s*(?:版|级)")
_RAW_YEAR = re.compile(r"20\d{2}")
_COURSE_ASPECTS = frozenset({"核心课程", "实践课程", "选修课程"})
_MEMBER_LIST_QUERY = re.compile(r"哪些|谁|列出|名单|成员|人选|分别|有哪|姓名")
_MEMBER_ASPECT = re.compile(r"成员|名单|人员|人选")
_MEMBER_HEADER = re.compile(r"姓名|成员(?:名单|姓名)?$")
_ROLE_HEADER = re.compile(r"分工|职务|角色")
_MEMBER_FALLBACK = re.compile(r"成员|人员|人选|谁|哪些人|哪几位")
_NAME_SPLIT = re.compile(r"[、,，;；/／\n]+")
_NAME_PLACEHOLDER = re.compile(r"^(?:无|暂无|略|/|—|－|-|详见附件|见附件|待定|未定|空)$")
_COMPOUND_SURNAMES = frozenset(
    {
        "欧阳",
        "司马",
        "上官",
        "诸葛",
        "东方",
        "皇甫",
        "尉迟",
        "公孙",
        "慕容",
        "令狐",
        "宇文",
        "长孙",
        "夏侯",
        "独孤",
        "南宫",
        "西门",
        "第五",
    }
)


def _years_in(text: str) -> set[int]:
    years: set[int] = set()
    for match in _YEAR_SCOPE.finditer(text or ""):
        years.update(int(year) for year in _RAW_YEAR.findall(match.group(1)))
    return years


def _unit_version_years(unit: QuestionUnit) -> set[int]:
    """Plan version only; cohort is a separate scope and never a substitute."""
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
    # An unmarked base plan belongs to the general cohort, not an innovation class.
    return "普通" in " ".join(own)


def _scoped_evidence(
    unit: QuestionUnit,
    evidence: list[Evidence],
    class_terms: dict[str, tuple[str, ...]] | None,
) -> list[Evidence]:
    """Only sources whose document subject/version/class can belong to this unit."""
    scoped = []
    for item in evidence:
        article = item.article
        detail = article.audience_detail
        context = trusted_context(article)
        scope = " ".join(
            [article.doc_title, *article.chapter_path, *context["majors"], *context["colleges"]]
        )
        if unit.subject and unit.subject not in scope:
            continue
        if (
            unit.cohort
            and detail
            and detail.intake_years
            and unit.cohort not in detail.intake_years
        ):
            continue
        # Classes and plan versions are separate scopes: a list from the general
        # cohort or another plan year cannot become the required list of this unit.
        class_context = " ".join([scope, article.article_no])
        if not _class_scope_ok(unit, class_context, class_terms):
            continue
        asked_versions = _unit_version_years(unit)
        source_versions = _article_version_years(article)
        if asked_versions and source_versions and not (asked_versions & source_versions):
            continue
        scoped.append(item)
    return scoped


def _clean_item(label: str) -> str:
    label = label.strip(" 。\n\t·•●○-—、）)（(")
    label = re.sub(r"^[（(]?\d{1,2}[）)、.]\s*", "", label)
    label = re.sub(r"[（(][^）)]*[）)]", "", label).strip()
    return label


def _course_list_items(
    unit: QuestionUnit,
    scoped: list[Evidence],
    aspects: set[str],
) -> list[str]:
    required = []
    for item in scoped:
        article = item.article
        if "核心课程" in aspects and "核心课程" in article.article_no:
            text = re.sub(r"[。；;]\s*$", "", article.text.strip())
            text = re.sub(r"^.*?(?:包括|为|：|:)", "", text)
            items = _LIST_SPLIT.split(text)
            if len(items) >= 2:
                for label in items:
                    # 中文句号后的说明句（“最为核心的5至8门…”“等”）不是课程名。
                    if _ITEM_NOISE.search(label):
                        continue
                    label = _clean_item(label)
                    if 2 <= len(label) <= 35 and not re.search(
                        r"学分|要求|应当|本专业|培养|环节|模块", label
                    ):
                        if re.fullmatch(r"(?:专业)?(?:核心|选修|实践|通识)?课程(?:设置|安排)?", label):
                            continue
                        required.append(label)
    for row in source_spans(scoped).values():
        if "_row_" not in row.span_id:
            continue
        # 支撑矩阵的行说明课程对毕业要求的支撑关系，不是开设课程清单。
        if row.is_matrix:
            continue
        # 新ingestion的非课程表行（如奖助等级表）不是培养方案的课程清单。
        if row.structured and not row.curriculum_row:
            continue
        label = row.label.strip()
        if _ITEM_NOISE.search(label):
            continue
        if "实践课程" in aspects:
            practical = bool(re.search(r"实验|实习|实践|毕业(?:论文|设计)|训练|劳动教育", label))
            if "集中实践" in unit.original_span and "实验" in label:
                practical = False
            if practical:
                required.append(label)
        if "选修课程" in aspects and row.module:
            module = re.sub(r"方向$|选修模块$|模块$", "", row.module).strip()
            if (
                len(module) >= 2
                and module in unit.original_span
                and ("实验" not in label or "实验" in unit.original_span)
            ):
                required.append(label)
    return list(dict.fromkeys(required))


def required_course_items(
    unit: QuestionUnit,
    evidence: list[Evidence],
    *,
    class_terms: dict[str, tuple[str, ...]] | None = None,
) -> list[str]:
    aspects = set(unit.requested_aspects)
    if not aspects & _COURSE_ASPECTS:
        return []
    return _course_list_items(
        unit, _scoped_evidence(unit, evidence, class_terms), aspects
    )


@dataclass(frozen=True)
class _MemberColumns:
    role: int
    member: int


def _column_index(row: StructuredTableRow, *, role: bool) -> int | None:
    best: tuple[int, int] | None = None
    for column in row.columns:
        compact = normalize_text("".join(column.path))
        leaf = normalize_text(column.path[-1]) if column.path else ""
        if role:
            if leaf in {"分工", "职务", "角色"}:
                score = 2
            elif _ROLE_HEADER.search(compact):
                score = 1
            else:
                continue
        elif leaf == "姓名" or leaf.endswith("姓名"):
            score = 3
        elif leaf.endswith("成员") or leaf in {"成员名单"} or "姓名" in compact:
            score = 2
        elif _MEMBER_HEADER.search(compact):
            score = 1
        else:
            continue
        if best is None or score > best[0]:
            best = (score, column.index)
    return best[1] if best is not None else None


def _member_columns(row: StructuredTableRow) -> _MemberColumns | None:
    role = _column_index(row, role=True)
    member = _column_index(row, role=False)
    if role is None or member is None or role == member:
        return None
    return _MemberColumns(role, member)


def _role_key(label: str) -> str:
    label = re.sub(r"[（(][^）)]*[）)]", "", label or "")
    return normalize_text(label).strip("：:·-—")


def _role_in_query(role: str, query: str) -> bool:
    if not role:
        return False
    start = 0
    while True:
        index = query.find(role, start)
        if index < 0:
            return False
        after = query[index + len(role) : index + len(role) + 1]
        # “评审委员会”里的“委员”是机构名，不是用户明确点名的职务值。
        if not (role.endswith("委员") and after == "会"):
            return True
        start = index + 1


def _row_role_key(
    row: StructuredTableRow,
    article: Article,
    role_index: int,
    rows_by_index: dict[int, tuple[Article, StructuredTableRow]],
) -> str:
    value = row.values[role_index] if role_index < len(row.values) else ""
    cell = next(
        (
            item
            for item in article.cell_provenance
            if item.row == row.row_index and item.col == role_index
        ),
        None,
    )
    if cell is None:
        return ""
    if not cell.covered:
        return _role_key(value) if value else ""
    if cell.origin_col != role_index:
        return ""
    origin = rows_by_index.get(cell.origin_row)
    if origin is None:
        return ""
    origin_article, origin_row = origin
    origin_cell = next(
        (
            item
            for item in origin_article.cell_provenance
            if item.row == origin_row.row_index and item.col == role_index
        ),
        None,
    )
    if origin_cell is None or origin_cell.covered:
        return ""
    origin_value = (
        origin_row.values[role_index] if role_index < len(origin_row.values) else ""
    )
    return _role_key(origin_value) if origin_value else ""


def _select_roles(unit: QuestionUnit, roles: list[str]) -> set[str] | None:
    query = normalize_text(unit.original_span or "")
    matched = [role for role in roles if _role_in_query(role, query)]
    if not matched:
        return None
    selected: list[str] = []
    for role in sorted(matched, key=len, reverse=True):
        if all(role not in other for other in selected):
            selected.append(role)
    return set(selected)


def _member_intent(unit: QuestionUnit, roles: list[str]) -> bool:
    span = unit.original_span or ""
    compact = normalize_text(span)
    if not compact or not _MEMBER_LIST_QUERY.search(compact):
        return False
    if any(_role_in_query(role, compact) for role in roles):
        return True
    aspects = [aspect for aspect in unit.requested_aspects if aspect]
    stripped_span = span.strip("？?。 ")
    fallback = bool(aspects) and all(
        aspect.strip("？?。 ") == stripped_span for aspect in aspects
    )
    if fallback:
        return bool(_MEMBER_FALLBACK.search(compact))
    return any(_MEMBER_ASPECT.search(normalize_text(aspect)) for aspect in aspects)


def _requested_audience_levels(unit: QuestionUnit) -> set[str]:
    terms = " ".join(unit.audience_scope)
    if not terms:
        return set()
    levels: set[str] = set()
    if re.search(r"本科|学士|undergraduate", terms, re.IGNORECASE):
        levels.add("undergraduate")
    if re.search(r"硕士|master", terms, re.IGNORECASE):
        levels.add("master")
    if re.search(r"博士|doctor", terms, re.IGNORECASE):
        levels.add("doctor")
    if re.search(r"研究生|graduate", terms, re.IGNORECASE) and not levels & {
        "master",
        "doctor",
    }:
        levels.add("graduate")
    return levels


def _article_audience_levels(article: Article) -> set[str]:
    levels: set[str] = set()
    for role in article.audience:
        if role == Audience.UNDERGRADUATE:
            levels.add("undergraduate")
        elif role == Audience.GRADUATE:
            levels.add("graduate")
        elif role == Audience.GRADUATE_MASTER:
            levels.add("master")
        elif role == Audience.GRADUATE_DOCTOR:
            levels.add("doctor")
    return levels


def _audience_scope_ok(unit: QuestionUnit, article: Article) -> bool:
    wanted = _requested_audience_levels(unit)
    if not wanted:
        return True
    levels = _article_audience_levels(article)
    if not levels:
        return True
    for level in wanted:
        if level == "undergraduate" and "undergraduate" in levels:
            return True
        if level == "graduate" and levels & {"graduate", "master", "doctor"}:
            return True
        if level in {"master", "doctor"} and (
            level in levels or "graduate" in levels
        ):
            return True
    return False


def _valid_name(text: str, *, latin: bool) -> str | None:
    text = text.strip(" 　\t\r\n。；;，,、/／-—－·")
    if not text:
        return None
    base = re.sub(r"[（(][^）)]*[）)]", "", text).strip()
    if not base:
        return None
    if latin:
        name = re.sub(r"\s+", " ", base).strip()
        if (
            len(name) < 2
            or len(name) > 30
            or not re.search(r"[A-Za-z]", name)
            or _NAME_PLACEHOLDER.fullmatch(name)
        ):
            return None
        return name
    compact = re.sub(r"\s+", "", base)
    if (
        len(compact) < 2
        or len(compact) > 30
        or _NAME_PLACEHOLDER.fullmatch(compact)
        or not re.fullmatch(r"[\u4e00-\u9fff·]+", compact)
    ):
        return None
    return compact


def _split_cjk_names(tokens: list[str]) -> list[str] | None:
    if any(not re.fullmatch(r"[\u4e00-\u9fff]+", token) for token in tokens):
        return None
    if len(tokens) == 1:
        return tokens
    if tokens[0] in _COMPOUND_SURNAMES:
        if len(tokens) == 2 and 2 <= len(tokens[0] + tokens[1]) <= 4:
            return [tokens[0] + tokens[1]]
        if len(tokens) == 3 and all(len(token) == 1 for token in tokens[1:]):
            combined = "".join(tokens)
            if len(combined) <= 4:
                return [combined]
    if all(len(token) == 1 for token in tokens):
        if len(tokens) in {2, 3}:
            return ["".join(tokens)]
        if len(tokens) % 2:
            return None
        pairs = ["".join(tokens[index : index + 2]) for index in range(0, len(tokens), 2)]
        if tokens[0] + tokens[1] in _COMPOUND_SURNAMES and len(pairs) >= 2:
            pairs = ["".join(tokens[:4]), *pairs[2:]]
        return pairs
    if all(2 <= len(token) <= 4 for token in tokens):
        return tokens
    names: list[str] = []
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if len(token) == 1:
            if index + 1 >= len(tokens):
                return None
            given = tokens[index + 1]
            if len(given) > 2:
                return None
            names.append(token + given)
            index += 2
        elif token in _COMPOUND_SURNAMES and index + 1 < len(tokens):
            given = tokens[index + 1]
            if len(given) > 2:
                return None
            names.append(token + given)
            index += 2
        elif 2 <= len(token) <= 4:
            names.append(token)
            index += 1
        else:
            return None
    return names


def _member_names(value: str) -> list[str]:
    names: list[str] = []
    for piece in _NAME_SPLIT.split(value or ""):
        piece = re.sub(r"[（(][^）)]*[）)]", "", piece).strip()
        if not piece:
            continue
        if re.search(r"[A-Za-z]", piece) and not re.search(r"[\u4e00-\u9fff]", piece):
            candidates: list[str] | None = [piece]
            latin = True
        else:
            tokens = piece.split()
            candidates = _split_cjk_names(tokens) if len(tokens) > 1 else [piece]
            latin = False
            if candidates is None:
                continue
        for candidate in candidates or []:
            name = _valid_name(candidate, latin=latin)
            if name and name not in names:
                names.append(name)
    return names


def _member_source_items(unit: QuestionUnit, evidence: list[Evidence]) -> list[str]:
    parsed: list[tuple[Article, StructuredTableRow, tuple[str, str, str]]] = []
    columns_by_table: dict[tuple[str, str, str], _MemberColumns | None] = {}
    for item in evidence:
        article = item.article
        if article.block_kind != "table_row":
            continue
        row = structured_table_row(article)
        if row is None or not row.reliable:
            continue
        table_key = (article.doc_id, article.version_id, row.table_id)
        if table_key not in columns_by_table:
            columns_by_table[table_key] = _member_columns(row)
        if columns_by_table[table_key] is None:
            continue
        parsed.append((article, row, table_key))
    if not parsed:
        return []
    rows_by_table: dict[
        tuple[str, str, str], dict[int, tuple[Article, StructuredTableRow]]
    ] = {}
    for article, row, table_key in parsed:
        if row.row_index is not None:
            rows_by_table.setdefault(table_key, {}).setdefault(
                row.row_index, (article, row)
            )
    candidates: list[tuple[StructuredTableRow, _MemberColumns, str]] = []
    for article, row, table_key in parsed:
        if not _audience_scope_ok(unit, article):
            continue
        columns = columns_by_table[table_key]
        if columns is None:
            continue
        role_key = _row_role_key(
            row, article, columns.role, rows_by_table.get(table_key, {})
        )
        if role_key:
            candidates.append((row, columns, role_key))
    if not candidates:
        return []
    roles = list(dict.fromkeys(role_key for _, _, role_key in candidates))
    if not _member_intent(unit, roles):
        return []
    selected = _select_roles(unit, roles)
    names: list[str] = []
    for row, columns, role_key in candidates:
        if selected is not None and role_key not in selected:
            continue
        value = row.values[columns.member] if columns.member < len(row.values) else ""
        for name in _member_names(value):
            if name not in names:
                names.append(name)
    return names


def required_source_items(
    unit: QuestionUnit,
    evidence: list[Evidence],
    *,
    class_terms: dict[str, tuple[str, ...]] | None = None,
) -> list[str]:
    scoped = _scoped_evidence(unit, evidence, class_terms)
    aspects = set(unit.requested_aspects)
    items = _course_list_items(unit, scoped, aspects) if aspects & _COURSE_ASPECTS else []
    items.extend(_member_source_items(unit, scoped))
    return list(dict.fromkeys(items))


def _missing_items(required: list[str], answer_text: str) -> list[str]:
    normalized = normalize_text(answer_text)
    return [item for item in required if normalize_text(item) not in normalized]


def missing_course_items(
    unit: QuestionUnit,
    evidence: list[Evidence],
    answer_text: str,
    *,
    class_terms: dict[str, tuple[str, ...]] | None = None,
) -> list[str]:
    return _missing_items(
        required_course_items(unit, evidence, class_terms=class_terms), answer_text
    )


def missing_source_items(
    unit: QuestionUnit,
    evidence: list[Evidence],
    answer_text: str,
    *,
    class_terms: dict[str, tuple[str, ...]] | None = None,
) -> list[str]:
    return _missing_items(
        required_source_items(unit, evidence, class_terms=class_terms), answer_text
    )
