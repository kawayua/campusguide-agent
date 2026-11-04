from __future__ import annotations

import hashlib
import json
import struct
import zlib
from pathlib import Path

import pytest
from pydantic import ValidationError

import campusguide.ingest.xiaomi_ocr as xiaomi_ocr
from campusguide.ingest.structure import TableStructure
from campusguide.ingest.xiaomi_ocr import (
    LoopbackOpenAIBackend,
    OcrBackendRequest,
    OcrBackendResult,
    OcrPageRequest,
    OcrPageResult,
    XiaomiOcrBackendError,
    XiaomiOcrValidationError,
    run_page_ocr,
)

SOURCE_SHA = "a" * 64
IMAGE_SHA = "b" * 64
POSTPROCESS_REVISION = "official-postprocess-test/1"
MERGED_TABLE = (
    "<table>"
    '<tr><td rowspan="2">A</td><td></td></tr>'
    "<tr><td>C</td></tr>"
    "</table>"
)


def _png_chunk(kind: bytes, data: bytes) -> bytes:
    return (
        struct.pack(">I", len(data))
        + kind
        + data
        + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)
    )


def _png(width: int, height: int) -> bytes:
    scanlines = b"".join(b"\x00" + b"\x00" * width for _ in range(height))
    header = struct.pack(">IIBBBBB", width, height, 8, 0, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + _png_chunk(b"IHDR", header)
        + _png_chunk(b"IDAT", zlib.compress(scanlines))
        + _png_chunk(b"IEND", b"")
    )


def _page(
    tmp_path: Path, *, width: int = 4, height: int = 3, name: str = "page.png"
) -> tuple[Path, str]:
    data = _png(width, height)
    path = tmp_path / name
    path.write_bytes(data)
    return path, hashlib.sha256(data).hexdigest()


def _request(image_path: Path, image_sha: str, **overrides: object) -> OcrPageRequest:
    values: dict[str, object] = {
        "doc_id": "grad-plan",
        "version_id": "v2026",
        "source_format": "pdf",
        "source_sha256": SOURCE_SHA,
        "page": 1,
        "image_path": image_path,
        "image_sha256": image_sha,
        "model": "SeerRay-Lab/Xiaomi-OCR-0",
        "model_revision": "0" * 40,
        "backend": "local-test",
        "postprocess_revision": POSTPROCESS_REVISION,
        "max_tokens": 4096,
    }
    values.update(overrides)
    return OcrPageRequest.model_validate(values)


def _ok(text: str = "hello", **overrides: object) -> OcrBackendResult:
    values: dict[str, object] = {"text": text, "finish_reason": "stop"}
    values.update(overrides)
    return OcrBackendResult.model_validate(values)


def _echo(text: str) -> str:
    return text


class _Backend:
    def __init__(self, outcome: OcrBackendResult | str | BaseException) -> None:
        self.outcome = outcome
        self.calls: list[OcrBackendRequest] = []

    def __call__(self, request: OcrBackendRequest) -> OcrBackendResult | str:
        self.calls.append(request)
        if isinstance(self.outcome, BaseException):
            raise self.outcome
        return self.outcome


def _cache_files(cache_dir: Path) -> list[Path]:
    if not cache_dir.exists():
        return []
    return sorted(cache_dir.glob("*.json"))


def _single_table(result: OcrPageResult):
    assert result.success is True
    assert len(result.tables) == 1
    return result.tables[0]


def _table_ids(table: TableStructure) -> set[str]:
    ids = {table.table_id, table.block_id}
    for row in table.rows:
        ids.add(row.row_id)
        ids.update(cell.cell_id for cell in row.cells)
    return ids


def _replace_table_id_namespace(table: object, namespace: str) -> None:
    assert isinstance(table, dict)
    table["table_id"] = f"{namespace}#table0"
    table["block_id"] = f"{namespace}#table0#block"
    rows = table["rows"]
    assert isinstance(rows, list)
    for row_index, row in enumerate(rows):
        assert isinstance(row, dict)
        row["row_id"] = f"{namespace}#table0#row{row_index}"
        cells = row["cells"]
        assert isinstance(cells, list)
        for col_index, cell in enumerate(cells):
            assert isinstance(cell, dict)
            cell["cell_id"] = f"{namespace}#table0#r{row_index}c{col_index}"


