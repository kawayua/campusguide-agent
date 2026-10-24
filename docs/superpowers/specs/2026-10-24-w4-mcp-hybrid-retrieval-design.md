# CampusGuide W4 设计：MCP 工具层与混合检索

| 项目 | 内容 |
|---|---|
| 文档版本 | 0.1（设计稿） |
| 日期 | 2026-10-24 |
| 基线 | W3 有条件通过：`f23d417`；W2/W3 代码与 295 项测试 |
| 范围 | FastMCP 两个只读 server、受众/时间过滤、BM25+dense 混合召回、bge-reranker、contract tests |
| 非目标 | W5 流程 DSL、W6 语义核对/重写、W7 冻结测试集 |
| 状态 | 已实现（2026-10-24）；验收与实测见 `docs/weekly/w4.md` |

---

## 0. 决策摘要

W4 做四件事：

1. **工具层协议化**：条款检索和版本/文档元数据分别由 `policy-search`、`policy-meta` 两个只读 FastMCP server 提供；应用通过 stdio client 调用，不再直接访问 Qdrant。
2. **混合召回**：现有 bge-m3 dense + **BM25 风格 sparse**，RRF 融合，bge-reranker-v2-m3 重排 top 8。
3. **受众/时间过滤**：支持角色、学院、专业、入学年份、阶段、培养类型、群体和 `as_of`；完整历史版本解析仍留 W6。
4. **契约测试**：每个工具覆盖正常、空结果、非法参数、不存在对象、索引降级；至少 5 个人群错配 case 全部通过。

W4 不改变 W3 的 `VerificationIssue` / `VerificationResult` / `PARTIAL_ANSWER` 契约。MCP 只提供证据和元数据，核对仍由应用侧 Verifier 完成。

### 0.1 sparse 选择

W4 默认 **BM25 风格 sparse**：

- 不需要额外下载 sparse 模型；当前环境已有 bge-m3 和 torch，但没有 FlagEmbedding；
- 离线契约测试可完全确定性；
- 中文制度文本的专有词（“推免”“学籍异动”“缓考”）适合词项匹配；
- 通过 `SparseEncoder` 接口保留 bge-m3 sparse 替换点，W6/W7 可按质量评估替换，不需要改 MCP schema。

BM25 参数：`k1=1.2`、`b=0.75`，CJK 单字 + 双字 token，英文/数字按连续串；权重在索引构建时按全库 IDF 计算，查询使用同一 vocab/idf 版本。

---

## 1. 当前基线

- W2：dense-only 检索、claim-first、引用 ID/quote 校验；`QAService` 直接持有 `DenseRetriever`。
- W3：原文 span、fact 覆盖、受众/版本 scope、输出隐私、废止探针、PASS/PARTIAL/ABSTAIN；295 项测试。
- W3 结项：W3 baseline 75.0% 未达；W6-A prompt remediation 98.1% 人工支持，W3 有条件通过（`f23d417`）。
- 索引：`articles_v1` 3,952 点、dense 1024；payload 已有 article/doc/version、block_type、audience、colleges、majors、status、effective dates、date_confidence、source_url。

W4 的改动集中在检索和工具层，不重新打开 W3 的核对逻辑。

---

## 2. 总体架构

```text
CLI / Gradio / QAService
        │
        │ FastMCP stdio client
        ├──────────────► policy-search server
        │                  ├─ bge-m3 dense
        │                  ├─ BM25 sparse
        │                  ├─ Qdrant articles_v1
        │                  └─ bge-reranker-v2-m3
        │
        └──────────────► policy-meta server
                           ├─ documents.jsonl / sources config
                           ├─ version resolver
                           └─ organization registry
```

分层：

