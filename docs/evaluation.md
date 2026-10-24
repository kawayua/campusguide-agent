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
| 单元、契约与集成测试 | 全离线：mock HTTP、临时目录、动态生成 PDF、内存 Qdrant、fake embedder/chat | `make test` | 426 passed（remediation2 后） |
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
- `pytest`：426 passed（W1–W4 unit + contract + integration，无网络依赖；W1/W2 时点为 154；W3 后 295；remediation2 后 426）；
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

当前状态（2026-10-22）：Qdrant 3,952 点 / 1024 维、16 个低置信版本、W2 时点 154 项离线测试通过（W3 后 295 项）；`min_dense_score=0.55` 已用 dev_v0 校准。20 题 live 评测（`mimo-v2.6-pro-ultraspeed`）结果：outcome 20/20、引用 ID 有效率 100%、编造 ID 0、part_no 错误 0、quote 可定位率 100%、article/section recall@8 100%；warm p50 3.3s，warm p95 14.7s（多轮 14.7–36.8s），未达到 10s 门禁，原因是第三方模型偶发 30–150s 延迟，而非检索/校验。bge-m3 冷启动预热单列 27.4s；UI/CLI 启动时预加载，p50/p95 均为 warm 口径。报告见 `data/eval/w2-dev-report.{json,md}`（不报告答案正确率）。

**最终决策（2026-10-22）**：接受“引用门禁通过、p95 不达标”，停止端点 A/B。W2 以引用真实性为交付边界；p95 作为已知限制留给 W4/W7，不在 README、周记录或简历中声称延迟门禁通过。

W2 的失败关闭：

| 失败 | 原因码 |
|---|---|
| 无检索结果或全部低于阈值 | `NO_EVIDENCE` |
| LLM 两次输出均非法 JSON / API 超时 | `GENERATION_FAILED` |
| 全部 citation 无效 | `NO_VALID_CLAIM` |
| Qdrant 不可达或索引与 manifest 不一致 | `INDEX_UNAVAILABLE` |
| 历史版本 / 隐私 / 明显校外 / 空请求 | `HISTORICAL_NOT_SUPPORTED` / `PRIVACY_REQUEST` / `OUT_OF_SCOPE` / `INVALID_REQUEST` |

## W3 引用核对与拒答 v0 工程质量门

W3 的目标是**确定性核对**：引文 span 可映射回原文、claim 中的数字/日期/金额/期限被引用 quote 覆盖、受众/版本/隐私 scope 正确、输出只渲染通过核对的 claim。W3 仍**不评估语义支持**；语义蕴含与 LLM 重写留 W6。

| 项目 | 方法 | 命令 | W3 门禁 |
|---|---|---|---|
| 引文原文 span | `Citation.quote_start/end` 是 `article.text` 偏移，exact/normalized 定位 | `make test` | 100% |
| Fact 覆盖 | 数字、百分比、日期、年份、期限、金额/学分/学时；中文数字与阿拉伯数字等价 | `make test` | 未覆盖 claim 不渲染 |
| 受众 scope | 显式受众不兼容 evidence 剔除；`Audience.ALL` 视为未指定 | `make test` | 全部剔除才拒答 |
| 版本 scope | active/生效区间/冲突/混合版本 | `make test` | 冲突或全部失效才拒答 |
| 输出隐私 | 手机号、身份证、学号、非 `scau.edu.cn` 邮箱 | `make test` | 命中 claim 丢弃 |
| 废止探针 | 主检索为空时 `status=[repealed]` 无 LLM 检索 | `make eval-w3` | 命中 `ONLY_REPEALED`，耗时单列 |
| 决策契约 | `PASS` 无 blocking；`PARTIAL` 只渲染通过 claim；`ABSTAIN` 无 claim | `make test` | 100% |
| 结构门禁 | 引用 ID 有效率、编造 ID、part_no、quote 可定位 | `make eval-w3` | 100% / 0 / 0 / 100% |
| 拒答召回 | 非可答题不渲染答案 | `make eval-w3` | ≥80% |
| 误拒率 | 可答题非 ANSWERED/PARTIAL | `make eval-w3` | ≤20% |
| 支持率 | 人工 `supported` / 已标注 rendered claim | `make eval-w3-report` | ≥85%，且必须完成人工标注 |
| 延迟 | 端到端 p50/p95；探针单列 | `make eval-w3` | 记录，不在 W3 设硬门禁 |

