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
| 单元、契约与集成测试 | 全离线：mock HTTP、临时目录、动态生成 PDF、内存 Qdrant、fake embedder/chat | `make test` | 154 passed |
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
- `ruff format --check src tests scripts`：通过；
- `pytest`：154 passed（unit + contract + integration，无网络依赖）；
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

## W2 单轮问答工程质量门

W2 的目标是“引用真实”，不是答案质量。开发集 v0 只用于调参，不是冻结测试集；W2 报告不包含答案正确率。

| 项目 | 方法 | 命令 | W2 门禁 |
|---|---|---|---|
| 向量索引 | Qdrant `articles_v1` 点数、维度与 manifest 一致 | `make vector-index` | 3,952 点 / 1024 维 |
| 日期置信度 | `date_confidence=low` 版本数 | `make index` | 16 个 |
| 引用 ID 有效 | 有效 citation / 总 citation | `make eval-w2` | 100% |
| 编造 ID | 不在本轮 Evidence 的 citation | `make eval-w2` | 0 |
| quote 可定位 | 能在 `text_normalized` 定位的 quote | `make eval-w2` | 100% |
| 条款召回 | `article_recall@8` | `make eval-w2` | 记录基线 |
| 章节召回 | `section_recall@8`，仅 section 子集 | `make eval-w2` | 记录基线 |
| 非可答题 | 6 道非可答题的原因码正确率 | `make eval-w2` | 记录基线，W3 设门禁 |
| 延迟 | 端到端 p50 / p95 | `make eval-w2` | p50 ≤6s、p95 ≤10s |

开发集：`data/eval/dev_v0.jsonl`，20 题（单条款 8、多条款 4、section 2、无证据/超范围 3、历史 2、隐私 1）。报告写入 `data/eval/w2-dev-report.{json,md}`，并标注“开发集 v0，非冻结测试集”。

当前状态（2026-10-22）：Qdrant 3,952 点 / 1024 维、16 个低置信版本、154 项离线测试通过；`min_dense_score=0.55` 已用 dev_v0 校准。20 题 live 评测（`mimo-v2.6-pro-ultraspeed`）结果：outcome 20/20、引用 ID 有效率 100%、编造 ID 0、part_no 错误 0、quote 可定位率 100%、article/section recall@8 100%；warm p50 3.3s，warm p95 14.7s（多轮 14.7–36.8s），未达到 10s 门禁，原因是第三方模型偶发 30–150s 延迟，而非检索/校验。bge-m3 冷启动预热单列 27.4s；UI/CLI 启动时预加载，p50/p95 均为 warm 口径。报告见 `data/eval/w2-dev-report.{json,md}`（不报告答案正确率）。

**最终决策（2026-10-22）**：接受“引用门禁通过、p95 不达标”，停止端点 A/B。W2 以引用真实性为交付边界；p95 作为已知限制留给 W4/W7，不在 README、周记录或简历中声称延迟门禁通过。

W2 的失败关闭：

| 失败 | 原因码 |
|---|---|
| 无检索结果或全部低于阈值 | `NO_EVIDENCE` |
| LLM 两次输出均非法 JSON / API 超时 | `GENERATION_FAILED` |
| 全部 citation 无效 | `NO_VALID_CLAIM` |
| Qdrant 不可达或索引与 manifest 不一致 | `INDEX_UNAVAILABLE` |
| 历史版本 / 隐私 / 明显校外 / 空请求 | `HISTORICAL_NOT_SUPPORTED` / `PRIVACY_REQUEST` / `OUT_OF_SCOPE` / `INVALID_REQUEST` |

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