- `mcp/policy_search_server.py`：只读检索工具；模型、Qdrant、reranker 在 server 进程内。
- `mcp/policy_meta_server.py`：只读文档/版本元数据；不读 Qdrant。
- `mcp/client.py`：FastMCP client、启动/关闭、超时、错误映射。
- `retrieval/`：被 server 调用；W4 不要求 QAService 直接 import Qdrant client。
- `qa/service.py`：依赖 `SearchClient` 协议，不依赖具体 server。

### 2.1 进程与模型

- 默认 stdio；app 启动时拉起两个 server。
- `policy-search` 启动时加载 bge-m3 和 reranker；启动慢，UI 显示“正在加载检索模型”，不把冷启动算入每轮 p95。
- `policy-meta` 轻量，启动只加载 JSON 元数据。
- 测试用 in-process fake client；contract test 用 FastMCP in-memory transport，可选一个 stdio smoke。
- HTTP transport 预留，但 W4 不实现。

---

## 3. MCP 契约

### 3.1 通用响应

成功：

```json
{"ok": true, "schema_version": "1.0", "data": ..., "warnings": []}
```

错误：

```json
{
  "ok": false,
  "schema_version": "1.0",
  "error": {
    "code": "INVALID_FILTER",
    "message": "...",
    "retryable": false,
    "details": {}
  }
}
```

错误码：`NOT_FOUND`、`INVALID_FILTER`、`INVALID_ARGUMENT`、`INDEX_UNAVAILABLE`、`RERANK_UNAVAILABLE`、`NOT_IMPLEMENTED`、`CONFLICT`、`RATE_LIMITED`、`INTERNAL`。`RERANK_UNAVAILABLE` 可重试；`NOT_IMPLEMENTED` 不可重试。

### 3.2 `policy-search`

#### `search_articles`

请求：

```json
{
  "query": "本科生国家奖学金申请条件",
  "filters": {
    "block_types": ["article"],
    "audience": ["undergraduate", "all"],
    "status": ["active"],
    "as_of": "2026-10-24",
    "version_mode": "current",
    "doc_ids": null,
    "flow_ids": null,
    "profile": {
      "colleges": ["agri"],
      "majors": ["agronomy"],
      "intake_year": 2024,
      "stage": "year2",
      "program_type": null,
      "groups": [],
      "status": "enrolled"
    }
  },
  "top_k": 30,
  "rerank_top_n": 8
}
```

响应 `data`：

```json
{
  "query": "本科生国家奖学金申请条件",
  "articles": [
    {
      "article": { "...ArticlePublic..." },
      "dense_score": 0.82,
      "sparse_score": 12.4,
      "rrf_score": 0.031,
      "rerank_score": 0.91,
      "matched_spans": [[12, 38]],
      "rank": 1
    }
  ],
  "index_version": "articles_v1@w4-hybrid-...",
  "degraded": false,
  "warnings": []
}
```

约束：

- `top_k` 1..50，默认 30；`rerank_top_n` 1..20，默认 8；
- `filters.doc_ids`/`flow_ids` 必须是已注册值；
- 返回 `ArticlePublic`，不含 `text_raw_ref`；
- 每条 evidence 保留 `article_id`、`part_no`、`block_type`、scores；
- `degraded=true` 只在 reranker 不可用且仍返回 dense+sparse 结果时出现；W4 默认失败关闭，不允许无 rerank 的“静默降级”；
- 空结果返回 `ok=true, articles=[]`，不是错误。

#### `get_article`

请求 `article_id`（含 part 可选）；返回单条 ArticlePublic 及其 metadata；不存在 → `NOT_FOUND`。只允许只读查询。

### 3.3 `policy-meta`

W4 只实现有数据的工具：

- `get_document_versions(doc_id)`：返回版本状态、生效区间、来源 URL、date_confidence；
- `get_document(doc_id)`：文档级元数据（不发全文）；
- `resolve_version(doc_id, as_of, mode)`：复用 W1 `resolve_version`，返回 selected/reason code；
- `list_documents(filter?)`：仅 id/title/audience/issuer，不返回条款正文。