def test_request_rejects_bool_page_and_invalid_hashes(tmp_path: Path) -> None:
    page = tmp_path / "missing.png"
    with pytest.raises(ValidationError):
        _request(page, IMAGE_SHA, page=True)
    with pytest.raises(ValidationError):
        _request(page, IMAGE_SHA, page=0)
    with pytest.raises(ValidationError):
        _request(page, "not-a-sha")
    with pytest.raises(ValidationError):
        _request(page, IMAGE_SHA, source_sha256="c" * 63)
    with pytest.raises(ValidationError):
        _request(page, IMAGE_SHA, postprocess_revision="  ")


def test_run_rejects_image_hash_mismatch_before_backend(tmp_path: Path) -> None:
    page, _ = _page(tmp_path)
    request = _request(page, "0" * 64)
    backend = _Backend(_ok())

    with pytest.raises(XiaomiOcrValidationError, match="哈希不匹配"):
        run_page_ocr(request, backend=backend, postprocess=_echo)

    assert backend.calls == []


def test_run_rejects_non_png(tmp_path: Path) -> None:
    page = tmp_path / "page.png"
    page.write_bytes(b"not a png")
    request = _request(page, hashlib.sha256(page.read_bytes()).hexdigest())

    with pytest.raises(XiaomiOcrValidationError, match="不是 PNG"):
        run_page_ocr(request, backend=_Backend(_ok()), postprocess=_echo)


def test_success_preserves_page_evidence_and_accounting(tmp_path: Path) -> None:
    page, image_sha = _page(tmp_path, width=7, height=5)
    request = _request(page, image_sha)
    backend = _Backend(
        _ok(
            "正文",
            prompt_tokens=12,
            completion_tokens=4,
            total_tokens=16,
        )
    )

    result = run_page_ocr(request, backend=backend, postprocess=_echo)

    assert result.success is True
    assert result.publishable is False
    assert result.visually_verified is False
    assert result.mode == "page"
    assert result.raw_response == "正文"
    assert result.markdown == "正文"
    assert result.finish_reason == "stop"
    assert result.cache == "disabled"
    assert result.tables == []
    assert result.elapsed_seconds >= 0
    assert result.image_path == str(page.resolve())
    assert result.image_width == 7
    assert result.image_height == 5
    assert result.evidence.source_format == "pdf"
    assert result.evidence.source_sha256 == SOURCE_SHA
    assert result.evidence.page == 1
    assert result.evidence.image_sha256 == image_sha
    assert result.evidence.image_width == 7
    assert result.evidence.image_height == 5
    assert result.evidence.bbox_unit == "unknown"
    assert result.evidence.image_path == str(page.resolve())
    assert result.usage.local_requests == 1
    assert result.usage.cloud_requests == 0
    assert result.usage.prompt_tokens == 12
    assert result.usage.completion_tokens == 4
    assert result.usage.total_tokens == 16
    assert len(backend.calls) == 1
    assert backend.calls[0].image_path == page.resolve()
    assert backend.calls[0].page == 1
    assert backend.calls[0].model_revision == "0" * 40
    assert backend.calls[0].prompt == xiaomi_ocr.OFFICIAL_DOCUMENT_PROMPT


def test_unreported_tokens_are_null_not_zero(tmp_path: Path) -> None:
    page, image_sha = _page(tmp_path)
    result = run_page_ocr(
        _request(page, image_sha), backend=_Backend(_ok()), postprocess=_echo
    )

    assert result.success is True
    assert result.usage.local_requests == 1
    assert result.usage.cloud_requests == 0
    assert result.usage.prompt_tokens is None
    assert result.usage.completion_tokens is None
    assert result.usage.total_tokens is None


@pytest.mark.parametrize("finish_reason", ["length", "max_tokens"])
def test_truncated_response_fails_and_is_not_cached(
    tmp_path: Path, finish_reason: str
) -> None:
    page, image_sha = _page(tmp_path)
    cache_dir = tmp_path / "cache"
    result = run_page_ocr(
        _request(page, image_sha),
        backend=_Backend(_ok("partial", finish_reason=finish_reason)),
        postprocess=_echo,
        cache_dir=cache_dir,
    )

    assert result.success is False
    assert result.publishable is False
    assert result.raw_response == "partial"
    assert result.markdown is None
    assert any("ocr_truncated" in error for error in result.errors)
    assert result.usage.local_requests == 1
    assert result.cache == "miss"
    assert _cache_files(cache_dir) == []


