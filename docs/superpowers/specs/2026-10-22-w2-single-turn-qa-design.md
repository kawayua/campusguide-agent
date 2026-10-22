# CampusGuide W2 设计：单轮问答垂直切片

| 项目 | 内容 |
|---|---|
| 文档版本 | 0.1（实现前设计稿） |
| 日期 | 2026-10-22 |
| 来源 | `2026-10-19-campusguide-design.md`、`2026-10-19-campusguide-implementation-plan.md`、W1 周记录 |
| 范围 | W2 单轮问答：查询 → 检索 → claim-first → 引用 ID/quote 校验 → 最小 UI |
| 当前基线 | 213 文档 / 232 版本 / 3,952 块；`article` 1,366、`section` 2,586 |
| 状态 | 设计已确认，待实现 |

---

## 0. 决策摘要

W2 采用**线性服务**，不提前引入 LangGraph；W6 再把同一批纯函数包进 Planner / Retriever / Advisor / Verifier 图。

已确认的技术选择：

1. **Embedding**：本地 `BAAI/bge-m3`，1024 维 dense；W2 只做 dense，W4 再加 sparse 与 reranker。
2. **生成模型**：OpenAI-compatible API；厂商、base URL 和模型名由 `.env` 提供，代码不绑定厂商。
3. **交付面**：最小 Gradio 单轮页面 + Docker Qdrant；不做多轮、MCP、流程 DSL。
4. **检索默认**：`block_type=article`、现行 active 版本；`section` 写入索引但只走调试/后续扩展。
5. **校验边界**：W2 校验引用 ID 存在、quote 可定位；数字/日期覆盖和语义蕴含留给 W3/W6。

---

## 1. 目标与非目标

### 1.1 W1 基线

W1 已交付可复现的文本索引：

- `data/processed/articles.jsonl`：3,952 个可检索块；
- `data/processed/documents.jsonl`：232 个版本；
- `data/manifest.json`：内容哈希与计数可重复；
- 1,366 个 `article`（第 X 条）与 2,586 个 `section`（培养方案节、教师段落、新闻目录）；
- 16 个版本的日期为低置信锚点（`notes` 标注 `date_confidence=low`）。

### 1.2 W2 目标

让一条单轮问题走完：

```text
问题
  → 查询规范化/别名
  → 现行 article 过滤 + dense 检索
  → Evidence[]
  → LLM 生成 claims
  → article_id + quote 校验
  → 只渲染已校验 claim
  → trace + 最小 Gradio
```

W2 完成时应当可以：

- 对 20 题开发集 v0 逐题运行并产出报告；
- 可答题给出带真实引用的答案；
- 无证据、历史版本、隐私、明显校外问题给出明确原因码；
- 引用 ID 有效率 100%，零编造 ID，quote 可定位率 100%；
- 在本地 Docker Qdrant + 本地 bge-m3 + 配置好的 API 上完成一次演示。

### 1.3 非目标

W2 明确不做：

- 多轮对话、槽位追问、流程 checklist；
- MCP server、FastMCP 契约；
- sparse 检索、RRF、bge-reranker；
- 语义 Verifier、claim-quote 蕴含判断；
- 完整拒答判定树和历史版本比较；
- 50 题冻结测试集和消融；
- Docker Compose 打包整个 app、公开 demo；
- 对 `section` 块做条款质量声明。

---

## 2. 总体架构

```text
┌────────────────────────────────────────────────────────────┐
│ Gradio 单页（问题、可选受众、答案、引用卡片、调试面板）      │
└──────────────────────────────┬─────────────────────────────┘
                               │
┌──────────────────────────────▼─────────────────────────────┐
│ QAService（W2 线性编排，不上 LangGraph）                    │
│                                                            │
│ guard → normalize/alias → filters → dense retrieve         │
│      → build evidence → chat provider → claim validator    │
│      → finalize → trace                                    │
└───────────────┬──────────────────────────────┬─────────────┘
                │                              │
┌───────────────▼──────────────┐  ┌────────────▼─────────────┐
│ Qdrant articles_v1           │  │ OpenAI-compatible Chat   │
│ dense: bge-m3 1024           │  │ ADVISOR_MODEL            │
│ payload: 条款/版本/块类型等   │  │ JSON claims              │
└──────────────────────────────┘  └──────────────────────────┘
```

### 2.1 为什么不先上 LangGraph

