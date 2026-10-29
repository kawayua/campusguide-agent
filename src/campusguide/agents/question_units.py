"""Grounded question decomposition; no corpus answers or case identifiers."""

from __future__ import annotations

import re

from campusguide.agents.plan import QuestionUnit
from campusguide.retrieval.profile_expand import ProfileLexicon

# Each aspect is independently checked after claim verification.
ASPECTS: tuple[tuple[str, str], ...] = (
    ("核心课程", r"核心课"),
    ("实践课程", r"实践课程|实践环节|集中实践|实践教学|实践要求"),
    ("选修课程", r"选修|科目"),
    ("毕业设计", r"毕业设计|毕业论文"),
    ("培养目标", r"培养目标"),
    ("课程设置", r"课程设置|课程安排|课程体系"),
    ("毕业要求", r"毕业要求"),
    ("毕业学分", r"毕业.{0,8}学分|总学分|毕业学分"),
    ("学制", r"学制"),
    ("授予学位", r"授予.{0,6}学位|什么学位"),
    ("研究方向", r"研究方向"),
    ("申请条件", r"条件|资格|适用|哪些学生|哪些作品|申报范围"),
    ("办理程序", r"如何申请|如何办理|怎样申请|怎样办理|办理程序|什么程序|申请流程|怎么申请"),
    ("提交材料", r"材料|提交|交什么"),
    ("截止时间", r"截止"),
    ("成绩记载", r"成绩.*记载|成绩.*登记"),
    ("证书", r"证书|结业证"),
    ("定义区别", r"区别|定义"),
    ("负责人", r"谁|负责人|主任|院长|书记"),
    ("师资归属", r"哪个系|哪.*教师|老师吗|老师嘛"),
    ("发布日期", r"哪天发布|何时发布|什么时候发布|发布于|哪天|发布日期"),
)


def mentioned_terms(text: str, registry: dict[str, tuple[str, ...]]) -> list[tuple[str, str]]:
    """Longest reliable registered names; short major aliases never match inside words."""
    found: list[tuple[int, int, str, str]] = []
    for key, terms in registry.items():
        for term in terms:
            if len(term) < 2 or (len(term) == 2 and term != terms[0]):
                continue
            for match in re.finditer(re.escape(term), text):
                if term == "农学" and text[match.end() :].startswith("院"):
                    continue
                found.append((match.start(), match.end(), key, term))
    chosen: list[tuple[int, int, str, str]] = []
    for item in sorted(found, key=lambda x: (-(x[1] - x[0]), x[0])):
        if not any(item[0] < other[1] and other[0] < item[1] for other in chosen):
            chosen.append(item)
    return list(dict.fromkeys((key, name) for _, _, key, name in sorted(chosen)))


def cohort_years(text: str) -> list[int]:
    """Include shared suffixes such as 2025、2026版 and 2025—2026级."""
    years: list[int] = []
    for match in re.finditer(r"20\d{2}(?:\s*[、和与及—–-]\s*20\d{2})*\s*(?:年入学|级|版)", text):
        years.extend(int(y) for y in re.findall(r"20\d{2}", match.group()))
    return list(dict.fromkeys(years))


_RANGE_TOKEN = re.compile(r"20\d{2}\s*[—–-]\s*20\d{2}\s*(?:级|版)?")
_YEAR_TOKEN = re.compile(r"20\d{2}\s*(?:级|版)")


def attached_span(text: str, obj: str) -> tuple[int | None, str | None]:
    """Version attached to one object: a single year or an explicit range.

    “2025—2026级工业工程与管理” is one range, not two cohorts (报告 A4).
    """
    for match in re.finditer(re.escape(obj), text):
        head = text[: match.start()]
        ranges = list(_RANGE_TOKEN.finditer(head))
        years = list(_YEAR_TOKEN.finditer(head))
        if ranges and (not years or ranges[-1].end() >= years[-1].start()):
            start, end = re.findall(r"20\d{2}", ranges[-1].group())
            return None, f"{start}-{end}"
        if years:
            token = years[-1]
            before = head[token.start() - 1 : token.start()]
            if before in ("、", "和", "与", "及"):
                # 列举式“2025、2026版”：保留多年，不压成单一年份。
                return None, None
            year = int(re.findall(r"20\d{2}", token.group())[0])
            return year, str(year)
    return None, None


GRADUATE_LEVEL = re.compile(r"研究生|硕士|博士|学硕|专硕|非全日制|MBA|MPA")
UNDERGRADUATE_LEVEL = re.compile(r"本科|学士|大[一二三四五]|专升本")