def test_unexpected_finish_reason_fails(tmp_path: Path) -> None:
    page, image_sha = _page(tmp_path)
    result = run_page_ocr(
        _request(page, image_sha),
        backend=_Backend(_ok("partial", finish_reason="content_filter")),
        postprocess=_echo,
    )

    assert result.success is False
    assert result.raw_response == "partial"
    assert result.markdown is None
    assert any("ocr_finish_reason_unexpected" in error for error in result.errors)


def test_bare_text_backend_warns_and_has_no_token_claims(tmp_path: Path) -> None:
    page, image_sha = _page(tmp_path)
    result = run_page_ocr(
        _request(page, image_sha), backend=_Backend("plain text"), postprocess=_echo
    )

    assert result.success is True
    assert result.raw_response == "plain text"
    assert result.finish_reason is None
    assert any("backend_returned_bare_text" in warning for warning in result.warnings)
    assert any(
        "backend_finish_reason_unavailable" in warning for warning in result.warnings
    )
    assert result.usage.prompt_tokens is None


def test_backend_error_summary_redacts_credentials(tmp_path: Path) -> None:
    page, image_sha = _page(tmp_path)
    error = RuntimeError(
        "api_key=sk-secret-123 http://user:pass@127.0.0.1:8000/v1/x?token=abc"
    )
    result = run_page_ocr(
        _request(page, image_sha), backend=_Backend(error), postprocess=_echo
    )

    assert result.success is False
    assert result.raw_response is None
    assert result.markdown is None
    assert result.usage.local_requests == 1
    assert result.usage.cloud_requests == 0
    summary = result.errors[0]
    assert summary.startswith("ocr_backend_error:")
    assert "sk-secret-123" not in summary
    assert "user:pass" not in summary
    assert "token=abc" not in summary


def test_postprocess_error_preserves_raw_output(tmp_path: Path) -> None:
    page, image_sha = _page(tmp_path)

    def fail(_: str) -> str:
        raise ValueError("postprocess boom")

    result = run_page_ocr(
        _request(page, image_sha), backend=_Backend(_ok("raw output")), postprocess=fail
    )

    assert result.success is False
    assert result.raw_response == "raw output"
    assert result.markdown is None
    assert any("ocr_postprocess_error" in error for error in result.errors)


def test_postprocess_must_return_text(tmp_path: Path) -> None:
    page, image_sha = _page(tmp_path)
    result = run_page_ocr(
        _request(page, image_sha),
        backend=_Backend(_ok("raw")),
        postprocess=lambda _: b"bytes",  # type: ignore[return-value]
    )

    assert result.success is False
    assert result.raw_response == "raw"
    assert result.markdown is None


def test_empty_model_output_fails_before_postprocess(tmp_path: Path) -> None:
    page, image_sha = _page(tmp_path)
    calls: list[str] = []

    def postprocess(text: str) -> str:
        calls.append(text)
        return text

    result = run_page_ocr(
        _request(page, image_sha),
        backend=_Backend(_ok("   ", finish_reason="stop")),
        postprocess=postprocess,
    )

    assert result.success is False
    assert result.raw_response == "   "
    assert result.markdown is None
    assert calls == []
    assert any("ocr_empty_response" in error for error in result.errors)


@pytest.mark.parametrize(
    "markdown",
    [
        '<table><tr><td rowspan="0">x</td></tr></table>',
        '<table><tr><td rowspan="101">x</td></tr></table>',
        "<table><tr><td>a</td><td>b</td></tr><tr><td>c</td></tr></table>",
        "<table><tr><td>a</td></tr><tr><td>b</td><td>c</td></tr></table>",
        "<table><tr><td>outer<table><tr><td>inner</td></tr></table></td></tr></table>",
        "<table><tr><td>a</td></tr>",
    ],
)
def test_malformed_table_conversion_zeroes_success(
    tmp_path: Path, markdown: str
) -> None:
    page, image_sha = _page(tmp_path)
    result = run_page_ocr(
        _request(page, image_sha), backend=_Backend(_ok(markdown)), postprocess=_echo
    )

    assert result.success is False
    assert result.publishable is False
    assert result.raw_response == markdown
    assert result.markdown == markdown
    assert any("ocr_table_malformed" in error for error in result.errors)