W2 的验收点是“引用是否真实”，不是“图是否复杂”。线性服务可以：

- 用纯函数测试每个环节；
- 在没有 checkpointer、回退边和状态合并的情况下先跑通主链；
- 在 W6 把 `normalize/retrieve/generate/validate` 原样包进图节点，减少重构风险。

W2 的 `QAService` 只依赖接口，不依赖 Gradio、LangGraph 或具体 provider。

---

## 3. 数据与索引契约

### 3.1 补齐 `date_confidence`

W1 只在 `notes` 里写 `date_confidence=low`，W2 必须把它变成结构化字段：

- `SourceVersion`、`DocumentVersion`、`Article`、`ArticlePublic` 增加
  `date_confidence: Literal["high", "low"] = "high"`；
- 从 `notes` 中识别 `date_confidence=low` 回填；预期 16 个版本；
- 构建校验：`low` 版本数必须等于 16，否则报错；
- 引用卡片对 `low` 显示“日期为文号年份/月份锚点，非精确发布日”。

### 3.2 `block_type`

W2 的检索、Evidence、Citation 和 UI 都必须携带 `block_type`：

| 类型 | W2 用法 |
|---|---|
| `article` | 默认检索；引用显示“第 X 条”；计入条款召回/引用指标 |
| `section` | 写入索引；只通过显式参数或后续版本检索；不计入条款指标 |
| `preface/appendix/other` | 不索引为可检索块；W1 已排除 |

`PolicyFilters` 增加 `block_types: list[BlockType] = [BlockType.ARTICLE]`。W2 默认只查 `article`。

`Citation` 增加 `part_no`（默认 1）：同一 `article_id` 可能有多个 part，没有 `part_no` 就无法确定 quote 来自哪一片段，也无法正确回链到 Qdrant 点。Evidence、Citation 和 UI 都以
`(article_id, part_no)` 作为片段身份，`article_id` 仍是对外展示的主引用 ID。

### 3.3 Qdrant payload

每个点保存检索与引用所需字段，不保存整篇文档：

```python
{
    "article_id": str,
    "doc_id": str,
    "version_id": str,
    "doc_title": str,
    "doc_type": str,
    "authority_level": str,
    "block_type": str,
    "chapter_path": list[str],
    "article_no": str,
    "article_index": int,
    "part_no": int,
    "text": str,
    "text_normalized": str,
    "audience": list[str],
    "audience_colleges": list[str],
    "audience_majors": list[str],
    "status": str,
    "effective_from": str,       # RFC3339 UTC
    "effective_to": str | None,  # RFC3339 UTC；null 表示长期有效
    "date_confidence": str,
    "source_url": str,
    "published_at": str,
    "issuing_department": str,
    "topic_tags": list[str],
    "flow_ids": list[str],
    "source_sha256": str,
}
```

payload 索引至少覆盖：`article_id`、`doc_id`、`version_id`、`block_type`、`doc_type`、
`authority_level`、`audience`、`audience_colleges`、`audience_majors`、`status`、
`date_confidence`、`flow_ids`、`effective_from`、`effective_to`。

### 3.4 向量文本

嵌入文本固定为：

```text
标题：{doc_title}
章节：{chapter_path or 无}
位置：{article_no}
正文：{text}
```

- 使用 bge-m3，`normalize_embeddings=True`；
- `max_seq_length=1024`，W1 单块最多 800 字，留出标题和章节空间；
- W2 只生成 dense 向量；collection 仍保留 W1 的 sparse 配置，W4 再写入 sparse。

### 3.5 点 ID 与幂等

Qdrant point ID 使用确定性 UUID5：

```python
uuid.uuid5(NAMESPACE_URL, f"campusguide:{article_id}:part{part_no}")
```

- 同一 `article_id + part_no` 重复构建得到同一 ID；
- 默认 upsert；提供 `--recreate` 重建集合；
- 构建完成后写 `data/vector_manifest.json`，记录 collection、embedding model、维度、点数、
  源 manifest 的 `content_hash`、设备与构建时间；
- 启动时校验 manifest 与 Qdrant 实际点数/维度，不一致时提示重建，不回退到旧索引。

### 3.6 集合与 alias

- 集合：`articles_v1`（沿用 W1 约定）；
- alias：`articles_current`；
- Docker 镜像使用固定 tag，不使用 `latest`；
- 向量 manifest 与文本 manifest 分离：文本 manifest 仍由 W1 脚本维护。

