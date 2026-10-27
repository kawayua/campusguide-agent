# CampusGuide

面向华南农业大学公开制度的**条款级问答与办事指引 Agent**（个人作品集项目）。

答案由检索证据 + claim/quote 校验 + 语义核对生成；流程事实由流程 DSL 渲染成 checklist；证据不足、隐私、校外或版本不符时明确拒答，不返回半成品。

> 个人演示项目，不构成官方答复。具体政策以学校官网和负责部门解释为准。请勿输入学号、身份证、成绩单等个人信息。

![CampusGuide 政策问答](docs/assets/w8-policy.png)

`条款级 RAG` · `MCP 工具层` · `LangGraph 四角色` · `流程 checklist` · `引用/语义核对` · `三栏 Gradio UI`

## 30 秒看懂

- **不是**把整篇制度丢给模型总结：每个结论必须绑定本轮检索到的条款和可定位 quote；
- **多轮流程**：国奖申请、推免资格、在读证明有确定性槽位、closed 状态和 checklist；
- **失败关闭**：无证据、引用不支持、版本冲突、隐私、校外问题都有明确 outcome；
- **可演示**：三栏页面、历史恢复、移动端抽屉/底部卡片、节点进度、引用与调试面板；
- **可复现**：本地 host 或 Docker Compose；fake backend 不加载模型也能跑 UI 测试。

## 架构

```text
Browser
  └─ Gradio 三栏 UI（历史 / 对话 / 清单·引用·调试；BrowserState）
       └─ ConversationService（session / thread_id / 串行发送）
            └─ LangGraph
               Planner → FlowExecutor / Retriever → Advisor → Verifier → Finalize
                    │              │
                    │              └─ MCP policy-search（Qdrant 4,026 块；hybrid + rerank）
                    └──────────────── MCP policy-meta（flow DSL / calendar / versions）
```

- 生成与语义核对使用 OpenAI-compatible 模型；
- 向量：`BAAI/bge-m3` dense + BM25 sparse，RRF 融合后用 `bge-reranker-v2-m3` 重排；
- 运行时只通过 MCP 读取 Qdrant/流程事实，UI 不直接访问 YAML、Qdrant 或 MCP。

## 当前状态（2026-10-27）

| 模块 | ultraspeed 记录 |
|---|---|
| 语料 | 225 份文档 / 244 个版本 / 4,026 个可检索块（W1–W7 历史报告按当时 213/232/3,952 记录，不回填） |
| W2 单轮问答 | outcome 20/20；引用 ID 100% 有效；quote 可定位 100%；p50 3.34s / p95 14.7s |
| W3 / W6-A | 55/55 引用有效、0 编造、拒答召回 100%；baseline support 75.0%，W6-A prompt remediation 后 98.1%（54/54 人工） |
| W4 / W5 | 检索 Recall@8 1.000、hybrid+rerank MRR 0.958；3 个流程 closed/open/clarify/handoff |
| W6-A 延迟 | p50 3.5s / p95 15.0s；warmup 29.5s |
| W6-B 完整图 | dev_v0 20 题；结构 38/38、quote mismatch 0、拒答召回 1.0、W6 硬门禁 PASS；p50 20.3s / p95 55.3s；support 0/45 未标注 |
| W8 演示层 | 三栏 UI + 移动端抽屉/底部 sheet + 历史恢复 + `handle_stream` 节点进度；Docker/CI 已落地 |
| 测试 | **646 passed**（离线，无真实模型） |

本 README 只列评测档 `ultraspeed` 的主记录；生产部署模型、vPro 运行与 W7 的完整技术审计、原始 run 和限制见
[评测与质量门](docs/evaluation.md)，不在本页混列其他档位数据。

## 快速开始

### 已有 `data/` 与 `.env`（host）

```bash
cp .env.example .env       # 填 LLM_BASE_URL / LLM_API_KEY / 三个模型
make init-w2               # torch / sentence-transformers / gradio
make compose-up            # Qdrant
make demo                  # Gradio 三栏页面
```

打开 `http://127.0.0.1:7860/`。主界面不展示模型名；模型配置统一由 `.env` 注入。评测档为 `ultraspeed`，生产部署模型与完整审计见 `docs/evaluation.md`。

### Docker（推荐）

```bash
cp .env.example .env       # 填模型配置；主界面不展示模型名
make demo-preflight        # 检查 .env / data / points=4026 / manifest hash
make demo-up               # preflight + docker compose up -d --build
```

- app：`http://127.0.0.1:7860/`；Qdrant：`6333/6334`；
- 首次启动会下载 bge-m3 + reranker（约 4.4GB），`start_period=240s`；
- 镜像不打包 `data/`、`.env` 和模型；`data/`、`hf_cache` 挂载；
- Docker Hub 不可达时可将 `PYTHON_IMAGE` 设为可用镜像源（见 `.env.example` 注释）；
- 查看日志：`docker compose logs -f app`。