W4 **不实现**：

- `get_flow` / `list_flows`：W5 才有可用的 YAML；
- `get_calendar`：当前没有校历数据，W5/W6 再接；
- 任何写入工具。

W4 不注册 flow/calendar 工具；客户端在调用前检查 tool list，缺失工具返回
`NOT_IMPLEMENTED` 并明确说明“W5 提供”，不返回空数据伪装成功。server 也不
注册只返回空数组的伪工具。

### 3.4 参数校验

- 所有请求用 Pydantic 模型校验；
- 非法 audience/status/version_mode/block_type → `INVALID_FILTER`；
- 越界 top_k/rerank_top_n → `INVALID_ARGUMENT`；
- 未知 doc_id/flow_id → `INVALID_FILTER` 或 `NOT_FOUND`，按工具语义；
- 不把 Pydantic traceback 透传给客户端。

---

## 4. 受众/时间过滤

### 4.1 UserProfile

```python
class UserProfile(BaseModel):
    colleges: list[str] = []
    majors: list[str] = []
    intake_year: int | None = None
    stage: AudienceStage | None = None
    program_type: ProgramType | None = None
    groups: list[StudentGroup] = []
    status: StudentStatus | None = None
```

- `audience` 仍在 filters 顶层；profile 只放细目；
- 所有字段空 = 未提供，不做硬过滤；
- profile 字段必须是 organization registry 中已登记的 college_id/major_id；
- UI 可选：受众、学院、专业、入学年份、阶段；不要求 W4 做完整账号。

### 4.2 角色过滤

- 已知角色：`audience` 匹配 `[selected, all]`；
- 未指定角色：不过滤，返回 `AUDIENCE_UNDETERMINED` warning；
- `Audience.ALL` 视为未指定，不做硬过滤；
- 与 W3 `check_scope` 的语义一致，MCP 只负责在检索时提前缩小结果。

### 4.3 细目过滤

payload 增加：

```text
audience_colleges, audience_majors,
audience_intake_years, audience_intake_from,
audience_graduating_years, audience_stages,
audience_program_types, audience_groups,
audience_status, audience_cycle
```

规则：

- 文档细目为空 = 不限制；
- 用户 profile 字段为空 = 不因该项过滤（保留并 warning）；
- 文档细目非空、用户值已知：
  - colleges/majors：交集为空 → 排除；
  - intake_year：`intake_from <= user_year` 或 `intake_years` 包含；
  - graduating_years：不匹配 → 排除；
  - stage/program_type/group/status：不匹配 → 排除；
  - cycle：按学年匹配；
- 用户未知而文档有细目：**保留**并返回 `PROFILE_UNDETERMINED` warning，不暗中当作不匹配；
- 任何硬过滤必须可解释：响应 warnings 记录 applied filters 和原因。

### 4.4 时间

- `as_of` 默认今天；`effective_from <= as_of`、`effective_to null/>= as_of`；
- `CURRENT`：status=active + 区间；
- `HISTORICAL`：按 as_of 取当时有效版本，忽略当前 status，只排除 draft；W4 MCP 工具支持，但 QAService 仍通过 W3 guard 拒绝历史问答，等 W6 接历史回答；
- `INCLUDE_REPEALED`：允许 repealed，返回 status/生效日期，回答必须标注；
- 版本冲突：同一 doc 多个候选active且生效日相同 → `CONFLICT`；不同届别版本 → warning；
- `NO_EFFECTIVE_VERSION` / `DATE_UNKNOWN` 的完整 QAService 分支留 W6。

### 4.5 人群错配验收

至少 5 个 case：

1. 本科生问研究生专属条款；
2. 研究生问本科课程条款；
3. 农学院学生问信息学院专属培养方案；
4. 非农学专业问农学专业专属方案；
5. 2024 级问仅适用于 2022 级的计划。

