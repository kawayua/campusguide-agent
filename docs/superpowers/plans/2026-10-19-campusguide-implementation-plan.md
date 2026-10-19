# CampusGuide Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. 本计划按用户要求停留在“周粒度”，执行时再把每周拆成具体任务和测试步骤。

**Goal:** 在 10 周内交付一个可演示、可度量、可讲清楚的校园制度问答与办事指引 Agent。

**Architecture:** LangGraph 四角色流水线（Planner / Retriever / Advisor / Verifier）+ Finalize；条款级 RAG、claim-first 生成、确定性核对与明确拒答；流程型事务由 YAML DSL 驱动并输出个性化 checklist。

**Tech Stack:** Python 3.11、uv、Pydantic v2、LangGraph、FastMCP、Qdrant、bge-m3 / bge-reranker、OpenAI-compatible 模型接口、Gradio、Docker Compose、pytest。

**Spec:** [CampusGuide 详细设计规格书](../specs/2026-10-19-campusguide-design.md)

## Global Constraints

- 无引用不作答；引用 ID 必须真实存在且可定位，禁止编造条款。
- 条款级切分，保留版本、生效日期、人群和来源 URL；版本/人群过滤是硬约束。
- 流程步骤、材料、时限、负责部门只能来自 YAML，LLM 不得增删。
- 不接真实教务系统，不存储个人成绩、学号、身份证、手机号等敏感信息。
- 20 题开发集只用于调参；50 题测试集冻结后不再修改。
- 本地 Docker demo 必达；公开 demo 可选，不作为十周硬目标。
- 简历和 README 中的数字只能来自冻结测试集的评测报告。

## 执行原则

- 先做端到端垂直切片，再逐层加深；每周结束都有一个可运行或可验证的产物。
- 核心规则先写测试再实现：条款切分、版本解析、三值条件、引用校验、拒答判定。
- 单元测试和契约测试使用 fake provider / 内存 Qdrant，不依赖真实模型和网络。
- 每周记录：完成了什么、验收数字、遗留风险、下周是否触发降级门。

## 十周计划

| 周 | 主题 | 本周完成什么 | 验收门 |
|---|---|---|---|
| W1 | 数据与索引地基 | 建仓库骨架与配置；完成来源清单、快照抓取、PDF/HTML 解析、条款切分、元数据、联系方式脱敏、manifest；搭 Qdrant 索引骨架 | 文档版本 ≥50 个、制度本体 ≥30 个；20 条切分抽检 ≥95%；`make index` 可复现 |
| W2 | 单轮问答垂直切片 | 查询规范化、基础条款检索、claim-first 回答、引用 ID 存在性校验、基础 trace；建立 20 题开发集 v0；最小 Gradio 页面 | 端到端可回答；引用 ID 有效率 100%；零编造 ID |
| W3 | 引用核对与拒答 v0 | 引文定位、数字/日期覆盖检查、硬校验、拒答判定树 v0；跑开发集 20 题 | 开发集引用支持率 ≥85%；拒答召回 ≥80%；误拒率 ≤20% |
| W4 | MCP 化与检索增强 | 两个只读 MCP server（policy-search / policy-meta）；人群/时间过滤；混合召回与 rerank；contract test | 工具层全部走 MCP；contract test 通过；至少 5 个人群错配用例通过率 ≥90% |
| W5 | 流程 DSL 与多轮槽位 | YAML 流程 DSL、受限三值谓词、三个首版流程、槽位追问、槽位更正重算、checklist 渲染 | 三组脚本化多轮对话全部通过；缺槽位会追问；更正槽位会重算 |
| W6 | 完整四角色与核对闭环 | 接通 Planner / Retriever / Advisor / Verifier 全图；语义核对；重写与追问两处回退；人工建议；12 条红队用例 | 完整流水线可演示；至少两个真实拒答案例；12 条红队用例全部符合预期 |
| W7 | 评测与消融 | 冻结 50 题测试集；跑主结果；四组固定消融（分块、Verifier、流程 DAG、元数据过滤）；人工核对与错误归因 | 报告含主表、分类指标、置信区间、消融表、失败归因；测试集 hash 固定 |
| W8 | 演示与文档 | UI 完善（引用展开、checklist、拒答原因、调试面板）；Docker Compose；CI；README、数据来源与合规声明、技术博客、GIF | 新机器按 README 一条命令启动；五个固定场景稳定走通；CI 通过 |
| W9 | 开源 PR 与外部反馈 | 优先给 Dify 知识库/引用展示文档、OpenManus 相关 good-first-issue 提 PR；收集反馈并修复主线问题 | 至少 1 个 merged，争取 2 个；不阻塞主线交付 |
| W10 | 求职材料与收尾 | 简历 bullet、面试追问准备、30 秒介绍、模拟面试、最终修复 | 30 秒介绍 + 8 个高频追问可答；所有数字可追溯到评测报告 |

## 五周裁剪线

如果 W4 结束仍没有稳定的条款 RAG、引用和拒答，立即切到五周压缩线：

| 周 | 内容 |
|---|---|
| W1 | 语料、切分、元数据、单轮检索 |
| W2 | claim-first 回答、引用 ID/引文校验、拒答 v0 |
| W3 | 两个 MCP server、人群/时间过滤、rerank |
| W4 | 30~50 题评测、两组消融（分块、Verifier） |
| W5 | UI、Docker、README、博客 |

砍掉流程 DAG、多轮槽位、语义核对和完整四组消融；保留条款级 RAG、强制引用、拒答和至少两组消融。简历中不得声称完成了未实现的能力。

## 关键决策门

- **W4 末**：RAG、引用、拒答不稳 → 切五周压缩线，停止流程 DAG 和多轮开发。
- **W6 末**：语义核对成本或稳定性不可接受 → 保留规则核对 + 更严格拒答，报告如实说明。
- **W7 末**：测试集冻结；指标不达标时只修错误归因中占比最高的两类问题，不改测试集。
- **W8 末**：公开 demo 受阻 → 保留本地 Docker demo + 录屏，不把公开链接作为硬目标。

## 完成定义

- 五个固定 demo 场景可稳定复现；
- 50 题测试集报告完整，含失败归因和置信区间；
- 引用 ID 有效率 100%，没有编造引用；
- 至少两个真实拒答案例可演示；
- README 含合规声明、数据来源、评测表、运行方式；
- Docker Compose 一条命令启动；
- 代码、配置、测试和文档可通过 CI；
- 简历 bullet 的数字全部来自评测报告。