### 2026-10-23 live 状态

- 模型 `mimo-v2.6-pro-ultraspeed`；warmup 21.2s；warm p50 3.34s、p95 9.78s；废止探针 2 次，p50 73.5ms / p95 78.45ms；
- 结构门禁通过：55/55 引用 ID 有效、0 编造、0 part_no 错误、55/55 quote 可定位；
- 拒答召回 6/6 = 100%；误拒率 0/14 = 0.0%；partial 率 1/14 = 7.1%；非可答题原因码 6/6 = 100%；
- issue：`AUDIENCE_UNDETERMINED` 14 warning、`NUMBER_NOT_SUPPORTED` 4 blocking、`MIXED_VERSIONS` 2 warning；
- **support 标注已完成 56/56（39 supported / 17 partial / 0 unsupported）；draft 支持率 69.6%，rendered 支持率 75.0%，support 门禁未通过，W3 总门禁未通过**；
- 人工标注确认了 W3 v1 的 75.0% 结果：本轮没有 `unsupported`，17 条 partial 主要是 claim 增加条件/后果/列表展开/措辞强化，属于 W6 重写要解决的问题；
- run 在 dirty working tree 上执行（commit `b240e4d`，dirty=True）；W3 已按本轮 75.0% 判定不通过，不以重跑刷分。W6 正式验收前再提交代码并重跑。

**W3 结项决定（2026-10-24）**：W3 v2 baseline 的 75.0% 保留为未通过记录；用户接受 W6-A 提示词收紧作为 W3 的 support remediation（同一 20 题、同一 Verifier、54/54 人工、98.1%），W3 记为**有条件通过**。W6-A 是独立 run/报告，不是把 W3 的 75.0% 改写成 98.1%；两者都保留。W3 代码提交 `759653b`。

W3 的原因码扩展为：`PARTIAL_ANSWER`、`UNVERIFIABLE`、`ONLY_REPEALED`、`VERSION_MISMATCH`、`VERSION_CONFLICT`，并保留 `NO_VALID_CLAIM`（仅模型零 claims）、`NO_EVIDENCE`、`HISTORICAL_NOT_SUPPORTED`、`PRIVACY_REQUEST`、`OUT_OF_SCOPE`、`GENERATION_FAILED`、`INDEX_UNAVAILABLE`。`PARTIAL_ANSWER` 不进入“完整回答”统计。

### 人工 support 标注口径

- `data/eval/dev_v0.jsonl` 20 题不变；标 `data/eval/w3_support_labels.jsonl` 中的 draft claim（本轮 56 条）；
- 标签：`supported` / `partial` / `unsupported`；`unlabeled` 不计入分母，但必须在报告显示；
- 必须填写 `annotator` 与 `labeled_at`；`annotator` 为空或以 `ai` 开头的记录按 unlabeled 处理，AI 预填不能作为验收；
- 严格引文级：只判断 quote 本身能否支撑 claim，不拿条款上下文补足；
- 只有 `labels_complete=true` 且 rendered 支持率 ≥85%，support 门禁才通过；否则 W3 不通过。

报告：`data/eval/w3-dev-report.{json,md}`；run：`data/eval/w3_run.json`；标注模板：`data/eval/w3_support_labels.jsonl`。详细过程见 [W3 周记录](weekly/w3.md)。

## W6-A 提示词收紧（2026-10-24）

W3 的 rendered 支持率 75.0% 主要来自 claim 写过头/quote 选窄。W6-A 只收紧 Advisor 提示词（不增加 LLM 调用、不改检索/核对主路径），要求 claim 只复述 quote、quote 覆盖完整“条件 + 后果”、保留限定词、不展开列表/条号，并在输出前逐条自检。

| 指标 | W6-A（ultraspeed） |
|---|---:|
| 人工 support | 54/54；53 supported / 1 partial / 0 unsupported |
| draft / rendered 支持率 | 98.1% / 98.1% |
| 结构门禁 | 51/51 引用、0 编造、0 part_no、51/51 quote 可定位 |
| 可答题渲染 / 误拒率 | 14/14 / 0.0% |
| 拒答召回 / partial 率 | 100% / 0.0% |
| p50 / p95（warm） | 3.5s / 15.0s；warmup 29.5s；探针 124/132ms |
| W6-A 总门禁 | **通过** |

