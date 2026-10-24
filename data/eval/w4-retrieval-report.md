# W4 检索评测报告

- 数据集：`dev_v0.jsonl`（e144b8001333…）
- 生成时间：2026-10-24T06:20:58+00:00
- git：`26afddfba1fc6c35f40a4791c3995d591ee49374`（dirty=False）
- 索引版本：`articles_v1@hybrid-0cb2167de3fe`
- 向量 manifest：`17441d486b2fb788456b875665c695d9bafa2cc4a9bef1c974c91b19bd24e7a2`

## 指标（Recall@8 / MRR）

| 模式 | 说明 | article Recall@8 | article MRR | section Recall@8 | section MRR | overall Recall@8 | overall MRR |
|---|---|---:|---:|---:|---:|---:|---:|
| `dense` | dense-only（W3 基线） | 1.000 | 1.000 | 1.000 | 0.333 | 1.000 | 0.905 |
| `hybrid_rrf` | dense+sparse RRF | 1.000 | 0.822 | 1.000 | 1.000 | 1.000 | 0.848 |
| `hybrid_rerank` | RRF + bge-reranker-v2-m3 | 1.000 | 0.958 | 1.000 | 0.750 | 1.000 | 0.929 |

## 延迟

| 模式 | p50 | p95 | rerank p50 | rerank p95 |
|---|---:|---:|---:|---:|
| `dense` | 44.5 ms | 139.1 ms | n/a | n/a |
| `hybrid_rrf` | 80.0 ms | 224.9 ms | n/a | n/a |
| `hybrid_rerank` | 1224.0 ms | 1367.2 ms | 1072.5 ms | 1196.3 ms |

## 口径与限制

- 只评估检索层，不调用 LLM；Recall@8/MRR 基于 dev_v0 的 `expected_article_ids`。
- 非可答题（无 expected ids）不计入检索指标。
- dev_v0 不是冻结测试集；W7 才报冻结区间。
- W4 不宣称答案质量提升，除非有 freeze 的评测数据。
