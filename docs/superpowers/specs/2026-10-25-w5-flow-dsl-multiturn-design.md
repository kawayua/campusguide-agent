# CampusGuide W5 设计：流程 DSL 与多轮槽位

| 项目 | 内容 |
|---|---|
| 文档版本 | 0.2（设计稿，2026-10-25 范围裁决） |
| 日期 | 2026-10-25 |
| 基线 | W7 已终止并打 tag `w7`（W7 总门禁未通过）；W4 MCP/混合检索稳定；435 tests |
| 范围 | YAML 流程 DSL、受限三值谓词、三个首版流程、多轮槽位、checklist 渲染、`closed` 截止态 |
| 阶段关系 | **W5 是 W7 之后的独立能力**；不修改 W7 冻结测试集/报告，也不把 W5 流程指标写进 W7 结果 |
| 首版边界 | 奖学金只做**国家级**三奖；院级综合/单项/社会奖只 handoff；在读证明本科生完整流程、研究生导航 handoff |
| 状态 | 已实现（2026-10-25）；验收与已知限制见 [W5 周记录](../../weekly/w5.md) |

---

## 0. 决策摘要

W5 做五件事：

1. **流程数据化**：三个流程由 YAML 定义，步骤、材料、时限、部门、引用条款都来自数据；
2. **三值条件引擎**：`true / false / unknown` 的受限表达式，不使用 `eval`；
3. **多轮槽位**：优先追问、单槽位追问、更正重算、拒答记录、澄清上限；
4. **确定性 checklist**：DAG 拓扑排序、阻塞/条件/不适用状态、依据条款和人工入口；
5. **MCP 化**：`policy-meta` 增加 `list_flows` / `get_flow` / `get_calendar`；应用不直接读流程 YAML。

W5 **不依赖 LLM API**：

- 槽位抽取先用确定性规则；
- 流程匹配用别名/标题/关键词；
- 解释文案由模板生成；
- 不把 LLM 作为流程事实来源。

LLM 槽位解析/多问拆解留 W6；W5 即使不接 LLM，也必须能通过脚本化多轮验收。

### 0.1 W7 边界

W7 已经终止（tag `w7`，总门禁未通过），其冻结测试集、报告和失败结论不再修改。
W5 是后加的流程能力：

- 不重跑、不覆盖 W7；
- W5 的流程验收单独记录；
- README 必须写“W5 不在 W7 测试集覆盖范围内”，不能据此声称 W7 通过；
- W7 的 answer/version/p95 失败仍是项目已知限制。

### 0.2 2026-10-25 范围裁决（本版新增）

1. **奖学金首版只做国家级**：国家奖学金、国家励志奖学金、国家助学金。
   - 院级综合奖、单项奖、社会奖不进入 W5 流程事实；只输出“看学院综测/评优通知”的 handoff。
   - 综合测评仍是国家级奖学金的前置条件，但综测成绩/拟获奖名单不作为国家级奖项事实。
2. **新增 `closed` 状态**：截止日期已过的步骤、以及已有公开证据证明本批次已结束的步骤，标记为 `closed`，不显示“现在可办理”。
   - 不把旧批次日期套到新批次；没有新通知时用 `deadline_note`，不猜下一年日期。
   - `closed` 是步骤状态；流程整体状态由步骤聚合为 `open / partial / closed / unknown`。
3. **在读证明改为双轨**：
   - 本科生：按教务处主页面 + 附件1 + 自助打印指南做**完整流程**，可写死材料、费用、地点、时间和领取方式；
   - 研究生：只做**导航 + 研究生院培养科 handoff**，材料/费用/时限以培养科确认为准；
   - 不再把整个在读证明流程标成 `navigation_only`。
4. **2026 奖学金当前批次已全部截止**（截至 2026-10-25）：
   - 数信国奖 10-10 09:00 截止，励志 10-21、助学金 10-16；
   - 公管国奖 10-05 24:00 截止，励志 10-19、助学金 10-10；
   - 农学院 2026 励志/助学金拟推荐名单已分别于 10-23、10-21 公示，但未检索到 2026 本科生评审通知，具体截止日不写死。
   - 因此 `as_of=2026-10-25` 时应输出 `closed`，而不是继续追问年级后给“现在去申请”。
5. **两个学院的支持口径**：
   - 数信：2026 国奖通知 + 2026 励志/助学金通知，作为当前批次日期与材料的直接来源；
   - 农学院：2026 励志/助学金**公示元数据**（不收录姓名/学号）作为“批次已结束”的证据；2026 国奖未检索到公开通知，标 `unknown` + 学院 handoff；
   - 公管：只作金额/程序口径的交叉核对，不进入 W5 首版流程 YAML，避免把“三个学院”写成首版承诺。

---

## 1. 当前基线与数据缺口

### 1.1 现有

- W4 `policy-search` / `policy-meta` MCP、hybrid 检索、受众/时间过滤；
- W3 `VerificationIssue` / `PARTIAL_ANSWER` 契约；
- 领域模型已有 `FlowDefinition`、`FlowStep`、`Prerequisite`、`SlotDefinition`、`FlowInstance`、`CalendarEvent`；
- `configs/flows/` 三个空骨架：`scholarship-application`、`recommendation-eligibility`、`enrollment-certificate`；
- `flows/` 模块目前只是占位文件；
- 435 个离线测试，W7 结束。

### 1.2 数据缺口与已确认来源（2026-10-25 更新）

1. **三个 flow 的 `flow_ids` 仍为空**：W5 要显式写入关联条款，不能靠运行时猜。
2. **在读证明已有公开依据，但分本科/研究生两套**：
   - 本科生：教务处主页面 + 附件1《关于办理出国成绩单及学历学位证明的说明》（2026-03-03）+《本科生自助打印服务指南》（2024-11-05）；
   - 研究生：研究生院培养科职责页 + 证明样本页，无独立办事长文；只做 handoff，不写死材料/费用/时限。
