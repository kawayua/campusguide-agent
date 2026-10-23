# CampusGuide W3 设计：引用核对与拒答 v0

| 项目 | 内容 |
|---|---|
| 文档版本 | 0.1（实现前设计稿） |
| 日期 | 2026-10-23 |
| 来源 | `2026-10-19-campusguide-design.md`、W2 周记录、W2 设计 |
| 范围 | 引文 span 定位、数字/日期覆盖、确定性硬校验、拒答判定树 v0、20 题开发集复测 |
| 基线 | W2：claim-first + 引用 ID/quote 校验 + 20 题 dev_v0；`ac830a4`（W2 当前收口） |
| 状态 | 设计已确认，待实现 |

---

## 0. 决策摘要

W3 不做语义核对，也不做重写循环。它是 **确定性 Verifier + 拒答 v0**：

1. **硬校验失败不静默丢弃**：失败 claim 不进入答案，返回 `PARTIAL_ANSWER` 并列出省略原因；全部失败则 `UNVERIFIABLE`。
2. **引文 span 迁移到原文偏移**：`locate_quote` 将归一化匹配映射回 `article.text`，`Citation.quote_start/end` 表示原文位置，新增 `quote_match`。
3. **fact coverage**：claim 中的数字、日期、比例、期限必须出现在本轮引用 quote 并集中；不支持则丢弃该 claim。
4. **拒答树 v0**：在现有 guard、检索和 evidence 元数据上实现 `ONLY_REPEALED`、`VERSION_CONFLICT`、`VERSION_MISMATCH`、`UNVERIFIABLE`、`PARTIAL_ANSWER`；完整历史版本分支留 W4。
5. **20 题开发集不变**：新增一次冻结运行 + 人工 support 标注；W3 报告同时给出 draft/rendered 支持率、拒答混淆矩阵和误拒率。
6. **不做**：语义蕴含判断、重写、重试循环、多轮追问、MCP、流程事实核对（H9）、检索增强、50 题冻结测试集。

W3 的核心不变量：

> 任何进入最终答案的事实性 claim 都必须通过引用 ID、原文 span、数字/日期覆盖和适用范围检查；失败结论要么被省略并显式提示，要么整题拒答。

---

## 1. W2 基线与 W3 目标

### 1.1 W2 已有

- 单轮 `QAService`：guard → dense 检索 → Evidence → Advisor claims → `ClaimValidator` → Finalize。
- `ClaimValidator` 已检查：citation 的 `article_id` 来自本轮 Evidence、quote ≥6 字、quote 在 `text_normalized` 中可定位、补全引用元数据。
- `Citation` 已带 `part_no`、`block_type`、`date_confidence`。
- 20 题 dev_v0：14 可答、6 非可答；W2 报告引用 ID 100%、quote 可定位 100%、复制 ID 0。
- `Qdrant`、`bge-m3`、Gradio/CLI、trace 和 live runner 可用。
- W2 的 p95 已知不达标（warm 14.7s），周记录已明确接受；W3 不重新开启端点 A/B。

### 1.2 W3 目标

- 引文定位从“归一化字符串可找到”升级为“可映射回原文 span”。
- 增加 claim 级数字/日期/比例/期限覆盖检查。
- 增加 claim 级受众、版本状态、输出隐私的确定性检查。
- 把散落的失败处理收束为可审计的拒答判定树 v0。
- 支持部分作答，但不允许任何未通过核对的 claim 进入答案。
- 在 20 题 dev_v0 上测量：引用支持率、拒答召回、误拒率、partial 率、issue 分布。

### 1.3 非目标

- 语义蕴含判断（W6）。
- 两次 revise 或任何 LLM 重写（W6）。
- 多轮追问、槽位（W5）。
- MCP（W4）。
- 流程步骤/材料核对（H9，W5）。
- 完整历史版本解析、`DATE_UNKNOWN`/`NO_EFFECTIVE_VERSION` 全分支（W4）。
- 检索/rerank/受众细目过滤改进（W4）。
- 冻结 50 题测试集（W7）。
- 答案正确率（W3 只评价引用支持与拒答行为）。

---

## 2. 总体架构

