# 评测与质量门

CampusGuide 的评测分为两个阶段，不能混为一谈：

1. **W1 工程质量门**：语料、切分、脱敏、确定性、索引构建是否可靠；
2. **W7 系统效果评测**：问答正确性、引用支持率、拒答、流程指引与成本/延迟。

W1 尚无问答模块，因此**没有任何回答质量指标**。简历、README 和面试材料中不得把工程质量门写成问答效果。

## W1 工程质量门

| 项目 | 方法 | 命令 | 当前状态 |
|---|---|---|---|
| 代码规范 | ruff（E/F/I/B/UP/SIM/RUF） | `make lint` | 通过 |
| 单元与集成测试 | 全离线：mock HTTP、临时目录、动态生成 PDF | `make test` | 64 passed |
| 索引确定性 | 同一输入重建，`content_hash` 与 JSONL 字节一致 | `pytest tests/unit/test_build_index.py` | 通过 |
| 联系方式脱敏 | 手机号、身份证、学号、邮箱、联系人姓名 | `pytest tests/unit/test_redact.py` | 通过 |
| 版本解析 | CURRENT / HISTORICAL / INCLUDE_REPEALED、冲突、无版本 | `pytest tests/unit/test_versioning.py` | 通过 |
| 条款切分 | 章节路径、条款边界、长条款 part、重复编号、落款 | `pytest tests/unit/test_segment.py` | 通过 |
| 端到端管线 | 本地 HTML → 快照 → 解析 → 切分 → JSONL + manifest | `pytest tests/integration` | 通过 |
| Qdrant 骨架 | 内存模式创建 named dense + sparse、payload 索引、幂等 | `pytest tests/unit/test_build_index.py` | 通过 |
| 语料覆盖 | 制度 ≥30、版本 ≥50 | `make validate-sources` | 待真实语料补齐 |
| 切分人工抽检 | 20 条，四项判定，正确率 ≥95% | `make check-segmentation` | 待真实语料补齐 |

统一入口：

```bash
make qa     # ruff check + 全量离线 pytest
```

CI 配置见 `.github/workflows/ci.yml`；Gitee 仓库当前以本地 `make qa` 或 `scripts/qa.sh` 作为质量门。

### 当前实测数字

截至 2026-10-19：

- `ruff check .`：通过；
- `pytest`：64 passed（unit + integration，无网络依赖）；
- 空语料构建：`documents=0, versions=0, articles=0`，manifest `content_hash` 可重复；
- 真实语料指标：未开始，等待 `configs/sources.yaml` 补齐。

### 可复现性约束

- 依赖锁定在 `uv.lock`；
- 每个版本的原始快照记录 SHA-256、抓取时间与解析器版本；
- `manifest.content_hash` 只覆盖条款与版本文档，不包含 `generated_at`，因此可用于判断索引是否一致；
- 抓取与解析测试全部离线，CI 不访问学校站点。

## W7 系统效果评测（计划）

| 维度 | 指标 | 目标 |
|---|---|---|
| 引用可信 | 引用 ID 有效率、引文可定位率 | 100% |
| 引用支持 | 引用支持率 | ≥95% |
| 可回答题 | 答案正确性/覆盖度、误拒率 | 误拒 ≤10% |
| 不可回答题 | 拒答率、reason code 正确率 | ≥90% |
| 检索 | Recall@k、MRR、条款级命中率 | 开发集校准 |
| 流程 | 必需材料完整率、条件判断正确率 | checklist ≥90% |
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
| 失败类型 | 检索未命中 / 版本错误 / 引用不支持 / 过度拒答 / 漏拒答 / 流程事实错误 |
| 根因 | 语料、切分、过滤、提示词、核对规则、模型能力 |
| 影响范围 | 是否影响一类问题 |
| 处理决定 | 修复、接受并记录、或移出测试集（仅限标注错误） |

测试集冻结后，指标不达标时只修错误归因中占比最高的两类问题，不改测试集。
