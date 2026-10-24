# CampusGuide

面向高校公开制度文档的**条款级**问答与办事指引 Agent（个人作品集项目）。

当前目标院校：**华南农业大学**。当前进度：W1 数据与索引地基、W2 单轮问答垂直切片、W3 引用核对与拒答 v0 已实现；语料 213 份文档 / 232 个版本 / 3,952 个可检索块（含两份 2020 级扫描件 OCR）。W3 在 W3 v2 代码上跑完 20 题 dev_v0：55/55 引用 ID 有效、0 编造、0 part_no 错误、55/55 quote 可定位；拒答召回 100%、误拒率 0.0%、partial 率 7.1%；warm p50 3.34s、p95 9.78s，废止探针 p50 73.5ms（单列）。**support 标注已完成 56/56（39 supported / 17 partial / 0 unsupported）；draft 支持率 69.6%，rendered 支持率 75.0%，低于 85%，support 门禁未通过，W3 总门禁未通过**；W6-A 提示词收紧已通过（见下）。W2 的 p95 未达 10s 已明确接受，详见 [W2 周记录](docs/weekly/w2.md)；W3 状态见 [W3 周记录](docs/weekly/w3.md)。

W2 只证明“引用真实、可定位”：答案由 claim-first 生成，正文只由通过校验的 claims 渲染，引用 ID 必须来自本轮检索证据，quote 必须能在原文中定位。W2 **不声称答案质量**。

W3 在 W2 之上增加：quote 映射回 `article.text` 原文 span、claim 数字/日期/金额/期限被引用覆盖、受众/版本 scope、输出隐私扫描、废止探针，以及 `PASS / PARTIAL_ANSWER / UNVERIFIABLE` 决策。W3 仍未做语义蕴含（W6）；本轮人工标注已完成，但 rendered 支持率 75.0% 未达 85%，W3 总门禁未通过。

W6-A 提示词收紧已在 `mimo-v2.6-pro-ultraspeed` 上通过：54/54 人工标注（53 supported / 1 partial / 0 unsupported），draft/rendered 支持率 **98.1%**；结构/拒答/误拒门禁通过；唯一 partial 是 d004/c1（“进入教室”范围边界）。v2.6pro A/B 已完成：support 100%（53/53；rendered 51/51），但出现 2 条 quote mismatch（d012 c4/c5）、d012 PARTIAL，p95 45.2s、warmup 111s，**可靠性低于 ultraspeed**；按可靠性优先，默认模型已回退到 `mimo-v2.6-pro-ultraspeed`（`ADVISOR_MAX_TOKENS=3000`）。W6-A 最终结果仍为 ultraspeed 的 98.1%；v2.6pro 报告保留为 A/B 记录。详见 [W6 周记录](docs/weekly/w6.md)。

**W3 结项决定（2026-10-24）**：W3 v2 自身的 rendered 支持率仍是 75.0%，不追溯修改；用户接受 W6-A 提示词收紧作为 W3 的 support remediation（同一 20 题、同一 Verifier、54/54 人工、98.1%），因此 W3 记为**有条件通过**。W3 baseline 75.0% 和 W6-A 98.1% 都保留在报告中，不合并成同一个数字。W3 代码 `759653b` 与报告随后同步到 Gitee。

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

## W3 能做什么

