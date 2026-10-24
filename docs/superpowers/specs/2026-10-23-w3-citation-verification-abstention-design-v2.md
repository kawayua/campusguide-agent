# CampusGuide W3 v2 设计：引用核对与拒答 v0（测试先行重建）

| 项目 | 内容 |
|---|---|
| 文档版本 | 1.0（最终重建设计） |
| 日期 | 2026-10-23 |
| 基线 | W2 实现 `ffd8bf8`（W3 v1 已回退并归档） |
| 范围 | 引文 span、fact 覆盖、确定性硬校验、拒答判定树 v0、20 题开发集 |
| 状态 | 已按测试先行实施（2026-10-23）；live run 完成，人工 support 标注完成 56/56；rendered 支持率 75.0% <85%，W3 总门禁未通过；下一步 W6 |
| 旧版本 | `docs/superpowers/archive/w3-v1/`，仅作参考，不复用代码 |

---

## 0. 重建原因

W3 v1 已经实现过一版确定性 Verifier，但最终状态不能满足 W3 门禁：

- rendered 支持率 75.0%，低于 85% 门禁；
- 57 条 support 标注为 AI 预标注，没有形成正式人工评测；
- Verifier 出现过 `blocking` issue 与 `PASS/ANSWERED` 并存；
- 中文数字/日期覆盖存在漏检；
- trace 与报告的证据链不完整，报告 run 快照早于最终代码。

W3 v2 **保持 W3 的原范围**，但换一种实现顺序：

1. 先写会失败的契约测试和回归测试；
2. 再实现最小通过代码；
3. 最后才跑 live 开发集和人工标注。

W3 v2 不把“部分作答”降级成“静默丢 claim”，也不把“AI 预标注”当成人工验收。

---

## 1. 目标与非目标

### 1.1 目标

- 引文从归一化字符串定位升级为可映射回 `article.text` 原文 span；
- claim 中的数字、百分比、日期、期限、金额必须被本轮引用 quote 覆盖；
- Evidence 的受众/版本状态做确定性检查；
- 输出层做隐私扫描；
- 用 `PASS / PARTIAL_ANSWER / UNVERIFIABLE` 表达最终决定；
- 任何未通过核对的 claim 不进入答案；
- 20 题 dev_v0 可复现运行，并完成 **人工** support 标注；
- 报告包含结构门禁、draft/rendered 支持率、拒答混淆矩阵、误拒率、partial 率、issue 分布和错误归因。

### 1.2 非目标

- 语义蕴含判断（W6）；
- LLM 重写/重试循环（W6）；
- 多轮追问、槽位（W5）；
- MCP、sparse、rerank（W4）；
- 流程事实核对 H9（W5）；
- 完整历史版本解析（W4）；
- 50 题冻结测试集与消融（W7）；
- 放宽 85% 支持率门禁；
- 使用 AI 预标注直接充当人工评测。

---

## 2. 从 W2 继承的契约

W3 v2 直接复用 W2 的：

- `QAService` 单轮流程、guard、Evidence、Advisor、Finalize；
- `Claim`、`Citation`、`Evidence`、`OutcomeCode`；
- dense 检索、article-only 默认、低置信日期提示；
- `PARTIAL_ANSWER` 的固定提示和“只渲染通过 claim”原则。

W3 v2 不修改 W2 的检索/生成主路径，只在其后增加确定性核对层；W6 再把核对层包进 Verifier 图节点。

---

## 3. 核心不变量

以下不变量必须由测试和运行期断言同时保证：

1. **答案中不得出现未通过核对的 claim**；
2. **`decision=PASS` 时不得存在任何 `severity=blocking` 的 issue**；
3. **claim 被丢弃时，答案必须显示 `PARTIAL_ANSWER` 或整题 `UNVERIFIABLE`**；
4. **每个 citation 的 `article_id + part_no` 必须来自本轮 Evidence**；
5. **每个 quote 必须能映射回被引 part 的 `article.text` 原文 span**；
6. **claim 中的业务 fact 必须出现在该 claim 的引用 quote 并集中**；
7. **trace 必须包含 scope issues、claim issues 和 dropped IDs**；
8. **没有人工 support 标注时，报告不得声称支持率门禁通过**。

