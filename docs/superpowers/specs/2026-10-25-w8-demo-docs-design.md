# CampusGuide W8 设计：演示与文档

| 项目 | 内容 |
|---|---|
| 文档版本 | 0.1（设计稿，待确认） |
| 日期 | 2026-10-25 |
| 基线 | W6 已验收：615 tests；clean MCP run `aed3834d…e1f3`；结构/拒答/语义/quote/trace 全过；support 97.8%；vPro p50 41.7s / p95 174.4s（已记录限制） |
| 当前 UI | W4 单轮 Gradio；只走 QAService，不接 W6 图 |
| 当前部署 | `compose.yaml` 只有 Qdrant；没有 app Dockerfile / 一键 demo |
| 当前 CI | GitHub Actions：ruff + format + pytest；无 Docker/UI 检查 |
| 当前文档 | README 很长且有过时数字（3,952、W6-A 默认 ultraspeed）；无演示脚本/GIF |
| 范围 | 图驱动多轮 UI、引用/checklist/澄清/调试面板、演示档延迟策略、Docker 一键启动、CI、README/合规/演示脚本/GIF |
| 明确不做 | 不改 W6 图/评测基线；不改 W7 冻结集/标签/报告；不做历史版本、跨学校、个人数据、公开多租户 |
| 状态 | UI 已定稿（三栏冷白 + STSong）；Gradio 三栏/移动端抽屉与底部 sheet、fake runner 642 tests 已落地；真实图 `handle_stream` 5/5、真实页面 6/6 smoke 已记录（实际 vPro / 页面展示 ultraspeed，延迟见 §8）；多阶段 Docker / compose app / preflight / `make demo-up` 已落地并验证；CI 已增补 compileall/config/compose 检查；README、演示脚本、合规、W8 周记录已收口（静态截图替代 GIF）。 |

---

## 0. 决策摘要

W8 做六件事：

1. **UI 从 W4 单轮迁到 W6 图**：多轮 session、checklist、clarify、引用展开、拒答原因、调试面板；
2. **演示档与评测档分离**：交互 demo 用 `ultraspeed` 降低延迟，W6 评测仍用 vPro；UI 明确标注，不混指标；
3. **只流式显示节点进度，不流式显示未核对答案**：避免把未通过 Verifier 的文本提前展示；
4. **Docker Compose 一键本地 demo**：Qdrant + app；数据/HF 缓存挂载，`.env` 不入镜像；一条命令启动；
5. **CI 与文档收口**：ruff/pytest/UI 构建/compose config；README 重写为“30 秒看懂 + 5 分钟跑起来 + 指标与限制”；
6. **5 个固定演示场景 + 2 个真实拒答 + GIF/截图**：覆盖政策问答、流程 closed/open、多轮澄清、混合问题、拒答。

W8 **不改变 W7 结论**：W7 仍 failed/tagged；README 必须同时展示 W6 通过和 W7 延迟未过，不选择性隐藏。

---

## 1. 当前基线与缺口

### 1.1 已有

- W6 图：Planner / FlowExecutor / Retriever / Advisor / Verifier / Finalize；`ConversationService` 有 graph 模式；
- `ConversationReply` 已携带 `text/checklist/claims/citations/outcome/decision/handoff/trace/clarification`；
- `create_graph_runtime` 装配 MCP search/meta、flow catalog、三角色 provider；
- `scripts/chat.py --graph` 已有 CLI；
- W6 615 tests、clean MCP run、5 个 MCP 固定场景、12 条红队；
- Qdrant compose、GitHub Actions 基础 CI。

### 1.2 缺口

1. `ui/app.py` 仍是 W4 单轮：没有 session、没有 graph、没有 checklist/clarify；
2. 没有 app Dockerfile；compose 只起 Qdrant；
3. 没有 UI view-model / 组件测试；
4. 没有节点级进度，174s p95 在页面上只会“转圈”；
5. README 有过时数字和默认模型描述；
6. 没有 5 场景演示脚本、截图、GIF、合规声明集中页；
7. CI 不检查 Docker compose、UI 构建或包完整性；
8. `.env`、数据、模型缓存与镜像边界没有明确文档。

