from __future__ import annotations

import hashlib
import importlib.util
import json
import struct
import zlib
from pathlib import Path
from unittest.mock import Mock

import pytest

from campusguide.ingest.ocr_layout import LayoutDetection, build_layout
from campusguide.ingest.ocr_native_expectation import (
    NativeExpectationAudit,
    NativeExpectationValidationError,
)
from campusguide.ingest.ocr_router import RegionExpectation
from campusguide.ingest.parse import ParsedDocument
from campusguide.ingest.xiaomi_ocr import OcrBackendResult

TABLE_HTML = (
    "<table><tr><td>Key</td><td>Value</td></tr>"
    "<tr><td>A</td><td>1</td></tr></table>"
)

_spec = importlib.util.spec_from_file_location(
    "ocr_router_cli", Path(__file__).resolve().parents[2] / "scripts" / "run_ocr_router.py"
)
assert _spec is not None and _spec.loader is not None
CLI = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(CLI)


def _json(path):
    return json.loads(path.read_text(encoding="utf-8"))


def _png(width=30, height=20):
    def chunk(kind, data):
        crc = struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)
        return struct.pack(">I", len(data)) + kind + data + crc

    raw = (b"\x00" + bytes(width * 3)) * height
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(
        b"IDAT", zlib.compress(raw)
    ) + chunk(b"IEND", b"")


def _install(root):
    model = root / "models" / "PP-DocLayoutV3"
    model.mkdir(parents=True)
    files = []
    for name in CLI.LAYOUT_REQUIRED:
        data = f"layout:{name}".encode()
        (model / name).write_bytes(data)
        digest = hashlib.sha256(data).hexdigest()
        files.append({"filename": name, "size": len(data), "sha256": digest})
    manifest = {"revision": "fake-layout-revision", "files": files}
    (root / "layout_installation_manifest.json").write_text(
        json.dumps(manifest), encoding="utf-8"
    )
    (root / "installation_manifest.json").write_text("{}\n", encoding="utf-8")


def _layout(evidence, labels, needs_review):
    detections = [
        LayoutDetection(label=label, score=0.9, order=index,
                        bbox_px=[2.0 + index * 2.0, 2.0, 12.0 + index * 2.0, 9.0])
        for index, label in enumerate(labels)
    ]
    layout = build_layout(
        evidence, detections, detector_id="fake-detector", detector_revision="fake-revision"
    )
    if needs_review is not None and layout.needs_review is not needs_review:
        layout = layout.model_copy(update={"needs_review": needs_review})
    return layout


def _patch(monkeypatch, *, labels=("text",), needs_review=None, local_text="hello world"):
    backends, detectors = [], []

    class FakeLocal:
        backend_identity = "fake-local/xiaomi"
        model = "fake-model"
        model_revision = "fake-model-revision"
        postprocess_revision = "fake-postprocess/1"

        def __init__(self, *args):
            self.calls = 0
            self.last_call_audit = {}
            backends.append(self)

        def __call__(self, request):
            self.calls += 1
            return OcrBackendResult(text=local_text, finish_reason="stop")

        def postprocess(self, text):
            return text

    class FakeDetector:
        def __init__(self, **kwargs):
            self.calls = 0
            detectors.append(self)

        def detect(self, evidence):
            self.calls += 1
            return _layout(evidence, labels, needs_review)

    monkeypatch.setattr("campusguide.ingest.ocr_runtime.TransformersXiaomiBackend", FakeLocal)
    monkeypatch.setattr("campusguide.ingest.ocr_layout.PPDocLayoutV3Detector", FakeDetector)
    return backends, detectors


def _write_parsed(path, source_sha, **overrides):
    payload = {
        "doc_id": "doc-1",
        "version_id": "v1",
        "title": "native",
        "text": "",
        "format": "pdf",
        "source_sha256": source_sha,
        "parser_version": "test",
        "page_count": 1,
        **overrides,
    }
    document = ParsedDocument(**payload)
    path.write_text(document.model_dump_json(), encoding="utf-8")


