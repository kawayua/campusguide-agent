"""Offline tests for the HTML DOM → render-PDF native geometry route.

The render PDF parser is stubbed (no real PDF is written), while every page
image is a real Pillow PNG so crop/asset byte proofs stay meaningful.
"""

from __future__ import annotations

import base64
import hashlib
import json
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace

from PIL import Image, ImageDraw

import campusguide.ingest.native_visual as native
from campusguide.ingest import visual
from campusguide.ingest.parse import ParseOutput
from campusguide.ingest.structure import (
    AssetResource,
    PageStructure,
    ParsedStructure,
    SourceLocator,
    TableCell,
    TableRow,
    TableStructure,
)
from campusguide.ingest.visual import (
    RenderedPage,
    VisionConfig,
    VisualPreparser,
    apply_vision_result,
)
from campusguide.ingest.visual_verify import build_native_grid_context

HTML_TABLE_ID = "doc@ver#htmltable0"
PAGE_W = 600
PAGE_H = 800
HEADER = ("科目", "金额")


class HtmlPageRenderer:
    version = "html-native-renderer/1"

    def __init__(self, pages: int, metadata: dict) -> None:
        self.page_count = pages
        self.last_render_metadata = dict(metadata)

    def render(
        self, source_path: Path, *, source_format: str, dpi: int
    ) -> list[RenderedPage]:
        rendered: list[RenderedPage] = []
        for index in range(self.page_count):
            image = Image.new("RGB", (PAGE_W, PAGE_H), "white")
            draw = ImageDraw.Draw(image)
            draw.rectangle(
                [20 + index * 20, 20, 200 + index * 20, 160],
                fill="#1f77b4",
            )
            buffer = BytesIO()
            image.save(buffer, format="PNG")
            png = buffer.getvalue()
            rendered.append(
                RenderedPage(
                    page_no=index + 1,
                    image_sha256=hashlib.sha256(png).hexdigest(),
                    width=PAGE_W,
                    height=PAGE_H,
                    dpi=dpi,
                    png_base64=base64.b64encode(png).decode("ascii"),
                )
            )
        return rendered


class HtmlNativeProvider:
    version = "html-native-provider/1"
    model = "html-native-model"

    def __init__(
        self,
        payloads: dict[int, dict],
        *,
        cell_payloads: dict[tuple[int, int, int, int], dict] | None = None,
        asset_payload: dict | None = None,
    ) -> None:
        self.payloads = payloads
        self.cell_payloads = dict(cell_payloads or {})
        self.asset_payload = asset_payload
        self.verify_calls: list[dict] = []
        self.cell_calls: list[dict] = []
        self.transcribe_calls: list[dict] = []
        self.asset_calls: list[dict] = []
        self.last_native_audit: dict = {}
        self.last_asset_audit: dict = {}

    def verify_native_grid(
        self, image_png: bytes, *, page_no: int, context: dict
    ) -> dict:
        self.verify_calls.append(
            {
                "page_no": page_no,
                "image_sha256": hashlib.sha256(image_png).hexdigest(),
                "context": dict(context),
            }
        )
        payload = json.loads(json.dumps(self.payloads[page_no]))
        self.last_native_audit = {
            "stage": "native_grid_verify",
            "response_sha256": _payload_sha(payload),
            "model": self.model,
        }
        return payload

    def recognize_cell(
        self, image_png: bytes, *, page_no: int, context: dict
    ) -> dict:
        bbox = tuple(int(value) for value in context["crop_bbox"])
        self.cell_calls.append(
            {
                "page_no": page_no,
                "image_sha256": hashlib.sha256(image_png).hexdigest(),
                "bbox": bbox,
            }
        )
        assert bbox in self.cell_payloads, f"unexpected crop {bbox}"
        return json.loads(json.dumps(self.cell_payloads[bbox]))

    def recognize_page(
        self, image_png: bytes, *, page_no: int, context: dict
    ) -> dict:
        self.transcribe_calls.append({"page_no": page_no, "context": dict(context)})
        return {
            "page": page_no,
            "blocks": [{"id": "legacy", "kind": "paragraph", "text": "legacy"}],
        }

    def recognize_asset(
        self,
        image_bytes: bytes,
        *,
        asset_id: str,
        mime_type: str,
        context: dict,
    ) -> dict:
        self.asset_calls.append(
            {
                "asset_id": asset_id,
                "mime_type": mime_type,
                "image_sha256": hashlib.sha256(image_bytes).hexdigest(),
                "context": dict(context),
            }
        )
        payload = json.loads(json.dumps(self.asset_payload or {}))
        self.last_asset_audit = {
            "response_sha256": hashlib.sha256(
                json.dumps(payload, ensure_ascii=False).encode("utf-8")
            ).hexdigest(),
            "model": self.model,
            "prompt_sha256": "c" * 64,
            "mime_type": mime_type,
        }
        return payload