3. **奖学金当前批次来源已补齐**：
   - 数信 2026 国奖通知（2026-10-09）：国奖 10000 元、18 人、10-10 09:00 截止；
   - 数信 2026 励志/助学金通知（2026-10-13）：励志 6000 元、10-21 截止；助学金 2500–5000 元、10-16 截止；
   - 公管 2026 国奖通知（2026-09-30）：10000 元、11 人、10-05 24:00 截止；励志 10-19、助学金 10-10；
   - 农学院 2026 励志/助学金公示（2026-10-23 / 10-21）：只取标题、日期、公示期，不收录姓名/学号；2026 国奖未检索到公开通知。
4. **推免来源已补齐**：2027 推荐阶段通知（2026-08-29）+ 拟获资格名单公示（9/12–9/18）+ 递补通告；推荐阶段已于 9/18 结束，当前同样进入 `closed`。
5. **校历/截止日**：新增 `configs/calendar.yaml`，只登记有公开来源的事件；第二学期校历表头为“征求意见稿”，春夏日期标注“以定稿为准”。
6. **失效链接**：`https://jwc.scau.edu.cn/2018/1112/c5171a146530/page.htm` 已 410/404，不得引用；模板入口改用主页面或模板 zip。
7. **`.doc` / `.docx` 不能直接解析**：附件1与自助打印指南使用本地 HTML 转录（`local_path` + 官网 `url`），引用仍指向官网原文；不把模板 zip 中的个人样例（姓名/生日/学号）入库。
8. **含个人信息的公示页**：数信 10-23 综合测评拟获奖名单、农学院 2026 励志/助学金公示均含姓名；只登记**脱敏元数据转录**，不索引原始名单。综合测评名单不进入国家级奖学金流程事实。

---

## 2. 总体架构

```text
CLI / UI（W8）
    │
ConversationService（W5 新增）
    ├─ FlowMatcher（别名/标题/关键词）
    ├─ FlowEngine（YAML + 三值谓词 + DAG）
    ├─ SlotExtractor（确定性规则）
    └─ QAService（现有单轮政策问答，fallback）
             │
        MCP policy-meta（W5 新增 flow/calendar tools）
        MCP policy-search（现有条款检索，用于引用校验/政策问答）
```

### 2.1 为什么不在 W5 上 LangGraph

W5 的状态机是有限、可枚举的：

```text
idle → matched → need_slot* → ready → rendered
                    ↑             │
                    └── corrected ┘
```

先用 `ConversationService` + 显式 session state 实现；W6 再把同一批节点包装进 LangGraph。W5 不引入 checkpointer 或图回退。

### 2.2 模块边界

```text
src/campusguide/flows/
  dsl.py          # FlowDefinition 加载/校验（复用 domain models）
  predicates.py   # tokenizer/AST/三值求值
  slots.py        # SlotExtractor、槽位校验、更正
  engine.py       # DAG、前置条件、checklist 状态
  registry.py     # 加载 configs/flows、版本选择
  matcher.py      # 别名/标题/关键词匹配
  checklist.py    # ChecklistItem 渲染
src/campusguide/conversation/
  service.py      # 多轮会话路由、状态、澄清上限
  session.py      # 内存/单进程 session store
src/campusguide/mcp/
  policy_meta_server.py  # 增加 flow/calendar tools
scripts/
  chat.py         # 交互式 CLI
  flow_demo.py    # 三条脚本化多轮 demo
tests/
  unit/test_w5_predicates.py
  unit/test_w5_slots.py
  unit/test_w5_flow_engine.py
  unit/test_w5_matcher.py
  unit/test_w5_conversation.py
  contract/test_w5_meta_flows.py
```

---

## 3. YAML 流程契约

### 3.1 现有模型扩展

复用 `FlowDefinition` / `FlowStep` / `Prerequisite` / `SlotDefinition`，补充：

```python
class FlowStep(BaseModel):
    ...
    citation_exempt: bool = False    # 纯导航步骤显式豁免依据
    deadline_note: str | None = None  # 没有可靠日期时的公开提示
    closed_observed_at: date | None = None  # 公示等公开证据证明本批次已结束

class FlowInstance(BaseModel):
    ...
    # step_states 的 value 增加 "closed"
    step_states: dict[str, Literal[
        "available", "blocked", "conditional", "done", "not_applicable", "closed"
    ]] = {}
    asked_slots: list[str] = []
    declined_slots: list[str] = []
    pending_slot: str | None = None
    clarify_count: int = 0
    done_step_ids: list[str] = []
    last_question: str | None = None
    batch_label: str | None = None
    status: Literal["open", "partial", "closed", "unknown"] = "unknown"

class ChecklistItem(BaseModel):
    step_id: str
    title: str
    status: Literal[
        "available", "blocked", "conditional", "done", "not_applicable", "closed"
    ]
    description: str
    materials: list[str]
    deadline: str | None          # ISO date 或 datetime（带 +08:00）
    deadline_note: str | None
    department: str
    citations: list[str]
    blocked_reason: str | None
    status_note: str | None       # closed 的原因/下次看哪里
```

`FlowDefinition` 增加：

```python
navigation_only: bool = False
batch_label: str | None = None   # 如“2026 年本科生国家奖助学金批次”
```

`navigation_only` 保留给纯 handoff 流程；**在读证明不再使用它**。若为 `true`：

- 所有步骤必须 `citation_exempt: true`；
- 只能输出导航/入口信息，不能输出资格判断、材料、时限或收费事实；
- 加载校验拒绝“navigation_only 但有制度事实”的流程。

