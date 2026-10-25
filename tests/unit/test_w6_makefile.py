from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_w6_make_targets_have_rules_not_only_phony() -> None:
    text = (ROOT / "Makefile").read_text(encoding="utf-8")
    for target in (
        "eval-w6b:",
        "eval-w6b-report:",
        "eval-w6b-mcp:",
        "eval-w6b-mcp-report:",
        "eval-w6b-scenarios:",
        "sheet-w6b:",
        "label-w6b:",
        "sheet-w6b-mcp:",
        "label-w6b-mcp:",
        "w6-chat:",
    ):
        assert target in text, f"Makefile 缺少规则：{target}"