def test_valid_merged_table_preserves_logical_spans_and_empty_leaves(
    tmp_path: Path,
) -> None:
    page, image_sha = _page(tmp_path)
    result = run_page_ocr(
        _request(page, image_sha), backend=_Backend(_ok(MERGED_TABLE)), postprocess=_echo
    )

    table = _single_table(result)
    assert table.table_id == "grad-plan@v2026#ocr-page1#table0"
    assert table.block_id == "grad-plan@v2026#ocr-page1#table0#block"
    assert table.row_count == 2
    assert table.column_count == 2
    assert table.page == 1
    assert table.bbox is None
    assert table.kind == "data"
    assert table.source == "vision"
    assert table.vision is None
    assert table.header_row_indices == []
    assert table.column_headers == []
    assert table.header_paths == []
    assert table.locator.format == "pdf"
    assert table.locator.page == 1
    assert table.locator.bbox is None
    assert any("ocr_table_no_physical_bbox" in warning for warning in table.warnings)
    assert any("ocr_table_header_unknown" in warning for warning in table.warnings)

    origin = table.rows[0].cells[0]
    assert origin.cell_id == "grad-plan@v2026#ocr-page1#table0#r0c0"
    assert origin.covered is False
    assert origin.rowspan == 2
    assert origin.colspan == 1
    assert origin.origin_row == 0
    assert origin.origin_col == 0
    assert origin.text == "A"
    assert origin.vision_text == "A"
    assert origin.text_source == "vision"
    assert origin.bbox is None
    assert origin.dom_path is None

    inherited = table.rows[1].cells[0]
    assert inherited.cell_id == "grad-plan@v2026#ocr-page1#table0#r1c0"
    assert inherited.covered is True
    assert inherited.origin_row == 0
    assert inherited.origin_col == 0
    assert inherited.text == ""
    assert inherited.vision_text is None
    assert inherited.text_source is None
    assert inherited.rowspan == 2

    empty_leaf = table.rows[0].cells[1]
    assert empty_leaf.cell_id == "grad-plan@v2026#ocr-page1#table0#r0c1"
    assert empty_leaf.covered is False
    assert empty_leaf.text == ""
    assert empty_leaf.vision_text == ""
    assert empty_leaf.text_source is None

    assert table.rows[0].text == "A | "
    assert table.rows[1].text == "A | C"
    assert table.rows[0].row_id == "grad-plan@v2026#ocr-page1#table0#row0"
    assert table.rows[1].row_id == "grad-plan@v2026#ocr-page1#table0#row1"
    assert all(not cell.is_header for row in table.rows for cell in row.cells)
    assert all(cell.needs_review is False for row in table.rows for cell in row.cells)
    assert result.publishable is False
    assert result.visually_verified is False


def test_table_ids_are_page_scoped_and_disjoint(tmp_path: Path) -> None:
    page_one, sha_one = _page(tmp_path, name="page1.png")
    page_two, sha_two = _page(tmp_path, width=5, name="page2.png")
    first = _single_table(
        run_page_ocr(
            _request(page_one, sha_one, page=1),
            backend=_Backend(_ok(MERGED_TABLE)),
            postprocess=_echo,
        )
    )
    second = _single_table(
        run_page_ocr(
            _request(page_two, sha_two, page=2),
            backend=_Backend(_ok(MERGED_TABLE)),
            postprocess=_echo,
        )
    )

    assert first.table_index == 0
    assert second.table_index == 0
    assert first.table_id == "grad-plan@v2026#ocr-page1#table0"
    assert second.table_id == "grad-plan@v2026#ocr-page2#table0"
    assert first.block_id != second.block_id
    assert _table_ids(first).isdisjoint(_table_ids(second))
    expected_ids = 2 + len(first.rows) + sum(len(row.cells) for row in first.rows)
    assert len(_table_ids(first)) == expected_ids


def test_table_ids_are_stable_for_repeated_same_page(tmp_path: Path) -> None:
    page, image_sha = _page(tmp_path)
    request = _request(page, image_sha)
    first = _single_table(
        run_page_ocr(request, backend=_Backend(_ok(MERGED_TABLE)), postprocess=_echo)
    )
    second = _single_table(
        run_page_ocr(request, backend=_Backend(_ok(MERGED_TABLE)), postprocess=_echo)
    )

    assert first.table_id == "grad-plan@v2026#ocr-page1#table0"
    assert _table_ids(first) == _table_ids(second)