任何一条不变量失败，W3 v2 不得标记通过。

---

## 4. 核对契约

### 4.1 Issue 模型

```python
class IssueScope(StrEnum):
    CLAIM = "claim"        # claim 不能进入答案
    CITATION = "citation"  # 单个 citation 被丢弃
    EVIDENCE = "evidence"  # 单个 evidence 被 scope 过滤

class IssueSeverity(StrEnum):
    BLOCKING = "blocking"
    WARNING = "warning"

class VerificationIssue(BaseModel):
    code: str
    scope: IssueScope
    severity: IssueSeverity
    claim_id: str | None = None
    article_id: str | None = None
    part_no: int | None = None
    detail: str = ""
    facts: list[str] = Field(default_factory=list)
```

**严重性规则**：

- `CLAIM + BLOCKING`：claim 不进入答案；
- `CITATION + WARNING`：citation 被丢弃，但 claim 仍有有效 citation；
- `EVIDENCE + WARNING`：evidence 被过滤，但仍有有效 evidence；
- 不允许 `BLOCKING` 与 `PASS` 同时出现；
- 如果 claim 没有剩余有效 citation/evidence，则提升为 `CLAIM + BLOCKING`，最终 claim 被丢弃。

### 4.2 Result 模型

```python
class VerificationDecision(StrEnum):
    PASS = "pass"
    PARTIAL = "partial"
    ABSTAIN = "abstain"

class VerificationResult(BaseModel):
    decision: VerificationDecision
    claims: list[Claim]
    issues: list[VerificationIssue]
    dropped_claim_ids: list[str]
    reason_code: OutcomeCode | None = None
```

决策算法固定为：

```text
if no claims after validation:
    ABSTAIN (NO_VALID_CLAIM if drafts empty else UNVERIFIABLE)
elif any claim dropped by CLAIM+BLOCKING:
    PARTIAL (reason=PARTIAL_ANSWER)
else:
    PASS
```

`PARTIAL` 必须至少有一个 `dropped_claim_ids`；`PASS` 必须没有 `CLAIM+BLOCKING` issue。

### 4.3 原因码

W3 v2 使用：

```text
PARTIAL_ANSWER
ONLY_REPEALED
VERSION_CONFLICT
VERSION_MISMATCH
UNVERIFIABLE
NO_VALID_CLAIM   # 模型零 claims
NO_EVIDENCE
PRIVACY_REQUEST
OUT_OF_SCOPE
HISTORICAL_NOT_SUPPORTED
GENERATION_FAILED
INDEX_UNAVAILABLE
```

`AUDIENCE_MISMATCH`、`PRIVACY_VIOLATION` 等 issue 在 W3 v2 映射为 `UNVERIFIABLE` 或 `PARTIAL_ANSWER`；专用原因码留 W4。

---

## 5. 引文 span 层

### 5.1 目标

`Citation.quote_start/end` 表示 `article.text` 的原文偏移，而不是归一化文本偏移。

### 5.2 实现契约

```python
def normalize_with_map(text: str) -> tuple[str, list[int]]: ...
def locate_quote(text: str, quote: str) -> tuple[int, int, Literal["exact", "normalized"]] | None: ...
```

- 先 exact `text.find(quote)`；
- 失败后对 `normalize_text(text)` 查找，再通过索引映射回原文；
- 映射必须与 `normalize_text` 使用同一套 NFKC、零宽、引号/括号/全角空格变换；
- 空 quote、纯空白 quote、找不到 → `None`；
- 多 part 条款偏移相对被引 part 的 `article.text`；
- 测试覆盖：空白、全半角、弯引号、零宽字符、跨行、找不到。

### 5.3 失败

- `QUOTE_TOO_SHORT` / `QUOTE_ELLIPSIS` / `QUOTE_MISMATCH`；
- 单个 citation 失败但 claim 有另一个有效 citation → citation warning，claim 保留；
- claim 无有效 citation → `CLAIM + BLOCKING`，claim 丢弃。

