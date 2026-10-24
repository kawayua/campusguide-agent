# CampusGuide W7 设计：冻结测试集与系统效果评测

| 项目 | 内容 |
|---|---|
| 文档版本 | 0.1（设计稿；执行结果见 `docs/weekly/w7.md`） |
| 日期 | 2026-10-24 |
| 基线 | W4 tag `w4`；W6-A2 tag `w6a2`；P0/P1 工作树（399 tests） |
| 范围 | 50 题冻结单轮测试集、人工标注、主结果、检索/过滤/Verifier 消融、Wilson 区间 |
| 非目标 | W5 流程 DSL/checklist、W6 历史版本回答与语义重写、W8 部署 |
| 状态 | 已执行三轮 remediation；remediation3（w6a2.3）质量门禁全部通过，但 p50/p95 未达标，W7 总门禁仍为未通过；A5 有效，A4/A2 为 w6a2.1 历史消融 |

## 0. 决策摘要

W7 做五件事：

1. **冻结 50 题单轮测试集**：40 可答 + 10 不可答；与 `dev_v0.jsonl`、`population_v1.jsonl` 零重复；
2. **冻结机制**：`test_v1.jsonl` + SHA-256 manifest；runner 拒绝执行被修改的测试集；
3. **系统效果报告**：通过生产 MCP QAService 跑全量 50 题，结构、检索、支持、正确性、拒答、版本、延迟、成本；
4. **人工标注**：claim 级 support + 题级 answer correctness；AI 预标注不计入门禁；
5. **可执行消融**：A5 检索（dense / RRF / rerank）、A4 元数据过滤、A2 Verifier on/off；A1 分块基线与 A3 流程 DAG 明确标 N/A，不伪造结果。

W7 不改变 W3 的 `VerificationIssue` / `VerificationResult` / `PARTIAL_ANSWER` 契约，
也不在冻结后调阈值。阈值冻结前由 dev_v0 决定；测试集只用于报告。

## 1. 当前能力与范围裁剪

当前已实现 W1–W4、W6-A2、P0/P1：

- 单轮条款/章节问答，claim-first + 确定性 Verifier + 拒答；
- bge-m3 dense + BM25 sparse + RRF + bge-reranker；
- 受众/时间过滤、profile-aware 查询变体、section 表格降权；
- 版本意图：`intake_year` 与问题版本年份不一致时 `VERSION_MISMATCH`。

尚未实现：

- W5 流程 DSL、槽位追问、校历；因此 W7 **不包含流程 checklist 指标**；
- W6 历史版本回答/语义重写；历史版问题只作为拒答类或版本敏感类，不作为可答题；
- 固定长度分块索引；因此 A1 分块消融标 N/A。

原总设计 40 可答中 5 道流程判断题，W7 替换为 5 道单条款/多条款题；报告明确
说明“流程指标延后到 W5”，不把未实现能力写成通过。

## 2. 测试集构成

`data/eval/test_v1.jsonl`，共 50 题：

| 类别 | 数量 | 说明 |
|---|---:|---|
| `single_article` | 25 | 单条款事实/条件/金额/期限 |
| `multi_article` | 10 | 跨 2 条条款或跨文档组合 |
| `section` | 5 | 培养方案/章节结构，`block_types=["section"]` |
| 可答小计 | **40** | 含版本敏感子集 |
| `out_of_scope` | 4 | 其他学校/校外政策 |
| `repealed` | 3 | 已废止/旧版制度 |
| `privacy` | 3 | 个人学号/成绩/排名等 |
| 不可答小计 | **10** | 期望明确拒答 |

**版本敏感题 5 道**（可答子集，单独统计）：使用 profile intake_year 与
当前版本年份，如“2024 级农学问 2024 版培养方案”；期望 `ANSWERED` 且命中
当前版本，不允许返回旧版。

**不可答题判定**：`repealed` 类可接受 `HISTORICAL_NOT_SUPPORTED` 或
`ONLY_REPEALED`；`out_of_scope` 期望 `OUT_OF_SCOPE`；`privacy` 期望
`PRIVACY_REQUEST`。主指标只要求“明确拒答”，reason code 另列正确率。

### 2.1 Schema

