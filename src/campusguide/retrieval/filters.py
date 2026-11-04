"""PolicyFilters / UserProfile -> Qdrant 过滤条件。

W2 只实现文章级硬过滤；W4 增加受众细目（学院、专业、入学年份、阶段、
培养类型、群体、学籍状态、学年）与版本模式。过滤语义与 W3 ``check_scope``
保持一致：文档细目为空 = 不限制；用户字段为空 = 不因该项过滤；
只排除“文档有约束且用户已知值与约束明确不匹配”的条款。
"""

from __future__ import annotations

from datetime import UTC, date, datetime, time
from typing import Any

from qdrant_client.models import (
    DatetimeRange,
    FieldCondition,
    Filter,
    IsEmptyCondition,
    IsNullCondition,
    MatchAny,
    MatchValue,
    Range,
)

from campusguide.domain.audience_match import expand_audience
from campusguide.domain.enums import ArticleStatus, Audience, VersionMode
from campusguide.domain.models import PolicyFilters
from campusguide.domain.profiles import UserProfile

PROFILE_FIELDS = (
    "colleges",
    "majors",
    "intake",
    "stages",
    "program_types",
    "groups",
    "status",
)


def _start_of_day(value: date) -> str:
    return datetime.combine(value, time.min, tzinfo=UTC).isoformat().replace("+00:00", "Z")


def _end_of_day(value: date) -> str:
    return datetime.combine(value, time.max, tzinfo=UTC).isoformat().replace("+00:00", "Z")


def academic_cycle(as_of: date) -> list[str]:
    """返回 as_of 所在学年的可匹配形式（9 月为新学年起点）。"""
    start = as_of.year if as_of.month >= 9 else as_of.year - 1
    return [str(start), f"{start}-{start + 1}"]


def _cycle_forms(value: str) -> list[str]:
    start = int(value.split("-", 1)[0])
    return [str(start), f"{start}-{start + 1}"]


def target_academic_cycle(filters: PolicyFilters) -> list[str]:
    """明确目标学年优先；否则按 as_of 推导，避免把“今天”硬套到招生/届别文件。"""
    if filters.academic_year:
        return _cycle_forms(filters.academic_year)
    if filters.profile is not None and filters.profile.academic_year:
        return _cycle_forms(filters.profile.academic_year)
    return academic_cycle(filters.as_of)


def _empty_or_match(key: str, values: list[Any]) -> Filter:
    return Filter(
        should=[
            IsEmptyCondition(is_empty={"key": key}),
            FieldCondition(key=key, match=MatchAny(any=values)),
        ]
    )


def _empty_or_value(key: str, value: Any) -> Filter:
    return Filter(
        should=[
            IsNullCondition(is_null={"key": key}),
            FieldCondition(key=key, match=MatchValue(value=value)),
        ]
    )


LEARNING_PROGRAM_VALUES = ("full_time", "part_time")
DEGREE_PROGRAM_VALUES = ("academic", "professional")


def _preferred_or_legacy(
    new_key: str,
    legacy_key: str,
    value: Any,
    *,
    legacy_domain_values: tuple[str, ...],
) -> Filter:
    """新字段优先、旧 ``program_types`` 仅在其确实描述该概念时兜底。

    旧字段把学习方式和培养类型混在一起：文档只写了 ``academic`` 时不应被
    “全日制”查询排除。因此只有旧字段含同类取值时才把它当作约束。
    """
    return Filter(
        should=[
            Filter(
                must=[
                    IsEmptyCondition(is_empty={"key": new_key}),
                    Filter(
                        must_not=[
                            FieldCondition(
                                key=legacy_key,
                                match=MatchAny(any=list(legacy_domain_values)),
                            )
                        ]
                    ),
                ]
            ),
            FieldCondition(key=new_key, match=MatchAny(any=[value])),
            FieldCondition(key=legacy_key, match=MatchAny(any=[value])),
        ]
    )


def effective_statuses(filters: PolicyFilters) -> list[ArticleStatus] | None:
    """按版本模式解析状态集合；None 表示不加 status 条件。"""
    if filters.status is not None:
        return list(filters.status)
    if filters.version_mode is VersionMode.CURRENT:
        return [ArticleStatus.ACTIVE]
    if filters.version_mode is VersionMode.HISTORICAL:
        return None
    if filters.version_mode is VersionMode.INCLUDE_REPEALED:
        return [ArticleStatus.ACTIVE, ArticleStatus.REPEALED]
    raise ValueError(f"未知版本模式：{filters.version_mode}")