def _setup(
    tmp_path,
    monkeypatch,
    *,
    labels=("text",),
    needs_review=None,
    local_text="hello world",
    native_paths=False,
):
    install = tmp_path / "install"
    _install(install)
    image = tmp_path / "page.png"
    image.write_bytes(_png())
    source_sha = "a" * 64
    row = {"sample_id": "sample-1", "doc_id": "doc-1", "version_id": "v1",
           "source_format": "pdf", "source_sha256": source_sha, "page_no": 1,
           "image_path": str(image),
           "image_sha256": hashlib.sha256(image.read_bytes()).hexdigest()}
    if native_paths:
        source = tmp_path / "source.pdf"
        source.write_bytes(b"%PDF-1.4\n%native crosscheck fixture\n")
        source_sha = hashlib.sha256(source.read_bytes()).hexdigest()
        row["source_sha256"] = source_sha
        row["source_path"] = source.name
        _write_parsed(tmp_path / "parsed.json", source_sha)
        row["native_parsed_path"] = "parsed.json"
    rows = tmp_path / "rows.jsonl"
    rows.write_text(json.dumps(row) + "\n", encoding="utf-8")
    backends, detectors = _patch(
        monkeypatch,
        labels=labels,
        needs_review=needs_review,
        local_text=local_text,
    )
    args = ["--input", str(rows), "--out", str(tmp_path / "out")]
    args += ["--install-root", str(install), "--device", "cpu"]
    return args, backends, detectors


def _calls(backends, detectors):
    return sum(item.calls for item in backends), sum(item.calls for item in detectors)


def _fake_native(monkeypatch, *, status="ready", error=None):
    calls = []

    def fake_build(crop, *, source_path, parsed):
        calls.append((crop, source_path, parsed))
        if error is not None:
            raise error
        expectation = None
        reasons = []
        if status == "ready":
            expectation = RegionExpectation(
                source_sha256=crop.page.source_sha256,
                page_image_sha256=crop.page.image_sha256,
                page=crop.page.page,
                region_id=crop.region.region_id,
                crop_sha256=crop.image_sha256,
                expected_table_column_count=2,
                numeric_checks=[
                    {"row_key": "A", "key_col": 0, "col": 1, "value": "9"}
                ],
            )
        else:
            reasons = ["ambiguous_native_table_match"]
        return NativeExpectationAudit(
            status=status,
            expectation=expectation,
            needs_review=status == "needs_review",
            reasons=reasons,
            audit={
                "diagnostic_only": True,
                "not_truth": True,
                "origin": "fake-native",
            },
        )

    monkeypatch.setattr(
        "campusguide.ingest.ocr_native_expectation.build_native_expectation",
        fake_build,
    )
    return calls


def test_verify_layout_install_rejects_tampered_bytes_and_missing_model(tmp_path):
    root = tmp_path / "install"
    _install(root)
    model = root / "models" / "PP-DocLayoutV3"
    manifest = _json(root / "layout_installation_manifest.json")
    CLI.verify_layout_install(manifest, model)

    config = model / "config.json"
    original = config.read_bytes()
    config.write_bytes(b"x" * len(original))
    with pytest.raises(ValueError, match="sha256 mismatch"):
        CLI.verify_layout_install(manifest, model)

    config.write_bytes(original)
    (model / "preprocessor_config.json").unlink()
    with pytest.raises(ValueError, match="layout model file missing"):
        CLI.verify_layout_install(manifest, model)

    manifest["files"] = [
        entry for entry in manifest["files"]
        if entry["filename"] != "preprocessor_config.json"
    ]
    with pytest.raises(ValueError, match="missing required files"):
        CLI.verify_layout_install(manifest, model)


def test_resume_reruns_layout_and_uses_verified_cache(tmp_path, monkeypatch):
    args, backends, detectors = _setup(tmp_path, monkeypatch)
    assert CLI.main(args) == 0
    assert _calls(backends, detectors) == (1, 1)
    assert _json(tmp_path / "out" / "summary.json")["current_local_requests"] == 1

    page_path = next((tmp_path / "out" / "pages").glob("*/page.json"))
    page = _json(page_path)
    page["counts"].update(local_requests=999, cache_hits=999, routed_count=0)
    page_path.write_text(json.dumps(page), encoding="utf-8")

    assert CLI.main([*args, "--resume"]) == 0
    assert _calls(backends, detectors) == (1, 2)
    page = _json(page_path)
    counts = page["counts"]
    assert (counts["local_requests"], counts["cache_hits"]) == (0, 1)
    assert (counts["routed_count"], counts["pending_count"]) == (1, 0)
    assert page["status"] == "ok"
    summary = _json(tmp_path / "out" / "summary.json")
    assert (summary["current_local_requests"], summary["cache_hits"]) == (0, 1)
    assert summary["routed_count"] == 1


def test_resume_rejects_changed_settings_before_inference(tmp_path, monkeypatch, capsys):
    args, backends, detectors = _setup(tmp_path, monkeypatch)
    assert CLI.main(args) == 0
    capsys.readouterr()

    for extra in (["--max-cloud-requests", "11"], ["--layout-device", "cuda:0"]):
        assert CLI.main([*args, "--resume", *extra]) == 2
        assert "existing output settings differ" in capsys.readouterr().err
        assert _calls(backends, detectors) == (1, 1)