def _payload_sha(payload: dict) -> str:
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()


def _config(tmp_path: Path, **overrides) -> VisionConfig:
    values = {
        "cache_dir": tmp_path / "cache",
        "dpi": 144,
        "max_requests": 20,
        "max_retries": 1,
        "retry_backoff_seconds": 0.0,
        "max_tables_per_page": 8,
    }
    values.update(overrides)
    return VisionConfig(**values)


def _html_cell(row: int, col: int, value: str, *, header: bool) -> TableCell:
    return TableCell(
        cell_id=f"{HTML_TABLE_ID}#r{row}c{col}",
        row=row,
        col=col,
        origin_row=row,
        origin_col=col,
        text=value,
        text_source="native" if value else None,
        is_header=header,
    )


def _html_table() -> TableStructure:
    values = [HEADER, ("A", "1"), ("B", "2")]
    rows = [
        TableRow(
            row_id=f"{HTML_TABLE_ID}#row{index}",
            row_index=index,
            is_header=index == 0,
            cells=[
                _html_cell(index, col, value, header=index == 0)
                for col, value in enumerate(row_values)
            ],
            text=" | ".join(row_values),
            locator=SourceLocator(
                format="html", dom_path=f"html:table:row{index}"
            ),
        )
        for index, row_values in enumerate(values)
    ]
    return TableStructure(
        table_id=HTML_TABLE_ID,
        block_id=f"{HTML_TABLE_ID}#block",
        table_index=0,
        page=None,
        bbox=None,
        rows=rows,
        header_row_indices=[0],
        column_count=2,
        row_count=3,
        column_headers=list(HEADER),
        header_paths=[[value] for value in HEADER],
        source="html_dom",
        locator=SourceLocator(format="html", dom_path="html:table"),
    )


def _render_table(
    page: int,
    table_id: str,
    data_rows: tuple[tuple[str, str], ...],
) -> TableStructure:
    values = [HEADER, *data_rows]
    rows: list[TableRow] = []
    for index, row_values in enumerate(values):
        y0 = 20.0 + index * 60.0
        cells = [
            TableCell(
                cell_id=f"{table_id}#r{index}c{col}",
                row=index,
                col=col,
                origin_row=index,
                origin_col=col,
                text=value,
                text_source="native",
                is_header=index == 0,
                bbox=[
                    20.0 + col * 280.0,
                    y0,
                    300.0 + col * 280.0,
                    y0 + 40.0,
                ],
            )
            for col, value in enumerate(row_values)
        ]
        rows.append(
            TableRow(
                row_id=f"{table_id}#row{index}",
                row_index=index,
                is_header=index == 0,
                cells=cells,
                text=" | ".join(row_values),
                locator=SourceLocator(
                    format="pdf", page=page, bbox=[20.0, y0, 580.0, y0 + 40.0]
                ),
            )
        )
    return TableStructure(
        table_id=table_id,
        block_id=f"{table_id}#block",
        table_index=0,
        page=page,
        bbox=[20.0, 20.0, 580.0, 20.0 + 60.0 * len(values)],
        rows=rows,
        header_row_indices=[0],
        column_count=2,
        row_count=len(rows),
        column_headers=list(HEADER),
        header_paths=[[value] for value in HEADER],
        source="pymupdf",
        locator=SourceLocator(
            format="pdf", page=page, bbox=[20.0, 20.0, 580.0, 20.0 + 60.0 * len(values)]
        ),
    )