---

## 2. 范围与非目标

### 2.1 范围内

- Gradio 多轮图 UI；
- 引用卡片、checklist、clarify 选项、拒答 handoff、调试面板；
- 演示档 `ultraspeed` + 评测档 `vpro` 的显式区分；
- 节点进度事件（planner/flow/retriever/advisor/verifier/finalize）；
- Dockerfile + compose app service + preflight + 一键 `make demo-up`；
- CI 增补 compose config、compileall、UI build tests；
- README、`docs/demo-script.md`、`docs/evaluation.md` 收口、合规声明、GIF/截图；
- 5 个固定场景 + 2 个拒答案例。

### 2.2 非目标

- 公开托管、账号体系、多租户、鉴权（如未来公开，另做反向代理+限流）；
- 历史版本回答、跨学校、个人成绩/学号查询；
- 新检索/生成模型，W6 图/评测重跑；
- W7 冻结集重跑或报告修改；
- 移动端原生 App、文件上传、管理后台；
- 语义核对算法改动；
- 把 W6 的 20 题/5 场景结果外推为 W7 通过。

---

## 3. 用户界面

### 3.1 页面结构

```text
┌───────────────────────┬──────────────────────────────────────┬────────────────────────────┐
│ CampusGuide           │ 会话标题                  [已核实] [结果] │ 最后一轮                    │
│ 华农制度问答           │ 第 N 轮 · 演示档 · 耗时              │ [办理清单] [引用] [调试]     │
│ [＋ 新对话]            ├──────────────────────────────────────┼────────────────────────────┤
│ [搜索对话]             │                                      │ 清单摘要：9 步 / 5 已截止    │
│ 今天                   │ Chatbot：用户与助手多轮              │ 状态筛选 / 显示不适用项      │
│ ● 国奖流程与 2026 截止  │ 助手带 outcome 徽标                   │ ┌ 步骤卡片 ─────────────┐  │
│ ● 国家奖学金申请条件    │ 回答只来自 Finalize                   │ │ 标题 / 状态 / 材料 / 时限 │  │
│ ○ 在读证明开具         │                                      │ │ 部门 / 依据 / 状态说明    │  │
│ 昨天                   │                                      │ └────────────────────────┘  │
│ × 绩点与排名查询       │ [节点进度，仅进度不流式答案]           │ 引用卡片：原文、来源、日期口径 │
│ · 中山大学国家奖学金    │ [澄清 chips / 跳过]                   │ 调试折叠：planner / 检索 / 核对 │
│                       │ [输入框 …………………………] [发送]          │                            │
│ 本机保留 · 刷新不丢失   │ 300 字 · 每分钟 10 次 · 不查询个人信息 │                            │
└───────────────────────┴──────────────────────────────────────┴────────────────────────────┘
```

- 三栏工作台：**左侧历史、中间对话、右侧结构化结果**；顶栏不单独占行，历史与结果都在同一工作区内。
- 中间输入框固定在底部，不随消息滚动；消息区独立滚动，输入、示例和隐私提示始终可见。
- 右侧只展示**当前会话最后一轮**的结构化结果；点击左侧任意历史会加载中栏对话，并自动打开右栏该会话的结果。
- 右栏可以收起；收起后中栏自动变宽；点击“结果”按钮或左侧历史会重新打开。新对话默认收起右栏。
- 调试 tab 默认折叠；`DEMO_DEBUG=true` 时可默认展开。
- 中栏顶部固定展示 `演示档 ultraspeed` 或 `评测档 vPro` 徽标，不能隐藏。
- 字体：正文无衬线；品牌名、会话标题、历史标题、右栏标题、步骤标题、引用标题使用
  `STSong → Source Han Serif SC → Noto Serif CJK SC → Noto Serif SC → Songti SC → serif`
  衬线栈。STSong 为系统字体，不打包进仓库，跨机器由开源宋体回退。
