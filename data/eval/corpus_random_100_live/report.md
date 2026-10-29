# 知识库100题真实服务测试报告

> 后续诊断更新（2026-10-27）：已观察到常驻检索进程误连7890代理端口，并复现HTTP客户端的代理缓存行为；另确认public受众过滤问题。详见[Bug定位与修复方案](bug_diagnosis_and_fix_plan.md)。本报告下方保留100题原始评测时点的结论。

测试日期：2026-10-27（Asia/Shanghai）；运行时间：21:14:27—21:23:08。

100题已逐条完成。当前服务首先存在检索链路故障：94题最终返回 INDEX_UNAVAILABLE；其余6题中，1题核心内容正确、1题部分正确、1题答非所问、2题错误澄清、1题把现行办法误判为历史版本。**目前不宜把本轮作为模型正常问答准确率评测，也不能把系统3次ANSWERED直接算成3题答对。**

## 测试方法与范围

通过 `gradio_client` 请求已经运行的 `http://127.0.0.1:7860/` 的 `/on_send`，与网页输入提交调用相同的UI回调、会话处理及真实后端。严格按题库顺序串行，每题传入空BrowserState建立独立对话；只记录首轮最终回答，不补充用户身份、不改写问题、不自动重试。未用fake答案、未替换检索后端；本轮不包含浏览器DOM点击、视觉布局或多轮完成率测试。

题库为本轮之前生成的100题，含126份不同文档的来源映射、15题近期活动与消息。它是探索性题集，没有事先人工冻结的完整标准答案；报告对可见的6道非基础设施输出作语料核对，94道基础设施故障单列。近期目录语料只保存标题、发布日期与链接，不能据此推断活动举办时间或正文细节。

页面调试信息：model_profile=`vpro`；模型配置为 `{'planner': 'gemini-3.5-flash-lite', 'advisor': 'gemini-3.5-flash-lite', 'semantic': 'gemini-3.5-flash-lite'}`；索引版本 `articles_v1@hybrid-6bafb68410b9`。这记录的是配置，不代表Advisor/Verifier实际被调用。

本次只新增测试脚本与结果文件，未修改业务源码、配置、语料或向量索引，未重启正在运行的网页服务。工作区此前已有 aliases.yaml、graph.py、section_select.py 及相关单元测试改动，本次保留。代码定位与静态风险以报告生成时工作区为准，当前进程在启动时加载的代码可能早于这些改动。

## 结果统计

| 指标 | 结果 |
|---|---:|
| 完成请求 | 100/100 |
| 最终INDEX_UNAVAILABLE | 94 |
| 最终CLARIFY | 2 |
| 最终HISTORICAL_NOT_SUPPORTED | 1 |
| 系统标记ANSWERED | 3 |
| 人工核对核心正确 | 1 |
| 人工核对部分正确 | 1 |
| 人工核对答非所问 | 1 |
| 请求耗时p50 / p95 / 最大值 | 5.35s / 8.36s / 10.11s |
| 请求耗时累计 / 测试墙钟时长 | 515.76s / 521.07s |
| 进入Retriever / Advisor / Verifier的题数 | 95 / 0 / 0 |

p95采用nearest-rank。耗时包含Gradio排队、回调与客户端开销；大量请求在生成前终止，因此这些延迟不能用作正常回答速度指标。95次Retriever均未返回有效证据，其中1次出现在最终仍标为ANSWERED的混合流程题054。

| 题目范围 | 总题数 | 索引失败 | 其他结果 |
|---|---:|---:|---|
| 学籍与教学管理 | 20 | 20 | 无 |
| 培养方案 | 20 | 20 | 无 |
| 研究生培养与办事 | 10 | 9 | CLARIFY 1 |
| 学院与师资 | 20 | 19 | CLARIFY 1 |
| 近期活动与消息 | 15 | 14 | ANSWERED 1 |
| 奖助评优与推免 | 15 | 12 | HISTORICAL_NOT_SUPPORTED 1、ANSWERED 2 |

## 发现与原因定位

### P0：正在运行的检索链路持续失败，尚不能确认具体底层异常

94道最终失败题均呈现 Planner → Retriever → Finalize，evidence=0，warnings含 qN:INDEX_UNAVAILABLE，未进入Advisor或Verifier。额外的054也有同类检索失败，但流程清单仍令最终状态成为ANSWERED。

