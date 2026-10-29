# R1–R9 修复建议实施记录（2026-10-28）

依据 `docs/reports/CampusGuide_100题逐题错误分析与修改建议_20261028.md`
（100 题逐题报告）的 R1–R9 建议，按报告给出的执行顺序落地。
每一步都先补确定性护栏与回归测试，再改主链路，保证"不改历史判定、不放松校验"。

离线回归：`& ".venv\Scripts\python.exe" -m pytest -q` → **780 passed**（基线 708 + 新增 72）。
A1–A7 复核整改后的最新状态与结果见
`docs/design/2026-10-28-answer-quality-a1-a7-remediation.md`（802 passed）。

## 状态总表

| 项 | 状态 | 主要改动 | 对应测试 |
| --- | --- | --- | --- |
| R1 条件方向语义校验 | 已落地 | `verification/conditions.py`（新：阈值方向、义务方向、角色、类别、例外）、`agents/verifier.py` 新增 `CONDITION_CONTRADICTED`（阻断）与 `EXCEPTION_DROPPED`（提示）、`verification/semantic.py` 提示词补条件方向规则 | `test_r1_conditions.py`（15） |
| R2 提问者身份与适用对象分离 | 已落地 | `agents/planner.py::_audience_from_text`、`domain/audience_match.py`（新）、`verification/scope.py`、`retrieval/filters.py`、`retrieval/hybrid.py` | `test_r2_audience.py`（13） |
| R3 层次/班型/版本/章节检索 | 已落地 | `configs/organization.yaml`（class_types）、`config.py::ClassType`、`retrieval/profile_expand.py`、`agents/question_units.py`、`retrieval/corpus_views.py` | `test_r3_hierarchy.py`（8） |
| R4 按所问要点核实覆盖 | 已落地 | `agents/coverage.py::_claim_supports_unit`（对象×方面绑定，泛称断言不算覆盖） | `test_r4_coverage_binding.py`（4） |
| R5 课程清单与表格结构 | 已落地 | `retrieval/source_spans.py`（表头/学分/学时/学期/模块/支撑矩阵）、`verification/coverage_items.py`（中文句号、说明句、剔除矩阵行） | `test_r5_course_tables.py`（6） |
| R6 答复合并去重与矛盾检查 | 已落地 | `qa/merge.py`（新）、`agents/graph.py` finalize 接入（去重、表格残片、同事项冲突、"未收录"与规则并存） | `test_r6_r7_merge_and_boundaries.py`（7） |
| R7 语料缺失/检索遗漏/政策冲突 | 已落地 | `agents/graph.py::boundary_notes`；学校与学院冲突并列保留并提示核实（不擅自择一） | 同上 |
| R8 供应商错误分类与可恢复运行 | 已落地 | `agents/providers.py`（rate_limit/timeout/auth/quota/parse/network/server/circuit_open、Retry-After 与指数退避、熔断与恢复、`as_dict()`） | `test_r8_provider_errors.py`（14） |
| R9 评测基准与统计可信度 | 已落地 | `gold_v1.jsonl` 必要/可选检查项 + `review_v2`、`scripts/freeze_100_manifest.py`、`scripts/apply_gold_review_v2.py`、`scripts/recount_selected_100.py`、两个报告生成器（312 条引用 / 统计中位数 / 首试与最终成功分开）、`scripts/audit_answer_quality.py`（可选检查项不降判定） | `test_r9_eval_stats.py`（5） |

## 关键行为

### R1（P0：001、098）
- 阈值方向逐字对照：`30分钟后`（after）与 `30分钟内`（within）互斥；
  `开学前两周内` 与 `开学后两周内` 判为 `threshold_phase`；`以上/以下`、`前/后` 同理。
- 只有**同一动作**下的阈值才互相比对（`入场` vs `离场` 不会误判）。
- 义务方向：`禁止` 与 `可以` 对同一动作互斥。
- 实体类别：`示范中心：A、B、C` 之后的 `另设/此外` 条目不得继承该类别（098）。
- 角色对调：第一/第二导师、校内/校外导师、第一/通信作者。
- 例外遗漏（035）：原文有“联合培养研究生……不适用”时，写成面向全体研究生的通则
  只给 `EXCEPTION_DROPPED` 提示，不丢弃结论（避免把合法概括误杀）。
- 命中即给 `CONDITION_CONTRADICTED`（BLOCKING），走原有 revise→丢弃流程；
  001 的错误条件被拒，替考开除学籍的正确结论保留。

### R2（028、043、059、060、096；007 反向）
- 角色词只作为事项对象时（招生、复试、选聘、推免、学位授予、答辩等）不再据此硬过滤检索。
- 明确自称（"我是一名研究生"）或"本人适用的规则"才作为提问者身份。
- 本科/研究生与硕士/博士、教师/教职工按父子关系互相匹配（检索、scope、重排警告共用一套）。

### R3（005、030、031、041、061、064、071、088、092）
- `organization.yaml` 新增 `class_types`（丁颖创新班等），专业可标注 `level`。
- 课程类问题按层次排除另一层次的培养方案；班型问题要求方案标题/章节含该班型；
  分流/补充/修订说明在未询问时降权，不替代创新班完整方案。

### R5（020 等）
- 表格行保留表头、学分、学时、学期、模块；`支撑矩阵` 行标记 `is_matrix` 并排除在课程清单之外
  （617017 毕业论文/设计只从进程表取）。
- 中文句号切分课程清单，剔除"最为核心的5至8门…"说明；`10.5学分` 不归一为 `105`。

### R8
- 429 → `rate_limit`，读 `Retry-After`；超时/网络/5xx 有界重试；401/403/额度/解析失败不重试；
  连续失败达阈值熔断，冷却后自动恢复；`ChatProviderError.as_dict()` 可直接写入 trace。
- 覆盖核对不可用时单元状态为 `verification_failed`，走"核对服务不可用"措辞，
  不会改写为"没有检索到可引用的现行条款"。

## 仍待补的部分（不影响已通过的用例）

1. **R3 比较题证据名额**：041 这类"两个对象各一方面"的比对已由 R4 绑定负责，但
   "按对象保护证据名额"的预算级约束尚未单独实现（当前靠 coverage 修复轮）。
2. **R4 流程补齐**：覆盖缺项时仍只做一轮补充检索（最多 3 个查询），未按"对象×方面"逐项补查。
3. **R5 选修合计层级**：`模块学分`逐层相加的反例已能识别课程行，但"合计不得被扁平相加"的
   数值层级校验尚未写成独立断言（依赖 facts 层的数值覆盖）。
4. **R8 trace 贯通**：`kind/attempts/retry_after` 已可序列化，尚未在 graph trace 中统一落字段。
5. **模型对照 A/B**：报告最后一步（受控 A/B 比较）需要真实模型调用配额，未在本轮执行。

## 复现

```
& ".venv\Scripts\python.exe" -m pytest -q                      # 779 passed
& ".venv\Scripts\python.exe" scripts\freeze_100_manifest.py     # 冻结 100 题会话
& ".venv\Scripts\python.exe" scripts\recount_selected_100.py    # 312 条引用 / 2615.427 秒 / 中位数 23.135
& "C:\Users\y'y\.dsh\dsh-runtimes\dsh-primary-runtime\dependencies\python\python.exe" scripts\build_merged_100_report.py
```