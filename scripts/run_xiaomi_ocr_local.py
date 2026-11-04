"""Standalone serial batch runner for the authorized Xiaomi-OCR-0 pilot.

This script is intentionally independent of ``campusguide.ingest.xiaomi_ocr``.
Run it with the isolated runtime interpreter that can see Torch/Transformers:

    D:/xiaomi-ocr/runtime/Scripts/python.exe scripts/run_xiaomi_ocr_local.py --input samples.jsonl --out out/xiaomi-pilot --install-root D:/xiaomi-ocr

The runner loads the local Safetensors checkpoint once, uses the official
``fit_image`` preprocessing and ``finalize_document`` postprocessing, and emits
raw plus normalized markdown with audit metadata. It does not judge answers,
invent confidence, call a cloud service, or register an MCP server.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import platform
import re
import sys
import time
import uuid
from collections.abc import Iterable, Mapping
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from importlib import metadata as importlib_metadata
from pathlib import Path
from typing import Any

SCHEMA_VERSION = "xiaomi_ocr_local/v1"
CACHE_SCHEMA_VERSION = "xiaomi_ocr_local_cache/v1"
RECORD_SCHEMA_VERSION = "xiaomi_ocr_local_record/v1"
PROGRESS_SCHEMA_VERSION = "xiaomi_ocr_local_progress/v1"
SUMMARY_SCHEMA_VERSION = "xiaomi_ocr_local_summary/v1"

DOCUMENT_PROMPT = (
    "Extract all information from the main body of the document image and represent it "
    "in markdown format, ignoring headers and footers. Tables should be expressed in OTSL "
    "format, formulas in the document should be represented using LATEX format, and the "
    "parsing should be organized according to the reading order."
)

DEFAULT_INSTALL_ROOT = "D:/xiaomi-ocr"
DEFAULT_MAX_NEW_TOKENS = 8192
MAX_NEW_TOKENS_LIMIT = 16384
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
REQUIRED_REPO_FILES = (
    "evaluation_pipeline/cropping.py",
    "postprocess/document.py",
)


class CacheIntegrityError(RuntimeError):
    """Raised when a resume cache entry does not match its sealed identity."""


@dataclass(frozen=True)
class SampleSpec:
    line_no: int
    sample_id: str
    image_path: Path
    source_sha256: str
    page_no: int
    image_sha256_expected: str | None


@dataclass
class PreparedSample:
    spec: SampleSpec
    input_image: dict[str, Any] | None = None
    cache_key: str | None = None
    identity: dict[str, Any] | None = None
    cached_payload: dict[str, Any] | None = None
    error: dict[str, Any] | None = None


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


def _new_run_id() -> str:
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    return f"{stamp}-{uuid.uuid4().hex[:8]}"


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sha256_file(path: Path, chunk_size: int = 8 * 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(chunk_size), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _digest_json(value: Any) -> str:
    normalized = json.loads(_canonical_json(value))
    return _sha256_bytes(_canonical_json(normalized).encode("utf-8"))


def _temp_sibling(path: Path) -> Path:
    return path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")


def _atomic_write_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = _temp_sibling(path)
    try:
        with temporary.open("wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            with suppress(OSError):
                temporary.unlink()


def _atomic_write_json(path: Path, payload: Mapping[str, Any]) -> None:
    data = (json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")
    _atomic_write_bytes(path, data)


def _append_progress(path: Path, line: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = (_canonical_json(line) + "\n").encode("utf-8")
    with path.open("ab") as handle:
        handle.write(encoded)
        handle.flush()
        os.fsync(handle.fileno())


def _error(stage: str, code: str, message: str, exc: BaseException | None = None) -> dict[str, Any]:
    return {
        "stage": stage,
        "code": code,
        "exception": type(exc).__name__ if exc is not None else None,
        "message": message,
    }


def _sha256_arg(value: str) -> str:
    if not isinstance(value, str) or re.fullmatch(r"[0-9a-fA-F]{64}", value) is None:
        raise ValueError("must be a 64-character hexadecimal SHA-256")
    return value.lower()


def _max_new_tokens_arg(raw: str) -> int:
    try:
        value = int(raw)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("--max-new-tokens must be an integer") from exc
    if not 1 <= value <= MAX_NEW_TOKENS_LIMIT:
        raise argparse.ArgumentTypeError(
            f"--max-new-tokens must be between 1 and {MAX_NEW_TOKENS_LIMIT}"
        )
    return value


def _positive_int_arg(raw: str) -> int:
    try:
        value = int(raw)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("--limit must be an integer") from exc
    if value < 1:
        raise argparse.ArgumentTypeError("--limit must be at least 1")
    return value


def load_sample_specs(input_path: Path) -> list[SampleSpec]:
    """Parse and validate the frozen JSONL sample list."""

    samples: list[SampleSpec] = []
    seen: dict[str, int] = {}
    with input_path.open("r", encoding="utf-8") as handle:
        for line_no, raw_line in enumerate(handle, 1):
            line = raw_line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"{input_path}:{line_no}: invalid JSON: {exc.msg}") from exc
            if not isinstance(record, Mapping):
                raise ValueError(f"{input_path}:{line_no}: sample must be a JSON object")

            missing = [
                key
                for key in ("sample_id", "image_path", "source_sha256", "page_no")
                if key not in record
            ]
            if missing:
                raise ValueError(f"{input_path}:{line_no}: missing field(s): {', '.join(missing)}")

            sample_id = record["sample_id"]
            if not isinstance(sample_id, str) or not sample_id.strip():
                raise ValueError(f"{input_path}:{line_no}: sample_id must be a non-empty string")
            if sample_id in seen:
                raise ValueError(
                    f"{input_path}:{line_no}: duplicate sample_id {sample_id!r} "
                    f"(first seen on line {seen[sample_id]})"
                )

            image_value = record["image_path"]
            if not isinstance(image_value, str) or not image_value.strip():
                raise ValueError(f"{input_path}:{line_no}: image_path must be a non-empty string")
            image_path = Path(image_value).expanduser()
            if not image_path.is_absolute():
                image_path = input_path.parent / image_path
            image_path = image_path.resolve()

            try:
                source_sha256 = _sha256_arg(record["source_sha256"])
            except ValueError as exc:
                raise ValueError(f"{input_path}:{line_no}: source_sha256 {exc}") from exc

            page_no = record["page_no"]
            if isinstance(page_no, bool) or not isinstance(page_no, int) or page_no <= 0:
                raise ValueError(f"{input_path}:{line_no}: page_no must be a positive integer")

            expected_image_sha = record.get("image_sha256")
            if expected_image_sha is not None:
                try:
                    expected_image_sha = _sha256_arg(expected_image_sha)
                except ValueError as exc:
                    raise ValueError(f"{input_path}:{line_no}: image_sha256 {exc}") from exc

            seen[sample_id] = line_no
            samples.append(
                SampleSpec(
                    line_no=line_no,
                    sample_id=sample_id,
                    image_path=image_path,
                    source_sha256=source_sha256,
                    page_no=page_no,
                    image_sha256_expected=expected_image_sha,
                )
            )

    if not samples:
        raise ValueError(f"{input_path}: no sample records")
    return samples


def _classify_finish(
    generated_tokens: int,
    max_new_tokens: int,
    last_token_id: int | None,
    eos_token_ids: Iterable[int],
) -> str:
    """Classify generation completion without pretending truncation succeeded."""

    if generated_tokens <= 0:
        return "empty"
    if last_token_id is not None and last_token_id in set(eos_token_ids):
        return "eos"
    if generated_tokens >= max_new_tokens:
        return "token_limit"
    return "stopped_other"


def _collect_eos_ids(*sources: Any) -> set[int]:
    result: set[int] = set()

    def visit(value: Any) -> None:
        if value is None or isinstance(value, bool):
            return
        if isinstance(value, int):
            result.add(value)
            return
        if isinstance(value, (list, tuple, set)):
            for item in value:
                visit(item)

    for source in sources:
        visit(source)
    return result


def _structural_repair(raw: str | None, normalized: str | None) -> dict[str, Any] | None:
    if raw is None or normalized is None:
        return None
    return {
        "applied": raw != normalized,
        "raw_characters": len(raw),
        "normalized_characters": len(normalized),
    }


def _pkg_version(name: str) -> str | None:
    try:
        return importlib_metadata.version(name)
    except importlib_metadata.PackageNotFoundError:
        return None


def _runtime_info(manifest: Mapping[str, Any] | None) -> dict[str, Any]:
    import torch

    backend = "cuda" if torch.cuda.is_available() else "cpu"
    dtype = "float16" if backend == "cuda" else "float32"
    runner_python = Path(sys.executable).resolve()
    manifest_python = None
    matches_manifest = None
    if manifest is not None and isinstance(manifest.get("runtime_python"), str):
        manifest_python = str(Path(manifest["runtime_python"]).expanduser().resolve())
        matches_manifest = runner_python == Path(manifest_python)

    return {
        "python": platform.python_version(),
        "platform": platform.platform(),
        "runner_python": str(runner_python),
        "manifest_runtime_python": manifest_python,
        "runtime_python_matches_manifest": matches_manifest,
        "torch": torch.__version__,
        "torch_cuda": torch.version.cuda,
        "transformers": _pkg_version("transformers"),
        "pillow": _pkg_version("Pillow"),
        "safetensors": _pkg_version("safetensors"),
        "torchvision": _pkg_version("torchvision"),
        "backend": backend,
        "dtype": dtype,
        "device_index": 0 if backend == "cuda" else None,
        "device_name": torch.cuda.get_device_name(0) if backend == "cuda" else "cpu",
    }


def _sha256_tree(root: Path) -> dict[str, str]:
    result: dict[str, str] = {}
    for path in sorted(root.rglob("*"), key=lambda item: item.relative_to(root).as_posix()):
        if path.is_file():
            result[path.relative_to(root).as_posix()] = _sha256_file(path)
    return result


def _repo_identity_files(repo_root: Path) -> dict[str, str]:
    paths: list[Path] = []
    for directory in ("evaluation_pipeline", "postprocess"):
        root = repo_root / directory
        if root.exists():
            paths.extend(sorted(root.rglob("*.py")))
    result: dict[str, str] = {}
    for path in paths:
        if path.is_file():
            result[path.relative_to(repo_root).as_posix()] = _sha256_file(path)
    return result


def _load_manifest(install_root: Path) -> dict[str, Any] | None:
    path = install_root / "installation_manifest.json"
    if not path.exists():
        return None
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"cannot read installation manifest {path}: {exc}") from exc
    if not isinstance(manifest, Mapping):
        raise RuntimeError(f"installation manifest is not a JSON object: {path}")
    return dict(manifest)


def _validate_manifest_hashes(
    manifest: Mapping[str, Any] | None,
    model_files: Mapping[str, str],
    repo_files: Mapping[str, str],
) -> None:
    if manifest is None:
        return
    entries = manifest.get("files")
    if not isinstance(entries, list):
        return
    expected: dict[str, str] = {}
    for entry in entries:
        if not isinstance(entry, Mapping):
            continue
        name = entry.get("name")
        digest = entry.get("sha256")
        if isinstance(name, str) and isinstance(digest, str):
            expected[name] = digest.lower()

    for relative, actual in model_files.items():
        wanted = expected.get(f"model/{relative}")
        if wanted is not None and wanted != actual:
            raise RuntimeError(f"model file hash changed since installation: {relative}")
    for relative, actual in repo_files.items():
        wanted = expected.get(f"repo/{relative}")
        if wanted is not None and wanted != actual:
            raise RuntimeError(f"official repository file hash changed since installation: {relative}")


def _build_static_identity(
    install_root: Path,
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    model_root = install_root.joinpath(*MODEL_DIRECTORY)
    repo_root = install_root / REPO_DIRECTORY
    if not model_root.is_dir():
        raise RuntimeError(f"model directory not found: {model_root}")
    if not repo_root.is_dir():
        raise RuntimeError(f"official repository not found: {repo_root}")

    missing_model = [name for name in REQUIRED_MODEL_FILES if not (model_root / name).is_file()]
    if missing_model:
        raise RuntimeError(f"model directory is incomplete: {', '.join(missing_model)}")
    missing_repo = [name for name in REQUIRED_REPO_FILES if not (repo_root / name).is_file()]
    if missing_repo:
        raise RuntimeError(f"official repository is incomplete: {', '.join(missing_repo)}")

    manifest = _load_manifest(install_root)
    model_files = _sha256_tree(model_root)
    repo_files = _repo_identity_files(repo_root)
    _validate_manifest_hashes(manifest, model_files, repo_files)

    model_identity = {
        "identifier": (
            manifest.get("model")
            if manifest is not None and isinstance(manifest.get("model"), str)
            else "SeerRay-Lab/Xiaomi-OCR-0"
        ),
        "path": str(model_root),
        "revision": manifest.get("model_revision") if manifest is not None else None,
        "repo_commit": manifest.get("github_commit") if manifest is not None else None,
        "repo_tree": manifest.get("github_tree") if manifest is not None else None,
        "files": model_files,
        "files_sha256": _digest_json(model_files),
    }

    preprocess_files = {
        name: digest
        for name, digest in repo_files.items()
        if name.startswith("evaluation_pipeline/")
    }
    postprocess_files = {
        name: digest for name, digest in repo_files.items() if name.startswith("postprocess/")
    }
    preprocess_identity = {
        "module": "evaluation_pipeline.cropping.fit_image",
        "repo_path": str(repo_root),
        "repo_commit": manifest.get("github_commit") if manifest is not None else None,
        "files": preprocess_files,
        "files_sha256": _digest_json(preprocess_files),
    }
    postprocess_identity = {
        "module": "postprocess.document.finalize_document",
        "repo_path": str(repo_root),
        "repo_commit": manifest.get("github_commit") if manifest is not None else None,
        "files": postprocess_files,
        "files_sha256": _digest_json(postprocess_files),
    }

    chat_template_path = model_root / "chat_template.jinja"
    prompt_identity = {
        "text": DOCUMENT_PROMPT,
        "sha256": _sha256_bytes(DOCUMENT_PROMPT.encode("utf-8")),
        "chat_template_sha256": _sha256_file(chat_template_path),
        "enable_thinking": False,
        "add_generation_prompt": True,
    }
    prompt_identity["identity_sha256"] = _digest_json(
        {
            "text_sha256": prompt_identity["sha256"],
            "chat_template_sha256": prompt_identity["chat_template_sha256"],
            "enable_thinking": prompt_identity["enable_thinking"],
            "add_generation_prompt": prompt_identity["add_generation_prompt"],
        }
    )

    installation = {
        "manifest_present": manifest is not None,
        "installed_at": manifest.get("installed_at") if manifest is not None else None,
        "environment": manifest.get("environment") if manifest is not None else None,
        "known_runtime_difference": (
            manifest.get("known_runtime_difference") if manifest is not None else None
        ),
    }
    static = {
        "model": model_identity,
        "prompt": prompt_identity,
        "preprocess": preprocess_identity,
        "postprocess": postprocess_identity,
        "installation": installation,
    }
    return static, manifest


def _build_cache_identity(
    static: Mapping[str, Any],
    runtime: Mapping[str, Any],
    image_sha256: str,
    max_new_tokens: int,
) -> tuple[dict[str, Any], str]:
    identity = {
        "schema": SCHEMA_VERSION,
        "model": {
            "identifier": static["model"]["identifier"],
            "files_sha256": static["model"]["files_sha256"],
            "revision": static["model"]["revision"],
            "repo_commit": static["model"]["repo_commit"],
        },
        "prompt_sha256": static["prompt"]["identity_sha256"],
        "preprocess_sha256": static["preprocess"]["files_sha256"],
        "postprocess_sha256": static["postprocess"]["files_sha256"],
        "image_sha256": image_sha256,
        "max_new_tokens": max_new_tokens,
        "runtime": {
            "python": runtime["python"],
            "torch": runtime["torch"],
            "transformers": runtime["transformers"],
            "pillow": runtime["pillow"],
            "safetensors": runtime["safetensors"],
            "backend": runtime["backend"],
            "dtype": runtime["dtype"],
            "device_name": runtime["device_name"],
        },
        "generation": {
            "do_sample": False,
            "enable_thinking": False,
            "add_generation_prompt": True,
        },
    }
    return identity, _digest_json(identity)


def _load_cache_payload(cache_path: Path, expected_key: str) -> dict[str, Any]:
    try:
        payload = json.loads(cache_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise CacheIntegrityError(f"cannot read cache entry {cache_path}: {exc}") from exc
    if not isinstance(payload, Mapping):
        raise CacheIntegrityError(f"cache entry is not a JSON object: {cache_path}")
    if payload.get("schema") != CACHE_SCHEMA_VERSION:
        raise CacheIntegrityError(f"unexpected cache schema: {cache_path}")
    if payload.get("cache_key") != expected_key:
        raise CacheIntegrityError(f"cache key mismatch: {cache_path}")
    identity = payload.get("identity")
    if not isinstance(identity, Mapping) or _digest_json(identity) != expected_key:
        raise CacheIntegrityError(f"cache identity hash mismatch: {cache_path}")
    claimed = payload.get("payload_sha256")
    body = {key: value for key, value in payload.items() if key != "payload_sha256"}
    if not isinstance(claimed, str) or claimed != _digest_json(body):
        raise CacheIntegrityError(f"cache payload hash mismatch: {cache_path}")
    result = payload.get("result")
    if not isinstance(result, Mapping):
        raise CacheIntegrityError(f"cache entry has no result object: {cache_path}")
    result_status = result.get("status")
    if result_status not in {"success", "error"}:
        raise CacheIntegrityError(f"cache entry has an invalid result status: {cache_path}")
    if not isinstance(result.get("raw_markdown"), str):
        raise CacheIntegrityError(f"cache entry has no raw markdown: {cache_path}")
    if result_status == "success" and not isinstance(result.get("normalized_markdown"), str):
        raise CacheIntegrityError(
            f"successful cache entry has no normalized markdown: {cache_path}"
        )
    return dict(payload)


def _load_official_modules(repo_root: Path) -> dict[str, Any]:
    repo_text = str(repo_root)
    if repo_text not in sys.path:
        sys.path.insert(0, repo_text)

    from evaluation_pipeline.cropping import fit_image
    from postprocess.document import finalize_document

    analyze_repetition = None
    repetition_error = None
    try:
        from postprocess.repetition_guard import analyze_repetition as repetition_analyzer

        analyze_repetition = repetition_analyzer
    except Exception as exc:
        repetition_error = f"{type(exc).__name__}: {exc}"
    return {
        "fit_image": fit_image,
        "finalize_document": finalize_document,
        "analyze_repetition": analyze_repetition,
        "repetition_error": repetition_error,
    }


def _load_model(
    model_root: Path,
    runtime: Mapping[str, Any],
) -> tuple[dict[str, Any], float]:
    import torch
    from transformers import AutoModelForImageTextToText, AutoProcessor

    started = time.perf_counter()
    processor = AutoProcessor.from_pretrained(
        str(model_root),
        local_files_only=True,
        trust_remote_code=False,
    )
    dtype = torch.float16 if runtime["backend"] == "cuda" else torch.float32
    device_name = (
        f"cuda:{runtime['device_index']}" if runtime["backend"] == "cuda" else "cpu"
    )
    device = torch.device(device_name)
    model = AutoModelForImageTextToText.from_pretrained(
        str(model_root),
        dtype=dtype,
        local_files_only=True,
        trust_remote_code=False,
    )
    model = model.to(device).eval()
    elapsed = time.perf_counter() - started

    try:
        parameter_dtype = str(next(model.parameters()).dtype)
    except (StopIteration, AttributeError):
        parameter_dtype = None
    config = getattr(model, "config", None)
    attention_implementation = None
    if config is not None:
        attention_implementation = getattr(config, "_attn_implementation", None)
        if attention_implementation is None:
            attention_implementation = getattr(config, "_attn_implementation_internal", None)
        if attention_implementation is not None:
            attention_implementation = str(attention_implementation)
    image_processor = getattr(processor, "image_processor", None)
    return {
        "torch": torch,
        "model": model,
        "processor": processor,
        "device": device,
        "model_class": type(model).__name__,
        "processor_class": type(processor).__name__,
        "processor_module": type(processor).__module__,
        "image_processor_class": (
            type(image_processor).__name__ if image_processor is not None else None
        ),
        "image_processor_module": (
            type(image_processor).__module__ if image_processor is not None else None
        ),
        "attention_implementation": attention_implementation,
        "parameter_dtype": parameter_dtype,
        "model_load_seconds": elapsed,
        "warmup_done": False,
    }, elapsed


def _prepare_image(
    image_path: Path,
    image_sha256: str,
    out_dir: Path,
    fit_image: Any,
) -> tuple[dict[str, Any], dict[str, Any], float]:
    from PIL import Image

    started = time.perf_counter()
    raw_bytes = image_path.read_bytes()
    if _sha256_bytes(raw_bytes) != image_sha256:
        raise RuntimeError("input image bytes changed after the pre-scan hash was recorded")
    with Image.open(io.BytesIO(raw_bytes)) as source:
        source.load()
        raw_format = source.format
        raw_mode = source.mode
        raw_width, raw_height = source.size
        image = source.convert("RGB")

    fitted = fit_image(image)
    if fitted is not None:
        if not isinstance(fitted, Image.Image):
            raise TypeError(
                "evaluation_pipeline.cropping.fit_image returned "
                f"{type(fitted).__name__}, expected PIL.Image or None"
            )
        image = fitted

    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    prepared_bytes = buffer.getvalue()
    prepared_sha256 = _sha256_bytes(prepared_bytes)
    prepared_relative = f"prepared/{prepared_sha256}.png"
    prepared_path = out_dir / prepared_relative
    if prepared_path.exists():
        if _sha256_file(prepared_path) != prepared_sha256:
            raise RuntimeError(f"prepared image content-address mismatch: {prepared_path}")
    else:
        _atomic_write_bytes(prepared_path, prepared_bytes)

    input_info = {
        "sha256": image_sha256,
        "width": raw_width,
        "height": raw_height,
        "mode": raw_mode,
        "format": raw_format,
        "bytes": len(raw_bytes),
    }
    prepared_info = {
        "sha256": prepared_sha256,
        "width": image.width,
        "height": image.height,
        "mode": image.mode,
        "format": "PNG",
        "bytes": len(prepared_bytes),
        "path": prepared_relative,
    }
    return input_info, prepared_info, time.perf_counter() - started


def _cached_artifact_problem(
    cached_result: Mapping[str, Any],
    input_info: Mapping[str, Any],
    prepared_info: Mapping[str, Any],
) -> str | None:
    cached_input = cached_result.get("input_image")
    cached_prepared = cached_result.get("prepared_image")
    if not isinstance(cached_input, Mapping) or not isinstance(cached_prepared, Mapping):
        return "cache entry is missing image metadata"
    if cached_input.get("sha256") != input_info["sha256"]:
        return "cached input image SHA-256 does not match the current file"
    if cached_prepared.get("sha256") != prepared_info["sha256"]:
        return "cached prepared image SHA-256 does not match official preprocessing"
    for field in ("width", "height"):
        if cached_prepared.get(field) != prepared_info[field]:
            return f"cached prepared image {field} does not match official preprocessing"
    if cached_prepared.get("path") != prepared_info["path"]:
        return "cached prepared image path does not match its content-addressed path"
    return None


def _base_result(
    static: Mapping[str, Any],
    runtime: Mapping[str, Any],
    model_context: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    model_record = dict(static["model"])
    model_record.update(
        {
            "class": model_context["model_class"] if model_context is not None else None,
            "processor_class": (
                model_context["processor_class"] if model_context is not None else None
            ),
            "processor_module": (
                model_context["processor_module"] if model_context is not None else None
            ),
            "image_processor_class": (
                model_context["image_processor_class"] if model_context is not None else None
            ),
            "image_processor_module": (
                model_context["image_processor_module"] if model_context is not None else None
            ),
            "attention_implementation": (
                model_context["attention_implementation"]
                if model_context is not None
                else None
            ),
            "backend": runtime["backend"],
            "dtype": runtime["dtype"],
            "parameter_dtype": (
                model_context["parameter_dtype"] if model_context is not None else None
            ),
            "device_name": runtime["device_name"],
        }
    )
    return {
        "status": "error",
        "error": None,
        "model": model_record,
        "runtime": dict(runtime),
        "prompt": dict(static["prompt"]),
        "preprocess": dict(static["preprocess"]),
        "postprocess": dict(static["postprocess"]),
        "installation": dict(static["installation"]),
        "input_image": None,
        "prepared_image": None,
        "generation": None,
        "timings": {},
        "gpu_peak_allocated_bytes": None,
        "raw_markdown": None,
        "normalized_markdown": None,
        "structural_repair": None,
        "repetition": None,
        "repetition_error": None,
    }


def _analyze_repetition(
    raw: str,
    finish_reason: str,
    analyze_repetition: Any,
    module_error: str | None,
) -> tuple[Any, dict[str, Any] | None]:
    if module_error is not None:
        return None, _error("repetition", "module_unavailable", module_error)
    if analyze_repetition is None or not raw.strip():
        return None, None
    official_finish = "length" if finish_reason == "token_limit" else "stop"
    try:
        analysis = analyze_repetition(raw, finish_reason=official_finish)
        json.dumps(analysis, ensure_ascii=False)
    except Exception as exc:
        return None, _error(
            "repetition",
            "analysis_failed",
            f"{type(exc).__name__}: {exc}",
            exc,
        )
    return analysis, None


def _postprocess(
    raw: str,
    finalize_document: Any,
) -> tuple[str | None, dict[str, Any] | None, float]:
    started = time.perf_counter()
    try:
        normalized = finalize_document(raw)
        if not isinstance(normalized, str):
            raise TypeError(
                "postprocess.document.finalize_document returned "
                f"{type(normalized).__name__}, expected str"
            )
        if not normalized.strip():
            raise ValueError("finalize_document returned empty markdown for non-empty raw output")
    except Exception as exc:
        return None, _error("postprocess", "finalize_failed", str(exc), exc), (
            time.perf_counter() - started
        )
    return normalized, None, time.perf_counter() - started


def _infer_one(
    state: PreparedSample,
    static: Mapping[str, Any],
    runtime: Mapping[str, Any],
    model_context: Mapping[str, Any] | None,
    official: Mapping[str, Any],
    out_dir: Path,
    image_sha256: str,
    max_new_tokens: int,
) -> tuple[dict[str, Any], bool]:
    total_started = time.perf_counter()
    base = _base_result(static, runtime, model_context)

    try:
        input_info, prepared_info, preprocess_seconds = _prepare_image(
            state.spec.image_path,
            image_sha256,
            out_dir,
            official["fit_image"],
        )
    except Exception as exc:
        result = dict(base)
        result["error"] = _error("preprocess", "prepare_failed", str(exc), exc)
        result["input_image"] = state.input_image
        result["timings"] = {"elapsed_seconds": time.perf_counter() - total_started}
        return result, False

    base["input_image"] = input_info
    base["prepared_image"] = prepared_info

    if state.cached_payload is not None:
        cached_result = state.cached_payload["result"]
        problem = _cached_artifact_problem(cached_result, input_info, prepared_info)
        if problem is not None:
            result = dict(base)
            result["error"] = _error("cache", "artifact_mismatch", problem)
            result["timings"] = {"elapsed_seconds": time.perf_counter() - total_started}
            return result, False
        result = dict(cached_result)
        result["input_image"] = input_info
        result["prepared_image"] = prepared_info
        return result, True

    if model_context is None:
        raise RuntimeError("model context is required for inference")
    torch = model_context["torch"]
    model = model_context["model"]
    processor = model_context["processor"]
    device = model_context["device"]
    prepared_path = out_dir / prepared_info["path"]
    messages = [
        {
            "role": "user",
            "content": [
                {"type": "image", "url": str(prepared_path)},
                {"type": "text", "text": DOCUMENT_PROMPT},
            ],
        }
    ]

    try:
        prompt_started = time.perf_counter()
        inputs = processor.apply_chat_template(
            messages,
            tokenize=True,
            add_generation_prompt=True,
            return_dict=True,
            return_tensors="pt",
            enable_thinking=False,
        )
        prompt_encode_seconds = time.perf_counter() - prompt_started
        input_ids = inputs["input_ids"]
        prompt_tokens = int(input_ids.shape[1])
        input_dtypes = {
            key: str(value.dtype)
            for key, value in inputs.items()
            if hasattr(value, "dtype")
        }
        inputs = inputs.to(device)
    except Exception as exc:
        result = dict(base)
        result["error"] = _error("prompt", "chat_template_failed", str(exc), exc)
        result["timings"] = {"elapsed_seconds": time.perf_counter() - total_started}
        return result, False

    generation_started = time.perf_counter()
    try:
        if runtime["backend"] == "cuda":
            torch.cuda.synchronize(device)
            torch.cuda.reset_peak_memory_stats(device)
        with torch.inference_mode():
            output = model.generate(
                **inputs,
                max_new_tokens=max_new_tokens,
                do_sample=False,
            )
        if runtime["backend"] == "cuda":
            torch.cuda.synchronize(device)
        generation_seconds = time.perf_counter() - generation_started
        new_tokens = output[:, prompt_tokens:]
        generated_tokens = int(new_tokens.shape[1])
        last_token_id = (
            int(new_tokens[0, -1].item()) if generated_tokens > 0 else None
        )
        raw = processor.batch_decode(new_tokens, skip_special_tokens=True)[0]
        gpu_peak = (
            int(torch.cuda.max_memory_allocated(device))
            if runtime["backend"] == "cuda"
            else None
        )
    except Exception as exc:
        result = dict(base)
        result["error"] = _error("generation", "generation_failed", str(exc), exc)
        result["timings"] = {"elapsed_seconds": time.perf_counter() - total_started}
        return result, False

    eos_token_ids = _collect_eos_ids(
        getattr(getattr(model, "generation_config", None), "eos_token_id", None),
        getattr(getattr(model, "config", None), "eos_token_id", None),
        getattr(getattr(processor, "tokenizer", None), "eos_token_id", None),
    )
    finish_reason = _classify_finish(
        generated_tokens,
        max_new_tokens,
        last_token_id,
        eos_token_ids,
    )
    is_warmup = not bool(model_context["warmup_done"])
    model_context["warmup_done"] = True

    status = "success"
    error = None
    normalized = None
    postprocess_seconds = 0.0
    if finish_reason == "token_limit":
        status = "error"
        error = _error(
            "generation",
            "token_limit",
            f"generation reached --max-new-tokens={max_new_tokens} without an EOS token",
        )
    elif finish_reason == "empty" or not raw.strip():
        status = "error"
        error = _error("generation", "empty_output", "model returned an empty response")
    elif finish_reason == "stopped_other":
        status = "error"
        error = _error(
            "generation",
            "stopped_without_eos",
            "generation stopped below --max-new-tokens without an EOS token",
        )
    else:
        normalized, error, postprocess_seconds = _postprocess(
            raw,
            official["finalize_document"],
        )
        if error is not None:
            status = "error"

    repetition, repetition_error = _analyze_repetition(
        raw,
        finish_reason,
        official["analyze_repetition"],
        official["repetition_error"],
    )
    elapsed = time.perf_counter() - total_started
    result = dict(base)
    result.update(
        {
            "status": status,
            "error": error,
            "generation": {
                "prompt_tokens": prompt_tokens,
                "generated_tokens": generated_tokens,
                "max_new_tokens": max_new_tokens,
                "finish_reason": finish_reason,
                "last_token_id": last_token_id,
                "eos_token_ids": sorted(eos_token_ids),
                "do_sample": False,
                "enable_thinking": False,
                "input_dtypes": input_dtypes,
            },
            "timings": {
                "preprocess_seconds": preprocess_seconds,
                "prompt_encode_seconds": prompt_encode_seconds,
                "generation_seconds": generation_seconds,
                "postprocess_seconds": postprocess_seconds,
                "elapsed_seconds": elapsed,
                "model_load_seconds": model_context["model_load_seconds"],
                "warmup": is_warmup,
                "warmup_seconds": elapsed if is_warmup else None,
            },
            "gpu_peak_allocated_bytes": gpu_peak,
            "raw_markdown": raw,
            "normalized_markdown": normalized,
            "structural_repair": _structural_repair(raw, normalized),
            "repetition": repetition,
            "repetition_error": repetition_error,
        }
    )
    return result, False


def _build_cache_payload(
    result: Mapping[str, Any],
    identity: Mapping[str, Any],
    cache_key: str,
    run_id: str,
) -> dict[str, Any]:
    payload = {
        "schema": CACHE_SCHEMA_VERSION,
        "cache_key": cache_key,
        "created_at": _utc_now(),
        "producer_run_id": run_id,
        "identity": dict(identity),
        "result": dict(result),
    }
    payload["payload_sha256"] = _digest_json(payload)
    return payload


def _safe_sample_component(sample_id: str) -> str:
    readable = re.sub(r"[^A-Za-z0-9._-]+", "_", sample_id)[:60].strip("._-")
    if not readable:
        readable = "sample"
    return f"{readable}-{_sha256_bytes(sample_id.encode('utf-8'))[:8]}"


def _record_path(
    records_dir: Path,
    state: PreparedSample,
) -> Path:
    suffix = state.cache_key[:16] if state.cache_key is not None else "no-cache"
    name = f"{state.spec.line_no:04d}--{_safe_sample_component(state.spec.sample_id)}--{suffix}.json"
    return records_dir / name


def _relative_to_out(path: Path, out_dir: Path) -> str:
    try:
        return path.relative_to(out_dir).as_posix()
    except ValueError:
        return str(path)


def _prescan(
    samples: list[SampleSpec],
    out_dir: Path,
    static: Mapping[str, Any],
    runtime: Mapping[str, Any],
    max_new_tokens: int,
    resume: bool,
) -> list[PreparedSample]:
    states: list[PreparedSample] = []
    for spec in samples:
        state = PreparedSample(spec=spec)
        if not spec.image_path.is_file():
            state.error = _error("input", "image_missing", f"image not found: {spec.image_path}")
            states.append(state)
            continue
        try:
            actual_sha256 = _sha256_file(spec.image_path)
        except OSError as exc:
            state.error = _error("input", "image_read_failed", str(exc), exc)
            states.append(state)
            continue
        state.input_image = {"sha256": actual_sha256}
        identity, cache_key = _build_cache_identity(
            static,
            runtime,
            actual_sha256,
            max_new_tokens,
        )
        state.identity = identity
        state.cache_key = cache_key

        if (
            spec.image_sha256_expected is not None
            and spec.image_sha256_expected != actual_sha256
        ):
            state.error = _error(
                "input",
                "image_sha256_mismatch",
                "image_sha256 in JSONL does not match the actual image file",
            )
            states.append(state)
            continue

        if resume:
            cache_path = out_dir / "cache" / f"{cache_key}.json"
            if cache_path.exists():
                try:
                    state.cached_payload = _load_cache_payload(cache_path, cache_key)
                except CacheIntegrityError as exc:
                    state.error = _error("cache", "cache_integrity_error", str(exc), exc)
        states.append(state)
    return states


def _process_state(
    state: PreparedSample,
    static: Mapping[str, Any],
    runtime: Mapping[str, Any],
    official: Mapping[str, Any],
    model_context: Mapping[str, Any] | None,
    out_dir: Path,
    max_new_tokens: int,
) -> tuple[dict[str, Any], bool]:
    started = time.perf_counter()
    if state.error is not None:
        result = _base_result(static, runtime, model_context)
        result["error"] = state.error
        result["input_image"] = state.input_image
        result["timings"] = {"elapsed_seconds": time.perf_counter() - started}
        return result, False

    if state.cache_key is None or state.identity is None:
        result = _base_result(static, runtime, model_context)
        result["error"] = _error("internal", "missing_identity", "cache identity was not computed")
        result["timings"] = {"elapsed_seconds": time.perf_counter() - started}
        return result, False

    if state.cached_payload is None and model_context is None:
        result = _base_result(static, runtime, model_context)
        result["error"] = _error("internal", "model_not_loaded", "model context is unavailable")
        result["timings"] = {"elapsed_seconds": time.perf_counter() - started}
        return result, False

    if state.input_image is None or not isinstance(state.input_image.get("sha256"), str):
        result = _base_result(static, runtime, model_context)
        result["error"] = _error("internal", "missing_image_hash", "input image hash is unavailable")
        result["timings"] = {"elapsed_seconds": time.perf_counter() - started}
        return result, False

    result, cache_hit = _infer_one(
        state,
        static,
        runtime,
        model_context,
        official,
        out_dir,
        state.input_image["sha256"],
        max_new_tokens,
    )
    return result, cache_hit


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Run the authorized Xiaomi-OCR-0 pilot serially on a local JSONL "
            "sample list and emit raw plus normalized markdown."
        )
    )
    parser.add_argument("--input", required=True, type=Path, help="frozen JSONL sample list")
    parser.add_argument("--out", required=True, type=Path, help="output directory")
    parser.add_argument(
        "--install-root",
        default=DEFAULT_INSTALL_ROOT,
        type=Path,
        help=f"isolated installation root (default: {DEFAULT_INSTALL_ROOT})",
    )
    parser.add_argument(
        "--max-new-tokens",
        default=DEFAULT_MAX_NEW_TOKENS,
        type=_max_new_tokens_arg,
        help=(
            f"maximum generated tokens per page (default: {DEFAULT_MAX_NEW_TOKENS}; "
            f"maximum: {MAX_NEW_TOKENS_LIMIT})"
        ),
    )
    parser.add_argument(
        "--resume",
        action="store_true",
        help="reuse only cache entries whose sealed identity and payload hash match exactly",
    )
    parser.add_argument(
        "--limit",
        default=None,
        type=_positive_int_arg,
        help="process at most this many leading JSONL sample records",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    run_id = _new_run_id()
    started_at = _utc_now()
    started = time.perf_counter()
    input_path = args.input.expanduser().resolve()
    out_dir = args.out.expanduser().resolve()
    install_root = args.install_root.expanduser().resolve()
    run_dir = out_dir / "runs" / run_id
    records_dir = run_dir / "records"
    progress_path = out_dir / "progress.jsonl"
    summary: dict[str, Any] = {
        "schema": SUMMARY_SCHEMA_VERSION,
        "run_id": run_id,
        "started_at": started_at,
        "input": str(input_path),
        "out": str(out_dir),
        "install_root": str(install_root),
        "max_new_tokens": args.max_new_tokens,
        "resume": bool(args.resume),
        "limit": args.limit,
        "counts": {},
        "samples": [],
        "fatal_error": None,
    }
    count_keys = (
        "selected",
        "success",
        "error",
        "cache_hits",
        "cache_hit_errors",
        "inference_attempts",
        "cache_integrity_errors",
        "cache_write_errors",
    )
    counts = dict.fromkeys(count_keys, 0)
    error_codes: dict[str, int] = {}

    try:
        if not input_path.is_file():
            raise FileNotFoundError(f"input JSONL not found: {input_path}")
        if not install_root.is_dir():
            raise FileNotFoundError(f"install root not found: {install_root}")
        out_dir.mkdir(parents=True, exist_ok=True)
        run_dir.mkdir(parents=True, exist_ok=True)
        records_dir.mkdir(parents=True, exist_ok=True)

        samples = load_sample_specs(input_path)
        if args.limit is not None:
            samples = samples[: args.limit]
        counts["selected"] = len(samples)

        static, manifest = _build_static_identity(install_root)
        runtime = _runtime_info(manifest)
        summary["model_identity"] = {
            "identifier": static["model"]["identifier"],
            "revision": static["model"]["revision"],
            "repo_commit": static["model"]["repo_commit"],
            "files_sha256": static["model"]["files_sha256"],
        }
        summary["runtime"] = runtime
        summary["preprocess_sha256"] = static["preprocess"]["files_sha256"]
        summary["postprocess_sha256"] = static["postprocess"]["files_sha256"]
        summary["prompt_identity_sha256"] = static["prompt"]["identity_sha256"]

        if (
            runtime["runtime_python_matches_manifest"] is False
            and runtime["manifest_runtime_python"] is not None
        ):
            print(
                "warning: runner interpreter differs from installation_manifest runtime_python",
                file=sys.stderr,
                flush=True,
            )

        states = _prescan(
            samples,
            out_dir,
            static,
            runtime,
            args.max_new_tokens,
            bool(args.resume),
        )
        official = (
            _load_official_modules(Path(static["preprocess"]["repo_path"]))
            if any(state.error is None for state in states)
            else {}
        )
        needs_model = any(
            state.error is None and state.cached_payload is None for state in states
        )
        model_context = None
        model_load_seconds = None
        if needs_model:
            model_context, model_load_seconds = _load_model(
                Path(static["model"]["path"]),
                runtime,
            )
        summary["model_loaded"] = model_context is not None
        summary["model_load_seconds"] = model_load_seconds
        summary["model_backend"] = {
            "class": model_context["model_class"] if model_context is not None else None,
            "processor_class": (
                model_context["processor_class"] if model_context is not None else None
            ),
            "processor_module": (
                model_context["processor_module"] if model_context is not None else None
            ),
            "image_processor_class": (
                model_context["image_processor_class"] if model_context is not None else None
            ),
            "image_processor_module": (
                model_context["image_processor_module"] if model_context is not None else None
            ),
            "attention_implementation": (
                model_context["attention_implementation"]
                if model_context is not None
                else None
            ),
            "parameter_dtype": (
                model_context["parameter_dtype"] if model_context is not None else None
            ),
            "torchvision": runtime["torchvision"],
        }

        print(
            f"run_id={run_id} selected={len(states)} "
            f"resume={bool(args.resume)} model_loaded={model_context is not None}",
            flush=True,
        )

        for index, state in enumerate(states, 1):
            sample_started = time.perf_counter()
            try:
                result, cache_hit = _process_state(
                    state,
                    static,
                    runtime,
                    official,
                    model_context,
                    out_dir,
                    args.max_new_tokens,
                )
            except Exception as exc:
                result = _base_result(static, runtime, model_context)
                result["error"] = _error("internal", "sample_processing_failed", str(exc), exc)
                result["timings"] = {"elapsed_seconds": time.perf_counter() - sample_started}
                cache_hit = False

            cache_info: dict[str, Any] | None = None
            if state.cache_key is not None:
                cache_target = out_dir / "cache" / f"{state.cache_key}.json"
                if cache_hit and state.cached_payload is not None:
                    cache_info = {
                        "key": state.cache_key,
                        "hit": True,
                        "path": _relative_to_out(cache_target, out_dir),
                        "payload_sha256": state.cached_payload.get("payload_sha256"),
                        "producer_run_id": state.cached_payload.get("producer_run_id"),
                        "producer_created_at": state.cached_payload.get("created_at"),
                        "write_error": None,
                    }
                elif result.get("raw_markdown") is not None:
                    payload = _build_cache_payload(
                        result,
                        state.identity or {},
                        state.cache_key,
                        run_id,
                    )
                    cache_write_error = None
                    try:
                        _atomic_write_json(cache_target, payload)
                    except Exception as exc:
                        cache_write_error = _error(
                            "cache",
                            "cache_write_failed",
                            str(exc),
                            exc,
                        )
                        counts["cache_write_errors"] += 1
                    cache_info = {
                        "key": state.cache_key,
                        "hit": False,
                        "path": _relative_to_out(cache_target, out_dir),
                        "payload_sha256": payload["payload_sha256"],
                        "producer_run_id": run_id,
                        "producer_created_at": payload["created_at"],
                        "write_error": cache_write_error,
                    }
                else:
                    source = state.cached_payload
                    cache_info = {
                        "key": state.cache_key,
                        "hit": False,
                        "path": (
                            _relative_to_out(cache_target, out_dir)
                            if cache_target.exists()
                            else None
                        ),
                        "payload_sha256": (
                            source.get("payload_sha256") if source is not None else None
                        ),
                        "producer_run_id": (
                            source.get("producer_run_id") if source is not None else None
                        ),
                        "producer_created_at": (
                            source.get("created_at") if source is not None else None
                        ),
                        "write_error": None,
                    }

            record: dict[str, Any] = {
                "schema": RECORD_SCHEMA_VERSION,
                "run_id": run_id,
                "recorded_at": _utc_now(),
                "cache_hit": cache_hit,
                "cache_key": state.cache_key,
                "identity": state.identity,
                "cache": cache_info,
                "sample": {
                    "sample_id": state.spec.sample_id,
                    "page_no": state.spec.page_no,
                    "source_sha256": state.spec.source_sha256,
                    "image_path": str(state.spec.image_path),
                    "image_sha256_expected": state.spec.image_sha256_expected,
                    "input_line_no": state.spec.line_no,
                },
                "run_processing_seconds": time.perf_counter() - sample_started,
            }
            record.update(result)
            record["record_sha256"] = _digest_json(record)
            record_path = _record_path(records_dir, state)
            _atomic_write_json(record_path, record)

            status = str(record.get("status", "error"))
            error = record.get("error")
            error_stage = error.get("stage") if isinstance(error, Mapping) else None
            error_code = error.get("code") if isinstance(error, Mapping) else None
            if status == "success":
                counts["success"] += 1
            else:
                counts["error"] += 1
                code_key = f"{error_stage or 'unknown'}:{error_code}"
                error_codes[code_key] = error_codes.get(code_key, 0) + 1
            if cache_hit:
                counts["cache_hits"] += 1
                if status != "success":
                    counts["cache_hit_errors"] += 1
            elif record.get("generation") is not None or (
                isinstance(error, Mapping)
                and error.get("stage") == "generation"
                and error.get("code") == "generation_failed"
            ):
                counts["inference_attempts"] += 1
            if (
                isinstance(error, Mapping)
                and error.get("stage") == "cache"
                and error.get("code") in {"cache_integrity_error", "artifact_mismatch"}
            ):
                counts["cache_integrity_errors"] += 1

            relative_record = _relative_to_out(record_path, out_dir)
            progress_line = {
                "schema": PROGRESS_SCHEMA_VERSION,
                "run_id": run_id,
                "recorded_at": _utc_now(),
                "sample_id": state.spec.sample_id,
                "page_no": state.spec.page_no,
                "cache_key": state.cache_key,
                "cache_hit": cache_hit,
                "status": status,
                "error_code": error_code,
                "record": relative_record,
                "elapsed_seconds": time.perf_counter() - sample_started,
            }
            _append_progress(progress_path, progress_line)
            summary["samples"].append(
                {
                    "sample_id": state.spec.sample_id,
                    "page_no": state.spec.page_no,
                    "status": status,
                    "cache_hit": cache_hit,
                    "error_code": error_code,
                    "record": relative_record,
                }
            )
            print(
                f"[{index}/{len(states)}] {state.spec.sample_id} "
                f"status={status} cache_hit={cache_hit}"
                + (f" error={error_code}" if error_code else ""),
                flush=True,
            )

        counts["error_codes"] = dict(sorted(error_codes.items()))
        summary["counts"] = counts
        summary["finished_at"] = _utc_now()
        summary["elapsed_seconds"] = time.perf_counter() - started
        summary["exit_code"] = 0 if counts["error"] == 0 else 1
    except Exception as exc:
        counts["error_codes"] = dict(sorted(error_codes.items()))
        summary["counts"] = counts
        summary["finished_at"] = _utc_now()
        summary["elapsed_seconds"] = time.perf_counter() - started
        summary["fatal_error"] = _error("batch", "fatal_error", str(exc), exc)
        summary["exit_code"] = 2
        print(f"fatal: {type(exc).__name__}: {exc}", file=sys.stderr, flush=True)

    exit_code = int(summary.get("exit_code", 2))
    try:
        run_dir.mkdir(parents=True, exist_ok=True)
        _atomic_write_json(run_dir / "summary.json", summary)
    except Exception as exc:
        print(f"error: cannot write run summary: {exc}", file=sys.stderr, flush=True)
        return 2
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