每个 case 要求：MCP 过滤后不返回不匹配的 article，或明确返回空/warning；不得让 Verifier 事后才发现。目标 5/5（≥90%）。

---

## 5. 混合召回与重排

### 5.1 索引

复用 `articles_v1`，重建为 dense+sparse：

- dense：bge-m3 1024，沿用 W2/W3；
- sparse：BM25 风格；vocab/idf 由 `data/processed/articles.jsonl` 构建；
- Qdrant `SparseVector(indices, values)`；token 通过稳定 hash 映射到 uint32；记录 `sparse_vocab_version`/`sparse_idf_hash`；
- payload 增加 W4 audience 细目字段；payload indexes 增加这些字段；
- `vector_manifest` 记录 `embedding_mode=hybrid`、dense model、sparse params、点数、source hash；
- 重建必须 `--recreate`，避免旧 dense-only 点与新 sparse 点混合；
- W3 dense-only 代码仍可读取同一集合的 dense 向量（向后兼容）。

### 5.2 SparseEncoder

```python
class SparseEncoder(Protocol):
    def encode_documents(self, texts: list[str]) -> list[SparseVector]: ...
    def encode_query(self, text: str) -> SparseVector: ...
    @property
    def version(self) -> str: ...
```

- 中文：单字 + 双字 token；英文/数字：连续串小写；停用词不删（BM25 IDF 处理）；
- 构建时收集 df，计算 idf；查询使用同一 idf；
- 空 query/空文档返回空 sparse vector，不崩；
- 不依赖网络；vocab 只从 corpus 构建；
- 未来 bge-m3 sparse 实现同一接口，重建索引即可；MCP schema 不变。

### 5.3 检索流程

```text
query variants
  ├─ dense top 30
  └─ sparse top 30
        ↓
RRF k=60，按 (article_id, part_no) 去重
        ↓
article_id 聚合，保留最高 RRF 片段
        ↓
bge-reranker top 8
        ↓
min_rerank_score + margin + token budget
        ↓
Evidence[]
```

- 多 query 变体：同一套别名扩展，最多 2 个；
- RRF 使用排名而非原始分数，避免 dense/sparse 尺度不同；
- 同一 article 多 part 只保留最高 rank；引用时保留 part_no；
- reranker 输入：query + doc_title + chapter_path + article_no + text；
- reranker logits → sigmoid 归一化；阈值在 dev_v0 校准；
- `min_rerank_score` 初值 0.30，`min_score_margin` 0.03；不直接写死；
- reranker 不可用：返回 `RERANK_UNAVAILABLE`，不静默降级；
- 延迟预算：dense+sparse+rerank 全部在 server 内；warmup 单列。

### 5.4 评估基线

同一 20 题 dev_v0，比较：

- A：dense-only（W3 基线）；
- B：dense+sparse RRF；
- C：B + reranker；
- 指标：Recall@8、MRR、article/section 分开；W3/W6-A 结构门禁回归；
- 不把 dev_v0 当冻结测试集；W7 再报区间。

---

## 6. FastMCP 实现

### 6.1 依赖

- `fastmcp`（或官方 `mcp`，以 FastMCP 为主）；
- `rank_bm25` 不强制；BM25 自研 tokenizer/df，避免额外依赖；
- `sentence-transformers` 已有，用于 bge-m3 和 reranker；
- Python 3.13 兼容性在 W4 第一步验证；失败则用独立 Python 3.11 venv 跑 server，app 仍用 3.13。

### 6.2 server 结构

```text
src/campusguide/mcp/
  policy_search_server.py   # FastMCP("policy-search")
  policy_meta_server.py     # FastMCP("policy-meta")
  client.py                 # FastMCP Client / stdio lifecycle
  schemas.py                # request/response Pydantic
```

- server 工具函数只做参数校验 + 调用 retrieval/meta service；
- server 不保存会话状态；
- 日志写 stderr，不污染 stdio；
- 模型加载在 server startup；失败即 server 启动失败，不返回半成品。
- 每个 tool 注册明确的 input/output schema。

