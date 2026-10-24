# CampusGuide W7 冻结测试集报告

- 数据集：`test_v1.jsonl`（文件 `8fb21ebebcea…`）
- 冻结测试集 hash：`ed451233c9fe…`
- run_sha256：`f9bd754411e1b1a9b173ce80c4cd4561e3a6f45f14fe3e669696b44e64086243`
- git：`57f91fd8d8fe36d0975ac650d2c98f42cd1bbcd1`（dirty=True）
- 模型：mimo-v2.6-pro-ultraspeed
- prompt：w6a2-2026-10-24
- 索引：17441d486b2fb788456b875665c695d9bafa2cc4a9bef1c974c91b19bd24e7a2

## 门禁

| 门禁 | 结果 | 数值 | 目标 |
|---|---|---:|---:|
| 结构门禁 | 通过 | 100% | — |
| 引用支持率 | 未通过 | n/a | 0.95 |
| 可回答题正确率 | 未通过 | n/a | 0.85 |
| 拒答召回 | 通过 | 0.900 | 0.90 |
| 误拒率 | 未通过 | 0.125 | 0.10 |
| 版本敏感题正确率 | 未通过 | n/a | 0.90 |
| article Recall@8 | 通过 | 0.971 | 0.90 |
| p95 延迟 | 未通过 | 25944 ms | 10000.00 |

**W7 总门禁：未通过**

## 检索与拒答

| 指标 | 数值 |
|---|---:|
| article Recall@8 | 0.971 |
| article MRR | 0.938 |
| section Recall@8 | 0.800 |
| section MRR | 0.640 |
| 拒答召回 | 0.900 |
| 误拒率 | 0.125 |
| 原因码正确率 | 0.800 |

## 支持与正确性

| 指标 | 数值 |
|---|---:|
| draft support | n/a |
| rendered support | n/a |
| answer correctness | n/a |
| version-sensitive | n/a |
| p50 / p95 | 8441.0 / 25944.3 ms |

## 限制

- 50 题冻结集只作方向性证据；比例指标给 Wilson 95% 区间。
- A1 固定分块、A3 流程 DAG 未执行（缺少相应实现），报告标 N/A。
- support 与 answer correctness 必须是人工标注；AI 预标注不计入。