def mentioned_level(text: str) -> str | None:
    """培养层次：本科 / 研究生；两者同时出现表示比较，不做层次限制。"""
    graduate = bool(GRADUATE_LEVEL.search(text))
    undergraduate = bool(UNDERGRADUATE_LEVEL.search(text))
    if graduate and not undergraduate:
        return "graduate"
    if undergraduate and not graduate:
        return "undergraduate"
    return None


def mentioned_class_types(
    text: str, lexicon: ProfileLexicon | None = None
) -> list[tuple[str, str]]:
    """班型（创新班、实验班、普通班等）；同一专业不同班型培养方案不通用。"""
    if lexicon is None or not lexicon.class_terms:
        return []
    return mentioned_terms(text, lexicon.class_terms)


def class_level(class_id: str, lexicon: ProfileLexicon | None) -> str | None:
    if lexicon is None:
        return None
    return lexicon.class_levels.get(class_id)


def decompose_question(text: str, lexicon: ProfileLexicon | None = None) -> list[QuestionUnit]:
    lexicon = lexicon or ProfileLexicon({}, {})
    majors = mentioned_terms(text, lexicon.major_terms)
    colleges = mentioned_terms(text, lexicon.college_terms)
    curriculum = bool(re.search(r"培养|课程|学分|学制|学位|选修|科目|毕业要求", text))
    objects = [name for _, name in (majors if curriculum and majors else colleges)]
    concepts = list(
        dict.fromkeys(
            re.findall(r"微专业|辅修学士学位|辅修专业|国家励志奖学金|国家奖学金|国家助学金", text)
        )
    )
    if len(concepts) > 1:
        objects = concepts
    years = cohort_years(text)
    aspects = [name for name, pattern in ASPECTS if re.search(pattern, text)]
    if "核心课程" in aspects and "课程设置" in aspects:
        aspects.remove("课程设置")
    # 培养方案比较题不需要额外的“定义区别”单元（报告 A4 / 041）。
    if "定义区别" in aspects and re.search(r"培养目标|课程|学分|学制", text):
        aspects.remove("定义区别")
    if not aspects:
        aspects = [text.strip("？?。 ")]
    shape = "fact"
    if re.search(r"哪些|分别|分别是|有什么", text):
        shape = "list"
    if re.search(r"区别|差异|不同|变化", text):
        shape = "comparison"
    if re.search(r"多少个|多少位|共.*多少", text):
        shape = "count"
    if re.search(r"最近|最新", text):
        shape = "latest"
    if re.search(r"流程|办理|程序", text):
        shape = "procedure"
    # Build object x version x aspect, with one shared original source span.
    # Versions with explicitly attached different objects are not cross-multiplied.
    subjects: list[tuple[str, int | None, str | None]] = []
    for obj in objects or [""]:
        obj_years = years
        version: str | None = None
        if obj:
            year, attached = attached_span(text, obj)
            if attached is not None:
                obj_years = [year] if year is not None else [None]
                version = attached
        if version is None and len(objects) > 1 and len(years) > 1:
            local = re.search(r"(20\d{2})\s*(?:版|级).*?" + re.escape(obj), text)
            if local and not any(other in local.group() for other in objects if other != obj):
                obj_years = [int(local.group(1))]
        subjects.extend((obj, year, version) for year in obj_years or [None])
    units: list[QuestionUnit] = []
    audience = [word for word in ("本科", "研究生") if word in text]
    level = mentioned_level(text)
    for obj, year, version in subjects:
        for aspect in aspects:
            units.append(
                QuestionUnit(
                    unit_id=f"u{len(units) + 1}",
                    original_span=text,
                    subject=obj,
                    objects=[obj] if obj else [],
                    audience_scope=audience or ([level] if level else []),
                    cohort=year,
                    version=version or (str(year) if year else None),
                    requested_aspects=[aspect],
                    answer_shape=shape,
                )
            )
    # No silent deletion: excessive complexity is explicitly retained as a missing slot.
    if len(units) > 12:
        units = [
            *units[:11],
            QuestionUnit(
                unit_id="u12",
                original_span=text,
                requested_aspects=["超出本轮预算的其余对象/方面"],
                required_slots=["请拆分问题"],
                ambiguity="UNIT_BUDGET_EXCEEDED",
            ),
        ]
    return units


def query_profile(text: str, lexicon: ProfileLexicon) -> dict:
    if not re.search(r"培养|课程|学分|专业|选修|毕业|学制|科目", text):
        return {}
    profile: dict = {}
    years = set(cohort_years(text))
    if len(years) == 1:
        profile["intake_year"] = int(next(iter(years)))
    majors = mentioned_terms(text, lexicon.major_terms)
    if len(majors) == 1:
        profile["majors"] = [majors[0][0]]
    return {"profile": profile} if profile else {}