### 6.3 client

```python
class SearchClient(Protocol):
    def search_articles(...) -> SearchResponse: ...
    def get_article(...) -> ArticlePublic: ...
```

- `MCPPolicySearchClient` 启动/关闭 server；超时 30s；重试 1 次（仅可重试错误）；
- `MCPPolicyMetaClient` 类似；
- 连接失败 → `TOOL_UNAVAILABLE`；QAService 映射为明确拒答；
- 测试使用 `FakeSearchClient`；contract test 使用 FastMCP in-memory/stdio；
- app 不 import QdrantClient / embedder / reranker。

### 6.4 启动与关闭

- `make mcp-smoke` 启动 servers，调用 `search_articles`、`get_article`、`get_document_versions`；
- UI/CLI 启动时创建 client，并在后台 warmup；关闭时终止子进程；
- 不允许 server 自动写本地文件（trace 由 app 写）。

---

## 7. QAService 集成

- W3 的 `QAService` 从 `DenseRetriever` 改为 `SearchClient`；
- `ask` 接受 `profile: UserProfile | None`；
- MCP 返回 Evidence，交给 W3 `DeterministicVerifier`；核对逻辑不变；
- MCP warnings 进入 trace，不直接当作 answer 事实；
- `search_articles` 返回 `index_version`；QAService 校验与本地 vector manifest 一致，不一致 → `INDEX_UNAVAILABLE`；
- MCP 超时/不可用 → `TOOL_UNAVAILABLE`（新增 OutcomeCode）或 `INDEX_UNAVAILABLE`；
- 任何 MCP 空结果 → `NO_EVIDENCE`，不进 Advisor；
- W3/W6-A 的 support 回归：检索变化后必须重新跑 dev_v0；若 claim 文本/quote 变化，旧 labels 失效，需重新人工标注。

---

## 8. 配置与可观测性

`configs/retrieval.yaml` 增加：

```yaml
collection: articles_v1
alias: articles_current
embedding_model: BAAI/bge-m3
dense_vector_name: dense
dense_vector_size: 1024
sparse_vector_name: sparse
sparse_mode: bm25
bm25_k1: 1.2
bm25_b: 0.75
rrf_k: 60
recall_top_k: 30
rerank_top_n: 8
reranker_model: BAAI/bge-reranker-v2-m3
min_rerank_score: 0.30
min_score_margin: 0.03
token_budget: 4000
max_articles: 8
```

`configs/mcp.yaml`：

```yaml
search_server:
  command: [python, -m, campusguide.mcp.policy_search_server]
  timeout_seconds: 30
  retries: 1
meta_server:
  command: [python, -m, campusguide.mcp.policy_meta_server]
  timeout_seconds: 10
```

Trace 增加：

- `mcp`: server names、tool、latency、retry、error_code；
- `retrieval`: dense/sparse ranks/scores、rrf、rerank、filters applied；
- `index_version`；
- 不记录原文全文、个人联系方式或 API key。

---

## 9. 测试与验收

### 9.1 契约测试（CI，无网络）

每个工具至少五类：

1. 正常响应；
2. 空结果（合法但无命中）；
3. 非法参数（top_k=0、未知 audience）；
4. 不存在对象（get_article/get_document）；
5. 索引/服务降级（Qdrant 不可达、reranker 不可用、meta 文件缺失）。

校验：

- Pydantic schema；
- `ok`/`error.code`/`schema_version`；
- 不含 `text_raw_ref`；
- 只读；
- 超时/重试映射。

### 9.2 过滤测试

- 5 个人群错配 case：目标 5/5；
- 正常人群 case、未知 profile、空约束、届别版本、repealed；
- 测试只通过 MCP client 调用，不直接查 Qdrant。

### 9.3 检索回归