def _render_output(
    page_count: int, render_tables: dict[int, list[TableStructure]]
) -> ParseOutput:
    structure = ParsedStructure(
        format="pdf",
        pages=[
            PageStructure(
                page=page,
                width=float(PAGE_W),
                height=float(PAGE_H),
                text_chars=20,
                has_text_layer=True,
            )
            for page in range(1, page_count + 1)
        ],
        tables=[
            table
            for page in sorted(render_tables)
            for table in render_tables[page]
        ],
    )
    return ParseOutput(
        format="pdf",
        structure=structure,
        text="",
        title="",
        parser_version="test",
    )


def _html_parsed(
    table: TableStructure,
    *,
    page_count: int = 1,
    assets: tuple[AssetResource, ...] = (),
) -> ParseOutput:
    structure = ParsedStructure(
        format="html",
        pages=[
            PageStructure(
                page=page,
                width=float(PAGE_W),
                height=float(PAGE_H),
                text_chars=20,
                has_text_layer=True,
            )
            for page in range(1, page_count + 1)
        ],
        tables=[table],
        assets=list(assets),
    )
    return ParseOutput(
        format="html",
        structure=structure,
        text="学院简介",
        title="学院简介",
        parser_version="test",
    )


def _verify_payload(
    table_id: str,
    checked_rows: tuple[int, ...],
    *,
    page: int,
    verified: bool = True,
    missing_rows: bool = False,
) -> dict:
    return {
        "page": page,
        "table_checks": [
            {
                "table_id": table_id,
                "checked_rows": list(checked_rows),
                "verified": verified,
                "missing_rows": missing_rows,
                "missing_columns": False,
            }
        ],
        "additional_content": [],
        "warnings": [],
    }


def _forbidden_pdf_audit(*args, **kwargs):
    raise AssertionError("HTML 不得走 PDF 原件审计")


def _prepare_html(
    tmp_path: Path,
    monkeypatch,
    provider,
    *,
    parsed: ParseOutput,
    page_count: int,
    render_tables: dict[int, list[TableStructure]],
    resources: tuple[dict, ...] = (),
    config: VisionConfig | None = None,
):
    source = tmp_path / "source.html"
    source.write_text("<html><body>学院简介</body></html>", encoding="utf-8")
    sha = hashlib.sha256(source.read_bytes()).hexdigest()
    render_pdf = tmp_path / "render.pdf"
    render_pdf.write_bytes(b"%PDF-1.4\nhtml render geometry fixture\n")
    rendered = _render_output(page_count, render_tables)
    import campusguide.ingest.parse as parse_module

    monkeypatch.setattr(
        parse_module,
        "parse_pdf_file",
        lambda path, *, doc_id, version_id: rendered,
    )
    monkeypatch.setattr(visual, "audit_pdf_grid_cells", _forbidden_pdf_audit)
    metadata = {
        "pdf_path": str(render_pdf),
        "resources": [dict(record) for record in resources],
        "warnings": [],
    }
    renderer = HtmlPageRenderer(page_count, metadata)
    preparser = VisualPreparser(
        config or _config(tmp_path), renderer=renderer, provider=provider
    )
    result = preparser.prepare(
        source_path=source,
        doc_id="doc",
        version_id="ver",
        source_sha256=sha,
        source_format="html",
        parsed=parsed,
    )
    return preparser, result


