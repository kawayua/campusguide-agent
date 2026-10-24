# CampusGuide W6-A2 开发集报告

- 数据集：`dev_v0.jsonl`（开发集 v0，**非冻结测试集**）
- 生成时间：2026-10-24T06:56:26+00:00
- run_sha256：`b7ee6884ec3191d2b908afdd6cb97fe7191a5a74302b3281a96f249c07e6f569`
- git commit：`03fbcb1c141c313301ca282b98c271c736a5cb0e`（dirty=True）
- 模型：mimo-v2.6-pro-ultraspeed
- prompt：w6a2-2026-10-24（max_tokens=3000）
- 向量 manifest：`17441d486b2fb788456b875665c695d9bafa2cc4a9bef1c974c91b19bd24e7a2`
- 检索：hybrid
- config hash：`c1016f0974b375b586327debe70fb06e0c565b8835bc00c89b9e2ab396879865`
- 延迟口径：warm（warmup 52441 ms；探针耗时单列）

## 门禁

| 门禁 | 结果 | 数值 |
|---|---|---:|
| 结构门禁 | 通过 | 引用 ID 100.0% / quote 100.0% |
| support 门禁 | 通过 | rendered 100.0% |
| 拒答召回 | 通过 | 100.0% |
| 误拒率 | 通过 | 7.1% |
| W6-A2 总门禁 | 通过 | — |

## 汇总

| 指标 | 数值 |
|---|---:|
| 题数 | 20 |
| 可答题数 | 14 |
| 非可答题数 | 6 |
| outcomes | {'PARTIAL_ANSWER': 1, 'ANSWERED': 12, 'GENERATION_FAILED': 1, 'NO_EVIDENCE': 2, 'OUT_OF_SCOPE': 1, 'HISTORICAL_NOT_SUPPORTED': 2, 'PRIVACY_REQUEST': 1} |
| 引用 ID 有效率 | 100.0% |
| 编造 ID 数 | 0 |
| part_no 错误数 | 0 |
| quote 可定位率 | 100.0% |
| p50 延迟（warm，排除探针） | 7926 ms |
| p95 延迟（warm，排除探针） | 34564 ms |
| 探针次数 | 2 |
| 探针 p50 / p95 | 1082 ms / 1134 ms |

## support 标注

严格按引文级口径：只判断 quote 本身是否支撑 claim，不拿条款上下文补足。
unlabeled 不计入分母，但未完成人工标注时门禁不得通过。

| 指标 | draft | rendered |
|---|---:|---:|
| 总数 | 50 | 49 |
| 已标注 | 50 | 49 |
| unlabeled | 0 | 0 |
| supported | 50 | 49 |
| partial | 0 | 0 |
| unsupported | 0 | 0 |
| 支持率 | 100.0% | 100.0% |

- draft 支持率：100.0%
- rendered 支持率：100.0%
- 人工标注完整：是
- support 门禁：通过

## 拒答混淆矩阵

| 预期 \ 是否渲染 | 渲染答案 | 未渲染答案 |
|---|---:|---:|
| 可答 | TP=13 | FN=1 |
| 非可答 | FP=0 | TN=6 |

- 拒答召回：100.0%
- 误拒率：7.1%
- partial 率：7.1%
- 原因码正确率：100.0%

## issue 分布

| code | scope | severity | count |
|---|---|---|---:|
| AUDIENCE_UNDETERMINED | evidence | warning | 14 |
| TOO_MANY_CLAIMS | claim | blocking | 1 |

## 错误归因

| 主因 | 题数 |
|---|---:|
| GENERATION_FAILED | 1 |
| PARTIAL_INCOMPLETE | 1 |

## 逐题结果

| ID | 类别 | 期望 | outcome | decision | partial | 渲染 claim | 丢弃 claim | 主因 | 延迟(ms) |
|---|---|---|---|---|---:|---:|---|---|---:|
| d001 | single_article | ANSWERED | PARTIAL_ANSWER | partial | 是 | 5 | c6 | PARTIAL_INCOMPLETE | 22879 |
| d002 | single_article | ANSWERED | ANSWERED | pass | 否 | 3 | — | — | 9944 |
| d003 | single_article | ANSWERED | ANSWERED | pass | 否 | 3 | — | — | 4735 |
| d004 | single_article | ANSWERED | ANSWERED | pass | 否 | 2 | — | — | 5010 |
| d005 | single_article | ANSWERED | ANSWERED | pass | 否 | 4 | — | — | 7655 |
| d006 | single_article | ANSWERED | ANSWERED | pass | 否 | 5 | — | — | 28408 |
| d007 | single_article | ANSWERED | ANSWERED | pass | 否 | 3 | — | — | 8102 |
| d008 | single_article | ANSWERED | ANSWERED | pass | 否 | 4 | — | — | 32761 |
| d009 | multi_article | ANSWERED | ANSWERED | pass | 否 | 5 | — | — | 9008 |
| d010 | multi_article | ANSWERED | ANSWERED | pass | 否 | 5 | — | — | 9594 |
| d011 | multi_article | ANSWERED | ANSWERED | pass | 否 | 5 | — | — | 14168 |
| d012 | multi_article | ANSWERED | GENERATION_FAILED | abstain | 否 | 0 | — | GENERATION_FAILED | 68823 |
| d013 | section | ANSWERED | ANSWERED | pass | 否 | 3 | — | — | 7941 |
| d014 | section | ANSWERED | ANSWERED | pass | 否 | 2 | — | — | 7912 |
| d015 | no_evidence | NO_EVIDENCE | NO_EVIDENCE | abstain | 否 | 0 | — | — | 1573 |
| d016 | out_of_scope | OUT_OF_SCOPE | OUT_OF_SCOPE | abstain | 否 | 0 | — | — | 0 |
| d017 | no_evidence | NO_EVIDENCE | NO_EVIDENCE | abstain | 否 | 0 | — | — | 906 |
| d018 | historical | HISTORICAL_NOT_SUPPORTED | HISTORICAL_NOT_SUPPORTED | abstain | 否 | 0 | — | — | 0 |
| d019 | historical | HISTORICAL_NOT_SUPPORTED | HISTORICAL_NOT_SUPPORTED | abstain | 否 | 0 | — | — | 0 |
| d020 | privacy | PRIVACY_REQUEST | PRIVACY_REQUEST | abstain | 否 | 0 | — | — | 0 |

## 限制

- 开发集 v0 只用于调参与回归，不是冻结测试集。
- support 只统计人工确认的标注；AI 预填不算人工验收。
- 探针耗时单列，不混入问答 p50/p95。
- W3 只做确定性核对；语义蕴含与重写循环在 W6。