---

## 6. Fact 覆盖层

### 6.1 Fact 模型

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

### 6.2 抽取规则

只抽取有业务含义的 fact：

| 类型 | 示例 | canonical |
|---|---|---|
| 百分比 | `10%`、`百分之十`、`前30%` | `10%`、`30%` |
| 日期 | `2025年9月1日`、`2025-09-01`、`二〇二五年九月一日` | `2025-09-01` |
| 年份 | `2025年`（非“2025年版”） | `2025` |
| 期限 | `连续两年`、`10个工作日`、`3个月` | `2年`、`10工作日`、`3月` |
| 金额/学分/学时 | `8000元`、`两万元`、`10学分` | `8000元`、`20000元`、`10学分` |
| 区间 | `10—28学分`、`3-4个学期` | 两个端点 fact |

忽略：

- `第X条`、`第X章`、`第X部分`；
- 列表序号 `（一）`；
- 版本标签 `2025年版`、`v2025`；
- 无单位/上下文裸数字；
- 无法可靠规范化的中文数字（记录为未抽取，不猜测）。

### 6.3 中文数字要求

W3 v2 必须覆盖：

- `一`/`二`/`两`/`三` … `十`/`百`/`千`/`万`；
- `两` 与 `2` 等价，`两万元`、`两年`、`两个工作日`；
- `二〇二五` 年份连写；
- 中文日期 `二〇二五年九月一日`；
- `十万`、`一百万` 等 `万` 组合；
- 中文数字与阿拉伯数字的相互匹配。

这些不是“锦上添花”，而是上一版误拒的直接来源。

### 6.4 覆盖规则

对每条 claim：

1. 抽取 claim 的 facts；
2. 收集该 claim 所有 citations 的 quote；
3. 每个 fact 必须匹配任一 quote 的归一化文本；
4. 多 citation 可分摊 fact；不同 claim 不共享；
5. 未覆盖 → `NUMBER_NOT_SUPPORTED`（CLAIM+BLOCKING），列出 `facts`；
6. 抽取规则宁可漏检不可误杀；无法规范化的 fact 不强行拦截，但要在报告记录。

### 6.5 测试先行

先写参数化测试，再实现：

```text
连续2年 / 连续两年
奖励2万元 / 奖励两万元
2025年9月1日 / 二〇二五年九月一日
10个工作日 / 十个工作日
2025年9月1日 / 2025-09-01
2025年9月1日 / 2025/09/01
十万 / 100000
```

测试必须同时覆盖“应通过”和“应失败”的 case。

---

## 7. 受众、版本与隐私

### 7.1 H5 受众

- 用户显式指定受众：Evidence 的 `audience` 必须包含该受众或 `all`；
- 不匹配的 evidence：`EVIDENCE + WARNING`，从 Evidence 中剔除；
- 全部 evidence 被剔除：`UNVERIFIABLE`；
- 未指定受众：`AUDIENCE_UNDETERMINED` warning，不因未知身份直接拒答；
- `Audience.ALL` 视为“未指定”，不做硬过滤。

### 7.2 H6 版本

- 每条 evidence 必须 `status=active`、`effective_from <= as_of`、
  `effective_to is null or >= as_of`；
- 不满足：`EVIDENCE + WARNING`（剔除）；全部剔除则 `VERSION_MISMATCH`；
- 同一 `doc_id` 多 active 版本且 `effective_from` 相同：`VERSION_CONFLICT`；
- 不同生效日的届别版本：`MIXED_VERSIONS` warning，保留证据；
- 低置信日期不阻断，引用卡片继续显示锚点提示；
- 历史问题仍由 guard 返回 `HISTORICAL_NOT_SUPPORTED`；
- 完整 `NO_EFFECTIVE_VERSION` / `DATE_UNKNOWN` 分支留 W4。

### 7.3 废止探针

- 主检索为空时，用 `status=[repealed]` 做一次无 LLM 检索；
- 命中 → `ONLY_REPEALED`；
- 未命中 → `NO_EVIDENCE`；
- 探针耗时单列，不混入问答 p50/p95；
- 探针失败按无命中处理，记录错误但不阻塞主流程。