约束：

- 每个步骤必须有 `responsible_department`；
- 每个步骤至少有 1 条 citation，除非 `citation_exempt: true`；
- `citation_exempt` 只允许导航/入口步骤；可含部门名与 handoff URL，不允许带材料/时限/收费/资格判断；
- `deadline` 与 `deadline_note` 二者至少有一个；`deadline` 用 ISO date 或带时区的 ISO datetime；
- **日期截止口径**：date-only 的 deadline 视为当天 23:59:59（Asia/Shanghai）；有具体时刻的写 datetime；比较用注入的 `as_of`，测试不读系统时钟；
- `closed_observed_at` 必须有公开来源（公示/名单页），且 `deadline_note` 说明“具体截止日未公开”；不能凭猜测写；
- `closed` 优先于 `blocked/conditional`；已 closed 的步骤不再追问槽位；
- `citation_exempt` 的步骤不能出现在“资格判断”中。

### 3.2 三个流程的初始数据

#### 奖学金申请（首版只做国家级三奖）

- `flow_id: scholarship-application`
- `batch_label: 2026 年本科生国家奖助学金批次`
- audience: `undergraduate`
- slots:
  - `college`（enum：数学与信息学院、软件学院 / 公共管理学院 / 农学院 / 其他；required）
  - `award_type`（enum：国家奖学金 / 国家励志奖学金 / 国家助学金；optional；未填时展示三奖对比）
  - `grade`（integer，1–4；required；国奖/励志要求二年级及以上）
  - `has_disciplinary_record`（boolean，required）
  - `has_financial_aid`（boolean，optional；**不强制追问**，unknown 时按条件性说明处理）
- 范围：
  - 只写国家级三奖的资格、金额、程序、材料与截止；
  - 院级综合奖/单项奖/社会奖不进步骤，只 handoff 学院学工办/综测通知；
  - 综合测评是前置基础，但综测拟获奖名单不是国家级奖项事实。
- 制度 citations：
  - `national-aid-undergrad@v2021#第八条` 基本条件
  - `#第九条` 国家奖学金条件
  - `#第十条` 国家励志奖学金条件
  - `#第十二条` 申请方式
  - `#第十三条` 材料真实性
  - `#第十六条` 评审程序
- 2026 当前批次 citations（新增，需先入库）：
  - `national-aid-2026-info-merit`：数信国奖通知（2026-10-09）
  - `national-aid-2026-info-aid`：数信励志/助学金通知（2026-10-13）
  - `national-aid-2026-agri-inspirational-publicity`：农学院励志公示（2026-10-23，脱敏元数据）
  - `national-aid-2026-agri-grant-publicity`：农学院助学金公示（2026-10-21，脱敏元数据）

**2026 批次事实表（金额/名额/截止均按当年通知，不用 2021 的 8000 旧口径）**

> 公管行只用于交叉核对金额/程序，不进入首版 flow YAML；首版实际引用的是数信 + 农学院。

| 学院 | 奖项 | 金额 | 名额 | 学生申请截止 | 截至 2026-10-25 |
|---|---|---|---:|---|---|
| 数信 | 国家奖学金 | 10000 元/人/年 | 18 | 2026-10-10 09:00 | `closed` |
| 数信 | 国家励志奖学金 | 6000 元/人/年 | 见分配表 | 2026-10-21 | `closed` |
| 数信 | 国家助学金 | 2500–5000 元/人/年 | 按认定人数 | 2026-10-16 | `closed` |
| 公管（校对） | 国家奖学金 | 10000 元/人/年 | 11（全校 171） | 2026-10-05 24:00 | `closed` |
| 公管（校对） | 国家励志奖学金 | 6000 元/人/年 | 见分配表 | 2026-10-19 | `closed` |
| 公管（校对） | 国家助学金 | 2500–5000 元/人/年 | 按认定人数 | 2026-10-10 | `closed` |
| 农学院 | 国家励志奖学金 | 6000 元/人/年 | 未公开 | 未检索到 2026 通知 | `closed`（公示 10-23 起） |
| 农学院 | 国家助学金 | 2500–5000 元/人/年 | 未公开 | 未检索到 2026 通知 | `closed`（公示 10-21 起） |
| 农学院 | 国家奖学金 | 10000 元/人/年 | 未公开 | 未检索到 2026 通知 | `unknown` + handoff |

- 共同程序：学生申请 → 学院评审/公示 ≥3 个工作日 → 报学工部 → 学校评审/公示 5 个工作日 → 报省教育厅；
- 共同材料（具体以学院通知为准）：国家奖学金申请审批表 / 国家励志奖学金资助业务名单申请表 / 本专科国家助学金导入模板、成绩与综测证明；励志/助学金另需家庭经济困难认定；
- 国奖 2026 校额 171 人（公管通知披露，只作校对）；学院名额以学院通知/分配表为准；
- 农学院未检索到 2026 本科生评审通知，不能把 2024 通知的 10 月日期套到 2026；`deadline_note` 写“具体截止日未公开，以学院当年通知为准”；
- `as_of=2026-10-25`：整批 `closed`，不追问年级；`as_of=2026-09-30`：国奖/部分助学金仍 `open`，按学院/奖项追问；
- `as_of=2026-10-18`：数信国奖/助学金 `closed`、励志仍 `open` → 流程 `partial`，只对未截止奖项追问。

#### 推免资格确认

- `flow_id: recommendation-eligibility`
- `batch_label: 2027 年推免推荐阶段`
- audience: `undergraduate`
- slots:
  - `is_graduating`（boolean，required）
  - `has_recommendation_quota`（boolean，required）
  - `in_recommendation_list`（boolean，optional）
  - `grade`（integer，optional）
