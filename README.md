# CampusGuide

面向华南农业大学公开制度的**条款级问答与办事指引 Agent**（个人作品集项目）。

答案由检索证据 + claim/quote 校验 + 语义核对生成；流程事实由流程 DSL 渲染成 checklist；证据不足、隐私、校外或版本不符时明确拒答或限答；部分回答只展示通过核验的结论并提示缺项。本轮已完成全量 OCR 与索引重建，内容及合理路由通过率为 86%，严格交付通过率为 78%，详见下方实验对比。

> 个人演示项目，不构成官方答复。具体政策以学校官网和负责部门解释为准。请勿输入学号、身份证、成绩单等个人信息。

![CampusGuide 政策问答](docs/assets/w8-policy.png)

`条款级 RAG` · `MCP 工具层` · `LangGraph 四角色` · `流程 checklist` · `引用/语义核对` · `三栏 Gradio UI`

## 30 秒看懂

- **不是**把整篇制度丢给模型总结：每个结论必须绑定本轮检索到的条款和可定位 quote；
- **多轮流程**：国奖申请、推免资格、在读证明有确定性槽位、closed 状态和 checklist；
- **失败关闭**：无证据、引用不支持、版本冲突、隐私、校外问题都有明确 outcome；
- **可演示**：三栏页面、历史恢复、移动端抽屉/底部卡片、节点进度、引用与调试面板；
- **可复现**：本地 host 或 Docker Compose；fake backend 不加载模型也能跑 UI 测试。

## 项目结构与处理流程

项目分为离线知识库建设与在线问答两条链路：**资料搜索与采集 → OCR 解析入库 → 检索 → 重排序 → LLM 基于证据回答 → 引用与事实核验**。当前实验语料已完成全量 OCR，主识别采用 Xiaomi OCR，Gemini 处理疑难区域；页覆盖与表格核验后，生成父子块和元数据并重建向量索引。

```text
离线：知识库建设
公开制度资料搜索 / 来源清单（configs/sources.yaml）
  → 抓取与保存原始文档、来源和版本
  → 页面 / 区域准备 → Xiaomi OCR → Gemini 疑难区域处理
  → 页覆盖核验 + 表格结构与来源核验
  → 条款 / 表格父子块 + 文档、版本、页码、受众等元数据
  → 生成 documents.jsonl / articles.jsonl / manifest.json
  → bge-m3 dense + BM25 sparse → 重建 Qdrant 混合检索索引

在线：证据驱动问答
Browser → Gradio 三栏 UI → ConversationService → LangGraph
  → Planner（识别问题、路由与澄清）
  → Retriever / FlowExecutor
      ├─ MCP policy-search → dense + sparse 检索 → RRF 融合
      │                      → bge-reranker-v2-m3 重排序 → 证据选择
      └─ MCP policy-meta → flow DSL / calendar / versions
  → Advisor（LLM 基于证据与流程事实生成回答）
  → Verifier（引用、quote、事实与覆盖核验）→ Finalize → 页面展示
```

### 资料采集、全量 OCR 与入库

资料入口由 `configs/sources.yaml` 管理，通过采集脚本保存公开制度快照及来源信息。OCR 处理覆盖本轮实验的全库语料：Xiaomi OCR 承担主识别，Gemini 处理疑难区域；随后核验页面覆盖、表格结构和来源，再把结果转换为可检索的条款、表格父子块与元数据。这里的“全量”指实验语料库，不表示已覆盖学校官网的全部资料或实时更新。

父块保留表格整体结构与上下文，子块承载可检索的具体内容，通过 `table_id` / `parent_table_id` 建立关联。文档与块模型记录文档/版本身份、来源、页码、区域位置、表格关系及受众等信息，供版本过滤、上下文恢复和引用定位使用。文本索引产物与 manifest 用于追踪语料身份；向量索引在本轮 OCR 入库后重新构建，避免继续使用旧解析内容的向量。

### 检索、重排序与回答

检索层使用 `BAAI/bge-m3` 生成 dense 向量，同时以 BM25 sparse 做词项检索；两路候选经 RRF 融合，再由 `bge-reranker-v2-m3` 重排序。检索服务结合版本、受众和问题范围选择证据，控制上下文预算，并保留可定位的来源信息。

