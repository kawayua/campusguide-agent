"""Contract tests for the standalone Xiaomi-OCR local runner.

These tests load the runner as a plain module and exercise only the lightweight
parsing, truncation classification, and cache-identity helpers. They never
import Torch/Transformers, read images, or run inference.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from pathlib import Path

import pytest

SCRIPT_PATH = Path(__file__).resolve().parents[2] / "scripts" / "run_xiaomi_ocr_local.py"


def _load_runner():
    spec = importlib.util.spec_from_file_location(
        "run_xiaomi_ocr_local_under_test",
        SCRIPT_PATH,
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # Dataclasses resolve postponed annotations through sys.modules for the
    # defining module, so register it before executing the module body and
    # restore whatever entry existed before.
    previous = sys.modules.get(spec.name)
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
    finally:
        if previous is None:
            sys.modules.pop(spec.name, None)
        else:
            sys.modules[spec.name] = previous
    return module


runner = _load_runner()


def _write_jsonl(path: Path, records: list[dict]) -> Path:
    lines = [json.dumps(record, ensure_ascii=False) for record in records]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def _static_identity() -> dict:
    return {
        "model": {
            "identifier": "local-model",
            "files_sha256": "model-files",
            "revision": "model-revision",
            "repo_commit": "repo-commit",
        },
        "prompt": {"identity_sha256": "prompt-v1"},
        "preprocess": {"files_sha256": "preprocess-v1"},
        "postprocess": {"files_sha256": "postprocess-v1"},
        "installation": {},
    }


def _runtime_identity() -> dict:
    return {
        "python": "3.11.9",
        "torch": "2.11.0",
        "transformers": "5.20.2",
        "pillow": "11.0.0",
        "safetensors": "0.5.0",
        "torchvision": "0.26.0",
        "backend": "cuda",
        "dtype": "float16",
        "device_name": "test-gpu",
    }


def test_finish_eos_at_limit_is_eos() -> None:
    assert runner._classify_finish(8192, 8192, 2, {2}) == "eos"


def test_finish_without_eos_at_limit_is_token_limit() -> None:
    assert runner._classify_finish(8192, 8192, 7, {2}) == "token_limit"


def test_finish_without_eos_below_limit_is_stopped_other() -> None:
    assert runner._classify_finish(10, 8192, 7, {2}) == "stopped_other"


def test_finish_with_no_generated_tokens_is_empty() -> None:
    assert runner._classify_finish(0, 8192, None, {2}) == "empty"


def test_collect_eos_ids_flattens_nested_sources() -> None:
    assert runner._collect_eos_ids(None, 2, [3, 4], (5, True)) == {2, 3, 4, 5}


def test_structural_repair_flag() -> None:
    changed = runner._structural_repair("a\n\nb", "a\nb")
    assert changed is not None
    assert changed["applied"] is True
    unchanged = runner._structural_repair("a", "a")
    assert unchanged is not None
    assert unchanged["applied"] is False
    assert runner._structural_repair("a", None) is None


@pytest.mark.parametrize("value", ["1", "8192", "16384"])
def test_max_new_tokens_accepts_in_range(value: str) -> None:
    assert runner._max_new_tokens_arg(value) == int(value)


@pytest.mark.parametrize("value", ["0", "-1", "16385", "abc"])
def test_max_new_tokens_rejects_out_of_range_or_non_integer(value: str) -> None:
    with pytest.raises(argparse.ArgumentTypeError):
        runner._max_new_tokens_arg(value)


def test_load_sample_specs_resolves_paths_and_normalizes_hashes(tmp_path: Path) -> None:
    source_sha = "A" * 64
    image_sha = "B" * 64
    input_path = _write_jsonl(
        tmp_path / "samples.jsonl",
        [
            {
                "sample_id": "page-1",
                "image_path": "images/page-1.png",
                "source_sha256": source_sha,
                "page_no": 1,
                "image_sha256": image_sha,
            }
        ],
    )

    samples = runner.load_sample_specs(input_path)

    assert len(samples) == 1
    spec = samples[0]
    assert spec.line_no == 1
    assert spec.sample_id == "page-1"
    assert spec.image_path == (tmp_path / "images" / "page-1.png").resolve()
    assert spec.source_sha256 == source_sha.lower()
    assert spec.image_sha256_expected == image_sha.lower()
    assert spec.page_no == 1


@pytest.mark.parametrize("page_no", [0, -1, True, False, 1.0, "1", None])
def test_load_sample_specs_rejects_non_positive_or_non_integer_page_no(
    tmp_path: Path,
    page_no: object,
) -> None:
    input_path = _write_jsonl(
        tmp_path / "samples.jsonl",
        [
            {
                "sample_id": "page-1",
                "image_path": "page-1.png",
                "source_sha256": "c" * 64,
                "page_no": page_no,
            }
        ],
    )

    with pytest.raises(ValueError, match="page_no must be a positive integer"):
        runner.load_sample_specs(input_path)


def test_load_sample_specs_allows_missing_optional_image_hash(tmp_path: Path) -> None:
    input_path = _write_jsonl(
        tmp_path / "samples.jsonl",
        [
            {
                "sample_id": "page-1",
                "image_path": "page-1.png",
                "source_sha256": "c" * 64,
                "page_no": 1,
            }
        ],
    )

    samples = runner.load_sample_specs(input_path)

    assert samples[0].image_sha256_expected is None


def test_load_sample_specs_rejects_duplicate_sample_ids(tmp_path: Path) -> None:
    record = {
        "sample_id": "page-1",
        "image_path": "page-1.png",
        "source_sha256": "c" * 64,
        "page_no": 1,
    }
    input_path = _write_jsonl(tmp_path / "samples.jsonl", [record, dict(record)])

    with pytest.raises(ValueError, match="duplicate sample_id"):
        runner.load_sample_specs(input_path)


def test_load_sample_specs_rejects_invalid_source_hash(tmp_path: Path) -> None:
    input_path = _write_jsonl(
        tmp_path / "samples.jsonl",
        [
            {
                "sample_id": "page-1",
                "image_path": "page-1.png",
                "source_sha256": "not-a-hash",
                "page_no": 1,
            }
        ],
    )

    with pytest.raises(ValueError, match="source_sha256"):
        runner.load_sample_specs(input_path)


def test_cache_identity_is_stable_and_sealed() -> None:
    identity, cache_key = runner._build_cache_identity(
        _static_identity(),
        _runtime_identity(),
        "a" * 64,
        8192,
    )
    same_identity, same_key = runner._build_cache_identity(
        _static_identity(),
        _runtime_identity(),
        "a" * 64,
        8192,
    )

    assert identity == same_identity
    assert cache_key == same_key
    assert cache_key == runner._digest_json(identity)


def test_base_result_reports_loaded_backend_identity() -> None:
    context = {
        "model_class": "Qwen3_5ForConditionalGeneration",
        "processor_class": "Qwen3_5Processor",
        "processor_module": "transformers.models.qwen3_5.processing_qwen3_5",
        "image_processor_class": "Qwen3_5ImageProcessorFast",
        "image_processor_module": "transformers.models.qwen3_5.image_processing_qwen3_5_fast",
        "attention_implementation": "sdpa",
        "parameter_dtype": "torch.float16",
    }

    result = runner._base_result(_static_identity(), _runtime_identity(), context)

    assert result["status"] == "error"
    assert result["model"]["class"] == "Qwen3_5ForConditionalGeneration"
    assert result["model"]["processor_class"] == "Qwen3_5Processor"
    assert result["model"]["processor_module"] == (
        "transformers.models.qwen3_5.processing_qwen3_5"
    )
    assert result["model"]["image_processor_class"] == "Qwen3_5ImageProcessorFast"
    assert result["model"]["image_processor_module"] == (
        "transformers.models.qwen3_5.image_processing_qwen3_5_fast"
    )
    assert result["model"]["attention_implementation"] == "sdpa"
    assert result["model"]["parameter_dtype"] == "torch.float16"
    assert result["runtime"]["torchvision"] == "0.26.0"


def test_cache_identity_changes_with_image_and_token_budget() -> None:
    static = _static_identity()
    runtime = _runtime_identity()
    _, base_key = runner._build_cache_identity(static, runtime, "a" * 64, 8192)
    _, image_key = runner._build_cache_identity(static, runtime, "b" * 64, 8192)
    _, token_key = runner._build_cache_identity(static, runtime, "a" * 64, 4096)

    assert len({base_key, image_key, token_key}) == 3


def test_cache_identity_changes_with_prompt_and_preprocess() -> None:
    runtime = _runtime_identity()
    _, base_key = runner._build_cache_identity(_static_identity(), runtime, "a" * 64, 8192)

    prompt_static = _static_identity()
    prompt_static["prompt"]["identity_sha256"] = "prompt-v2"
    _, prompt_key = runner._build_cache_identity(prompt_static, runtime, "a" * 64, 8192)

    preprocess_static = _static_identity()
    preprocess_static["preprocess"]["files_sha256"] = "preprocess-v2"
    _, preprocess_key = runner._build_cache_identity(
        preprocess_static,
        runtime,
        "a" * 64,
        8192,
    )

    assert len({base_key, prompt_key, preprocess_key}) == 3


def test_cache_identity_changes_with_runtime_device() -> None:
    static = _static_identity()
    _, base_key = runner._build_cache_identity(static, _runtime_identity(), "a" * 64, 8192)

    other_runtime = _runtime_identity()
    other_runtime["device_name"] = "other-gpu"
    _, other_key = runner._build_cache_identity(static, other_runtime, "a" * 64, 8192)

    assert base_key != other_key


def _sealed_cache_payload(result: dict, tmp_path: Path) -> tuple[Path, str]:
    identity, cache_key = runner._build_cache_identity(
        _static_identity(),
        _runtime_identity(),
        "a" * 64,
        8192,
    )
    payload = runner._build_cache_payload(result, identity, cache_key, "run-test")
    path = tmp_path / "cache.json"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path, cache_key


def test_load_cache_payload_accepts_sealed_success(tmp_path: Path) -> None:
    path, cache_key = _sealed_cache_payload(
        {"status": "success", "raw_markdown": "raw", "normalized_markdown": "normalized"},
        tmp_path,
    )

    loaded = runner._load_cache_payload(path, cache_key)

    assert loaded["result"]["status"] == "success"


def test_load_cache_payload_rejects_success_without_markdown(tmp_path: Path) -> None:
    path, cache_key = _sealed_cache_payload(
        {"status": "success", "raw_markdown": None, "normalized_markdown": None},
        tmp_path,
    )

    with pytest.raises(runner.CacheIntegrityError, match="raw markdown"):
        runner._load_cache_payload(path, cache_key)