- 制度 citations：
  - `postgraduate-recommendation@v2025#第十条` 基本条件
  - `#第十一条` 学业要求
  - `#第十六条` 工作程序
  - `#第十七条` 自主选择志愿/报名
- 2027 节点 citations（新增，需先入库）：
  - `recommendation-2027-notice`：推荐阶段通知（2026-08-29）
  - `recommendation-2027-publicity`：拟获资格名单公示（2026-09-12）
- 2027 公开节点：
  - 通知 2026-08-29；拟获资格公示 2026-09-12～09-18；
  - 放弃资格书面申请截止 2026-09-16 12:00（行政楼 305）；
  - 推荐阶段结束 2026-09-18；名额正式 904 + 候补 8；研究生支教团正式 6 + 候补 6；
  - 接收阶段 9-18 后登录 `https://yz.chsi.com.cn/tm/`，由招生单位组织复试/待录取。
- 截至 2026-10-25，推荐阶段已结束：流程状态 `closed` 或 `partial`（仅接收阶段 handoff），不再追问“是否在名单”后给“现在申请”；
- 2028 批次未发布，不得推断日期；学院细则、接收复试只 handoff 学院，`deadline_note`。

#### 在读证明开具（本科生完整流程 + 研究生导航）

- `flow_id: enrollment-certificate`
- `batch_label: null`（常年可办，无批次）
- `navigation_only: false`
- audience: `undergraduate`, `graduate`
- slots:
  - `level`（enum：本科生 / 研究生；required）
  - `enrollment_status`（enum：在校生 / 已毕业；required）
  - `purpose`（enum：升学 / 就业 / 实习 / 出国留学 / 其他；required）
  - `delivery`（enum：自助打印 / 窗口领取 / 委托他人；required，本科生分支）
- citations（新增，需先入库）：
  - `enrollment-certificate-guide`：教务处主页面
  - `enrollment-certificate-fee-note-2026`：附件1《关于办理出国成绩单及学历学位证明的说明》本地 HTML 转录（URL 指向官网 `.doc`）
  - `enrollment-certificate-self-print-2024`：《本科生自助打印服务指南》本地 HTML 转录（URL 指向官网 `.docx`）
  - `enrollment-certificate-grad-office`：研究生院培养科职责页
  - `enrollment-certificate-grad-sample`：研究生证明样本页（可选）
- 本科生事实（可写死）：
  - **未毕业学生不办理学历证明，只出具在读证明**；已毕业走学历学位证明，不是在读证明；
  - 在校生 **5 份内免费**，累计超出 **5 元/份**；已毕业 **15 元/份**；收费，**自备现金**；
  - 窗口：行政楼 **305**，电话 **020-85280049**；周一至周五 **8:00–12:00，14:30–17:30**；领取当天或隔天；
  - 密封：**自带信封**；可委托他人现场办理并携带身份证；**不接受电话、邮件办理**；
  - 自助打印：2007 级（含）以后在校生/毕业生；在校生用身份证、校园卡、学号及统一身份认证密码；**24 小时**开放，地点为行政楼一楼大厅、三角市紫荆e站 24 小时自助服务大厅；宿舍区另有泰山区、华山区、启林南、启林北学生社区服务中心；
  - 留学密封：本科生院教学运行科教材服务中心（图书馆-紫荆e站综合服务大厅旁），**020-38675286**；
  - 电子证明：可信电子成绩单证明入口 `http://cjd.scau.edu.cn/transcript_server/#/login`，学信网验证入口另列。
- 研究生事实（只 handoff）：
  - 研究生院 **培养科**：**020-85280189**，行政楼 **309**；职责含开具中英文学籍证明、学历证明；
  - 样本页：`https://yjsy.scau.edu.cn/2024/0621/c285a378932/page.htm`；
  - 材料/费用/时限：**以培养科确认为准**（`deadline_note`），不套用本科生 5 份免费/5 元规则。
- 分支：
  - `level=本科生 & enrollment_status=已毕业` → 流程 `not_applicable`，handoff 学历学位证明；
  - `level=研究生` → 只出培养科导航步骤，不写材料/费用/时限；
  - `delivery=委托他人` → 加“受托人携带身份证”步骤；
  - `purpose=出国留学` → 加“密封/自带信封”步骤；
  - `delivery=自助打印` → 只用自助打印指南中的登录方式和 24 小时点位。
- 不引用已失效的 2018 模板页；模板入口用主页面/模板 zip，不索引模板中的个人样例（姓名/生日/学号）。
- 在读证明无批次截止，正常状态 `open`；窗口下班时间不是 `closed`，只显示办理时间。

### 3.3 版本与加载

- `FlowRegistry` 扫描 `configs/flows/*.yaml`；
- 同一 `flow_id` 多版本按 `effective_from`/`version` 选当前；重叠时拒绝加载；
- 启动时校验：
  - flow_id 唯一、version 合法；
  - step ID 唯一、`depends_on` 存在、DAG 无环；
  - 条件表达式可解析、槽位已定义、类型合法；
  - citations 存在；
  - 步骤部门/依据/截止信息完整；
  - `citation_exempt` 仅用于导航步骤；
  - `deadline` 可解析为 ISO date/datetime；date-only 不报错但按当日结束处理；
  - `closed_observed_at` 有 citation 或明确来源，且同时有 `deadline_note`；
  - 没有 `deadline` 且没有 `closed_observed_at` 时，必须有 `deadline_note`；
  - `navigation_only=true` 时拒绝材料/时限/收费/资格事实；
  - 失败直接报错，不加载半成品。

---

## 4. 三值谓词

### 4.1 语法