def test_html_single_page_geometry_uses_dom_ids_and_cache(tmp_path, monkeypatch):
    table = _html_table()
    provider = HtmlNativeProvider(
        {1: _verify_payload(HTML_TABLE_ID, (0, 1, 2), page=1)}
    )
    preparser, result = _prepare_html(
        tmp_path,
        monkeypatch,
        provider,
        parsed=_html_parsed(table),
        page_count=1,
        render_tables={1: [_render_table(1, "render@t1", (("A", "1"), ("B", "2")))]},
    )

    assert preparser is not None
    assert result.ok is True
    assert result.publishable is True
    assert result.mode == "native_visual_verified"
    assert provider.transcribe_calls == []
    assert provider.cell_calls == []

    context = provider.verify_calls[0]["context"]["native_context"]
    assert context["source_format"] == "html"
    assert context["native_page_size"] == [float(PAGE_W), float(PAGE_H)]
    entry = context["tables"][0]
    assert entry["table_id"] == HTML_TABLE_ID
    assert entry["row_indices"] == [0, 1, 2]
    assert entry["header_row_indices"] == [0]
    assert entry["header_paths"] == [["科目"], ["金额"]]
    compact = native.model_native_context(context)
    assert compact["source_format"] == "html"
    assert compact["tables"][0]["rows"][1]["cells"] == [[0, "A"], [1, "1"]]
    assert result.page_evidence[1]["native_source_format"] == "html"
    assert result.page_evidence[1]["native_source_audit_sha256"] == ""
    assert result.page_evidence[1]["native_html_geometry_sha256"]
    assert result.native_pages[1]["source_audit"] is None
    assert result.native_pages[1]["tables"][HTML_TABLE_ID]["row_indices"] == [
        0,
        1,
        2,
    ]

    cache_files = list((tmp_path / "cache").rglob("native_grid_verify-*.json"))
    assert len(cache_files) == 1
    record = json.loads(cache_files[0].read_text(encoding="utf-8"))
    assert record["source_format"] == "html"
    assert record["source_audit_sha256"] == ""
    assert record["source_audit"] is None
    assert record["html_geometry_version"] == native.NATIVE_HTML_GEOMETRY_VERSION
    assert record["html_geometry_sha256"] == (
        result.page_evidence[1]["native_html_geometry_sha256"]
    )

    second_provider = HtmlNativeProvider(
        {1: _verify_payload(HTML_TABLE_ID, (0, 1, 2), page=1)}
    )
    _, second = _prepare_html(
        tmp_path,
        monkeypatch,
        second_provider,
        parsed=_html_parsed(table),
        page_count=1,
        render_tables={1: [_render_table(1, "render@t1", (("A", "1"), ("B", "2")))]},
    )
    assert second_provider.verify_calls == []
    assert second.cache_hits == 1
    assert second.publishable is True

    applied = _html_parsed(_html_table())
    apply_vision_result(applied, result)
    assert applied.structure.tables[0].page is None
    assert applied.structure.tables[0].rows[1].cells[0].text == "A"
    assert applied.structure.tables[0].rows[2].cells[1].text == "2"


def test_html_two_page_row_union_publishes_only_with_full_union(tmp_path, monkeypatch):
    table = _html_table()
    provider = HtmlNativeProvider(
        {
            1: _verify_payload(HTML_TABLE_ID, (0, 1), page=1),
            2: _verify_payload(HTML_TABLE_ID, (0, 2), page=2),
        }
    )
    preparser, result = _prepare_html(
        tmp_path,
        monkeypatch,
        provider,
        parsed=_html_parsed(table, page_count=2),
        page_count=2,
        render_tables={
            1: [_render_table(1, "render@t1", (("A", "1"),))],
            2: [_render_table(2, "render@t2", (("B", "2"),))],
        },
    )

    assert preparser is not None
    assert result.ok is True
    assert result.publishable is True
    assert provider.transcribe_calls == []
    assert provider.cell_calls == []
    assert len(provider.verify_calls) == 2

    first = provider.verify_calls[0]["context"]["native_context"]
    second = provider.verify_calls[1]["context"]["native_context"]
    assert first["source_format"] == second["source_format"] == "html"
    assert first["tables"][0]["row_indices"] == [0, 1]
    assert second["tables"][0]["row_indices"] == [0, 2]
    assert first["tables"][0]["header_row_indices"] == [0]
    assert second["tables"][0]["header_row_indices"] == [0]
    assert result.native_pages[1]["tables"][HTML_TABLE_ID]["checked_rows"] == [0, 1]
    assert result.native_pages[2]["tables"][HTML_TABLE_ID]["checked_rows"] == [0, 2]

    union = {
        row
        for audit in result.native_pages.values()
        for row in audit["tables"][HTML_TABLE_ID]["checked_rows"]
    }
    assert union == {0, 1, 2}

    second_provider = HtmlNativeProvider(
        {
            1: _verify_payload(HTML_TABLE_ID, (0, 1), page=1),
            2: _verify_payload(HTML_TABLE_ID, (0, 2), page=2),
        }
    )
    _, cached = _prepare_html(
        tmp_path,
        monkeypatch,
        second_provider,
        parsed=_html_parsed(table, page_count=2),
        page_count=2,
        render_tables={
            1: [_render_table(1, "render@t1", (("A", "1"),))],
            2: [_render_table(2, "render@t2", (("B", "2"),))],
        },
    )
    assert second_provider.verify_calls == []
    assert cached.cache_hits == 2
    assert cached.publishable is True