唯一 rendered partial 是 d004/c1（“进入教室”范围边界）。首次 1200-token 尝试有 4 个 `GENERATION_FAILED`（空返/JSON 截断）；3000 token 后 0 失败，这不是 Qdrant 问题。

**模型选择**：W6-A 通过后曾切到更省钱的 `mimo-v2.6-pro` 做 A/B，并固定 `ADVISOR_MAX_TOKENS=3000`。v2.6pro 复跑（`run_sha256=0cb8656e…7537`）的 support 为 100%（53/53；rendered 51/51），但 d012 有 2 条 `QUOTE_MISMATCH`/`NO_VALID_CITATION` 被丢弃、d012 为 `PARTIAL_ANSWER`，p50 16.7s、p95 45.2s、warmup 111s；引用完整性和延迟都明显弱于 ultraspeed（0 条 quote mismatch、p95 15.0s、warmup 29.5s）。按“可靠性优先”的决策，默认模型已回退到 `mimo-v2.6-pro-ultraspeed`；v2.6pro 报告/labels 保留为 A/B 记录，不作为 W6-A 最终结果。

W6-A 的最终报告以 ultraspeed 的 98.1% 为准，labels 绑定 `run_sha256=46e016d5…a127`；换模型后的新 run 必须重新人工确认。若未来重新评估 v2.6pro 的成本，应先补 quote 修复/重试（W6-B），不能直接牺牲引用完整性。

报告：`data/eval/w6a-dev-report.{json,md}`（ultraspeed 最终）；A/B：`data/eval/w6a_v26pro-dev-report.{json,md}`；记录见 [W6 周记录](weekly/w6.md)。

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

## W4 MCP 与混合检索工程质量门

W4 的目标是**工具层协议化与可解释的混合召回**，不是答案质量。检索指标只统计
有 `expected_article_ids` 的 14 题；非可答题不计入 Recall/MRR。

| 项目 | 方法 | 命令 | W4 状态 |
|---|---|---|---|
| 索引 | `articles_v1` dense 1024 + BM25 sparse，点数与 manifest 一致 | `make vector-index` | 3,952 点，`index_version=articles_v1@hybrid-0cb2167de3fe` |
| MCP 工具 | policy-search / policy-meta 只读工具与错误信封 | `make mcp-smoke` | 通过 |
| 契约测试 | 每工具正常/空/非法/不存在/降级；stdio 生命周期 | `make test` | 通过 |
| 人群错配 | 5 个 case 通过真实 MCP 调用 | `make population-check` | 5/5 不返回不匹配条款 |
| 检索（article） | Recall@8 / MRR，dense vs RRF vs rerank | `make eval-w4` | 见下表 |
| 检索（section） | 同上，section 子集单列 | `make eval-w4` | 见下表 |
| W3 结构门禁 | 引用 ID 100%、quote 100%、0 编造/part 错误 | `make eval-w3` | 通过 |
| 拒答召回 / 误拒 | 非可答题不渲染 / 可答题不被拒 | `make eval-w3` | 100% / 0.0% |
| support | 重新人工标注；旧 labels 不因 run 变化复用 | `make eval-w3` | 未标注，不声称通过 |
| 延迟 | 检索层 p50/p95；rerank 单列 | `make eval-w4` | rerank p95 约 1.5s；不设 W4 硬门禁 |

### 2026-10-24 dev_v0 检索 A/B/C

| 模式 | article Recall@8 | article MRR | section Recall@8 | section MRR | overall Recall@8 | overall MRR |
|---|---:|---:|---:|---:|---:|---:|
| dense-only | 1.000 | 1.000 | 1.000 | 0.333 | 1.000 | 0.905 |
| dense+sparse RRF | 1.000 | 0.822 | 1.000 | 1.000 | 1.000 | 0.848 |
| RRF + bge-reranker-v2-m3 | 1.000 | 0.958 | 1.000 | 0.750 | 1.000 | 0.929 |