- 会话历史使用同一浏览器 `localStorage`（Gradio `BrowserState`）保留；刷新后恢复会话列表、当前会话、消息和最后一轮结构化结果，不做跨设备续聊。
- 移动端：左上角小按钮打开发历史抽屉；聊天区在上，输入框固定在聊天区底部；结果卡片默认不占位，点历史或“结果”后从底部上滑，约占 40dvh，聊天区保留约 60dvh，互不遮挡。历史抽屉选择会话后自动关闭。

### 3.2 一轮回复的组件

```text
SessionHistoryItem
  ├─ outcome 状态点 / 标题 / 摘要
  └─ 点击 → 中栏加载会话 + 右栏打开最后一轮

AssistantTurn
  ├─ outcome badge: ANSWERED / PARTIAL_ANSWER / CLARIFY / ABSTAIN + reason
  ├─ closed badge（flow.status = closed；独立于 outcome）
  ├─ answer text（只来自 Finalize）
  ├─ checklist summary（完整卡片在右栏）
  ├─ claims + citation cards（policy / mixed）
  ├─ clarification chips + 跳过（CLARIFY）
  ├─ handoff card（拒答 / partial）
  ├─ semantic_unchecked banner（如启用）
  └─ debug payload（右栏折叠）

ResultPanel
  ├─ checklist tab：摘要 / 状态筛选 / 显示不适用项 / 步骤卡片
  ├─ citations tab：claim 原文引用 + 流程条款定位（明确无逐字原文）
  └─ debug tab：trace、耗时、模型与版本；默认折叠、脱敏

Composer
  ├─ 输入框 ≤300 字，空输入不发送
  ├─ 示例问题 chips
  ├─ 每分钟 10 次额度提示
  └─ 固定免责声明与“不查询个人信息”
```

状态徽标：

| outcome | 颜色/文案 |
|---|---|
| ANSWERED | 绿色“已核实回答” |
| PARTIAL_ANSWER | 黄色“部分回答”：claim 被省略时说明“未核实结论已省略”，流程未确认时说明“未确认项已标注” |
| CLARIFY | 蓝色“需要补充信息” |
| ABSTAIN / NO_EVIDENCE / UNVERIFIABLE / GENERATION_FAILED / TOOL_UNAVAILABLE | 灰色/红色“未作答 + 原因” |
| flow.status = closed | 红色“批次已截止”，独立于 outcome，不新增 outcome code |

- 左侧历史条目展示标题、outcome 状态点和摘要；时间弱化或不展示，保持密度。
- 移动端结果区采用 40dvh 底部 sheet：有结果时可用“结果”收起/展开；点历史自动展开；不覆盖聊天和输入框。
- 历史加载的是 UI 保存的消息与最后一轮结果；继续追问时使用同一 `session_id`，服务端图上下文仍在 MemorySaver TTL 内时延续。
- 字体与视觉：冷灰/青绿主色；标题用 STSong 衬线栈，正文用无衬线；粘贴/插入用户原文不转成 HTML，避免 XSS。

### 3.3 Checklist 展示

每个 `ChecklistItem` 渲染为卡片：

- 状态 badge：`可办理 / 待确认 / 受阻 / 已截止 / 已完成 / 不适用`；
- 标题、description；
- 材料列表；
- `deadline` 或 `deadline_note`；
- 负责部门；
- citations（可展开引用）；
- `status_note` / `blocked_reason`；
- 只展示 `not_applicable` 以外的项；调试模式可打开“显示不适用项”。

`closed` 步骤必须显著显示“本批次已截止”，不显示“现在去办”；不推断下一年日期。

### 3.4 引用展开

- 每条 citation 显示：文档标题、条款号、块类型、quote、来源 URL、`date_confidence=low` 提示；
- claim 文本后的 `[文档 条款]` 可定位到对应引用卡片；
- 不展示内部 `text_raw_ref`、MCP payload 或原始 HTML；
- 引用 URL 直连学校官网，不经过本系统代理。