```json
{
  "qid": "test-001",
  "split": "test",
  "category": "single_article",
  "question": "……",
  "user_profile": {
    "audience": "undergraduate",
    "colleges": [],
    "majors": [],
    "intake_year": null,
    "stage": null,
    "program_type": null,
    "groups": [],
    "status": null
  },
  "block_types": ["article"],
  "as_of": "2026-10-24",
  "gold_article_ids": ["doc@v2024#第九条"],
  "gold_quote_spans": [[12, 38]],
  "gold_answer": "……",
  "required_facts": ["学习成绩优异", "综合表现突出"],
  "should_abstain": false,
  "expected_outcome": "ANSWERED",
  "version_sensitive": false,
  "notes": "",
  "annotator": "yaukin",
  "reviewed_at": "2026-10-24"
}
```

约束：

- `gold_article_ids` 必须存在于 `data/processed/articles.jsonl`；
- `should_abstain=false` 时至少 1 个 gold；`true` 时 gold 为空；
- `block_types` 与 gold 的 `block_type` 一致（article/section 不混算）；
- `version_sensitive=true` 时 profile 必须有 `intake_year`；
- 题面不得包含 gold 的完整原文，避免直接泄漏；
- 不使用 `dev_v0` / `population_v1` 的题面或只改标点。

## 3. 冻结机制

新增：

- `scripts/freeze_testset.py --draft data/eval/test_v1.draft.jsonl --output data/eval/test_v1.jsonl --manifest data/eval/test_v1.manifest.json`
- `src/campusguide/eval/w7.py` 中的 `validate_testset()` / `freeze_manifest()` / `verify_frozen()`。

冻结校验：

1. 50 个唯一 `qid`、题面唯一；
2. 类别数量严格匹配 25/10/5/4/3/3；
3. 与 dev_v0、population_v1 的归一化题面零重复；
4. gold ID 全部存在；article/section 类型一致；
5. `should_abstain` 与 gold/expected_outcome 自洽；
6. 所有 `annotator` 非空、`reviewed_at` 非空；
7. 规范化 JSONL 的 SHA-256 写入 manifest；
8. runner 启动时校验 manifest 与文件 hash，不一致 → `TEST_SET_CHANGED`，拒绝运行。

冻结后只允许修正标注错误，并在 `data/eval/test_v1.changelog.md` 记录：
改了什么、为什么、影响哪些题、旧 hash/新 hash。**不允许根据模型结果改题。**

## 4. Runner 与报告

`scripts/eval_w7.py`：

- 默认通过 `create_mcp_qa_service` 走生产 MCP 路径；
- 按每题的 `user_profile`、`block_types`、`as_of` 调 `QAService.ask`；
- 输出 `data/eval/w7_run.json`：run hash、数据集 hash、模型、prompt、索引版本、
  config hash、逐题 outcome/decision/evidence/citations/issues/latency/token；
- 生成 `w7_support_labels.jsonl`（claim 级）和 `w7_answer_labels.jsonl`（题级）；
- 支持 `--limit` 只做 smoke，但**正式报告必须 full 50**，报告中标记 limit 则不得称为冻结结果。

报告 `data/eval/w7-dev-report.{json,md}` 包含：

- 运行配置与测试集 hash；
- 主表：Overall / single / multi / section / version-sensitive；
- 检索：article/section Recall@8、MRR；
- 结构：引用 ID 有效率、quote 可定位率、编造 ID、part_no 错误；
- 支持：draft/rendered 人工 support；
- 正确性：题级 correct/partial/incorrect，`required_facts` 覆盖；
- 拒答：混淆矩阵、拒答召回、误拒率、reason code 正确率；
- 延迟 p50/p95、每题 token/成本；
- 错误归因（12.7 的类别）；
- 消融表；
- 限制与单标注披露。

比例指标一律给 **Wilson 95% 置信区间**；50 题只作方向性证据，不把 90% 说成精确值。

## 5. 人工标注

### 5.1 Claim support

沿用 W3/W6-A2 的严格引文级口径：

- `supported`：quote 直接支持 claim 的主要事实，条件/范围/数字一致；
- `partial`：quote 支持主要事实，但 claim 多了关键条件/范围/第二断言，或强弱不一致；
- `unsupported`：主要事实不符、矛盾或仅主题相关；
- 不看条款上下文，不看问题，不补外部知识。

`w7_support_review.md` 生成 Markdown 核对表；`annotator` 为空或以 `ai` 开头
的记录按 `unlabeled` 处理。旧 run 的 labels 不复用。

### 5.2 Answer correctness

题级标注 `correct / partial / incorrect / unanswerable_correct`：

- `correct`：关键事实、条件、时限、金额均正确，引用有效；
- `partial`：主要事实正确，但漏条件/多条件/表述不完整；
- `incorrect`：关键事实错误或引用不支持；
- 不可答题：明确拒答且 reason code 合理记 `unanswerable_correct`。

