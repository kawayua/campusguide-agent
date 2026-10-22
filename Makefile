SHELL := /bin/sh
UV ?= $(shell command -v uv 2>/dev/null || echo python -m uv)
PYTHON ?= $(UV) run python
PYTEST ?= $(UV) run pytest
RUFF ?= $(UV) run ruff

.PHONY: help init sync fetch parse index index-qdrant vector-index validate-sources \
	check-segmentation test lint format format-check qa ask eval-w2 demo compose-up \
	compose-down clean

help:
	@echo "CampusGuide commands"
	@echo "  make init              安装基础依赖（uv sync）"
	@echo "  make init-w2           安装 W2 依赖（torch / sentence-transformers / gradio）"
	@echo "  make fetch             按 configs/sources.yaml 抓取/复制原始快照"
	@echo "  make parse             解析 data/raw 下的 HTML/PDF"
	@echo "  make index             生成 articles.jsonl、documents.jsonl、manifest.json"
	@echo "  make index-qdrant      额外初始化 Qdrant 空集合与 payload 索引"
	@echo "  make vector-index      用 bge-m3 写入 Qdrant dense 向量与 vector_manifest"
	@echo "  make validate-sources  校验来源清单的版本/元数据完整性"
	@echo "  make check-segmentation 抽样 20 条 article，生成人工核对清单"
	@echo "  make test              运行离线测试（unit + contract + integration）"
	@echo "  make lint              ruff check"
	@echo "  make format            ruff format（写入）"
	@echo "  make format-check      ruff format --check src tests scripts"
	@echo "  make qa                lint + format-check + test（本地与 CI 共用）"
	@echo "  make ask Q='...'       单题 CLI 调试"
	@echo "  make eval-w2           跑 20 题开发集并生成报告"
	@echo "  make demo              启动 Gradio 单轮页面"
	@echo "  make compose-up        启动 Qdrant（docker compose）"

init: sync

sync:
	$(UV) sync

fetch:
	$(PYTHON) scripts/fetch_corpus.py

parse:
	$(PYTHON) scripts/parse_corpus.py

index:
	$(PYTHON) scripts/build_index.py

index-qdrant:
	$(PYTHON) scripts/build_index.py --init-qdrant

init-w2:
	$(UV) sync --group w2

vector-index:
	$(PYTHON) scripts/build_vectors.py

ask:
	$(PYTHON) scripts/ask.py "$(Q)"

eval-w2:
	$(PYTHON) scripts/eval_w2.py

validate-sources:
	$(PYTHON) scripts/validate_sources.py

check-segmentation:
	$(PYTHON) scripts/check_segmentation.py

test:
	$(PYTEST) tests/unit tests/contract tests/integration

lint:
	$(RUFF) check .

format:
	$(RUFF) format src tests scripts

format-check:
	$(RUFF) format --check src tests scripts

qa: lint format-check test

demo:
	$(PYTHON) -m campusguide.ui.app

compose-up:
	docker compose up -d qdrant

compose-down:
	docker compose down

clean:
	rm -rf .pytest_cache .ruff_cache
