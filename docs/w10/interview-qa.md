# W10 高频面试问答

> 用法：先给 20–30 秒结论，再给 1 个设计取舍/数字，最后主动说限制。所有数字见 `metrics-ledger.md`。

## 1. 为什么按“条款”切分，不按固定长度 chunk？

制度文档的最小可引用单位是“第 X 条”。按条切分让 citation 能回到具体条款，版本/受众/生效区间也能按条继承；固定 chunk 容易把一条拆成两半，引用无法定位。培养方案、教师页、新闻目录这类非条款内容用 section 块，检索和评测时显式区分 `block_type`，不把 section 算进条款指标。

## 2. 怎么防止模型编造引用或说过头？

先 claim-first：模型只输出结论和 citation，不直接写正文；引用 ID 必须来自本轮证据，quote 必须能在原文定位；再检查数字、日期、金额、期限、条件、版本和受众。W6 加 claim-quote 语义核对，contradicted 不渲染；不通过就丢弃或重写，重写最多 2 次，仍失败就 partial/abstain。没有引用就不回答。

## 3. W3、W6、W7 分别是什么关系？

W3 是引用/数字/版本/隐私的确定性核对层，baseline 人工 rendered support 75.0%，未过 85%。W6-A 只改 Advisor 提示词，把 20 题 rendered support 提到 98.1%（54/54 人工）；W6-B 再把线性流程包进 LangGraph，加语义核对和 revise/clarify。W7 是冻结 50 题评测，最终 remediation4 的 support 1.000、answer 0.875、version 1.000，但 p95 21.38s 超 10s，**总门禁仍未通过**。我不会把 W6-A 的 98.1% 说成 W7 通过。

## 4. W7 到底失败在哪？

最终 remediation4 的质量门禁是过的：support 1.000、answer correctness 0.875（35/40）、version-sensitive 1.000；失败的是延迟：p95 21.38s，超过原定 10s。更早的 remediation2 还出现过 answer 0.750、version 0.200。W7 已终止并打 tag `w7`，冻结集/标签/报告不再修改；W8 的演示不拿 W7 当通过。

## 5. W6-B 的 support 92.7% 和 97.8% 为什么有两个？

它们是两条 run：评测档 ultraspeed 是 42 条 draft，39 supported / 3 partial / 0 unsupported，rendered 92.7%，p50 20.2s / p95 43.6s；生产档 vPro 是 45 条，44 supported / 1 partial，rendered 97.8%，p95 174.4s。模型和 run 不同，不能混写。UI 不展示模型名，但评测报告和部署记录分开保留。

## 6. 为什么评测用 ultraspeed，生产用 vPro？

两者同一基座能力，ultraspeed 是快速版。评测档要快速、可重复地跑图；生产档选 vPro 作为部署基线。这个选择不是“能力更高”，而是延迟/端点稳定性取舍。换模型必须新 run、重新标注，不能复用旧标签。

## 7. 流程问题怎么保证不编截止日期？

流程事实全部来自 `configs/flows/*.yaml`：步骤、材料、部门、deadline/deadline_note、citation。W5 引擎做 DAG、三值谓词、槽位和 `closed`/`partial`；整批 closed 时不再追问年级，只给截止信息和 handoff。Advisor 不允许写 checklist 事实；混合问题由 Finalize 分区合并，流程事实以 YAML 为准。

## 8. 多轮澄清和更正怎么处理？

Planner 规则优先：先 guard、流程匹配、槽位；缺必填槽位只问一个，按 priority；用户拒答记 declined，不重复问；用户更正覆盖旧值并重算 checklist；clarify 最多 3 次，超过给通用路径 + handoff。W6 的 LangGraph 里 clarify 回 Planner，revise 回 Advisor，都有次数上限。

## 9. 为什么要 LangGraph，线性 pipeline 不够吗？

线性 pipeline 能跑通单轮，但表达不了 Verifier → Advisor 的 revise、Verifier → Planner 的 clarify、flow/QA 混合路由和多轮 session。LangGraph 的 StateGraph/checkpointer 让这些回退和状态显式化；同时保留 `handle` 兼容路径，测试不依赖真实图运行时。

## 10. 数据合规怎么做的？

只用学校官网公开页面/附件；不下载登录页、个人申请表、成绩单、名单；联系人姓名/手机号/学号/身份证脱敏；完整快照和解析文本只在本地，公开仓库不放原始语料；引用始终指向官网。含个人信息的公示只保留标题/日期/公示期元数据。

## 11. 最大的技术取舍是什么？

把“确定性”尽量留给程序：检索过滤、引用校验、版本/受众 scope、流程事实、closed 状态都不交给 LLM；LLM 只负责 claim 生成、语义核对和少量查询改写。代价是规则维护和流程 YAML 成本，收益是可复现、可解释、失败关闭。

## 12. 如果重做，优先改什么？

先改延迟和 W7 的答案覆盖：vPro 生产 p95 174s，评测 ultraspeed 43.6s，仍不适合所有现场演示；W7 的 answer 0.875 还差 2 题。其次是把 W9 开源 PR 做掉（当前因 GitHub 网络/认证 deferred），以及补更多学院的流程来源。不会通过改冻结测试集或换模型复用旧标签来“提升”指标。

## 追问速答

- **为什么不用纯向量？** 制度有专有名词/条号，BM25 补词面；RRF 后 rerank。
- **为什么不用纯 LLM 判断资格？** 资格是高 stakes，规则/YAML 可审计，LLM 只做解释。
- **为什么 W3 仍说有条件通过？** baseline 75.0% 不改；W6-A 同 20 题、同 Verifier、人工 98.1% 作为 remediation，两个数字都保留。
- **为什么 W9 没做？** GitHub 443 超时、无 gh/token/proxy；没有提交 PR，不声称 merged。