@pytest.mark.parametrize(
    ("labels", "needs_review", "local_requests", "ignored"),
    [(("text",), True, 1, 0), (("header",), None, 0, 1)],
    ids=["layout-needs-review", "header-only"],
)
def test_needs_review_and_header_only_pages_stay_pending(
    tmp_path, monkeypatch, labels, needs_review, local_requests, ignored
):
    args, backends, _ = _setup(tmp_path, monkeypatch, labels=labels, needs_review=needs_review)
    assert CLI.main(args) == 1
    page_path = next((tmp_path / "out" / "pages").glob("*/page.json"))
    page = _json(page_path)
    counts = page["counts"]
    assert page["layout_needs_review"] is True
    assert page["status"] == "pending"
    assert (counts["region_count"], counts["ignored_count"]) == (1, ignored)
    assert (counts["local_requests"], counts["pending_count"]) == (local_requests, 1)
    assert counts["routed_count"] == 0
    assert sum(item.calls for item in backends) == local_requests
    summary = _json(tmp_path / "out" / "summary.json")
    assert (summary["status"], summary["pending_count"]) == ("pending", 1)


def test_dotenv_is_not_read_without_allow_gemini(tmp_path, monkeypatch):
    args, backends, detectors = _setup(tmp_path, monkeypatch)
    dotenv = tmp_path / "fake.env"
    dotenv.write_text("SENTINEL=1\n", encoding="utf-8")
    spy = Mock(return_value={})
    monkeypatch.setattr("campusguide.env.load_dotenv", spy)
    assert CLI.main([*args, "--dotenv", str(dotenv)]) == 0
    spy.assert_not_called()
    assert _calls(backends, detectors) == (1, 1)
    assert _json(tmp_path / "out" / "summary.json")["status"] == "ok"


def test_native_crosscheck_passes_expectation_and_records_audit(tmp_path, monkeypatch):
    root = tmp_path / "native"
    args, backends, detectors = _setup(
        root,
        monkeypatch,
        labels=("table",),
        native_paths=True,
        local_text=TABLE_HTML,
    )
    calls = _fake_native(monkeypatch)

    assert CLI.main([*args, "--native-crosscheck"]) == 1

    assert _calls(backends, detectors) == (1, 1)
    assert len(calls) == 1
    _crop, source_path, parsed = calls[0]
    assert source_path == root / "source.pdf"
    assert parsed.doc_id == "doc-1"
    assert parsed.title == "native"

    page = _json(next((root / "out" / "pages").glob("*/page.json")))
    region = _json(next((root / "out" / "pages").glob("*/regions/*.json")))
    audit = region["native_crosscheck"]
    assert page["status"] == "pending"
    assert region["status"] == "pending"
    assert region["result"]["success"] is False
    assert "local_expectation_value" in region["result"]["reason_codes"]
    assert audit["status"] == "ready"
    assert audit["needs_review"] is False
    assert audit["audit"]["origin"] == "fake-native"
    assert audit["audit"]["diagnostic_only"] is True
    assert audit["audit"]["not_truth"] is True
    assert audit["expectation"]["numeric_checks"][0]["row_key"] == "A"


def test_native_crosscheck_adapter_error_blocks_local_call(tmp_path, monkeypatch):
    root = tmp_path / "native-error"
    args, backends, detectors = _setup(
        root,
        monkeypatch,
        labels=("table",),
        native_paths=True,
        local_text=TABLE_HTML,
    )
    calls = _fake_native(
        monkeypatch,
        error=NativeExpectationValidationError("source binding mismatch"),
    )

    assert CLI.main([*args, "--native-crosscheck"]) == 2

    assert _calls(backends, detectors) == (0, 1)
    assert len(calls) == 1
    page = _json(next((root / "out" / "pages").glob("*/page.json")))
    region = _json(next((root / "out" / "pages").glob("*/regions/*.json")))
    assert page["status"] == "error"
    assert region["status"] == "error"
    assert region["native_crosscheck"]["status"] == "error"
    assert "NativeExpectationValidationError" in region["native_crosscheck"]["error"]
    assert page["counts"]["error_count"] == 1
    assert page["counts"]["local_requests"] == 0
    summary = _json(root / "out" / "summary.json")
    assert (summary["status"], summary["error_count"], summary["pending_count"]) == (
        "error",
        1,
        1,
    )