记录人工修改处数：改事实、改条件、删无依据句、补引用分别计数。

### 5.3 复核

- 20% 题目由第二人复核；若只有作者，至少隔天盲审一次，并在报告披露单标注；
- 标注分歧写入 `notes`，不静默合并；
- AI 预标注只做候选，不计入门禁。

## 6. 消融

| 编号 | 变体 | 当前可执行 | 指标 |
|---|---|---|---|
| A5 | dense-only / hybrid RRF / RRF+rerank | 是（复用 W4/P1 runner） | Recall@8、MRR、p95 |
| A4 | 有/无 profile 元数据过滤 | 是（同一测试集，关闭 profile 重跑） | 人群错配率、拒答/误拒 |
| A2 | 有/无 Verifier | 是（eval-only pass-through verifier） | support、无依据 claim 率、误拒 |
| A1 | 固定长度分块 vs 条款级 | **N/A**：无固定长度索引 | — |
| A3 | 有/无流程 DAG | **N/A**：W5 未实现 | — |

A2 的“无 Verifier”只用于评测，不进入生产；报告明确它会把未校验 claim
直接渲染，结构门禁不适用，只用于隔离核对层贡献。

## 7. 门禁

| 指标 | 目标 |
|---|---|
| 引用 ID 有效率 / quote 可定位率 | 100% / 100% |
| 编造 ID / part_no 错误 | 0 / 0 |
| 引用支持率（人工） | ≥95% |
| 可回答题正确率（人工） | ≥85% |
| 拒答召回（不可答题） | ≥90% |
| 误拒率（可答题） | ≤10% |
| 版本敏感题正确率 | ≥90% |
| article Recall@8 | ≥90% |
| p50 / p95 延迟 | p50 ≤6s，p95 ≤10s |
| 人工修改处数 | ≤1.0/题 |

- p95 若因第三方端点长尾不达标，报告实际值并标记“已知限制”，不删题、不改门禁；
- support/correctness 未完成人工标注时，W7 不得声称通过；
- 指标不达标时只修错误归因中占比最高的两类问题，不改测试集。

## 8. 执行顺序

1. **Draft 50 题**：从 `articles.jsonl` 选题，写 standard answer / required facts /
   gold IDs；人工检查题面不泄漏、gold 正确性。（本阶段可 AI 辅助起草，但必须人工确认）
2. **Freeze**：`freeze_testset.py` 校验并写 manifest/hash。
3. **Pilot smoke**：`--limit 3` 验证 MCP、profile、section、标签模板。
4. **Full run**：50 题，warm；记录 run hash/index version。
5. **Human labels**：support + answer correctness；20% 复核。
6. **Ablations**：A5/A4/A2；N/A 项明确标注。
7. **Report**：主表、分类、Wilson 区间、混淆矩阵、错误归因、限制。
8. **Closeout**：README 只引用冻结报告数字；不把 dev 指标写成测试集指标。

## 9. 风险与处理

| 风险 | 影响 | 处理 |
|---|---|---|
| 测试集与 dev 泄漏 | 指标虚高 | 零重复校验；题面/答案人工审；冻结 hash |
| 50 题样本小 | 区间宽 | Wilson 95% CI；方向性证据表述 |
| 只有单标注者 | 支持率偏差 | 隔天盲审 + 披露；20% 第二人 |
| 流程能力未实现 | 无法覆盖 checklist | W7 主表不含流程；明确延后 W5 |
| p95 长尾 | 延迟门禁失败 | 报告 p50/p95 与端点长尾；不删题 |
| 表格 section 生成失败 | 答案错误/拒答 | P1 section-aware 选择 + 重试；错误归因单列 |
| 冻结后想调阈值 | 评测污染 | 阈值冻结前用 dev；冻结后只修标注错误并记账 |
| 消融不可比 | 结论无效 | 同测试集/模型/prompt/index；N/A 不伪造 |

## 10. 与后续版本接口

- W5：补流程题/checklist 后，可新增 `test_v2` 或扩展 W7 报告，不改 `test_v1`；
- W6：历史版本回答实现后，可把 `repealed` 类从“只拒答”升级为“历史回答”单独评测；
- W8：README/简历只引用 `test_v1` 冻结报告；dev/P0/P1 数字只作开发证据；
- W9/W10：不修改已冻结测试集；新增能力另建测试集并记录 hash。
