# W8 真实 UI 页面级 smoke

- 时间：2026-10-27T13:19:52+08:00
- URL：http://127.0.0.1:7862/（1440×900）
- 页面 profile：`演示 · ultraspeed`
- 实际模型：profile=`vpro`；models=`{'planner': 'mimo-v2.6-pro', 'advisor': 'mimo-v2.6-pro', 'semantic': 'mimo-v2.6-pro'}`
- 请求上限：240s（默认 120s；超时 fail-closed）
- 核心场景：**5/5 PASS**
- 拒答：**2/2 PASS**
- 全部场景：**6/6 PASS**
- 单轮 p50：1461ms；p95：138782ms；目标 p50≤15000ms / p95≤45000ms：未达标

| 场景 | 轮次 | 最终 outcome | 耗时 | checklist | 引用 | clarify | 结果 |
|---|---:|---|---:|---:|---:|---:|---|
| 政策问答：国奖条件 | 1 | ANSWERED | 138782ms | 0 | 5 | 0 | PASS |
| 流程 closed：国奖怎么申请 | 1 | ANSWERED | 1461ms | 5 | 7 | 0 | PASS |
| 多轮澄清：在读证明 | 4 | PARTIAL_ANSWER | 5191ms | 4 | 9 | 3 | PASS |
| 混合问题：怎么申请 + 金额 | 1 | ANSWERED | 104943ms | 5 | 10 | 0 | PASS |
| 隐私拒答：绩点与排名 | 1 | PRIVACY_REQUEST | 1689ms | 0 | 0 | 0 | PASS |
| 校外拒答：隔壁学校奖学金 | 1 | OUT_OF_SCOPE | 1961ms | 0 | 0 | 0 | PASS |

## 失败原因

- 无

## 边界

- 本报告只验证真实 Gradio 页面和固定场景；不替代 W6 20 题 / W7 冻结集报告。
- 页面展示档与真实模型分开记录；不把本页延迟写成 ultraspeed 指标，也不混入 W6 指标。
- 应用默认单轮上限 120s；页面级 smoke 若为容纳 vPro 长尾调大上限，会在上方单独记录。
- 页面级 smoke 不检查答案支持度，人工 support 标注仍以既有评测报告为准。
