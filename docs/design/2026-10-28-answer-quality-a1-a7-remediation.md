# R1–R9 复核整改记录（A1–A7，2026-10-28 第二轮）

依据 `docs/design/2026-10-28-answer-quality-r1-r9-review-next-steps.md` 的阻断项
A1–A7 整改。每条都以**真实历史 claim/原文**或复核给出的最小反例回放，不允许只改
合成 fixture。

离线回归：`& ".venv\Scripts\python.exe" -m pytest -q` → **812 passed**（含新增 32 项
真实回放/边界用例）。

## 状态定义

- **未做**：尚未实现。
- **代码接入**：实现已合并进主链路，但未用真实样本回放。
- **离线回放通过**：用真实历史 claim/原文或复核最小反例回放通过。
- **线上验收通过**：真实模型重跑该题并通过人工评判。

## 状态总表

| 项 | 状态 | 关键改动 | 回放/回归用例 |
| --- | --- | --- | --- |
| A1 跨版本事实误删 | 离线回放通过 | `qa/merge.py` 重写：结论键含对象/属性/层次/班型/年级版本/适用条件；近似去重前核对数值、否定、时间、范围；冲突并列保留并提示核实，不按引用数或顺序择一；合并后重算覆盖 | `test_r6_r7_merge_and_boundaries.py`、`test_real_replay_a1_a6.py`（2022/2024 双版本、顺序互换、班型区分） |
| A2 098 类别穿透 | 离线回放通过 | 新增 `verification/entities.py`：按类型后缀切分连续无标点名录，保留“与”连接词，逐项判别示范中心/实验室；接入 `conditions.compare_categories` | 真实 098 原文（无标点、正常分段、正确三中心）三种形式 |
| A3 035 真实例外 | 离线回放通过 | `conditions.signature_branches` 解析“第一导师为X的情形……署名单位必须/可为Y”；无适用条件的全称结论**阻断**（`CONDITION_CONTRADICTED`），条件正确的分支通过；标记词例外仍只提示 | 真实 2026-09-14 补充说明；本校导师/合作单位导师/未说明归属三种输入 |
| A4 041 对象与版本 | 代码接入（对象与版本已回放） | `organization.yaml` 增研究生专业（农业工程/农业工程与信息技术/工业工程与管理）与 `directions`；`attached_span` 解析“2025—2026级”为单一区间；培养方案比较题不再增设“定义区别”单元 | 真实 041 问题分解为 2 对象 × 2 方面，版本 `2026` 与 `2025-2026` |
| A5 泛称当完整回答 | 离线回放通过 | `coverage._claim_supports_unit` 增加内容核对：清单题需课程项、数字题需数值、流程题需步骤；flow 步骤须匹配对象与事项；有效 claim_ids 写回 | “两个专业均设置了核心课程”不再算已回答；无关 flow 步骤不支撑 |
| A6 矛盾未消除/提示影响完整性 | 离线回放通过 | 合并改为全量、顺序无关的两遍处理：正面结论可撤销同事项“未收录/未列出”；note 分 `duplicate/cross_version/conflict/stale_missing`；仅实质缺项影响完整性，冲突只展示 | 真实 039、084 正反顺序；“其余各系主任未列出”被撤销 |
| A7 429 额度误判 | 离线回放通过 | `classify_error` 先解析结构化 error.code/type，429+insufficient_quota → `quota` 不重试；Retry-After 支持 HTTP 日期；真实 Retry-After 不被 backoff_cap 截短，超出重试预算即停止；`attempt_log()`/`last_recovered` 记录尝试链；advisor 节点 trace 写 `provider_error` | 429 配额、429 限流、日期格式、预算终止、恢复记录 |
| R3 对象证据名额 | 离线回放通过 | `planner._protect_unit_queries`：未被查询覆盖的单元各补一条定向查询（总数 ≤6）；`retriever._select` 比较题每单元保留 2 条证据，受保护名额不被总上限截断 | `test_r3_r4_slots_and_repair.py`（2 对象 × 2 方面均有证据） |
| R4 逐单元补查 | 离线回放通过 | `graph.supplement` 改为最多两轮（3 条 + 2 条），按单元生成补查查询，轮次计数与停止条件显式 | 二轮补查后第三轮停止；已核实内容不被替换 |
| R5 模块合计层级 | 离线回放通过 | 新增 `verification/aggregates.py`：分别解析毕业总学分与模块学分，模块值被写成总学分即阻断（`aggregate_level`） | 37 学分冒充 163 学分被拒；正确总数与“选修课程为37学分”不误报 |
| R8 trace 全链路 | 离线回放通过 | `as_dict()` 含 kind/status/retryable/retry_after/attempts；advisor、planner（`last_provider_error`）、coverage（`error_kind`/`error_attempts`）三个节点写入 trace | `test_r3_r4_slots_and_repair.py`、`test_r8_provider_errors.py` |
| 线上小批试跑 | 未做（需真实模型配额） | 需真实模型重跑 001/020/028/035/039/041/049/064/065/098 并人工评判；上一轮观测到 `HTTP 429 user active session limit (200)`，需等窗口恢复 | — |

