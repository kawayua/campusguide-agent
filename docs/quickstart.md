# 部署与复现

## 离线界面

从[项目主页](../README.md#-快速开始)开始。`make demo-fake` 对应 `DEMO_FAKE=1 uv run python -m campusguide.ui.app`，不加载检索模型，不连接 Qdrant 或远程 LLM。

## 已有语料与索引：运行真实问答

在 Bash/WSL 中把 `.env.example` 复制为 `.env`，根据文件说明配置模型地址、密钥和模型名。现有索引应与语料、manifest 和服务指向的 Qdrant 集合一致。

```bash
cp .env.example .env
# 编辑 .env 后再继续。
make demo-preflight
make demo-up
```

Docker 模式提供 `http://127.0.0.1:7860/` 和 Qdrant `6333/6334` 端口。镜像不打包语料、模型或 `.env`，通过本地挂载使用 `data/` 和模型缓存。首次启动会下载 bge-m3 和重排序模型；可用 `docker compose logs -f app` 查看状态。

主机模式可运行 `make init-w2`、`make compose-up`（启动 Qdrant）和 `make demo`；开始前仍需准备语料与索引。

## 没有索引：基础建库

```bash
make init-w2
make compose-up
make fetch
make parse-native
make index
make vector-index
make demo
```

`make parse-native` 用于原生解析诊断。`make parse` 或 `make parse-vision` 可进入配置中的视觉解析链路。仓库还提供 `scripts/run_xiaomi_ocr_local.py` 和 `scripts/run_ocr_router.py`，后者输出区域识别产物，不写 Qdrant。当前公开命令没有把本轮 Xiaomi 全量 OCR、Gemini 回退、覆盖核验和入库过程串成可复跑的单一入口，也没有附该轮完整运行配置。新建或更新索引时，先确认目标集合和 manifest。

- `make qa`：运行 Ruff、格式检查和离线测试。
- `make w8-smoke`：在真实服务所需配置齐全时，验证五个问答和办理场景。
- `make demo-ui-smoke`：应用启动后验证页面。场景见[演示脚本](demo-script.md)。
