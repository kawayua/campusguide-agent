# CampusGuide

面向高校公开制度文档的**条款级**问答与办事指引 Agent（个人作品集项目）。

当前目标院校：**华南农业大学**。当前进度：W1 数据与索引地基、W2 单轮问答垂直切片已实现；语料 213 份文档 / 232 个版本 / 3,952 个可检索块（含两份 2020 级扫描件 OCR）。W2 的 20 题 live 开发集评测已完成：outcome 100%、引用 ID 有效率 100%、quote 可定位率 100%；最新一轮 warm p50 3.3s、p95 14.7s（多轮 14.7–36.8s），bge-m3 冷启动预热 27.4s。p95 未达到 10s 门禁，已明确接受这一限制，详见 [W2 周记录](docs/weekly/w2.md)。

W2 只证明“引用真实、可定位”：答案由 claim-first 生成，正文只由通过校验的 claims 渲染，引用 ID 必须来自本轮检索证据，quote 必须能在 `text_normalized` 中定位。W2 **不声称答案质量**，答案正确性从 W3/W7 开始评估。

这里的“条款级”专指 `article` 块（第 X 条）；培养方案、教师详情、新闻/讲座目录以 `section` 块入库。检索、引用和评测必须显式区分 `block_type`，不能把 `section` 计入条款指标。

> 本系统为个人演示项目，只覆盖指定学校的公开制度文件，不构成官方答复。具体政策以学校官网和负责部门解释为准。请勿输入学号、身份证、成绩单等个人信息。

## W1 能做什么

- 用 `configs/sources.yaml` 登记制度文件与版本；
- 抓取（或复制本地）HTML/PDF 快照，记录 URL、发布日期、哈希与抓取时间；
- 解析正文、切分“章/条”、按规则识别版本、脱敏联系方式，生成条款元数据；
- 生成 `data/processed/articles.jsonl`、`documents.jsonl` 与 `data/manifest.json`；
- 初始化 Qdrant 集合骨架（`articles_v1`，W2 再写入向量）；
- 抽样 20 条条款生成人工切分核对清单；
- 用离线测试和 ruff 守住切分、脱敏、版本解析与索引确定性。

## W2 能做什么

- 单轮问题 → 查询规范化/别名扩展 → 现行 `article` 过滤 → bge-m3 dense 检索；
- 调用 OpenAI-compatible 模型生成 1–5 条 claims，每条结论必须带 `article_id + quote`；
- 确定性校验：引用 ID 必须在本轮 Evidence 中，quote 必须能在原文 `text_normalized` 中定位；
- 只渲染已校验 claims，低置信日期引用显示“日期为锚点”提示；
- 无证据、历史版本、隐私、校外、生成失败、无有效 claim、索引不可用均返回明确原因码；
- 单轮 Gradio 页面、单题 CLI、JSONL trace 与 20 题开发集 runner。

W2 **不做**：多轮、MCP、sparse/rerank、语义蕴含校验、历史版本比较、完整拒答判定树、50 题冻结测试集。详细边界见 [W2 设计](docs/superpowers/specs/2026-10-22-w2-single-turn-qa-design.md)。

## 快速开始

```bash
make init                 # uv sync
cp .env.example .env      # 按需填写，W1 不强制
# 把下载好的公开制度文件放到 data/incoming/，再填写 configs/sources.yaml
make validate-sources     # 查看来源清单统计与缺失字段
make fetch                # 抓取/复制原始快照到 data/raw（已 gitignore）
make parse                # 解析到 data/processed/parsed（已 gitignore）
make index                # 生成条款 JSONL 与 data/manifest.json
make index-qdrant         # 额外创建 Qdrant 集合骨架（需本地 Qdrant）
make check-segmentation   # 生成 20 条人工抽检清单
make qa                   # ruff check + format check + 全量离线测试
```

W2 单轮问答：

```bash
cp .env.example .env      # 填 LLM_BASE_URL / LLM_API_KEY / ADVISOR_MODEL
make init-w2              # torch + sentence-transformers + gradio
make compose-up           # 启动 Qdrant（默认 http://localhost:6333）
make index                # 文本索引（W2 需重新生成以带上 date_confidence）
make vector-index         # bge-m3 -> Qdrant dense 1024 + data/vector_manifest.json
make ask Q='申请国家奖学金需要满足哪些条件？'
make eval-w2              # 20 题开发集，输出 data/eval/w2-dev-report.{json,md}
make demo                 # Gradio 单轮页面（默认 127.0.0.1:7860）
```

首次加载 `BAAI/bge-m3` 需要下载约 2 GB 权重；国内网络可先设置 `HF_ENDPOINT=https://hf-mirror.com`。CPU 可运行但索引较慢；有 NVIDIA GPU 时可安装对应 CUDA wheel，并在 `data/vector_manifest.json` 的 `device` 字段确认实际设备。Docker Hub 直连不稳定时，可先从镜像站拉取同一固定 tag 再重命名，例如：

```bash
docker pull docker.m.daocloud.io/qdrant/qdrant:v1.19.1
docker tag docker.m.daocloud.io/qdrant/qdrant:v1.19.1 qdrant/qdrant:v1.19.1
make compose-up
```

没有 Docker 时可用 Qdrant 本地持久化模式做单机 smoke：`export QDRANT_LOCATION=./data/qdrant_local`，并让 `build_vectors.py` 与 UI/CLI 使用同一路径。该模式只用于本机验证，不是正式部署方式。