```text
expr        := or_expr
or_expr     := and_expr ("or" and_expr)*
and_expr    := unary ("and" unary)*
unary       := "not" unary | comparison | bool_literal
comparison  := path op literal
op          := "==" | "!=" | ">" | ">=" | "<" | "<=" | "in"
path        := "slots." IDENT | "today" | "constants." IDENT
literal     := STRING | NUMBER | BOOLEAN | DATE
```

禁止：

- 函数调用、属性访问、下标、任意 Python、任意变量；
- `eval` / `exec` / `getattr`；
- 未注册的槽位/常量；
- 类型不匹配的比较。

### 4.2 三值语义

- 缺槽位或值未知 → `UNKNOWN`；
- `not UNKNOWN = UNKNOWN`；
- `false and UNKNOWN = false`；`true or UNKNOWN = true`；
- 其余组合按三值逻辑表；
- `unknown` 处理由 `on_unknown` 决定：`ask` / `conditional` / `block`。

### 4.3 测试

- 真值表全组合；
- 类型错误（字符串与整数比较）→ 加载期错误；
- 未定义槽位 → 加载期错误；
- 含 `in` 的枚举匹配；
- `today`/日期比较；
- 没有 `eval` 路径。

---

## 5. 槽位填充

### 5.1 确定性抽取

`SlotExtractor` 只接受明确表达：

| 类型 | 规则 |
|---|---|
| integer | `大一/大二/大三/大四/研一/研二/研三`、纯数字、范围校验 |
| boolean | 是/否/有/没有/受过/未受过；模糊表达不猜 |
| enum | 精确值 + YAML aliases；不做语义推断 |
| date | ISO、`YYYY年M月D日` |
| string | 用户明确文本；不改变原文含义 |

- 一次用户消息可以填多个明确槽位；
- 不因为姓名、地区、口音等推断身份/性别/经济情况；
- 无法确定 → 不填，继续按 priority 追问；
- `source` 只写 `user_explicit` 或 `user_confirmed`；系统默认值只能来自 YAML。

### 5.2 追问

- 每轮最多问一个缺失的必填槽位，按 `priority` 升序；
- 已问过/已填的槽位不重复；
- 用户拒答：记 `declined_slots`，不再追问；影响资格判断时给条件性说明 + 人工入口；
- 澄清轮数上限 3；超过后给“通用路径 + 需要补充的信息”，不假装个性化；
- 用户中途更正：覆盖旧值，清空下游 checklist 计算并重新渲染。

### 5.3 会话状态

```python
@dataclass
class ConversationSession:
    session_id: str
    turn_id: int
    active_flow_id: str | None
    slots: dict[str, SlotValue]
    declined_slots: list[str]
    asked_slots: list[str]
    pending_slot: str | None
    clarify_count: int
    flow_instance: FlowInstance | None
```

- 单进程内存存储即可；W8 再换持久化。
- trace 记录 flow_id、turn_id、slot 名称和 `user_explicit/declined`，不记录敏感值。
- 同一会话跨轮保留；政策问答不保存流程状态。

---

## 6. 流程匹配与路由

### 6.1 匹配

1. 用户话中的别名精确/包含匹配；
2. 流程标题/描述关键词重叠；
3. 唯一最高分 → 选定；
4. 多个接近 → 追问“你要办理 A 还是 B？”；
5. 无候选 → 退回 `QAService` 普通政策问答；
6. “政策是什么/条件是什么”而没有任何办理动作 → 优先 QAService，不强套流程。

W5 不使用 LLM 做流程匹配；W6 可加入语义 Planner。

### 6.2 路由

```text
新消息
  ├─ 有 active_flow 且 pending_slot → 尝试填槽
  ├─ 有 active_flow → 更新/更正槽位
  ├─ 匹配到流程 → 创建 FlowInstance，追问
  ├─ 匹配到多个 → 追问选择
  └─ 无流程 → QAService（政策问答）
```

`ConversationService` 是新入口；现有 `QAService` 保持单轮 API，不被 W5 改写。

---

## 7. Checklist 渲染

### 7.1 前置条件

- `true`：步骤/流程可用；
- `false`：`on_false=ineligible/block` → 不输出可办理步骤，给依据和人工入口；
- `unknown`：
  - `ask` → 继续追问缺失槽位；
  - `conditional` → 输出“满足条件后可办理”，列出缺什么；
  - `block` → 输出暂不可办理 + 原因 + 依据。

### 7.2 步骤状态

- `available`：依赖完成/条件真；
- `blocked`：依赖被 blocked/ineligible；
- `conditional`：条件 unknown 且 on_unknown=conditional；
- `not_applicable`：条件 false 且步骤不适用；
- `closed`：本批次已截止/已结束，保留事实与来源，但不显示“现在可办理”；
- `done`：仅会话内勾选，不持久化。

### 7.3 排序与字段

- DAG 拓扑排序；同层按 YAML 声明顺序；
- 每项输出 `ChecklistItem`：标题、状态、描述、材料、截止日期/提示、部门、引用条款、阻塞原因、`status_note`；
- `deadline` 来自绝对日期或 `calendar:event_id`；日期显示 `Asia/Shanghai`；
- 没有可靠日期时显示 `deadline_note`，不伪造日期；
- `citation_exempt` 步骤只显示导航信息，不显示制度事实；
- 每一步的 citations 来自 YAML；不能在渲染时新增；
- handoff 固定来自 YAML（department/url/note），不伪造电话/承诺。

### 7.4 `closed` 判定与呈现

```text
if deadline exists and as_of > deadline:            closed
elif closed_observed_at exists and as_of >= it:     closed
elif only deadline_note:                            not closed (unknown window)
else:                                               open/conditional/blocked per prerequisites
```