- 引文从“归一化字符串定位”升级为可映射回 `article.text` 的**原文 span**（空白、全半角、弯引号、零宽、跨行、多 part）；
- 抽取 claim 中的百分比、日期/年份、期限、金额/学分/学时与区间，要求被本轮引用 quote 覆盖；中文数字（`十万`、`两万元`、`连续两年`、`二〇二五年九月一日`）与阿拉伯数字等价；
- 受众/版本 scope：显式受众不兼容、repealed/未生效/过期、同 doc 版本冲突会剔除 evidence；混合版本保留但提示；
- 输出隐私扫描：手机号、身份证、学号、非 `scau.edu.cn` 邮箱命中即丢弃 claim，不记录原始隐私值；
- 主检索为空时跑一次无 LLM 的废止探针：命中 `ONLY_REPEALED`，否则 `NO_EVIDENCE`，探针耗时单列；
- `PASS / PARTIAL / ABSTAIN` 决策：任何未通过核对的 claim 不进入答案；部分作答显示固定提示“部分结论未通过引用核对，已省略；以下仅为可核实内容。”，只渲染通过 claim；
- trace/UI/CLI 显示 decision、partial、issues、scope issues、dropped IDs、探针耗时；20 题 dev_v0 runner 输出 `w3_run.json`、人工标注模板与报告。

W3 **不做**：语义蕴含判断、LLM 重写/重试、多轮追问、MCP/sparse/rerank、完整历史版本解析、流程事实核对 H9、50 题冻结测试集。W3 也不把 AI 预标注当人工验收。详细边界见 [W3 v2 设计](docs/superpowers/specs/2026-10-23-w3-citation-verification-abstention-design-v2.md)。

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

W2/W3 单轮问答：

```bash
cp .env.example .env      # 填 LLM_BASE_URL / LLM_API_KEY / ADVISOR_MODEL
make init-w2              # torch + sentence-transformers + gradio
make compose-up           # 启动 Qdrant（默认 http://localhost:6333）
make index                # 文本索引（W2 需重新生成以带上 date_confidence）
make vector-index         # bge-m3 -> Qdrant dense 1024 + data/vector_manifest.json
make ask Q='申请国家奖学金需要满足哪些条件？'
make eval-w2              # 20 题开发集，输出 data/eval/w2-dev-report.{json,md}
make eval-w3              # W3：run + 人工标注模板 + 报告（不会自动标注）
make sheet-w3             # 生成 data/eval/w3_support_review.md 核对表
make label-w3 ANNOTATOR=yaukin  # 交互式人工标注 56 条 draft claim
make eval-w3-report       # 人工标注后复用 run 生成报告
make demo                 # Gradio 单轮页面（默认 127.0.0.1:7860）
```