---

## 4. 检索设计

### 4.1 查询规范化与别名

`normalize_query` 继续做 NFKC、空白压缩；W2 增加一个小而可审计的别名表（`configs/aliases.yaml`）：

```yaml
aliases:
  保研: [推免, 推荐免试]
  评奖: [奖学金, 奖学金评审]
  挂科: [不及格, 补考]
  休学: [学籍异动, 休学办理]
```

规则：

- 别名只扩展检索文本，不改变用户原始问题；
- 每个问题最多生成 2 个检索变体；
- 别名表可测试、可版本化，不把同义词硬编码进 prompt。

### 4.2 过滤器

W2 的 `PolicyFilters`：

```python
class PolicyFilters(BaseModel):
    block_types: list[BlockType] = [BlockType.ARTICLE]
    audience: list[Audience] | None = None
    status: list[ArticleStatus] = [ArticleStatus.ACTIVE]
    as_of: date
    version_mode: VersionMode = VersionMode.CURRENT
    doc_ids: list[str] | None = None
```

Qdrant 过滤条件：

- `block_type` 在 `block_types` 中；
- `status == active`；
- `effective_from <= as_of`；
- `effective_to is null OR effective_to >= as_of`；
- 用户显式选择受众时：`audience` 匹配 `[selected, all]`；
- `doc_ids` 只在调试/开发集使用。

W2 不实现：

- `audience_detail` 的学院/专业/年级过滤（W4）；
- `HISTORICAL` / `INCLUDE_REPEALED` 的版本比较（W4，且必须先有 `date_confidence` 语义）；
- 跨文档版本冲突处理（W3/W4）。

### 4.3 检索与证据

1. dense 检索 `top_k=20`（配置可调）；
2. 按 `article_id` 聚合多个 `part_no`，保留分数最高的片段；
3. 过滤低于 `min_dense_score` 的候选；初值 0.35，必须用 20 题开发集重新校准；
4. 最多保留 8 条 Evidence；单条最多 800 字，并按 `token_budget`（中文约 1 字≈1 token）裁剪总预算；
5. 每条 Evidence 记录 `article`、`score`、`rank`、`retrieval_query`。

`section` 检索只在显式 `block_types` 包含 `SECTION` 时启用，并单独记录 `section_recall@k`。section 开发题只选培养方案、新闻目录等非个人页面，不选教师名录、领导分工、辅导员名单等含个人联系方式的内容。

### 4.4 无证据与前置 guard

W2 只实现最小的确定性 guard，完整判定树在 W3：

| 情况 | 行为/原因码 |
|---|---|
| 无检索结果或全部低于阈值 | `NO_EVIDENCE` |
| 问题含“去年/旧版/废止/2020 年版本” | `HISTORICAL_NOT_SUPPORTED` |
| 明显隐私请求（学号/成绩/排名/身份证/手机号 + 第一人称） | `PRIVACY_REQUEST` |
| 明显其他学校（“隔壁学校/其他学校/外校”，不匹配“华南农业大学”全称） | `OUT_OF_SCOPE` |
| 问题为空或超长 | `INVALID_REQUEST` |

这些 guard 只做正则/规则判断，不调用 LLM；宁可漏拦，也不要把本校问题误判为校外。

---

## 5. 生成与校验

### 5.1 ChatProvider

```python
class ChatProvider(Protocol):
    def complete(
        self,
        messages: list[dict[str, str]],
        *,
        temperature: float = 0.2,
        max_tokens: int = 1200,
    ) -> ChatResponse: ...
```

- `OpenAICompatibleChat` 使用 `httpx` 请求 `{base_url}/chat/completions`；
- 从 `.env` 读取 `LLM_BASE_URL`、`LLM_API_KEY`、`ADVISOR_MODEL`；缺失时启动即报错；
- 超时 40 秒，最多重试 1 次（仅 429/5xx/网络错误）；
- 不使用流式输出；
- `FakeChatProvider` 用于离线测试，返回预置 JSON。

不把厂商 SDK 作为硬依赖；只要服务兼容 `/chat/completions` 即可。

### 5.2 Claim JSON 契约

LLM 只输出 claims，不输出最终正文：