def build_qdrant_filter(filters: PolicyFilters) -> Filter:
    """把领域过滤条件翻译成 Qdrant Filter。

    ``effective_from <= as_of`` 且 ``effective_to is null or >= as_of``；
    ``effective_to`` 在 payload 中显式存 null，因此用 IsNullCondition 处理长期有效。
    """
    must: list[Any] = [
        FieldCondition(
            key="block_type",
            match=MatchAny(any=[block_type.value for block_type in filters.block_types]),
        ),
    ]

    if not filters.ignore_effective_dates:
        must.append(
            FieldCondition(
                key="effective_from",
                range=DatetimeRange(lte=_end_of_day(filters.as_of)),
            )
        )

    statuses = effective_statuses(filters)
    if statuses:
        must.append(
            FieldCondition(
                key="status",
                match=MatchAny(any=[status.value for status in statuses]),
            )
        )
    if ArticleStatus.DRAFT not in (statuses or []):
        must.append(
            Filter(
                must_not=[
                    FieldCondition(
                        key="status",
                        match=MatchAny(any=[ArticleStatus.DRAFT.value]),
                    )
                ]
            )
        )

    if not filters.ignore_effective_dates:
        must.append(
            Filter(
                should=[
                    IsNullCondition(is_null={"key": "effective_to"}),
                    FieldCondition(
                        key="effective_to",
                        range=DatetimeRange(gte=_start_of_day(filters.as_of)),
                    ),
                ]
            )
        )

    if filters.audience:
        audiences = {
            audience.value for audience in expand_audience(list(filters.audience))
        }
        audiences.update({Audience.ALL.value, Audience.PUBLIC.value})
        must.append(
            FieldCondition(
                key="audience",
                match=MatchAny(any=sorted(audiences)),
            )
        )

    if filters.doc_ids:
        must.append(
            FieldCondition(
                key="doc_id",
                match=MatchAny(any=list(filters.doc_ids)),
            )
        )

    if filters.flow_ids:
        must.append(
            FieldCondition(
                key="flow_ids",
                match=MatchAny(any=list(filters.flow_ids)),
            )
        )

    profile = filters.profile
    if profile is not None and not profile.is_empty:
        if profile.colleges:
            must.append(_empty_or_match("audience_colleges", list(profile.colleges)))
        if profile.majors:
            must.append(_empty_or_match("audience_majors", list(profile.majors)))
        if profile.intake_year is not None:
            year = profile.intake_year
            must.append(
                Filter(
                    must=[
                        _empty_or_match("audience_intake_years", [year]),
                        Filter(
                            should=[
                                IsNullCondition(is_null={"key": "audience_intake_from"}),
                                FieldCondition(
                                    key="audience_intake_from",
                                    range=Range(lte=year),
                                ),
                            ]
                        ),
                    ]
                )
            )
        if profile.stage is not None:
            must.append(_empty_or_match("audience_stages", [profile.stage.value]))
        if profile.program_type is not None:
            must.append(_empty_or_match("audience_program_types", [profile.program_type.value]))
        if profile.learning_mode is not None:
            must.append(
                _preferred_or_legacy(
                    "audience_learning_modes",
                    "audience_program_types",
                    profile.learning_mode.value,
                    legacy_domain_values=LEARNING_PROGRAM_VALUES,
                )
            )
        if profile.degree_type is not None:
            must.append(
                _preferred_or_legacy(
                    "audience_degree_types",
                    "audience_program_types",
                    profile.degree_type.value,
                    legacy_domain_values=DEGREE_PROGRAM_VALUES,
                )
            )
        if profile.class_types:
            must.append(_empty_or_match("audience_class_types", list(profile.class_types)))
        if profile.groups:
            must.append(
                _empty_or_match(
                    "audience_groups",
                    sorted(group.value for group in profile.groups),
                )
            )
        if profile.status is not None:
            must.append(_empty_or_match("audience_status", [profile.status.value]))

    graduating_year = filters.graduating_year
    admission_year = filters.admission_year
    if profile is not None:
        if graduating_year is None:
            graduating_year = profile.graduating_year
        if admission_year is None:
            admission_year = profile.admission_year
    if graduating_year is not None:
        must.append(_empty_or_match("audience_graduating_years", [graduating_year]))
    if admission_year is not None:
        must.append(_empty_or_value("audience_admission_year", admission_year))

    if (profile is not None and not profile.is_empty) or filters.academic_year:
        must.append(_empty_or_match("audience_cycle", target_academic_cycle(filters)))

    return Filter(must=must)