```text
问题
  → guard（W2）
  → dense 检索（W2）
  → evidence 范围检查（W3：audience/version）
  → Advisor claims（W2）
  → DeterministicVerifier（W3）
       1. citation ID / quote span
       2. fact coverage（数字/日期/比例/期限）
       3. audience / version / privacy
       4. claim 级 issue 汇总
  → AbstentionPolicy v0（W3）
       PASS / PARTIAL_ANSWER / ABSTAIN
  → Finalize：只渲染通过 claim
  → trace：issues、partial、reason code
```

W3 仍然不引入 LangGraph；Verifier 是纯函数/服务对象，W6 可以直接包进图节点。

### 2.1 模块边界

```text
src/campusguide/verification/
  quotes.py       # 原文 span 定位与归一化映射
  facts.py        # fact 抽取、规范化、覆盖检查
  issues.py       # VerificationIssue / VerificationResult
  verifier.py     # 确定性 Verifier 编排
src/campusguide/qa/
  service.py      # 接入 Verifier 与 PARTIAL/ABSTAIN
  finalize.py     # partial 警告 + 固定模板
  decision.py     # 拒答判定树 v0（也可放 verification/）
scripts/
  eval_w3.py      # 20 题 + support 标注 + 拒答混淆矩阵
tests/unit/
  test_w3_quotes.py
  test_w3_facts.py
  test_w3_verifier.py
  test_w3_abstention.py
```

现有 `verification/claims.py` 保留为 W2 的 ID/quote 基础校验；W3 的 `verifier.py` 调用它并追加 fact/scope/privacy 检查，避免复制逻辑。W3 需要把 `ValidationResult` 中现有的 `dropped`/`dropped_citations` 扩展为带 claim_id、citation、reason 的 issue 记录，否则 `PARTIAL_ANSWER` 无法解释“哪条 claim 为什么被省略”。

---

## 3. 核对契约

### 3.1 Issue 与结果

```python
class IssueSeverity(StrEnum):
    BLOCKING = "blocking"   # claim 不能进入答案
    WARNING = "warning"     # 保留 claim，但提示

class VerificationIssue(BaseModel):
    code: str
    severity: IssueSeverity
    claim_id: str | None = None
    article_id: str | None = None
    part_no: int | None = None
    detail: str = ""
    facts: list[str] = Field(default_factory=list)

class VerificationDecision(StrEnum):
    PASS = "pass"
    PARTIAL = "partial"
    ABSTAIN = "abstain"
    # 已有 REVISE / CLARIFY 保留给 W6 重写和 W5 追问，W3 不产生这两个值

class VerificationResult(BaseModel):
    decision: VerificationDecision
    claims: list[Claim]                 # 只含可渲染 claim
    issues: list[VerificationIssue]
    reason_code: OutcomeCode | None = None
    dropped_claim_ids: list[str] = Field(default_factory=list)
```

规则：

- `PASS`：没有 blocking issue，全部 claim 可渲染。
- `PARTIAL`：至少一条 claim 可渲染，且至少一条被 blocking issue 丢弃。
- `ABSTAIN`：没有可渲染 claim；`reason_code` 必填。
- `warning` 不影响渲染；例如未指定受众时的 `AUDIENCE_UNDETERMINED`。

### 3.2 原因码扩展

在现有 `OutcomeCode` 上增加：

```python
PARTIAL_ANSWER = "PARTIAL_ANSWER"
ONLY_REPEALED = "ONLY_REPEALED"
VERSION_CONFLICT = "VERSION_CONFLICT"
VERSION_MISMATCH = "VERSION_MISMATCH"
UNVERIFIABLE = "UNVERIFIABLE"
```

保留：

- `NO_VALID_CLAIM`：模型返回零条 claim（没有可核对结论）。
- `NO_EVIDENCE`：检索无证据。
- `PRIVACY_REQUEST` / `OUT_OF_SCOPE` / `HISTORICAL_NOT_SUPPORTED` / `INVALID_REQUEST`：输入 guard。
- `GENERATION_FAILED` / `INDEX_UNAVAILABLE`：基础设施失败。