@pytest.mark.parametrize(
    "override",
    [
        {"doc_id": "doc-2"},
        {"version_id": "v2"},
        {"source_sha256": "b" * 64},
        {"format": "html"},
        {"page_count": 0},
    ],
    ids=["doc-id", "version-id", "source-sha", "format", "page-count"],
)
def test_native_crosscheck_rejects_wrong_parsed_binding_before_inference(
    tmp_path, monkeypatch, capsys, override
):
    root = tmp_path / "wrong-binding"
    args, backends, detectors = _setup(root, monkeypatch, native_paths=True)
    source_sha = hashlib.sha256((root / "source.pdf").read_bytes()).hexdigest()
    _write_parsed(root / "parsed.json", source_sha, **override)
    capsys.readouterr()

    assert CLI.main([*args, "--native-crosscheck"]) == 2

    assert "binding mismatch" in capsys.readouterr().err
    assert _calls(backends, detectors) == (0, 0)
    assert not (root / "out" / "manifest.json").exists()


def test_native_needs_review_propagates_to_routed_result(tmp_path, monkeypatch):
    root = tmp_path / "native-review"
    args, backends, detectors = _setup(
        root,
        monkeypatch,
        labels=("table",),
        needs_review=False,
        native_paths=True,
        local_text=TABLE_HTML,
    )
    _fake_native(monkeypatch, status="needs_review")

    assert CLI.main([*args, "--native-crosscheck"]) == 1

    assert _calls(backends, detectors) == (1, 1)
    page = _json(next((root / "out" / "pages").glob("*/page.json")))
    region = _json(next((root / "out" / "pages").glob("*/regions/*.json")))
    result = region["result"]
    assert page["status"] == "pending"
    assert region["status"] == "pending"
    assert region["native_crosscheck"]["status"] == "needs_review"
    assert region["native_crosscheck"]["audit"]["not_truth"] is True
    assert result["success"] is True
    assert result["needs_review"] is True
    assert result["reason_codes"].count("native_source_needs_review") == 1
    assert result["publishable"] is False
    assert result["visually_verified"] is False
    assert result["usage"]["local_requests"] == 1
    assert result["cache"] == "miss"
    cached = _json(next((root / "out" / "route_cache").glob("*.json")))
    assert "native_source_needs_review" not in cached["result"]["reason_codes"]
    assert result["reason_codes"] == [
        *cached["result"]["reason_codes"],
        "native_source_needs_review",
    ]
    assert result["local_result"] == cached["result"]["local_result"]
    assert result["usage"] == cached["result"]["usage"]
    summary = _json(root / "out" / "summary.json")
    assert (summary["status"], summary["pending_count"], summary["routed_count"]) == (
        "pending",
        1,
        0,
    )


def test_native_crosscheck_manifest_identity_and_disabled_no_read(
    tmp_path, monkeypatch, capsys
):
    no_read = tmp_path / "no-read"
    args, backends, detectors = _setup(no_read, monkeypatch, native_paths=True)
    (no_read / "parsed.json").unlink()
    assert CLI.main(args) == 0
    assert _calls(backends, detectors) == (1, 1)
    assert _json(no_read / "out" / "summary.json")["status"] == "ok"

    flag_root = tmp_path / "flag"
    args, backends, detectors = _setup(flag_root, monkeypatch, native_paths=True)
    assert CLI.main(args) == 0
    capsys.readouterr()
    manifest = _json(flag_root / "out" / "manifest.json")
    assert manifest["native_crosscheck"] is False
    assert manifest["native_parsed_sha256"] == {}
    assert CLI.main([*args, "--resume", "--native-crosscheck"]) == 2
    assert "existing output settings differ" in capsys.readouterr().err
    assert _calls(backends, detectors) == (1, 1)

    sha_root = tmp_path / "sha"
    args, backends, detectors = _setup(sha_root, monkeypatch, native_paths=True)
    assert CLI.main([*args, "--native-crosscheck"]) == 0
    capsys.readouterr()
    row = json.loads((sha_root / "rows.jsonl").read_text(encoding="utf-8"))
    initial_sha = hashlib.sha256((sha_root / "parsed.json").read_bytes()).hexdigest()
    manifest = _json(sha_root / "out" / "manifest.json")
    assert manifest["native_crosscheck"] is True
    assert manifest["native_parsed_sha256"] == {"sample-1": initial_sha}
    _write_parsed(sha_root / "parsed.json", row["source_sha256"], title="changed")
    assert CLI.main([*args, "--resume", "--native-crosscheck"]) == 2
    assert "existing output settings differ" in capsys.readouterr().err
    assert _calls(backends, detectors) == (1, 1)