```json
{
  "claims": [
    {
      "claim_id": "c1",
      "text": "申请国家奖学金需要满足……",
      "kind": "eligibility",
      "citations": [
        {
          "article_id": "undergraduate-status-rules@v2021#第十二条",
          "quote": "原文中的连续片段"
        }
      ]
    }
  ]
}
```

约束：

- 1–5 条 claim；每条事实性结论至少 1 个 citation；
- `article_id` 必须来自本轮 Evidence；
- `quote` 必须是证据文本的连续片段，不得改写、不得用省略号；
- 证据不足时返回 `{"claims": []}`；
- `claim_id` 缺失时服务端补 `c1`、`c2`；重复则拒绝该条。

### 5.3 校验

W2 的 `ClaimValidator` 只做确定性校验：

1. `article_id` 在本轮 Evidence 集合中，否则丢弃该 citation；
2. `quote` 非空、至少 6 个字符；
3. `normalize_text(quote)` 必须能在 `article.text_normalized` 中定位；
4. 补全 `Citation` 的 doc_title、article_no、part_no、effective_from、source_url、block_type；
5. `quote_start/end` 记录在被引片段 `text_normalized` 中的位置；
6. 一条 claim 的 citations 全部无效则丢弃该 claim，并在 trace 记录原因。

W2 不检查：

- 数字/日期/比例是否被 quote 覆盖（W3）；
- quote 是否在语义上支持 claim（W6）；
- 答案正文是否有未映射句子（W2 正文完全由 claims 渲染，天然规避）。

### 5.4 Finalize

`Finalize` 只拼接校验通过的内容：

```text
结论要点：
1. <claim 1 text> [文档标题 第 X 条]
2. <claim 2 text> [文档标题 第 X 条]

依据：
- [文档标题 第 X 条](source_url)
  > quote
```

规则：

- 不在 claims 之外添加新的事实句；
- 低置信日期在引用卡片下显示提示；
- `section` 显示“章节/段落”，不写“第 X 条”；
- 固定免责声明：“演示系统不构成官方答复，具体以学校官网为准。”
- 如果没有任何通过校验的 claim，返回原因码，不输出半截答案。

### 5.5 失败关闭

| 失败 | 行为 |
|---|---|
| 无 Evidence | `NO_EVIDENCE` |
| LLM 返回非法 JSON | 一次修复重试；仍失败 → `GENERATION_FAILED` |
| 全部 citation 无效 | `NO_VALID_CLAIM` |
| API 超时/密钥缺失 | `GENERATION_FAILED`，UI 显示可操作提示 |
| Qdrant 不可达 | `INDEX_UNAVAILABLE`，不降级为无检索回答 |

---

## 6. Trace 与最小 UI

### 6.1 Trace

每轮写一个 JSON 对象到 `data/traces/YYYY-MM-DD.jsonl`（gitignored）：

```json
{
  "trace_id": "uuid",
  "session_id": "uuid",
  "started_at": "ISO8601",
  "duration_ms": 0,
  "question_redacted": "……",
  "normalized_query": "……",
  "filters": {},
  "retrieval": [{"article_id": "...", "score": 0.0, "block_type": "article", "rank": 1}],
  "llm": {"model": "...", "latency_ms": 0, "prompt_tokens": 0, "completion_tokens": 0},
  "claims_total": 0,
  "claims_kept": 0,
  "dropped_claims": [{"claim_id": "c2", "reason": "CITATION_NOT_IN_EVIDENCE"}],
  "outcome": "ANSWERED"
}
```

不记录完整证据文本、API key 或未脱敏的个人联系方式。

### 6.2 Gradio

最小单页：

- 问题输入框；
- 受众下拉：未指定 / 本科生 / 研究生 / 教师；
- “提问”按钮；
- 答案 Markdown + 引用卡片（标题、条款号、块类型、来源链接、quote）；
- 调试折叠区：原因码、检索 ID/分数、校验丢弃原因、耗时；
- 单轮提交，不保留多轮历史。

启动命令：`python -m campusguide.ui.app` 或 `make demo`。

---

## 7. 开发集与 W2 评测

### 7.1 20 题开发集 v0

文件：`data/eval/dev_v0.jsonl`；只用于调参，不冻结。

```json
{
  "id": "d001",
  "question": "……",
  "category": "single_article",
  "expected_article_ids": ["..."],
  "expected_outcome": "ANSWERED",
  "block_types": ["article"],
  "notes": "……"
}
```