LangGraph 负责问题规划、资料检索、流程执行、答案生成与核验。Advisor 使用 OpenAI-compatible LLM 基于证据生成回答；Verifier 检查 claim/quote、引用支持、事实与覆盖情况。办事步骤来自流程 DSL，隐私、校外、版本不符或证据不足时进入相应拒答、澄清或限答路由。生成与语义核对的模型通过环境变量配置，Gemini 的疑难区域 OCR 职责与在线回答职责分别配置。

运行时通过 MCP 访问检索与流程事实；UI 通过 `ConversationService` 管理 session、thread_id 和串行发送，不直接读取 YAML、Qdrant 或 MCP。

### 代码模块

| 模块 / 路径 | 职责 |
|---|---|
| `src/campusguide/ingest/` | 资料采集、解析、Xiaomi OCR、Gemini 区域处理、版面路由、结构化、分块、版本及索引产物 |
| `src/campusguide/domain/` | 文档、条款、表格、来源位置与受众等数据模型和约束 |
| `src/campusguide/retrieval/` | dense / sparse 编码与索引、混合检索、RRF、重排序、过滤与证据预算 |
| `src/campusguide/mcp/` | `policy-search` / `policy-meta` 服务、工具协议与客户端 |
| `src/campusguide/agents/` | LangGraph 状态和编排，Planner、Retriever、Advisor、Verifier 及覆盖处理 |
| `src/campusguide/verification/` | 引用定位、语义支持、事实、条件、范围与覆盖核验 |
| `src/campusguide/flows/` | 办事流程执行、槽位与 checklist |
| `src/campusguide/conversation/`、`ui/` | 会话服务、历史恢复、流式进度与 Gradio 页面 |
| `src/campusguide/eval/`、`qa/`、`observability/` | 评测、质量检查与运行观测 |
| `configs/` | 来源、模型、OCR 路由、检索、图编排及流程配置 |
| `scripts/`、`tests/` | 采集/解析/建库/评测入口及 unit、contract、integration 测试 |

## 当前状态与实验对比（2026-11-04）

**本轮已完成全库 Xiaomi OCR → Gemini 疑难区域处理 → 页覆盖与表格核验 → 父子块及元数据入库 → 重建向量索引。** 最新实验结果由项目作者提供：内容正确完整及合理路由 **43/50（86%）**，严格交付通过 **39/50（78%）**，最终 Recall@12 **92.67%**，最终 MRR@12 **0.9630**，p95 **15.6 秒**。

### 50 题实验结果

| 指标 | 原版 B | 修改后 V7（原生库，0.80） | 最新全量 OCR 实验 |
|---|---:|---:|---:|
| 内容正确完整及合理路由 | 35/50（70%） | 32/50（64%） | **43/50（86%）** |
| 严格交付通过 | 15/50（30%） | 22/50（44%） | **39/50（78%）** |
| 最终 Recall@5 | 84.67% | 84.00% | **87.33%** |
| 最终 Recall@8 | 86.67% | 88.00% | **90.00%** |
| 最终 Recall@12 | 89.67% | 88.00% | **92.67%** |
| 最终 MRR@12 | 0.9420 | 0.8900 | **0.9630** |
| p5 | 未记录 | 未记录 | **4.5 秒** |
| p95 | 50.26 秒 | 119.72 秒 | **15.6 秒** |

相较原版 B，本轮内容通过增加 **8 题 / 16 个百分点**，严格交付增加 **24 题 / 48 个百分点**；Recall@5、@8、@12 分别提高 **2.66、3.33、3.00 个百分点**，MRR@12 提高 **0.0210**，p95 缩短 **34.66 秒（约 69.0%）**。相较原生库 V7，内容通过增加 **11 题 / 22 个百分点**，严格交付增加 **17 题 / 34 个百分点**，Recall@12 提高 **4.67 个百分点**，MRR@12 提高 **0.0730**，p95 缩短 **104.12 秒（约 87.0%）**。