def filter_explanations(filters: PolicyFilters) -> list[str]:
    """返回可写入 warnings/trace 的“实际应用了什么过滤”说明。"""
    applied: list[str] = []
    applied.append(f"as_of={filters.as_of.isoformat()}")
    applied.append(f"version_mode={filters.version_mode.value}")
    if filters.audience:
        applied.append("audience=" + ",".join(sorted(a.value for a in filters.audience)))
    else:
        applied.append("audience=未指定（不硬过滤）")
    if filters.doc_ids:
        applied.append("doc_ids=" + ",".join(filters.doc_ids))
    if filters.ignore_effective_dates:
        applied.append("effective_dates=ignored")
    if filters.academic_year:
        applied.append(f"academic_year={filters.academic_year}")
    if filters.admission_year is not None:
        applied.append(f"admission_year={filters.admission_year}")
    if filters.graduating_year is not None:
        applied.append(f"graduating_year={filters.graduating_year}")
    profile = filters.profile
    if profile is not None and not profile.is_empty:
        parts = []
        if profile.colleges:
            parts.append("colleges=" + ",".join(profile.colleges))
        if profile.majors:
            parts.append("majors=" + ",".join(profile.majors))
        if profile.intake_year is not None:
            parts.append(f"intake_year={profile.intake_year}")
        if profile.stage is not None:
            parts.append(f"stage={profile.stage.value}")
        if profile.program_type is not None:
            parts.append(f"program_type={profile.program_type.value}")
        if profile.learning_mode is not None:
            parts.append(f"learning_mode={profile.learning_mode.value}")
        if profile.degree_type is not None:
            parts.append(f"degree_type={profile.degree_type.value}")
        if profile.class_types:
            parts.append("class_types=" + ",".join(profile.class_types))
        if profile.graduating_year is not None:
            parts.append(f"graduating_year={profile.graduating_year}")
        if profile.admission_year is not None:
            parts.append(f"admission_year={profile.admission_year}")
        if profile.academic_year is not None:
            parts.append(f"academic_year={profile.academic_year}")
        if profile.groups:
            parts.append("groups=" + ",".join(group.value for group in profile.groups))
        if profile.status is not None:
            parts.append(f"status={profile.status.value}")
        applied.append("profile[" + "; ".join(parts) + "]")
    else:
        applied.append("profile=未提供")
    return applied


def _detail_list(detail: Any, name: str) -> list[Any]:
    return list(getattr(detail, name, None) or [])