建议分布：

| 类别 | 数量 |
|---|---:|
| 单条款 | 8 |
| 多条款组合 | 4 |
| section 结构（培养方案/目录类，不含个人页面） | 2 |
| 无证据/超范围 | 3 |
| 历史版本 | 2 |
| 隐私/明显校外 | 1 |

题目和期望 ID 来自 W1 索引；不写完整原文，不包含个人信息。

### 7.2 W2 指标

| 指标 | 定义 | W2 门禁 |
|---|---|---|
| 引用 ID 有效率 | 有效 citation / 总 citation | 100% |
| 编造 ID 数 | 不在证据集的 citation 数 | 0 |
| quote 可定位率 | 能在 text_normalized 定位的 quote / 总 quote | 100% |
| `article_recall@8` | 期望 article 是否在 top 8 | 记录基线，不设硬门禁 |
| `section_recall@8` | 仅 section 子集 | 记录基线 |
| 非可答题 outcome 正确率 | 6 道非可答题的原因码 | 记录基线，W3 设门禁 |
| p50 / p95 延迟 | 端到端 | p50 ≤6s、p95 ≤10s（demo 环境） |

W2 不报告答案正确率；答案质量从 W3/W7 开始评估。

---

## 8. 配置、依赖与运行

### 8.1 依赖

新增可选依赖组 `w2`：

```toml
[dependency-groups]
w2 = [
  "torch>=2.6",
  "sentence-transformers>=3.0",
  "gradio>=5.0",
]
```

- `torch` 优先使用 CUDA wheel；CPU 可回退；设备选择记录在 vector manifest；
- `sentence-transformers` 只用于 bge-m3 dense；W4 的 sparse/rerank 另行设计；
- 核心测试使用 fake embedder/chat，不要求安装 `w2` 组；
- CI 只跑离线测试；live smoke 单独执行。

### 8.2 配置

`.env` 必填：

```dotenv
LLM_BASE_URL=
LLM_API_KEY=
ADVISOR_MODEL=
EMBEDDING_MODEL=BAAI/bge-m3
QDRANT_URL=http://localhost:6333
```

`configs/retrieval.yaml` 增加：

```yaml
query_top_k: 20
max_evidence: 8
max_chars_per_evidence: 800
min_dense_score: 0.35   # 初值，必须用 20 题开发集校准
include_sections_default: false
```

缺失 `LLM_*` 时，UI/CLI 给出明确配置错误，不进入“无引用自由回答”。

### 8.3 运行拓扑

```text
docker compose up -d qdrant     # 仅 Qdrant
python scripts/build_vectors.py # bge-m3 -> Qdrant
python -m campusguide.ui.app    # Gradio 单页
```

- Qdrant 使用本地 Docker 服务；
- app 在宿主机运行，方便加载 GPU；
- 不把 app 放进容器是 W2 的临时决定，W8 再做完整 Compose。

---

## 9. 模块布局

```text
src/campusguide/
  retrieval/
    embedder.py       # EmbeddingProvider / BgeM3Embedder / FakeEmbedder
    indexer.py        # articles.jsonl -> Qdrant points
    filters.py        # PolicyFilters -> Qdrant Filter
    service.py        # DenseRetriever
    normalize.py      # 查询规范化 + 别名扩展
  agents/
    providers.py      # ChatProvider / OpenAICompatibleChat / FakeChatProvider
    advisor.py        # claim JSON 生成与一次修复
  verification/
    claims.py         # Citation/Claim 确定性校验与补全
  qa/
    service.py        # QAService 线性编排
    finalize.py       # claims -> answer_markdown
    guard.py          # 最小前置 guard
  observability/
    trace.py          # JSONL trace
  ui/
    app.py            # Gradio 单页
scripts/
  build_vectors.py    # 向量索引 CLI
  ask.py              # 单题 CLI，便于调试
  eval_w2.py          # 20 题开发集 runner
configs/
  aliases.yaml
compose.yaml          # qdrant service
data/eval/dev_v0.jsonl
```

`agents/graph.py`、`mcp/`、`flows/` 继续留空，W4/W5/W6 再实现。

---

## 10. 测试策略

### 10.1 离线测试（CI）