“内容正确完整及合理路由”和“严格交付通过”是不同判定口径，分别保留计数；Recall/MRR 衡量检索效果，不能等同于最终答案通过率。延迟按本轮提供的 **p5（第 5 百分位）**、p95 记录，不将 p5 改写为 p50。

以上为现有实验结果的版本对比。历史 B 与原生库 V7 来自已有报告；最新全量 OCR 数值依据作者本次实验汇总更新，未附新的逐题明细、运行配置及计时边界。因此，此表用于展示阶段变化；是否完全同题、同模型、同配置和同计时边界仍需以原始运行记录核对，提升幅度不能单独归因于 OCR。

### 历史实验与复现边界

全量 OCR 之前，原生解析诊断库记录为 225 份文档、244 个版本，全部 `native_only`，共 10,047 个存储块，其中 9,589 个可检索、458 个表格父块。这些是**历史诊断库规模**，不作为本轮全量 OCR 后的块数。

早期还做过“原生库 + 21 个 OCR 影子块”的 8 题定向对照：内容通过 **4/8 → 6/8**，严格交付 **1/8 → 1/8**；两组最终 Recall@5/@8/@12 均为 **87.50%**、MRR@12 均为 **0.8125**，p95 **86.76 → 134.08 秒**。该实验仅验证局部 OCR 增补，与本次全库重建分开记录。

历史 V7 使用过测评进程覆盖阈值 `0.80`，仓库 `configs/retrieval.yaml` 仍为 `0.60`；本轮全量 OCR 实验未提供阈值和索引标识，不据此修改默认运行配置。历史收尾离线测试为 2,639 passed、9 个警告，编译通过；当时全库 lint 有 19 项问题、格式检查有 164 文件未通过。这些检查属于此前记录，不代表本轮已重新执行或通过 CI。

[历史 V7 阶段总结](docs/reports/v7-final-report-20261104.md)和[历史指标 JSON](docs/reports/v7-final-metrics-20261104.json)记录的是全量 OCR 完成之前的状态，其中“全库 OCR 未完成”的描述仅适用于当时；最新完成状态及成绩以本页为准。早期 W1–W8 记录见下方与[评测文档](docs/evaluation.md)，提交范围和历史工程检查见 [Git 准备说明](docs/git-preparation-v7-20261104.md)。

## UI 改动对比

本次同时保留并纳入现有 UI 改动，以下按代码差异说明；原有页面截图用于演示，未作为本次新版截图，也未新增浏览器实测结论。

| 位置 | 修改前 | 当前实现 |
|---|---|---|
| 输入发送 | 输入框 `submit` 与发送按钮分别绑定回调 | Enter 转交发送按钮；Shift+Enter 保留换行；中文输入法组合状态不触发发送 |
| 用户气泡 | 浅绿色背景、深色文字、圆角边框 | 深绿色渐变、白色文字、非对称圆角和阴影 |
| 节点进度 | 深色宽条，附带一行说明 | 紧凑浅色胶囊，绿色活动节点，移除额外说明；隐藏 Gradio 默认状态条 |
| 输入框与按钮 | 底部对齐，38px 发送按钮 | 居中对齐，36px 发送按钮，调整收缩和溢出样式；移动端保留 34px 按钮 |

## 快速开始

### 已有 `data/` 与 `.env`（host）

```bash
cp .env.example .env       # 填 LLM_BASE_URL / LLM_API_KEY / 三个模型
make init-w2               # torch / sentence-transformers / gradio
make compose-up            # Qdrant
make demo                  # Gradio 三栏页面
```

打开 `http://127.0.0.1:7860/`。主界面不展示模型名；模型配置统一由 `.env` 注入。历史演示档为 `ultraspeed`；历史 V7 问答实测使用 Gemini 3.5 Flash Lite；当前模型由环境变量选择。启动演示不会自动切换到本轮全量 OCR 实验索引，需核对运行配置与索引 manifest。

### Docker（推荐）

```bash
cp .env.example .env       # 填模型配置；主界面不展示模型名
make demo-preflight        # 检查 .env / data / 当前 manifest 与索引身份
make demo-up               # preflight + docker compose up -d --build
```

