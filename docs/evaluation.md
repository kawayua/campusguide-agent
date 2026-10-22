# 评测与质量门

CampusGuide 的评测分为两个阶段，不能混为一谈：

1. **W1 工程质量门**：语料、切分、脱敏、确定性、索引构建是否可靠；
2. **W7 系统效果评测**：问答正确性、引用支持率、拒答、流程指引与成本/延迟。

W1 尚无问答模块，因此**没有任何回答质量指标**。简历、README 和面试材料中不得把工程质量门写成问答效果。

## W1 工程质量门

| 项目 | 方法 | 命令 | 当前状态 |
|---|---|---|---|
| 代码规范 | ruff check（E/F/I/B/UP/SIM/RUF） | `make lint` | 通过 |
| 代码格式 | ruff format --check src tests scripts | `make format-check` | 通过 |
| 单元、契约与集成测试 | 全离线：mock HTTP、临时目录、动态生成 PDF | `make test` | 107 passed |
| 索引确定性 | 同一输入重建，`content_hash` 与 JSONL 字节一致 | `pytest tests/unit/test_build_index.py` | 通过 |
| 联系方式脱敏 | 手机号、身份证、学号、邮箱、联系人姓名 | `pytest tests/unit/test_redact.py` | 通过 |
| 版本解析 | CURRENT / HISTORICAL / INCLUDE_REPEALED、冲突、无版本 | `pytest tests/unit/test_versioning.py` | 通过 |
| 条款切分 | 章节路径、条款边界、长条款 part、重复编号、落款 | `pytest tests/unit/test_segment.py` | 通过 |
| 端到端管线 | 本地 HTML → 快照 → 解析 → 切分 → JSONL + manifest | `pytest tests/integration` | 通过 |
| Qdrant 骨架 | 内存模式创建 named dense + sparse、payload 索引、幂等 | `pytest tests/unit/test_build_index.py` | 通过 |
| 语料覆盖 | 制度 ≥30、版本 ≥50 | `make validate-sources` | 55 个 regulation / 213 文档 / 232 版本，通过 |
| 切分人工抽检 | 20 条 `article`，四项判定，正确率 ≥95% | `make check-segmentation` | 20/20 = 100%，通过 |
| 结构补充抽查 | 10 条 `section`，边界与完整性 | `check_segmentation.py --block-type section` | 10/10，通过（不计入条款正确率） |

统一入口：

```bash
make qa     # ruff check + format check + 全量离线 pytest
```

CI 配置见 `.github/workflows/ci.yml`；Gitee 仓库当前以本地 `make qa` 或 `scripts/qa.sh` 作为质量门。

### 当前实测数字

截至 2026-10-22：

- `ruff check .`：通过；
- `ruff format --check src tests scripts`：通过（79 files formatted）；
- `pytest`：107 passed（unit + contract + integration，无网络依赖）；
- 真实语料：213 文档 / 232 版本 / 3,952 块；其中 `article` 1,366、`section` 2,586；
- 规模构成：校级 44 文档 / 52 版本；信息学院 36 / 44；农学院 133 / 136；
- 版本状态：`active` 222、`repealed` 10；每个文档至少有一个 `active` 版本；
- 低置信日期：16 个版本（15 个文号年份 12-31 锚点 + 1 个月份锚点），见 `docs/weekly/w1.md`；
- 索引一致性：`missing_versions=0`、`empty_text=0`，manifest `content_hash` 可重复。

### 可复现性约束

- 依赖锁定在 `uv.lock`；
- 每个版本的原始快照记录 SHA-256、抓取时间与解析器版本；
- `manifest.content_hash` 只覆盖条款与版本文档，不包含 `generated_at`，因此可用于判断索引是否一致；
- 抓取与解析测试全部离线，CI 不访问学校站点。

## W1/W2 口径交接

1. **`block_type` 必须进入检索与引用**：`article`（第 X 条）和 `section`（培养方案节、教师段落、新闻目录）不能混算。W2 的报告至少拆成 `article_recall@k` 与 `section_recall@k`，引用卡片显示块类型；W7 的“条款级命中率”只统计 `article`。
2. **日期置信度必须进入版本/证据元数据**：16 个版本是低置信锚点，W2 必须携带 `date_confidence`（`high`/`low`），引用卡片对 `low` 标注“日期为文号年份/月份锚点”；“去年/2020 年版本”类问题不能把 12-31 当成精确发布日。
3. **Flow 引用门禁延后到 W5**：W1 只登记 `flow_ids`；W2 的引用门禁只覆盖 `article`/`section` 问答，流程事实的逐项引用校验在 W5 流程 DSL 实现。

## W7 系统效果评测（计划）

| 维度 | 指标 | 目标 |
|---|---|---|
| 引用可信 | 引用 ID 有效率、引文可定位率 | 100% |
| 引用支持 | 引用支持率 | ≥95% |
| 可回答题 | 答案正确性/覆盖度、误拒率 | 误拒 ≤10% |
| 不可回答题 | 拒答率、reason code 正确率 | ≥90% |
| 检索（条款） | `article_recall@k`、MRR、条款级命中率 | 开发集校准 |
| 检索（章节） | `section_recall@k`、结构完整性 | 开发集校准，不计入条款指标 |
| 版本歧义 | 低置信日期提示率、历史版本选择正确率 | 100% 提示 / ≥90% 正确 |
| 流程 | 必需材料完整率、条件判断正确率 | checklist ≥90%；引用门禁 W5 |
| 成本/延迟 | 每轮 token、估算成本、p50/p95 | p95 ≤10 秒 |
| 消融 | 分块、Verifier、流程 DAG、元数据过滤 | 四组固定对照 |

数据集安排：

- 20 题开发集：只用于提示词与阈值调参；
- 50 题冻结测试集：W7 冻结后不再修改，报告固定 hash；
- 12 条红队用例：超范围、隐私、废止版本、版本冲突、人群错配等。

## 失败归因模板

每次评测报告至少包含：

| 字段 | 说明 |
|---|---|
| 题目 ID | 可追溯到测试集条目 |
| 失败类型 | 检索未命中 / 版本错误 / 低置信日期误用 / `block_type` 混算 / 引用不支持 / 过度拒答 / 漏拒答 / 流程事实错误 |
| 根因 | 语料、切分、过滤、提示词、核对规则、模型能力 |
| 影响范围 | 是否影响一类问题 |
| 处理决定 | 修复、接受并记录、或移出测试集（仅限标注错误） |

测试集冻结后，指标不达标时只修错误归因中占比最高的两类问题，不改测试集。
