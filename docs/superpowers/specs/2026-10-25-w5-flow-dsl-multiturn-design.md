# CampusGuide W5 设计：流程 DSL 与多轮槽位

| 项目 | 内容 |
|---|---|
| 文档版本 | 0.1（设计稿） |
| 日期 | 2026-10-25 |
| 基线 | W7 已终止并打 tag `w7`（W7 总门禁未通过）；W4 MCP/混合检索稳定；435 tests |
| 范围 | YAML 流程 DSL、受限三值谓词、三个首版流程、多轮槽位、checklist 渲染 |
| 阶段关系 | **W5 是 W7 之后的独立能力**；不修改 W7 冻结测试集/报告，也不把 W5 流程指标写进 W7 结果 |
| 状态 | 设计待确认；可以进入实现 |

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

---

## 1. 当前基线与数据缺口

### 1.1 现有

- W4 `policy-search` / `policy-meta` MCP、hybrid 检索、受众/时间过滤；
- W3 `VerificationIssue` / `PARTIAL_ANSWER` 契约；
- 领域模型已有 `FlowDefinition`、`FlowStep`、`Prerequisite`、`SlotDefinition`、`FlowInstance`、`CalendarEvent`；
- `configs/flows/` 三个空骨架：`scholarship-application`、`recommendation-eligibility`、`enrollment-certificate`；
- `flows/` 模块目前只是占位文件；
- 435 个离线测试，W7 结束。

### 1.2 必须先解决的数据缺口

1. **三个 flow 的 `flow_ids` 目前为空**：W5 要显式写入关联条款，不能靠运行时猜。
2. **在读证明没有明确公开依据**：现有语料中没有“在读证明/学籍证明/在校证明”的对应条款。
   - 方案 A（推荐）：先补一份公开的教务处办事指南/通知，作为 flow 的引用来源；
   - 方案 B：该 flow 只做**导航/人工入口**，每个步骤显式 `citation_exempt: true`，不输出制度事实；
   - 不允许编造“需要某部门盖章/收费/时限”。
3. **校历/截止日数据不存在**：W5 新增 `configs/calendar.yaml`，只登记有公开来源的事件；没有可靠日期时使用 `deadline_note: 以当年通知为准`，不写绝对日期。

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
    citation_exempt: bool = False   # 纯导航步骤显式豁免依据
    deadline_note: str | None = None # 没有可靠日期时的公开提示

class FlowInstance(BaseModel):
    ...
    asked_slots: list[str] = []
    declined_slots: list[str] = []
    pending_slot: str | None = None
    clarify_count: int = 0
    done_step_ids: list[str] = []
    last_question: str | None = None

class ChecklistItem(BaseModel):
    step_id: str
    title: str
    status: Literal["available", "blocked", "conditional", "done", "not_applicable"]
    description: str
    materials: list[str]
    deadline: str | None
    deadline_note: str | None
    department: str
    citations: list[str]
    blocked_reason: str | None
```

`FlowDefinition` 增加 `navigation_only: bool = False`。若为 `true`：

- 所有步骤必须 `citation_exempt: true`；
- 只能输出导航/入口信息，不能输出资格判断、材料、时限或收费事实；
- 加载校验拒绝“navigation_only 但有制度事实”的流程。

约束：

- 每个步骤必须有 `responsible_department`；
- 每个步骤至少有 1 条 citation，除非 `citation_exempt: true`；
- `citation_exempt` 只允许导航/入口步骤，不允许带材料/时限/部门事实；
- `deadline` 与 `deadline_note` 二者至少有一个；有可靠日期时用 `calendar:event_id` 或 ISO 日期；
- `citation_exempt` 的步骤不能出现在“资格判断”中。

### 3.2 三个流程的初始数据

#### 奖学金申请

- `flow_id: scholarship-application`
- audience: `undergraduate`
- slots:
  - `grade`（integer，1–5，required）
  - `award_type`（enum：国家奖学金/国家励志奖学金/国家助学金/校级奖学金，optional）
  - `has_disciplinary_record`（boolean，required）
- citations 候选（已存在）：
  - `national-aid-undergrad@v2021#第八条` 基本条件
  - `#第九条` 国家奖学金条件
  - `#第十条` 国家励志奖学金条件
  - `#第十二条` 申请方式
  - `#第十三条` 材料真实性
  - `#第十六条` 评审程序
- 材料/截止时间来自公开通知或 `configs/calendar.yaml`；没有通知时用 `deadline_note`。

#### 推免资格确认

- `flow_id: recommendation-eligibility`
- audience: `undergraduate`
- slots:
  - `is_graduating`（boolean，required）
  - `has_recommendation_quota`（boolean，required）
  - `in_recommendation_list`（boolean，optional）
  - `grade`（integer，optional）
