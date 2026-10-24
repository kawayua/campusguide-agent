SHELL := /bin/sh
UV ?= $(shell command -v uv 2>/dev/null || echo python -m uv)
PYTHON ?= $(UV) run python
PYTEST ?= $(UV) run pytest
RUFF ?= $(UV) run ruff

.PHONY: help init sync fetch parse index index-qdrant vector-index vector-index-dense validate-sources \
	check-segmentation test lint format format-check qa ask eval-w2 eval-w3 eval-w3-hybrid eval-w4 \
	eval-w6a2 eval-w6a2-report mcp-smoke population-check eval-w3-report label-w3 sheet-w3 \
	label-w6a2 sheet-w6a2 demo compose-up compose-down clean

help:
	@echo "CampusGuide commands"
	@echo "  make init              安装基础依赖（uv sync）"
	@echo "  make init-w2           安装 W2 依赖（torch / sentence-transformers / gradio）"
	@echo "  make fetch             按 configs/sources.yaml 抓取/复制原始快照"
	@echo "  make parse             解析 data/raw 下的 HTML/PDF"
	@echo "  make index             生成 articles.jsonl、documents.jsonl、manifest.json"
	@echo "  make index-qdrant      额外初始化 Qdrant 空集合与 payload 索引"
	@echo "  make vector-index      重建 W4 dense+sparse 索引（--recreate）"
	@echo "  make vector-index-dense 只构建 dense（W2/W3 兼容）"
	@echo "  make mcp-smoke         启动 policy-search/policy-meta 做 stdio smoke"
	@echo "  make population-check  通过 MCP 跑 5 个人群错配验收 case"
	@echo "  make eval-w4           dense vs hybrid RRF vs hybrid+rerank 检索对比"
	@echo "  make eval-w6a2         W6-A2 quote 契约 20 题回归（独立 run）"
	@echo "  make eval-w6a2-report  人工标注后复用 W6-A2 run 生成报告"
	@echo "  make sheet-w6a2        生成 W6-A2 Markdown 核对表"
	@echo "  make label-w6a2 ANNOTATOR=你的名字  交互式标注 W6-A2 claims"
	@echo "  make validate-sources  校验来源清单的版本/元数据完整性"
	@echo "  make check-segmentation 抽样 20 条 article，生成人工核对清单"
	@echo "  make test              运行离线测试（unit + contract + integration）"
	@echo "  make lint              ruff check"
	@echo "  make format            ruff format（写入）"
	@echo "  make format-check      ruff format --check src tests scripts"
	@echo "  make qa                lint + format-check + test（本地与 CI 共用）"
	@echo "  make ask Q='...'       单题 CLI 调试"
	@echo "  make eval-w2           跑 20 题开发集并生成报告"
	@echo "  make eval-w3           跑 W3 开发集（默认 dense）"
	@echo "  make eval-w3-hybrid    用 W4 hybrid 检索跑 W3 回归（独立 run）"
	@echo "  make eval-w3-report    复用已有 run + support labels 生成报告"
	@echo "  make sheet-w3          生成 56 条 Markdown 标注核对表"
	@echo "  make label-w3 ANNOTATOR=你的名字  交互式人工标注"
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
	$(PYTHON) scripts/build_vectors.py --recreate

vector-index-dense:
	$(PYTHON) scripts/build_vectors.py --recreate --dense-only

mcp-smoke:
	$(PYTHON) scripts/mcp_smoke.py

population-check:
	$(PYTHON) scripts/check_population_cases.py

ask:
	$(PYTHON) scripts/ask.py "$(Q)"

eval-w2:
	$(PYTHON) scripts/eval_w2.py

eval-w3:
	$(PYTHON) scripts/eval_w3.py

eval-w3-hybrid:
	$(PYTHON) scripts/eval_w3.py --retrieval hybrid \
		--run data/eval/w4-w3-regression-run.json \
		--labels data/eval/w4-w3-regression-labels.jsonl \
		--output-json data/eval/w4-w3-regression-report.json \
		--output-md data/eval/w4-w3-regression-report.md

eval-w3-report:
	$(PYTHON) scripts/eval_w3.py --report-only

eval-w4:
	$(PYTHON) scripts/eval_w4.py

eval-w6a2:
	$(PYTHON) scripts/eval_w3.py --retrieval hybrid \
		--run data/eval/w6a2-run.json \
		--labels data/eval/w6a2_support_labels.jsonl \
		--output-json data/eval/w6a2-dev-report.json \
		--output-md data/eval/w6a2-dev-report.md

eval-w6a2-report:
	$(PYTHON) scripts/eval_w3.py --report-only \
		--run data/eval/w6a2-run.json \
		--labels data/eval/w6a2_support_labels.jsonl \
		--output-json data/eval/w6a2-dev-report.json \
		--output-md data/eval/w6a2-dev-report.md

sheet-w6a2:
	$(PYTHON) scripts/label_w3.py --run data/eval/w6a2-run.json \
		--labels data/eval/w6a2_support_labels.jsonl --sheet \
		--sheet-output data/eval/w6a2_support_review.md

label-w6a2:
	$(PYTHON) scripts/label_w3.py --run data/eval/w6a2-run.json \
		--labels data/eval/w6a2_support_labels.jsonl --annotator "$(ANNOTATOR)"

label-w3:
	$(PYTHON) scripts/label_w3.py --annotator "$(ANNOTATOR)"

sheet-w3:
	$(PYTHON) scripts/label_w3.py --sheet

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