W3 不新增 `AUDIENCE_UNKNOWN`、`NO_EFFECTIVE_VERSION`、`DATE_UNKNOWN` 的完整分支；这些在 W4 接入版本解析和身份追问时补齐。W3 的受众/版本 issue 在 issue 层记录；`AUDIENCE_MISMATCH` 和 `PRIVACY_VIOLATION` 在全部 claim 被丢弃时统一映射为 `UNVERIFIABLE`，在部分 claim 存活时返回 `PARTIAL_ANSWER`，W4 再补专用原因码。

---

## 4. 引文定位

### 4.1 目标

W2 的 `quote_start/end` 是 `text_normalized` 偏移；W3 要回答“quote 在原文的哪个位置”，支持 UI 高亮和 QA 复核。

### 4.2 映射算法

在 `domain/text.py` 增加：

```python
def normalize_with_map(text: str) -> tuple[str, list[int]]:
    """返回归一化文本和 normalized index -> original index 的映射。"""
```

规则：

1. 使用与 `normalize_text` 一致的 NFKC、空白/引号/括号归一化；
2. 保留原始字符索引映射；被删除的空白映射到相邻原文字符；
3. `locate_quote(text, quote)` 先尝试精确子串；失败后对 `normalize_text(text)` 与
   `normalize_text(quote)` 查找，再映射回原 span；
4. 返回 `(start, end, match_type)`，`match_type ∈ {exact, normalized}`；
5. 找不到返回 `None`，对应 `QUOTE_MISMATCH`。

### 4.3 Citation 契约变更

```python
class Citation(BaseModel):
    ...
    quote: str
    quote_start: int          # article.text 的原始字符偏移
    quote_end: int
    quote_match: Literal["exact", "normalized"] = "normalized"
```

- 旧 W2 的 `text_normalized` 偏移迁移为原文偏移；
- 多 part 条款的偏移相对被引 `part` 的 `article.text`；
- `finalize` 和 UI 直接用 `quote` 渲染，不依赖偏移；
- W2 测试中关于偏移的断言同步更新。

### 4.4 失败处理

| 情况 | Issue |
|---|---|
| quote 空或过短 | `QUOTE_TOO_SHORT` |
| quote 含省略号/拼接 | `QUOTE_ELLIPSIS` |
| quote 不在 cited part 中 | `QUOTE_MISMATCH` |
| 同一 claim 的 citation 重复 | 去重，不产生 issue |

---

## 5. 数字、日期与期限覆盖

### 5.1 Fact 模型

```python
class FactKind(StrEnum):
    NUMBER = "number"
    PERCENT = "percent"
    DATE = "date"
    DURATION = "duration"
    AMOUNT = "amount"

class Fact(BaseModel):
    kind: FactKind
    raw: str
    canonical: str
```

### 5.2 抽取范围

只抽取有业务含义的事实，避免把“第九条”“2025 年版”当事实：

| 类型 | 示例 | canonical |
|---|---|---|
| 百分比 | `10%`、`百分之十`、`前30%` | `10%`、`30%` |
| 日期 | `2025年9月1日`、`2025-09-01` | `2025-09-01` |
| 年份 | `2025年`（不跟“版/版本”） | `2025` |
| 期限 | `连续两年`、`10个工作日`、`3个月` | `2年`、`10工作日`、`3月` |
| 金额/学分/学时 | `8000元`、`10学分`、`48学时` | `8000元`、`10学分`、`48学时` |
| 区间 | `10—28学分`、`10至28学分` | `10学分`、`28学分` |

明确忽略：

- `第X条`、`第X章`、`第X部分`；
- `（一）`列表序号；
- `2025年版`、`v2025` 等版本标签；
- 没有单位/上下文的裸数字（避免把页码、条款号当事实）。

### 5.3 覆盖规则

对每条 claim：

1. 从 `claim.text` 抽取 facts；
2. 收集该 claim 的所有 citation quote；
3. 对每个 fact，检查 `canonical` 是否出现在任一 quote 的归一化文本中；
4. 未覆盖 → `NUMBER_NOT_SUPPORTED`（统一 H4 code），`facts` 字段列出未覆盖项；
5. 任一 fact 未覆盖 → claim 为 blocking，不进入最终答案。

fact 可以跨多个 citation quote 覆盖；跨 claim 不共享。

### 5.4 保守性

