# CampusGuide W3 开发集报告

- 数据集：`dev_v0.jsonl`（开发集 v0，**非冻结测试集**）
- 生成时间：2026-10-23T01:58:25+00:00
- commit：`e81828b`
- 模型：mimo-v2.6-pro-ultraspeed
- 支持标注：57 条，标注人 qoder-ai-preannotation（**AI 预标注，待人工确认**）
- 标注口径：严格引文级——引文自身须支撑 claim 的实质内容；所属条款的引导句/上下文不计入。

## 汇总

| 指标 | 数值 |
|---|---:|
| 题数 | 20 |
| 引用 ID 有效率 | 100.0% |
| 编造 ID 数 | 0 |
| quote 可定位率（原文 span） | 100.0% |
| draft 支持率 | 68.4% （unlabeled 0） |
| rendered 支持率 | 75.0% （unlabeled 0） |
| partial 率 | 0.0% |
| 拒答召回 | 100.0% |
| 误拒率 | 7.1% |
| 原因码正确率 | 100.0% |
| p50 / p95（warm） | 2900 / 9968 ms |
| 废止探针 | 本快照未记录 probe_ms（旧版 runner）；下次运行起单独记录 |
| 结构门禁 | 通过 |
| 拒答门禁 | 通过 |
| rendered 支持率门禁（≥85%） | 未通过 |
| W3 总门禁 | 未通过 |

## issue 分布

| code | 数量 |
|---|---:|
| AUDIENCE_UNDETERMINED | 14 |
| MIXED_VERSIONS | 2 |
| NUMBER_NOT_SUPPORTED | 5 |

## 拒答混淆矩阵

| 期望 | 实际 | 数量 |
|---|---|---:|
| ANSWERED | ANSWERED | 13 |
| ANSWERED | UNVERIFIABLE | 1 |
| HISTORICAL_NOT_SUPPORTED | HISTORICAL_NOT_SUPPORTED | 2 |
| NO_EVIDENCE | NO_EVIDENCE | 2 |
| OUT_OF_SCOPE | OUT_OF_SCOPE | 1 |
| PRIVACY_REQUEST | PRIVACY_REQUEST | 1 |

## 错误归因

| 主因 | 数量 |
|---|---:|
| NUMBER_NOT_SUPPORTED | 1 |

## 逐题结果

| ID | 类别 | outcome | 期望 | 主因 | partial | 引用 | 延迟(ms) |
|---|---|---|---|---|---|---:|---:|
| d001 | single_article | ANSWERED | ANSWERED | — | 否 | 5 | 6065 |
| d002 | single_article | ANSWERED | ANSWERED | — | 否 | 5 | 2699 |
| d003 | single_article | ANSWERED | ANSWERED | — | 否 | 3 | 10654 |
| d004 | single_article | ANSWERED | ANSWERED | — | 否 | 2 | 9932 |
| d005 | single_article | ANSWERED | ANSWERED | — | 否 | 5 | 2902 |
| d006 | single_article | UNVERIFIABLE | ANSWERED | NUMBER_NOT_SUPPORTED | 否 | 0 | 2899 |
| d007 | single_article | ANSWERED | ANSWERED | — | 否 | 3 | 2012 |
| d008 | single_article | ANSWERED | ANSWERED | — | 否 | 5 | 5156 |
| d009 | multi_article | ANSWERED | ANSWERED | — | 否 | 5 | 2529 |
| d010 | multi_article | ANSWERED | ANSWERED | — | 否 | 5 | 8422 |
| d011 | multi_article | ANSWERED | ANSWERED | — | 否 | 5 | 3190 |
| d012 | multi_article | ANSWERED | ANSWERED | — | 否 | 5 | 5777 |
| d013 | section | ANSWERED | ANSWERED | — | 否 | 4 | 4462 |
| d014 | section | ANSWERED | ANSWERED | — | 否 | 2 | 7939 |
| d015 | no_evidence | NO_EVIDENCE | NO_EVIDENCE | — | 否 | 0 | 82 |
| d016 | out_of_scope | OUT_OF_SCOPE | OUT_OF_SCOPE | — | 否 | 0 | 0 |
| d017 | no_evidence | NO_EVIDENCE | NO_EVIDENCE | — | 否 | 0 | 114 |
| d018 | historical | HISTORICAL_NOT_SUPPORTED | HISTORICAL_NOT_SUPPORTED | — | 否 | 0 | 0 |
| d019 | historical | HISTORICAL_NOT_SUPPORTED | HISTORICAL_NOT_SUPPORTED | — | 否 | 0 | 0 |
| d020 | privacy | PRIVACY_REQUEST | PRIVACY_REQUEST | — | 否 | 0 | 0 |

## 限制

- 20 题样本仅提供方向性证据；支持率依赖单标注人，W7 冻结测试集再报告区间。
- 严格引文级口径下，rendered 支持率未达 85% 的主因是**引文过窄**：d005 的引文只截取第八条情形列举项（处分后果在条款引导句），d008 的引文只截取条件项（“申请励志奖学金应同时符合…”的匹配句未入选）；若按条款上下文口径，两条可判 supported。W6 用重写/提示词改进引文跨度。
- p95 沿用 W2 已知限制（MiMo 长尾），W3 未新增 LLM 调用，不新增延迟达标声明。
- 版本冲突只在 Evidence 内检测；不同生效日的届别版本按 warning 保留，完整版本解析在 W4 接入。
