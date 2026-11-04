from __future__ import annotations

import base64
import hashlib
import json
import struct
import subprocess
import zlib
from pathlib import Path
from typing import Any

import httpx

from campusguide.ingest import html_visual
from campusguide.ingest.html_visual import (
    HtmlRenderOptions,
    build_edge_command,
    prepare_html_render,
)

LONG_ZH = (
    "华南农业大学本科培养方案课程信息与教学安排说明，本页包含公式图片、"
    "课程表格和培养目标说明，供学生查阅。公式资源用于说明课程计算方式，"
    "正文内容必须完整保留，导航和脚本必须移除。"
)


def _png_chunk(kind: bytes, payload: bytes) -> bytes:
    return (
        struct.pack(">I", len(payload))
        + kind
        + payload
        + struct.pack(">I", zlib.crc32(kind + payload) & 0xFFFFFFFF)
    )


def _png_bytes() -> bytes:
    return (
        b"\x89PNG\r\n\x1a\n"
        + _png_chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0))
        + _png_chunk(b"IDAT", zlib.compress(b"\x00\x00\x00\x00"))
        + _png_chunk(b"IEND", b"")
    )


def _options(tmp_path: Path, **overrides: Any) -> HtmlRenderOptions:
    values: dict[str, Any] = {
        "cache_dir": tmp_path / "cache",
        "edge_path": "unused-edge.exe",
    }
    values.update(overrides)
    return HtmlRenderOptions(**values)


def _source(
    tmp_path: Path,
    html: str,
    *,
    meta: dict[str, Any] | None = None,
) -> Path:
    documents = tmp_path / "docs"
    documents.mkdir(exist_ok=True)
    source = documents / "source.html"
    source.write_text(html, encoding="utf-8")
    if meta is not None:
        (documents / "meta.json").write_text(
            json.dumps(meta, ensure_ascii=False),
            encoding="utf-8",
        )
    return source


def _mock_render(monkeypatch: Any) -> None:
    def fake_render(_html_path: Path, pdf_path: Path, _options: HtmlRenderOptions) -> None:
        pdf_path.write_bytes(b"%PDF-1.4\n% offline fixture\n")

    monkeypatch.setattr(html_visual, "_render_html_to_pdf", fake_render)