## 复核指出并已修正的旧测试预期

| 旧预期 | 现预期 |
| --- | --- |
| 同文档冲突按引用数/先后择一 | 冲突并列保留 + 核实提示（`test_true_conflict_keeps_both_statements...`） |
| 错误全称例外仍 PASS | 035 全称断言阻断；只有标记词例外为提示 |
| 泛称覆盖算已回答 | 无课程清单不算已答（`test_r4_coverage_binding.py`） |
| 负面结论只在后面出现时撤销 | 顺序无关撤销（039/084 正反顺序） |

## 验证记录

- `pytest -q`：833 passed、2 warnings；无失败用例。
- 真实回放文件：`tests/unit/test_real_replay_a1_a6.py`，文本逐字来自历史答案与证据块。
- 018 类“未收录”表述仅在确无正面结论时保留（`test_missing_statement_survives_when_nothing_covers_it`）。
- 线上第一批 10 题（2026-10-29，`data/eval/answer_quality/live_batch1_20261029/batch1_judgement.md`）：
  complete 6、partial 3、failed 1。001/039/041/064/065/028 判定 complete；
  020（毕业设计细节缺失）、035（学术不端缺失）、098（三个示范中心未列出）为 partial；
  049 首轮超时，根因是补查重置改写预算触发 LangGraph 20 步上限，已修复并在 420 秒预算下复现出答案。

## 线上试跑中追加修复

1. 覆盖反向核对：已答单元被模型误判缺项、并回显问题时，按确定性内容核对恢复为 answered 并写回 claim_ids。
2. 课程清单识别放宽到真实课程名（“数据结构/操作系统”），并保留“泛称不算已答”。
3. 确定性漏项清洗：“）数据结构”“重要”等碎片不再算漏项。
4. 补查不再重置 `revision_count`，消除“生成→核对”递归死循环（049）。

## 内容缺口修复（098/035/020）

按 `docs/design/2026-10-29-content-gap-fix-plan.md` 实施并线上复验：
098 已能列出三个示范中心且不再把实验室计入；035 会明写“学术不端未做明确规定”
（原文确无该条款）；020 仍缺“毕业设计 6 学分/14 周/第 8 学期”，原因是
`十、培养计划进程表` 的毕业设计行没有被切分进库，属于入库解析问题，不是判定问题。

## 仍未完成（需下一轮）

1. 020 的毕业设计行需要补进 `十、培养计划进程表` 的解析结果；098/035 已修复。
2. 049 的作答时延仍需收敛到 120 秒预算内。
3. 线上验收通过前不得以 818 项离线测试通过宣称 100 题质量问题已修复；第一批仍有 3 题 partial、1 题 failed。
4. 100 题历史答案与人工判定保持原样，新结论只写入新文件/新字段。