`make eval-w3` 会写 `data/eval/w3_run.json`、`data/eval/w3_support_labels.jsonl`
和 `data/eval/w3-dev-report.{json,md}`。报告在 `annotator` 为空或以 `ai`
开头时按 `unlabeled` 处理，不会声称 support 门禁通过。

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
python -m uv run python scripts/eval_w3.py
python -m uv run python scripts/eval_w3.py --report-only
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
scripts/            fetch / parse / index / build_vectors / ask / eval_w2 / eval_w3 / validate / 抽检 CLI
src/campusguide/    domain / ingest / retrieval / verification / qa / flows / agents / mcp / eval / ui
tests/              unit / contract / integration / redteam（全部离线）
docs/               数据来源、评测与质量门、周记录（W1/W2/W3）
```

## 质量门

`make qa`（等价于 `scripts/qa.sh`）执行 `ruff check`、`ruff format --check src tests scripts` 与全量 pytest。所有测试使用 mock HTTP、临时目录、内存 Qdrant 和 fake embedder/chat，不访问学校站点或模型服务。当前测试覆盖抓取、解析、切分、脱敏、版本解析、索引确定性、Qdrant 集合骨架、本地 HTML 端到端管线、W2 的别名/过滤/向量点/dense 检索/claim quote 校验/finalize/guard/trace，以及 W3 的原文 span、中文 fact 覆盖、scope、隐私、废止探针、PASS/PARTIAL/ABSTAIN 决策、run hash 与 support/拒答指标。全量离线测试 **295 passed**（W1/W2 时点 154；W3 标注工具与 W6-A 提示词/prefill 新增 8 项）。

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

W3 的指标口径：

| 指标 | W3 门禁 / 当前状态 |
|---|---|
| 引用 ID 有效率 / 编造 ID / part_no | 100% / 0 / 0；live 55/55、0、0，通过 |
| quote 可定位率 | 100%；live 55/55，通过 |
| 拒答召回 | ≥80%；live 6/6 = 100%，通过 |
| 误拒率 | ≤20%；live 0/14 = 0.0%，通过 |
| partial 率 | 记录；live 1/14 = 7.1% |
| rendered 支持率 | ≥85%，且必须人工标注；**56/56 已标注，rendered 75.0%，未通过** |
| 延迟 | 记录；live warm p50 3.34s / p95 9.78s，探针 p50 73.5ms 单列 |
| W3 总门禁 | **未通过**：人工 support 标注已完成，但 rendered 75.0% <85% |

W3 不把 AI 预标注当人工验收。`data/eval/w3_support_labels.jsonl` 已由 `yaukin` 完成 56 条人工标注（39 supported / 17 partial / 0 unsupported）；`annotator` 为空或以 `ai` 开头的记录仍按 `unlabeled` 处理。当前 live run 在 dirty working tree 上执行（commit `b240e4d`，dirty=True）；W3 已按本轮 75.0% 判定不通过，不以重跑刷分。W6-A 提示词收紧已在 ultraspeed 上通过：54/54 人工标注（53 supported / 1 partial / 0 unsupported），rendered **98.1%**，结构/拒答/误拒门禁通过；唯一 partial 是 d004/c1。v2.6pro A/B 的 support 为 100%（53/53；rendered 51/51），但有 2 条 quote mismatch、d012 PARTIAL、p95 45.2s、warmup 111s，可靠性低于 ultraspeed；默认模型已回退到 `mimo-v2.6-pro-ultraspeed`（`ADVISOR_MAX_TOKENS=3000`）。W6-A 最终报告仍为 ultraspeed 的 98.1%；v2.6pro 报告保留为 A/B 记录。报告见 `data/eval/w6a-dev-report.md`。

W1 的验收状态、实测数字和遗留风险记录在 [docs/weekly/w1.md](docs/weekly/w1.md)；W2/W3 分别在 [W2 周记录](docs/weekly/w2.md)、[W3 周记录](docs/weekly/w3.md)；工程质量门与 W7 效果评测的边界见 [docs/evaluation.md](docs/evaluation.md)。GitHub Actions 工作流位于 `.github/workflows/ci.yml`；Gitee 仓库可使用本地 `make qa`。

## 合规

- 只使用学校官网公开可访问的制度文件；
- 不抓取登录后页面、申请表、成绩单、名单等个人信息；
- 完整快照、解析文本与 `data/incoming/` 只保存在本地，不进入公开仓库；
- 索引文本对身份证号、学号始终脱敏；手机号、个人邮箱和联系人姓名按来源区分：院系官网公开的教职工名录保留原文（便于师生联系），其余文档脱敏；部门公开座机保留；引用链接指回官网原文；
- 删除/更正入口：如学校要求下架，删除对应 `data/raw`、`data/processed` 与索引即可。

## 计划

总体设计见 [docs/superpowers/specs/2026-10-19-campusguide-design.md](docs/superpowers/specs/2026-10-19-campusguide-design.md)，实施计划见 [docs/superpowers/plans/2026-10-19-campusguide-implementation-plan.md](docs/superpowers/plans/2026-10-19-campusguide-implementation-plan.md)。周记录：[W1](docs/weekly/w1.md)、[W2](docs/weekly/w2.md)、[W3](docs/weekly/w3.md)、[W6](docs/weekly/w6.md)。W2 设计见 [docs/superpowers/specs/2026-10-22-w2-single-turn-qa-design.md](docs/superpowers/specs/2026-10-22-w2-single-turn-qa-design.md)；W3 设计见 [docs/superpowers/specs/2026-10-23-w3-citation-verification-abstention-design-v2.md](docs/superpowers/specs/2026-10-23-w3-citation-verification-abstention-design-v2.md)；W6-A 记录见 [W6 周记录](docs/weekly/w6.md)。