- app：`http://127.0.0.1:7860/`；Qdrant：`6333/6334`；
- 首次启动会下载 bge-m3 + reranker（约 4.4GB），`start_period=240s`；
- 镜像不打包 `data/`、`.env` 和模型；`data/`、`hf_cache` 挂载；
- Docker Hub 不可达时可将 `PYTHON_IMAGE` 设为可用镜像源（见 `.env.example` 注释）；
- 查看日志：`docker compose logs -f app`。

### 数据准备入口与全量 OCR 复现

```bash
make init-w2
make compose-up
make fetch                 # 按 configs/sources.yaml 抓取/复制公开制度
make parse-native          # 原生解析诊断；不会复现全量 OCR 实验
make index
make vector-index          # 对本地解析产物构建 hybrid 索引
make demo
```

上述命令提供基础采集、原生诊断和建库流程。`make parse` / `make parse-vision` 使用 `configs/vision.yaml` 的 Gemini 视觉链路；`make parse-native` 不调用 Xiaomi。**这些默认命令不等同于本轮已完成的全量 Xiaomi OCR 实验流程。**

仓库提供 Xiaomi 本地批处理入口 `scripts/run_xiaomi_ocr_local.py` 和区域路由入口 `scripts/run_ocr_router.py`，本地安装根目录为 `D:/xiaomi-ocr`。区域路由脚本本身只输出影子产物，不写入 Qdrant。复现全量实验需要准备对应的页面/区域清单与 OCR 运行环境，依次完成 Xiaomi 识别、Gemini 疑难区域处理、页覆盖及表格核验，再将父子块和元数据桥接至索引产物并重建向量索引；本轮专用编排命令与产物路径尚未在本页提供。

重建前保留所需旧索引，实验使用独立输出路径与集合；在线服务启动前核对文档版本、manifest 和目标集合，确认使用本轮 OCR 产物。

离线 UI（不加载模型、不需要 Qdrant）：

```bash
make demo-fake
```

## 5 个演示场景 + 拒答

| 场景 | 输入 | 期望 | 页面体现 |
|---|---|---|---|
| 政策问答 | 申请国家奖学金需要满足哪些条件？ | ANSWERED | 5 条引用；无 checklist |
| 流程 closed | 国家奖学金怎么申请 | ANSWERED / closed | 5 个可见办理步骤，批次已截止 |
| 多轮澄清 | 我要开在读证明 → 本科生 → 在校生 → 升学 | CLARIFY ×3 → PARTIAL_ANSWER | 澄清按钮、4 项 checklist |
| 混合问题 | 国家奖学金怎么申请，金额是多少 | ANSWERED | checklist + 政策引用 |
| 隐私拒答 | 查一下我的绩点和排名 | PRIVACY_REQUEST | 不查询个人信息，handoff |
| 校外拒答 | 隔壁学校的奖学金政策是什么？ | OUT_OF_SCOPE | 只回答华农公开制度 |

完整讲解、面板预期和失败兜底见 [演示脚本](docs/demo-script.md)。

## 历史 ultraspeed 记录（截至 2026-10-27）

| 阶段 | 记录 | 结论 |
|---|---|---|
| W2 | 20/20 outcome；引用 ID 100% 有效；quote 可定位 100%；warm p50 3.34s / p95 14.7s | 垂直切片；p95 未达 10s，已接受 |
| W3 | 55/55 引用有效、0 编造；拒答召回 100%；baseline support 75.0% | baseline 未过；W6-A prompt remediation 98.1%，有条件通过 |
| W4 | Recall@8 1.000；hybrid+rerank MRR 0.958；population 5/5 | MCP + 混合检索通过 |
| W5 | 3 个流程；closed/open、clarify、handoff | 流程事实可控，不靠 LLM 编造 |
| W6-A | 54/54 人工，98.1%；p50 3.5s / p95 15.0s；warmup 29.5s | 引用契约通过 |
| W6-B | dev_v0 20 题；结构 39/39、quote mismatch 0、拒答召回 1.0、误拒 0、硬门禁 PASS；p50 20.2s / p95 43.6s | 完整图链路通过；p95 达标，p50 未达 15s；support 92.7%（39 supported / 3 partial / 0 unsupported；rendered 92.7%） |
| W8 | 三栏 UI/移动端/历史恢复/节点进度；fake tests 646 passed；Docker/CI 落地 | 演示层完成 |