- `closed` 优先于 `blocked/conditional`；
- 关闭原因写入 `status_note`：具体截止时间 + 来源；若只有公示证据，写“具体截止日未公开，按公示日期判定已结束”；
- 不输出下一年度日期；只写“下一批次以当年通知为准”；
- 流程整体状态（只统计带 `deadline`/`closed_observed_at` 的步骤）：
  - 所有带日期的步骤 `closed` → `closed`；
  - 部分 `closed`、部分未截止 → `partial`；
  - 无 `closed` 且有确定窗口 → `open`；
  - 无确定窗口 → `unknown`；
- 整批 `closed` 时跳过槽位追问，直接输出 closed checklist + 官方入口；`partial` 时只对未截止的步骤追问槽位；
- `closed` 步骤仍保留材料/费用/地点等历史事实，便于下一年准备，但必须带 `batch_label`，避免被误读为当前批次。

---

## 8. MCP 扩展

`policy-meta` 增加：

| 工具 | 请求 | 响应 |
|---|---|---|
| `list_flows` | `audience?` | FlowSummary[]（不返回完整 steps） |
| `get_flow` | `flow_id`, `as_of` | FlowDefinition（引用已校验） |
| `get_calendar` | `start`, `end`, `audience?` | CalendarEvent[] |

约束：

- 只读；
- `get_flow` 只返回有效版本；历史流程暂时不开放（W6）；
- `get_flow` 接受 `as_of`，返回每步 `closed` 与流程聚合状态；不传时用服务端 Asia/Shanghai 当天；
- `list_flows` 不返回长步骤，避免 prompt 过长；
- `get_calendar` 只返回公开校历事件；
- 未注册 flow/calendar → `NOT_FOUND` / `NOT_IMPLEMENTED`；
- 流程/校历 schema 变化更新 `schema_version`；
- contract tests 覆盖正常/空/非法/不存在/降级/closed/partial。

应用侧 `FlowClient` 通过 stdio 调用；W5 不在应用进程直接读 YAML。测试可用 in-memory fake client。

---

## 9. 测试与验收

### 9.1 单元/契约

- predicates 真值表、类型错误、未定义槽位、无 eval；
- slots 抽取、范围、枚举、拒答、更正；
- registry 加载、DAG 环、重复 ID、引用不存在、citation_exempt 约束；
- matcher 别名/歧义/无候选；
- checklist 状态、排序、deadline_note、citation_exempt；
- MCP flow/calendar contract；
- ConversationService 多轮状态、澄清上限、路由。

### 9.2 脚本化多轮与 `closed` 场景

1. **奖学金（open → 更正）**，`as_of=2026-09-30`：
   - “我想申请国家奖学金” → 匹配流程，追问学院；
   - “数信，大二，没受过处分” → 补 `college/award_type/grade/has_disciplinary_record`，输出开放 checklist（数信国奖 10-10 09:00）；
   - 用户改“其实我是大三” → 重算，不重复问已填槽位。
2. **奖学金（整批 closed）**，`as_of=2026-10-25`：
   - “我想申请国家奖学金” → **不追问年级**，直接输出 `closed`：数信 10-10 09:00、公管 10-05 24:00 已截止；农学院国奖 `unknown` + 学院 handoff；
   - 明确“下一批次以当年通知为准”，不写 2027 日期。
3. **奖学金（partial）**，`as_of=2026-10-18`：
   - 数信国奖/助学金已截止，励志仍开放 → 流程 `partial`；问奖项后按奖项给 open 或 closed；
   - 不能把国奖的截止日套到励志。
4. **奖学金（农学院 observed closed）**，`as_of=2026-10-25`：
   - 励志/助学金按公示日期判定 `closed`，`status_note` 写明“具体截止日未公开”；
   - 不把 2024 通知日期套到 2026。
5. **推免**：
   - `as_of=2026-10-25`：“我大四，已经拿到推免资格，还需要统考报名吗？” → 推荐阶段 `closed`，只给接收阶段 `yz.chsi.com.cn/tm` 与学院 handoff；
   - `as_of=2026-09-10`：追问是否应届/是否有名额，输出推荐阶段 checklist；更正后重算；
   - 不推断 2028 日期。
6. **在读证明（本科生自助打印）**：
   - “本科生，在校生，出国留学，自助打印” → 输出 5 份内免费、24 小时点位、密封自带信封、引用附件1/自助打印指南。
7. **在读证明（本科生窗口/委托/已毕业）**：
   - “委托同学去窗口” → 加受托人身份证步骤、305/85280049、工作日时间、收费现金；
   - “我已经毕业了” → `not_applicable`，handoff 学历学位证明（15 元/份），不输出在读证明。
8. **在读证明（研究生）**：
   - “研究生在读证明” → 只输出培养科 handoff（020-85280189，行政楼 309），材料/费用/时限写“以培养科确认为准”；不套本科生 5 份免费规则。

### 9.3 验收门

- 三个流程 YAML 通过加载校验；
- 上述脚本化多轮全部通过；
- `closed` 判定：deadline 过期、`closed_observed_at` 过期、整批 closed/partial 三类测试都通过；
- 整批 closed 时不再追问槽位；partial 时只问未截止步骤；
- 缺槽位会追问，已填不重复；更正槽位会重算 checklist；拒答槽位不重复追问；
- 每步引用存在；`citation_exempt` 仅导航步骤；
- 数信/农学院两个学院分支可渲染；公管只作校对；农学院未知截止日不被编造；
- 在读证明本科生/研究生分支正确，2018 失效链接未出现；
- W5 新增源不含姓名/学号/身份证/手机号；模板个人样例未入库；
- 不调用 LLM API；
- W7 冻结测试集/报告未被修改；
- 全量离线测试通过；新增 W5 测试不回归 W2–W4。

