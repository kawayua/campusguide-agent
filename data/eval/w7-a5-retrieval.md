# W7 A5 检索消融

- 数据集：`test_v1.jsonl`（文件 `208cf3c5f032…`）
- 冻结测试集 hash：`2a8d0e122362…`
- 可答题：40 道
- git：`3a68f6dd8efc60b793ea49021fccb79a0100e82f`（dirty=True）
- 索引：`articles_v1@hybrid-0cb2167de3fe`

## 指标（Recall@8 / MRR）

| 模式 | article Recall@8 | article MRR | section Recall@8 | section MRR | overall Recall@8 | overall MRR |
|---|---:|---:|---:|---:|---:|---:|
| `dense`（dense-only） | 0.957 | 0.824 | 1.000 | 0.867 | 0.963 | 0.829 |
| `hybrid_rrf`（dense+sparse RRF） | 1.000 | 0.877 | 1.000 | 0.767 | 1.000 | 0.863 |
| `hybrid_rerank`（RRF + bge-reranker-v2-m3） | 1.000 | 0.967 | 1.000 | 0.900 | 1.000 | 0.958 |

## 延迟

| 模式 | p50 | p95 | rerank p50 | rerank p95 |
|---|---:|---:|---:|---:|
| `dense` | 96.5 ms | 248.2 ms | n/a | n/a |
| `hybrid_rrf` | 169.0 ms | 412.4 ms | n/a | n/a |
| `hybrid_rerank` | 1177.0 ms | 1365.4 ms | 971.5 ms | 1067.9 ms |

## 说明

- A5 只比较检索层；三种模式使用同一冻结题、同一索引、同一过滤配置。
- dense-only 不做 profile 查询扩展；hybrid 模式保留 profile 扩展与过滤。
- A1 固定分块、A3 流程 DAG 为 N/A。
