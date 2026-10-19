# 数据来源与语料登记

本文说明如何把华南农业大学的公开制度文件登记进 `configs/sources.yaml`。该文件是**唯一数据入口**，抓取、解析、切分和索引都以它为准。

当前状态：仓库只包含清单模板与离线样例，真实语料由项目作者本地收集。W1 目标为制度本体 ≥30 个、文档版本 ≥50 个；在语料补齐前，`validate_sources.py` 会如实报告 `w1_target=not met`。

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
| `doc_id` | 是 | 小写 slug，例如 `undergraduate-status-rules`；一经确定不复用 |
| `title` | 是 | 制度全称，写入引用卡片 |
| `doc_type` | 否 | `regulation` / `notice` / `guide` / `faq` / `other`，默认 `other` |
| `issuing_department` | 是 | 发文部门，例如 `教务处` |
| `audience` | 否 | `undergraduate` / `graduate` / `all`，默认 `all`；可多选 |
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
| `notes` | 否 | 文号、修订说明等自由文本 |

## 最小示例

```yaml
schema_version: "1.0"
corpus_id: scau
school: 华南农业大学

documents:
  - doc_id: undergraduate-status-rules
    title: 华南农业大学本科学生学籍管理实施细则
    doc_type: regulation
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

- `document_count` / `version_count` 与 W1 目标是否达成；
- 缺少 `published_at` 或 `effective_from` 的版本；
- 缺少引用 URL 的版本；
- 已出现的人群取值。

这些报告和生成物都在 `data/` 下，默认不进入公开仓库。