def test_html_missing_row_fails_closed_without_legacy_transcription(
    tmp_path, monkeypatch
):
    table = _html_table()
    provider = HtmlNativeProvider(
        {
            1: _verify_payload(
                HTML_TABLE_ID,
                (0, 1),
                page=1,
                verified=False,
                missing_rows=True,
            )
        }
    )
    _, result = _prepare_html(
        tmp_path,
        monkeypatch,
        provider,
        parsed=_html_parsed(table),
        page_count=1,
        render_tables={1: [_render_table(1, "render@t1", (("A", "1"), ("B", "2")))]},
    )

    assert provider.transcribe_calls == []
    assert result.pages[1].tables == []
    assert result.ok is True
    assert result.publishable is False
    assert any(
        item["kind"] == "native_grid_coverage_incomplete"
        for item in result.unresolved_conflicts
    )

    applied = _html_parsed(_html_table())
    apply_vision_result(applied, result)
    assert applied.structure.tables[0].rows[2].cells[0].text == "B"


def test_html_wrong_table_id_fails_closed_without_legacy_transcription(
    tmp_path, monkeypatch
):
    table = _html_table()
    provider = HtmlNativeProvider(
        {1: _verify_payload("wrong#table", (0, 1, 2), page=1)}
    )
    _, result = _prepare_html(
        tmp_path,
        monkeypatch,
        provider,
        parsed=_html_parsed(table),
        page_count=1,
        render_tables={1: [_render_table(1, "render@t1", (("A", "1"), ("B", "2")))]},
    )

    assert provider.transcribe_calls == []
    assert result.pages == {}
    assert result.publishable is False
    assert any(
        failure["stage"] == "native_grid_verify"
        and failure["code"] == "native_grid_parse"
        for failure in result.failures
    )
    assert any(
        item["kind"] == "html_native_page_missing"
        for item in result.unresolved_conflicts
    )


def test_html_page_slice_drops_header_rows_outside_this_page():
    table = _html_table()
    page_two = table.model_copy(
        deep=True,
        update={
            "page": 2,
            "rows": [table.rows[2].model_copy(deep=True)],
            "row_count": 1,
        },
    )
    geometry = SimpleNamespace(page_tables={2: [page_two]})

    sliced = visual._html_page_table_slice(geometry, 2)

    assert [row.row_index for row in sliced[0].rows] == [2]
    assert sliced[0].header_row_indices == []
    assert sliced[0].header_paths == [["科目"], ["金额"]]
    # 原始 DOM 表的 header 索引不被切片改写。
    assert page_two.header_row_indices == [0]

    context = build_native_grid_context(
        sliced, page_no=2, source_format="html", page_size=(600.0, 800.0)
    )
    entry = context["tables"][0]
    assert entry["row_indices"] == [2]
    assert entry["header_row_indices"] == []
    assert entry["header_paths"] == [["科目"], ["金额"]]


