# Bug定位与修复方案

日期：2026-10-27。状态：已完成只读诊断和方案设计；业务代码、配置、语料、索引及正在运行的7860服务均未修改或重启。

本文件补充并更新上一份100题测试报告中“底层原因未确定”的结论。原始100题结果保留，后续少量诊断请求不混入100题统计。

## 结论与证据强度

最优先修复的是：**常驻policy-search进程的检索请求被送往127.0.0.1:7890代理，而本地Qdrant位于6333；数据库HTTP客户端没有显式隔离代理继承。** 这是94题INDEX_UNAVAILABLE的主要故障定位。检索恢复后仍有文档类型、公开受众过滤、意图与槽位、澄清、历史正则及完成状态等独立问题，不能指望仅重启就让100题正常。

可直接确认的证据：

- 网页请求“农学院院长是谁”仍返回INDEX_UNAVAILABLE；同一请求期间，常驻policy-search实际工作进程PID 41544向127.0.0.1:7890发起连接，持续处于SYN_SENT。采样期间未观察到该查询连接6333。只统计该PID，未把其他应用的代理连接混入证据。
- Qdrant 6333端口正常、articles_v1为green、4,026点；全部payload可被当前Article模型还原。
- 新建隔离MCP实例在不改变现有网页服务的情况下，两次检索均成功。同一索引版本；“院长是谁”允许section后能命中agri-leadership。
- 独立实验在子进程内临时设置不可用代理，创建Qdrant客户端后恢复环境，再查询：默认客户端仍走创建时的代理并超时；同样创建时显式trust_env=False的客户端成功读取4,026点。未修改系统代理。
- 本机已安装HTTPX 0.28.1源码显示：trust_env默认True，初始化时构建并保存proxy mounts；其代理来源包括环境变量及Windows系统代理。Qdrant-client 1.19.1把kwargs传递给HTTPX。
- 当前读到的系统代理为空，当前新建默认客户端与显式直连客户端都正常；因此“现在关闭代理”不等于“已存在的客户端丢弃了创建时的代理路由”。

证据边界：**代理路径错误已观察到，旧代理缓存机制已独立复现；尚未记录常驻进程启动瞬间的系统代理快照，也未取得其被上层丢弃的完整ToolError原文。** 不声称知道是谁、在何时切换了代理。代理最初来自环境还是Windows系统设置属于来源推断，不影响对本地数据库连接进行代理隔离的修复方向。

关键证据文件：[连接采样](diagnostic_live_connections.json)、[客户端代理缓存复现](diagnostic_proxy_cache_repro.json)、[新MCP检索结果](diagnostic_fresh_mcp.json)、[HTTP传输实现核对](diagnostic_http_transport.json)、[进程白名单环境](diagnostic_process_environment.json)、[当前代理A/B](diagnostic_proxy_ab.json)。

## 已确认的代码问题

| 编号 | 优先级 | 问题与复现 | 代码位置 | 影响 |
|---|---|---|---|---|
| B01 | P0 | 常驻检索请求误走7890代理；Qdrant客户端未显式设定直连策略 | [policy_search_server.py:239](D:/big-third-up/campusguide-agent/src/campusguide/mcp/policy_search_server.py:239)、[runtime.py:71](D:/big-third-up/campusguide-agent/src/campusguide/qa/runtime.py:71) | 本轮94题最终索引失败，另有1道混合题检索失败 |
| B02 | P1 | 默认只检索article，师资、简介、活动等section被排除；100题中45题至少有一份预期来源被此规则排除 | [graph.py:88](D:/big-third-up/campusguide-agent/src/campusguide/agents/graph.py:88)、[section_select.py:20](D:/big-third-up/campusguide-agent/src/campusguide/retrieval/section_select.py:20) | 即便连接恢复，很多事实和活动问题仍检索不到正确来源 |
| B03 | P1 | 把题目里的“本科生/硕士研究生”等描述当作提问人身份；硬过滤只允许该身份或all，排除了public；后续scope也同样排除public | [planner.py:173](D:/big-third-up/campusguide-agent/src/campusguide/agents/planner.py:173)、[filters.py:129](D:/big-third-up/campusguide-agent/src/campusguide/retrieval/filters.py:129)、[scope.py:123](D:/big-third-up/campusguide-agent/src/campusguide/verification/scope.py:123) | 公开新闻被过滤；只修section仍无法召回 |
| B04 | P1 | “奖学金”通用别名触发国家奖助学金流程，“何时发布”没有识别为事实查询 | [planner.py:40](D:/big-third-up/campusguide-agent/src/campusguide/agents/planner.py:40)、[planner.py:412](D:/big-third-up/campusguide-agent/src/campusguide/agents/planner.py:412)、[scholarship-application.yaml:14](D:/big-third-up/campusguide-agent/configs/flows/scholarship-application.yaml:14) | 057题问袁隆平追梦奖学金公示日期，却返回国家奖助学金流程 |
| B05 | P1 | 多奖项问题用单值award_type表示；按别名长度只选一个 | [slots.py:117](D:/big-third-up/campusguide-agent/src/campusguide/flows/slots.py:117) | 089题同时问助学金与励志奖学金，仅留下inspirational |
| B06 | P1 | 同时含本科、研究生及“分别/区别”等字样就追问单一身份 | [planner.py:640](D:/big-third-up/campusguide-agent/src/campusguide/agents/planner.py:640) | 018领导分工、055跨人群证明办理被提前截断 |
| B07 | P1 | 年份+办法等字样一律视作历史意图，未核对当年与active版本 | [guard.py:17](D:/big-third-up/campusguide-agent/src/campusguide/qa/guard.py:17) | 035现行2026年奖学金办法被拒；正则实际命中片段已保存 |
| B08 | P2 | 有流程清单就可按closed及缺槽情况给ANSWERED，未确认原问题事实完整性 | [graph.py:632](D:/big-third-up/campusguide-agent/src/campusguide/agents/graph.py:632) | 054有检索失败仍ANSWERED；057和089答非所问或漏答仍ANSWERED |
| B09 | P1 | 预期检索异常映射为通用INDEX_UNAVAILABLE，Graph只保存错误码，页面提示将原因归结为未启动/未构建 | [policy_search_server.py:145](D:/big-third-up/campusguide-agent/src/campusguide/mcp/policy_search_server.py:145)、[retriever.py:232](D:/big-third-up/campusguide-agent/src/campusguide/agents/retriever.py:232) | 网络/代理故障被掩盖，定位成本高，用户提示失真 |