def article_matches_profile(
    article: Any,
    profile: UserProfile | None,
    *,
    as_of: date,
    academic_year: str | None = None,
    admission_year: int | None = None,
    graduating_year: int | None = None,
) -> tuple[bool, list[str]]:
    """Python 侧二次校验，防止 payload 索引缺失或 Qdrant 条件遗漏。

    返回 ``(是否保留, 排除原因)``；用户未提供的字段永远不排除。``academic_year``
    等显式年度优先于画像与 ``as_of``，避免把招生/届别文件套进当前学年。
    """
    target_cycle = None
    if academic_year:
        target_cycle = _cycle_forms(academic_year)
    elif profile is not None and profile.academic_year:
        target_cycle = _cycle_forms(profile.academic_year)
    if target_cycle is None:
        target_cycle = academic_cycle(as_of)

    target_graduating = graduating_year
    target_admission = admission_year
    if profile is not None:
        if target_graduating is None:
            target_graduating = profile.graduating_year
        if target_admission is None:
            target_admission = profile.admission_year

    reasons: list[str] = []

    def check_cohort(detail: Any) -> None:
        cycle = getattr(detail, "cycle", None)
        if cycle and cycle not in target_cycle:
            reasons.append(f"学年不匹配：文档={cycle}，目标={target_cycle}")
        if target_graduating is not None:
            graduating = _detail_list(detail, "graduating_years")
            if graduating and target_graduating not in graduating:
                reasons.append(f"毕业届别不匹配：文档={graduating}，用户={target_graduating}")
        if target_admission is not None:
            doc_admission = getattr(detail, "admission_year", None)
            if doc_admission is not None and doc_admission != target_admission:
                reasons.append(
                    f"招生年度不匹配：文档={doc_admission}，用户={target_admission}"
                )

    detail = getattr(article, "audience_detail", None)
    if profile is None or profile.is_empty:
        if detail is not None:
            check_cohort(detail)
        return not reasons, reasons
    if detail is None:
        return True, []

    check_cohort(detail)

    colleges = _detail_list(detail, "colleges")
    if colleges and profile.colleges and not set(colleges) & set(profile.colleges):
        reasons.append(f"学院不匹配：文档={colleges}，用户={profile.colleges}")

    majors = _detail_list(detail, "majors")
    if majors and profile.majors and not set(majors) & set(profile.majors):
        reasons.append(f"专业不匹配：文档={majors}，用户={profile.majors}")

    intake_years = _detail_list(detail, "intake_years")
    intake_from = getattr(detail, "intake_from", None)
    if profile.intake_year is not None:
        if intake_years and profile.intake_year not in intake_years:
            reasons.append(f"入学年份不匹配：文档={intake_years}，用户={profile.intake_year}")
        if intake_from is not None and profile.intake_year < intake_from:
            reasons.append(f"入学年份早于适用起点：文档>={intake_from}，用户={profile.intake_year}")

    stages = _detail_list(detail, "stages")
    if stages and profile.stage is not None and profile.stage not in stages:
        reasons.append(f"阶段不匹配：文档={[s.value for s in stages]}，用户={profile.stage.value}")

    program_types = _detail_list(detail, "program_types")
    if (
        program_types
        and profile.program_type is not None
        and profile.program_type not in program_types
    ):
        reasons.append(
            "培养类型不匹配："
            f"文档={[p.value for p in program_types]}，用户={profile.program_type.value}"
        )

    if profile.learning_mode is not None:
        modes = _detail_list(detail, "learning_modes")
        legacy_modes = [
            item for item in program_types if item.value in LEARNING_PROGRAM_VALUES
        ]
        if modes:
            if profile.learning_mode not in modes:
                reasons.append(
                    "学习方式不匹配："
                    f"文档={[m.value for m in modes]}，用户={profile.learning_mode.value}"
                )
        elif legacy_modes and profile.learning_mode not in legacy_modes:
            reasons.append(
                "学习方式不匹配（旧字段）："
                f"文档={[m.value for m in legacy_modes]}，用户={profile.learning_mode.value}"
            )

    if profile.degree_type is not None:
        degrees = _detail_list(detail, "degree_types")
        legacy_degrees = [
            item for item in program_types if item.value in DEGREE_PROGRAM_VALUES
        ]
        if degrees:
            if profile.degree_type not in degrees:
                reasons.append(
                    "学位类型不匹配："
                    f"文档={[d.value for d in degrees]}，用户={profile.degree_type.value}"
                )
        elif legacy_degrees and profile.degree_type not in legacy_degrees:
            reasons.append(
                "学位类型不匹配（旧字段）："
                f"文档={[d.value for d in legacy_degrees]}，用户={profile.degree_type.value}"
            )

    class_types = _detail_list(detail, "class_types")
    if class_types and profile.class_types and not set(class_types) & set(profile.class_types):
        reasons.append(f"班型不匹配：文档={class_types}，用户={profile.class_types}")

    groups = _detail_list(detail, "groups")
    if groups and profile.groups and not set(groups) & set(profile.groups):
        reasons.append(
            "群体不匹配："
            f"文档={[g.value for g in groups]}，用户={[g.value for g in profile.groups]}"
        )

    statuses = _detail_list(detail, "status")
    if statuses and profile.status is not None and profile.status not in statuses:
        reasons.append(
            f"学籍状态不匹配：文档={[s.value for s in statuses]}，用户={profile.status.value}"
        )

    return not reasons, reasons


def profile_undetermined_warnings(
    articles: list[Any],
    profile: UserProfile | None,
    *,
    audience: list[Audience] | None,
) -> list[str]:
    """对“用户未提供但结果里存在相应约束”的字段给出显式 warning。"""
    warnings: list[str] = []
    if not audience or Audience.ALL in audience:
        warnings.append("AUDIENCE_UNDETERMINED: 未指定受众，未按角色做硬过滤")

    if profile is None:
        missing = set(PROFILE_FIELDS)
        details = [getattr(article, "audience_detail", None) for article in articles]
    else:
        missing = profile.missing_fields()
        details = [getattr(article, "audience_detail", None) for article in articles]

    present: dict[str, bool] = {
        "colleges": any(getattr(d, "colleges", None) for d in details if d),
        "majors": any(getattr(d, "majors", None) for d in details if d),
        "intake": any(
            getattr(d, "intake_years", None) or getattr(d, "intake_from", None) is not None
            for d in details
            if d
        ),
        "stages": any(getattr(d, "stages", None) for d in details if d),
        "program_types": any(getattr(d, "program_types", None) for d in details if d),
        "groups": any(getattr(d, "groups", None) for d in details if d),
        "status": any(getattr(d, "status", None) for d in details if d),
    }
    labels = {
        "colleges": "学院",
        "majors": "专业",
        "intake": "入学年份",
        "stages": "年级/阶段",
        "program_types": "培养类型",
        "groups": "学生群体",
        "status": "学籍状态",
    }
    for field in PROFILE_FIELDS:
        if field in missing and present.get(field):
            warnings.append(
                f"PROFILE_UNDETERMINED: 用户未提供{labels[field]}，结果保留可能限定该细目的条款"
            )
    return warnings