- `FakeEmbedder`：确定性向量，不加载 torch；
- `FakeChatProvider`：返回预置 claims JSON；
- 内存 Qdrant（`:memory:`）；
- 覆盖：别名、过滤器、点 ID 幂等、dense 检索去重、claim 校验、finalize、guard、trace 脱敏；
- 端到端：问题 → fake retrieval → fake LLM → 校验 → 答案，不访问网络。

### 10.2 Live smoke（手动）

标记 `network` / `live`：

- 加载 bge-m3，把前 20 个块写入临时 Qdrant，验证维度和查询；
- 用真实 API 跑 1 道题，验证 JSON 解析、引用和延迟；
- 不把 live 结果写进 CI 或冻结评测。

### 10.3 开发集运行

`python scripts/eval_w2.py --dev data/eval/dev_v0.jsonl`：

- 输出 `data/eval/w2-dev-report.json` 与 `.md`；
- 包含逐题 outcome、citation ID 校验、quote 定位、Recall@8、延迟；
- 报告明确写“开发集 v0，非冻结测试集”。

---

## 11. 风险与处理

| 风险 | 影响 | 处理 |
|---|---|---|
| Python 3.13 + torch/CUDA 安装失败 | 无法本地 embedding | 实现第一步先做 bge-m3 smoke；失败则停下评估其他 embedding 路线，不静默降级 |
| bge-m3 首次下载大、CPU 慢 | 构建/提问延迟高 | GPU 优先；`max_seq_length=1024`；batch=16；记录设备与耗时 |
| dense-only 召回不稳 | 可答题检索不到 | W2 记录 Recall 基线；W4 加 sparse/rerank；不把 W2 指标当最终 |
| LLM 输出 JSON 不稳定 | 生成失败 | 严格 schema + 一次修复 + 失败关闭 |
| quote 含省略号/改写 | 引用不可定位 | 校验失败丢弃 claim；prompt 明确“逐字复制” |
| 低置信日期 | 历史版本误答 | W2 只查现行；历史问题直接 `HISTORICAL_NOT_SUPPORTED` |
| section 混入条款指标 | 指标虚高 | 默认 article-only；section 走显式参数和单独指标 |
| 教师名录含公开联系方式 | 引用卡片泄露个人联系方式 | W2 默认 article-only；section 调试模式不展示联系方式；W2 不扩大 `redact_contacts:false` 范围 |
| Qdrant/API 不可达 | 无法回答 | 明确错误码；不降级为无引用回答 |
| 本地 trace 含问题 | 隐私 | 问题脱敏后记录；不记录证据全文；数据目录 gitignored |

---

## 12. 实施顺序

1. **运行环境**：安装 `w2` 依赖，验证 bge-m3 加载与 GPU/CPU；起 Qdrant Docker。
2. **索引契约**：补 `date_confidence`，扩展 payload/index，构建向量索引并校验点数。
3. **检索层**：查询规范化/别名、过滤器、dense 检索、Evidence 聚合；fake embedder 测试。
4. **生成与校验**：ChatProvider、claim JSON、ClaimValidator、Finalize、guard；fake provider 测试。
5. **交付面**：QAService、CLI、Gradio、trace、20 题开发集与报告。

每步结束都必须有可运行的测试或可验证的产物；不把整条链塞进一个 prompt。

---

## 13. W2 验收门

- Qdrant 中有 3,952 个块（或按 `--recreate` 重建后的等价点数），维度 1024；
- `date_confidence` 回填 16 个 low，构建时断言；
- 20 题开发集 v0 全部可运行；
- 引用 ID 有效率 100%，编造 ID = 0，quote 可定位率 100%；
- 可答题能给出由 claims 渲染的答案；非可答题返回明确原因码；
- p50 ≤6s、p95 ≤10s（本地 demo 环境，记录实测）；
- 离线测试全部通过；live smoke 手动通过；
- README 增加 W2 运行命令、配置说明和“W2 不声称答案质量”的限制说明。

---

## 14. 与后续版本的接口

- W3：加入 quote 数字/日期覆盖、拒答判定树、引用支持率；
- W4：加入 sparse + rerank、MCP server、完整 audience/time 过滤；
- W5：流程 DSL 与多轮槽位；
- W6：把 `QAService` 的纯函数包进 LangGraph 四角色；
- W7：冻结 50 题测试集、消融与失败归因。

W2 的 `Evidence`、`Citation`、`Claim` 和原因码是后续版本的稳定契约；后续只扩展字段，不改变“引用必须来自本轮证据”的语义。
