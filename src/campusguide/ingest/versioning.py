"""版本解析：按 as_of 与版本模式选择唯一适用版本（规格书 5.3）。

输出只有三种情况：选中一个版本、标记 VERSION_CONFLICT、标记 NO_EFFECTIVE_VERSION。
绝不回退到任意版本。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from campusguide.domain.enums import ArticleStatus, VersionMode
from campusguide.domain.models import DocumentVersion


@dataclass(frozen=True)
class VersionResolution:
    selected: DocumentVersion | None
    candidates: tuple[DocumentVersion, ...]
    reason_code: str | None = None
    warnings: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return self.selected is not None and self.reason_code is None


def _in_interval(version: DocumentVersion, as_of: date) -> bool:
    if version.effective_from > as_of:
        return False
    return version.effective_to is None or as_of <= version.effective_to


def resolve_version(
    versions: list[DocumentVersion],
    *,
    as_of: date,
    mode: VersionMode = VersionMode.CURRENT,
) -> VersionResolution:
    if not versions:
        return VersionResolution(
            selected=None,
            candidates=(),
            reason_code="NO_EFFECTIVE_VERSION",
            warnings=("没有任何已登记版本",),
        )

    if mode is VersionMode.CURRENT:
        candidates = [
            version
            for version in versions
            if version.status is ArticleStatus.ACTIVE and _in_interval(version, as_of)
        ]
    elif mode in {VersionMode.HISTORICAL, VersionMode.INCLUDE_REPEALED}:
        # 历史模式下，当时有效的版本今天可能已经标记废止；只排除草稿。
        candidates = [
            version
            for version in versions
            if version.status is not ArticleStatus.DRAFT and _in_interval(version, as_of)
        ]
    else:  # pragma: no cover - 枚举已封闭
        raise ValueError(f"未知版本模式：{mode}")

    if not candidates:
        return VersionResolution(
            selected=None,
            candidates=(),
            reason_code="NO_EFFECTIVE_VERSION",
            warnings=(f"{as_of.isoformat()} 在已登记版本中找不到适用版本",),
        )

    latest_from = max(version.effective_from for version in candidates)
    latest = tuple(version for version in candidates if version.effective_from == latest_from)
    if len(latest) > 1:
        return VersionResolution(
            selected=None,
            candidates=latest,
            reason_code="VERSION_CONFLICT",
            warnings=(
                f"{as_of.isoformat()} 存在 {len(latest)} 个生效日期相同的候选版本，需人工确认",
            ),
        )

    selected = latest[0]
    warnings: list[str] = []
    if mode is VersionMode.HISTORICAL:
        warnings.append(
            f"以下依据为 {selected.version_id} 版本，现行版本可能已调整；如需现行政策请明确说明"
        )
    if mode is VersionMode.INCLUDE_REPEALED and selected.status is ArticleStatus.REPEALED:
        warnings.append(f"{selected.version_id} 已废止，仅可用于解释历史条款")
    return VersionResolution(
        selected=selected,
        candidates=latest,
        warnings=tuple(warnings),
    )
