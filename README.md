# CampusGuide

面向高校公开制度文档的**条款级**问答与办事指引 Agent（个人作品集项目）。

当前目标院校：**华南农业大学**。当前进度：W1 数据与索引地基、W2 单轮问答垂直切片、W3 引用核对与拒答 v0、W4 MCP 工具层与混合检索、W6-A2 quote 契约、P0 profile-aware 检索、P1 section 证据与生成重试、W7 冻结评测（四轮均已人工标注；remediation4 质量门禁全过，p50 7.37s/p95 21.38s 未过，W7 总门禁未通过）；语料 213 份文档 / 232 个版本 / 3,952 个可检索块（含两份 2020 级扫描件 OCR）。W3 在 W3 v2 代码上跑完 20 题 dev_v0：55/55 引用 ID 有效、0 编造、0 part_no 错误、55/55 quote 可定位；拒答召回 100%、误拒率 0.0%、partial 率 7.1%；warm p50 3.34s、p95 9.78s，废止探针 p50 73.5ms（单列）。**support 标注已完成 56/56（39 supported / 17 partial / 0 unsupported）；draft 支持率 69.6%，rendered 支持率 75.0%，低于 85%，support 门禁未通过，W3 总门禁未通过**；W6-A 提示词收紧已通过（见下）。W2 的 p95 未达 10s 已明确接受，详见 [W2 周记录](docs/weekly/w2.md)；W3 状态见 [W3 周记录](docs/weekly/w3.md)。

W4 已实现 `policy-search` / `policy-meta` 两个只读 FastMCP server、BM25 sparse + bge-m3 dense 的 RRF 混合召回、bge-reranker-v2-m3 重排，以及受众/时间过滤。`articles_v1` 已重建为 3,952 点 hybrid 索引。dev_v0 20 题检索层：Recall@8 在 dense-only / RRF / RRF+rerank 三种模式均为 **1.000**；MRR 分别为 **0.905 / 0.848 / 0.929**。5/5 人群错配 case 通过 MCP 实跑，不返回不匹配条款。W3 回归（同一 20 题、W6-A prompt、hybrid+rerank）：结构 100%、拒答召回 100%、误拒率 0.0%、原因码 100%，partial 率 14.3%；support 未重新人工标注，不能声称通过。详见 [W4 周记录](docs/weekly/w4.md)。

W2 只证明“引用真实、可定位”：答案由 claim-first 生成，正文只由通过校验的 claims 渲染，引用 ID 必须来自本轮检索证据，quote 必须能在原文中定位。W2 **不声称答案质量**。

W3 在 W2 之上增加：quote 映射回 `article.text` 原文 span、claim 数字/日期/金额/期限被引用覆盖、受众/版本 scope、输出隐私扫描、废止探针，以及 `PASS / PARTIAL_ANSWER / UNVERIFIABLE` 决策。W3 仍未做语义蕴含（W6）；本轮人工标注已完成，但 rendered 支持率 75.0% 未达 85%，W3 总门禁未通过。

W6-A 提示词收紧已在 `mimo-v2.6-pro-ultraspeed` 上通过：54/54 人工标注（53 supported / 1 partial / 0 unsupported），draft/rendered 支持率 **98.1%**；结构/拒答/误拒门禁通过；唯一 partial 是 d004/c1（“进入教室”范围边界）。v2.6pro A/B 已完成：support 100%（53/53；rendered 51/51），但出现 2 条 quote mismatch（d012 c4/c5）、d012 PARTIAL，p95 45.2s、warmup 111s，**可靠性低于 ultraspeed**；按可靠性优先，默认模型已回退到 `mimo-v2.6-pro-ultraspeed`（`ADVISOR_MAX_TOKENS=3000`）。W6-A 最终结果仍为 ultraspeed 的 98.1%；v2.6pro 报告保留为 A/B 记录。详见 [W6 周记录](docs/weekly/w6.md)。

W6-A2（2026-10-24）在 W6-A 上增加 quote 连续性契约：quote 必须是同一证据正文的连续子串，不得拼接“引导句 + 非相邻分项”；时间/范围与数字必须原样出现在 quote；一条 claim 只表达一个条件、后果或数量；quote 不可定位时只重试一次，仍失败由 W3 Verifier 丢弃。20 题 hybrid 回归中 d005/d006 已从 PARTIAL 恢复 **ANSWERED**，partial 率 7.1%，结构/拒答/误拒门禁通过；50 draft / 49 rendered 已人工标注 **100% supported**，**W6-A2 总门禁通过**。详见 [W6 周记录](docs/weekly/w6.md)。

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

## W4 能做什么