缺少 `LLM_BASE_URL` / `LLM_API_KEY` / `ADVISOR_MODEL` 时，UI 和 CLI 会给出配置错误，不会退化成“无引用自由回答”。

`local_path` 建议指向 `data/incoming/` 下的文件；该目录与所有快照、解析结果一样不会进入公开仓库。`url` 仍须填写，作为最终引用链接。

Windows 下若 `uv` 不在 PATH，可显式执行 `make init UV="python -m uv"`。若未安装 `make`，在 Git Bash 中使用等价命令：

```bash
python -m uv sync
python -m uv run python scripts/validate_sources.py
python -m uv run python scripts/fetch_corpus.py
python -m uv run python scripts/parse_corpus.py
python -m uv run python scripts/build_index.py
python -m uv run python scripts/check_segmentation.py
python -m uv run pytest
python -m uv run ruff check .
python -m uv run ruff format --check src tests scripts

# W2 等价命令
python -m uv sync --group w2
python -m uv run python scripts/build_vectors.py
python -m uv run python scripts/ask.py "申请国家奖学金需要满足哪些条件？"
python -m uv run python scripts/eval_w2.py
python -m uv run python -m campusguide.ui.app
```

扫描版 PDF 需要可选 OCR 依赖。识别缓存只写入本地 `data/processed/ocr/`，原始 PDF 不改动；解析时按 SHA-256 匹配后自动接入：

```bash
python -m uv sync --group ocr
python -m uv run --group ocr python scripts/ocr_scans.py --only <doc_id>@<version_id>
```

OCR 默认逐页比较 200dpi 与 240dpi 两个渲染结果，以覆盖更多的版本为底稿并补齐漏行；表格仍按 OCR 行序保留，文本未经逐字校对，引用以官网原文为准。

## 来源清单

`configs/sources.yaml` 是唯一数据入口，字段与填写方式见 [docs/data-sources.md](docs/data-sources.md)。

W1 验收目标：文档版本 ≥ 50 个、制度本体 ≥ 30 个；20 条 `article` 抽检切分正确率 ≥ 95%。当前语料由项目作者收集，仓库只提交清单、元数据与哈希，不提交完整原文。字段说明、版本规则和抽检流程见该文档。

## 目录

```text
configs/            来源清单、模型/检索/拒答配置、流程骨架
data/               本地语料与生成物（incoming/raw/snapshots/processed 不入库）
scripts/            fetch / parse / index / build_vectors / ask / eval_w2 / validate / 抽检 CLI
src/campusguide/    domain / ingest / retrieval / flows / agents / mcp / eval / ui
tests/              unit / contract / integration / redteam（全部离线）
docs/               数据来源、评测与质量门、周记录
```

## 质量门

`make qa`（等价于 `scripts/qa.sh`）执行 `ruff check`、`ruff format --check src tests scripts` 与全量 pytest。所有测试使用 mock HTTP、临时目录、内存 Qdrant 和 fake embedder/chat，不访问学校站点或模型服务。当前测试覆盖抓取、解析、切分、脱敏、版本解析、索引确定性、Qdrant 集合骨架、本地 HTML 端到端管线，以及 W2 的别名、过滤、向量点 ID、dense 检索去重、claim/quote 校验、finalize、guard、trace 脱敏与 fake 端到端。

W2 的指标口径：

| 指标 | W2 门禁 |
|---|---|
| 引用 ID 有效率 | 100% |
| 编造 ID 数 | 0 |
| quote 可定位率 | 100% |
| `article_recall@8` / `section_recall@8` | 记录基线，不设硬门禁 |
| 非可答题原因码正确率 | 记录基线，W3 设门禁 |
| p50 / p95 延迟（warm） | p50 ≤6s、p95 ≤10s（本地 demo 环境；冷启动预热单列） |

W2 不报告答案正确率。开发集报告明确标注“开发集 v0，非冻结测试集”。p50/p95 是 warm 状态；UI/CLI 启动时会预加载 bge-m3，冷启动预热时间单独记录在报告里（本机最新实测 27.4s）。

W1 的验收状态、实测数字和遗留风险记录在 [docs/weekly/w1.md](docs/weekly/w1.md)；W1 工程质量门与 W7 效果评测的边界见 [docs/evaluation.md](docs/evaluation.md)。GitHub Actions 工作流位于 `.github/workflows/ci.yml`；Gitee 仓库可使用本地 `make qa`。

## 合规

- 只使用学校官网公开可访问的制度文件；
- 不抓取登录后页面、申请表、成绩单、名单等个人信息；
- 完整快照、解析文本与 `data/incoming/` 只保存在本地，不进入公开仓库；
- 索引文本对身份证号、学号始终脱敏；手机号、个人邮箱和联系人姓名按来源区分：院系官网公开的教职工名录保留原文（便于师生联系），其余文档脱敏；部门公开座机保留；引用链接指回官网原文；
- 删除/更正入口：如学校要求下架，删除对应 `data/raw`、`data/processed` 与索引即可。

## 计划

总体设计见 [docs/superpowers/specs/2026-10-19-campusguide-design.md](docs/superpowers/specs/2026-10-19-campusguide-design.md)，实施计划见 [docs/superpowers/plans/2026-10-19-campusguide-implementation-plan.md](docs/superpowers/plans/2026-10-19-campusguide-implementation-plan.md)。周记录：[W1](docs/weekly/w1.md)、[W2](docs/weekly/w2.md)。W2 设计见 [docs/superpowers/specs/2026-10-22-w2-single-turn-qa-design.md](docs/superpowers/specs/2026-10-22-w2-single-turn-qa-design.md)。