独立只读诊断取得的反证：Qdrant返回HTTP 200，articles_v1为green、4,026点；所有4,026个payload可被当前Article模型正常还原，其中article 1,366、section 2,660。独立创建同配置检索服务可以完成构建、本地模型预热与一次真实检索，返回ok=true和相同index_version。这排除了“本机Qdrant完全没启动”“集合没构建”“当前数据全部不可解析”作为统一解释；不证明网页常驻MCP进程也连接到了同一目标、具有相同环境或处于正常状态。该独立查询只用于可用性诊断，不计入100题正确率，也未验证其返回结果能正确回答院长问题。

错误传播定位：[src/campusguide/mcp/policy_search_server.py:145](D:/big-third-up/campusguide-agent/src/campusguide/mcp/policy_search_server.py:145) 将RetrievalError等映射为INDEX_UNAVAILABLE；[src/campusguide/agents/retriever.py:232](D:/big-third-up/campusguide-agent/src/campusguide/agents/retriever.py:232) 对全部查询失败只保留query_id与错误码，未将原始异常消息写入本轮trace。页面据此统一提示“Qdrant未启动或索引未构建”，具体原因不足。

已确认的是网页当前检索路径失败；尚未确认的是常驻MCP原始异常及具体故障点。应用实际运行在Windows本地Python进程，docker compose的app日志为空。应首先读取该应用启动终端的policy-search stderr，核对其Qdrant连接目标、工作目录、环境、模型状态与异常堆栈，再决定是否重启或修复；本轮没有进行这些修改。证据见diagnostic_backend.json、diagnostic_payloads.json和matched_server_traces.jsonl。

### P1：事实型新闻问题被“奖学金”通用别名误路由

**057题**问“袁隆平追梦奖学金公示何时发布”，语料明确登记2026-10-09，实际却输出本科生国家奖助学金申请清单，没有回答日期。

[configs/flows/scholarship-application.yaml:14](D:/big-third-up/campusguide-agent/configs/flows/scholarship-application.yaml:14) 存在通用别名“奖学金”；[src/campusguide/flows/registry.py:84](D:/big-third-up/campusguide-agent/src/campusguide/flows/registry.py:84) 按子串匹配流程；[src/campusguide/agents/planner.py:40](D:/big-third-up/campusguide-agent/src/campusguide/agents/planner.py:40) 的事实词规则不包含“何时发布”；[src/campusguide/agents/planner.py:437](D:/big-third-up/campusguide-agent/src/campusguide/agents/planner.py:437) 在命中流程且没有识别到事实意图时默认走流程。diagnostic_rules.json复现procedure_flag=false、fact_flag=false而命中国家奖助学金流程。

### P1：同一问题含两类奖助项目时只保留一个槽位

**089题**要求分别介绍国家助学金和国家励志奖学金公示，实际只保留励志奖学金相关清单，漏掉助学金。语料对应公示期分别为2026-10-21—10-22与2026-10-23—10-24。

此问同样被视为流程；award_type是单值enum，[src/campusguide/flows/slots.py:117](D:/big-third-up/campusguide-agent/src/campusguide/flows/slots.py:117) 收集命中的别名后按长度排序只取一个，独立复现抽取值为inspirational。多项目事实咨询缺乏逐项拆分，既造成遗漏，也引入大量无关办理信息。

### P1：公开事实与跨人群比较被无条件追问个人身份

**018题**问三位领导分工，**055题**要求分别说明本科与研究生证明办理，两题均只得到“你是本科生还是研究生”。018的正确分工可直接从agri-leadership读到：王少奎负责本科教学、谢庆军负责研究生、梁志辉负责实验室安全。055本应分别解释两个对象的规则。

[src/campusguide/agents/planner.py:640](D:/big-third-up/campusguide-agent/src/campusguide/agents/planner.py:640) 只要同时出现“本科/研究生”及“分别/区别/不同/差异”，在无个人profile时就返回AUDIENCE_UNKNOWN；未区分公开事实查询、多对象比较和个人办理咨询。两题在进入检索前即被截断，diagnostic_rules.json可独立复现。

### P1：带当年年份的现行办法被历史版本正则误拒

**035题**查询2026年研究生奖学金评选办法和补充说明，却得到HISTORICAL_NOT_SUPPORTED。两份预期文档在语料中均存在现行v2026。

[src/campusguide/qa/guard.py:17](D:/big-third-up/campusguide-agent/src/campusguide/qa/guard.py:17) 将“年份+年+最多8字符+办法/政策/规定/版”等全部视为历史意图，实际命中完整片段“2026年研究生奖学金评选办法”，没有结合当前年份、版本状态或比较意图判断。此问题在Planner guard阶段发生，与Qdrant不可用无关。

