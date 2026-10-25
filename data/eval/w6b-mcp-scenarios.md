# CampusGuide W6-B 生产 MCP 固定场景

- 生成时间：2026-10-25T08:16:04+00:00
- graph：`w6-2026-10-25`
- commit：`f03b9f27b4d87bac06abd987861f4defb44a8bc6`（dirty=False）
- index：`articles_v1@hybrid-6bafb68410b9`
- 结果：**5/5 PASS**

| case | 结果 | outcome | flow | closed/status | claims | checklist | handoff |
|---|---|---|---|---|---|---|---|
| mcp-closed-flow | PASS | ANSWERED | scholarship-application | closed | 0 | 9 | 是 |
| mcp-open-flow | PASS | CLARIFY→ANSWERED | scholarship-application | open | 0 | 9 | 是 |
| mcp-certificate-clarify | PASS | CLARIFY | enrollment-certificate | unknown | 0 | 0 | 是 |
| mcp-mixed | PASS | ANSWERED | scholarship-application | closed | 3 | 9 | 是 |
| mcp-decline | PASS | CLARIFY→PARTIAL_ANSWER | enrollment-certificate | unknown | 0 | 10 | 是 |

## 逐场景

### mcp-closed-flow：政策流程：国奖 2026 批次已截止

- 第 1 轮：`ANSWERED`，flow=`scholarship-application`，status=`closed`，claims=0，checklist=9，next_slot=`-`，347ms；trace=planner→flow→finalize

### mcp-open-flow：政策流程：数信大二申请国奖（开放期）

- 第 1 轮：`CLARIFY`，flow=`scholarship-application`，status=`open`，claims=0，checklist=0，next_slot=`has_record`，27ms；trace=planner→finalize
- 第 2 轮：`ANSWERED`，flow=`scholarship-application`，status=`open`，claims=0，checklist=9，next_slot=`-`，12ms；trace=planner→flow→finalize

### mcp-certificate-clarify：在读证明：本科/研究生必须先澄清

- 第 1 轮：`CLARIFY`，flow=`enrollment-certificate`，status=`unknown`，claims=0，checklist=0，next_slot=`level`，11ms；trace=planner→finalize

### mcp-mixed：混合：国奖怎么申请 + 金额是多少

- 第 1 轮：`ANSWERED`，flow=`scholarship-application`，status=`closed`，claims=3，checklist=9，next_slot=`-`，132834ms；trace=planner→flow→retriever→advisor→verifier→advisor→verifier→finalize

### mcp-decline：拒答槽位：在读证明转通用路径 + handoff

- 第 1 轮：`CLARIFY`，flow=`enrollment-certificate`，status=`unknown`，claims=0，checklist=0，next_slot=`level`，8ms；trace=planner→finalize
- 第 2 轮：`PARTIAL_ANSWER`，flow=`enrollment-certificate`，status=`unknown`，claims=0，checklist=10，next_slot=`-`，14ms；trace=planner→flow→finalize

## 边界

- 本文件是 dev_v0 之外的流程/混合补充证据，不替代 20 题 dev_v0 主 run；
- 生产 MCP 路径为 policy-search + policy-meta stdio；本地 flow registry 仅用于离线评测；
- W6 不修改 W7 冻结集/标签/报告，W7 仍为 failed/tagged。