### 3.5 Clarify 交互

- `clarification.allowed_values` 非空 → 渲染为按钮/chips，点击即发送；
- 无 allowed values → 使用输入框；
- 显示“为什么问”：来自 `why_needed` 或 clarification reason；
- “跳过”按钮发送“跳过”，走 W5/W6 的 declined 路径；
- 最多 3 次；超过后显示通用路径 + handoff，不继续循环。

### 3.6 拒答与 handoff

- 显示 reason code 的用户可读文案、缺什么、下一步；
- 显示 `handoff.department` / URL / note；
- 不显示未核实 claims；
- 不出现自由发挥的“建议你……”事实句；
- 隐私请求只提示不要输入个人信息，不回显用户输入中的敏感内容。

### 3.7 调试面板

默认关闭，面试时可打开：

- planner_source（rule/llm/fallback）、intent、flow_id、query plan；
- retrieval：每条证据的 article_id、分数、matched query；
- verifier：hard issues、semantic labels、decision、revise/clarify 次数；
- finalize：outcome、reason、citations 数、handoff；
- 节点耗时、总耗时、模型名、prompt 版本、index_version、trace_id；
- 不显示 API key、`.env`、完整系统提示词、用户隐私原文。

### 3.8 输入与错误

- 单次输入 ≤300 字；空输入不发送；
- 示例问题 chips：国奖条件、国奖流程、在读证明、推免、隐私测试；
- 配置缺失/索引不可用/MCP 超时：显示可操作提示（先启动 Qdrant、配置 `.env`、检查 manifest），不显示堆栈；
- 请求失败不自动重试无限循环；最多 1 次可重试提示；
- 页面固定免责声明和“请勿输入学号/身份证/成绩单”。

### 3.9 限流与成本

- 本地 demo 默认每 session 每分钟最多 10 次请求；超过后提示稍后再试，不排队无限等待；
- `DEMO_DAILY_REQUESTS` 可选上限；达到后显示“今日演示额度已用完”；
- 不提供文件上传、批量导出、自动抓取；
- 不记录完整聊天原文；日志只保留 trace_id、outcome、耗时和脱敏 query；
- 公开部署不在 W8 范围；如未来公开，必须加反向代理、鉴权、全局限流和预算熔断。

---

## 4. 后端接入

### 4.1 统一入口

UI 只调用 `ConversationService`（graph 模式）：

```python
runtime = create_graph_runtime(today=as_of)
reply = runtime.service.handle(
    session_id,
    message,
    as_of=as_of,
    audience=audience,
    profile=profile,
)
```

- 不在 UI 里直接读 YAML、Qdrant 或 MCP；
- 不在 UI 里调用 QAService 旧单轮路径；
- `ConversationReply` 是唯一 UI 数据契约；
- W5 flow-only 与 W6 QA 都由 graph 返回同一 `ConversationReply`。

### 4.2 会话

- `session_id`：每个 UI 会话生成 UUID；`thread_id=session_id`；MemorySaver TTL 24h；
- UI 会话列表、当前会话、消息和最后一轮结构化结果存入 `gr.BrowserState`（浏览器 localStorage，加密），同一浏览器刷新后恢复；不做跨设备续聊；
- 服务端图上下文仍在 MemorySaver 时，恢复的会话可以继续多轮；服务重启导致图上下文丢失时，UI 历史保留，从当前消息开始新图上下文，不伪造旧槽位；
- “重置”清空当前 UI 会话并调用 `service.reset(session_id)`；新对话生成新 UUID；
- 同一 session 串行发送；`demo.queue(default_concurrency_limit=1)`，避免同一 thread 并发写；
- 不把完整聊天原文写入服务端日志或本地文件；localStorage 只服务当前浏览器 UI 恢复。

### 4.3 节点进度

新增 `ConversationService.handle_stream()` / `GraphRunner.stream()`：

```python
for event in service.handle_stream(session_id, message, as_of=as_of):
    if event.kind == "progress":
        yield update_status(event.node, event.label)
    elif event.kind == "final":
        yield render_reply(event.reply)
```