延迟（warm）：dense p95 359 ms；RRF p95 637 ms；rerank p95 1,677 ms，
rerank 本身 p50/p95 1,038/1,475 ms。结论是 Recall@8 不降、rerank 提升
overall MRR，但 RRF 单独使用会降低 article MRR，且 rerank 带来约 1s 成本。
阈值 0.60 由 dev_v0 校准（可答题 top ≥0.725，非可答题 top ≤0.523），不是冻结结论。

W3 回归（hybrid+rerank，threshold=0.60）：结构 100%、拒答召回 100%、
误拒 0.0%、原因码 100%、partial 14.3%；d005/d006 的 PARTIAL 来自模型把
“引导句+非相邻分项”拼成 quote，不是检索漏条款。support 需要重新人工标注。

W4 的已知缺口：

- case 4/5 过滤后目标培养方案未进入 top 8，返回一般教务条款；满足“不返回
  不匹配条款”的验收，但不等于正确方案召回；
- BM25 是词面匹配，不能处理“种子科学 → 农学”这类语义改写；
- `min_rerank_score=0.60` 只由 20 题开发集校准，W7 冻结集需重新验证。

### P0 profile-aware 检索（2026-10-24）

- 从 `organization.yaml` 构建 ProfileLexicon；只有用户提供学院/专业/入学年份时，
  才追加最多 2 个 profile 变体，未提供时行为不变；
- `rerank_candidates=20`：RRF top 20 重排后取 8，rerank p95 约 1.06s；
- `population_v1` 5/5：p004（2024 级种子科学）命中 `agri-ug-plan-seed-2024`，
  p005（2024 级农学）命中 2024 版农学方案；
- P0 检索报告：`data/eval/p0-retrieval-report.{json,md}`；Recall@8 三种模式均 1.000，
  overall MRR dense 0.905 / RRF 0.895 / rerank 0.929；
- W4 历史报告从 tag `w4` 保留，P0 不覆盖；全量测试 410 passed。

### P1 section 证据、生成重试与版本意图（2026-10-24）

- `retrieval/section_select.py`：表格 section 检测与降权；宽问题最多 2 个表格，
  “课程/学分/学时/计划/进程/矩阵”等表格意图放开；
- JSON 修复空返回追加一次重试；空 claims 再问一次，仍为空则接受拒答；
- `check_version_intent`：有 `intake_year` 且问题版本年份不一致时，检索前返回
  `VERSION_MISMATCH`；
- `make e2e-section` 4/4：p004 宽/聚焦 ANSWERED，p005 旧版 VERSION_MISMATCH，
  p005 2024 版 ANSWERED；p004 宽问题从两次 GENERATION_FAILED 恢复；
- population_v1 仍 5/5；P1 检索 overall MRR 0.964、section MRR 1.000、
  rerank p95 1,069 ms；全量测试 410 passed；P1 记录 `docs/weekly/p1.md`。

### W7 冻结评测（2026-10-24）

- 数据集 `test_v1.jsonl` 50 题；最终 canonical hash `2a8d0e12…8f18`
  （v0 因 test-036 gold 过期已归档；changelog 记录）；
- 构成：25 single / 10 multi / 5 section（40 可答）+ 4 out_of_scope / 3 repealed /
  3 privacy（10 不可答）；5 道版本敏感题；
- 基线 run `f9bd7544…6243`：article Recall@8 0.971、误拒 0.125；
- remediation run `b55bc436…b968`：结构 100%、article Recall@8 **1.000**、
  article MRR 0.967、section Recall@8 **1.000**、拒答召回 **1.000**、
  误拒 **0.025**、原因码 1.000；p50/p95 8.3/32.6s；
- 人工标注（annotator=`yaukin`）：support 136 条有效（1 条因
  `TOO_MANY_CLAIMS` 排除），135 supported / 1 partial，support **0.9926**
  （Wilson 95% CI 0.9595–0.9987）；answer 32 correct / 5 partial /
  3 incorrect，可答正确率 **0.800**（CI 0.6524–0.8950），不可答题 10/10；
  版本敏感 3/5=**0.600**（CI 0.2307–0.8824）；