### P1：默认仅查article会排除教师、学院与活动语料（静态确认）

对100题按当前工作区 `_default_block_types` 与预先映射的语料类型交叉检查，**45题**存在：至少一份预期文档只有section，但默认检索仅允许article。[src/campusguide/agents/graph.py:88](D:/big-third-up/campusguide-agent/src/campusguide/agents/graph.py:88) 仅对allows_sections命中问题加入section；[src/campusguide/retrieval/section_select.py:21](D:/big-third-up/campusguide-agent/src/campusguide/retrieval/section_select.py:21) 主要识别培养方案、课程、学分等词，没有覆盖师资、院长、学院简介和大多数活动新闻意图。

典型包括“农学院院长是谁”“梁云属于哪个系”“近期论坛何时发布”。这是一项代码和语料元数据交叉验证出的潜在召回阻断，不计作本轮已经复现的独立线上检索错误；当前基础设施故障优先发生，无法用本轮结果量化其实际影响。完整题目及被排除文档见static_section_risks.json。

### P2：ANSWERED状态不能代表题意已完整满足，混合流程还可能掩盖检索失败

**054题**材料和截止时间与语料一致，记核心正确；但其检索evidence=0，Advisor/Verifier未运行，答案来自确定性流程。057、089也未运行这两个节点，却都标ANSWERED。

[src/campusguide/agents/graph.py:632](D:/big-third-up/campusguide-agent/src/campusguide/agents/graph.py:632) 对已有flow/checklist的结果以缺槽及closed状态决定ANSWERED或PARTIAL_ANSWER，未以原问题事实覆盖完整性决定。建议后续把“流程可渲染”“检索成功”“问题已完整答复”分别判断；本报告没有修改实现。

## 逐题记录与复核边界

完整的100题问题、实际最终答案、人工判断、预期行为、参考语料片段和session_id见[case_details.md](case_details.md)；review.jsonl为同内容的结构化版本。results.jsonl保留全部原始Gradio输出（含BrowserState、答案、引用、清单及调试HTML），matched_server_traces.jsonl保留按本轮session_id筛选的服务端轨迹，避免混入其他用户或历史测试。

本轮最终答案的顶层claim citations共0条；流程checklist中引用ID提及75次，其中不存在于语料的ID为0个。这只能说明预置流程引用ID存在，不表示引用已被线上水合、更不表示答案语义受到证据支持；不报告100%引用支持率。

“农学院有多少个实验室”这题的语料主要列举重点平台，未提供所有实验室的完整总数；恢复检索后应核对统计口径并在不足时明确说明，不能强行列为必须给出数字的可答题。跨年份培养方案题也应先核对业务是否支持并列现行入学年级，不能仅因年份不同就默认历史文件已废止。

## 后续处理顺序（仅建议，尚未修改）

先取得网页常驻policy-search原始异常，恢复运行链路并用本轮同一题库复测；随后检查section范围、事实/流程意图和多项目拆解，再调整身份澄清与历史正则。保留本轮原始结果作为故障基线，新一轮写入不同目录；至少对100题逐项标注标准事实或可接受拒答条件后，再统计完整正确率、部分正确率、召回与引用支持。

本轮未覆盖正常生成、语义核对质量和多轮办理完成率，因为没有任何题进入Advisor或Verifier。不能据此评价模型本身好坏，也不能根据失败拒答数量推断知识库覆盖率。

## 复现与产物

运行环境使用项目现有.venv。可在仓库根目录执行：

```powershell
.venv\Scripts\python.exe scripts/eval_corpus_random_100_live.py
.venv\Scripts\python.exe scripts/report_corpus_random_100_live.py
```

测试脚本遇到已完成id会跳过，不重复消耗请求；如需新一轮应先改用独立输出目录。diagnose_corpus_random_100.py为独立服务诊断脚本；原诊断在HF_HUB_OFFLINE=1及TRANSFORMERS_OFFLINE=1下执行，只读取现有本地模型缓存。报告脚本只读取结果、语料和只读Qdrant元信息。

本报告、逐题详单、summary.json、review.jsonl、运行配置、源代码哈希和诊断结果均在本目录。所有100条结果已检查唯一ID、与输入顺序一致、服务端trace匹配及对应源文档存在。