- 使用 LangGraph `stream_mode="updates"` 或节点包装 callback；
- UI 先显示 `planner → flow/retriever → advisor → verifier → finalize` 的状态；
- **不流式输出 claim/answer token**；所有事实文本在 Finalize 后一次性出现；
- 每个节点可显示耗时；超时显示当前节点与已耗时；
- 进度事件失败不影响最终 `invoke` 兼容路径；CLI/测试仍用 `handle`。

### 4.4 演示档

W6 评测固定 `vpro`；W8 交互 demo 的页面档位与真实 run 分开记录：

- `W8_DEMO_PROFILE` **只控制页面展示档位**；真实 run profile 由 `W6_MODEL_PROFILE`/`configs/graph.yaml` 决定，角色模型由 `PLANNER_MODEL/ADVISOR_MODEL/VERIFIER_MODEL` 决定；
- 本机按演示需要可设 `W8_DEMO_PROFILE=ultraspeed`，但调试面板与 smoke 报告必须记录实际 `model_profile`/models；不得把 vPro 延迟写成 ultraspeed 指标；
- compose 的正式演示档应同时把角色模型设为快速版（见 5.3）；UI 徽标明确“演示档 ultraspeed；评测档 vPro”；
- 不把 demo 的延迟/回答混入 W6 报告；
- `configs/graph.yaml` 的 W6 默认仍 vpro，不改评测基线。

### 4.5 延迟策略

- 启动时 `warmup()`：拉起 MCP、加载 bge/reranker，避免首个问题承担 30–60s；当前实现在 `GraphBackend.warmup()`，启动日志同时打印展示档与实际 profile；
- 5 个 demo 场景预热后执行；
- 交互目标（演示档）：p50 ≤15s、p95 ≤45s；记录实际值，不隐藏；
- 单请求上限默认 120s；超时走 `GENERATION_FAILED`/`TOOL_UNAVAILABLE`，不返回半成品；页面级 smoke 为容纳实际 vPro 长尾可显式调大并记录，不能借此改写默认值；
- 进度条 + 节点状态；不显示假的“即将完成”；
- 不做跨用户最终答案缓存；检索缓存可选，但不能缓存答案。

---

## 5. Docker 与一键启动

### 5.1 文件

```text
Dockerfile
.dockerignore
compose.yaml            # qdrant + app
scripts/demo_preflight.py
scripts/demo_smoke.py   # 5 场景 live，可选
scripts/demo_ui_smoke.py # 真实 Gradio 页面级 smoke，可选
```

### 5.2 Dockerfile

- 多阶段 `builder` + `runtime`；基础镜像默认 `python:3.11-slim`，可用
  `--build-arg PYTHON_IMAGE=...` 换镜像源（CN 网络可换 daocloud 镜像）；
- builder 安装 `uv`（0.13.1），`uv sync --frozen --no-dev --group w2`；
  `--no-install-package` 排除 torch/triton/nvidia-*/cuda-*，再用 PyTorch CPU
  index 安装 `torch==2.14.1+cpu`，避免把 CUDA 栈打进镜像；
- 只复制 `pyproject.toml`、`uv.lock`、`src/`、`configs/`、`scripts/`、`README.md`；
- **不复制** `data/`、`.env`、`.git/`、`docs/`、`tests/`（测试镜像另说）；
- 运行时 `PYTHONPATH=/app/src`，确保 `ROOT=/app` 且 `data/configs` 路径正确；
- 非 root 用户 `app`（UID/GID 10001）；
- 环境变量：`GRADIO_SERVER_NAME=0.0.0.0`、`GRADIO_SERVER_PORT=7860`、`HF_HOME=/app/hf_cache`；
- 启动命令：`python -m campusguide.ui.app`；
- 镜像不内置模型权重；HF 缓存挂载 volume，首次启动下载约 4.4GB。

### 5.3 compose

