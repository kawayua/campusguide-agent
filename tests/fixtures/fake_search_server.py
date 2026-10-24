"""stdio 契约测试用的轻量 MCP server；不加载任何模型或语料。"""

from __future__ import annotations

from fastmcp import FastMCP

from campusguide.mcp.schemas import SearchArticlesRequest

mcp = FastMCP("fake-policy-search")
_calls = {"search_articles": 0}


@mcp.tool(name="search_articles")
def search_articles(request: SearchArticlesRequest) -> dict:
    _calls["search_articles"] += 1
    if _calls["search_articles"] == 1:
        return {
            "ok": False,
            "schema_version": "1.0",
            "error": {
                "code": "RERANK_UNAVAILABLE",
                "message": "模拟首次重排失败",
                "retryable": True,
                "details": {},
            },
        }
    return {
        "ok": True,
        "schema_version": "1.0",
        "data": {
            "query": request.query,
            "queries": [request.query],
            "articles": [],
            "index_version": "fake@stdio",
            "degraded": False,
            "candidates": 0,
            "applied_filters": [],
            "warnings": [],
            "timing": {"dense_ms": 0.0, "sparse_ms": 0.0, "rerank_ms": 0.0},
        },
        "warnings": [],
    }


@mcp.tool(name="get_article")
def get_article(article_id: str, part_no: int | None = None) -> dict:
    return {
        "ok": False,
        "schema_version": "1.0",
        "error": {
            "code": "NOT_FOUND",
            "message": f"fake 未找到 {article_id}",
            "retryable": False,
            "details": {},
        },
    }


if __name__ == "__main__":
    mcp.run(transport="stdio")