def test_relative_formula_uses_verified_manifest_cache(tmp_path: Path, monkeypatch: Any) -> None:
    png = _png_bytes()
    digest = hashlib.sha256(png).hexdigest()
    image_path = tmp_path / "official" / "formula.png"
    image_path.parent.mkdir()
    image_path.write_bytes(png)
    manifest_path = tmp_path / "image_resource_fetch.json"
    manifest_path.write_text(
        json.dumps(
            {
                "resources": [
                    {
                        "url": "https://www.scau.edu.cn/images/formula.png",
                        "sha256": digest,
                        "bytes": len(png),
                        "content_type": "image/png",
                        "local_path": "official/formula.png",
                    }
                ]
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    source = _source(
        tmp_path,
        (
            "<!doctype html><html><body><div class='paging_content wp_articlecontent'>"
            f"<p>{LONG_ZH}</p>"
            "<img src='../images/formula.png' alt='公式图片'>"
            "</div></body></html>"
        ),
        meta={"final_url": "https://www.scau.edu.cn/college/article.htm"},
    )
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(500)

    _mock_render(monkeypatch)
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        bundle = prepare_html_render(
            source,
            _options(tmp_path, resource_manifest=manifest_path),
            doc_id="doc",
            version_id="v1",
            client=client,
        )

    assert calls == []
    assert len(bundle.resources) == 1
    resource = bundle.resources[0]
    assert resource.asset_id == "doc@v1#image1"
    assert bundle.assets[0].asset_id == resource.asset_id
    assert resource.dom_path
    assert resource.ref == "src"
    assert resource.original_ref == "../images/formula.png"
    assert resource.url == "https://www.scau.edu.cn/images/formula.png"
    assert resource.final_url == resource.url
    assert resource.status == "embedded"
    assert resource.sha256 == digest
    assert resource.byte_size == len(png)
    assert resource.attempts == 0
    assert resource.cooldown_seconds == 0.0
    assert resource.local_path is not None
    assert Path(resource.local_path).resolve() == image_path.resolve()
    assert resource.cache_path is not None
    cache_path = Path(resource.cache_path)
    assert cache_path.read_bytes() == png
    clean_path = bundle.manifest_path.parent / "clean.html"
    clean = clean_path.read_text(encoding="utf-8")
    assert f"data:image/png;base64,{base64.b64encode(png).decode('ascii')}" in clean
    assert f'data-asset-id="{resource.asset_id}"' in clean
    assert bundle.clean_html_sha256 == hashlib.sha256(clean_path.read_bytes()).hexdigest()
    manifest = json.loads(bundle.manifest_path.read_text(encoding="utf-8"))
    assert manifest["resources"][0]["sha256"] == digest
    assert manifest["resources"][0]["ref"] == "src"
    assert manifest["resources"][0]["original_ref"] == "../images/formula.png"
    assert manifest["resources"][0]["local_path"] == str(image_path)


def test_redirect_boundary_mime_and_size_failures(tmp_path: Path, monkeypatch: Any) -> None:
    png = _png_bytes()
    max_bytes = len(png) + 32
    base = "https://www.scau.edu.cn"
    requests: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        requests.append(url)
        if url == f"{base}/formula.png":
            return httpx.Response(
                302,
                headers={"Location": "https://img.scau.edu.cn/static/formula.png"},
            )
        if url == "https://img.scau.edu.cn/static/formula.png":
            return httpx.Response(200, content=png, headers={"Content-Type": "image/png"})
        if url == f"{base}/offsite.png":
            return httpx.Response(
                302,
                headers={"Location": "https://evil.example/formula.png"},
            )
        if url == "https://evil.example/formula.png":
            return httpx.Response(200, content=png, headers={"Content-Type": "image/png"})
        if url == f"{base}/mime.png":
            return httpx.Response(200, content=png, headers={"Content-Type": "image/jpeg"})
        if url == f"{base}/large.png":
            return httpx.Response(
                200,
                content=png + b"x" * 128,
                headers={"Content-Type": "image/png"},
            )
        if url == f"{base}/hop.png":
            return httpx.Response(302, headers={"Location": f"{base}/hop2.png"})
        if url == f"{base}/hop2.png":
            return httpx.Response(302, headers={"Location": f"{base}/hop3.png"})
        if url == f"{base}/hop3.png":
            return httpx.Response(200, content=png, headers={"Content-Type": "image/png"})
        return httpx.Response(404)

    source = _source(
        tmp_path,
        (
            "<!doctype html><html><body>"
            f"<img src='{base}/formula.png' alt='formula'>"
            f"<img src='{base}/offsite.png' alt='offsite'>"
            f"<img src='{base}/mime.png' alt='mime'>"
            f"<img src='{base}/large.png' alt='large'>"
            f"<img src='{base}/hop.png' alt='hop'>"
            "</body></html>"
        ),
    )
    _mock_render(monkeypatch)
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        bundle = prepare_html_render(
            source,
            _options(tmp_path, max_bytes=max_bytes, redirect_limit=1),
            client=client,
        )

    by_url = {resource.url: resource for resource in bundle.resources}
    success = by_url[f"{base}/formula.png"]
    assert success.status == "embedded"
    assert success.final_url == "https://img.scau.edu.cn/static/formula.png"
    assert success.attempts == 2
    assert by_url[f"{base}/offsite.png"].status == "failed"
    assert by_url[f"{base}/mime.png"].status == "failed"
    assert by_url[f"{base}/large.png"].status == "failed"
    assert by_url[f"{base}/hop.png"].status == "failed"
    assert "https://evil.example/formula.png" not in requests
    assert f"{base}/hop3.png" not in requests
    assert any("官方允许列表" in warning for warning in bundle.warnings)
    assert any("Content-Type" in warning for warning in bundle.warnings)
    assert any("大小上限" in warning for warning in bundle.warnings)
    assert any("重定向超过" in warning for warning in bundle.warnings)


def test_429_cooldown_is_injected_and_recorded(tmp_path: Path, monkeypatch: Any) -> None:
    png = _png_bytes()
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        if len(calls) == 1:
            return httpx.Response(429, headers={"Retry-After": "7"})
        return httpx.Response(200, content=png, headers={"Content-Type": "image/png"})

    source = _source(
        tmp_path,
        (
            "<!doctype html><html><body>"
            "<img src='https://www.scau.edu.cn/limited.png' alt='limited'>"
            "</body></html>"
        ),
    )
    delays: list[float] = []
    _mock_render(monkeypatch)
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        bundle = prepare_html_render(
            source,
            _options(tmp_path, sleeper=delays.append),
            client=client,
        )

    assert delays == [180.0]
    assert len(calls) == 2
    resource = bundle.resources[0]
    assert resource.status == "embedded"
    assert resource.attempts == 2
    assert resource.cooldown_seconds == 180.0
    assert any("429" in warning for warning in bundle.warnings)


def test_retry_after_above_platform_floor_is_preserved(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    png = _png_bytes()
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        if len(calls) == 1:
            return httpx.Response(429, headers={"Retry-After": "300"})
        return httpx.Response(200, content=png, headers={"Content-Type": "image/png"})

    source = _source(
        tmp_path,
        (
            "<!doctype html><html><body>"
            "<img src='https://www.scau.edu.cn/long-wait.png' alt='long wait'>"
            "</body></html>"
        ),
    )
    delays: list[float] = []
    _mock_render(monkeypatch)
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        bundle = prepare_html_render(
            source,
            _options(tmp_path, sleeper=delays.append),
            client=client,
        )

    assert delays == [300.0]
    assert len(calls) == 2
    resource = bundle.resources[0]
    assert resource.status == "embedded"
    assert resource.attempts == 2
    assert resource.cooldown_seconds == 300.0


def test_clean_html_keeps_content_and_removes_navigation_and_scripts(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    png = _png_bytes()
    data_uri = f"data:image/png;base64,{base64.b64encode(png).decode('ascii')}"
    source = _source(
        tmp_path,
        (
            "<!doctype html><html><head>"
            "<link rel='stylesheet' href='https://evil.example/site.css'>"
            "<style>@import url('https://evil.example/import.css');</style>"
            "</head><body>"
            "<nav>导航菜单不应进入正文</nav>"
            "<div class='paging_content wp_articlecontent'>"
            f"<p>{LONG_ZH}</p>"
            "<style>.x{background:url(https://evil.example/bg.png)}</style>"
            "<table><tr><th>课程</th><th>学分</th></tr>"
            "<tr><td>软件工程实践</td><td>2</td></tr></table>"
            "<iframe src='https://evil.example/frame'></iframe>"
            "<script>alert('x')</script>"
            "<p><a href='https://www.scau.edu.cn/next' onclick='alert(1)'>下一页</a></p>"
            f"<img src='{data_uri}' alt='公式图片'>"
            "</div></body></html>"
        ),
    )
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(500)

    _mock_render(monkeypatch)
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        bundle = prepare_html_render(source, _options(tmp_path), client=client)

    assert calls == []
    clean_path = bundle.manifest_path.parent / "clean.html"
    clean = clean_path.read_text(encoding="utf-8")
    assert "<table" in clean
    assert "软件工程实践" in clean
    assert "导航菜单" not in clean
    assert "<script" not in clean.lower()
    assert "<iframe" not in clean.lower()
    assert "onclick" not in clean.lower()
    assert "href=" not in clean
    assert "@import" not in clean
    assert "https://evil.example" not in clean
    assert "default-src 'none'" in clean
    assert "img-src data: file:" in clean
    assert "style-src 'unsafe-inline'" in clean
    assert data_uri in clean
    assert bundle.clean_html_sha256 == hashlib.sha256(clean_path.read_bytes()).hexdigest()
    manifest = json.loads(bundle.manifest_path.read_text(encoding="utf-8"))
    assert manifest["clean_html_sha256"] == bundle.clean_html_sha256
    assert bundle.resources[0].status == "embedded"


def test_edge_command_is_headless_isolated_and_offline(tmp_path: Path) -> None:
    html_path = tmp_path / "clean.html"
    pdf_path = tmp_path / "rendered.pdf"
    profile_dir = tmp_path / "profile"
    profile_dir.mkdir()
    command = build_edge_command(
        edge_path="edge.exe",
        html_path=html_path,
        pdf_path=pdf_path,
        profile_dir=profile_dir,
    )

    assert command[0] == "edge.exe"
    assert "--headless=new" in command
    assert "--disable-javascript" in command
    assert "--disable-extensions" in command
    assert "--disable-background-networking" in command
    assert f"--user-data-dir={profile_dir.resolve()}" in command
    assert "--host-resolver-rules=MAP * ~NOTFOUND" in command
    assert f"--print-to-pdf={pdf_path.resolve()}" in command
    assert command[-1] == html_path.resolve().as_uri()


def test_renderer_uses_mocked_subprocess_without_visible_window(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    edge_path = tmp_path / "msedge.exe"
    edge_path.write_bytes(b"")
    html_path = tmp_path / "clean.html"
    html_path.write_text(
        "<!doctype html><meta http-equiv='Content-Security-Policy' "
        "content=\"default-src 'none'; img-src data: file:; style-src 'unsafe-inline'\">",
        encoding="utf-8",
    )
    pdf_path = tmp_path / "rendered.pdf"
    processes: list[Any] = []

    class FakePopen:
        def __init__(self, command: list[str], **kwargs: Any) -> None:
            self.command = list(command)
            self.kwargs = kwargs
            self.pid = 424242
            self.returncode = 0
            processes.append(self)

        def communicate(self, timeout: float | None = None) -> tuple[bytes, bytes]:
            output = next(
                arg.split("=", 1)[1]
                for arg in self.command
                if arg.startswith("--print-to-pdf=")
            )
            Path(output).write_bytes(b"%PDF-1.4\n% mock edge output\n")
            return b"", b""

        def poll(self) -> int:
            return self.returncode

        def kill(self) -> None:
            self.returncode = -9

    monkeypatch.setattr(html_visual.subprocess, "Popen", FakePopen)
    html_visual._render_html_to_pdf(
        html_path,
        pdf_path,
        _options(tmp_path, edge_path=str(edge_path), render_timeout=5),
    )

    assert pdf_path.read_bytes().startswith(b"%PDF-")
    assert len(processes) == 1
    process = processes[0]
    assert "--headless=new" in process.command
    assert any(arg.startswith("--user-data-dir=") for arg in process.command)
    assert "--host-resolver-rules=MAP * ~NOTFOUND" in process.command
    assert any(arg.startswith("--print-to-pdf=") for arg in process.command)
    assert process.command[-1] == html_path.resolve().as_uri()
    assert process.kwargs["stdin"] == subprocess.DEVNULL
    if hasattr(subprocess, "CREATE_NO_WINDOW"):
        assert process.kwargs["creationflags"] & subprocess.CREATE_NO_WINDOW
    assert "default-src 'none'" in html_path.read_text(encoding="utf-8")
