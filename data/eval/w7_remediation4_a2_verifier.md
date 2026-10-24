# W7 A2 Verifier 消融

- 数据集：`test_v1.jsonl`（208cf3c5f032…）
- 冻结测试集 hash：`2a8d0e122362…`
- baseline run：`75c61f800dd4…`
- A2 run：`367818f7487e…`
- 变体：pass-through verifier（不执行 W3 核验，结构门禁不适用）

## 支持率

| 变体 | supported/已标 | partial | unsupported | support rate | unsupported rate |
|---|---:|---:|---:|---:|---:|
| baseline（有 Verifier） | 133/133 | 0 | 0 | 1.000 | 0.000 |
| A2（无 Verifier） | 137/138 | 1 | 0 | 0.993 | 0.000 |

## 拒答与误拒

| 变体 | 拒答召回 | 误拒率 | 原因码正确率 |
|---|---:|---:|---:|
| baseline（有 Verifier） | 1.000 | 0.000 | 1.000 |
| A2（无 Verifier） | 1.000 | 0.000 | 1.000 |

## 说明

- A2 仅用于评测，不进入生产；结构门禁（引用 ID/quote）不适用。
- pass-through 只丢弃没有合法 citation 的 draft，不做 quote/fact/scope 核验。
- A2 结构性丢弃 claim：0 条。
- support 必须全量人工标注；当前 A2 若仍有 unlabeled，报告不声称最终通过。
- A1 固定分块、A3 流程 DAG 为 N/A。