```yaml
services:
  qdrant:
    image: qdrant/qdrant:v1.19.1
    volumes: [./data/qdrant_storage:/qdrant/storage]
    healthcheck: ...
  app:
    build: .
    depends_on:
      qdrant:
        condition: service_healthy
    ports: ["7860:7860"]
    env_file: .env
    environment:
      QDRANT_URL: http://qdrant:6333
      W8_DEMO_PROFILE: ultraspeed
    volumes:
      - ./data:/app/data
      - hf_cache:/app/hf_cache
    healthcheck:
      test: ["CMD", "python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:7860/', timeout=3)"]
      start_period: 240s
volumes:
  hf_cache:
```

- `data/` 必须已包含 parsed/index/vector manifest；镜像不打包语料；
- `.env` 通过 `env_file` 只读注入；不上传 API key；
- app 健康检查等待 MCP 冷启动和模型加载；`start_period` 240s；
- 页面展示档 `W8_DEMO_PROFILE=ultraspeed`，真实模型仍由 `.env` 决定；
- `make demo-up` = `demo-preflight` + `docker compose up -d --build`；失败时打印具体缺失文件/命令。

2026-10-27 已验证：`docker compose config` 通过；镜像 `campusguide:local` 2.79GB，
`torch 2.14.1+cpu`、`cuda False`；fake 容器与真实入口容器均返回 HTTP 200；
真实容器使用宿主 HF 缓存 warmup 5.0s，MCP 子进程与 sparse vocab 路径正常。

### 5.4 preflight

`scripts/demo_preflight.py` 检查：

- `.env` 存在且 `LLM_BASE_URL/LLM_API_KEY/PLANNER_MODEL/ADVISOR_MODEL/VERIFIER_MODEL` 非空；
- `QDRANT_URL` 或 `QDRANT_LOCATION` 已配置（preflight 不主动连接）；
- `data/processed/articles.jsonl`、`documents.jsonl`、`data/manifest.json`、
  `data/vector_manifest.json`、`data/sparse_vocab.json` 存在且非空；
- `data/vector_manifest.json` 的 `points=4026`、`source_content_hash` 与文本
  manifest 的 `content_hash` 一致，且 `index_version` 非空；
- 缺失时打印：
  ```
  make fetch && make parse && make index && make vector-index
  ```
  不自动抓取学校站点（合规边界）。
- 容器内 manifest 里的 Windows 绝对 `vocab_file` 找不到时，回退到
  `/app/data/sparse_vocab.json`，避免 Docker 路径导致检索启动失败。

### 5.5 一条命令的边界

README 必须写清楚：

- “一条命令”指**本地数据已准备 + `.env` 已配置**后 `make demo-up`；
- 公开仓库不包含原始语料、解析文本、向量索引、模型权重或 `.env`；
- 新机器首次需要本地网络抓取/解析/建索引（或从合法本地备份恢复 `data/`），并下载模型；
- 不把“公开 demo 链接”作为 W8 硬目标；本地 Docker + 录屏即可。

---

## 6. CI

### 6.1 现有

`.github/workflows/ci.yml`：uv sync、ruff、format、compileall、YAML 配置加载、
带 `.env.example` 的 `docker compose config`、pytest（642）。

### 6.2 增补

- `python -m compileall src scripts`（已加）；
- `docker compose config`（已加；CI 先 `cp .env.example .env`，不启动容器）；
- UI view-model/build 测试（已有，fake runner，不加载真实模型）；
- `pytest` 全量（642，已加）；
- 可选 `workflow_dispatch` 的 `docker build`（PR 不做，镜像太大）；
- 检查 `configs/graph.yaml`/`calendar.yaml`/`flows` 可加载（已加）；
- 不在 CI 跑真实 LLM、Qdrant、MCP 模型或 live demo。

### 6.3 Gitee/GitHub

- 主仓库在 Gitee；GitHub Actions 作为参考 CI 配置；
- 若 Gitee 侧未接 CI，README 明确“本地 `make qa` 是等价门禁”；
- 不把 GitHub Actions 绿色徽章写成唯一证据；W8 验收记录本地命令与输出。

