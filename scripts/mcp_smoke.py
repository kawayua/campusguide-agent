"""W4 MCP smoke：启动两个 stdio server，调用 search/get/meta 工具。

不启动 Gradio、不调用 LLM；用于验证真实索引与模型能通过 MCP 工作。
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

from campusguide.config import load_mcp_config
from campusguide.env import load_dotenv
from campusguide.mcp.client import (
    MCPClientConfig,
    MCPPolicyMetaClient,
    MCPPolicySearchClient,
)
from campusguide.mcp.schemas import SearchArticlesRequest

ROOT = Path(__file__).resolve().parents[1]


def _client_config(server) -> MCPClientConfig:
    command = list(server.command)
    if command and command[0] in {"python", "python3"}:
        command[0] = sys.executable
    return MCPClientConfig(
        command=command,
        timeout_seconds=server.timeout_seconds,
        retries=server.retries,
        startup_timeout_seconds=server.startup_timeout_seconds,
        cwd=str(ROOT),
    )


def main() -> int:
    load_dotenv(ROOT / ".env")
    config = load_mcp_config(ROOT / "configs" / "mcp.yaml")
    search = MCPPolicySearchClient(_client_config(config.search_server))
    meta = MCPPolicyMetaClient(_client_config(config.meta_server))
    try:
        started = time.perf_counter()
        search.start()
        print(f"policy-search ready in {int((time.perf_counter() - started) * 1000)}ms")
        started = time.perf_counter()
        meta.start()
        print(f"policy-meta ready in {int((time.perf_counter() - started) * 1000)}ms")

        request = SearchArticlesRequest(
            query="本科生国家奖学金申请条件",
            filters={"audience": ["undergraduate"], "as_of": "2026-10-24"},
            top_k=10,
            rerank_top_n=5,
        )
        started = time.perf_counter()
        response = search.search_articles(request)
        print(
            f"search_articles ok: {len(response.data.articles)} hits, "
            f"index={response.data.index_version}, "
            f"{int((time.perf_counter() - started) * 1000)}ms"
        )

        def _fmt(value):
            return "n/a" if value is None else f"{value:.4f}"

        for hit in response.data.articles[:3]:
            print(
                f"  #{hit.rank} {hit.article_id} "
                f"dense={_fmt(hit.dense_score)} sparse={_fmt(hit.sparse_score)} "
                f"rerank={_fmt(hit.rerank_score)}"
            )
        if not response.data.articles:
            print("error: search_articles 返回空结果，smoke 失败", file=sys.stderr)
            return 1

        first = response.data.articles[0]
        article = search.get_article(first.article_id, first.part_no)
        print(f"get_article ok: {article.article.article_id} status={article.article.status.value}")
        versions = meta.get_document_versions(first.article.doc_id)
        print(
            f"get_document_versions ok: {first.article.doc_id} {len(versions['versions'])} versions"
        )
        resolved = meta.resolve_version(first.article.doc_id, "2026-10-24", "current")
        print(f"resolve_version ok: selected={resolved['selected_version_id']}")
        print(json.dumps({"index_version": response.data.index_version}, ensure_ascii=False))
    finally:
        search.close()
        meta.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
