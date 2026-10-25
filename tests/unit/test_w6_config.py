from __future__ import annotations

from pathlib import Path

import pytest

from campusguide.config import (
    GraphConfig,
    HandoffConfig,
    load_graph_config,
    load_handoff_config,
)

ROOT = Path(__file__).resolve().parents[2]


def test_graph_config_defaults_and_profile() -> None:
    config = load_graph_config(ROOT / "configs" / "graph.yaml")
    assert isinstance(config, GraphConfig)
    assert config.enabled is True
    assert config.model_profile == "vpro"
    assert config.max_queries == 3
    assert config.max_revise == 2
    assert config.max_clarify == 3
    assert config.semantic.on_unavailable == "strict"
    assert config.semantic.batch_size == 5


def test_graph_config_rejects_invalid_limits(tmp_path: Path) -> None:
    path = tmp_path / "graph.yaml"
    path.write_text(
        "enabled: true\nmodel_profile: vpro\nmax_queries: 4\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError):
        load_graph_config(path)


def test_graph_config_rejects_unknown_semantic_mode(tmp_path: Path) -> None:
    path = tmp_path / "graph.yaml"
    path.write_text(
        "enabled: true\nsemantic:\n  on_unavailable: lenient\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError):
        load_graph_config(path)


def test_handoff_config_has_flows_and_reasons() -> None:
    config = load_handoff_config(ROOT / "configs" / "handoff.yaml")
    assert isinstance(config, HandoffConfig)
    assert "scholarship-application" in config.flows
    assert "NO_EVIDENCE" in config.reasons
    assert config.defaults.department == "学校相关负责部门"
    assert str(config.flows["enrollment-certificate"].url).startswith("https://")


def test_handoff_config_requires_defaults(tmp_path: Path) -> None:
    path = tmp_path / "handoff.yaml"
    path.write_text("flows: {}\n", encoding="utf-8")
    with pytest.raises(ValueError):
        load_handoff_config(path)


def test_graph_and_handoff_files_exist() -> None:
    assert (ROOT / "configs" / "graph.yaml").exists()
    assert (ROOT / "configs" / "handoff.yaml").exists()