def test_html_assets_still_require_independent_recognition_and_cache(
    tmp_path, monkeypatch
):
    table = _html_table()
    asset = AssetResource(
        asset_id="asset-1",
        kind="image",
        src="https://www.scau.edu.cn/demo.png",
        locator=SourceLocator(format="html", dom_path="html:img:1"),
        factual=True,
    )
    resource = _html_resource(tmp_path)
    payload = {"has_text": True, "blocks": [{"kind": "formula", "text": "E=mc^2"}]}
    provider = HtmlNativeProvider(
        {1: _verify_payload(HTML_TABLE_ID, (0, 1, 2), page=1)},
        asset_payload=payload,
    )
    _, result = _prepare_html(
        tmp_path,
        monkeypatch,
        provider,
        parsed=_html_parsed(table, assets=(asset,)),
        page_count=1,
        render_tables={1: [_render_table(1, "render@t1", (("A", "1"), ("B", "2")))]},
        resources=(resource,),
    )

    assert result.publishable is True
    assert provider.cell_calls == []
    assert [call["asset_id"] for call in provider.asset_calls] == ["asset-1"]
    assert provider.asset_calls[0]["image_sha256"] == resource["sha256"]
    assert result.asset_recognitions[0]["status"] == "recognized"
    assert result.asset_recognitions[0]["image_sha256"] == resource["sha256"]
    assert result.images_sent == 2
    asset_cache = list((tmp_path / "cache").rglob("assets/*.json"))
    assert len(asset_cache) == 1

    applied = _html_parsed(
        _html_table(),
        assets=(
            AssetResource(
                asset_id="asset-1",
                kind="image",
                src="https://www.scau.edu.cn/demo.png",
                locator=SourceLocator(format="html", dom_path="html:img:1"),
                factual=True,
            ),
        ),
    )
    apply_vision_result(applied, result)
    assert any(
        block.text == "E=mc^2" for block in applied.structure.blocks
    )
    assert applied.structure.assets[0].recognized is True

    second_asset = AssetResource(
        asset_id="asset-1",
        kind="image",
        src="https://www.scau.edu.cn/demo.png",
        locator=SourceLocator(format="html", dom_path="html:img:1"),
        factual=True,
    )
    second_provider = HtmlNativeProvider(
        {1: _verify_payload(HTML_TABLE_ID, (0, 1, 2), page=1)},
        asset_payload=payload,
    )
    _, cached = _prepare_html(
        tmp_path,
        monkeypatch,
        second_provider,
        parsed=_html_parsed(table, assets=(second_asset,)),
        page_count=1,
        render_tables={1: [_render_table(1, "render@t1", (("A", "1"), ("B", "2")))]},
        resources=(resource,),
    )
    assert second_provider.asset_calls == []
    assert cached.asset_cache_hits == 1
    assert cached.asset_recognitions[0]["cache_hit"] is True
    assert cached.publishable is True


def _png_bytes(seed: str = "html-asset") -> bytes:
    digest = hashlib.sha256(seed.encode("utf-8")).digest()
    color = (digest[0], digest[1], digest[2])
    image = Image.new("RGB", (24, 24), "white")
    image.paste(color, (0, 0, 8, 8))
    buffer = BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def _html_resource(tmp_path: Path) -> dict:
    data = _png_bytes()
    path = tmp_path / "resource-asset-1.png"
    path.write_bytes(data)
    digest = hashlib.sha256(data).hexdigest()
    return {
        "asset_id": "asset-1",
        "dom_path": "html:img:1",
        "ref": "src",
        "original_ref": "https://www.scau.edu.cn/demo.png",
        "url": "https://www.scau.edu.cn/demo.png",
        "final_url": "https://www.scau.edu.cn/demo.png",
        "status": "embedded",
        "http_status": 200,
        "bytes": len(data),
        "sha256": digest,
        "content_type": "image/png",
        "image_format": "png",
        "local_path": str(path),
        "cache_path": str(path),
        "source_manifest": None,
        "attempts": 1,
        "cooldown_seconds": 0.0,
        "error": None,
    }