- citations 候选：
  - `postgraduate-recommendation@v2025#第十条` 基本条件
  - `#第十一条` 学业要求
  - `#第十六条` 工作程序
  - `#第十七条` 自主选择志愿/报名
- 学院细则存在时通过 `parent_doc_id`/alias 关联；首版不拆分所有学院差异。

#### 在读证明开具

- `flow_id: enrollment-certificate`
- slots:
  - `purpose`（enum：升学/就业/实习/其他，required）
  - `delivery`（enum：自助打印/窗口领取/邮寄，required）
- **数据缺口**：现有语料没有对应公开依据。
  - W5 先把该流程标为 `navigation_only: true`，每步 `citation_exempt: true`，只输出“去教务处/查看官网模板/带学生证”等导航信息；
  - 或者先补充公开办事指南，再填 citations；
  - 不能在 YAML 中写未经来源支持的材料、费用或时限。

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
- `done`：仅会话内勾选，不持久化。

### 7.3 排序与字段

- DAG 拓扑排序；同层按 YAML 声明顺序；
- 每项输出 `ChecklistItem`：标题、状态、描述、材料、截止日期/提示、部门、引用条款、阻塞原因；
- `deadline` 来自绝对日期或 `calendar:event_id`；日期显示 `Asia/Shanghai`；
- 没有可靠日期时显示 `deadline_note`，不伪造日期；
- `citation_exempt` 步骤只显示导航信息，不显示制度事实；
- 每一步的 citations 来自 YAML；不能在渲染时新增；
- handoff 固定来自 YAML（department/url/note），不伪造电话/承诺。

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
- `list_flows` 不返回长步骤，避免 prompt 过长；
- `get_calendar` 只返回公开校历事件；
- 未注册 flow/calendar → `NOT_FOUND` / `NOT_IMPLEMENTED`；
- 流程/校历 schema 变化更新 `schema_version`；
- contract tests 覆盖正常/空/非法/不存在/降级。

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

### 9.2 三组脚本化多轮

1. **奖学金**：
   - “我想申请国家奖学金” → 追问年级；
   - “大二，没受过处分” → 补齐槽位，输出 checklist；
   - 用户改“其实我是大三” → 重算，不重复问已填槽位。
2. **推免资格**：
   - “我大四，已经拿到推免资格，还需要统考报名吗？” → 匹配流程；
   - 追问是否在名单/是否应届（按 YAML），输出资格判断与下一步；
   - 更正资格状态 → checklist 重算。
3. **在读证明**：
   - “我要开在读证明” → 追问用途/领取方式；
   - 输出导航型 checklist + 教务处人工入口；
   - 没有公开依据时不得编造材料/时限。

### 9.3 验收门

- 三个流程 YAML 通过加载校验；
- 三组脚本化多轮全部通过；
- 缺槽位会追问，已填不重复；
- 更正槽位会重算 checklist；
- 拒答槽位不重复追问；
- 每步引用存在；`citation_exempt` 仅导航步骤；
- 不调用 LLM API；
- W7 冻结测试集/报告未被修改；
- 全量离线测试通过；新增 W5 测试不回归 W2–W4。

---

## 10. 风险与处理

| 风险 | 影响 | 处理 |
|---|---|---|
| 在读证明无公开依据 | 不能输出材料/时限 | 导航型流程或先补来源；不编造 |
| 流程 YAML 引用错误 | 引用不可核 | 加载时逐条校验 article_id |
| 三值谓词实现错误 | 错误资格判断 | 真值表/类型测试；禁止 eval |
| 槽位抽取误判 | 个性化错误 | 只接受明确表达；模糊就追问 |
| 追问循环 | 用户烦躁 | 澄清上限 3；declined 不重复 |
| 流程匹配过宽 | 强套流程 | 无候选回退政策问答；歧义追问 |
| 截止日期无来源 | 误导 | 只用公开校历/通知；否则 deadline_note |
| W7 已完成且失败 | 不能声称 W7 通过 | W5 独立验收；README 明确边界 |
| W5 引入 LLM 成本 | 与省钱目标冲突 | W5 默认规则；LLM 仅 W6 可选 |

---

## 11. 实施顺序

1. **数据门**：补齐奖学金/推免 citations；决定在读证明是“补来源”还是“导航型”；建立 `configs/calendar.yaml`。
2. **DSL 校验**：FlowDefinition 扩展、registry、citation/DAG/谓词校验。
3. **谓词引擎**：tokenizer/AST/三值求值；先写失败测试。
4. **槽位与匹配**：规则抽取、priority、拒答/更正、flow matcher。
5. **Checklist**：DAG 状态、deadline/calendar、citation_exempt、handoff。
6. **ConversationService**：会话状态、路由、澄清上限、CLI/demo。
7. **MCP**：policy-meta 增加 flow/calendar tools + contract tests。
8. **三组脚本化验收**、文档、README 边界说明。

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
