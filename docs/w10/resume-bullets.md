# W10 简历 bullet

> 原则：每条都带一个可追溯数字；不把工程质量门写成问答效果；不写未完成的 W9 merged。

## 版本 A（RAG / LLM 应用岗，4 条）

- 构建面向华南农业大学公开制度的**条款级 RAG 问答 Agent**：225 份文档 / 244 个版本 / 4,026 个可检索块；dense + BM25 sparse 混合召回、RRF 融合与 bge-reranker 重排，dev_v0 检索 Recall@8 **1.000**、MRR **0.958**。
- 设计 **claim-first 生成 + 双层核对**：引用 ID 必须来自本轮证据、quote 必须可定位、数字/日期/版本/受众必须覆盖；规则层后再做 claim-quote 语义核对；20 题评测引用 **39/39** 有效、**0** 条 quote 错配、拒答召回 **100%**，人工 support **92.7%**。
- 实现 **YAML 流程 DSL + 确定性 checklist**：3 个办事流程、槽位追问/更正、`closed` 截止态、流程事实不交给 LLM；W5 离线测试 495 项，后续 W8 全量 646 项。
- 交付 **可运行演示层**：Gradio 三栏多轮 UI、引用/checklist/clarify/调试面板、节点进度；Docker Compose 启动 app + Qdrant（镜像 2.79GB）；CI 覆盖 lint、format、compose config 与全量测试。

## 版本 B（Agent / 平台岗，3 条）

- 用 **LangGraph 四角色图**（Planner / Retriever / Advisor / Verifier + Finalize）组织政策问答：规则优先规划、多查询检索、流程 citation 水合、Advisor 只修失败 claim、clarify/revise 有上限；生产 MCP 路径 20 题结构门禁通过。
- 建立 **claim + quote + semantic 三层可信链**：引用不通过不渲染、语义 contradicted 不释放、超时/工具失败 fail-closed；20 题评测 0 quote mismatch、拒答 100%、人工 support 92.7%。
- 把流程办理从 LLM 手里拿回来：**YAML DSL + 三值谓词 + DAG checklist**，支持 closed/partial、材料/截止/部门/引用；Docker/CI/演示文档完整，W7 冻结集失败保留在报告中。

## 版本 C（一句话，用于开头或摘要）

从公开制度文档到可核对答案和可执行 checklist：我做了检索、claim-first 生成、双层核对、流程 DSL 和可运行 demo，并用 20 题评测与 W7 冻结集记录效果和失败。

## 不要写进简历

- “W7 通过”或“50 题全过”——W7 总门禁未通过，tag `w7` 已冻结；
- “W9 已 merged / 已贡献 Dify”——W9 deferred，没有 PR；
- “生产 support 92.7%”——92.7% 是评测档 ultraspeed；生产 vPro 是 97.8%，两者是不同 run；
- “所有问题 p95 <10s”——W7 p95 21.38s，生产 vPro 曾 174.4s，评测 ultraspeed 43.6s；
- “全自动、无需人工”——有 handoff、澄清上限和人工入口。

## 证据入口

- 数据：`data/manifest.json`（225 / 244 / 4,026）
- W6-B 评测：`data/eval/w6b-ultraspeed-final-dev-report.md`
- W6-B 生产：`data/eval/w6b-mcp-final-dev-report.md`
- W7：`data/eval/w7-remediation4-report.md`、`docs/weekly/w7.md`
- W8：`docs/weekly/w8.md`、`data/eval/w8-ui-smoke.md`
- W9：`docs/weekly/w9.md`（deferred）