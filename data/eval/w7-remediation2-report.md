# CampusGuide W7 冻结测试集报告

- 数据集：`test_v1.jsonl`（文件 `208cf3c5f032…`）
- 冻结测试集 hash：`2a8d0e122362…`
- run_sha256：`23b384f155560320f5cb32c836cd2c965190bd4746c9f9dc17f423e81ffa264a`
- git：`57f91fd8d8fe36d0975ac650d2c98f42cd1bbcd1`（dirty=True）
- 模型：mimo-v2.6-pro-ultraspeed
- prompt：w6a2.1-2026-10-24
- 索引：17441d486b2fb788456b875665c695d9bafa2cc4a9bef1c974c91b19bd24e7a2
- 标注：yaukin

## 门禁

| 门禁 | 结果 | 数值 | 目标 |
|---|---|---:|---:|
| 结构门禁 | 通过 | 100% | — |
| 引用支持率 | 通过 | 0.985 | 0.95 |
| 可回答题正确率 | 未通过 | 0.750 | 0.85 |
| 拒答召回 | 通过 | 1.000 | 0.90 |
| 误拒率 | 通过 | 0.000 | 0.10 |
| 版本敏感题正确率 | 未通过 | 0.200 | 0.90 |
| article Recall@8 | 通过 | 1.000 | 0.90 |
| p50 延迟 | 未通过 | 8876 ms | 6000.00 |
| p95 延迟 | 未通过 | 35095 ms | 10000.00 |

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
| draft support | 0.985 |
| rendered support | 0.985 |
| answer correctness | 0.750 |
| version-sensitive | 0.200 |
| p50 / p95 | 8875.5 / 35095.3 ms |

## 分类正确率

| 类别 | correct/可答 | partial | incorrect | 正确率 |
|---|---:|---:|---:|---:|
| Overall | 30/40 | 8 | 2 | 0.750 |
| single_article | 23/25 | 1 | 1 | 0.920 |
| multi_article | 6/10 | 4 | 0 | 0.600 |
| section | 1/5 | 3 | 1 | 0.200 |
| version_sensitive | 1/5 | 3 | 1 | 0.200 |

## 拒答混淆矩阵

| 实际 \ 系统 | 渲染答案 | 明确拒答 |
|---|---:|---:|
| 可回答（40） | 40 | 0 |
| 不可回答（10） | 0 | 10 |

## 标注样本与 Wilson 95% 区间

| 指标 | 成功/样本 | 比率 | 95% CI |
|---|---:|---:|---:|
| rendered support | 133/135 | 0.985 | 0.948–0.996 |
| 可回答题正确 | 30/40 | 0.750 | 0.598–0.858 |
| 版本敏感 | 1/5 | 0.200 | 0.036–0.625 |
| 拒答召回 | 10/10 | 1.000 | 0.723–1.000 |
| 误拒 | 0/40 | 0.000 | 0.000–0.088 |

- support 排除 1 条未进入统计的 draft（共 136 条）。
- answer 明细：correct 30 / partial 8 / incorrect 2；不可答题 10/10 判断正确。

## 错误归因（设计 12.7）

| 主因 | 数量 | 题号 |
|---|---:|---|
| UNSUPPORTED_CLAIM | 10 | test-003, test-007, test-029, test-031, test-033, test-035, test-036, test-037, test-038, test-039 |

- 其余类别为 0：RETRIEVAL_MISS, WRONG_VERSION, WRONG_AUDIENCE, CITATION_MISMATCH, INCOMPLETE_FLOW, OVER_REFUSAL, UNDER_REFUSAL, TOOL_FAILURE, LATENCY；INCOMPLETE_FLOW 为 N/A（W5 未实现）。

### UNSUPPORTED_CLAIM
- test-003: 缺 required fact「助推学校“双一流”建设」，后备军/蓄水池作用亦未写入结论
- test-007: 缺「国发〔2007〕13号」与「完善学校奖助学体系」；目的答成第二条「发挥学院主动性」，非 gold 第一条目的
- test-029: 缺 required fact「合同专用章」；原则与合法性审查正确
- test-031: 缺 required fact「纳入大学生人才培养方案」；原则与出发点正确
- test-033: 缺 required fact「成建制培养」；连续三年配置与 15-30 个正确
- test-035: 缺 required fact「下一学期开学初到我校办理报到注册手续」；一式4份等正确
- test-036: 缺 required fact「创新性研究成果」；外语与硕士生学术研究能力正确
- test-037: 缺「被同行认可的创新性成果」；就业去向正确，但研究成果答成第五条论文条款
- test-038: 缺「严格遵守科学技术研究学术规范」精确表述；答的是二、基本素质学术道德，非一、培养目标原文
- test-039: 3 个 required facts 均未覆盖；命中的是「基本知识/基本学术能力」章节，非 gold「一、人才培养目标」

- 延迟：p50/p95 未达门禁属于系统级延迟限制，不计入单题错误主因。

## 消融

### A5 检索

| 模式 | overall Recall@8 | overall MRR | article MRR | section MRR | p95 |
|---|---:|---:|---:|---:|---:|
| `dense` | 0.963 | 0.829 | 0.824 | 0.867 | 248.2 ms |
| `hybrid_rrf` | 1.000 | 0.863 | 0.877 | 0.767 | 412.4 ms |
| `hybrid_rerank` | 1.000 | 0.958 | 0.967 | 0.900 | 1365.4 ms |

- A4 profile 过滤、A2 Verifier 消融待跑；A1 固定分块、A3 流程 DAG 为 N/A。

## 限制

- 50 题冻结集只作方向性证据；比例指标给 Wilson 95% 区间。
- A1 固定分块、A3 流程 DAG 未执行（缺少相应实现），报告标 N/A。
- support 与 answer correctness 必须是人工标注；AI 预标注不计入。
- 单标注人：yaukin；未执行 20% 第二人复核/隔天盲审，按设计披露。
- 人工修改处数（改事实/改条件/删无依据句/补引用）未分项记录，标 N/A。
- token/成本：当前 run row 未落盘 token，且无统一单价配置，标 N/A。
- remediation2 对 32 条 support、11 题 answer 完全一致项沿用第一轮标签；设计原要求旧 run 标签不复用，此为记录在案的偏差。
- p50/p95 实际为 8876/35095 ms，未达 6s/10s；按设计记录实际值，不删题、不改门禁。
