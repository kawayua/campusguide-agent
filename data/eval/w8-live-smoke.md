# W8 真实图 5 场景 live smoke

- 时间：2026-10-27T12:31:58+08:00
- as_of：2026-10-25
- profile：`vpro`
- index：`articles_v1@hybrid-6bafb68410b9`
- 结果：**5/5 PASS**
- 单轮 p50：46ms；p95：160265ms

| 场景 | 轮次 | 最终 outcome | progress | 耗时 | 结果 |
|---|---:|---|---|---:|---|
| 政策问答：国奖条件 | 1 | ANSWERED | 规划 → 检索 → 生成 → 核对 → 整理 | 160265ms | PASS |
| 隐私拒答：绩点与排名 | 1 | PRIVACY_REQUEST | 规划 → 整理 | 30ms | PASS |
| 流程 closed：国奖怎么申请 | 1 | ANSWERED | 规划 → 流程 → 整理 | 53ms | PASS |
| 多轮澄清：在读证明 | 4 | PARTIAL_ANSWER | 规划 → 流程 → 整理 | 59ms | PASS |
| 混合问题：怎么申请 + 金额 | 1 | ANSWERED | 规划 → 流程 → 检索 → 生成 → 核对 → 生成 → 核对 → 整理 | 72829ms | PASS |

## 失败原因

- 无

## 边界

- 本 smoke 只验证 `handle_stream` 与 5 个固定场景，不替代 W6 20 题/W7 冻结集报告。
- 不修改 W6 图、W7 标签/报告；耗时按单轮记录，不混入 W6 指标。