- 应用通过 stdio 调用两个只读 MCP server：`policy-search`（检索/取条款）与 `policy-meta`（文档/版本/解析），QAService 不再直接访问 Qdrant；
- bge-m3 dense + BM25 风格 sparse（中文单字+双字）各取 top 30，RRF(k=60) 融合，按 `(article_id, part_no)` 去重、按 article 聚合；
- bge-reranker-v2-m3 重排，`min_rerank_score=0.60` + margin 0.03 + token budget 截断；reranker 不可用时失败关闭；
- 受众/时间过滤：角色、学院、专业、入学年份、阶段、培养类型、群体、学籍状态、学年 cycle、`as_of`；未知用户字段保留并给出 warning，只排除明确 mismatch；
- `search_articles` 返回 dense/sparse/RRF/rerank 分数、`index_version`、`applied_filters` 与 warnings；`get_article` 不返回 `text_raw_ref`；
- `policy-meta` 提供 `get_document_versions` / `get_document` / `resolve_version` / `list_documents`；W4 不注册 flow/calendar 工具，客户端缺失工具时返回 `NOT_IMPLEMENTED` 并说明 W5 提供；
- MCP 超时/不可用/索引错误映射为明确拒答；QAService 校验 server 与本地 vector manifest 的 `index_version` 一致；
- `make eval-w4` 对比 dense-only vs hybrid RRF vs hybrid+rerank；`make population-check` 通过 MCP 跑 5 个人群错配 case。

W4 **不做**：W5 流程 DSL/日历、W6 语义蕴含与历史回答、W7 冻结 50 题；W4 不宣称答案质量提升。BM25 仍是词面匹配，case 4/5 暴露的“语义改写召回”缺口留 W6/W7。详细边界见 [W4 设计](docs/superpowers/specs/2026-10-24-w4-mcp-hybrid-retrieval-design.md)。

**P0 增补（2026-10-24）**：`UserProfile` 的学院/专业/入学年份会映射为 organization registry 的名称/别名，追加 profile-aware 查询变体；`rerank_candidates=20` 先重排 RRF top 20 再取 8。`population_v1` 5/5 通过，p004/p005 已命中目标培养方案。P0 检索报告独立为 `data/eval/p0-retrieval-report.{json,md}`，不覆盖 W4 tag 的历史报告。详见 [P0 记录](docs/weekly/p0.md)。

**P1 增补（2026-10-24）**：section-aware 证据选择（宽问题最多 2 个表格 section，课程/学分问题放开）、JSON 空返回与空 claims 各一次重试、`intake_year` 与问题版本年份不一致时提前返回 `VERSION_MISMATCH`。`make e2e-section` 4/4：p004 宽/聚焦 ANSWERED，p005 旧版 VERSION_MISMATCH，2024 版 ANSWERED；P1 检索 overall MRR **0.964**（section MRR 1.000），population 仍 5/5。详见 [P1 记录](docs/weekly/p1.md)。

**W7 冻结评测（2026-10-24）**：50 题冻结集（40 可答 + 10 不可答，5 题版本敏感），test-036 因 gold 过期有记录在案的标注修正（changelog + 归档 v0）。第一轮 remediation run `b55bc436…`：结构 100%、article/section Recall@8 均 **1.000**、article MRR 0.967、拒答召回 10/10、误拒 0.025。人工标注：support **0.993**（135/136，95% CI 0.960–0.999）、answer **0.800**（32/40，CI 0.652–0.895）、version-sensitive **0.600**（3/5）；p50/p95 8.3/32.6s。第二轮 remediation2（prompt `w6a2.1`、多问/列举覆盖、空/截断更大预算、就业意图 section 提权）run `23b384f1…`：结构 100%、Recall@8 1.000、overall MRR 0.958、section MRR 0.900、误拒 0、p95 35.1s；人工标注 support **0.985**（133/135，通过）、answer **0.750**（30/40）、version-sensitive **0.200**（1/5）。test-027/028 修复为 correct，test-037 从 incorrect 升为 partial；但 test-007/031/036/038/039 回归，answer 与 version 反而低于第一轮。**W7 总门禁未通过**：answer 与 version 未达门槛，p50/p95 超 6s/10s。第三轮 remediation3（prompt `w6a2.3`，培养方案多方面问题时聚焦培养目标 section）run `6b9be808…`：support **1.000**、answer **0.875**、version-sensitive **1.000**、误拒 0.025、结构/Recall 均通过；p50/p95 12.5/31.5s 未通过，**质量门禁全绿但总门禁仍为未通过**。A5 检索消融同索引/配置可保留；A4/A2 为 w6a2.1 历史结果，严格同配置需重跑。详见 [W7 周记录](docs/weekly/w7.md)。A5 检索消融（冻结集 40 题）：dense-only Recall@8 0.963 / MRR 0.829；hybrid RRF **1.000 / 0.863**；RRF+rerank **1.000 / 0.958**，section MRR 0.900。A4 profile 消融：baseline 0/5 题错配、0/35 证据；关闭 profile 元数据后 1/5 题、7/34 证据错配（test-040 命中 2024 级方案），拒答/误拒不变。A2 pass-through Verifier 已跑并完成全量 support 标注：136/138 supported、2 partial、0 unsupported（0.986，baseline 0.985），拒答/误拒不变；关闭 Verifier 主要改变 claim 数量/过滤，未观察到 support 下降。A1/A3 N/A。