@pytest.mark.parametrize(
    "namespace",
    ["grad-plan@v2026", "grad-plan@v2026#ocr-page2"],
)
def test_cache_rejects_legacy_or_wrong_page_table_ids(
    tmp_path: Path, namespace: str
) -> None:
    page, image_sha = _page(tmp_path)
    request = _request(page, image_sha)
    cache_dir = tmp_path / "cache"
    run_page_ocr(
        request,
        backend=_Backend(_ok(MERGED_TABLE)),
        postprocess=_echo,
        cache_dir=cache_dir,
    )
    files = _cache_files(cache_dir)
    assert len(files) == 1
    payload = json.loads(files[0].read_text(encoding="utf-8"))
    _replace_table_id_namespace(payload["result"]["tables"][0], namespace)
    files[0].write_text(json.dumps(payload), encoding="utf-8")

    backend = _Backend(_ok(MERGED_TABLE))
    result = run_page_ocr(
        request, backend=backend, postprocess=_echo, cache_dir=cache_dir
    )

    assert len(backend.calls) == 1
    assert result.success is True
    assert result.cache == "miss"
    assert any("cache_entry_rejected" in warning for warning in result.warnings)
    assert _single_table(result).table_id == "grad-plan@v2026#ocr-page1#table0"


@pytest.mark.parametrize("target", ["row_id", "cell_id"])
def test_cache_rejects_wrong_page_row_or_cell_ids(tmp_path: Path, target: str) -> None:
    page, image_sha = _page(tmp_path)
    request = _request(page, image_sha)
    cache_dir = tmp_path / "cache"
    run_page_ocr(
        request,
        backend=_Backend(_ok(MERGED_TABLE)),
        postprocess=_echo,
        cache_dir=cache_dir,
    )
    files = _cache_files(cache_dir)
    assert len(files) == 1
    payload = json.loads(files[0].read_text(encoding="utf-8"))
    table = payload["result"]["tables"][0]
    if target == "row_id":
        table["rows"][0]["row_id"] = "grad-plan@v2026#ocr-page2#table0#row0"
    else:
        table["rows"][0]["cells"][0]["cell_id"] = (
            "grad-plan@v2026#ocr-page2#table0#r0c0"
        )
    files[0].write_text(json.dumps(payload), encoding="utf-8")

    backend = _Backend(_ok(MERGED_TABLE))
    result = run_page_ocr(
        request, backend=backend, postprocess=_echo, cache_dir=cache_dir
    )

    assert len(backend.calls) == 1
    assert result.success is True
    assert result.cache == "miss"
    assert any("cache_entry_rejected" in warning for warning in result.warnings)
    table = _single_table(result)
    assert table.rows[0].row_id == "grad-plan@v2026#ocr-page1#table0#row0"
    assert table.rows[0].cells[0].cell_id == "grad-plan@v2026#ocr-page1#table0#r0c0"


def test_all_empty_cells_keep_empty_row_text(tmp_path: Path) -> None:
    page, image_sha = _page(tmp_path)
    markdown = "<table><tr><td></td><td></td></tr></table>"
    result = run_page_ocr(
        _request(page, image_sha), backend=_Backend(_ok(markdown)), postprocess=_echo
    )

    table = _single_table(result)
    assert table.row_count == 1
    assert table.column_count == 2
    assert table.rows[0].text == ""
    assert [cell.text for cell in table.rows[0].cells] == ["", ""]
    assert all(cell.covered is False for cell in table.rows[0].cells)


def test_th_markup_is_not_treated_as_header_evidence(tmp_path: Path) -> None:
    page, image_sha = _page(tmp_path)
    markdown = "<table><tr><th>H</th><td>V</td></tr></table>"
    result = run_page_ocr(
        _request(page, image_sha), backend=_Backend(_ok(markdown)), postprocess=_echo
    )

    table = _single_table(result)
    assert table.header_row_indices == []
    assert [cell.is_header for cell in table.rows[0].cells] == [False, False]
    assert [cell.text for cell in table.rows[0].cells] == ["H", "V"]