B02、B03是独立检索条件及语料元数据验证出的确定性缺陷，其对原100题的实际成功率影响需修复连接后复测，不把45题额外加到94题上。

B03补充实测：在Qdrant中固定预期文档和article+section范围，“数学与信息学院2026年10月20日……硕士研究生招生”被推断为graduate后，info-recent-news可检索块数从68降为0；“农学院2026年10月……本科生军训”被推断为undergraduate后，agri-recent-news从33降为0。两组均只改变受众条件。见[受众过滤对照](diagnostic_public_audience.json)。

## 修复顺序与具体设计

### 第一批：连接、错误诊断与实例更新（B01、B09）

统一MCP、QA runtime及索引脚本中的Qdrant创建策略。本地回环和容器内数据库连接明确不继承系统代理，HTTP客户端传入trust_env=False；如确有远端企业代理需求，应通过显式数据库专用配置启用，而不是隐式复用桌面系统代理。不要全局清空HTTP_PROXY或关闭所有客户端的trust_env，以免影响模型服务与其他外网请求。仅改URL为127.0.0.1不能确保绕过代理。

现有客户端代理映射在创建时形成，因此代码修复后必须重新创建Qdrant连接和常驻policy-search实例；通过受控重启应用及其MCP子进程加载新代码。只在磁盘改源码不会更新旧连接。当前语料和4,026点索引都正常，无需重建向量索引或重下模型。重启属于后续实施步骤，本轮未执行。

服务端对预期异常也记录结构化诊断：操作、实际数据库目标、直连/代理策略、底层异常类型和经脱敏的原因、trace_id；Graph保留可定位的错误摘要，前端显示“检索服务连接失败”等准确分类，不把所有网络错误解释为无索引。日志不包含API密钥、带认证信息的URL或完整用户隐私。可以保留现有公共outcome兼容性，先增加internal cause，避免第一批引入不必要的协议变更。

启动检查增加一次真实Qdrant读取和一条代表性检索；embedding/reranker warmup成功不代表数据库路径成功。若当前连接失效，不应仅靠无限重试或自动重建索引掩盖问题；连接重建及重试须有次数上限。

验收：带不可用代理设置创建本地数据库客户端仍可读取4,026点、get_article成功、制度检索有结果；打开/关闭系统代理不改变本地Qdrant目标路径；线上查询不再连接7890。故意断开数据库时日志能定位原始原因，最终输出安全降级。

### 第二批：覆盖整份知识库的检索范围（B02、B03）

按知识查询覆盖的实际语料类型允许article与section共同参与召回；优先用现有block_type、doc_type和authority_level做排序与配额。普通制度题仍可优先article，课程、教师、学院事实与活动题允许section；不要继续只在课程词表中逐个补“院长/梁云/讲座”之类实例。保留表格与叙述section的选择规则，并按任务安排配额，避免宽泛开放后新闻淹没制度条款。

定义public为可被各身份引用的公开信息证据，在Qdrant受众条件和后置scope中采用一致语义；仍保留学院、专业、入学年份、有效版本等明确适用限制。区分“用户本人身份”和“查询对象里出现的本科生/研究生”；公开新闻事实查询无需把关键词当成用户身份。不能只放开召回而让scope再次剔除同一公开证据。