---

## 7. 文档与演示

### 7.1 README 重写

目标：面试官 30 秒知道是什么，5 分钟能跑；不再堆历史。

结构：

1. 一句话 + 架构图 + 5 个能力标签；
2. 30 秒演示 GIF / 截图；
3. 快速开始：host 与 Docker 两条路径；
4. 5 个演示场景（prompt + 预期）；
5. 评测摘要：W6 通过 + W7 failed 并列；W3/W4/W5 历史链接；
6. 数据来源、截止日期、合规与隐私；
7. 已知限制：vPro 174s p95、W7 未过、历史版本不做、单学校；
8. 复现命令、目录结构、测试数；
9. 免责声明。

必须修正的过时数字：

- 语料 **225 / 244 / 4,026**；向量 **4,026**；
- W6-B 默认 **vPro**；W6-A 的 ultraspeed 是历史/A-B，不是当前默认；
- W7 仍 failed/tagged，不能写成通过。

### 7.2 `docs/demo-script.md`

5 个固定场景，每个包含：

- 用户输入；
- 期望 outcome；
- UI 应显示的面板；
- 讲解点（为什么这样设计）；
- 失败兜底（如果端点长尾）。

场景：

1. **政策问答**：“申请国家奖学金需要满足哪些条件？” → ANSWERED + citations + semantic supported；
2. **隐私拒答**：“查一下我的绩点和排名” → PRIVACY_REQUEST + handoff；
3. **流程 closed**：“国家奖学金怎么申请”（as_of 2026-10-25）→ checklist，数信/农学院 closed；不追问年级；
4. **多轮澄清**：“我要开在读证明”→“本科生”→“在校生” → clarify chips + checklist；
5. **混合问题**：“国家奖学金怎么申请，金额是多少” → checklist + claims + citations。

另加 1 个 **校外/无证据** 拒答作为第 2 个真实拒答案例。

### 7.3 GIF/截图

- 15–30 秒 GIF：输入 → 节点进度 → 回答 + 引用/checklist；
- 3 张静态截图：政策问答、closed checklist、调试面板；
- 存 `docs/assets/`；压缩到 <5MB/张；
- 不出现真实姓名、学号、API key、内部路径；
- 录制使用 demo profile + 预热。

### 7.4 技术博客

1000–1500 字，主题：

- 为什么 claim-first + 硬校验 + 语义核对；
- 为什么流程事实由 YAML/checklist 渲染，不让 LLM 写；
- W6 图的两个回退、W7 失败与延迟限制；
- 不把“能跑”写成“已解决所有问题”。

---

## 8. 测试与验收

### 8.1 离线测试

- `tests/unit/test_w8_ui.py`：outcome badge、checklist 渲染、citation 卡片、clarify chips、debug payload、错误态；
- `tests/contract/test_w8_app.py`：`build_demo()` 可构建；输入空/超长；fake runner 下不加载真实模型；
- `scripts/demo_preflight.py`：缺失文件/point 数不一致/`.env` 缺失的失败路径；
- `docker compose config` 在 CI；
- `ruff`、`pytest` 全量。

### 8.2 Live 5 场景

在 demo profile + 预热后人工执行：

- 5/5 完成（ANSWERED/PARTIAL/CLARIFY 按预期）；
- 2 个拒答案例；
- 引用可展开、checklist 可读、clarify 可点、debug 可开；
- 记录每个场景耗时；目标 p50 ≤15s、p95 ≤45s；
- 超过 120s 的场景必须失败关闭并显示原因，不能挂死。

2026-10-27 真实页面 smoke（`scripts/demo_ui_smoke.py`，实际 vPro、页面展示 ultraspeed）：
6/6 PASS（核心 5/5，拒答 2/2）；warmup 42.6s；p50 1.46s、p95 138.8s。
政策问答 138.8s、混合问题 104.9s，超过默认 120s 的单项在本轮为完成长尾显式使用 240s 上限；
目标 p95≤45s 未达标。报告：`data/eval/w8-ui-smoke.{json,md}`，截图在 `data/eval/w8-ui-smoke/`。

