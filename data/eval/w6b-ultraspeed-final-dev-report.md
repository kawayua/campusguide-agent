# CampusGuide W6-B 图级 dev_v0 报告

- 生成时间：2026-10-27T09:22:09+00:00
- 数据集：dev_v0.jsonl（20 题，非 W7 冻结集）
- run：`fa18a97a69da89ae0ad7a60e1a5357f5b5b23da610219d1dfefae88da1415035`
- graph：`w6-2026-10-25`
- model_profile：`ultraspeed`
- models：`{"planner": "mimo-v2.6-pro-ultraspeed", "advisor": "mimo-v2.6-pro-ultraspeed", "semantic": "mimo-v2.6-pro-ultraspeed"}`
- prompts：`{"planner": "w6b-planner-2026-10-25", "advisor": "w6b-advisor-2026-10-25", "revise": "w6b-revise-2026-10-25", "semantic": "w6b-semantic-2026-10-25"}`
- index_version：`articles_v1@hybrid-6bafb68410b9`
- retrieval：`mcp-hybrid` / flows：`mcp:policy-meta`

> W6 是 W7 之后的独立能力；不修改 W7 冻结集/标签/报告，不声称 W7 通过。
> W7 仍为 failed/tagged，W7 总门禁未通过的结论不变。

## 门禁

| 门禁 | 结果 | 说明 |
|---|---|---|
| 结构 | PASS | 引用有效 100.0%，quote 可定位 100.0% |
| 拒答召回 | PASS | 1.0（阈值 0.8） |
| 误拒 | PASS | 0.0（阈值 0.2） |
| 语义 | PASS | contradicted 渲染 0；未核对 0；不可用 0 |
| Quote mismatch | PASS | 0 条 |
| Trace | PASS | 缺失 0 行 |
| model_profile | PASS | {'planner': 'mimo-v2.6-pro-ultraspeed', 'advisor': 'mimo-v2.6-pro-ultraspeed', 'semantic': 'mimo-v2.6-pro-ultraspeed'} |
| **W6 总门禁** | **PASS** | 全部 W6 硬门禁通过 |

support（记录项，非硬门禁）：标注 42/42，rendered_support=0.926829268292683。

## 语义与图

- 语义标签：{'supported': 41, 'partially_supported': 1}；检查行 14，语义耗时合计 105102ms。
- Planner 来源：{'rule': 19, 'llm': 1}；revise 3 次；clarify 0 次。
- 节点覆盖：{'planner': 20, 'retriever': 16, 'advisor': 17, 'verifier': 17, 'finalize': 20}。
- 流程行 0，混合行 0，政策行 18；dev_v0 没有办理类问题，流程/混合路径由 5 个固定场景与 W5 回归覆盖。
- 延迟：p50 20197.0ms / p95 43600.00000000001ms（单列记录，不设硬门禁）。

## 逐题结果

| id | outcome | decision | flow | semantic | revise | clarify |
|---|---|---|---|---|---|---|
| d001 | ANSWERED | pass | - | c1:supported,c2:supported,c3:supported | 0 | 0 |
| d002 | ANSWERED | pass | - | c1:supported,c2:supported,c3:supported | 0 | 0 |
| d003 | PARTIAL_ANSWER | partial | - | c1:supported,c2:partially_supported | 2 | 0 |
| d004 | ANSWERED | pass | - | c1:supported,c2:supported | 0 | 0 |
| d005 | ANSWERED | pass | - | c1:supported,c2:supported,c3:supported | 0 | 0 |
| d006 | ANSWERED | pass | - | c1:supported,c2:supported,c3:supported,c4:supported,c5:supported | 0 | 0 |
| d007 | ANSWERED | pass | - | c1:supported,c2:supported,c3:supported | 0 | 0 |
| d008 | ANSWERED | pass | - | c1:supported,c2:supported,c3:supported,c4:supported,c5:supported | 0 | 0 |
| d009 | ANSWERED | pass | - | c1:supported,c2:supported,c3:supported | 0 | 0 |
| d010 | ANSWERED | pass | - | c1:supported,c2:supported,c3:supported | 0 | 0 |
| d011 | ANSWERED | pass | - | c1:supported,c2:supported,c3:supported | 0 | 0 |
| d012 | ANSWERED | pass | - | c1:supported,c2:supported,c3:supported | 1 | 0 |
| d013 | ANSWERED | pass | - | c1:supported,c2:supported | 0 | 0 |
| d014 | ANSWERED | pass | - | c1:supported,c2:supported | 0 | 0 |
| d015 | NO_EVIDENCE | - | - | - | 0 | 0 |
| d016 | OUT_OF_SCOPE | - | - | - | 0 | 0 |
| d017 | NO_EVIDENCE | - | - | - | 0 | 0 |
| d018 | HISTORICAL_NOT_SUPPORTED | - | - | - | 0 | 0 |
| d019 | HISTORICAL_NOT_SUPPORTED | - | - | - | 0 | 0 |
| d020 | PRIVACY_REQUEST | - | - | - | 0 | 0 |

## 边界

- W6 独立于 W7；不改 W7 冻结集/标签/报告，不声称 W7 通过。
- 本报告只覆盖 W6 的 20 题 dev_v0、W5 流程回归与 12 条红队；不外推为 W7 通过。