---

## 10. 风险与处理

| 风险 | 影响 | 处理 |
|---|---|---|
| 旧批次日期被当成新批次 | 用户误以为还能申请 | `batch_label` + `closed`；新年度建新 flow 版本；不猜日期 |
| 整批 closed 仍追问年级 | 体验差/误导 | closed 优先；整批 closed 跳过槽位追问 |
| 多学院截止日混用 | 错给截止日 | `college` 槽位；每步独立 deadline/citation |
| 农学院 2026 通知缺失 | 无法给精确日期 | 用公示元数据判 closed，并写“具体截止日未公开” |
| 公示页含姓名 | 隐私风险 | 只入库脱敏元数据；不索引原始名单 |
| 在读证明本科/研究生混用 | 错写费用/时限 | 分支步骤 + 研究生 handoff；不套本科生规则 |
| 2018 模板页失效 | 引用不可达 | 已确认 410/404；改用主页面/模板 zip |
| 流程 YAML 引用错误 | 引用不可核 | 加载时逐条校验 article_id |
| 三值谓词实现错误 | 错误资格判断 | 真值表/类型测试；禁止 eval |
| 槽位抽取误判 | 个性化错误 | 只接受明确表达；模糊就追问 |
| 追问循环 | 用户烦躁 | 澄清上限 3；declined 不重复 |
| 流程匹配过宽 | 强套流程 | 无候选回退政策问答；歧义追问 |
| 截止日期无来源 | 误导 | 只用公开通知；否则 deadline_note |
| W7 已完成且失败 | 不能声称 W7 通过 | W5 独立验收；README 明确边界 |
| W5 引入 LLM 成本 | 与省钱目标冲突 | W5 默认规则；LLM 仅 W6 可选 |

---

## 11. 实施顺序

1. **数据门**：
   - 入库 3 个核心制度 doc（已有）+ 4 个 2026 学院奖学金通知（数信 2、农学院 2 个脱敏公示元数据）+ 2 个综测前置/参考；
   - 入库在读证明 5 个来源（主页面、附件1 转录、自助打印转录、研究生培养科页、研究生样本页）；
   - 入库推免 2 个 2027 通知/公示；
   - 写入 `flow_ids`，建立 `configs/calendar.yaml`，修正 2018 失效链接引用；
   - 断言 W5 新源不含个人姓名/学号/手机号。
2. **DSL 校验**：FlowDefinition 扩展（`batch_label`/`deadline_note`/`closed_observed_at`/`citation_exempt`）、registry、citation/DAG/谓词校验。
3. **谓词引擎**：tokenizer/AST/三值求值；先写失败测试。
4. **槽位与匹配**：规则抽取、priority、拒答/更正、flow matcher；奖学金加 `college`。
5. **Checklist 与 closed**：DAG 状态、deadline/calendar、closed/partial 聚合、citation_exempt、handoff。
6. **ConversationService**：会话状态、路由、澄清上限、closed 跳过追问、CLI/demo。
7. **MCP**：policy-meta 增加 flow/calendar tools + contract tests。
8. **脚本化验收**、文档、README 边界说明。

每一步先测试后实现；W5 不接 LLM。

---

## 12. 与 W6/W8 的接口

- W6：在 `ConversationService` 外包 LangGraph Planner；加入 LLM 槽位抽取/多问拆解/重写；FlowEngine/DSL 不变。
- W8：Gradio 多轮对话直接调用 ConversationService；checklist 卡片、引用展开、人工入口。
- W7：已终止；W5 的流程验收不写入 W7 报告；不得暗示 W7 覆盖流程。

稳定契约：

- `FlowDefinition` / `FlowStep` / `Prerequisite` / `SlotDefinition`；
- `FlowInstance` 扩展字段；
- `ChecklistItem`；
- 三值谓词结果；
- MCP `list_flows` / `get_flow` / `get_calendar`；
- 流程事实只能来自 YAML。

---

## 附录 A：W5 来源登记（2026-10-25 核验）

> 核验结果：以下除明确标注外均为 HTTP 200。`local_path` 转录只用于无法解析的 `.doc/.docx`，`url` 仍指向官网原文。

### A.1 奖学金（国家级）

| 计划 doc_id | 官方 URL | 发布日 | flow_ids | 用途/备注 |
|---|---|---|---|---|
| `national-aid-undergrad`（已有） | https://xg.scau.edu.cn/2022/1018/c11387a327371/page.htm | 2021-12-31 | `scholarship-application` | 华农党发〔2021〕35号；资格/程序主依据；金额用当年通知 |
| `national-aid-2026-info-merit` | https://info.scau.edu.cn/2026/1010/c3772a448289/page.htm | 2026-10-09 | `scholarship-application` | 数信国奖：10000 元、18 人、10-10 09:00 截止 |
| `national-aid-2026-info-aid` | https://info.scau.edu.cn/2026/1013/c3772a448651/page.htm | 2026-10-13 | `scholarship-application` | 数信励志 6000、10-21；助学金 2500–5000、10-16 |
| `national-aid-2026-agri-inspirational-publicity` | https://nxy.scau.edu.cn/2026/1023/c17982a449674/page.htm | 2026-10-23 | `scholarship-application` | **仅脱敏元数据**：公示 10-23～10-24；姓名/学号不入库 |
| `national-aid-2026-agri-grant-publicity` | https://nxy.scau.edu.cn/2026/1021/c17982a449483/page.htm | 2026-10-21 | `scholarship-application` | **仅脱敏元数据**：公示 10-21～10-22；姓名/学号不入库 |

### A.2 综合测评（前置/参考，不是国家级奖项）

