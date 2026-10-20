# 数据来源与语料登记

本文说明如何把华南农业大学的公开制度文件登记进 `configs/sources.yaml`。该文件是**唯一数据入口**，抓取、解析、切分和索引都以它为准。

当前状态：`configs/sources.yaml` 已登记 44 个校级公开制度、52 个版本，`validate_sources.py` 报告 `w1_target=met`。完整快照与解析文本只保存在本地 `data/` 下，不入库；引用始终指向官网原文。

## 合规边界

- 只登记学校官网可公开访问的页面或附件；不绕过登录、验证码或访问控制。
- 不下载申请表、成绩单、名单、评审结果等包含个人信息的材料。
- 完整 PDF/HTML 快照与解析文本只保存在本地，不提交到公开仓库。
- 索引文本会对手机号、身份证号、学号、个人邮箱和联系人姓名脱敏；部门公开座机保留。
- 引用链接始终指向官网原文，读者应以原文为准。

## 推荐流程

1. 把下载好的文件放到 `data/incoming/`（已 gitignore，不会入库）。
2. 在 `configs/sources.yaml` 的 `documents` 下登记制度本体和版本。
3. 执行 `make validate-sources` 检查必填日期和来源。
4. 执行 `make fetch` 复制/下载为 `data/raw/{doc_id}/{version_id}/source.*` 与 `meta.json`。
5. 执行 `make parse` 生成 `data/processed/parsed/{doc_id}/{version_id}.json`。
6. 执行 `make index` 生成 `data/processed/articles.jsonl`、`documents.jsonl` 与 `data/manifest.json`。
7. 执行 `make check-segmentation` 生成 20 条人工抽检清单。

## 字段说明

### 制度本体（documents[]）

| 字段 | 必填 | 说明 |
|---|---|---|
| `doc_id` | 是 | 小写 slug，例如 `undergraduate-status-rules`；一经确定不复用。院级文件建议加学院前缀，如 `engineering-recommendation` |
| `title` | 是 | 制度全称，写入引用卡片 |
| `doc_type` | 否 | `regulation` / `notice` / `guide` / `faq` / `report` / `other`，默认 `other` |
| `authority_level` | 否 | `binding` / `operational` / `informational` / `unknown`；不填时按 `doc_type` 推导：制度为 `binding`，通知为 `operational`，指南/问答/报告为 `informational` |
| `issuer_level` | 否 | `university` / `college` / `department`，默认 `university` |
| `issuer_college_id` | 院/系级必填 | 发文学院 ID；校级文件必须留空 |
| `parent_doc_id` | 否 | 上位文件 `doc_id`；学院实施细则指向校级办法，年度通知指向学院细则 |
| `relation` | 否 | `standalone` / `implements` / `supplements`，默认 `standalone`；非 `standalone` 时必须填 `parent_doc_id` |
| `issuing_department` | 是 | 发文部门，例如 `教务处`；院级文件填学院名称 |
| `audience` | 否 | 角色标签，可多选：`undergraduate` / `graduate` / `graduate_master` / `graduate_doctor` / `faculty` / `staff` / `unit` / `public` / `postdoc` / `all`。默认 `all` 仅用于未细分对象的公开文件；非学生文件不要用 `all` |
| `audience_detail` | 否 | 角色之外的年级、阶段、培养类型、群体、学院/专业限制，见下文 |
| `flow_ids` | 否 | 关联流程 ID，W1 只登记，W5 使用 |
| `topic_tags` | 否 | 检索辅助标签，不作为回答依据 |
| `versions` | 是 | 至少一个版本 |

### 版本（versions[]）

| 字段 | 必填 | 说明 |
|---|---|---|
| `version_id` | 是 | 同一 `doc_id` 下唯一，例如 `v2025` |
| `url` | 是 | 官网原文链接，也是最终引用链接；即使使用本地文件也必须填写 |
| `local_path` | 否 | 本地文件路径；相对路径按仓库根目录解析，文件放到 `data/incoming/` 最方便 |
| `published_at` | 是 | 发布日期；缺失会导致 `make index` 直接失败 |
| `effective_from` | 推荐 | 生效日期；缺省时回退为 `published_at` |
| `effective_to` | 否 | 失效日期；现行版本留空 |
| `status` | 否 | `active` / `repealed` / `draft` / `unknown`，默认 `active` |
| `format` | 否 | `auto` / `html` / `pdf`；`auto` 依据扩展名与 Content-Type 判断 |
| `audience` | 否 | 版本级角色覆盖；不填继承 `documents[]`。只有新版改变适用对象时才填 |
| `audience_detail` | 否 | 版本级约束覆盖；不填继承文档级。填 `{}` 表示显式取消文档级限制 |
| `notes` | 否 | 文号、修订说明等自由文本 |

### 适用人群细目（audience_detail）

`audience` 只回答“哪类角色”，`audience_detail` 回答“哪一级、哪个阶段、哪个群体、哪个学院/专业”。字段不填表示不限制；同一字段多个值之间是 OR，不同字段之间是 AND。

