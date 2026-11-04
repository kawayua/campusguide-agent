"""把 42 个已审计低置信版本的日期依据显式写入 sources.yaml。

只改 ``date_confidence`` 与 ``date_source`` 两个字段：注释、文号、notes 和
字段顺序全部保留。脚本以 ``date_fields_to_materialize.json`` 为唯一清单，
要求清单恰好 42 个版本且都能定位，缺失/重复/证据原文缺失即拒绝。
默认原子写回；``--check`` 只报告差异。

用法：
    uv run python scripts/materialize_date_fields.py --check
    uv run python scripts/materialize_date_fields.py
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG = ROOT / "configs" / "sources.yaml"
DEFAULT_MAPPING = (
    ROOT
    / "data"
    / "eval"
    / "answer_quality"
    / "ingestion_refactor_20261030"
    / "date_fields_to_materialize.json"
)
EXPECTED_TOTAL = 42
DOC_RE = re.compile(r"^- doc_id:\s*(\S+)\s*$")
VERSION_RE = re.compile(r"^  - version_id:\s*(\S+)\s*$")
# versions 之后仍可能有文档级 audience_detail/relation 等 2 空格字段；
# 版本块必须在此结束，否则 4 空格日期字段会插进文档字段。
DOC_LEVEL_FIELD_RE = re.compile(r"^  (?![-#\s])\S")
DATE_CONFIDENCE_RE = re.compile(r"^    date_confidence:\s*")
DATE_SOURCE_RE = re.compile(r"^    date_source:\s*")


class MigrationError(RuntimeError):
    """清单或 YAML 结构不符合预期，拒绝写入。"""


def _load_mapping(path: Path) -> list[dict[str, Any]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    versions = payload.get("versions") or []
    expected = int(payload.get("expected_low_from_source_notes") or 0)
    if expected != EXPECTED_TOTAL:
        raise MigrationError(
            f"清单期望低置信数为 {expected}，与脚本约定的 {EXPECTED_TOTAL} 不符"
        )
    if len(versions) != EXPECTED_TOTAL:
        raise MigrationError(f"清单包含 {len(versions)} 个版本，应为 {EXPECTED_TOTAL}")
    seen: set[tuple[str, str]] = set()
    for item in versions:
        key = (str(item["doc_id"]), str(item["version_id"]))
        if key in seen:
            raise MigrationError(f"清单存在重复版本：{key[0]}@{key[1]}")
        seen.add(key)
        if item.get("expected_date_confidence") != "low":
            raise MigrationError(f"{key[0]}@{key[1]} 的目标置信度不是 low")
        if not str(item.get("recommended_date_source") or "").strip():
            raise MigrationError(f"{key[0]}@{key[1]} 缺少 recommended_date_source")
    return versions


def _iter_blocks(
    lines: list[str],
) -> tuple[dict[str, tuple[int, int]], dict[tuple[str, str], tuple[int, int]]]:
    doc_blocks: dict[str, tuple[int, int]] = {}
    starts = [index for index, line in enumerate(lines) if DOC_RE.match(line)]
    for position, start in enumerate(starts):
        end = starts[position + 1] if position + 1 < len(starts) else len(lines)
        doc_id = DOC_RE.match(lines[start]).group(1)  # type: ignore[union-attr]
        if doc_id in doc_blocks:
            raise MigrationError(f"sources.yaml 存在重复 doc_id：{doc_id}")
        doc_blocks[doc_id] = (start, end)

    blocks: dict[tuple[str, str], tuple[int, int]] = {}
    for doc_id, (start, end) in doc_blocks.items():
        version_starts = [
            index for index in range(start, end) if VERSION_RE.match(lines[index])
        ]
        for position, version_start in enumerate(version_starts):
            next_version = (
                version_starts[position + 1]
                if position + 1 < len(version_starts)
                else end
            )
            version_end = next_version
            for index in range(version_start + 1, next_version):
                if DOC_LEVEL_FIELD_RE.match(lines[index]):
                    version_end = index
                    break
            version_id = VERSION_RE.match(  # type: ignore[union-attr]
                lines[version_start]
            ).group(1)
            key = (doc_id, version_id)
            if key in blocks:
                raise MigrationError(f"sources.yaml 存在重复版本：{doc_id}@{version_id}")
            blocks[key] = (version_start, version_end)
    return doc_blocks, blocks


def _insert_at(lines: list[str], start: int, end: int) -> int:
    position = end
    while position > start and (
        not lines[position - 1].strip() or lines[position - 1].lstrip().startswith("#")
    ):
        position -= 1
    return position


def _quote(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def _field_values(
    lines: list[str], start: int, end: int, pattern: re.Pattern[str]
) -> list[Any]:
    """按 YAML 值解析版本块内的字段，不能拿带缩进的行做成员判断。"""
    values: list[Any] = []
    for index in range(start, end):
        match = pattern.match(lines[index])
        if not match:
            continue
        raw = lines[index][match.end() :].strip()
        if not raw:
            values.append(None)
            continue
        try:
            values.append(yaml.safe_load(raw))
        except yaml.YAMLError:
            values.append(raw)
    return values


def _materialize_version(
    lines: list[str],
    start: int,
    end: int,
    *,
    date_source: str,
    newline: str,
) -> bool:
    existing = [
        index
        for index in range(start, end)
        if DATE_CONFIDENCE_RE.match(lines[index]) or DATE_SOURCE_RE.match(lines[index])
    ]
    position = existing[0] if existing else _insert_at(lines, start, end)
    replacement = [
        f"    date_confidence: low{newline}",
        f"    date_source: {_quote(date_source)}{newline}",
    ]
    for index in reversed(existing):
        del lines[index]
    if position > 0 and not lines[position - 1].endswith(("\n", "\r")):
        lines[position - 1] += newline
    lines[position:position] = replacement
    return True


def _verify_notes(
    lines: list[str],
    doc_blocks: dict[str, tuple[int, int]],
    blocks: dict[tuple[str, str], tuple[int, int]],
    versions: list[dict[str, Any]],
) -> None:
    for item in versions:
        key = (str(item["doc_id"]), str(item["version_id"]))
        for location, note in (item.get("notes") or {}).items():
            if location == "version.notes":
                start, end = blocks[key]
            elif location == "document.audience_detail.note":
                start, end = doc_blocks[key[0]]
            else:
                raise MigrationError(f"未知依据位置：{location}")
            if note not in "".join(lines[start:end]):
                raise MigrationError(f"{key[0]}@{key[1]} 的原始依据缺失：{location}")


def _validate_yaml(text: str, versions: list[dict[str, Any]]) -> None:
    payload = yaml.safe_load(text)
    documents = payload.get("documents") if isinstance(payload, dict) else payload
    if not isinstance(documents, list):
        raise MigrationError("sources.yaml 顶层不是文档列表")
    by_key = {
        (str(document["doc_id"]), str(version["version_id"])): version
        for document in documents
        for version in document.get("versions") or []
    }
    for item in versions:
        key = (str(item["doc_id"]), str(item["version_id"]))
        version = by_key.get(key)
        if version is None:
            raise MigrationError(f"迁移后找不到版本：{key[0]}@{key[1]}")
        if version.get("date_confidence") != "low":
            raise MigrationError(f"{key[0]}@{key[1]} 的 date_confidence 未落为 low")
        if version.get("date_source") != item["recommended_date_source"]:
            raise MigrationError(f"{key[0]}@{key[1]} 的 date_source 与清单不一致")


def migrate(config_path: Path, mapping_path: Path, *, check: bool) -> dict[str, Any]:
    versions = _load_mapping(mapping_path)
    raw = config_path.read_bytes().decode("utf-8")
    newline = "\r\n" if "\r\n" in raw else "\n"
    lines = raw.splitlines(keepends=True)
    doc_blocks, blocks = _iter_blocks(lines)
    missing = [
        f"{item['doc_id']}@{item['version_id']}"
        for item in versions
        if (str(item["doc_id"]), str(item["version_id"])) not in blocks
    ]
    if missing:
        raise MigrationError("sources.yaml 缺少清单版本：" + ", ".join(sorted(missing)))
    _verify_notes(lines, doc_blocks, blocks, versions)

    changed = 0
    # 从后往前插入，避免前面版本写入后使后面版本的行号失效。
    ordered = sorted(
        versions,
        key=lambda item: blocks[(str(item["doc_id"]), str(item["version_id"]))][0],
        reverse=True,
    )
    for item in ordered:
        start, end = blocks[(str(item["doc_id"]), str(item["version_id"]))]
        confidences = _field_values(lines, start, end, DATE_CONFIDENCE_RE)
        sources = _field_values(lines, start, end, DATE_SOURCE_RE)
        if confidences == ["low"] and sources == [item["recommended_date_source"]]:
            continue
        _materialize_version(
            lines,
            start,
            end,
            date_source=item["recommended_date_source"],
            newline=newline,
        )
        changed += 1

    text = "".join(lines)
    _validate_yaml(text, versions)
    if not check and changed:
        temporary = config_path.with_name(config_path.name + ".part")
        temporary.write_text(text, encoding="utf-8", newline="")
        os.replace(temporary, config_path)
    return {
        "versions": len(versions),
        "changed": changed,
        "check": check,
        "config": str(config_path),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default=str(DEFAULT_CONFIG))
    parser.add_argument("--mapping", default=str(DEFAULT_MAPPING))
    parser.add_argument(
        "--check",
        action="store_true",
        help="只校验并报告待修改版本数，不写回配置",
    )
    args = parser.parse_args()
    try:
        summary = migrate(
            Path(args.config).resolve(),
            Path(args.mapping).resolve(),
            check=args.check,
        )
    except (MigrationError, OSError, ValueError, KeyError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    action = "待修改" if summary["check"] else "已修改"
    print(
        f"{action} {summary['changed']}/{summary['versions']} 个版本：{summary['config']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