W6-B ultraspeed 报告：[w6b-ultraspeed-final-dev-report.md](data/eval/w6b-ultraspeed-final-dev-report.md)。
W7 的原始技术审计、指标和限制见 [评测与质量门](docs/evaluation.md)；本节保留早期 ultraspeed 记录；最新全量 OCR 成绩见页面上方，历史 V7 详见阶段总结，跨题集指标不直接混比。

## 数据与合规

- 目标院校：华南农业大学；早期快照截止 **2026-10-25**，后续诊断使用冻结补采快照，未声明官网实时全量覆盖；
- 只处理公开制度/办事材料，不查询个人成绩、学号、身份证、宿舍或一卡通；
- 公开仓库不包含原始语料、解析全文、向量索引、模型权重或 `.env`；
- API key 由 `.env` 注入；日志、调试面板与报告脱敏，不展示完整系统提示词或 `text_raw_ref`；
- 不把公开 demo 链接、账号体系或多租户作为当前目标。

详见 [合规与数据边界](docs/compliance.md)。

## 已知限制

- **答案仍有失败样本**：本轮内容及路由通过 43/50、严格交付通过 39/50，尚有 7 题和 11 题分别未达到对应标准；具体原因需结合逐题记录分析。
- **单学校、公开制度**：不查询个人记录；全量 OCR 覆盖的是实验语料，指定年份、班型、文档关系和官网后续更新仍需核对。
- **延迟有测量边界**：本轮 p5 4.5 秒、p95 15.6 秒是实验统计值，不代表所有请求均在 15.6 秒内完成；历史超时记录属于旧版本，跨版本需核对计时口径。
- **复现信息待补齐**：最新实验的语料/索引标识、全量产物数量、逐题结果、模型配置及计时边界未随本次汇总提供；默认 Makefile 入口不会自动复现该实验，也不能仅凭汇总视为正式上线验收通过。
- **Docker 体积**：历史 CPU-only 镜像约 2.79GB，首次检索模型下载约 4.4GB；本地 Xiaomi OCR 需另行准备运行环境与权重。

## 复现与目录

```bash
make qa                    # ruff + format check + 当前离线测试集
make demo-fake             # fake UI，不加载模型
make demo                  # 真实图 UI（host）
make demo-preflight        # Docker 前置检查
make demo-up               # preflight + compose
make w8-smoke              # 真实图 handle_stream 5 场景
make demo-ui-smoke         # 真实页面 smoke（先启动 app）
```

```text
src/campusguide/          # ingest / domain / retrieval / agents / verification / flows / mcp / conversation / ui
configs/                  # graph、retrieval、flows、sources、organization
data/processed/           # 本地生成，不提交
data/eval/                # 历史评测；RAG/OCR 大型实验产物只在本地保存
docs/                     # 设计、周记录、演示脚本、合规；reports 放可提交汇总
scripts/                  # CLI、评测、preflight、页面 smoke
tests/                    # unit / contract / integration（离线）
Dockerfile                # 多阶段 CPU-only 镜像
compose.yaml              # Qdrant + app
```

历史设计：[W2](docs/superpowers/specs/2026-10-22-w2-single-turn-qa-design.md) · [W3](docs/superpowers/specs/2026-10-23-w3-citation-verification-abstention-design-v2.md) · [W4](docs/superpowers/specs/2026-10-24-w4-mcp-hybrid-retrieval-design.md) · [W5](docs/superpowers/specs/2026-10-25-w5-flow-dsl-multiturn-design.md) · [W6](docs/superpowers/specs/2026-10-25-w6-full-graph-semantic-verification-design.md) · [W8](docs/superpowers/specs/2026-10-25-w8-demo-docs-design.md)。

## 免责声明

本项目为个人学习与作品集演示，不构成学校官方答复、政策解释或办理承诺。制度与流程以华南农业大学官网、学院通知和负责部门解释为准。请勿在演示页面输入真实个人信息。