### 从零准备数据

```bash
make init-w2
make compose-up
make fetch                 # 按 configs/sources.yaml 抓取/复制公开制度
make parse
make index
make vector-index          # 生成 4,026 块 hybrid 索引
make demo
```

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

## ultraspeed 记录

| 阶段 | 记录 | 结论 |
|---|---|---|
| W2 | 20/20 outcome；引用 ID 100% 有效；quote 可定位 100%；warm p50 3.34s / p95 14.7s | 垂直切片；p95 未达 10s，已接受 |
| W3 | 55/55 引用有效、0 编造；拒答召回 100%；baseline support 75.0% | baseline 未过；W6-A prompt remediation 98.1%，有条件通过 |
| W4 | Recall@8 1.000；hybrid+rerank MRR 0.958；population 5/5 | MCP + 混合检索通过 |
| W5 | 3 个流程；closed/open、clarify、handoff | 流程事实可控，不靠 LLM 编造 |
| W6-A | 54/54 人工，98.1%；p50 3.5s / p95 15.0s；warmup 29.5s | 引用契约通过 |
| W6-B | dev_v0 20 题；结构 38/38、quote mismatch 0、拒答召回 1.0、硬门禁 PASS；p50 20.3s / p95 55.3s | 完整图链路通过；support 尚未人工标注 |
| W8 | 三栏 UI/移动端/历史恢复/节点进度；fake tests 646 passed；Docker/CI 落地 | 演示层完成 |

W6-B ultraspeed 报告：[w6b-ultraspeed-dev-report.md](data/eval/w6b-ultraspeed-dev-report.md)。
W7 的原始技术审计、指标和限制见 [评测与质量门](docs/evaluation.md)；本页只列评测档 ultraspeed 的主记录，不混列生产 vPro 或 W7 数据。

## 数据与合规

- 目标院校：华南农业大学；数据截止 **2026-10-25**；
- 只处理公开制度/办事材料，不查询个人成绩、学号、身份证、宿舍或一卡通；
- 公开仓库不包含原始语料、解析全文、向量索引、模型权重或 `.env`；
- API key 由 `.env` 注入；日志、调试面板与报告脱敏，不展示完整系统提示词或 `text_raw_ref`；
- 不把公开 demo 链接、账号体系或多租户作为当前目标。

详见 [合规与数据边界](docs/compliance.md)。

## 已知限制

- **120s fail-closed**：默认单轮上限 120s，超过后不返回半成品；
- **单学校、公开制度**：不做跨学校、历史版本并排回答、个人数据查询；
- **Docker 体积**：CPU-only 镜像约 2.79GB，首次模型下载约 4.4GB；
- 完整图/W7 的原始评测与限制见 [评测与质量门](docs/evaluation.md)；README 不混列其他档位数据。

## 复现与目录

```bash
make qa                    # ruff + format + 646 离线测试
make demo-fake             # fake UI，不加载模型
make demo                  # 真实图 UI（host）
make demo-preflight        # Docker 前置检查
make demo-up               # preflight + compose
make w8-smoke              # 真实图 handle_stream 5 场景
make demo-ui-smoke         # 真实页面 smoke（先启动 app）
```

```text
src/campusguide/          # agents / conversation / retrieval / flows / mcp / ui
configs/                  # graph、retrieval、flows、sources、organization
data/processed/           # 本地生成，不提交
data/eval/                # 评测报告与 W8 页面 smoke 证据
docs/                     # 设计、周记录、演示脚本、合规
scripts/                  # CLI、评测、preflight、页面 smoke
tests/                    # unit / contract / integration（离线）
Dockerfile                # 多阶段 CPU-only 镜像
compose.yaml              # Qdrant + app
```

历史设计：[W2](docs/superpowers/specs/2026-10-22-w2-single-turn-qa-design.md) · [W3](docs/superpowers/specs/2026-10-23-w3-citation-verification-abstention-design-v2.md) · [W4](docs/superpowers/specs/2026-10-24-w4-mcp-hybrid-retrieval-design.md) · [W5](docs/superpowers/specs/2026-10-25-w5-flow-dsl-multiturn-design.md) · [W6](docs/superpowers/specs/2026-10-25-w6-full-graph-semantic-verification-design.md) · [W8](docs/superpowers/specs/2026-10-25-w8-demo-docs-design.md)。

## 免责声明

本项目为个人学习与作品集演示，不构成学校官方答复、政策解释或办理承诺。制度与流程以华南农业大学官网、学院通知和负责部门解释为准。请勿在演示页面输入真实个人信息。
