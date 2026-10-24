# CampusGuide W7 冻结测试集报告

- 数据集：`test_v1.jsonl`（文件 `208cf3c5f032…`）
- 冻结测试集 hash：`2a8d0e122362…`
- run_sha256：`6b9be80811af1287dec87c6da7d6455a7551eceb9c62fb243fce0323bfaa7b14`
- git：`f215e606e5d41c78f0435c52c0d517ac37413e1c`（dirty=True）
- 模型：mimo-v2.6-pro-ultraspeed
- prompt：w6a2.3-2026-10-24
- 索引：17441d486b2fb788456b875665c695d9bafa2cc4a9bef1c974c91b19bd24e7a2
- 标注：yaukin

## 门禁

| 门禁 | 结果 | 数值 | 目标 |
|---|---|---:|---:|
| 结构门禁 | 通过 | 100% | — |
| 引用支持率 | 通过 | 1.000 | 0.95 |
| 可回答题正确率 | 通过 | 0.875 | 0.85 |
| 拒答召回 | 通过 | 1.000 | 0.90 |
| 误拒率 | 通过 | 0.025 | 0.10 |
| 版本敏感题正确率 | 通过 | 1.000 | 0.90 |
| article Recall@8 | 通过 | 1.000 | 0.90 |
| p50 延迟 | 未通过 | 12478 ms | 6000.00 |
| p95 延迟 | 未通过 | 31544 ms | 10000.00 |

**W7 总门禁：未通过**

## 检索与拒答

| 指标 | 数值 |
|---|---:|
| article Recall@8 | 1.000 |
| article MRR | 0.967 |
| section Recall@8 | 1.000 |
| section MRR | 0.900 |
| 拒答召回 | 1.000 |
| 误拒率 | 0.025 |
| 原因码正确率 | 1.000 |

## 支持与正确性

| 指标 | 数值 |
|---|---:|
| draft support | 1.000 |
| rendered support | 1.000 |
| answer correctness | 0.875 |
| version-sensitive | 1.000 |
| p50 / p95 | 12478.5 / 31544.2 ms |

## 分类正确率

| 类别 | correct/可答 | partial | incorrect | 正确率 |
|---|---:|---:|---:|---:|
| Overall | 35/40 | 4 | 1 | 0.875 |
| single_article | 23/25 | 1 | 1 | 0.920 |
| multi_article | 7/10 | 3 | 0 | 0.700 |
| section | 5/5 | 0 | 0 | 1.000 |
| version_sensitive | 5/5 | 0 | 0 | 1.000 |

## 拒答混淆矩阵

| 实际 \ 系统 | 渲染答案 | 明确拒答 |
|---|---:|---:|
| 可回答（40） | 39 | 1 |
| 不可回答（10） | 0 | 10 |

## 标注样本与 Wilson 95% 区间

| 指标 | 成功/样本 | 比率 | 95% CI |
|---|---:|---:|---:|
| rendered support | 134/134 | 1.000 | 0.972–1.000 |
| 可回答题正确 | 35/40 | 0.875 | 0.739–0.945 |
| 版本敏感 | 5/5 | 1.000 | 0.566–1.000 |
| 拒答召回 | 10/10 | 1.000 | 0.723–1.000 |
| 误拒 | 1/40 | 0.025 | 0.004–0.129 |

- support 排除 2 条未进入统计的 draft（共 137 条）。
- answer 明细：correct 35 / partial 4 / incorrect 1；不可答题 10/10 判断正确。

## 错误归因（设计 12.7）

| 主因 | 数量 | 题号 |
|---|---:|---|
| UNSUPPORTED_CLAIM | 4 | test-003, test-029, test-033, test-035 |
| OVER_REFUSAL | 1 | test-001 |

- 其余类别为 0：RETRIEVAL_MISS, WRONG_VERSION, WRONG_AUDIENCE, CITATION_MISMATCH, INCOMPLETE_FLOW, UNDER_REFUSAL, TOOL_FAILURE, LATENCY；INCOMPLETE_FLOW 为 N/A（W5 未实现）。

### UNSUPPORTED_CLAIM
- test-003: 缺 required fact「助推学校“双一流”建设」；质量数量双提升正确
- test-029: 缺 required fact「合同专用章」；原则与合法性审查正确
- test-033: 缺 required fact「成建制培养」；连续三年配置与 15-30 个正确
- test-035: 缺「一式4份」与「下一学期开学初办理报到注册」；转学条件部分正确

### OVER_REFUSAL
- test-001: 应答题但模型输出为空

- 延迟：p50/p95 未达门禁属于系统级延迟限制，不计入单题错误主因。

## 消融

### A5 检索

| 模式 | overall Recall@8 | overall MRR | article MRR | section MRR | p95 |
|---|---:|---:|---:|---:|---:|
| `dense` | 0.963 | 0.829 | 0.824 | 0.867 | 248.2 ms |
| `hybrid_rrf` | 1.000 | 0.863 | 0.877 | 0.767 | 412.4 ms |
| `hybrid_rerank` | 1.000 | 0.958 | 0.967 | 0.900 | 1365.4 ms |

### A4 profile 过滤

| 变体 | 有 retrieved 错配的题 | retrieved 错配/总数 | 误拒率 |
|---|---:|---:|---:|
| baseline（有 profile） | 0/5 | 0/35 | 0.000 |
| A4（无 profile 元数据） | 1/5 | 7/34 | 0.000 |

### A2 Verifier

| 变体 | supported/已标 | support rate | unsupported rate | 误拒率 |
|---|---:|---:|---:|---:|
| baseline（有 Verifier） | 133/135（共 136） | 0.985 | 0.000 | 0.000 |
| A2（无 Verifier） | 136/138（共 138） | 0.986 | 0.000 | 0.000 |

- 注意：a4=w6a2.1-2026-10-24, a2=w6a2.1-2026-10-24，与主结果 prompt `w6a2.3-2026-10-24` 不同；这些消融是历史结果，未随本轮 prompt 重跑。A5 为检索层消融，同索引/配置，不受 prompt 影响。

- A1 固定分块、A3 流程 DAG 为 N/A。

## 限制

- 50 题冻结集只作方向性证据；比例指标给 Wilson 95% 区间。
- A1 固定分块、A3 流程 DAG 未执行（缺少相应实现），报告标 N/A。
- support 与 answer correctness 必须是人工标注；AI 预标注不计入。
- 单标注人：yaukin；未执行 20% 第二人复核/隔天盲审，按设计披露。
- 人工修改处数（改事实/改条件/删无依据句/补引用）未分项记录，标 N/A。
- token/成本：当前 run row 未落盘 token，且无统一单价配置，标 N/A。
- w7_remediation3 对 28 条 support、12 题 answer 完全一致项沿用上一轮标签；设计原要求旧 run 标签不复用，此为记录在案的偏差。
- p50/p95 实际为 12478/31544 ms，未达 6s/10s；按设计记录实际值，不删题、不改门禁。