**W7 remediation4（2026-10-25，采用 `w6a2.4-2026-10-24`）**：冻结集 50 题全量 run 已完成人工标注。主结果：support **1.000**（133/133）、answer correctness **0.875**（35/40）、version-sensitive **1.000**（5/5）、拒答召回 1.000、误拒 0、article Recall@8 1.000；W7 质量门禁全部通过。p50/p95 为 **7.37/21.38s**，超过原 6/10s 阈值，因此正式总门禁仍显示未通过。负责人已接受延迟为 known limitation，W7 以质量门禁通过收口；报告总门禁仍按原定义保留。A5：dense/RRF/RRF+rerank Recall@8 **0.9625/1.000/1.000**，MRR **0.8294/0.8633/0.9583**。A4：profile baseline 错配 0/5 题、0/35 证据；关闭 profile 后 1/5 题、7/34 证据、5/18 引用错配。A2：baseline 133/133 supported；pass-through 137/138 supported、1 partial、0 unsupported，拒答表现不变。详见 [W7 remediation4 报告](data/eval/w7-remediation4-report.md) 与 [W7 周记录](docs/weekly/w7.md)。

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

W2/W3/W4 单轮问答：

```bash
cp .env.example .env      # 填 LLM_BASE_URL / LLM_API_KEY / ADVISOR_MODEL
make init                # 基础依赖（含 FastMCP）
make init-w2             # torch + sentence-transformers + gradio
make compose-up          # 启动 Qdrant（默认 http://localhost:6333）
make index               # 文本索引（W2 需重新生成以带上 date_confidence）
make vector-index        # W4：bge-m3 dense + BM25 sparse，重建 3,952 点
make mcp-smoke           # 启动两个 MCP server，调用 search/get/meta
make ask Q='申请国家奖学金需要满足哪些条件？'
make eval-w2             # 20 题开发集，输出 data/eval/w2-dev-report.{json,md}
make eval-w3             # W3 dense 基线：run + 人工标注模板 + 报告
make eval-w3-hybrid      # W4 回归：hybrid 检索 + 独立 run
make eval-w4             # 检索层 A/B/C：dense vs RRF vs RRF+rerank
make eval-w6a2           # W6-A2 quote 契约 20 题回归
make population-check    # 5 个人群错配 case（真实 MCP + 真实索引）
make e2e-section         # P1 培养方案 section 端到端 4 case
make freeze-w7           # 校验并冻结 W7 50 题测试集
make eval-w7             # 跑 W7 冻结测试集并生成报告
make eval-w7-report      # 人工标注后复用 W7 run 生成报告
make sheet-w3            # 生成 data/eval/w3_support_review.md 核对表
make label-w3 ANNOTATOR=yaukin  # 交互式人工标注 draft claim
make eval-w3-report      # 人工标注后复用 run 生成报告
make demo                # Gradio 单轮页面（默认 127.0.0.1:7860）
```

`make vector-index` 会 `--recreate` 重建 hybrid 索引；只需 dense 时用
`make vector-index-dense`。`make eval-w3` 保持 W3 dense 基线；
`make eval-w3-hybrid` 用 W4 hybrid 检索跑回归并写入独立 run。

`make eval-w3` 会写 `data/eval/w3_run.json`、`data/eval/w3_support_labels.jsonl`
和 `data/eval/w3-dev-report.{json,md}`。报告在 `annotator` 为空或以 `ai`
开头时按 `unlabeled` 处理，不会声称 support 门禁通过。

首次加载 `BAAI/bge-m3` 约 2 GB、`BAAI/bge-reranker-v2-m3` 约 2.2 GB；
国内网络可先设置 `HF_ENDPOINT=https://hf-mirror.com`，并建议设置
`HF_HUB_DISABLE_XET=1`（部分镜像的 Xet 下载会 401）。MCP server 冷启动
需要加载两个模型，约 30–60s；该时间不计入每轮问答 p95。Docker Hub 直连
不稳定时，可先从镜像站拉取同一固定 tag 再重命名：

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

