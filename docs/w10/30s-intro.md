# W10 30 秒项目介绍

## 中文版（约 30–40 秒）

我做了一个面向华南农业大学公开制度的条款级问答与办事指引 Agent。它覆盖 225 份文档、244 个版本、4,026 个可检索块；先做 dense + BM25 混合检索，再让模型只写带原文引用的 claims，经过引用、数字、版本、受众和语义核对后才渲染。办事流程用 YAML checklist 生成，不靠模型编截止日期。20 题开发集里，引用 39/39 有效、0 条 quote 错配、拒答 100%，人工 support 92.7%。W7 冻结集仍有延迟失败，我没有把它写成通过。

## English version (~30–40 seconds)

I built a clause-level RAG agent for South China Agricultural University's public policies: 225 documents, 244 versions, and 4,026 retrievable blocks. It uses hybrid dense-plus-BM25 retrieval, claim-first generation with verifiable quotes, and deterministic plus semantic verification before anything is rendered. Procedure guidance comes from YAML checklists, not from the model. On a 20-question dev set it had 39/39 valid citations, zero quote mismatches, 100% refusal recall, and 92.7% human-verified support. The frozen W7 set still failed on latency, and I kept that failure in the report.

## 讲解提示

- 先说“解决什么问题”，再说“怎么保证不编”，最后主动说失败；
- 不主动展开 W1–W8 每层细节；对方追问再展开；
- 不把 W6-A 的 98.1% 或 W6-B 生产 vPro 的 97.8% 说成“当前评测主结果”；
- 如果对方时间更短，保留：**225/244/4,026 + claim-first + 39/39、0 quote mismatch、92.7% + W7 延迟失败**。