# CampusGuide W6-A 开发集报告

- 数据集：`dev_v0.jsonl`（开发集 v0，**非冻结测试集**）
- 生成时间：2026-10-24T05:40:09+00:00
- run_sha256：`1fcc19a8573f9e7e524aaf47d83989d4966b898fd71f2694306d5b2616c184d4`
- git commit：`018ce43e54efbccf9b4be043053afcb0ed650bf4`（dirty=True）
- 模型：mimo-v2.6-pro-ultraspeed
- prompt：w6a-2026-10-23（max_tokens=3000）
- 向量 manifest：`17441d486b2fb788456b875665c695d9bafa2cc4a9bef1c974c91b19bd24e7a2`
- 检索：hybrid
- config hash：`c1016f0974b375b586327debe70fb06e0c565b8835bc00c89b9e2ab396879865`
- 延迟口径：warm（warmup 36119 ms；探针耗时单列）

## 门禁

| 门禁 | 结果 | 数值 |
|---|---|---:|
| 结构门禁 | 通过 | 引用 ID 100.0% / quote 100.0% |
| support 门禁 | 未通过 | rendered n/a |
| 拒答召回 | 通过 | 100.0% |
| 误拒率 | 通过 | 0.0% |
| W6-A 总门禁 | 未通过 | — |

**未通过原因：**

- 未完成人工 support 标注
- support 门禁未通过：存在未标注的 draft claim（含非人工标注）

## 汇总

| 指标 | 数值 |
|---|---:|
| 题数 | 20 |
| 可答题数 | 14 |
| 非可答题数 | 6 |
| outcomes | {'ANSWERED': 12, 'PARTIAL_ANSWER': 2, 'NO_EVIDENCE': 2, 'OUT_OF_SCOPE': 1, 'HISTORICAL_NOT_SUPPORTED': 2, 'PRIVACY_REQUEST': 1} |
| 引用 ID 有效率 | 100.0% |
| 编造 ID 数 | 0 |
| part_no 错误数 | 0 |
| quote 可定位率 | 100.0% |
| p50 延迟（warm，排除探针） | 8612 ms |
| p95 延迟（warm，排除探针） | 22448 ms |
| 探针次数 | 2 |
| 探针 p50 / p95 | 1143 ms / 1208 ms |

## support 标注

严格按引文级口径：只判断 quote 本身是否支撑 claim，不拿条款上下文补足。
unlabeled 不计入分母，但未完成人工标注时门禁不得通过。

| 指标 | draft | rendered |
|---|---:|---:|
| 总数 | 48 | 42 |
| 已标注 | 0 | 0 |
| unlabeled | 48 | 42 |
| supported | 0 | 0 |
| partial | 0 | 0 |
| unsupported | 0 | 0 |
| 支持率 | n/a | n/a |

- draft 支持率：n/a
- rendered 支持率：n/a
- 人工标注完整：否
- support 门禁：未通过（存在未标注的 draft claim（含非人工标注））

## 拒答混淆矩阵

| 预期 \ 是否渲染 | 渲染答案 | 未渲染答案 |
|---|---:|---:|
| 可答 | TP=14 | FN=0 |
| 非可答 | FP=0 | TN=6 |

- 拒答召回：100.0%
- 误拒率：0.0%
- partial 率：14.3%
- 原因码正确率：100.0%

## issue 分布

| code | scope | severity | count |
|---|---|---|---:|
| AUDIENCE_UNDETERMINED | evidence | warning | 14 |
| NO_VALID_CITATION | claim | blocking | 4 |
| QUOTE_MISMATCH | citation | warning | 4 |
| NUMBER_NOT_SUPPORTED | claim | blocking | 2 |

## 错误归因

| 主因 | 题数 |
|---|---:|
| PARTIAL_INCOMPLETE | 2 |

## 逐题结果

| ID | 类别 | 期望 | outcome | decision | partial | 渲染 claim | 丢弃 claim | 主因 | 延迟(ms) |
|---|---|---|---|---|---:|---:|---|---|---:|
| d001 | single_article | ANSWERED | ANSWERED | pass | 否 | 5 | — | — | 17814 |
| d002 | single_article | ANSWERED | ANSWERED | pass | 否 | 3 | — | — | 8281 |
| d003 | single_article | ANSWERED | ANSWERED | pass | 否 | 2 | — | — | 9912 |
| d004 | single_article | ANSWERED | ANSWERED | pass | 否 | 1 | — | — | 8742 |
| d005 | single_article | ANSWERED | PARTIAL_ANSWER | partial | 是 | 1 | c2, c3, c4, c5 | PARTIAL_INCOMPLETE | 23687 |
| d006 | single_article | ANSWERED | PARTIAL_ANSWER | partial | 是 | 2 | c3, c4 | PARTIAL_INCOMPLETE | 12491 |
| d007 | single_article | ANSWERED | ANSWERED | pass | 否 | 1 | — | — | 8481 |
| d008 | single_article | ANSWERED | ANSWERED | pass | 否 | 4 | — | — | 11280 |
| d009 | multi_article | ANSWERED | ANSWERED | pass | 否 | 5 | — | — | 15192 |
| d010 | multi_article | ANSWERED | ANSWERED | pass | 否 | 5 | — | — | 11083 |
| d011 | multi_article | ANSWERED | ANSWERED | pass | 否 | 5 | — | — | 22383 |
| d012 | multi_article | ANSWERED | ANSWERED | pass | 否 | 5 | — | — | 8343 |
| d013 | section | ANSWERED | ANSWERED | pass | 否 | 2 | — | — | 7127 |
| d014 | section | ANSWERED | ANSWERED | pass | 否 | 1 | — | — | 11117 |
| d015 | no_evidence | NO_EVIDENCE | NO_EVIDENCE | abstain | 否 | 0 | — | — | 1241 |
| d016 | out_of_scope | OUT_OF_SCOPE | OUT_OF_SCOPE | abstain | 否 | 0 | — | — | 0 |
| d017 | no_evidence | NO_EVIDENCE | NO_EVIDENCE | abstain | 否 | 0 | — | — | 967 |
| d018 | historical | HISTORICAL_NOT_SUPPORTED | HISTORICAL_NOT_SUPPORTED | abstain | 否 | 0 | — | — | 0 |
| d019 | historical | HISTORICAL_NOT_SUPPORTED | HISTORICAL_NOT_SUPPORTED | abstain | 否 | 0 | — | — | 0 |
| d020 | privacy | PRIVACY_REQUEST | PRIVACY_REQUEST | abstain | 否 | 0 | — | — | 0 |

## 限制

- 开发集 v0 只用于调参与回归，不是冻结测试集。
- support 只统计人工确认的标注；AI 预填不算人工验收。
- 探针耗时单列，不混入问答 p50/p95。
- W3 只做确定性核对；语义蕴含与重写循环在 W6。