def test_unconverted_otsl_markers_fail_even_when_returned_unchanged(
    tmp_path: Path,
) -> None:
    page, image_sha = _page(tmp_path)
    raw = "<fcel>A<ecel><nl><fcel>B<ecel><nl>"

    result = run_page_ocr(
        _request(page, image_sha),
        backend=_Backend(_ok(raw)),
        postprocess=_echo,
    )

    assert result.success is False
    assert result.publishable is False
    assert result.visually_verified is False
    assert result.raw_response == raw
    assert result.markdown == raw
    assert result.tables == []
    assert any("otsl_unconverted" in error for error in result.errors)


def test_ragged_otsl_cannot_be_repaired_by_postprocess(tmp_path: Path) -> None:
    page, image_sha = _page(tmp_path)
    raw = "<fcel>A<fcel>B<nl><fcel>C<nl>"
    repaired = (
        "<table><tr><td>A</td><td>B</td></tr>"
        "<tr><td>C</td><td></td></tr></table>"
    )

    result = run_page_ocr(
        _request(page, image_sha),
        backend=_Backend(_ok(raw)),
        postprocess=lambda _: repaired,
    )

    assert result.success is False
    assert result.publishable is False
    assert result.visually_verified is False
    assert result.raw_response == raw
    assert result.markdown == repaired
    assert result.tables == []
    assert any("otsl_ragged_rows" in error for error in result.errors)


def test_missing_otsl_block_conversion_fails(tmp_path: Path) -> None:
    page, image_sha = _page(tmp_path)
    raw = "<fcel>A<ecel><nl>\n\n<fcel>B<ecel><nl>"
    converted_one = "<table><tr><td>A</td><td></td></tr></table>"

    result = run_page_ocr(
        _request(page, image_sha),
        backend=_Backend(_ok(raw)),
        postprocess=lambda _: converted_one,
    )

    assert result.success is False
    assert result.raw_response == raw
    assert result.markdown == converted_one
    assert result.tables == []
    assert any("otsl_conversion_missing" in error for error in result.errors)


def test_valid_otsl_conversion_keeps_empty_cells(tmp_path: Path) -> None:
    page, image_sha = _page(tmp_path)
    raw = "<fcel>A<ecel><nl><fcel>C<fcel>D<nl>"
    converted = (
        "<table><tr><td>A</td><td></td></tr>"
        "<tr><td>C</td><td>D</td></tr></table>"
    )

    result = run_page_ocr(
        _request(page, image_sha),
        backend=_Backend(_ok(raw)),
        postprocess=lambda _: converted,
    )

    table = _single_table(result)
    assert result.raw_response == raw
    assert result.markdown == converted
    assert table.row_count == 2
    assert table.column_count == 2
    assert table.rows[0].cells[1].text == ""
    assert table.rows[0].cells[1].vision_text == ""
    assert table.rows[0].text == "A | "
    assert table.rows[1].text == "C | D"


def test_cache_rejects_unconverted_otsl(tmp_path: Path) -> None:
    page, image_sha = _page(tmp_path)
    request = _request(page, image_sha)
    cache_dir = tmp_path / "cache"
    run_page_ocr(
        request, backend=_Backend(_ok("hello")), postprocess=_echo, cache_dir=cache_dir
    )
    files = _cache_files(cache_dir)
    assert len(files) == 1
    payload = json.loads(files[0].read_text(encoding="utf-8"))
    raw = "<fcel>A<ecel><nl>"
    payload["result"]["raw_response"] = raw
    payload["result"]["markdown"] = raw
    files[0].write_text(json.dumps(payload), encoding="utf-8")

    backend = _Backend(_ok("fresh"))
    result = run_page_ocr(
        request, backend=backend, postprocess=_echo, cache_dir=cache_dir
    )

    assert len(backend.calls) == 1
    assert result.success is True
    assert result.cache == "miss"
    assert any("cache_entry_rejected" in warning for warning in result.warnings)


def test_cache_rejects_ragged_otsl(tmp_path: Path) -> None:
    page, image_sha = _page(tmp_path)
    request = _request(page, image_sha)
    cache_dir = tmp_path / "cache"
    run_page_ocr(
        request,
        backend=_Backend(_ok(MERGED_TABLE)),
        postprocess=_echo,
        cache_dir=cache_dir,
    )
    files = _cache_files(cache_dir)
    assert len(files) == 1
    payload = json.loads(files[0].read_text(encoding="utf-8"))
    payload["result"]["raw_response"] = "<fcel>A<fcel>B<nl><fcel>C<nl>"
    files[0].write_text(json.dumps(payload), encoding="utf-8")

    backend = _Backend(_ok(MERGED_TABLE))
    result = run_page_ocr(
        request, backend=backend, postprocess=_echo, cache_dir=cache_dir
    )

    assert len(backend.calls) == 1
    assert result.success is True
    assert result.cache == "miss"
    assert any("cache_entry_rejected" in warning for warning in result.warnings)


