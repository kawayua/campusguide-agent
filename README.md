# CampusGuide

面向高校公开制度文档的**条款级**问答与办事指引 Agent（个人作品集项目）。

当前目标院校：**华南农业大学**。当前进度：W1（数据与索引地基）。

> 本系统为个人演示项目，只覆盖指定学校的公开制度文件，不构成官方答复。具体政策以学校官网和负责部门解释为准。请勿输入学号、身份证、成绩单等个人信息。

## W1 能做什么

- 用 `configs/sources.yaml` 登记制度文件与版本；
- 抓取（或复制本地）HTML/PDF 快照，记录 URL、发布日期、哈希与抓取时间；
- 解析正文、切分“章/条”、清洗联系方式、生成条款元数据；
- 生成 `data/processed/articles.jsonl`、`documents.jsonl` 与 `data/manifest.json`；
- 初始化 Qdrant 集合骨架（`articles_v1`，W2 再写入向量）；
- 抽样 20 条条款生成人工切分核对清单。

问答链路（Planner / Retriever / Advisor / Verifier）从 W2 开始实现，本阶段不包含。

## 快速开始

```bash
make init                 # uv sync
cp .env.example .env      # 按需填写，W1 不强制
make validate-sources     # 查看来源清单统计
make fetch                # 抓取/复制原始快照到 data/raw（已 gitignore）
make parse                # 解析到 data/processed/parsed（已 gitignore）
make index                # 生成条款 JSONL 与 manifest
make check-segmentation   # 生成 20 条人工抽检清单
make test                 # 离线测试
```

Windows 下若 `uv` 不在 PATH，可显式执行 `make init UV="python -m uv"`。

## 来源清单

`configs/sources.yaml` 是唯一数据入口，字段与填写方式见 [docs/data-sources.md](docs/data-sources.md)。

W1 验收目标：文档版本 ≥ 50 个、制度本体 ≥ 30 个；20 条抽检切分正确率 ≥ 95%。当前语料由项目作者收集，仓库只提交清单、元数据与哈希，不提交完整原文。

## 目录

```text
configs/            来源清单、模型/检索/拒答配置、流程骨架
data/               本地语料与生成物（raw/snapshots/processed 不入库）
scripts/            fetch / parse / index / validate / 抽检 CLI
src/campusguide/    domain / ingest / retrieval / flows / agents / mcp / eval / ui
tests/              unit / contract / integration / redteam
docs/               数据来源、评测、面试笔记、周记录
```

## 合规

- 只使用学校官网公开可访问的制度文件；
- 不抓取登录后页面、申请表、成绩单、名单等个人信息；
- 完整快照与解析文本只保存在本地，不进入公开仓库；
- 索引文本对手机号、身份证号、学号、个人邮箱等做脱敏，引用链接指回官网原文；
- 删除/更正入口：如学校要求下架，删除对应 `data/raw`、`data/processed` 与索引即可。

## 计划

详细设计见 `docs/superpowers/specs/2026-10-19-campusguide-design.md`，实施计划见 `docs/superpowers/plans/2026-10-19-campusguide-implementation-plan.md`。