数字/日期抽取是启发式，W3 采用 **宁可漏检，不可误杀**：

- 只抽取带单位、百分号、日期结构或明确期限的数字；
- 无法规范化的中文数字不强行猜测；
- 开发集上把误拒原因分为“抽取误报”和“模型真的没写依据”，并在报告披露；
- 抽取规则全部参数化、可单测，不把阈值写进 prompt。

---

## 6. 适用范围与版本检查

### 6.1 H5 受众

输入 `audience` 可选：

- 指定受众时：Evidence 的 `audience` 必须包含该受众或 `all`，否则 `AUDIENCE_MISMATCH`（blocking）；
- 未指定受众时：不做硬失败，添加 `AUDIENCE_UNDETERMINED` warning，引用卡片显示原文 audience；
- W3 不做本科/研究生/年级的自动追问，W4 接用户画像后再补 `AUDIENCE_UNKNOWN`/clarify。

### 6.2 H6 版本

W3 只检查本轮 Evidence：

1. `status == active`；
2. `effective_from <= as_of`；
3. `effective_to is null or effective_to >= as_of`；
4. 同一 `doc_id` 出现多个重叠的 active version → `VERSION_CONFLICT`；
5. 全部 Evidence 都是废止版本 → `ONLY_REPEALED`（需要检索探针，见 6.3）；
6. 其他区间/状态不符 → `VERSION_MISMATCH`。

低置信日期：

- `date_confidence=low` 不是 blocking；在 citation 卡片继续显示锚点提示；
- 历史问题仍由 W2 guard 直接 `HISTORICAL_NOT_SUPPORTED`；
- 完整 `DATE_UNKNOWN`/`NO_EFFECTIVE_VERSION` 分支留 W4。

### 6.3 只命中废止版本的探针

当主检索返回空 Evidence、且问题不是历史/隐私/校外时：

1. 用 `status=[repealed]` 做一次无 LLM 的 Qdrant 检索探针；
2. 若命中 → `ONLY_REPEALED`，列出废止版本和现行入口；
3. 若仍无命中 → `NO_EVIDENCE`；
4. 探针不计入正常 p50/p95，单独记录 probe 时间。

探针只用于拒答解释，不把废止条款交给 Advisor。

### 6.4 H10 输出隐私

对 claim 文本和 citation quote 做输出扫描：

- 手机号、身份证、学号、非 `@scau.edu.cn` 个人邮箱：`PRIVACY_VIOLATION`（blocking）；
- 部门公开座机、`@scau.edu.cn` 官方邮箱：允许；
- 默认 article-only 已排除大部分教师名录，但输出层仍保留最后一道扫描；
- 命中 claim 丢弃；全部丢弃 → `UNVERIFIABLE`。

---

## 7. 硬校验矩阵

| 编号 | W3 检查 | Issue code | 处理 |
|---|---|---|---|
| H1 | citation ID 在本轮 Evidence | `CITATION_NOT_IN_EVIDENCE` | 丢弃 citation；无 citation 则丢弃 claim |
| H2 | citation ID 在知识库 | Evidence 从索引 payload 还原，H1 已覆盖；不单独查库 | — |
| H3 | quote 能映射到 cited part 原文 | `QUOTE_MISMATCH` / `QUOTE_TOO_SHORT` / `QUOTE_ELLIPSIS` | 丢弃 citation |
| H4 | claim 的 fact 被 quote 覆盖 | `NUMBER_NOT_SUPPORTED` | 丢弃 claim |
| H5 | 受众兼容或写明条件 | `AUDIENCE_MISMATCH`（全部失败映射 `UNVERIFIABLE`） / warning | 丢弃 claim / 提示 |
| H6 | 状态/生效区间正确 | `VERSION_MISMATCH` / `ONLY_REPEALED` / `VERSION_CONFLICT` | 丢弃或整题拒答 |
| H7 | 每个事实性 claim 至少一个 citation | `MISSING_CITATION` | 丢弃 claim |
| H8 | 答案没有未映射事实句 | 由 Finalize 只渲染 claims；结构性测试保证 | — |
| H9 | 流程事实与 YAML 一致 | 延后 W5 | — |
| H10 | 输出无隐私数据 | `PRIVACY_VIOLATION` | 丢弃 claim |

