# CampusGuide W6-A 开发集报告

- 数据集：`dev_v0.jsonl`（开发集 v0，**非冻结测试集**）
- 生成时间：2026-10-24T04:06:32+00:00
- run_sha256：`46e016d5b2f1a106a7ad6d9f188c1f55c37cd3041a91e974c4418d41b4cba127`
- git commit：`b240e4dfae98ca3f173e6be252a6d8a6226b943e`（dirty=True）
- 模型：mimo-v2.6-pro-ultraspeed
- prompt：w6a-2026-10-23（max_tokens=3000）
- 向量 manifest：`89b9cf78de226850fe16b0004cc5152dda2f32b502b3c51a869e41cd7074b69d`
- config hash：`8473ab37ea3e736978c6eeb2a990930ed5068cdfe056d74bdfbd6089a799a414`
- 延迟口径：warm（warmup 29530 ms；探针耗时单列）

## 门禁

| 门禁 | 结果 | 数值 |
|---|---|---:|
| 结构门禁 | 通过 | 引用 ID 100.0% / quote 100.0% |
| support 门禁 | 通过 | rendered 98.1% |
| 拒答召回 | 通过 | 100.0% |
| 误拒率 | 通过 | 0.0% |
| W6-A 总门禁 | 通过 | — |

## 汇总

| 指标 | 数值 |
|---|---:|
| 题数 | 20 |
| 可答题数 | 14 |
| 非可答题数 | 6 |
| outcomes | {'ANSWERED': 14, 'NO_EVIDENCE': 2, 'OUT_OF_SCOPE': 1, 'HISTORICAL_NOT_SUPPORTED': 2, 'PRIVACY_REQUEST': 1} |
| 引用 ID 有效率 | 100.0% |
| 编造 ID 数 | 0 |
| part_no 错误数 | 0 |
| quote 可定位率 | 100.0% |
| p50 延迟（warm，排除探针） | 3506 ms |
| p95 延迟（warm，排除探针） | 14985 ms |
| 探针次数 | 2 |
| 探针 p50 / p95 | 124 ms / 132 ms |

## support 标注

严格按引文级口径：只判断 quote 本身是否支撑 claim，不拿条款上下文补足。
unlabeled 不计入分母，但未完成人工标注时门禁不得通过。

| 指标 | draft | rendered |
|---|---:|---:|
| 总数 | 54 | 54 |
| 已标注 | 54 | 54 |
| unlabeled | 0 | 0 |
| supported | 53 | 53 |
| partial | 1 | 1 |
| unsupported | 0 | 0 |
| 支持率 | 98.1% | 98.1% |

- draft 支持率：98.1%
- rendered 支持率：98.1%
- 人工标注完整：是
- support 门禁：通过

## 拒答混淆矩阵

| 预期 \ 是否渲染 | 渲染答案 | 未渲染答案 |
|---|---:|---:|
| 可答 | TP=14 | FN=0 |
| 非可答 | FP=0 | TN=6 |

- 拒答召回：100.0%
- 误拒率：0.0%
- partial 率：0.0%
- 原因码正确率：100.0%

## issue 分布

| code | scope | severity | count |
|---|---|---|---:|
| AUDIENCE_UNDETERMINED | evidence | warning | 14 |
| MIXED_VERSIONS | evidence | warning | 2 |

## 错误归因

| 主因 | 题数 |
|---|---:|
| PARTIAL_INCOMPLETE | 1 |

## 逐题结果

| ID | 类别 | 期望 | outcome | decision | partial | 渲染 claim | 丢弃 claim | 主因 | 延迟(ms) |
|---|---|---|---|---|---:|---:|---|---|---:|
| d001 | single_article | ANSWERED | ANSWERED | pass | 否 | 5 | — | — | 6316 |
| d002 | single_article | ANSWERED | ANSWERED | pass | 否 | 3 | — | — | 2568 |
| d003 | single_article | ANSWERED | ANSWERED | pass | 否 | 2 | — | — | 2309 |
| d004 | single_article | ANSWERED | ANSWERED | pass | 否 | 2 | — | PARTIAL_INCOMPLETE | 3217 |
| d005 | single_article | ANSWERED | ANSWERED | pass | 否 | 5 | — | — | 14364 |
| d006 | single_article | ANSWERED | ANSWERED | pass | 否 | 5 | — | — | 10564 |
| d007 | single_article | ANSWERED | ANSWERED | pass | 否 | 4 | — | — | 5151 |
| d008 | single_article | ANSWERED | ANSWERED | pass | 否 | 5 | — | — | 4467 |
| d009 | multi_article | ANSWERED | ANSWERED | pass | 否 | 5 | — | — | 6599 |
| d010 | multi_article | ANSWERED | ANSWERED | pass | 否 | 5 | — | — | 3755 |
| d011 | multi_article | ANSWERED | ANSWERED | pass | 否 | 5 | — | — | 7142 |
| d012 | multi_article | ANSWERED | ANSWERED | pass | 否 | 5 | — | — | 26789 |
| d013 | section | ANSWERED | ANSWERED | pass | 否 | 2 | — | — | 3256 |
| d014 | section | ANSWERED | ANSWERED | pass | 否 | 1 | — | — | 13243 |
| d015 | no_evidence | NO_EVIDENCE | NO_EVIDENCE | abstain | 否 | 0 | — | — | 130 |
| d016 | out_of_scope | OUT_OF_SCOPE | OUT_OF_SCOPE | abstain | 否 | 0 | — | — | 0 |
| d017 | no_evidence | NO_EVIDENCE | NO_EVIDENCE | abstain | 否 | 0 | — | — | 94 |
| d018 | historical | HISTORICAL_NOT_SUPPORTED | HISTORICAL_NOT_SUPPORTED | abstain | 否 | 0 | — | — | 0 |
| d019 | historical | HISTORICAL_NOT_SUPPORTED | HISTORICAL_NOT_SUPPORTED | abstain | 否 | 0 | — | — | 0 |
| d020 | privacy | PRIVACY_REQUEST | PRIVACY_REQUEST | abstain | 否 | 0 | — | — | 0 |

## 限制

- 开发集 v0 只用于调参与回归，不是冻结测试集。
- support 只统计人工确认的标注；AI 预填不算人工验收。
- 探针耗时单列，不混入问答 p50/p95。
- W3 只做确定性核对；语义蕴含与重写循环在 W6。