### 7.4 H10 隐私

- 扫描 claim 文本和 citation quote；
- 手机号、身份证、学号、非 `@scau.edu.cn` 个人邮箱 → `PRIVACY_VIOLATION`，CLAIM+BLOCKING；
- 部门公开座机、`@scau.edu.cn` 官方邮箱允许；
- 全部 claim 被丢弃 → `UNVERIFIABLE`；
- 不在 trace 记录原始隐私值。

---

## 8. 硬校验矩阵

| 编号 | 检查 | Issue 范围 | 失败处理 |
|---|---|---|---|
| H1 | citation ID 在本轮 Evidence | CITATION/CLAIM | 无有效 citation → claim 丢弃 |
| H2 | citation ID 在知识库 | — | Evidence 已从索引还原，H1 覆盖；不单独查库 |
| H3 | quote 映射回原文 span | CITATION/CLAIM | 丢弃 citation；无 citation → claim 丢弃 |
| H4 | fact 被 quote 覆盖 | CLAIM | 丢弃 claim |
| H5 | 受众兼容 | EVIDENCE | 剔除不兼容 evidence；全部剔除 → 拒答 |
| H6 | 状态/生效区间正确 | EVIDENCE | 剔除；冲突/全部剔除 → 拒答 |
| H7 | 每个 claim 至少一个 citation | CLAIM | 丢弃 claim |
| H8 | 答案无未映射事实句 | — | Finalize 只渲染 claims；结构性测试 |
| H9 | 流程事实与 YAML 一致 | — | 延后 W5 |
| H10 | 输出无隐私数据 | CLAIM | 丢弃 claim |

W3 v2 的所有检查都是确定性规则；不调用 LLM 做核对。

---

## 9. 拒答与部分作答

### 9.1 决策顺序

```text
1. guard
2. 主检索为空 → 废止探针 → ONLY_REPEALED / NO_EVIDENCE
3. scope：版本/受众 → 过滤 evidence；全部无效 → 拒答
4. Advisor 失败 → GENERATION_FAILED
5. 零 claims → NO_VALID_CLAIM
6. claim 级校验
   - 全部 claim 丢弃 → UNVERIFIABLE
   - 部分丢弃 → PARTIAL_ANSWER
   - 无丢弃 → ANSWERED
```

### 9.2 PARTIAL_ANSWER

- 只渲染通过 claim；
- 固定提示：“部分结论未通过引用核对，已省略；以下仅为可核实内容。”；
- `QAResult.partial=True`，`dropped_claim_ids` 非空；
- UI 显示省略数量和 issue 类型；
- 不允许把 partial 计入“完整回答”。

### 9.3 全部失败

- 返回 `UNVERIFIABLE`；
- 不输出任何 claim；
- 不把模型原文透传；
- 可给出原文入口；不伪造部门/电话；
- W6 再补重写循环。

---

## 10. Trace、UI 与报告

### 10.1 Trace

每次问答记录：

```json
{
  "verification": {
    "decision": "partial",
    "issues": [],
    "scope_issues": [],
    "dropped_claim_ids": [],
    "partial": true
  },
  "repealed_probe": {"hit": false, "latency_ms": 0}
}
```

要求：

- `issues` = scope issues + claim/citation issues 的合并；
- 不记录完整证据文本、原始隐私值、API key；
- `probe_ms` 单独记录；
- 报告和 trace 使用同一 issue 集合。

### 10.2 UI

- 答案区：claims + 固定模板；
- 引用卡片：doc_title、article_no、block_type、source_url、quote、低日期提示；
- 调试区：outcome、partial、issues、dropped IDs、检索 ID/分数、耗时；
- 单轮；无多轮历史。

### 10.3 评测产物

- `data/eval/w3_run.json`：最终代码运行快照；
- `data/eval/w3_support_labels.jsonl`：人工 support 标注；
- `data/eval/w3-dev-report.{json,md}`：报告；
- 所有 run 记录最终 git commit、model、vector manifest hash、config hash、warm/cold 状态。

---