def test_cache_hit_skips_backend_and_resets_current_usage(tmp_path: Path) -> None:
    page, image_sha = _page(tmp_path)
    request = _request(page, image_sha)
    cache_dir = tmp_path / "cache"
    first_backend = _Backend(
        _ok("hello", prompt_tokens=8, completion_tokens=2, total_tokens=10)
    )

    first = run_page_ocr(
        request, backend=first_backend, postprocess=_echo, cache_dir=cache_dir
    )
    assert first.success is True
    assert first.cache == "miss"
    assert first.usage.prompt_tokens == 8
    files = _cache_files(cache_dir)
    assert len(files) == 1

    second_backend = _Backend(RuntimeError("cache hit must not call backend"))
    second = run_page_ocr(
        request, backend=second_backend, postprocess=_echo, cache_dir=cache_dir
    )

    assert second.cache == "hit"
    assert second.success is True
    assert second_backend.calls == []
    assert second.raw_response == "hello"
    assert second.markdown == "hello"
    assert second.usage.local_requests == 0
    assert second.usage.cloud_requests == 0
    assert second.usage.prompt_tokens is None
    assert second.usage.completion_tokens is None
    assert second.usage.total_tokens is None
    assert second.image_path == str(page.resolve())


def test_corrupted_cache_json_is_ignored_and_regenerated(tmp_path: Path) -> None:
    page, image_sha = _page(tmp_path)
    request = _request(page, image_sha)
    cache_dir = tmp_path / "cache"
    run_page_ocr(
        request, backend=_Backend(_ok()), postprocess=_echo, cache_dir=cache_dir
    )
    files = _cache_files(cache_dir)
    assert len(files) == 1
    files[0].write_text("{", encoding="utf-8")

    backend = _Backend(_ok("regenerated"))
    result = run_page_ocr(
        request, backend=backend, postprocess=_echo, cache_dir=cache_dir
    )

    assert len(backend.calls) == 1
    assert result.success is True
    assert result.cache == "miss"
    assert result.markdown == "regenerated"
    assert any("cache_entry_rejected" in warning for warning in result.warnings)


def test_cache_binding_source_mismatch_is_rejected(tmp_path: Path) -> None:
    page, image_sha = _page(tmp_path)
    request = _request(page, image_sha)
    cache_dir = tmp_path / "cache"
    run_page_ocr(
        request, backend=_Backend(_ok()), postprocess=_echo, cache_dir=cache_dir
    )
    files = _cache_files(cache_dir)
    payload = json.loads(files[0].read_text(encoding="utf-8"))
    payload["binding"]["source_sha256"] = "c" * 64
    files[0].write_text(json.dumps(payload), encoding="utf-8")

    backend = _Backend(_ok("fresh"))
    result = run_page_ocr(
        request, backend=backend, postprocess=_echo, cache_dir=cache_dir
    )

    assert len(backend.calls) == 1
    assert result.success is True
    assert result.cache == "miss"
    assert any("cache_entry_rejected" in warning for warning in result.warnings)


def test_cache_result_field_mismatch_is_rejected(tmp_path: Path) -> None:
    page, image_sha = _page(tmp_path)
    request = _request(page, image_sha)
    cache_dir = tmp_path / "cache"
    run_page_ocr(
        request, backend=_Backend(_ok()), postprocess=_echo, cache_dir=cache_dir
    )
    files = _cache_files(cache_dir)
    payload = json.loads(files[0].read_text(encoding="utf-8"))
    payload["result"]["source_sha256"] = "c" * 64
    files[0].write_text(json.dumps(payload), encoding="utf-8")

    backend = _Backend(_ok("fresh"))
    result = run_page_ocr(
        request, backend=backend, postprocess=_echo, cache_dir=cache_dir
    )

    assert len(backend.calls) == 1
    assert result.success is True
    assert result.cache == "miss"
    assert any("cache_entry_rejected" in warning for warning in result.warnings)


