from __future__ import annotations

import ast
import contextlib
import hashlib
import json
import re
import sys
import tempfile
import types
from pathlib import Path
from types import SimpleNamespace

import pytest

from campusguide.ingest import ocr_runtime
from campusguide.ingest.xiaomi_ocr import (
    OcrBackendRequest,
    XiaomiOcrBackendError,
    XiaomiOcrValidationError,
)

DEFAULT_CROPPING = "def fit_image(image):\n    return image\n"
DEFAULT_DOCUMENT = "def finalize_document(text):\n    return text.strip().upper()\n"
MODEL_ROOT = Path("models") / "Xiaomi-OCR-0"
REPO_ROOT = Path("Xiaomi-OCR-0")


@pytest.fixture(autouse=True)
def _isolated_official_modules():
    before_path = list(sys.path)
    saved = {
        name: module
        for name, module in sys.modules.items()
        if name == "evaluation_pipeline"
        or name.startswith("evaluation_pipeline.")
        or name == "postprocess"
        or name.startswith("postprocess.")
    }
    for name in saved:
        sys.modules.pop(name, None)
    yield
    for name in list(sys.modules):
        if (
            name == "evaluation_pipeline"
            or name.startswith("evaluation_pipeline.")
            or name == "postprocess"
            or name.startswith("postprocess.")
        ):
            sys.modules.pop(name, None)
    sys.modules.update(saved)
    sys.path[:] = before_path