W3 的 H1/H3/H4/H5/H6/H7/H10 全部是确定性规则；不存在“LLM 判断通过”。

---

## 8. 拒答判定树 v0

### 8.1 顺序

```text
1. 输入 guard
   ├─ 空/超长 → INVALID_REQUEST
   ├─ 隐私请求 → PRIVACY_REQUEST
   ├─ 其他学校/非本校 → OUT_OF_SCOPE
   └─ 历史/废止 → HISTORICAL_NOT_SUPPORTED
2. 主检索无 Evidence
   ├─ 废止探针命中 → ONLY_REPEALED
   └─ 否则 → NO_EVIDENCE
3. Evidence 版本
   ├─ 多个现行版本重叠 → VERSION_CONFLICT
   ├─ 状态/区间不符 → VERSION_MISMATCH
   └─ 受众不符 → AUDIENCE_MISMATCH（全部证据不符时拒答）
4. Advisor 失败 → GENERATION_FAILED
5. 模型零 claims → NO_VALID_CLAIM
6. 硬校验
   ├─ 全部 claim 失败 → UNVERIFIABLE
   ├─ 部分失败 → PARTIAL_ANSWER
   └─ 全部通过 → ANSWERED
```

### 8.2 PARTIAL_ANSWER

- 只渲染通过 claim；
- 顶部/底部固定提示：“部分结论未通过引用核对，已省略；以下仅为可核实内容。”
- `QAResult` 增加 `partial: bool = True`、`issues`；
- UI 显示省略数量和 blocking issue 类型；
- trace 记录每条被丢弃 claim 的 issue code；
- 报告中 partial 单独计数，不混入“完整回答”。

### 8.3 全部失败

- 输出 `UNVERIFIABLE`，不输出任何 claim；
- 给原文入口（若能确定最相关条款）和人工建议；
- 不把模型原始文本透传；
- W3 不做自动重写；W6 再补重写循环。

### 8.4 不做的分支

- `AUDIENCE_UNKNOWN` / 追问：W4/W5；
- `NO_EFFECTIVE_VERSION` / `DATE_UNKNOWN`：W4 版本解析；
- `TOOL_UNAVAILABLE`：W3 继续用 `INDEX_UNAVAILABLE` / `GENERATION_FAILED`；
- 流程事实：W5。

---

## 9. QAService 集成

### 9.1 调用顺序

```python
guard = check_guard(question)
retrieval = retriever.retrieve(question, filters)
scope = verifier.check_scope(retrieval.evidence, audience, as_of)
if scope.abstain: return abstain
advisor = advisor.generate(question, evidence, audience)
verification = verifier.verify(drafts, evidence, audience, as_of)
if verification.decision is ABSTAIN: return abstain
answer = finalize(verification.claims, partial=verification.decision is PARTIAL)
return result
```

### 9.2 QAResult

新增：

```python
partial: bool = False
issues: list[VerificationIssue] = []
verification: VerificationResult | None = None
```

`answered` 语义：

- `ANSWERED` 或 `PARTIAL_ANSWER` 为“有答案”；
- `PARTIAL_ANSWER` 不能计入“完整回答”指标；
- UI 用 `has_answer` 判断是否渲染答案，用 `partial` 判断是否显示省略提示。

### 9.3 Trace

新增字段：

```json
{
  "verification": {
    "decision": "partial",
    "issues": [{"code": "NUMBER_NOT_SUPPORTED", "claim_id": "c2", "facts": ["10%"]}],
    "dropped_claim_ids": ["c2"],
    "partial": true
  },
  "repealed_probe": {"hit": false, "latency_ms": 0}
}
```

不记录完整证据文本或未脱敏联系方式。

### 9.4 Finalize

- `finalize(claims, partial=False)` 增加 partial 提示；
- 仍然只拼接 claims 和固定模板；
- 不生成新的解释句；
- 引用卡片继续展示 `block_type`、`date_confidence` 和原文链接。

---

## 10. 开发集与 W3 评测

### 10.1 保持 W2 dev_v0

W3 继续使用 `data/eval/dev_v0.jsonl` 的 20 题，不修改题目和期望结果，保证与 W2 可比：