- dev_v0 20 题：dense-only vs hybrid RRF vs hybrid+rerank；
- Recall@8/MRR，article/section 分开；
- W3 结构门禁 100%、拒答召回 ≥80、误拒 ≤20% 不回归；
- W6-A prompt 下 support 若 claim/quote 改变，必须重新人工标注；旧标签不可直接复用；
- p95 记录，不设新硬门禁；reranker 延迟单列。

### 9.4 W4 验收门

- QAService 不直接 import Qdrant/embedding/reranker；
- 所有检索经 `policy-search` MCP；版本元数据经 `policy-meta`；
- contract tests 通过；
- 5/5 人群错配 case 通过；
- hybrid/rerank 报告完成，W3 结构/拒答不回归；
- 文档说明 sparse 选择、reranker 模型、阈值与限制；
- 不宣称 W4 提升答案质量，除非有 freeze 的评测数据。

---

## 10. 风险与处理

| 风险 | 影响 | 处理 |
|---|---|---|
| BM25 稀疏质量不足 | 专有词召回不如 bge-m3 sparse | 先上线 BM25 可测版本；用 A/B 与 W6/W7 评估是否替换 SparseEncoder |
| FastMCP/Python 3.13 兼容 | server 启动失败 | 第一步做 import/stdio smoke；必要时独立 3.11 环境 |
| stdio 日志污染 | JSON 协议解析失败 | 所有日志写 stderr；禁止 print 到 stdout |
| 模型加载慢/占显存 | server 启动慢，GPU 竞争 | server 启动 warmup；bge-m3 + reranker 分阶段加载；记录内存/设备 |
| reranker 延迟 | p95 变差 | 限制 top30、batch、记录 rerank 延迟；W6/W7 再调 |
| 过滤语义过严 | 可答题误拒 | 未知 profile 不硬过滤；只排除明确 mismatch；5 case + dev 回归 |
| 索引重建不一致 | 老 dense 点/新 sparse 点混合 | `--recreate`；manifest source hash/稀疏 vocab hash 校验 |
| MCP 错误被吞 | 无引用自由回答 | client 抛错映射为 `TOOL_UNAVAILABLE`/`INDEX_UNAVAILABLE`，失败关闭 |
| W3 support 回退 | W4 引用质量下降 | 重新跑 W6-A；claim/quote 变化则重新标注，不复用旧 labels |

---

## 11. 实施顺序

1. **依赖与契约**：FastMCP import/stdio smoke；W4 schemas；UserProfile/filters。
2. **索引**：payload 细目字段；BM25 vocab/idf；dense+sparse 重建；manifest。
3. **混合检索**：dense/sparse/RRF、聚合、reranker、阈值；离线测试。
4. **MCP servers**：policy-search/policy-meta tools + 错误契约。
5. **MCP client + QAService**：SearchClient 协议、启动/关闭、trace、失败关闭。
6. **过滤/人群 cases**：profile 字段、5 case、dev 回归。
7. **评测/文档**：dense-only vs hybrid vs rerank 报告；README/W4 周记录。

每步先测试后实现；不要在 MCP server 内复制 W3 核对逻辑。

---

## 12. 与后续版本接口

- W5：在 policy-meta 增加 flow/calendar 工具；MCP schemas 不变，新增 tool；流程事实仍由 Verifier H9 校验。
- W6：可能替换 SparseEncoder 为 bge-m3 sparse；历史版本解析；语义重写；MCP 契约不变。
- W7：冻结 50 题、消融 A5（dense-only vs hybrid+rerank）；报告 MCP 延迟/错误率。

W4 稳定契约：

- MCP `search_articles` / `get_article` / `get_document_versions` / `resolve_version` 的 schema；
- Evidence 的 dense/sparse/rerank 分数；
- `article_id + part_no` 片段身份；
- 过滤 warnings 与错误码；
- W3 `VerificationIssue` / `VerificationResult` 不变。