def test_cache_malformed_table_is_rejected(tmp_path: Path) -> None:
    page, image_sha = _page(tmp_path)
    request = _request(page, image_sha)
    cache_dir = tmp_path / "cache"
    run_page_ocr(
        request,
        backend=_Backend(_ok(MERGED_TABLE)),
        postprocess=_echo,
        cache_dir=cache_dir,
    )
    files = _cache_files(cache_dir)
    payload = json.loads(files[0].read_text(encoding="utf-8"))
    payload["result"]["tables"][0]["rows"][0]["cells"][0]["rowspan"] = 1
    files[0].write_text(json.dumps(payload), encoding="utf-8")

    backend = _Backend(_ok(MERGED_TABLE))
    result = run_page_ocr(
        request, backend=backend, postprocess=_echo, cache_dir=cache_dir
    )

    assert len(backend.calls) == 1
    assert result.success is True
    assert result.cache == "miss"
    assert any("cache_entry_rejected" in warning for warning in result.warnings)


def test_cache_key_changes_with_revision(tmp_path: Path) -> None:
    page, image_sha = _page(tmp_path)
    cache_dir = tmp_path / "cache"
    first = _request(page, image_sha)
    second = _request(
        page, image_sha, postprocess_revision="official-postprocess-test/2"
    )

    backend_one = _Backend(_ok("one"))
    backend_two = _Backend(_ok("two"))
    result_one = run_page_ocr(
        first, backend=backend_one, postprocess=_echo, cache_dir=cache_dir
    )
    result_two = run_page_ocr(
        second, backend=backend_two, postprocess=_echo, cache_dir=cache_dir
    )

    assert result_one.cache == "miss"
    assert result_two.cache == "miss"
    assert len(backend_one.calls) == 1
    assert len(backend_two.calls) == 1
    assert len(_cache_files(cache_dir)) == 2


def test_loopback_backend_accepts_only_loopback_http() -> None:
    for base_url in (
        "http://127.0.0.1:8000/v1",
        "http://127.0.0.1:8000",
        "http://localhost:11434/v1",
        "http://[::1]:8000/v1",
    ):
        LoopbackOpenAIBackend(
            base_url=base_url, timeout_seconds=10.0, max_tokens=4096
        )

    for base_url in (
        "https://127.0.0.1:8000/v1",
        "http://192.168.1.20:8000/v1",
        "http://localhost.evil.example/v1",
        "http://user:pass@127.0.0.1:8000/v1",
        "http://127.0.0.1:8000/v1?token=x",
        "http://127.0.0.1:8000/api",
        "http://127.0.0.1:not-a-port/v1",
    ):
        with pytest.raises(ValueError):
            LoopbackOpenAIBackend(
                base_url=base_url, timeout_seconds=10.0, max_tokens=4096
            )


def test_loopback_backend_refuses_redirects_without_network() -> None:
    handler = xiaomi_ocr._RejectRedirects()
    with pytest.raises(XiaomiOcrBackendError, match="拒绝重定向"):
        handler.redirect_request(  # type: ignore[arg-type]
            None,
            None,
            302,
            "Found",
            None,
            "http://127.0.0.1:8000/v1/chat/completions",
        )


def test_loopback_backend_enforces_explicit_max_tokens_before_request() -> None:
    backend = LoopbackOpenAIBackend(
        base_url="http://127.0.0.1:8000/v1", timeout_seconds=10.0, max_tokens=100
    )
    request = OcrBackendRequest(
        image_path=Path("unused.png"),
        image_sha256="0" * 64,
        page=1,
        prompt="x",
        max_tokens=101,
        model="SeerRay-Lab/Xiaomi-OCR-0",
        model_revision="0" * 40,
        backend="loopback-openai-compatible",
    )

    with pytest.raises(XiaomiOcrBackendError, match="超过后端上限"):
        backend(request)


def test_openai_response_parser_keeps_unavailable_usage_null() -> None:
    parsed = xiaomi_ocr._openai_result(
        {
            "choices": [
                {
                    "message": {"content": [{"type": "text", "text": "hi"}]},
                    "finish_reason": "length",
                }
            ],
            "usage": {"prompt_tokens": 3, "completion_tokens": "bad"},
        }
    )

    assert parsed.text == "hi"
    assert parsed.finish_reason == "length"
    assert parsed.prompt_tokens == 3
    assert parsed.completion_tokens is None
    assert parsed.total_tokens is None
