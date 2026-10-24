# CampusGuide W7 冻结测试集报告

- 数据集：`test_v1.jsonl`（文件 `208cf3c5f032…`）
- 冻结测试集 hash：`2a8d0e122362…`
- run_sha256：`75c61f800dd49a0e717492dad1755a18b99014906d5dd8aa5abca506a9aad396`
- git：`75f10a442a89731b0b757f38d11cbca88f237555`（dirty=True）
- 模型：mimo-v2.6-pro-ultraspeed
- prompt：w6a2.4-2026-10-24
- 索引：17441d486b2fb788456b875665c695d9bafa2cc4a9bef1c974c91b19bd24e7a2
- 标注：yaukin

## 门禁

| 门禁 | 结果 | 数值 | 目标 |
|---|---|---:|---:|
| 结构门禁 | 通过 | 100% | — |
| 引用支持率 | 通过 | 1.000 | 0.95 |
| 可回答题正确率 | 通过 | 0.875 | 0.85 |
| 拒答召回 | 通过 | 1.000 | 0.90 |
| 误拒率 | 通过 | 0.000 | 0.10 |
| 版本敏感题正确率 | 通过 | 1.000 | 0.90 |
| article Recall@8 | 通过 | 1.000 | 0.90 |
| p50 延迟 | 未通过 | 7368 ms | 6000.00 |
| p95 延迟 | 未通过 | 21385 ms | 10000.00 |

**W7 总门禁：未通过**

## 检索与拒答

| 指标 | 数值 |
|---|---:|
| article Recall@8 | 1.000 |
| article MRR | 0.967 |
| section Recall@8 | 1.000 |
| section MRR | 0.900 |
| 拒答召回 | 1.000 |
| 误拒率 | 0.000 |
| 原因码正确率 | 1.000 |

## 支持与正确性

| 指标 | 数值 |
|---|---:|
| draft support | 1.000 |
| rendered support | 1.000 |
| answer correctness | 0.875 |
| version-sensitive | 1.000 |
| p50 / p95 | 7367.5 / 21384.7 ms |

## 分类正确率

| 类别 | correct/可答 | partial | incorrect | 正确率 |
|---|---:|---:|---:|---:|
| Overall | 35/40 | 5 | 0 | 0.875 |
| single_article | 24/25 | 1 | 0 | 0.960 |
| multi_article | 6/10 | 4 | 0 | 0.600 |
| section | 5/5 | 0 | 0 | 1.000 |
| version_sensitive | 5/5 | 0 | 0 | 1.000 |

## 拒答混淆矩阵

| 实际 \ 系统 | 渲染答案 | 明确拒答 |
|---|---:|---:|
| 可回答（40） | 40 | 0 |
| 不可回答（10） | 0 | 10 |

## 标注样本与 Wilson 95% 区间

| 指标 | 成功/样本 | 比率 | 95% CI |
|---|---:|---:|---:|
| rendered support | 133/133 | 1.000 | 0.972–1.000 |
| 可回答题正确 | 35/40 | 0.875 | 0.739–0.945 |
| 版本敏感 | 5/5 | 1.000 | 0.566–1.000 |
| 拒答召回 | 10/10 | 1.000 | 0.723–1.000 |
| 误拒 | 0/40 | 0.000 | 0.000–0.088 |

- support 排除 0 条未进入统计的 draft（共 133 条）。
- answer 明细：correct 35 / partial 5 / incorrect 0；不可答题 10/10 判断正确。

## 错误归因（设计 12.7）

| 主因 | 数量 | 题号 |
|---|---:|---|
| UNSUPPORTED_CLAIM | 5 | test-003, test-029, test-031, test-033, test-035 |

- 其余类别为 0：RETRIEVAL_MISS, WRONG_VERSION, WRONG_AUDIENCE, CITATION_MISMATCH, INCOMPLETE_FLOW, OVER_REFUSAL, UNDER_REFUSAL, TOOL_FAILURE, LATENCY；INCOMPLETE_FLOW 为 N/A（W5 未实现）。

### UNSUPPORTED_CLAIM
- test-003: 缺 required fact「助推学校“双一流”建设」；质量数量双提升正确
- test-029: 缺「合法性审查」与「合同专用章」；原则部分正确
- test-031: 缺 required fact「纳入大学生人才培养方案」；出发点与原则正确
- test-033: 缺 required fact「成建制培养」；连续三年配置与 15-30 个正确
- test-035: 缺「下一学期开学初到我校办理报到注册」；条件与一式4份正确

- 延迟：p50/p95 未达门禁属于系统级延迟限制，不计入单题错误主因。

## 消融

### A5 检索

| 模式 | overall Recall@8 | overall MRR | article MRR | section MRR | p95 |
|---|---:|---:|---:|---:|---:|
| `dense` | 0.963 | 0.829 | 0.824 | 0.867 | 80.1 ms |
| `hybrid_rrf` | 1.000 | 0.863 | 0.877 | 0.767 | 123.0 ms |
| `hybrid_rerank` | 1.000 | 0.958 | 0.967 | 0.900 | 1050.0 ms |

### A4 profile 过滤

| 变体 | 有 retrieved 错配的题 | retrieved 错配/总数 | 误拒率 |
|---|---:|---:|---:|
| baseline（有 profile） | 0/5 | 0/35 | 0.000 |
| A4（无 profile 元数据） | 1/5 | 7/34 | 0.000 |

### A2 Verifier

| 变体 | supported/已标 | support rate | unsupported rate | 误拒率 |
|---|---:|---:|---:|---:|
| baseline（有 Verifier） | 133/133（共 133） | 1.000 | 0.000 | 0.000 |
| A2（无 Verifier） | 137/138（共 138） | 0.993 | 0.000 | 0.000 |

- A1 固定分块、A3 流程 DAG 为 N/A。

## 限制

- 50 题冻结集只作方向性证据；比例指标给 Wilson 95% 区间。
- A1 固定分块、A3 流程 DAG 未执行（缺少相应实现），报告标 N/A。
- support 与 answer correctness 必须是人工标注；AI 预标注不计入。
- 单标注人：yaukin；未执行 20% 第二人复核/隔天盲审，按设计披露。
- 人工修改处数（改事实/改条件/删无依据句/补引用）未分项记录，标 N/A。
- token/成本：当前 run row 未落盘 token，且无统一单价配置，标 N/A。
- w7_remediation4 对 27 条 support、11 题 answer 完全一致项沿用上一轮标签；设计原要求旧 run 标签不复用，此为记录在案的偏差。
- p50/p95 实际为 7368/21385 ms，未达 6s/10s；按设计记录实际值，不删题、不改门禁。