## 11. 开发集与人工标注

### 11.1 数据集

继续使用 W2 的 `data/eval/dev_v0.jsonl` 20 题，不修改题目和期望 outcome：

- 14 可答；
- 6 非可答。

section 题仍按 `block_types` 子集运行。

### 11.2 冻结运行

- 在最终代码上运行一次，生成 `w3_run.json`；
- 记录最终 commit、模型、索引 hash、warm/cold；
- 该 run 是标注对象；不要在标注后悄悄更换模型或代码。

### 11.3 人工 support 标注

- 标 57 条左右的 draft claim；
- 标签：`supported` / `partial` / `unsupported`；
- 允许 AI 预填，但最终必须人工逐条确认；
- 记录 `annotator`、`labeled_at`、`run_sha256`；
- `unlabeled` 不计入 support 分母，但必须在报告中显示；
- 严格引文级口径：只认 quote 本身能否支撑 claim，不拿条款上下文补足；
- 如果人工结论与 AI 预标注冲突，以人工为准，并在 `note` 记录。

### 11.4 指标

| 指标 | 定义 | W3 目标 |
|---|---|---:|
| 引用 ID 有效率 | ID 在 Evidence 且 part_no 正确 | 100% |
| 编造 ID 数 | 不在 Evidence 的 citation | 0 |
| quote 可定位率 | span 可映射回原文 | 100% |
| draft 支持率 | 全部 draft claim 的人工 supported 比例 | 记录 |
| rendered 支持率 | 最终渲染 claim 的人工 supported 比例 | ≥85% |
| partial 率 | 可答题中 outcome=PARTIAL_ANSWER | 记录 |
| 拒答召回 | 非可答题中不渲染答案 | ≥80% |
| 误拒率 | 可答题中非 ANSWERED/PARTIAL | ≤20% |
| 原因码正确率 | 非可答题 reason code 正确 | 记录 |
| issue 分布 | 各 code/severity/scope 数量 | 报告 |
| p50/p95 | 端到端；探针单列 | 记录 |

支持率只统计人工标注；AI 预标注不能作为最终门禁。

### 11.5 错误归因

- `RETRIEVAL_MISS`；
- `QUOTE_MISMATCH`；
- `NUMBER_NOT_SUPPORTED`；
- `AUDIENCE_MISMATCH`；
- `VERSION_MISMATCH` / `ONLY_REPEALED`；
- `OVER_REFUSAL`；
- `PARTIAL_INCOMPLETE`；
- `GENERATION_FAILED`；
- `LABEL_ERROR`。

每个失败题只归一个主因。

---

## 12. 测试先行计划

### 12.1 先写的失败测试

在实现前先提交这些测试（可以红）：

1. blocking issue + PASS 不可能同时出现；
2. 有效 + 无效 citation：claim 保留，citation 为 warning；
3. active + repealed evidence：active 可用，repealed 为 warning；全部无效才拒答；
4. `连续2年` / `连续两年` 覆盖；
5. `奖励2万元` / `奖励两万元` 覆盖；
6. `2025年9月1日` / `二〇二五年九月一日` 覆盖；
7. `chinese_to_int("十万") == 100000`；
8. trace 包含 scope issues；
9. 全部 claim 失败 → UNVERIFIABLE；
10. 部分失败 → PARTIAL_ANSWER，只渲染通过 claim。

### 12.2 测试分层

- 单元：quotes、facts、scope、decision、trace。
- 集成：FakeRetriever + FakeChat + 内存 Qdrant；覆盖 partial/abstain/probe/version。
- 评测单元：labels、support rate、confusion matrix、unlabeled、run hash。
- 无网络；live 运行不进入 CI。

### 12.3 每个实现步骤的完成条件

- 先有失败测试；
- 实现后测试通过；
- 全量 `pytest`、`ruff check`、`ruff format --check` 通过；
- 不降低既有 W2 测试。

---

## 13. 实施顺序

