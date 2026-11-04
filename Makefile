SHELL := /bin/sh
UV ?= $(shell command -v uv 2>/dev/null || echo python -m uv)
PYTHON ?= $(UV) run python
PYTEST ?= $(UV) run pytest
RUFF ?= $(UV) run ruff
DOCKER_COMPOSE ?= docker compose

.PHONY: help init sync fetch parse parse-native parse-vision index index-strict index-vision index-qdrant vector-index vector-index-dense shadow-build validate-sources \
	check-segmentation test lint format format-check qa ask eval-w2 eval-w3 eval-w3-hybrid eval-w4 \
	eval-w6a2 eval-w6a2-report eval-w6b eval-w6b-report eval-w6b-mcp eval-w6b-mcp-report eval-w6b-scenarios sheet-w6b label-w6b sheet-w6b-mcp label-w6b-mcp import-w6b-mcp freeze-w7 eval-w7 eval-w7-report eval-w7-a5 eval-w7-a4 eval-w7-a4-report eval-w7-a2 eval-w7-a2-report draft-w7 mcp-smoke population-check e2e-section eval-w3-report label-w3 sheet-w3 \
	label-w6a2 sheet-w6a2 demo demo-fake demo-ui-smoke demo-up demo-preflight w8-smoke w5-chat w5-demo w6-chat compose-up compose-down clean

help:
	@echo "CampusGuide commands"
	@echo "  make init              安装基础依赖（uv sync）"
	@echo "  make init-w2           安装 W2 依赖（torch / sentence-transformers / gradio）"
	@echo "  make fetch             按 configs/sources.yaml 抓取/复制原始快照"
	@echo "  make parse             解析 data/raw（默认 vision_first，失败显式报错）"
	@echo "  make parse-native      仅原生 DOM/文本结构，结果标记 native_only"
	@echo "  make parse-vision      同 make parse（显式 vision_first）"
	@echo "  make index             生成 articles.jsonl、documents.jsonl、manifest.json"
	@echo "  make index-strict      强制校验 parse_report 与致命质量告警"
	@echo "  make index-vision      正式门禁：全部来源必须 vision_first"
	@echo "  make shadow-build      在 data/shadow 独立构建影子文本索引"
	@echo "  make index-qdrant      额外初始化 Qdrant 空集合与 payload 索引"
	@echo "  make vector-index      重建 W4 dense+sparse 索引（--recreate）"
	@echo "  make vector-index-dense 只构建 dense（W2/W3 兼容）"
	@echo "  make mcp-smoke         启动 policy-search/policy-meta 做 stdio smoke"
	@echo "  make population-check  通过 MCP 跑 5 个人群错配验收 case"
	@echo "  make e2e-section       P1 培养方案 section 端到端 4 case"
	@echo "  make draft-w7          AI 辅助生成 W7 50 题 draft + 核对表"
	@echo "  make freeze-w7         校验并冻结 W7 50 题测试集"
	@echo "  make eval-w7           跑 W7 冻结测试集并生成报告"
	@echo "  make eval-w7-report    人工标注后复用 W7 run 生成报告"
	@echo "  make eval-w7-a5        W7 A5 检索消融（dense/RRF/rerank）"
	@echo "  make eval-w7-a4        W7 A4 profile 过滤消融"
	@echo "  make eval-w7-a4-report  A4 标注后复用 run 生成报告"
	@echo "  make eval-w7-a2        W7 A2 Verifier 消融（pass-through，全量 support 待标）"
	@echo "  make eval-w7-a2-report  A2 标注后复用 run 生成报告"
	@echo "  make eval-w4           dense vs hybrid RRF vs hybrid+rerank 检索对比"
	@echo "  make eval-w6a2         W6-A2 quote 契约 20 题回归（独立 run）"
	@echo "  make eval-w6a2-report  人工标注后复用 W6-A2 run 生成报告"
	@echo "  make eval-w6b          W6-B 图级 20 题回归（local-hybrid）"
	@echo "  make eval-w6b-mcp      W6-B 生产 MCP 20 题回归（独立 run）"
	@echo "  make eval-w6b-scenarios  W6-B MCP 流程/混合固定场景"
	@echo "  make eval-w6b-report   人工标注后复用 W6-B run 生成报告"
	@echo "  make sheet-w6b         生成 W6-B Markdown 核对表"
	@echo "  make label-w6b ANNOTATOR=你的名字  标注 W6-B local claims"
	@echo "  make sheet-w6b-mcp     生成 W6-B MCP 核对表"
	@echo "  make label-w6b-mcp ANNOTATOR=你的名字  标注 W6-B MCP claims"
	@echo "  make import-w6b-mcp ANNOTATOR=你的名字  从 MCP 核对表写回 labels"
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
	@echo "  make demo              启动 Gradio 三栏页面（真实图；页面展示档 ultraspeed）"
	@echo "  make demo-fake         启动 Gradio 三栏页面（无模型 fake backend）"
	@echo "  make demo-ui-smoke     真实页面 smoke（先启动 app；URL/SCREENSHOTS 可覆盖，TIMEOUT 仅记录）"
	@echo "  make demo-preflight    检查 .env / 语料 / manifest / points"
	@echo "  make demo-up           preflight + docker compose up -d --build"
	@echo "  make demo-down         停止 app 与 Qdrant"
	@echo "  make w8-smoke          真实图 5 场景 live smoke（AS_OF=2026-10-25）"
	@echo "  make w5-chat           启动 W5 流程 CLI（可加 AS_OF=2026-10-25）"
	@echo "  make w6-chat           启动 W6 图 CLI（vPro，可加 AS_OF=...）"
	@echo "  make w5-demo           跑 W5 脚本化流程 demo（SCENARIO=certificate）"
	@echo "  make compose-up        启动 Qdrant（docker compose）"