- 14 道可答题；
- 6 道非可答题（no_evidence/out_of_scope/historical/privacy）；
- section 题仍只作结构子集。

### 10.2 冻结一次运行

W3 评测前保存一份运行快照：

```text
data/eval/w3_run.json
```

包含：

- git commit、模型名、索引 manifest hash、配置 hash；
- 每题 question_id、trace_id、outcome、claims、citations、issues、duration；
- 不含完整原文。

support 标注不再依赖会变化的 claim_id，而使用：

```text
data/eval/w3_support_labels.jsonl
```

```json
{
  "question_id": "d001",
  "claim_text_normalized": "……",
  "label": "supported",
  "note": "引文覆盖年级与排名条件"
}
```

标注值：`supported` / `partial` / `unsupported`。同一 claim 文本重复时用序号区分；标注文件记录标注人、日期和 run hash。

### 10.3 指标

| 指标 | 定义 | W3 目标 |
|---|---|---:|
| 引用 ID 有效率 | citation ID 在 Evidence 中且 part_no 正确 | 100% |
| 编造 ID 数 | 不在 Evidence 中的 citation | 0 |
| quote 可定位率 | 能映射回原文 span 的 quote | 100% |
| draft 支持率 | 模型生成的全部 claim 中人工标为 supported 的比例 | 报告基线 |
| rendered 支持率 | 最终渲染 claim 中 supported 的比例 | ≥85% |
| partial 率 | 有部分 claim 被省略的题比例 | 报告 |
| 拒答召回 | 6 道非可答题中明确拒答/不渲染答案的比例 | ≥80% |
| 误拒率 | 14 道可答题中被拒答（非 ANSWERED/PARTIAL）的比例 | ≤20% |
| 原因码正确率 | 非可答题的 reason code 与期望一致 | 报告 |
| issue 分布 | 各 blocking/warning code 数量 | 报告 |
| 延迟 | 端到端 p50/p95 | 记录；沿用 W2 p95 已知限制 |

说明：

- `supported` 才计入支持率；`partial` 单列，不四舍五入成 supported；
- partial answer 仍算“有答案”，但不算“完整回答”；
- 支持率必须人工判定，LLM 只可做预标注，不作为最终结论；
- 20 题样本小，报告用“开发集方向性证据”，不声称统计显著。

### 10.4 错误归因

每个失败题标记唯一主因：

- `RETRIEVAL_MISS`；
- `QUOTE_MISMATCH`；
- `NUMBER_NOT_SUPPORTED`；
- `AUDIENCE_MISMATCH`；
- `VERSION_MISMATCH` / `ONLY_REPEALED`；
- `OVER_REFUSAL`；
- `PARTIAL_INCOMPLETE`；
- `GENERATION_FAILED`；
- `LABEL_ERROR`。

报告列出每类数量，不只给总分。

---

## 11. 测试策略

### 11.1 单元测试

- `normalize_with_map`：空白、全半角、引号/括号、原文 span；
- `locate_quote`：exact、normalized、不可定位、省略号；
- fact 抽取：百分比、日期、期限、金额、区间、版本标签/条款号忽略；
- fact coverage：多 claim、多 citation、跨 quote 覆盖、漏覆盖；
- H5/H6/H10、`ONLY_REPEALED` 探针、`VERSION_CONFLICT`；
- 判定树：PASS/PARTIAL/ABSTAIN、原因码优先级；
- Finalize partial 模板不产生事实句。

### 11.2 集成测试

- FakeChat 返回 1 条有效 + 1 条数字不支持 → `PARTIAL_ANSWER`，只渲染 1 条；
- FakeChat 全部 claim 失败 → `UNVERIFIABLE`，无答案正文；
- 有 Evidence 但全部 claim 缺失 citation → `UNVERIFIABLE`；
- 只命中 repealed 的合成 Qdrant → `ONLY_REPEALED`；
- 同 doc 两个 active 重叠版本 → `VERSION_CONFLICT`；
- 隐私 quote → claim 丢弃/拒答；
- trace 包含 issues、partial、probe 字段，不含原文全文。

所有测试使用 fake embedding/chat 和内存 Qdrant，不访问网络。

### 11.3 开发集运行

