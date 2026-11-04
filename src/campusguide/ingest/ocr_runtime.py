"""Reusable local Transformers backend for the original Xiaomi-OCR-0 weights.

Importing this module stays light: Torch, Transformers, Pillow and the official
pre/post-processing modules load only when the backend runs. Every use verifies
``installation_manifest.json``, hashes each declared file once, then checks stat
fingerprints so later mutation fails closed without re-hashing 1.7 GB per crop.
"""

from __future__ import annotations

import hashlib
import io
import json
import re
import sys
import tempfile
import threading
import time
from collections.abc import Mapping
from contextlib import suppress
from importlib import import_module
from pathlib import Path, PurePosixPath
from typing import Any

from campusguide.ingest.xiaomi_ocr import (
    OcrBackendRequest,
    OcrBackendResult,
    XiaomiOcrBackendError,
    XiaomiOcrError,
    XiaomiOcrValidationError,
)
from campusguide.observability.errors import safe_error_summary

BACKEND_ID = "transformers-xiaomi-ocr-local"
AUDIT_SCHEMA_VERSION = "xiaomi-ocr-runtime-audit/1"
EXPECTED_MODEL = "SeerRay-Lab/Xiaomi-OCR-0"
EXPECTED_MODEL_REVISION = "bbd8cf8d21f256b3644b44cb860f3a7cecb93c30"
EXPECTED_GITHUB_COMMIT = "b1a9bb4c07060c453785e9151715ccda6b96f0b1"
MODEL_DIRECTORY = ("models", "Xiaomi-OCR-0")
REPO_DIRECTORY = "Xiaomi-OCR-0"
REQUIRED_MODEL_FILES = (
    "config.json",
    "processor_config.json",
    "tokenizer.json",
    "tokenizer_config.json",
    "chat_template.jinja",
    "model.safetensors",
)
REQUIRED_REPO_FILES = ("evaluation_pipeline/cropping.py", "postprocess/document.py")
MAX_NEW_TOKENS = 16384
FIT_IMAGE_MAX_EDGE = 2048
_PINNED_REVISION_RE = re.compile(r"^[0-9a-f]{40}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_DEVICE_RE = re.compile(r"^(?:cpu|cuda(?::\d+)?)$")
_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
_SHA256_CHUNK = 8 * 1024 * 1024


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sha256_file(path: Path, chunk_size: int = _SHA256_CHUNK) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(chunk_size), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _digest(value: Any) -> str:
    text = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return _sha256_bytes(text.encode("utf-8"))


def _fingerprint(path: Path) -> tuple[int, int, int, int]:
    stat = path.stat()
    return (int(stat.st_size), int(stat.st_mtime_ns), int(stat.st_ino), int(stat.st_dev))


def _manifest_name(name: object) -> str:
    if not isinstance(name, str) or not name:
        raise XiaomiOcrBackendError("安装清单文件名必须是非空字符串")
    if "\\" in name or "\x00" in name:
        raise XiaomiOcrBackendError(f"安装清单文件名含不允许的字符: {name!r}")
    pure = PurePosixPath(name)
    if pure.is_absolute() or pure.as_posix() != name:
        raise XiaomiOcrBackendError(f"安装清单文件名不是规范相对路径: {name!r}")
    if any(part in {"", ".", ".."} or ":" in part for part in pure.parts):
        raise XiaomiOcrBackendError(f"安装清单文件名含路径穿越: {name!r}")
    if not (name.startswith("model/") or name.startswith("repo/")):
        raise XiaomiOcrBackendError(f"安装清单文件名前缀不受支持: {name!r}")
    return name


def _eos_ids(*sources: object) -> set[int]:
    result: set[int] = set()

    def visit(value: object) -> None:
        if value is None or isinstance(value, bool):
            return
        if isinstance(value, int) and value >= 0:
            result.add(value)
        elif isinstance(value, (list, tuple, set)):
            for item in value:
                visit(item)

    for source in sources:
        visit(source)
    return result


def _classify(generated: int, limit: int, last: int | None, eos: set[int]) -> str:
    if generated <= 0:
        return "empty"
    if last is not None and last in eos:
        return "stop"
    return "length" if generated >= limit else "stopped_other"


class TransformersXiaomiBackend:
    """Local, serial, manifest-verified Xiaomi-OCR-0 inference backend."""

    def __init__(
        self,
        install_root: Path = Path("D:/xiaomi-ocr"),
        device: str = "cuda",
        model_revision: str | None = None,
    ) -> None:
        if not isinstance(install_root, (str, Path)) or not str(install_root).strip():
            raise XiaomiOcrValidationError("install_root 必须是非空路径")
        if not isinstance(device, str) or _DEVICE_RE.fullmatch(device.strip()) is None:
            raise XiaomiOcrValidationError("device 必须是 cpu 或 cuda[:N]")
        self.install_root = Path(install_root).expanduser().resolve()
        self.device = device.strip()
        self._lock = threading.RLock()
        self.last_call_audit: dict[str, Any] = {}
        self._manifest_path = self.install_root / "installation_manifest.json"
        self._manifest_sha256 = ""
        self._manifest_fingerprint: tuple[int, int, int, int] | None = None
        self._model_root = self.install_root.joinpath(*MODEL_DIRECTORY)
        self._repo_root = self.install_root / REPO_DIRECTORY
        self._model_entries: dict[str, dict[str, Any]] = {}
        self._repo_entries: dict[str, dict[str, Any]] = {}
        self._model_name = ""
        self._model_revision = ""
        self._github_commit = ""
        self._verified_model: dict[str, str] | None = None
        self._verified_repo: dict[str, str] | None = None
        self._fingerprints: dict[Path, tuple[int, int, int, int]] = {}
        self._fit_image: Any = None
        self._finalize_document: Any = None
        self._runtime: dict[str, Any] | None = None
        self._read_manifest(model_revision)

    @property
    def model(self) -> str:
        return self._model_name

    @property
    def model_revision(self) -> str:
        return self._model_revision

    @property
    def backend_identity(self) -> str:
        return BACKEND_ID

    @property
    def postprocess_revision(self) -> str:
        with self._lock:
            self._ensure_repo()
            self._check()
            if self._verified_repo is None:
                raise XiaomiOcrBackendError("官方后处理代码尚未校验")
            return self._postprocess_digest(self._verified_repo)

    def __call__(self, request: OcrBackendRequest) -> OcrBackendResult:
        started = time.perf_counter()
        audit = self._new_audit(request)
        try:
            with self._lock:
                self._validate_request(request)
                self._check()
                with tempfile.TemporaryDirectory(prefix="xiaomi-ocr-runtime-") as temp:
                    audit["stage"] = "preprocess"
                    input_info, prepared, prepared_path = self._prepare(
                        request, Path(temp)
                    )
                    audit["input_image"] = input_info
                    audit["prepared_image"] = prepared
                    audit["timing"]["preprocess_seconds"] = prepared["preprocess_seconds"]
                    audit["stage"] = "load"
                    runtime, loaded = self._ensure_loaded()
                    audit["timing"]["load_seconds"] = (
                        float(runtime["load_seconds"]) if loaded else 0.0
                    )
                    audit["timing"]["load_seconds_recorded"] = runtime.get("load_seconds")
                    audit["runtime"].update(
                        {
                            "device": runtime.get("device"),
                            "dtype": runtime.get("dtype"),
                            "processor_class": runtime.get("processor_class"),
                            "model_class": runtime.get("model_class"),
                            "torch_version": runtime.get("torch_version"),
                            "transformers_version": runtime.get("transformers_version"),
                            "device_name": runtime.get("device_name"),
                        }
                    )
                    audit["install"] = self._install_audit()
                    audit["stage"] = "prompt"
                    result = self._generate(request, runtime, prepared_path, audit)
                    audit["stage"] = "complete"
                    return result
        except XiaomiOcrError as exc:
            audit["errors"].append(self._error(audit["stage"], exc))
            raise
        except Exception as exc:
            stage = audit["stage"]
            audit["errors"].append(self._error(stage, exc))
            raise XiaomiOcrBackendError(
                f"{stage} 失败: {safe_error_summary(exc)}"
            ) from exc
        finally:
            audit["timing"]["total_seconds"] = time.perf_counter() - started
            if audit["prepared_image"] is not None:
                audit["prepared_image"]["temporary_removed"] = True
            self.last_call_audit = audit

    def postprocess(self, text: str) -> str:
        if not isinstance(text, str):
            raise XiaomiOcrValidationError("postprocess 输入必须是字符串")
        with self._lock:
            self._ensure_official_modules()
            self._check()
            finalize = self._finalize_document
            if not callable(finalize):
                raise XiaomiOcrBackendError("官方 finalize_document 不可用")
            try:
                normalized = finalize(text)
            except Exception as exc:
                raise XiaomiOcrBackendError(
                    f"官方后处理失败: {safe_error_summary(exc)}"
                ) from exc
        if not isinstance(normalized, str):
            raise XiaomiOcrBackendError("官方后处理未返回字符串")
        if text.strip() and not normalized.strip():
            raise XiaomiOcrBackendError("官方后处理对非空输入返回空文本")
        return normalized

    def _new_audit(self, request: object) -> dict[str, Any]:
        prompt = getattr(request, "prompt", None)
        runtime = {
            "requested_device": self.device,
            "expected_dtype": "torch.float16" if self.device.startswith("cuda") else "torch.float32",
            "device": None, "dtype": None, "processor_class": None, "model_class": None,
            "torch_version": None, "transformers_version": None, "device_name": None,
        }
        return {
            "schema": AUDIT_SCHEMA_VERSION,
            "backend_identity": BACKEND_ID,
            "model": self._model_name,
            "model_revision": self._model_revision,
            "request": {
                "page": getattr(request, "page", None),
                "max_tokens": getattr(request, "max_tokens", None),
                "model": getattr(request, "model", None),
                "model_revision": getattr(request, "model_revision", None),
                "backend": getattr(request, "backend", None),
                "image_sha256": getattr(request, "image_sha256", None),
                "prompt_sha256": _sha256_bytes(prompt.encode("utf-8")) if isinstance(prompt, str) else None,
            },
            "input_image": None, "prepared_image": None, "install": None,
            "runtime": runtime,
            "timing": {
                "preprocess_seconds": 0.0, "load_seconds": 0.0, "load_seconds_recorded": None,
                "inference_seconds": None, "total_seconds": 0.0,
            },
            "tokens": {"prompt": None, "completion": None, "total": None},
            "generation": {
                "do_sample": False, "enable_thinking": False,
                "max_new_tokens": getattr(request, "max_tokens", None),
                "generated_tokens": None, "finish_reason": None, "last_token_id": None,
                "eos_token_ids": [], "raw_characters": None, "raw_sha256": None,
            },
            "gpu_peak_memory_bytes": None,
            "stage": "initialized",
            "errors": [],
        }

    @staticmethod
    def _error(stage: str, exc: BaseException) -> dict[str, Any]:
        return {"stage": stage, "type": type(exc).__name__, "message": safe_error_summary(exc)}

    def _install_audit(self) -> dict[str, Any]:
        if self._verified_model is None or self._verified_repo is None:
            raise XiaomiOcrBackendError("安装文件尚未完成校验")
        return {
            "manifest_path": str(self._manifest_path),
            "manifest_sha256": self._manifest_sha256,
            "model": self._model_name,
            "model_revision": self._model_revision,
            "github_commit": self._github_commit,
            "model_files": dict(sorted(self._verified_model.items())),
            "repo_python_files": dict(sorted(self._verified_repo.items())),
            "postprocess_revision": self._postprocess_digest(self._verified_repo),
        }

    @staticmethod
    def _postprocess_digest(hashes: Mapping[str, str]) -> str:
        return _digest({k: v for k, v in hashes.items() if k.startswith("postprocess/")})

    def _validate_request(self, request: object) -> None:
        if not isinstance(request, OcrBackendRequest):
            raise XiaomiOcrValidationError("request 必须是 OcrBackendRequest")
        if (
            isinstance(request.max_tokens, bool)
            or not isinstance(request.max_tokens, int)
            or not 1 <= request.max_tokens <= MAX_NEW_TOKENS
        ):
            raise XiaomiOcrValidationError(f"max_tokens 必须在 1..{MAX_NEW_TOKENS} 之间")
        if request.model != self._model_name:
            raise XiaomiOcrValidationError(f"request.model={request.model!r} 与固定模型不一致")
        if request.model_revision != self._model_revision:
            raise XiaomiOcrValidationError("request.model_revision 与固定 revision 不一致")

    def _prepare(self, request: OcrBackendRequest, temp_dir: Path) -> tuple[dict, dict, Path]:
        from PIL import Image

        started = time.perf_counter()
        path = request.image_path.expanduser()
        raw = path.read_bytes()
        actual = _sha256_bytes(raw)
        info: dict[str, Any] = {
            "path": str(path),
            "sha256": actual,
            "declared_sha256": request.image_sha256,
            "bytes": len(raw),
            "format": None,
            "mode": None,
            "width": None,
            "height": None,
        }
        if actual != request.image_sha256:
            raise XiaomiOcrBackendError(
                f"请求 PNG 在核验后发生变化: 声明 {request.image_sha256}，实际 {actual}"
            )
        if not raw.startswith(_PNG_SIGNATURE):
            raise XiaomiOcrBackendError("请求图像不是 PNG 文件")
        with Image.open(io.BytesIO(raw)) as source:
            source.load()
            info.update(
                {
                    "format": source.format,
                    "mode": source.mode,
                    "width": int(source.size[0]),
                    "height": int(source.size[1]),
                }
            )
            image = source.convert("RGB")
        if info["format"] != "PNG":
            raise XiaomiOcrBackendError(f"请求图像格式是 {info['format']!r}，只接受 PNG")
        if int(info["width"]) <= 0 or int(info["height"]) <= 0:
            raise XiaomiOcrBackendError("请求 PNG 尺寸无效")

        self._ensure_official_modules()
        fit_image = self._fit_image
        if not callable(fit_image):
            raise XiaomiOcrBackendError("官方 fit_image 不可用")
        fitted = fit_image(image)
        if fitted is not None:
            if not isinstance(fitted, Image.Image):
                raise XiaomiOcrBackendError(
                    f"官方 fit_image 返回 {type(fitted).__name__}，期望 PIL.Image 或 None"
                )
            image = fitted
        if not isinstance(image, Image.Image):
            raise XiaomiOcrBackendError("预处理结果不是 PIL.Image")
        if image.mode != "RGB":
            image = image.convert("RGB")
        width, height = int(image.size[0]), int(image.size[1])
        if width <= 0 or height <= 0:
            raise XiaomiOcrBackendError("预处理后图像尺寸无效")
        if max(width, height) > FIT_IMAGE_MAX_EDGE:
            raise XiaomiOcrBackendError(
                f"官方 fit_image 未把图像限制在 max_edge={FIT_IMAGE_MAX_EDGE} 内: {width}x{height}"
            )
        buffer = io.BytesIO()
        image.save(buffer, format="PNG")
        data = buffer.getvalue()
        digest = _sha256_bytes(data)
        prepared_path = temp_dir / f"{digest}.png"
        prepared_path.write_bytes(data)
        prepared = {
            "path": str(prepared_path),
            "sha256": digest,
            "bytes": len(data),
            "format": "PNG",
            "mode": image.mode,
            "width": width,
            "height": height,
            "fit_image_max_edge": FIT_IMAGE_MAX_EDGE,
            "temporary": True,
            "preprocess_seconds": time.perf_counter() - started,
        }
        return info, prepared, prepared_path

    def _generate(
        self,
        request: OcrBackendRequest,
        runtime: Mapping[str, Any],
        prepared_path: Path,
        audit: dict[str, Any],
    ) -> OcrBackendResult:
        torch, processor, model = runtime["torch"], runtime["processor"], runtime["model"]
        messages = [
            {
                "role": "user",
                "content": [
                    {"type": "image", "url": str(prepared_path)},
                    {"type": "text", "text": request.prompt},
                ],
            }
        ]
        inputs = processor.apply_chat_template(
            messages, tokenize=True, add_generation_prompt=True,
            return_dict=True, return_tensors="pt", enable_thinking=False,
        )

        prompt_tokens = int(inputs["input_ids"].shape[1])
        if prompt_tokens <= 0:
            raise XiaomiOcrBackendError("chat template 返回了空 prompt")
        audit["tokens"]["prompt"] = prompt_tokens
        if hasattr(inputs, "to"):
            inputs = inputs.to(runtime["device"])
        elif isinstance(inputs, Mapping):
            inputs = {k: (v.to(runtime["device"]) if hasattr(v, "to") else v) for k, v in inputs.items()}
        else:
            raise XiaomiOcrBackendError("chat template 输出既不是映射也没有 .to()")

        audit["stage"] = "generate"
        if runtime.get("cuda"):
            torch.cuda.synchronize(runtime["device"])
            torch.cuda.reset_peak_memory_stats(runtime["device"])
        started = time.perf_counter()
        with torch.inference_mode():
            output = model.generate(**inputs, max_new_tokens=request.max_tokens, do_sample=False)
        if runtime.get("cuda"):
            torch.cuda.synchronize(runtime["device"])
        audit["timing"]["inference_seconds"] = time.perf_counter() - started
        if runtime.get("cuda"):
            try:
                audit["gpu_peak_memory_bytes"] = int(
                    torch.cuda.max_memory_allocated(runtime["device"])
                )
            except Exception:
                audit["gpu_peak_memory_bytes"] = None

        audit["stage"] = "decode"
        shape = getattr(output, "shape", None)
        if shape is None or len(shape) != 2 or int(shape[0]) != 1:
            raise XiaomiOcrBackendError("generate 返回的序列不是单样本二维张量")
        if int(shape[1]) < prompt_tokens:
            raise XiaomiOcrBackendError("生成序列短于 prompt")
        new_tokens = output[:, prompt_tokens:]
        generated = int(new_tokens.shape[1])
        last_token_id = int(new_tokens[0, -1].item()) if generated > 0 else None
        decoded = processor.batch_decode(new_tokens, skip_special_tokens=True)
        if not isinstance(decoded, (list, tuple)) or not decoded or not isinstance(decoded[0], str):
            raise XiaomiOcrBackendError("batch_decode 未返回字符串")
        raw = decoded[0]

        eos = _eos_ids(
            getattr(getattr(model, "generation_config", None), "eos_token_id", None),
            getattr(getattr(model, "config", None), "eos_token_id", None),
            getattr(getattr(processor, "tokenizer", None), "eos_token_id", None),
        )
        finish_reason = _classify(generated, request.max_tokens, last_token_id, eos)
        audit["tokens"]["completion"] = generated
        audit["tokens"]["total"] = prompt_tokens + generated
        audit["generation"].update(
            {
                "generated_tokens": generated,
                "finish_reason": finish_reason,
                "last_token_id": last_token_id,
                "eos_token_ids": sorted(eos),
                "raw_characters": len(raw),
                "raw_sha256": _sha256_bytes(raw.encode("utf-8")),
            }
        )
        if finish_reason == "empty" or not raw.strip():
            raise XiaomiOcrBackendError("本地模型未返回可解码文本")
        return OcrBackendResult(
            text=raw, finish_reason=finish_reason, prompt_tokens=prompt_tokens,
            completion_tokens=generated, total_tokens=prompt_tokens + generated,
        )

    def _read_manifest(self, model_revision: str | None) -> None:
        if not self._manifest_path.is_file():
            raise XiaomiOcrBackendError(f"安装清单不存在: {self._manifest_path}")
        try:
            manifest_bytes = self._manifest_path.read_bytes()
            manifest = json.loads(manifest_bytes.decode("utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise XiaomiOcrBackendError(f"无法读取安装清单: {safe_error_summary(exc)}") from exc
        if not isinstance(manifest, Mapping):
            raise XiaomiOcrBackendError("安装清单必须是 JSON 对象")
        revision = manifest.get("model_revision")
        if manifest.get("model") != EXPECTED_MODEL:
            raise XiaomiOcrBackendError(f"安装清单 model={manifest.get('model')!r} 与固定值不一致")
        if (
            not isinstance(revision, str)
            or revision != EXPECTED_MODEL_REVISION
            or _PINNED_REVISION_RE.fullmatch(revision) is None
        ):
            raise XiaomiOcrBackendError(f"安装清单 model_revision={revision!r} 不是固定 revision")
        if model_revision is not None and (
            not isinstance(model_revision, str) or model_revision.strip() != revision
        ):
            raise XiaomiOcrValidationError(
                f"请求的 model_revision={model_revision!r} 与安装清单 {revision!r} 不一致"
            )
        if manifest.get("github_commit") != EXPECTED_GITHUB_COMMIT:
            raise XiaomiOcrBackendError("安装清单 github_commit 与固定 commit 不一致")
        files = manifest.get("files")
        if not isinstance(files, list):
            raise XiaomiOcrBackendError("安装清单 files 必须是数组")
        entries: dict[str, dict[str, Any]] = {}
        for index, raw_entry in enumerate(files):
            if not isinstance(raw_entry, Mapping):
                raise XiaomiOcrBackendError(f"安装清单 files[{index}] 必须是 JSON 对象")
            name = _manifest_name(raw_entry.get("name"))
            if name in entries:
                raise XiaomiOcrBackendError(f"安装清单文件名重复: {name!r}")
            digest = raw_entry.get("sha256")
            if not isinstance(digest, str) or _SHA256_RE.fullmatch(digest.strip().lower()) is None:
                raise XiaomiOcrBackendError(f"安装清单 {name} 的 sha256 不是 64 位十六进制")
            size = raw_entry.get("bytes")
            if isinstance(size, bool) or not isinstance(size, int) or size < 0:
                raise XiaomiOcrBackendError(f"安装清单 {name} 的 bytes 必须是非负整数")
            entries[name] = {"sha256": digest.strip().lower(), "bytes": size}
        self._model_entries = {
            n[len("model/") :]: e for n, e in entries.items() if n.startswith("model/")
        }
        missing_model = [n for n in REQUIRED_MODEL_FILES if n not in self._model_entries]
        if missing_model:
            raise XiaomiOcrBackendError("安装清单缺少模型文件: " + ", ".join(missing_model))
        self._repo_entries = {
            n[len("repo/") :]: e
            for n, e in entries.items()
            if n.startswith("repo/")
            and n[len("repo/") :].startswith(("evaluation_pipeline/", "postprocess/"))
            and n.endswith(".py")
        }
        missing_repo = [n for n in REQUIRED_REPO_FILES if n not in self._repo_entries]
        if missing_repo:
            raise XiaomiOcrBackendError("安装清单缺少官方代码文件: " + ", ".join(missing_repo))
        if not self._model_root.is_dir():
            raise XiaomiOcrBackendError(f"模型目录不存在: {self._model_root}")
        if not self._repo_root.is_dir():
            raise XiaomiOcrBackendError(f"官方仓库目录不存在: {self._repo_root}")
        self._model_name = str(manifest["model"])
        self._model_revision = revision
        self._github_commit = str(manifest["github_commit"])
        self._manifest_sha256 = _sha256_bytes(manifest_bytes)
        try:
            self._manifest_fingerprint = _fingerprint(self._manifest_path)
        except OSError as exc:
            raise XiaomiOcrBackendError(f"无法读取安装清单状态: {safe_error_summary(exc)}") from exc

    def _actual_files(self, root: Path, label: str) -> dict[str, Path]:
        if not root.is_dir():
            raise XiaomiOcrBackendError(f"{label} 目录不存在: {root}")
        result: dict[str, Path] = {}
        for path in root.rglob("*.py" if label == "repo" else "*"):
            if path.is_symlink():
                raise XiaomiOcrBackendError(f"{label} 目录中不允许符号链接: {path}")
            if not path.is_file():
                continue
            relative = path.relative_to(root).as_posix()
            if label == "repo" and not relative.startswith(
                ("evaluation_pipeline/", "postprocess/")
            ):
                continue
            result[relative] = path
        return result

    def _verify_tree(
        self,
        root: Path,
        entries: Mapping[str, Mapping[str, Any]],
        label: str,
    ) -> tuple[dict[str, str], dict[Path, tuple]]:
        actual = self._actual_files(root, label)
        if set(actual) != set(entries):
            raise XiaomiOcrBackendError(
                f"{label} 与安装清单不一致: missing={sorted(set(entries) - set(actual))}; "
                f"unexpected={sorted(set(actual) - set(entries))}"
            )
        root_resolved = root.resolve()
        hashes: dict[str, str] = {}
        fingerprints: dict[Path, tuple] = {}
        for relative in sorted(entries):
            expected = entries[relative]
            candidate = root.joinpath(*PurePosixPath(relative).parts)
            try:
                resolved = candidate.resolve(strict=True)
            except OSError as exc:
                raise XiaomiOcrBackendError(
                    f"{label}/{relative} 不存在或不可访问: {safe_error_summary(exc)}"
                ) from exc
            if not resolved.is_relative_to(root_resolved):
                raise XiaomiOcrBackendError(f"{label}/{relative} 解析后越出安装根目录")
            if candidate.is_symlink():
                raise XiaomiOcrBackendError(f"{label}/{relative} 不允许符号链接")
            if not resolved.is_file():
                raise XiaomiOcrBackendError(f"{label}/{relative} 不是普通文件")
            before = _fingerprint(candidate)
            if before[0] != int(expected["bytes"]):
                raise XiaomiOcrBackendError(
                    f"{label}/{relative} 大小不匹配: 清单 {int(expected['bytes'])}，"
                    f"实际 {before[0]}"
                )
            digest = _sha256_file(candidate)
            after = _fingerprint(candidate)
            if before != after:
                raise XiaomiOcrBackendError(f"{label}/{relative} 在校验过程中发生变化")
            if digest != str(expected["sha256"]):
                raise XiaomiOcrBackendError(
                    f"{label}/{relative} SHA-256 不匹配: 清单 {expected['sha256']}，"
                    f"实际 {digest}"
                )
            hashes[relative] = digest
            fingerprints[candidate] = after
        return hashes, fingerprints

    def _ensure_tree(self, label: str) -> None:
        with self._lock:
            verified = self._verified_model if label == "model" else self._verified_repo
            if verified is not None:
                self._check()
                return
            root, entries = (
                (self._model_root, self._model_entries)
                if label == "model"
                else (self._repo_root, self._repo_entries)
            )
            hashes, fingerprints = self._verify_tree(root, entries, label)
            if label == "model":
                self._verified_model = hashes
            else:
                self._verified_repo = hashes
            self._fingerprints.update(fingerprints)
            self._check()

    def _ensure_model(self) -> None:
        self._ensure_tree("model")

    def _ensure_repo(self) -> None:
        self._ensure_tree("repo")

    def _check(self) -> None:
        if self._manifest_fingerprint is None:
            raise XiaomiOcrBackendError("安装清单尚未读取")
        try:
            current_manifest = _fingerprint(self._manifest_path)
        except OSError as exc:
            raise XiaomiOcrBackendError(
                f"安装清单在首次校验后不可访问: {safe_error_summary(exc)}"
            ) from exc
        if current_manifest != self._manifest_fingerprint:
            raise XiaomiOcrBackendError("安装清单在首次校验后发生变化；拒绝继续使用")
        if self._verified_model is not None and set(
            self._actual_files(self._model_root, "model")
        ) != set(self._verified_model):
            raise XiaomiOcrBackendError("模型目录在首次校验后新增或删除了文件")
        if self._verified_repo is not None and set(
            self._actual_files(self._repo_root, "repo")
        ) != set(self._verified_repo):
            raise XiaomiOcrBackendError("官方仓库 Python 文件在首次校验后新增或删除了文件")
        for path, expected in self._fingerprints.items():
            try:
                current = _fingerprint(path)
            except OSError as exc:
                raise XiaomiOcrBackendError(
                    f"已验证文件在首次校验后不可访问: {path}: {safe_error_summary(exc)}"
                ) from exc
            if current != expected:
                raise XiaomiOcrBackendError(f"已验证文件在首次校验后发生变化: {path}")

    def _verified_module_file(self, module: Any, relative: str) -> None:
        file_value = getattr(module, "__file__", None)
        if not isinstance(file_value, str):
            raise XiaomiOcrBackendError(f"已导入的 {relative} 没有 __file__")
        try:
            resolved = Path(file_value).resolve(strict=True)
            actual_relative = resolved.relative_to(self._repo_root.resolve()).as_posix()
        except (OSError, ValueError) as exc:
            raise XiaomiOcrBackendError(
                f"已导入的 {relative} 来源不明: {safe_error_summary(exc)}"
            ) from exc
        if actual_relative != relative:
            raise XiaomiOcrBackendError(
                f"已导入模块来源不匹配: 期望 {relative}，实际 {actual_relative}"
            )
        expected = self._verified_repo.get(relative) if self._verified_repo is not None else None
        if expected is None or _sha256_file(resolved) != expected:
            raise XiaomiOcrBackendError(f"已导入模块与已验证哈希不一致: {relative}")

    def _ensure_official_modules(self) -> None:
        with self._lock:
            if self._fit_image is not None and self._finalize_document is not None:
                self._check()
                return
            self._ensure_repo()
            self._check()
            repo_text = str(self._repo_root)
            inserted = repo_text not in sys.path
            previous_dont_write = sys.dont_write_bytecode
            if inserted:
                sys.path.insert(0, repo_text)
            sys.dont_write_bytecode = True
            try:
                cropping = import_module("evaluation_pipeline.cropping")
                document = import_module("postprocess.document")
            finally:
                sys.dont_write_bytecode = previous_dont_write
                if inserted:
                    with suppress(ValueError):
                        sys.path.remove(repo_text)
            self._verified_module_file(cropping, "evaluation_pipeline/cropping.py")
            self._verified_module_file(document, "postprocess/document.py")
            fit_image = getattr(cropping, "fit_image", None)
            finalize_document = getattr(document, "finalize_document", None)
            if not callable(fit_image) or not callable(finalize_document):
                raise XiaomiOcrBackendError("官方模块缺少 fit_image 或 finalize_document")
            self._fit_image = fit_image
            self._finalize_document = finalize_document
            self._check()

    def _load_runtime(self) -> dict[str, Any]:
        try:
            torch = import_module("torch")
            transformers = import_module("transformers")
        except Exception as exc:
            raise XiaomiOcrBackendError(
                f"无法导入 Torch/Transformers: {safe_error_summary(exc)}"
            ) from exc
        auto_processor = getattr(transformers, "AutoProcessor", None)
        auto_model = getattr(transformers, "AutoModelForImageTextToText", None)
        if auto_processor is None or auto_model is None:
            raise XiaomiOcrBackendError("Transformers 缺少所需 Auto 类")
        cuda = self.device.startswith("cuda")
        if cuda and not bool(torch.cuda.is_available()):
            raise XiaomiOcrBackendError(f"请求设备 {self.device} 不可用；不进行隐式 CPU 回退")
        dtype = torch.float16 if cuda else torch.float32
        started = time.perf_counter()
        try:
            processor = auto_processor.from_pretrained(
                str(self._model_root), local_files_only=True, trust_remote_code=False
            )
            model = auto_model.from_pretrained(
                str(self._model_root),
                dtype=dtype,
                local_files_only=True,
                trust_remote_code=False,
            )
            model = model.to(self.device)
            model.eval()
        except Exception as exc:
            raise XiaomiOcrBackendError(
                f"本地模型加载失败: {safe_error_summary(exc)}"
            ) from exc
        load_seconds = time.perf_counter() - started
        device_name = "cpu"
        if cuda:
            try:
                device_name = str(torch.cuda.get_device_name(self.device))
            except Exception:
                device_name = self.device
        return {
            "torch": torch, "processor": processor, "model": model,
            "device": self.device, "dtype": str(dtype), "cuda": cuda,
            "model_class": type(model).__name__, "processor_class": type(processor).__name__,
            "torch_version": str(getattr(torch, "__version__", "unknown")),
            "transformers_version": str(getattr(transformers, "__version__", "unknown")),
            "device_name": device_name, "load_seconds": load_seconds,
        }

    def _ensure_loaded(self) -> tuple[dict[str, Any], bool]:
        with self._lock:
            if self._runtime is not None:
                self._check()
                return self._runtime, False
            self._check()
            self._ensure_model()
            self._ensure_repo()
            self._ensure_official_modules()
            self._check()
            runtime = self._load_runtime()
            self._check()
            self._runtime = runtime
            return runtime, True
