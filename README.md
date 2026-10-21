# CampusGuide

面向高校公开制度文档的**条款级**问答与办事指引 Agent（个人作品集项目）。

当前目标院校：**华南农业大学**。当前进度：W1 数据与索引地基完成，已收录 77 份文档 / 105 个版本（含两份 2020 级扫描件 OCR）；问答链路从 W2 开始实现。

> 本系统为个人演示项目，只覆盖指定学校的公开制度文件，不构成官方答复。具体政策以学校官网和负责部门解释为准。请勿输入学号、身份证、成绩单等个人信息。

## W1 能做什么

- 用 `configs/sources.yaml` 登记制度文件与版本；
- 抓取（或复制本地）HTML/PDF 快照，记录 URL、发布日期、哈希与抓取时间；
- 解析正文、切分“章/条”、按规则识别版本、脱敏联系方式，生成条款元数据；
- 生成 `data/processed/articles.jsonl`、`documents.jsonl` 与 `data/manifest.json`；
- 初始化 Qdrant 集合骨架（`articles_v1`，W2 再写入向量）；
- 抽样 20 条条款生成人工切分核对清单；
- 用离线测试和 ruff 守住切分、脱敏、版本解析与索引确定性。

问答链路（Planner / Retriever / Advisor / Verifier）从 W2 开始实现，本阶段不包含。

## 快速开始

```bash
make init                 # uv sync
cp .env.example .env      # 按需填写，W1 不强制
# 把下载好的公开制度文件放到 data/incoming/，再填写 configs/sources.yaml
make validate-sources     # 查看来源清单统计与缺失字段
make fetch                # 抓取/复制原始快照到 data/raw（已 gitignore）
make parse                # 解析到 data/processed/parsed（已 gitignore）
make index                # 生成条款 JSONL 与 data/manifest.json
make index-qdrant         # 额外创建 Qdrant 集合骨架（需本地 Qdrant）
make check-segmentation   # 生成 20 条人工抽检清单
make qa                   # ruff + 全量离线测试
```

`local_path` 建议指向 `data/incoming/` 下的文件；该目录与所有快照、解析结果一样不会进入公开仓库。`url` 仍须填写，作为最终引用链接。

Windows 下若 `uv` 不在 PATH，可显式执行 `make init UV="python -m uv"`。若未安装 `make`，在 Git Bash 中使用等价命令：

```bash
python -m uv sync
python -m uv run python scripts/validate_sources.py
python -m uv run python scripts/fetch_corpus.py
python -m uv run python scripts/parse_corpus.py
python -m uv run python scripts/build_index.py
python -m uv run python scripts/check_segmentation.py
python -m uv run pytest
python -m uv run ruff check .
```

扫描版 PDF 需要可选 OCR 依赖。识别缓存只写入本地 `data/processed/ocr/`，原始 PDF 不改动；解析时按 SHA-256 匹配后自动接入：

```bash
python -m uv sync --group ocr
python -m uv run --group ocr python scripts/ocr_scans.py --only <doc_id>@<version_id>
```

OCR 默认逐页比较 200dpi 与 240dpi 两个渲染结果，以覆盖更多的版本为底稿并补齐漏行；表格仍按 OCR 行序保留，文本未经逐字校对，引用以官网原文为准。

## 来源清单

`configs/sources.yaml` 是唯一数据入口，字段与填写方式见 [docs/data-sources.md](docs/data-sources.md)。

W1 验收目标：文档版本 ≥ 50 个、制度本体 ≥ 30 个；20 条抽检切分正确率 ≥ 95%。当前语料由项目作者收集，仓库只提交清单、元数据与哈希，不提交完整原文。字段说明、版本规则和抽检流程见该文档。

## 目录

```text
configs/            来源清单、模型/检索/拒答配置、流程骨架
data/               本地语料与生成物（incoming/raw/snapshots/processed 不入库）
scripts/            fetch / parse / index / validate / 抽检 CLI
src/campusguide/    domain / ingest / retrieval / flows / agents / mcp / eval / ui
tests/              unit / contract / integration / redteam（全部离线）
docs/               数据来源、评测与质量门、周记录
```

## 质量门

`make qa`（等价于 `scripts/qa.sh`）执行 ruff 与全量 pytest。所有测试使用 mock HTTP、临时目录和动态生成的 PDF，不访问学校站点，也不需要 Qdrant 服务。当前测试覆盖抓取、解析、切分、脱敏、版本解析、索引确定性、Qdrant 集合骨架与本地 HTML 端到端管线。

W1 的验收状态、实测数字和遗留风险记录在 [docs/weekly/w1.md](docs/weekly/w1.md)；W1 工程质量门与 W7 效果评测的边界见 [docs/evaluation.md](docs/evaluation.md)。GitHub Actions 工作流位于 `.github/workflows/ci.yml`；Gitee 仓库可使用本地 `make qa`。

## 合规

- 只使用学校官网公开可访问的制度文件；
- 不抓取登录后页面、申请表、成绩单、名单等个人信息；
- 完整快照、解析文本与 `data/incoming/` 只保存在本地，不进入公开仓库；
- 索引文本对手机号、身份证号、学号、个人邮箱等做脱敏，引用链接指回官网原文；
- 删除/更正入口：如学校要求下架，删除对应 `data/raw`、`data/processed` 与索引即可。

## 计划

详细设计见 [docs/superpowers/specs/2026-10-19-campusguide-design.md](docs/superpowers/specs/2026-10-19-campusguide-design.md)，实施计划见 [docs/superpowers/plans/2026-10-19-campusguide-implementation-plan.md](docs/superpowers/plans/2026-10-19-campusguide-implementation-plan.md)，W1 周记录见 [docs/weekly/w1.md](docs/weekly/w1.md)。
