# CampusGuide W3 开发集报告

- 数据集：`dev_v0.jsonl`（开发集 v0，**非冻结测试集**）
- 生成时间：2026-10-23T15:11:07+00:00
- run_sha256：`5e542b25fb248b7924f0eca56788e5924715999f1fbeb0ace7f2d79273624442`
- git commit：`b240e4dfae98ca3f173e6be252a6d8a6226b943e`（dirty=True）
- 模型：mimo-v2.6-pro-ultraspeed
- 向量 manifest：`89b9cf78de226850fe16b0004cc5152dda2f32b502b3c51a869e41cd7074b69d`
- config hash：`8473ab37ea3e736978c6eeb2a990930ed5068cdfe056d74bdfbd6089a799a414`
- 延迟口径：warm（warmup 21168 ms；探针耗时单列）

## 门禁

| 门禁 | 结果 | 数值 |
|---|---|---:|
| 结构门禁 | 通过 | 引用 ID 100.0% / quote 100.0% |
| support 门禁 | 未通过 | rendered 75.0% |
| 拒答召回 | 通过 | 100.0% |
| 误拒率 | 通过 | 0.0% |
| W3 总门禁 | 未通过 | — |

**未通过原因：**

- support 门禁未通过：rendered 支持率 75.0% 低于 85%

## 汇总

| 指标 | 数值 |
|---|---:|
| 题数 | 20 |
| 可答题数 | 14 |
| 非可答题数 | 6 |
| outcomes | {'ANSWERED': 13, 'PARTIAL_ANSWER': 1, 'NO_EVIDENCE': 2, 'OUT_OF_SCOPE': 1, 'HISTORICAL_NOT_SUPPORTED': 2, 'PRIVACY_REQUEST': 1} |
| 引用 ID 有效率 | 100.0% |
| 编造 ID 数 | 0 |
| part_no 错误数 | 0 |
| quote 可定位率 | 100.0% |
| p50 延迟（warm，排除探针） | 3342 ms |
| p95 延迟（warm，排除探针） | 9778 ms |
| 探针次数 | 2 |
| 探针 p50 / p95 | 74 ms / 78 ms |

## support 标注

严格按引文级口径：只判断 quote 本身是否支撑 claim，不拿条款上下文补足。
unlabeled 不计入分母，但未完成人工标注时门禁不得通过。

| 指标 | draft | rendered |
|---|---:|---:|
| 总数 | 56 | 52 |
| 已标注 | 56 | 52 |
| unlabeled | 0 | 0 |
| supported | 39 | 39 |
| partial | 17 | 13 |
| unsupported | 0 | 0 |
| 支持率 | 69.6% | 75.0% |

- draft 支持率：69.6%
- rendered 支持率：75.0%
- 人工标注完整：是
- support 门禁：未通过（rendered 支持率 75.0% 低于 85%）

## 拒答混淆矩阵

| 预期 \ 是否渲染 | 渲染答案 | 未渲染答案 |
|---|---:|---:|
| 可答 | TP=14 | FN=0 |
| 非可答 | FP=0 | TN=6 |

- 拒答召回：100.0%
- 误拒率：0.0%
- partial 率：7.1%
- 原因码正确率：100.0%

## issue 分布

| code | scope | severity | count |
|---|---|---|---:|
| AUDIENCE_UNDETERMINED | evidence | warning | 14 |
| NUMBER_NOT_SUPPORTED | claim | blocking | 4 |
| MIXED_VERSIONS | evidence | warning | 2 |

## 错误归因

| 主因 | 题数 |
|---|---:|
| PARTIAL_INCOMPLETE | 8 |

## 逐题结果

| ID | 类别 | 期望 | outcome | decision | partial | 渲染 claim | 丢弃 claim | 主因 | 延迟(ms) |
|---|---|---|---|---|---:|---:|---|---|---:|
| d001 | single_article | ANSWERED | ANSWERED | pass | 否 | 5 | — | PARTIAL_INCOMPLETE | 3190 |
| d002 | single_article | ANSWERED | ANSWERED | pass | 否 | 3 | — | — | 4506 |
| d003 | single_article | ANSWERED | ANSWERED | pass | 否 | 3 | — | PARTIAL_INCOMPLETE | 2988 |
| d004 | single_article | ANSWERED | ANSWERED | pass | 否 | 2 | — | PARTIAL_INCOMPLETE | 12784 |
| d005 | single_article | ANSWERED | ANSWERED | pass | 否 | 5 | — | PARTIAL_INCOMPLETE | 4088 |
| d006 | single_article | ANSWERED | PARTIAL_ANSWER | partial | 是 | 1 | c2, c3, c4, c5 | PARTIAL_INCOMPLETE | 3495 |
| d007 | single_article | ANSWERED | ANSWERED | pass | 否 | 3 | — | — | 2406 |
| d008 | single_article | ANSWERED | ANSWERED | pass | 否 | 5 | — | — | 3789 |
| d009 | multi_article | ANSWERED | ANSWERED | pass | 否 | 5 | — | PARTIAL_INCOMPLETE | 5812 |
| d010 | multi_article | ANSWERED | ANSWERED | pass | 否 | 4 | — | — | 9620 |
| d011 | multi_article | ANSWERED | ANSWERED | pass | 否 | 5 | — | — | 3761 |
| d012 | multi_article | ANSWERED | ANSWERED | pass | 否 | 5 | — | PARTIAL_INCOMPLETE | 3072 |
| d013 | section | ANSWERED | ANSWERED | pass | 否 | 4 | — | — | 8030 |
| d014 | section | ANSWERED | ANSWERED | pass | 否 | 2 | — | PARTIAL_INCOMPLETE | 3528 |
| d015 | no_evidence | NO_EVIDENCE | NO_EVIDENCE | abstain | 否 | 0 | — | — | 114 |
| d016 | out_of_scope | OUT_OF_SCOPE | OUT_OF_SCOPE | abstain | 否 | 0 | — | — | 0 |
| d017 | no_evidence | NO_EVIDENCE | NO_EVIDENCE | abstain | 否 | 0 | — | — | 101 |
| d018 | historical | HISTORICAL_NOT_SUPPORTED | HISTORICAL_NOT_SUPPORTED | abstain | 否 | 0 | — | — | 0 |
| d019 | historical | HISTORICAL_NOT_SUPPORTED | HISTORICAL_NOT_SUPPORTED | abstain | 否 | 0 | — | — | 0 |
| d020 | privacy | PRIVACY_REQUEST | PRIVACY_REQUEST | abstain | 否 | 0 | — | — | 0 |

## 限制

- 开发集 v0 只用于调参与回归，不是冻结测试集。
- support 只统计人工确认的标注；AI 预填不算人工验收。
- 探针耗时单列，不混入问答 p50/p95。
- W3 只做确定性核对；语义蕴含与重写循环在 W6。