| 字段 | 取值 | 说明 |
|---|---|---|
| `colleges` | 学院 ID 列表 | 空 = 全校；院级文件通常填发文学院 |
| `majors` | 专业 ID 列表 | 空 = 不限专业；专业必须在 `organization.yaml` 登记 |
| `intake_years` | 年份列表，如 `[2021, 2022]` | 固定入学年级，即“2021级、2022级” |
| `intake_from` | 年份，如 `2021` | “2021级及以后”；与 `intake_years` 二选一 |
| `graduating_years` | 年份列表，如 `[2026]` | 固定毕业届，即“2026届” |
| `stages` | `year1`…`year6` / `final_year` | 相对年级阶段；“大二”用 `year2`，“毕业年级”用 `final_year` |
| `program_types` | `full_time` / `part_time` / `academic` / `professional` | 全日制/非全日制、学术型/专业型 |
| `groups` | `financial_aid` / `directed` / `non_directed` / `top_up` / `second_degree` / `international` / `hk_mo_tw` / `veteran` / `minority` | 家庭经济困难、定向、专升本、第二学士学位、港澳台、退役士兵等 |
| `status` | `enrolled` / `graduating` / `suspended` / `completed` | 在读、应届/毕业年级、休学、已结业 |
| `cycle` | `2025` 或 `2025-2026` | 年度通知、评审周期；不是版本有效期 |
| `note` | 自由文本 | 仅作人工说明，不参与自动判断 |

“级”是入学年，“届”是毕业年，“年级”是在读阶段，三者不能混用。例如“2021级大四”应写 `intake_years: [2021]` + `stages: [year4]`；只写“大四”则用 `stages: [year4]`，对所有届别生效。

`audience_detail` 一旦写在 `documents[]` 或 `versions[]` 上，就表示**整份文件**受该限制。如果只是个别条款限定年级（例如新生复查、毕业审核），不要把它写到文档级，否则会把整份制度误判为仅适用于该年级。条款级适用人群覆盖尚未实现，计划在 W2/W3 评估，目前不要在 `sources.yaml` 中模拟。

### 组织注册表（organization.yaml）

院系和专业使用稳定 ID，不使用中文名称做主键。`configs/organization.yaml` 登记 `college_id`、`major_id`、所属学院、专业代码和别名；`sources.yaml` 只能引用已登记的 ID。当前 W1 只收校级文件，注册表为空；首次收录院级/专业级文件时再填。

院级文件不是校级文件的新版本：校级《推免办法》和学院《推免实施细则》使用不同 `doc_id`，后者用 `parent_doc_id` 指向校级文件，`relation: implements`。学院 2026 年工作通知再用独立 `doc_id`、`doc_type: notice`、`cycle: 2025-2026`，也不放进 `versions[]`。

## 最小示例

```yaml
schema_version: "1.0"
corpus_id: scau
school: 华南农业大学

documents:
  - doc_id: undergraduate-status-rules
    title: 华南农业大学本科学生学籍管理实施细则
    doc_type: regulation
    authority_level: binding
    issuer_level: university
    issuing_department: 教务处
    audience: [undergraduate]
    topic_tags: [学籍, 转专业]
    versions:
      - version_id: v2021
        url: https://jwc.scau.edu.cn/2021/0830/c5127a288407/page.htm
        local_path: data/incoming/undergraduate-status-rules-2021.pdf
        published_at: 2021-08-30
        effective_from: 2021-09-01
        effective_to: null
        status: active
        format: pdf
        notes: 华南农办〔2021〕68 号
```

院级文件的写法：

```yaml
  - doc_id: engineering-recommendation
    title: 工程学院推荐优秀应届本科毕业生免试攻读研究生实施细则
    doc_type: regulation
    authority_level: binding
    issuer_level: college
    issuer_college_id: engineering
    issuing_department: 工程学院
    parent_doc_id: undergraduate-recommendation
    relation: implements
    audience: [undergraduate]
    audience_detail:
      colleges: [engineering]
      stages: [final_year]
      status: [graduating]
    versions:
      - version_id: v2025
        url: https://example.scau.edu.cn/page.htm
        published_at: 2025-05-30
        effective_from: 2025-05-30
        status: active
```

## 版本与人群规则

- 同一制度可以登记多个版本；索引保留全部版本及其生效区间。
- 默认只按“当前有效版本 + 用户人群”检索；历史版本必须由用户明确询问年份后启用。
- 找不到适用版本时返回 `NO_EFFECTIVE_VERSION`，同一日期存在多个候选时返回 `VERSION_CONFLICT`，都不回退到任意版本。
- 已废止版本必须能在原文或通知中找到依据，不能凭印象标记。

## 切分与抽检

- 章标题写入 `chapter_path`，不单独作为检索单位；
- `第 X 条` 是最小检索与引用单位，支持“第 X 条之一”；
- 单条超过 800 字按段落/句子切分，多个 part 共享同一个 `article_id`；
- 重复条款号追加 `~N` 保证 ID 唯一，并在 manifest 中记录告警；
- 文末落款单独识别，不进入条款库；
- 抽检清单见 `data/review/segmentation-sample.md`，每行需人工确认边界、编号、章节路径和正文完整性。

## 校验报告

`make validate-sources` 会写出 `data/review/sources-report.json`，包含：

- `document_count` / `version_count` 与 `meets_w1_target`（制度 ≥30 且版本 ≥50）；
- `documents_without_dates` / `versions_without_dates`：缺少 `published_at` 或 `effective_from`；
- `versions_without_source`：缺少引用 URL；
- `audience_values` / `authority_values` / `issuer_levels`：当前语料实际出现的角色、效力层级和发文层级；
- `documents_with_audience_detail` / `college_scoped_documents` / `major_scoped_documents`：人群细目与院系/专业限定覆盖情况；
- `organization_loaded` 与 `errors`：组织注册表是否载入，以及未知学院/专业、缺失上位文件、专业与学院不匹配等引用错误。

存在 `errors` 时脚本返回 2；加 `--strict-target` 且未达到 W1 目标时返回 1。空语料下 `meets_w1_target` 为 `false` 属于预期，不代表 schema 有误。

这些报告和生成物都在 `data/` 下，默认不进入公开仓库。
