"""日期物化：versions 之后的文档级字段不能吞进最后一个版本块。"""

from __future__ import annotations

import copy
import json
from pathlib import Path

import yaml
from scripts.materialize_date_fields import (
    DATE_CONFIDENCE_RE,
    DATE_SOURCE_RE,
    _iter_blocks,
    migrate,
)

ROOT = Path(__file__).resolve().parents[2]
EXPERIMENT = ROOT / "data" / "eval" / "answer_quality" / "ingestion_refactor_20261030"
SOURCES = ROOT / "configs" / "sources.yaml"
MAPPING = EXPERIMENT / "date_fields_to_materialize.json"


def _strip_target_date_fields(raw: str, mapping: dict) -> tuple[str, int]:
    """在临时副本中只去掉 42 个目标版本的显式日期字段，其余行原样保留。"""
    lines = raw.splitlines(keepends=True)
    _, blocks = _iter_blocks(lines)
    remove: set[int] = set()
    for item in mapping["versions"]:
        start, end = blocks[(item["doc_id"], item["version_id"])]
        for index in range(start, end):
            if DATE_CONFIDENCE_RE.match(lines[index]) or DATE_SOURCE_RE.match(
                lines[index]
            ):
                remove.add(index)
    stripped = "".join(
        line for index, line in enumerate(lines) if index not in remove
    )
    return stripped, len(remove)


def test_version_block_stops_before_trailing_document_fields() -> None:
    lines = [
        "- doc_id: work-study\n",
        "  versions:\n",
        "  - version_id: v2019\n",
        "    notes: 文号：华南农办〔2019〕29号；仅知印发年份（date_confidence=low）。\n",
        "  audience_detail:\n",
        "    groups:\n",
        "    - financial_aid\n",
        "    note: 勤工助学岗位面向家庭经济困难学生\n",
        "- doc_id: student-loan\n",
        "  versions:\n",
        "  - version_id: v2018\n",
        "    notes: 文号：华南农办〔2018〕81号；仅知印发年份（date_confidence=low）。\n",
        "  audience_detail:\n",
        "    groups:\n",
        "    - financial_aid\n",
    ]

    _, blocks = _iter_blocks(lines)

    start, end = blocks[("work-study", "v2019")]
    assert start == 2
    assert end == 4
    assert lines[end] == "  audience_detail:\n"
    assert blocks[("student-loan", "v2018")][1] == 12


def test_last_version_of_multi_version_doc_stops_before_document_fields() -> None:
    lines = [
        "- doc_id: mixed\n",
        "  versions:\n",
        "  - version_id: v1\n",
        "    notes: 旧版正文日期。\n",
        "  - version_id: v2\n",
        "    notes: 文号：华南农办〔2020〕1号；仅知印发年份（date_confidence=low）。\n",
        "  audience_detail:\n",
        "    note: 文档级说明\n",
    ]

    _, blocks = _iter_blocks(lines)

    assert blocks[("mixed", "v1")] == (2, 4)
    assert blocks[("mixed", "v2")] == (4, 6)
    assert lines[6] == "  audience_detail:\n"


def test_real_migration_touches_only_42_fields_and_is_idempotent(tmp_path: Path) -> None:
    mapping = json.loads(MAPPING.read_text(encoding="utf-8"))
    raw = SOURCES.read_bytes().decode("utf-8")
    unmaterialized, removed = _strip_target_date_fields(raw, mapping)
    assert removed == 42 * 2
    target = tmp_path / "sources.yaml"
    target.write_text(unmaterialized, encoding="utf-8", newline="")
    before = yaml.safe_load(unmaterialized)

    expected = copy.deepcopy(yaml.safe_load(raw))
    versions = {
        (document["doc_id"], version["version_id"]): version
        for document in expected["documents"]
        for version in document.get("versions", [])
    }
    for item in mapping["versions"]:
        version = versions[(item["doc_id"], item["version_id"])]
        version.pop("date_confidence", None)
        version.pop("date_source", None)
    # 临时副本只缺 42 个目标字段，其他 YAML 语义与当前源一致。
    assert before == expected

    check_first = migrate(target, MAPPING, check=True)
    assert check_first["changed"] == 42
    assert target.read_bytes().decode("utf-8") == unmaterialized

    first = migrate(target, MAPPING, check=False)
    assert first["changed"] == 42

    after = yaml.safe_load(target.read_bytes().decode("utf-8"))
    for item in mapping["versions"]:
        version = versions[(item["doc_id"], item["version_id"])]
        version["date_confidence"] = "low"
        version["date_source"] = item["recommended_date_source"]
    assert after == expected

    documents = {document["doc_id"]: document for document in after["documents"]}
    work_study = documents["work-study"]
    assert work_study["versions"][0]["date_confidence"] == "low"
    assert work_study["audience_detail"]["groups"] == ["financial_aid"]
    assert "date_confidence" not in work_study["audience_detail"]
    assert "date_source" not in work_study["audience_detail"]
    student_loan = documents["student-loan"]
    assert student_loan["versions"][0]["date_confidence"] == "low"
    assert student_loan["audience_detail"]["groups"] == ["financial_aid"]

    after_first = target.read_bytes().decode("utf-8")
    check_second = migrate(target, MAPPING, check=True)
    assert check_second["changed"] == 0
    assert target.read_bytes().decode("utf-8") == after_first
    second = migrate(target, MAPPING, check=False)
    assert second["changed"] == 0
    assert target.read_bytes().decode("utf-8") == after_first

    # 当前仓库源已由 Codex 物化；--check 应报告 0 修改且不改写字节。
    real_raw = SOURCES.read_bytes()
    real = migrate(SOURCES, MAPPING, check=True)
    assert real["changed"] == 0
    assert SOURCES.read_bytes() == real_raw