`python scripts/eval_w3.py`：

1. 加载 W3 run 配置；
2. 逐题调用 QAService；
3. 保存 run 快照；
4. 读取人工 support labels；
5. 输出 `data/eval/w3-dev-report.{json,md}`；
6. 未标注 claim 在报告中单列为 `unlabeled`，不能算 supported。

---

## 12. 风险与处理

| 风险 | 影响 | 处理 |
|---|---|---|
| 数字/日期抽取误报 | 可答题被误拒 | 只抽带单位/日期结构的事实；开发集校准；报告误报类型 |
| fact 抽取漏报 | 不支持的数字未被拦住 | 报告漏检；W6 语义核对补强；不宣称 100% 覆盖 |
| quote span 映射错误 | UI 高亮/引用定位错误 | 映射单测覆盖空白/全半角/标点；保留 exact 快路径 |
| partial 答案不完整 | 用户误以为答案完整 | 固定提示 + partial 标记 + 报告单列 |
| 20 题支持标注过拟合 | 指标不可外推 | 明确 dev_v0、单标注限制；W7 冻结测试集 |
| 版本冲突只在 Evidence 内检测 | 漏掉未召回的冲突 | 报告为 W3 限制；W4 接版本解析器 |
| 受众检查过于严格 | 可答题误拒 | 只在显式受众时硬检查；未指定只 warning |
| 输出隐私扫描误伤公开电话 | 误拒/误删 | 只拦手机号、身份证、学号、个人邮箱；官方邮箱/座机放行 |
| p95 已知抖动 | 延迟不稳定 | W3 不增加 LLM 调用；沿用 W2 豁免记录，不宣称 p95 达标 |

---

## 13. 实施顺序

1. **原文 span 层**：`normalize_with_map`、`locate_quote`、Citation 字段迁移和测试。
2. **Fact 层**：抽取/规范化/覆盖检查、W3 issue code、参数化测试。
3. **Verifier 层**：H1/H3/H4/H5/H6/H7/H10、`ONLY_REPEALED` 探针、判定结果。
4. **QAService/Finalize/UI**：PARTIAL_ANSWER、UNVERIFIABLE、trace、partial 提示。
5. **W3 评测**：冻结 run、人工 support 标注、eval_w3 报告。
6. **文档与回归**：README/周记录、全量测试、W2 指标回归。

每步必须有可运行测试或可验证产物；不把整个 Verifier 塞进一个 prompt。

---

## 14. 验收门

- `locate_quote` 能把归一化 quote 映射回原文 span，exact/normalized 均有测试；
- W3 硬校验 H1/H3/H4/H5/H6/H7/H10 有单元测试，H9 明确延后 W5；
- 任何未通过核对的 claim 不会出现在最终答案；
- `PARTIAL_ANSWER` 有可见提示和 issue trace；全部失败返回 `UNVERIFIABLE`；
- dev_v0 20 题可运行，结构门禁 100%、编造 ID 0、quote 可定位 100%；
- 人工 support 标注完成：rendered 支持率 ≥85%、拒答召回 ≥80%、误拒率 ≤20%；
- 报告包含 draft/rendered 支持率、partial 率、issue 分布、拒答混淆矩阵和错误归因；
- 全量离线测试通过；W2 的 p95 已知限制继续如实记录，不新增“延迟达标”声明。

---

## 15. 与后续版本的接口

- W4：将 `audience_detail`、版本解析、混合召回/rerank 接入同一 Verifier；补 `AUDIENCE_UNKNOWN`、`NO_EFFECTIVE_VERSION`、`DATE_UNKNOWN`。
- W5：H9 流程事实核对；PARTIAL/ABSTAIN 文案扩展到流程指引。
- W6：在 Verifier 前接入 Advisor 重写循环；语义核对只在硬校验通过后调用；`PARTIAL_ANSWER` 仍保留为最终兜底。
- W7：冻结 50 题测试集，报告 support/abstention 的 Wilson 区间和消融。

W3 的 `VerificationIssue`、`VerificationResult`、`PARTIAL_ANSWER`、原文 span 和 fact coverage 是后续版本的稳定契约；后续只扩展检查项和原因码，不改变“未核实 claim 不进入答案”的语义。