验收：45题的预期section来源不再被类型条件先验排除；上述两道新闻受众A/B不再从68/33归零；本科生查询公开学院简介能够引用public，但本科/研究生专属政策不会被互相套用。对院长、教师归属、专业数量、2024级人工智能选修组和近期讲座逐一验证关键证据进入最终上下文，而不只检查“返回非空”。

### 第三批：事实咨询、办理流程与多对象问题（B04、B05、B06）

先识别查询动作与对象，再决定是否进入办理流程。明确区分“查询公示发布日期/讲座主题/领导分工”和“替本人申请/办理”；奖学金这个名词不能单独决定流程。事实意图覆盖“谁、哪位、何时发布、哪天、发布了哪些、公告、公示”等表达，但设计必须结合句意，而不是无限扩展词表。明确命中的国家奖助类别可以进入对应流程，未命名清楚的社会奖学金不应回落成国家奖学金。

多项目事实比较拆为独立子查询，例如089分别查国家助学金与国家励志奖学金，再合并作答。保留单个办理流程award_type的单值模型，不让一个流程槽位静默吞掉多个项目；若用户确实要同时办理两项，明确创建独立流程或要求选择事项。不要在所有现有流程中直接把单值槽位改成list，避免破坏when表达式。

只有个人办理结果依赖未知身份时才澄清；公开事实和明确要求跨群体比较时，直接按群体分别作答。018应给出三项领导分工；055应分别说明本、研办理及已知收费，缺少的研究生收费交由负责部门确认，不套用本科规则。

验收：057直接回答2026-10-09；089同时覆盖两份公示及其公示期；018无身份追问；055不强制二选一。保留“国家奖学金怎么申请”“我要开在读证明”等真实办理流程及必要澄清行为。

### 第四批：日期版本与最终完成判定（B07、B08）

将历史意图拆成“明确查询废止/旧版规则”和“只带年份的有效文件名称”。保留隐私与校外guard；只有明确旧版意图时可提前按产品能力拒答。对当年或特定入学年份的文件，先查询版本元信息与适用对象，再决定适用性；2022与2024培养方案可能同时对各自年级有效，不能因年份较早就判断废止。并列比较需保持每个结论对应的版本来源，不把不同年级学分混在一起。

把“流程清单可以展示”和“原问题已回答”分开。纯流程closed信息可按现行规则显示；混合题必须核对事实子问题coverage和检索失败情况。存在未回答的事实部分时应为PARTIAL_ANSWER，且明确哪些部分未完成；仅有无关流程不能算ANSWERED。保留054题中已被语料支持的材料和截止时间，不应为了状态收紧而删掉仍有确定性来源的有用信息。

验收：035进入现行v2026查询；“请比较2009旧版和2021版学籍规定”仍走明确的历史能力判断；2024入学方案选择准确。对“流程正常+事实检索失败”注入故障时，最终状态反映未完成子问题，不能因closed批次就整体ANSWERED。

## 复测与交付方案

| 阶段 | 检查范围 | 通过条件 |
|---|---|---|
| A：确定性回归 | 代理继承与失效缓存、public与角色过滤、section范围、历史识别、事实/流程路由、多项目拆分、完成状态 | 每个缺陷有能在修复前失败、修复后通过的最小回归；保留本科/研究生隔离、隐私、校外与旧版对照用例 |
| B：真实服务冒烟 | 院长、梁云、专业数、2024计算机AI选修组、近期讲座，以及018/035/054/055/057/089代表题 | 正确文档进入证据；事实有来源；必要时进入Advisor/Verifier；非预期代理连接为0 |
| C：100题串行复测 | 固定原始题库、as_of与语料快照、独立会话 | 100题均有最终结果；INDEX_UNAVAILABLE=0；同时逐题审查答非所问、遗漏、误拒、版本和日期，而不是只看ANSWERED比例 |
| D：评测标注 | 为100题补齐标准事实、适用版本与可接受拒答 | 单独统计完整正确、部分正确、错误、合理拒答、运行失败；明确样本分母；新结果与原始基线分目录保存 |

第C阶段不预设“100题全应给出肯定答案”。例如“农学院有多少实验室”在现有语料缺乏完整总量时，应明确统计口径与证据不足；活动汇总只有标题和发布日期，不可编造举办日期或正文内容。这些边界需要在标注中先定义。

交付建议按上述四批形成可独立审查的改动。第一批只验收可用性与诊断，不混入大幅提示词改写；第二批验证召回和受众语义；第三、四批处理路由与答复状态。每批完成后保存针对性结果，最终运行项目要求的lint与相关单元/契约测试，再执行同接口100题真实复测。不要以提高ANSWERED比例为目标放松引用和语义校验。

## 本轮未做的修改

没有改业务源码、配置或索引，也没有重启现有应用。新增的内容只有诊断脚本、诊断结果与本方案。新MCP实例为隔离诊断，已按脚本关闭；它的成功结果不替换原先100题的失败记录。