| 计划 doc_id | 官方 URL | 发布日 | flow_ids | 用途/备注 |
|---|---|---|---|---|
| `comprehensive-evaluation-2026-info` | https://info.scau.edu.cn/2026/0917/c3772a446211/page.htm | 2026-09-17 | `scholarship-application` | 数信综测通知；说明综测是国奖/励志基础；不写奖项事实 |
| `comprehensive-evaluation-2026-final-publicity` | https://info.scau.edu.cn/2026/1023/c3772a449715/page.htm | 2026-10-23 | `[]`（reference only） | **仅脱敏元数据**：公示 10-23～10-27、异议截止 10-27；不进入国家级流程 |

### A.3 推免

| 计划 doc_id | 官方 URL | 发布日 | flow_ids | 用途/备注 |
|---|---|---|---|---|
| `postgraduate-recommendation`（已有） | https://xngk.scau.edu.cn/_upload/article/files/29/5e/4d0d348b41b19bdb05d7400bf83d/ec66ed2f-3117-4291-be55-3a06cb72e4ba.pdf | 2025-05-30 | `recommendation-eligibility` | 华南农办〔2025〕32号 |
| `recommendation-2027-notice` | https://jwc.scau.edu.cn/2026/0829/c5135a442400/page.htm | 2026-08-29 | `recommendation-eligibility` | 2027 推荐阶段通知；904+8；支教团 6+6 |
| `recommendation-2027-publicity` | https://jwc.scau.edu.cn/2026/0912/c5135a443871/page.htm | 2026-09-12 | `recommendation-eligibility` | 公示 9-12～9-18；放弃截止 9-16 12:00；推荐阶段 9-18 结束 |
| `agri-postgrad-recommendation-rule-2024`（已有） | https://nxy.scau.edu.cn/2024/0825/c17984a382772/page.htm | 2024-08-25 | `recommendation-eligibility` | 学院细则样例；只 handoff，不写全校通用事实 |

补充（只入 notes，不单独入库）：递补通告一 https://jwc.scau.edu.cn/2026/0916/c5135a445961/page.htm ；递补通告二 https://jwc.scau.edu.cn/2026/0917/c5135a446139/page.htm 。

### A.4 在读证明

| 计划 doc_id | 官方 URL | 日期 | flow_ids | 用途/备注 |
|---|---|---|---|---|
| `enrollment-certificate-guide` | https://jwc.scau.edu.cn/hnnydxywcjdwxlxwzmjqtzm/list.htm | 页面现行 | `enrollment-certificate` | 主页面：对象、注意事项、时间、电话、打印点 |
| `enrollment-certificate-fee-note-2026` | https://jwc.scau.edu.cn/_upload/article/files/ae/b5/4c10927c4cbbaec9352e590a6d96/b8a3e914-1eee-4ea6-8995-441fca761a37.doc | 2026-03-03 | `enrollment-certificate` | 附件1；本地 HTML 转录；5 份免费/5 元、已毕业 15 元、305/85280049 |
| `enrollment-certificate-self-print-2024` | https://jwc.scau.edu.cn/_upload/article/files/ae/b5/4c10927c4cbbaec9352e590a6d96/d6814017-7423-44be-9821-81548d26630f.docx | 2024-11-05 | `enrollment-certificate` | 自助打印指南；本地 HTML 转录；24h 点位/登录方式/密封 |
| `enrollment-certificate-grad-office` | https://yjsy.scau.edu.cn/299/list.htm | 页面现行 | `enrollment-certificate` | 研究生院培养科：020-85280189、行政楼 309 |
| `enrollment-certificate-grad-sample` | https://yjsy.scau.edu.cn/2024/0621/c285a378932/page.htm | 2024-06-21 | `enrollment-certificate` | 研究生证明样本入口；材料/费用/时限以培养科为准 |

模板 zip（不索引个人样例）：https://jwc.scau.edu.cn/_upload/article/files/ae/b5/4c10927c4cbbaec9352e590a6d96/d0d8410e-5cc4-42eb-aec8-88360791515c.zip 。

### A.5 校历

| 计划 id | 来源 | 备注 |
|---|---|---|
| `calendar-2026-2027` | https://jwc.scau.edu.cn/2026/0427/c6957a433190/page.htm + `2026-2027学年校历及作息时间表.xlsx` | 2026-04-27；第二学期表头“征求意见稿”，春夏日期标注以定稿为准 |

### A.6 明确排除

- `https://jwc.scau.edu.cn/2018/1112/c5171a146530/page.htm`：410/404，不引用；
- 数信 2026 国奖评审结果名单（2026-10-10）、数信 10-23 综合测评拟获奖名单原始表、农学院 2026 励志/助学金公示原始名单：含姓名/学号/成绩，**不下载、不索引**；只允许脱敏元数据；
- 2024 农学院本科生国家奖助学金通知：只作程序背景，不把其 10 月日期套到 2026；
- 2023 校级奖学金通知：只作流程骨架背景，不用于 2026 金额/截止；
- 国家奖学金 2021 的 8000 元旧口径：不用于 2026；2026 以 10000 元通知为准。

### A.7 公管交叉核对（不入库、不进 flow）

- 国奖：https://ggglxy.scau.edu.cn/2026/0930/c11886a447806/page.htm （10000 元、11 人、全校 171、10-05 24:00）
- 励志：https://ggglxy.scau.edu.cn/2026/1001/c11883a447815/page.htm （10-19）
- 助学金：https://ggglxy.scau.edu.cn/2026/1001/c11886a447814/page.htm （10-10）
- 用途：校验数信通知的金额/程序口径；不作为 W5 首版 flow 的 citation，也不写入 `configs/sources.yaml`。