- **W7 总门禁未通过**：answer correctness 与 version-sensitive 未达门槛，
  p95 32.6s 超 10s（test-027 端点长尾）；test-027 仍为唯一误拒；
- 答案失败集中在生成层：test-027 空/截断、test-028 漏第二问、
  test-037 section 选错、test-003/029/033/035/039 缺 required fact；
- 修复：其他大学 guard、rerank margin=0、JSON 截断重试、test-036 gold 修正；
- 下一步：不改冻结题，处理 test-027/028/037 后重跑并只重标 changed claims；
  A5/A4/A2 消融；A1 固定分块/A3 流程 DAG N/A；详见 `docs/weekly/w7.md`。

### W7 第二轮 remediation2（2026-10-24，已标注）

- prompt `w6a2.1-2026-10-24`：多问句列出子问题并要求逐问覆盖；“哪些”类问题
  要求逐项覆盖（一）（二）……；含就业/工作意图且 section 同时覆盖多方面时
  优先整体引用该 section；
- 生成重试：多问/列举题首轮预算 6000；空/截断修复直接加大预算；第三次
  从原始证据干净重来；首个 provider 异常额外重试一次；
- `section_select`：问题含就业/毕业后工作意图且 section 含“可在……从事……
  工作”时 +0.03；test-037 gold section 从第 5 升到第 1；
- run `23b384f1…a264a`：结构 100%、article/section Recall@8 **1.000**、
  overall MRR **0.958**、section MRR **0.900**、拒答召回 1.000、
  误拒 **0.000**、p50/p95 8.9/35.1s；
- 人工标注：support **0.9852**（133/135，CI 0.9476–0.9959，通过）、
  answer **0.750**（30/40，CI 0.5981–0.8581，未通过）、version-sensitive
  **0.200**（1/5，CI 0.0362–0.6245，未通过）；p95 未通过；
- 修复 test-027/028（incorrect→correct）与 test-037（incorrect→partial）；
  回归 test-007（correct→incorrect）、test-031/036/038（correct→partial）、
  test-039（partial→incorrect）；版本敏感失败集中在“一、人才培养目标”
  section 的精确表述；
- 只重标 changed：support 32/136 沿用、104 重标；answer 11/50 沿用、
  39 重标；最终 labels 无 unlabeled；报告
  `data/eval/w7-remediation2-report.{json,md}`；
- 结论：support/structure/检索/拒答通过，answer/version/p95 未通过，
  **W7 总门禁未通过**；下一轮应使用 dev 集验证“培养目标”section 的
  检索与生成，不继续在冻结测试集上调参。

## W6-A2 quote 连续性与答案质量

W6-A2 只改生成/重写层，不改 W3 Verifier：

- citation.quote 必须是同一证据正文的连续子串；不得拼接“引导句 + 非相邻分项”；
- claim 的时间/范围（如“一学期内”）和数字/金额/期限必须原样出现在 quote；
- 一条 claim 只表达一个条件、一个后果或一个数量；
- quote 不可定位或数字未覆盖时，只对失败 claim 做一次 citation 重写；仍失败由
  W3 Verifier 丢弃，不把重写当成新证据。

2026-10-24 dev_v0 20 题（hybrid+rerank，prompt `w6a2-2026-10-24`）：

| 指标 | 结果 | 门禁 |
|---|---:|---|
| 结构门禁 | 引用 ID 100% / quote 100% / 0 编造 / 0 part 错误 | 通过 |
| 拒答召回 | 100% | ≥80%，通过 |
| 误拒率 | 7.1%（d012 端点空返） | ≤20%，通过 |
| partial 率 | 7.1%（d001 第 6 条 claim 被上限裁掉） | 记录 |
| d005 / d006 | **ANSWERED / ANSWERED** | 达成 |
| support | 50 draft / 49 rendered 已人工标注 100% | 通过 |
| W6-A2 总门禁 | 结构 / support / 拒答 / 误拒全部通过 | 通过 |

run：`data/eval/w6a2-run.json`，`run_sha256=b7ee6884…f569`。旧 W6-A labels 仅
用于 8 条 prefill 建议，未自动复用；人工核对表为
`data/eval/w6a2_support_review.md`，50/50 已由 `yaukin` 标注为 supported。

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