def _entry(name: str, data: bytes) -> dict:
    return {"name": name, "sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data)}


def build_install(
    tmp_path: Path,
    *,
    cropping: str = DEFAULT_CROPPING,
    document: str = DEFAULT_DOCUMENT,
    manifest_edit=None,
) -> Path:
    root = tmp_path / "install"
    model_root = root / MODEL_ROOT
    repo_root = root / REPO_ROOT
    model_root.mkdir(parents=True)
    (repo_root / "evaluation_pipeline").mkdir(parents=True)
    (repo_root / "postprocess").mkdir(parents=True)
    entries = []
    for name in ocr_runtime.REQUIRED_MODEL_FILES:
        data = f"fake-weights-{name}".encode()
        (model_root / name).write_bytes(data)
        entries.append(_entry(f"model/{name}", data))
    repo_contents = {
        "evaluation_pipeline/__init__.py": b"",
        "evaluation_pipeline/cropping.py": cropping.encode(),
        "postprocess/__init__.py": b"",
        "postprocess/document.py": document.encode(),
    }
    for relative, data in repo_contents.items():
        (repo_root / relative).parent.mkdir(parents=True, exist_ok=True)
        (repo_root / relative).write_bytes(data)
        entries.append(_entry(f"repo/{relative}", data))
    manifest = {
        "model": ocr_runtime.EXPECTED_MODEL,
        "model_revision": ocr_runtime.EXPECTED_MODEL_REVISION,
        "github_commit": ocr_runtime.EXPECTED_GITHUB_COMMIT,
        "files": entries,
    }
    if manifest_edit is not None:
        manifest_edit(manifest)
    (root / "installation_manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False), encoding="utf-8"
    )
    return root


def make_backend(root: Path, device: str = "cuda:0"):
    return ocr_runtime.TransformersXiaomiBackend(root, device=device)


def make_request(tmp_path: Path, **overrides) -> OcrBackendRequest:
    image = tmp_path / "crop.png"
    image.write_bytes(ocr_runtime._PNG_SIGNATURE + b"crop-bytes")
    sha = hashlib.sha256(image.read_bytes()).hexdigest()
    values = {
        "image_path": image,
        "image_sha256": sha,
        "page": 1,
        "prompt": "Extract the document.",
        "max_tokens": 8,
        "model": ocr_runtime.EXPECTED_MODEL,
        "model_revision": ocr_runtime.EXPECTED_MODEL_REVISION,
        "backend": ocr_runtime.BACKEND_ID,
    }
    values.update(overrides)
    return OcrBackendRequest(**values)


def test_module_import_does_not_touch_heavy_packages():
    tree = ast.parse(Path(ocr_runtime.__file__).read_text(encoding="utf-8"))
    imported = set()
    for node in tree.body:
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    assert imported.isdisjoint({"torch", "transformers", "PIL"})
    assert not hasattr(ocr_runtime, "torch")
    assert not hasattr(ocr_runtime, "PIL")


def test_identity_properties_and_request_binding(tmp_path):
    root = build_install(tmp_path)
    backend = make_backend(root)
    assert backend.model == ocr_runtime.EXPECTED_MODEL
    assert backend.model_revision == ocr_runtime.EXPECTED_MODEL_REVISION
    assert backend.backend_identity == ocr_runtime.BACKEND_ID
    assert backend.last_call_audit == {}

    ocr_runtime.TransformersXiaomiBackend(
        root, model_revision=ocr_runtime.EXPECTED_MODEL_REVISION, device="cpu"
    )
    with pytest.raises(XiaomiOcrValidationError):
        ocr_runtime.TransformersXiaomiBackend(root, model_revision="0" * 40)

    request = make_request(tmp_path)
    with pytest.raises(XiaomiOcrValidationError):
        backend._validate_request(request.model_copy(update={"model": "other/model"}))
    with pytest.raises(XiaomiOcrValidationError):
        backend._validate_request(
            request.model_copy(update={"model_revision": "0" * 40})
        )
    with pytest.raises(XiaomiOcrValidationError):
        backend._validate_request(request.model_copy(update={"max_tokens": 16385}))
    forged = OcrBackendRequest.model_construct(
        image_path=request.image_path,
        image_sha256=request.image_sha256,
        page=1,
        prompt=request.prompt,
        max_tokens=True,
        model=request.model,
        model_revision=request.model_revision,
        backend=request.backend,
    )
    with pytest.raises(XiaomiOcrValidationError):
        backend._validate_request(forged)


@pytest.mark.parametrize(
    "key,value",
    [
        ("model", "someone/else"),
        ("model_revision", "0" * 40),
        ("model_revision", "main"),
        ("github_commit", "0" * 40),
    ],
)
def test_manifest_identity_is_pinned(tmp_path, key, value):
    root = build_install(tmp_path, manifest_edit=lambda m: m.__setitem__(key, value))
    with pytest.raises(XiaomiOcrBackendError):
        make_backend(root)


@pytest.mark.parametrize(
    "name",
    [
        "model/../evil.bin",
        "model/./config.json",
        "model//config.json",
        "/model/config.json",
        "model\\config.json",
        "elsewhere/file.bin",
        "model/",
    ],
)
def test_manifest_rejects_unsafe_names(tmp_path, name):
    def edit(manifest):
        manifest["files"].append(
            {"name": name, "sha256": "0" * 64, "bytes": 0}
        )

    root = build_install(tmp_path, manifest_edit=edit)
    with pytest.raises(XiaomiOcrBackendError):
        make_backend(root)


def test_manifest_rejects_duplicates_and_bad_digests(tmp_path):
    def duplicate(manifest):
        manifest["files"].append(dict(manifest["files"][0]))

    with pytest.raises(XiaomiOcrBackendError):
        make_backend(build_install(tmp_path / "dup", manifest_edit=duplicate))

    def bad_sha(manifest):
        manifest["files"].append(
            {"name": "model/extra.bin", "sha256": "not-a-digest", "bytes": 1}
        )

    with pytest.raises(XiaomiOcrBackendError):
        make_backend(build_install(tmp_path / "sha", manifest_edit=bad_sha))

    def bad_bytes(manifest):
        manifest["files"].append(
            {"name": "model/extra.bin", "sha256": "0" * 64, "bytes": True}
        )

    with pytest.raises(XiaomiOcrBackendError):
        make_backend(build_install(tmp_path / "bytes", manifest_edit=bad_bytes))


@pytest.mark.parametrize(
    "missing",
    ["model/config.json", "repo/postprocess/document.py"],
)
def test_manifest_requires_declared_files(tmp_path, missing):
    def edit(manifest):
        manifest["files"] = [e for e in manifest["files"] if e["name"] != missing]

    root = build_install(tmp_path, manifest_edit=edit)
    with pytest.raises(XiaomiOcrBackendError):
        make_backend(root)


def test_manifest_tamper_after_init_fails_closed(tmp_path):
    root = build_install(tmp_path)
    backend = make_backend(root)
    manifest = root / "installation_manifest.json"
    manifest.write_text(manifest.read_text(encoding="utf-8") + " ", encoding="utf-8")
    with pytest.raises(XiaomiOcrBackendError):
        backend._check()


def test_model_hash_and_size_mismatch_fail_before_load(tmp_path):
    def edit_sha(manifest):
        for entry in manifest["files"]:
            if entry["name"] == "model/config.json":
                entry["sha256"] = "0" * 64

    backend = make_backend(build_install(tmp_path / "sha", manifest_edit=edit_sha))
    with pytest.raises(XiaomiOcrBackendError, match="SHA-256"):
        backend._ensure_model()

    def edit_size(manifest):
        for entry in manifest["files"]:
            if entry["name"] == "model/config.json":
                entry["bytes"] += 1

    backend = make_backend(build_install(tmp_path / "size", manifest_edit=edit_size))
    with pytest.raises(XiaomiOcrBackendError, match="大小"):
        backend._ensure_model()


def test_model_set_changes_fail_closed(tmp_path):
    root = build_install(tmp_path)
    backend = make_backend(root)
    backend._ensure_model()
    assert set(backend._verified_model or {}) == set(
        ocr_runtime.REQUIRED_MODEL_FILES
    )
    (root / MODEL_ROOT / "extra.bin").write_bytes(b"unexpected")
    with pytest.raises(XiaomiOcrBackendError):
        backend._ensure_model()

    missing_root = build_install(tmp_path / "missing")
    missing = make_backend(missing_root)
    (missing_root / MODEL_ROOT / "tokenizer.json").unlink()
    with pytest.raises(XiaomiOcrBackendError):
        missing._ensure_model()

    tamper_root = build_install(tmp_path / "tamper")
    tampered = make_backend(tamper_root)
    tampered._ensure_model()
    config = tamper_root / MODEL_ROOT / "config.json"
    data = bytearray(config.read_bytes())
    data[-1] ^= 1
    config.write_bytes(data)
    with pytest.raises(XiaomiOcrBackendError):
        tampered._ensure_model()


def test_repo_set_tamper_fail_closed(tmp_path):
    root = build_install(tmp_path)
    backend = make_backend(root)
    revision = backend.postprocess_revision
    assert re.fullmatch(r"[0-9a-f]{64}", revision)
    assert backend.postprocess(" hello ") == "HELLO"

    (root / REPO_ROOT / "postprocess" / "extra.py").write_text("x = 1\n")
    with pytest.raises(XiaomiOcrBackendError):
        _ = backend.postprocess_revision


def test_repo_content_tamper_fail_closed(tmp_path):
    root = build_install(tmp_path)
    backend = make_backend(root)
    backend.postprocess("hello")
    document = root / REPO_ROOT / "postprocess" / "document.py"
    document.write_text("def finalize_document(text):\n    return 'tampered'\n")
    with pytest.raises(XiaomiOcrBackendError):
        backend.postprocess("hello")


def test_official_module_origin_is_verified(tmp_path, monkeypatch):
    root = build_install(tmp_path)
    backend = make_backend(root)
    fake_package = types.ModuleType("postprocess")
    fake_package.__path__ = []
    fake_document = types.ModuleType("postprocess.document")
    fake_document.__file__ = str(tmp_path / "elsewhere" / "document.py")
    fake_document.finalize_document = lambda text: text
    fake_package.document = fake_document
    monkeypatch.setitem(sys.modules, "postprocess", fake_package)
    monkeypatch.setitem(sys.modules, "postprocess.document", fake_document)
    with pytest.raises(XiaomiOcrBackendError):
        backend._ensure_official_modules()


class FakeImage:
    def __init__(self, size=(800, 600), format="PNG", mode="RGB"):
        self.size = tuple(size)
        self.format = format
        self.mode = mode

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def load(self):
        return None

    def convert(self, mode):
        return FakeImage(self.size, "PNG", mode)

    def save(self, buffer, format):
        buffer.write(b"fake-png-" + repr(self.size).encode())


def install_fake_pil(monkeypatch, *, size=(800, 600), format="PNG"):
    module = types.ModuleType("PIL")
    image_module = types.ModuleType("PIL.Image")
    image_module.Image = FakeImage
    image_module.open = lambda source: FakeImage(size=size, format=format)
    module.Image = image_module
    monkeypatch.setitem(sys.modules, "PIL", module)
    monkeypatch.setitem(sys.modules, "PIL.Image", image_module)


def test_prepare_uses_verified_fit_image_and_content_addressed_png(
    tmp_path, monkeypatch
):
    install_fake_pil(monkeypatch)
    backend = make_backend(build_install(tmp_path))
    request = make_request(tmp_path)
    with tempfile.TemporaryDirectory() as temp:
        input_info, prepared, path = backend._prepare(request, Path(temp))
        assert input_info["sha256"] == request.image_sha256
        assert input_info["format"] == "PNG"
        assert (input_info["width"], input_info["height"]) == (800, 600)
        assert path.is_file()
        assert path != request.image_path
        assert path.name == f"{prepared['sha256']}.png"
        assert prepared["sha256"] == hashlib.sha256(path.read_bytes()).hexdigest()
        assert prepared["bytes"] == len(path.read_bytes())
        assert prepared["width"] == 800
        assert prepared["temporary"] is True
        assert prepared["fit_image_max_edge"] == 2048


def test_prepare_rejects_hash_mismatch_non_png_and_oversize(tmp_path, monkeypatch):
    install_fake_pil(monkeypatch)
    backend = make_backend(build_install(tmp_path))

    with pytest.raises(XiaomiOcrBackendError, match="发生变化"):
        backend._prepare(
            make_request(tmp_path, image_sha256="0" * 64),
            tmp_path,
        )

    non_png = tmp_path / "not.png"
    non_png.write_bytes(b"not a png")
    request = make_request(
        tmp_path,
        image_path=non_png,
        image_sha256=hashlib.sha256(non_png.read_bytes()).hexdigest(),
    )
    with pytest.raises(XiaomiOcrBackendError, match="PNG"):
        backend._prepare(request, tmp_path)

    install_fake_pil(monkeypatch, size=(3000, 10))
    with pytest.raises(XiaomiOcrBackendError, match="max_edge"):
        backend._prepare(make_request(tmp_path), tmp_path)


class FakeScalar:
    def __init__(self, value):
        self.value = value

    def item(self):
        return self.value


class FakeTensor:
    def __init__(self, tokens):
        self.tokens = list(tokens)
        self.shape = (1, len(self.tokens))

    def __getitem__(self, key):
        if isinstance(key, tuple) and isinstance(key[0], slice):
            start = key[1].start or 0
            return FakeTensor(self.tokens[start:])
        if isinstance(key, tuple) and key[0] == 0:
            return FakeScalar(self.tokens[key[1]])
        raise TypeError(f"unsupported index {key!r}")


class FakeCuda:
    def __init__(self):
        self.peak = 654321
        self.reset_calls = 0

    def synchronize(self, device):
        return None

    def reset_peak_memory_stats(self, device):
        self.reset_calls += 1

    def max_memory_allocated(self, device):
        return self.peak


class FakeTorch:
    __version__ = "2.fake"
    float16 = "torch.float16"
    float32 = "torch.float32"

    def __init__(self):
        self.version = SimpleNamespace(cuda="12.1")
        self.cuda = FakeCuda()

    def inference_mode(self):
        return contextlib.nullcontext()


class FakeModel:
    def __init__(self, output_tokens=(), error=None):
        self.output_tokens = list(output_tokens)
        self.error = error
        self.calls = []
        self.moved_to = None
        self.evaluated = False
        self.generation_config = SimpleNamespace(eos_token_id=2)
        self.config = SimpleNamespace(eos_token_id=2)

    def generate(self, **kwargs):
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        return FakeTensor([100, 101, 102, *self.output_tokens])

    def to(self, device):
        self.moved_to = device
        return self

    def eval(self):
        self.evaluated = True
        return self

    def parameters(self):
        return iter([SimpleNamespace(dtype="torch.float16")])


class FakeProcessor:
    def __init__(self, decoded="OCR text"):
        self.decoded = decoded
        self.calls = []
        self.tokenizer = SimpleNamespace(eos_token_id=2)

    def apply_chat_template(self, messages, **kwargs):
        self.calls.append((messages, kwargs))
        return {"input_ids": SimpleNamespace(shape=(1, 3))}

    def batch_decode(self, tokens, skip_special_tokens=True):
        return [self.decoded if tokens.tokens else ""]


def runtime_for(model, processor, *, cuda=True):
    return {
        "torch": FakeTorch(),
        "processor": processor,
        "model": model,
        "device": "cuda:0" if cuda else "cpu",
        "dtype": "torch.float16" if cuda else "torch.float32",
        "cuda": cuda,
        "model_class": type(model).__name__,
        "processor_class": type(processor).__name__,
        "model_module": type(model).__module__,
        "processor_module": type(processor).__module__,
        "torch_version": "2.fake",
        "transformers_version": "4.fake",
        "cuda_version": "12.1" if cuda else "None",
        "device_name": "Fake GPU" if cuda else "cpu",
        "parameter_dtype": "torch.float16" if cuda else "torch.float32",
        "load_seconds": 0.25,
    }


def install_runtime(monkeypatch, backend, runtime):
    calls = {"load": 0}

    def load():
        calls["load"] += 1
        return runtime

    def prepare(request, temp_dir):
        path = Path(temp_dir) / "prepared.png"
        path.write_bytes(b"prepared")
        return (
            {
                "path": str(request.image_path),
                "sha256": request.image_sha256,
                "width": 100,
                "height": 50,
            },
            {
                "path": str(path),
                "sha256": "prepared-sha",
                "width": 100,
                "height": 50,
                "preprocess_seconds": 0.01,
                "temporary": True,
            },
            path,
        )

    monkeypatch.setattr(backend, "_load_runtime", load)
    monkeypatch.setattr(backend, "_prepare", prepare)
    return calls


def test_one_load_repeated_calls_and_complete_audit(tmp_path, monkeypatch):
    backend = make_backend(build_install(tmp_path))
    model = FakeModel([10, 2])
    processor = FakeProcessor()
    runtime = runtime_for(model, processor)
    calls = install_runtime(monkeypatch, backend, runtime)
    request = make_request(tmp_path)

    first = backend(request)
    second = backend(request)

    assert calls["load"] == 1
    assert backend._runtime is runtime
    assert first.finish_reason == "stop"
    assert (first.prompt_tokens, first.completion_tokens, first.total_tokens) == (
        3,
        2,
        5,
    )
    assert second.text == "OCR text"
    messages, template_kwargs = processor.calls[0]
    assert messages[0]["content"][0]["type"] == "image"
    assert messages[0]["content"][0]["url"].endswith("prepared.png")
    assert messages[0]["content"][1]["text"] == request.prompt
    assert template_kwargs == {
        "tokenize": True,
        "add_generation_prompt": True,
        "return_dict": True,
        "return_tensors": "pt",
        "enable_thinking": False,
    }
    assert model.calls[0]["max_new_tokens"] == 8
    assert model.calls[0]["do_sample"] is False
    assert runtime["torch"].cuda.reset_calls == 2

    audit = backend.last_call_audit
    assert audit["schema"] == ocr_runtime.AUDIT_SCHEMA_VERSION
    assert audit["stage"] == "complete"
    assert audit["request"]["image_sha256"] == request.image_sha256
    assert audit["input_image"]["width"] == 100
    assert audit["prepared_image"]["temporary_removed"] is True
    assert audit["timing"]["load_seconds"] == 0.0
    assert audit["timing"]["inference_seconds"] is not None
    assert audit["runtime"]["device"] == "cuda:0"
    assert audit["runtime"]["dtype"] == "torch.float16"
    assert audit["runtime"]["model_class"] == "FakeModel"
    assert audit["runtime"]["processor_class"] == "FakeProcessor"
    assert audit["tokens"] == {"prompt": 3, "completion": 2, "total": 5}
    assert audit["generation"]["last_token_id"] == 2
    assert audit["generation"]["eos_token_ids"] == [2]
    assert audit["generation"]["finish_reason"] == "stop"
    assert audit["gpu_peak_memory_bytes"] == 654321
    assert audit["install"]["model_revision"] == ocr_runtime.EXPECTED_MODEL_REVISION
    assert audit["install"]["github_commit"] == ocr_runtime.EXPECTED_GITHUB_COMMIT
    assert set(audit["install"]["model_files"]) == set(
        ocr_runtime.REQUIRED_MODEL_FILES
    )
    assert re.fullmatch(r"[0-9a-f]{64}", audit["install"]["manifest_sha256"])


@pytest.mark.parametrize(
    "tokens,max_tokens,expected",
    [
        ([10, 11], 2, "length"),
        ([10, 11], 8, "stopped_other"),
    ],
)
def test_generation_finish_reason_comes_from_tokens(
    tmp_path, monkeypatch, tokens, max_tokens, expected
):
    backend = make_backend(build_install(tmp_path))
    processor = FakeProcessor()
    install_runtime(
        monkeypatch, backend, runtime_for(FakeModel(tokens), processor)
    )
    result = backend(make_request(tmp_path, max_tokens=max_tokens))
    assert result.finish_reason == expected
    assert backend.last_call_audit["generation"]["generated_tokens"] == len(tokens)


def test_empty_generation_is_rejected_with_actual_usage(tmp_path, monkeypatch):
    backend = make_backend(build_install(tmp_path))
    install_runtime(
        monkeypatch, backend, runtime_for(FakeModel([]), FakeProcessor())
    )
    with pytest.raises(XiaomiOcrBackendError, match="未返回"):
        backend(make_request(tmp_path))
    audit = backend.last_call_audit
    assert audit["tokens"] == {"prompt": 3, "completion": 0, "total": 3}
    assert audit["generation"]["finish_reason"] == "empty"
    assert audit["errors"]


def test_generation_failure_preserves_unknown_usage_and_redacts_error(
    tmp_path, monkeypatch
):
    backend = make_backend(build_install(tmp_path))
    install_runtime(
        monkeypatch,
        backend,
        runtime_for(
            FakeModel(error=RuntimeError("token=supersecret123")), FakeProcessor()
        ),
    )
    with pytest.raises(XiaomiOcrBackendError):
        backend(make_request(tmp_path))
    audit = backend.last_call_audit
    assert audit["tokens"] == {"prompt": 3, "completion": None, "total": None}
    assert audit["stage"] == "generate"
    assert "supersecret123" not in json.dumps(audit, ensure_ascii=False)
    assert audit["errors"][0]["stage"] == "generate"


def test_load_runtime_refuses_implicit_cpu_fallback(tmp_path, monkeypatch):
    torch = types.ModuleType("torch")
    torch.__version__ = "2.fake"
    torch.float16 = "torch.float16"
    torch.float32 = "torch.float32"
    torch.version = SimpleNamespace(cuda="12.1")
    torch.cuda = SimpleNamespace(is_available=lambda: False)
    transformers = types.ModuleType("transformers")
    transformers.AutoProcessor = object
    transformers.AutoModelForImageTextToText = object
    monkeypatch.setitem(sys.modules, "torch", torch)
    monkeypatch.setitem(sys.modules, "transformers", transformers)

    backend = make_backend(build_install(tmp_path), device="cuda")
    with pytest.raises(XiaomiOcrBackendError, match="CPU 回退"):
        backend._load_runtime()


def test_load_runtime_cpu_uses_local_float32_contract(tmp_path, monkeypatch):
    torch = types.ModuleType("torch")
    torch.__version__ = "2.fake"
    torch.float16 = "torch.float16"
    torch.float32 = "torch.float32"
    torch.version = SimpleNamespace(cuda=None)
    torch.cuda = SimpleNamespace(is_available=lambda: False)
    model_calls = []
    processor_calls = []
    model = FakeModel()

    class AutoProcessor:
        @classmethod
        def from_pretrained(cls, path, **kwargs):
            processor_calls.append((path, kwargs))
            return FakeProcessor()

    class AutoModel:
        @classmethod
        def from_pretrained(cls, path, **kwargs):
            model_calls.append((path, kwargs))
            return model

    transformers = types.ModuleType("transformers")
    transformers.__version__ = "4.fake"
    transformers.AutoProcessor = AutoProcessor
    transformers.AutoModelForImageTextToText = AutoModel
    monkeypatch.setitem(sys.modules, "torch", torch)
    monkeypatch.setitem(sys.modules, "transformers", transformers)

    root = build_install(tmp_path)
    runtime = make_backend(root, device="cpu")._load_runtime()
    assert runtime["dtype"] == "torch.float32"
    assert runtime["device"] == "cpu"
    assert model.moved_to == "cpu"
    assert model.evaluated is True
    assert processor_calls[0][0] == str(root / MODEL_ROOT)
    assert processor_calls[0][1] == {
        "local_files_only": True,
        "trust_remote_code": False,
    }
    assert model_calls[0][1]["dtype"] == "torch.float32"
    assert model_calls[0][1]["local_files_only"] is True
    assert model_calls[0][1]["trust_remote_code"] is False