### 8.3 验收门

- `make demo-up` 在已准备 data + `.env` 的机器上启动 app+Qdrant；
- `make qa` 全量通过（预计 630+）；
- `docker compose config` 通过；
- README 快速开始、指标、限制、合规齐全；
- 5 场景 + 2 拒答 + GIF/截图；
- 无原始语料/`.env`/模型权重进入 git；
- W7 冻结文件 hash 不变；
- 不把 W6/W7 指标混写。

### 8.4 非门禁

- 公开 URL、账号体系、移动端原生、历史版本、文件上传；
- 公网高并发/多租户；
- vPro 演示档的 p95 不设硬门禁，但必须记录；交互 demo 用 ultraspeed。

---

## 9. 风险与处理

| 风险 | 影响 | 处理 |
|---|---|---|
| vPro 长尾 174s | 页面像卡死 | demo profile ultraspeed；节点进度；120s fail-closed；录屏兜底 |
| Docker 镜像过大 | 构建/拉取慢 | CPU torch；不打包模型/数据；HF 缓存 volume；多阶段构建 |
| 首次下载模型慢 | 一键启动失败感 | README 写明 ~4.2GB + 镜像加速；start_period 240s；预 warmup |
| 数据缺失 | app 启动后检索空 | preflight 明确报错；不自动抓取；打印 make 命令 |
| MCP 子进程启动失败 | 首问失败 | 启动 warmup；healthcheck；错误文案指向 `.env`/日志 |
| Gradio 并发写同一 thread | 状态串线 | queue concurrency=1；session UUID；串行发送 |
| 未核对文本提前展示 | 引用风险 | 不流式输出 answer token，只流式节点进度 |
| README 过度承诺 | 面试反噬 | W6/W7 并列、延迟限制、数据边界；不写“全自动” |
| CI 无 Docker | compose 语法漏检 | `docker compose config`；可选 workflow_dispatch build |
| 隐私/密钥入镜像 | 合规风险 | `.dockerignore` 排除 `.env/data`；只读挂载；debug 脱敏 |
| W7 边界混淆 | 误称通过 | README/W8 报告固定边界声明；不改 w7 tag |

---

## 10. 实施顺序

1. **UI view-model + stream 契约**：先写 fake reply 渲染测试；
2. **Graph Chat UI**：多轮 session、输入、重置、进度；
3. **结构化面板**：checklist、引用、clarify、拒答 handoff、debug；
4. **演示档/延迟**：ultraspeed profile、warmup、120s 超时、5 场景；
5. **Docker**：Dockerfile、compose app、preflight、healthcheck、`make demo-up`；
6. **CI**：compose config、compileall、UI build tests；
7. **文档/素材**：README 重写、demo-script、GIF/截图、博客；
8. **Live 验收**：5 场景 + 2 拒答 + 记录 p50/p95。

每一步先测试后实现；W8 不改 W6 图/评测口径。

---

## 11. 接口与边界

### 11.1 与 W6

- UI 只消费 `ConversationReply`；新增字段必须向后兼容；
- `handle_stream` 是 W8 增量，`handle` 仍保留；
- 不改 W6 的 Planner/Verifier/模型默认；
- W6 eval 仍 vPro；W8 demo 用 ultraspeed，UI 标注。

### 11.2 与 W5

- Flow checklist 直接渲染；不改 YAML；
- closed/clarify/decline 行为沿用 W6 图；
- W5 CLI 保留。

### 11.3 与 W7

- W7 冻结集/标签/报告不修改；
- README 同时展示 W6 通过和 W7 未通过；
- W8 不重跑 W7，不用 W8 结果刷新 W7。

### 11.4 与 W9/W10

- W9 开源 PR 使用 W8 的 README/演示素材；
- W10 简历数字来自 W6/W7 报告和 W8 live 记录，不从 UI 截图推断。
