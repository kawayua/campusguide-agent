"""W8 demo preflight：检查 .env、语料/manifest 与索引版本。

不自动抓取学校站点，不启动 Qdrant，不修改任何数据；失败时打印可直接
执行的 make 命令。供 ``make demo-up`` 在 compose 启动前调用。
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EXPECTED_POINTS = 4026
REQUIRED_ENV = (
    "LLM_BASE_URL",
    "LLM_API_KEY",
    "PLANNER_MODEL",
    "ADVISOR_MODEL",
    "VERIFIER_MODEL",
)
REQUIRED_FILES = (
    "data/processed/articles.jsonl",
    "data/processed/documents.jsonl",
    "data/manifest.json",
    "data/vector_manifest.json",
    "data/sparse_vocab.json",
)


@dataclass
class PreflightResult:
    issues: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.issues


def read_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export ") :]
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if not key:
            continue
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        values[key] = value
    return values


def _read_json(path: Path, issues: list[str]) -> dict:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        issues.append(f"{path.relative_to(path.parents[1])} 不是合法 JSON：{exc}")
        return {}
    if not isinstance(payload, dict):
        issues.append(f"{path.relative_to(path.parents[1])} 顶层必须是对象")
        return {}
    return payload


def run_preflight(root: Path = ROOT, *, expected_points: int = EXPECTED_POINTS) -> PreflightResult:
    result = PreflightResult()
    env_path = root / ".env"
    if not env_path.is_file():
        result.issues.append("缺少 .env；请从 .env.example 复制并填写模型配置")
    else:
        env = read_env(env_path)
        missing = [key for key in REQUIRED_ENV if not env.get(key, "").strip()]
        if missing:
            result.issues.append("`.env` 缺少非空配置：" + "、".join(missing))
        qdrant = env.get("QDRANT_URL", "") or env.get("QDRANT_LOCATION", "")
        if qdrant:
            result.notes.append(f"Qdrant 配置：{qdrant}（preflight 不主动连接）")
        else:
            result.issues.append("`.env` 未配置 QDRANT_URL 或 QDRANT_LOCATION")

    for relative in REQUIRED_FILES:
        path = root / relative
        if not path.is_file() or path.stat().st_size == 0:
            result.issues.append(f"缺少或为空：{relative}")

    text_manifest_path = root / "data" / "manifest.json"
    vector_manifest_path = root / "data" / "vector_manifest.json"
    if text_manifest_path.is_file() and vector_manifest_path.is_file():
        text_manifest = _read_json(text_manifest_path, result.issues)
        vector_manifest = _read_json(vector_manifest_path, result.issues)
        text_hash = str(text_manifest.get("content_hash") or "")
        vector_hash = str(vector_manifest.get("source_content_hash") or "")
        if not text_hash:
            result.issues.append("data/manifest.json 缺少 content_hash")
        if not vector_hash:
            result.issues.append("data/vector_manifest.json 缺少 source_content_hash")
        if text_hash and vector_hash and text_hash != vector_hash:
            result.issues.append(
                "文本/向量 manifest 的 content_hash 不一致；请重新运行 `make vector-index`"
            )
        points = vector_manifest.get("points")
        try:
            points_value = int(points)
        except (TypeError, ValueError):
            points_value = -1
        if points_value != expected_points:
            result.issues.append(
                f"vector_manifest.points={points or '—'}，期望 {expected_points}；"
                "请重新运行 `make vector-index`"
            )
        index_version = str(vector_manifest.get("index_version") or "")
        if not index_version:
            result.issues.append("data/vector_manifest.json 缺少 index_version")
        elif points_value == expected_points and text_hash == vector_hash:
            result.notes.append(f"索引：{index_version} · points={points_value}")
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--expected-points", type=int, default=EXPECTED_POINTS)
    args = parser.parse_args()
    result = run_preflight(args.root, expected_points=args.expected_points)
    if result.ok:
        print("[preflight] OK")
        for note in result.notes:
            print(f"  - {note}")
        return 0
    print(f"[preflight] FAIL ({len(result.issues)} issues)")
    for issue in result.issues:
        print(f"  - {issue}")
    print("")
    print("[next] make fetch && make parse && make index && make vector-index")
    print("[hint] 若只缺 .env，请先 cp .env.example .env 并填写模型配置。")
    return 1


if __name__ == "__main__":
    sys.exit(main())