# W2–W4 等价命令
python -m uv sync --group w2
python -m uv run python scripts/build_vectors.py
python -m uv run python scripts/ask.py "申请国家奖学金需要满足哪些条件？"
python -m uv run python scripts/eval_w2.py
python -m uv run python scripts/eval_w3.py
python -m uv run python scripts/eval_w3.py --report-only
python -m uv run python scripts/eval_w4.py
python -m uv run python scripts/mcp_smoke.py
python -m uv run python scripts/check_population_cases.py
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
scripts/            fetch / parse / index / build_vectors / ask / eval_w2 / eval_w3 / eval_w4 / mcp_smoke / population-check / validate / 抽检 CLI
src/campusguide/    domain / ingest / retrieval / verification / qa / flows / agents / mcp / eval / ui
tests/              unit / contract / integration / redteam（全部离线）
docs/               数据来源、评测与质量门、周记录（W1/W2/W3/W4）
```

## 质量门

`make qa`（等价于 `scripts/qa.sh`）执行 `ruff check`、`ruff format --check src tests scripts` 与全量 pytest。所有测试使用 mock HTTP、临时目录、内存 Qdrant 和 fake embedder/chat，不访问学校站点或模型服务。当前测试覆盖抓取、解析、切分、脱敏、版本解析、索引确定性、Qdrant 集合骨架、本地 HTML 端到端管线、W2 的别名/过滤/向量点/dense 检索/claim quote 校验/finalize/guard/trace，W3 的原文 span、中文 fact 覆盖、scope、隐私、废止探针、PASS/PARTIAL/ABSTAIN 决策、run hash 与 support/拒答指标，以及 W4 的 BM25 sparse、RRF/rerank、受众细目过滤、MCP 契约/stdio client、5 个人群错配 case。全量离线测试 **435 passed**（W1/W2 时点 154；W3 后 295；W4 后 367；W6-A2/P0/P1 新增 32；W7 基础设施新增 8；W7 remediation 新增 3；remediation2/A5/A4/A2 新增 22；remediation3/remediation4 后为 435）。

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

W1 的验收状态、实测数字和遗留风险记录在 [docs/weekly/w1.md](docs/weekly/w1.md)；W2/W3/W4/P0/P1/W7 分别在 [W2 周记录](docs/weekly/w2.md)、[W3 周记录](docs/weekly/w3.md)、[W4 周记录](docs/weekly/w4.md)、[P0 记录](docs/weekly/p0.md)、[P1 记录](docs/weekly/p1.md)、[W7 周记录](docs/weekly/w7.md)；工程质量门与 W7 效果评测的边界见 [docs/evaluation.md](docs/evaluation.md)。GitHub Actions 工作流位于 `.github/workflows/ci.yml`；Gitee 仓库可使用本地 `make qa`。

## 合规

- 只使用学校官网公开可访问的制度文件；
- 不抓取登录后页面、申请表、成绩单、名单等个人信息；
- 完整快照、解析文本与 `data/incoming/` 只保存在本地，不进入公开仓库；
- 索引文本对身份证号、学号始终脱敏；手机号、个人邮箱和联系人姓名按来源区分：院系官网公开的教职工名录保留原文（便于师生联系），其余文档脱敏；部门公开座机保留；引用链接指回官网原文；
- 删除/更正入口：如学校要求下架，删除对应 `data/raw`、`data/processed` 与索引即可。

## 计划

总体设计见 [docs/superpowers/specs/2026-10-19-campusguide-design.md](docs/superpowers/specs/2026-10-19-campusguide-design.md)，实施计划见 [docs/superpowers/plans/2026-10-19-campusguide-implementation-plan.md](docs/superpowers/plans/2026-10-19-campusguide-implementation-plan.md)。周记录：[W1](docs/weekly/w1.md)、[W2](docs/weekly/w2.md)、[W3](docs/weekly/w3.md)、[W4](docs/weekly/w4.md)、[W6](docs/weekly/w6.md)。W2 设计见 [docs/superpowers/specs/2026-10-22-w2-single-turn-qa-design.md](docs/superpowers/specs/2026-10-22-w2-single-turn-qa-design.md)；W3 设计见 [docs/superpowers/specs/2026-10-23-w3-citation-verification-abstention-design-v2.md](docs/superpowers/specs/2026-10-23-w3-citation-verification-abstention-design-v2.md)；W4 设计见 [docs/superpowers/specs/2026-10-24-w4-mcp-hybrid-retrieval-design.md](docs/superpowers/specs/2026-10-24-w4-mcp-hybrid-retrieval-design.md)；W5 设计见 [docs/superpowers/specs/2026-10-25-w5-flow-dsl-multiturn-design.md](docs/superpowers/specs/2026-10-25-w5-flow-dsl-multiturn-design.md)；W7 设计见 [docs/superpowers/specs/2026-10-24-w7-frozen-evaluation-design.md](docs/superpowers/specs/2026-10-24-w7-frozen-evaluation-design.md)；W6-A 记录见 [W6 周记录](docs/weekly/w6.md)。