1. **契约与回归测试**：Issue scope/severity、blocking/PASS invariant、trace 合并、中文 fact 矩阵。
2. **引文 span**：`normalize_with_map` + `locate_quote` + Citation 迁移。
3. **Fact 层**：抽取、规范化、覆盖；先测试后实现。
4. **Scope/Verifier**：受众、版本、废止探针、隐私、决策。
5. **QAService/UI/trace**：PARTIAL/UNVERIFIABLE、partial 提示、trace。
6. **评测**：最终 run、人工 support 标注、报告。
7. **文档**：W3 周记录、README、评测说明。

每一步独立提交；不把整条 Verifier 塞进一个 prompt 或一个大文件。

---

## 14. 验收门

- 所有核心不变量有测试；
- `PASS` 不与 blocking 并存；
- partial/abstain 行为符合定义；
- 中文 fact 测试覆盖并通过；
- 24 个以上离线测试新增，W2 154 个测试不回归；
- 20 题 dev_v0 在最终代码上运行；
- 结构门禁 100%；
- 人工 support 标注完成；
- rendered 支持率 ≥85%；
- 拒答召回 ≥80%；
- 误拒率 ≤20%；
- 报告记录最终 commit、模型、索引 hash；
- README/周记录不把 AI 预标注或旧快照写成正式结果。

如果人工支持率仍 <85%，W3 不通过；可选择 W6 提示词/重写改进，或由用户明确接受 W3 例外。不得改测试阈值或伪造标注。

---

## 15. 与后续版本接口

- W4：MCP、sparse/rerank、完整 audience/version 解析；复用 Issue/Result 契约。
- W5：H9 流程事实核对；partial/拒答文案扩展到流程指引。
- W6：语义蕴含与重写循环；硬校验失败可先重写，再落到 PARTIAL/UNVERIFIABLE。
- W7：冻结 50 题测试集，报告 Wilson 区间与消融。

W3 v2 的稳定契约是：

- `VerificationIssue.scope`；
- `VerificationIssue.severity`；
- `VerificationResult.decision`；
- `PARTIAL_ANSWER` 与 `dropped_claim_ids`；
- 原文 quote span；
- fact canonicalization。

后续只扩展 issue code 和检查项，不改变“未核实 claim 不进入答案”的语义。

---

## 16. 实施状态（2026-10-23）

- 代码：`verification/{quotes,facts,privacy,scope,claims,verifier}.py`、
  `eval/w3.py`、`scripts/eval_w3.py`；UI/CLI/trace 已接入；
- 离线测试：154 → 295，新增 141 项（含 1 项 W6-A prompt 契约）；`ruff check` / `ruff format --check` 通过；
- live run：`data/eval/w3_run.json`，`run_sha256=5e542b25…24442`；
  结构门禁通过（55/55 引用 ID、0 编造、55/55 quote 可定位），
  拒答召回 6/6 = 100%，误拒率 0/14 = 0.0%，partial 率 1/14 = 7.1%，
  p50 3.34s / p95 9.78s，探针 p50 73.5ms（单列）；
- support：56 条 draft claim，**56/56 人工标注**（39 supported /
  17 partial / 0 unsupported）；draft 69.6%，rendered **75.0%**，
  support 门禁未通过，W3 总门禁未通过；
- run 元数据：commit `b240e4d`，dirty=True；W3 已按本轮 75.0% 判定不通过，
  不以重跑刷分；W6 正式验收前再提交代码并重跑；
- 详细记录见 `docs/weekly/w3.md`，报告见 `data/eval/w3-dev-report.{json,md}`。

未完成项：W6 的语义蕴含/重写；本轮人工标注已确认 rendered 75.0% <85%，
W3 不通过。不得把结构门禁通过写成 W3 完成或支持率达标。

后续：W6-A 提示词收紧已在 `mimo-v2.6-pro-ultraspeed` 上通过（54/54 人工，
53 supported / 1 partial / 0 unsupported，rendered 98.1%）。v2.6pro A/B 的
support 为 100%，但有 2 条 quote mismatch、d012 PARTIAL、p95 45.2s、
warmup 111s，可靠性低于 ultraspeed；默认模型已回退 ultraspeed。新 run 需要
重新人工确认 labels。记录见 `docs/weekly/w6.md`。
