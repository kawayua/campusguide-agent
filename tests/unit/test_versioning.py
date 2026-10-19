from __future__ import annotations

from datetime import UTC, date, datetime

from campusguide.domain.enums import ArticleStatus, Audience, VersionMode
from campusguide.domain.models import DocumentVersion
from campusguide.ingest.versioning import resolve_version


def make_version(
    version_id: str,
    *,
    effective_from: date,
    effective_to: date | None = None,
    status: ArticleStatus = ArticleStatus.ACTIVE,
) -> DocumentVersion:
    return DocumentVersion(
        doc_id="status-rules",
        version_id=version_id,
        title="本科学生学籍管理实施细则",
        doc_type="regulation",
        issuing_department="教务处",
        source_url="https://jwc.scau.edu.cn/example.htm",
        published_at=date(2020, 1, 1),
        effective_from=effective_from,
        effective_to=effective_to,
        status=status,
        source_sha256="abc",
        fetched_at=datetime(2026, 10, 19, tzinfo=UTC),
        parser_version="test",
        audience=[Audience.UNDERGRADUATE],
    )


def test_current_selects_latest_active_in_range() -> None:
    versions = [
        make_version("v2021", effective_from=date(2021, 9, 1), effective_to=date(2025, 8, 31), status=ArticleStatus.REPEALED),
        make_version("v2025", effective_from=date(2025, 9, 1)),
    ]
    resolution = resolve_version(versions, as_of=date(2026, 10, 19))
    assert resolution.ok
    assert resolution.selected is not None
    assert resolution.selected.version_id == "v2025"


def test_current_ignores_future_and_draft_versions() -> None:
    versions = [
        make_version("v2025", effective_from=date(2025, 9, 1)),
        make_version("v2026", effective_from=date(2026, 12, 1), status=ArticleStatus.DRAFT),
    ]
    resolution = resolve_version(versions, as_of=date(2026, 10, 19))
    assert resolution.selected is not None
    assert resolution.selected.version_id == "v2025"


def test_current_reports_conflict_for_overlapping_versions() -> None:
    versions = [
        make_version("v2025a", effective_from=date(2025, 9, 1)),
        make_version("v2025b", effective_from=date(2025, 9, 1)),
    ]
    resolution = resolve_version(versions, as_of=date(2026, 10, 19))
    assert resolution.reason_code == "VERSION_CONFLICT"
    assert resolution.selected is None


def test_historical_uses_as_of_and_adds_warning() -> None:
    versions = [
        make_version("v2021", effective_from=date(2021, 9, 1), effective_to=date(2025, 8, 31), status=ArticleStatus.REPEALED),
        make_version("v2025", effective_from=date(2025, 9, 1)),
    ]
    resolution = resolve_version(
        versions,
        as_of=date(2023, 12, 31),
        mode=VersionMode.HISTORICAL,
    )
    assert resolution.selected is not None
    assert resolution.selected.version_id == "v2021"
    assert any("现行版本可能已调整" in warning for warning in resolution.warnings)


def test_include_repealed_returns_repealed_with_warning() -> None:
    versions = [
        make_version("v2021", effective_from=date(2021, 9, 1), effective_to=date(2025, 8, 31), status=ArticleStatus.REPEALED),
    ]
    resolution = resolve_version(
        versions,
        as_of=date(2023, 12, 31),
        mode=VersionMode.INCLUDE_REPEALED,
    )
    assert resolution.selected is not None
    assert resolution.selected.version_id == "v2021"
    assert any("已废止" in warning for warning in resolution.warnings)


def test_no_effective_version_never_falls_back() -> None:
    versions = [make_version("v2025", effective_from=date(2025, 9, 1))]
    resolution = resolve_version(versions, as_of=date(2024, 1, 1))
    assert resolution.reason_code == "NO_EFFECTIVE_VERSION"
    assert resolution.selected is None