init: sync

sync:
	$(UV) sync

fetch:
	$(PYTHON) scripts/fetch_corpus.py

parse:
	$(PYTHON) scripts/parse_corpus.py --vision-first

parse-native:
	$(PYTHON) scripts/parse_corpus.py --native-only

parse-vision:
	$(PYTHON) scripts/parse_corpus.py --vision-first

index:
	$(PYTHON) scripts/build_index.py

index-strict:
	$(PYTHON) scripts/build_index.py --parse-report data/processed/parse_report.json --strict

index-vision:
	$(PYTHON) scripts/build_index.py --parse-report data/processed/parse_report.json --require-vision

shadow-build:
	$(PYTHON) scripts/build_shadow.py

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

draft-w7:
	$(PYTHON) scripts/draft_w7_testset.py

freeze-w7:
	$(PYTHON) scripts/freeze_testset.py

eval-w7:
	$(PYTHON) scripts/eval_w7.py

eval-w7-report:
	$(PYTHON) scripts/eval_w7.py --report-only

eval-w7-a5:
	$(PYTHON) scripts/eval_w7_a5.py

eval-w7-a4:
	$(PYTHON) scripts/eval_w7_a4.py

eval-w7-a4-report:
	$(PYTHON) scripts/eval_w7_a4.py --report-only

eval-w7-a2:
	$(PYTHON) scripts/eval_w7_a2.py

eval-w7-a2-report:
	$(PYTHON) scripts/eval_w7_a2.py --report-only

population-check:
	$(PYTHON) scripts/check_population_cases.py

e2e-section:
	$(PYTHON) scripts/e2e_section_check.py

ask:
	$(PYTHON) scripts/ask.py "$(Q)"

w5-chat:
	$(PYTHON) scripts/chat.py $(if $(AS_OF),--as-of $(AS_OF),)

w6-chat:
	$(PYTHON) scripts/chat.py --graph $(if $(AS_OF),--as-of $(AS_OF),)

w5-demo:
	$(PYTHON) scripts/flow_demo.py --scenario $(or $(SCENARIO),scholarship)

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
	W8_DEMO_PROFILE=ultraspeed $(PYTHON) -m campusguide.ui.app

demo-fake:
	DEMO_FAKE=1 $(PYTHON) -m campusguide.ui.app

demo-ui-smoke:
	$(PYTHON) scripts/demo_ui_smoke.py --request-timeout $(or $(TIMEOUT),120) $(if $(URL),--url $(URL)) $(if $(SCREENSHOTS),--screenshots $(SCREENSHOTS))

demo-preflight:
	$(PYTHON) scripts/demo_preflight.py

demo-up: demo-preflight
	$(DOCKER_COMPOSE) up -d --build
	@echo "demo: http://127.0.0.1:7860/（首次启动需下载 bge/reranker，start_period 240s）"

demo-down:
	$(DOCKER_COMPOSE) down

w8-smoke:
	$(PYTHON) scripts/demo_smoke.py $(if $(AS_OF),--as-of $(AS_OF),)

compose-up:
	docker compose up -d qdrant

compose-down:
	docker compose down

clean:
	rm -rf .pytest_cache .ruff_cache

eval-w6b:
	$(PYTHON) scripts/eval_w6b.py

eval-w6b-report:
	$(PYTHON) scripts/eval_w6b.py --report-only

eval-w6b-mcp:
	$(PYTHON) scripts/eval_w6b.py --mcp \
		--run data/eval/w6b-mcp-final-run.json \
		--labels data/eval/w6b-mcp-final_support_labels.jsonl \
		--output-json data/eval/w6b-mcp-final-dev-report.json \
		--output-md data/eval/w6b-mcp-final-dev-report.md

eval-w6b-mcp-report:
	$(PYTHON) scripts/eval_w6b.py --report-only \
		--run data/eval/w6b-mcp-final-run.json \
		--labels data/eval/w6b-mcp-final_support_labels.jsonl \
		--output-json data/eval/w6b-mcp-final-dev-report.json \
		--output-md data/eval/w6b-mcp-final-dev-report.md

eval-w6b-scenarios:
	$(PYTHON) scripts/eval_w6b_mcp_scenarios.py

sheet-w6b:
	$(PYTHON) scripts/label_w3.py --run data/eval/w6b-run.json \
		--labels data/eval/w6b_support_labels.jsonl --sheet \
		--sheet-output data/eval/w6b_support_review.md

label-w6b:
	$(PYTHON) scripts/label_w3.py --run data/eval/w6b-run.json \
		--labels data/eval/w6b_support_labels.jsonl --annotator "$(ANNOTATOR)"

sheet-w6b-mcp:
	$(PYTHON) scripts/label_w3.py --run data/eval/w6b-mcp-final-run.json \
		--labels data/eval/w6b-mcp-final_support_labels.jsonl --sheet \
		--sheet-output data/eval/w6b-mcp-final_support_review.md

label-w6b-mcp:
	$(PYTHON) scripts/label_w3.py --run data/eval/w6b-mcp-final-run.json \
		--labels data/eval/w6b-mcp-final_support_labels.jsonl --annotator "$(ANNOTATOR)"

import-w6b-mcp:
	$(PYTHON) scripts/import_w7_review.py --annotator "$(ANNOTATOR)" \
		--support-labels data/eval/w6b-mcp-final_support_labels.jsonl \
		--support-review data/eval/w6b-mcp-final_support_review.md
