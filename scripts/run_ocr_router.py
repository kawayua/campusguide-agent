#!/usr/bin/env python3
"""Minimal shadow-only OCR router CLI; never writes Qdrant or production state."""

import argparse
import hashlib
import json
import os
import sys
import time
from pathlib import Path

REQUIRED = (
    "sample_id",
    "doc_id",
    "version_id",
    "source_format",
    "source_sha256",
    "page_no",
    "image_path",
    "image_sha256",
)
LAYOUT_REQUIRED = ("model.safetensors", "config.json", "preprocessor_config.json")
IGNORED, PENDING = {"header", "footer"}, {"unknown", "figure"}
COUNTS = (
    "region_count",
    "ignored_count",
    "routed_count",
    "pending_count",
    "error_count",
    "cloud_requests",
    "local_requests",
    "cache_hits",
)
SOURCES = ("local", "gemini", "none")


def sha256_file(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def is_sha256(value):
    if not isinstance(value, str):
        return False
    text = value.strip()
    return len(text) == 64 and all(
        character in "0123456789abcdefABCDEF" for character in text
    )


def verify_layout_install(manifest, model_dir):
    files = manifest.get("files") if isinstance(manifest, dict) else None
    if not isinstance(files, list) or not files:
        raise ValueError("layout manifest files must be a non-empty array")
    seen = set()
    for index, entry in enumerate(files):
        if not isinstance(entry, dict):
            raise ValueError(f"layout manifest files[{index}] must be an object")
        filename = entry.get("filename")
        if (
            not isinstance(filename, str)
            or not filename
            or filename in {".", ".."}
            or Path(filename).name != filename
            or "/" in filename
            or "\\" in filename
            or ":" in filename
        ):
            raise ValueError(f"layout manifest files[{index}] has invalid filename")
        if filename in seen:
            raise ValueError(f"layout manifest duplicate filename: {filename}")
        seen.add(filename)
        size = entry.get("size")
        if isinstance(size, bool) or not isinstance(size, int) or size < 0:
            raise ValueError(f"layout manifest {filename} has invalid size")
        digest = entry.get("sha256")
        if not is_sha256(digest):
            raise ValueError(f"layout manifest {filename} has invalid sha256")
        path = model_dir / filename
        if not path.is_file():
            raise ValueError(f"layout model file missing: {filename}")
        if path.stat().st_size != size:
            raise ValueError(f"layout model file size mismatch: {filename}")
        if sha256_file(path) != digest.strip().lower():
            raise ValueError(f"layout model file sha256 mismatch: {filename}")
    missing = [name for name in LAYOUT_REQUIRED if name not in seen]
    if missing:
        raise ValueError(
            "layout manifest missing required files: " + ", ".join(missing)
        )


def write_json(path, payload):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True, default=str)
        + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def nonnegative(text):
    try:
        value = int(text)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be an integer") from exc
    if value < 0:
        raise argparse.ArgumentTypeError("must be non-negative")
    return value


def new_counts():
    return {
        **{key: 0 for key in COUNTS},
        "selected_source_counts": {key: 0 for key in SOURCES},
    }


def merge_counts(total, part):
    for key in COUNTS:
        total[key] += int(part.get(key, 0))
    selected = part.get("selected_source_counts", {})
    for key in SOURCES:
        total["selected_source_counts"][key] += int(selected.get(key, 0))


def check_native_parsed_binding(row, document):
    problems = []
    if document.doc_id != row["doc_id"]:
        problems.append("doc_id")
    if document.version_id != row["version_id"]:
        problems.append("version_id")
    if document.source_sha256.strip().lower() != row["source_sha256"]:
        problems.append("source_sha256")
    if document.format != row["source_format"]:
        problems.append("format")
    page_count = document.page_count
    if page_count is not None and (
        isinstance(page_count, bool)
        or not isinstance(page_count, int)
        or page_count < row["page_no"]
    ):
        problems.append("page_count")
    if problems:
        raise ValueError(
            f"native parsed document binding mismatch for {row['sample_id']}: "
            + ", ".join(problems)
        )


def load_rows(path, evidence_cls, *, native_crosscheck=False):
    rows, seen = [], set()
    for line_no, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not raw.strip():
            continue
        try:
            row = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError(f"line {line_no}: invalid JSON: {exc}") from exc
        if not isinstance(row, dict):
            raise ValueError(f"line {line_no}: row must be an object")
        missing = [key for key in REQUIRED if key not in row]
        if missing:
            raise ValueError(f"line {line_no}: missing keys: {', '.join(missing)}")
        bad = [
            key
            for key in REQUIRED
            if key != "page_no"
            and (not isinstance(row[key], str) or not row[key].strip())
        ]
        if bad:
            raise ValueError(
                f"line {line_no}: fields must be non-empty strings: {', '.join(bad)}"
            )
        sample_id, page_no = row["sample_id"].strip(), row["page_no"]
        if sample_id in seen:
            raise ValueError(f"line {line_no}: duplicate sample_id {sample_id!r}")
        seen.add(sample_id)
        if isinstance(page_no, bool) or not isinstance(page_no, int) or page_no <= 0:
            raise ValueError(f"line {line_no}: page_no must be a positive integer")
        source_sha = row["source_sha256"].strip().lower()
        image_sha = row["image_sha256"].strip().lower()
        source_file = None
        if row.get("source_path") is not None:
            if not isinstance(row["source_path"], str) or not row["source_path"].strip():
                raise ValueError(
                    f"line {line_no}: source_path must be a non-empty string"
                )
            source_file = Path(row["source_path"].strip())
            if not source_file.is_absolute():
                source_file = path.parent / source_file
            if not source_file.is_file():
                raise ValueError(f"line {line_no}: source_path does not exist")
            if sha256_file(source_file) != source_sha:
                raise ValueError(f"line {line_no}: source_path sha256 mismatch")
        native_parsed_file = None
        if native_crosscheck and row["source_format"] == "pdf":
            if source_file is None:
                raise ValueError(
                    f"line {line_no}: source_path is required for PDF rows"
                )
            if "native_parsed_path" not in row:
                raise ValueError(
                    f"line {line_no}: native_parsed_path is required for PDF rows"
                )
            if (
                not isinstance(row["native_parsed_path"], str)
                or not row["native_parsed_path"].strip()
            ):
                raise ValueError(
                    f"line {line_no}: native_parsed_path must be a non-empty string"
                )
            native_parsed_file = Path(row["native_parsed_path"].strip())
            if not native_parsed_file.is_absolute():
                native_parsed_file = path.parent / native_parsed_file
            if not native_parsed_file.is_file():
                raise ValueError(
                    f"line {line_no}: native_parsed_path does not exist"
                )
        image_file = Path(row["image_path"])
        if not image_file.is_absolute():
            image_file = path.parent / image_file
        evidence = evidence_cls.from_png(
            doc_id=row["doc_id"],
            version_id=row["version_id"],
            source_format=row["source_format"],
            source_sha256=source_sha,
            page=page_no,
            image_path=image_file,
        )
        if evidence.image_sha256.lower() != image_sha:
            raise ValueError(f"line {line_no}: image_sha256 mismatch")
        rows.append(
            {
                **{key: row[key] for key in REQUIRED},
                "sample_id": sample_id,
                "source_sha256": source_sha,
                "image_sha256": image_sha,
                "image_file": image_file,
                "source_file": source_file,
                "native_parsed_file": native_parsed_file,
                "evidence": evidence,
            }
        )
    if not rows:
        raise ValueError("input contains no rows")
    return rows, sha256_file(path)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--install-root", type=Path, default=Path("D:/xiaomi-ocr"))
    parser.add_argument("--device", default="cuda")
    parser.add_argument("--layout-device", default="cpu")
    parser.add_argument("--allow-gemini", action="store_true")
    parser.add_argument("--dotenv", type=Path)
    parser.add_argument("--max-cloud-requests", type=nonnegative, default=10)
    parser.add_argument("--native-crosscheck", action="store_true")
    parser.add_argument("--resume", action="store_true")
    args = parser.parse_args(argv)
    started = time.monotonic()
    out = args.out
    manifest_path = out / "manifest.json"
    if manifest_path.exists() and not args.resume:
        print(f"error: {manifest_path} exists; pass --resume", file=sys.stderr)
        return 2
    native_parsed, native_parsed_sha = {}, {}
    try:
        from campusguide.ingest.ocr_layout import (
            PageImageEvidence,
            PPDocLayoutV3Detector,
            crop_layout_region,
        )
        from campusguide.ingest.ocr_native_expectation import build_native_expectation
        from campusguide.ingest.ocr_router import RouterConfig, route_region
        from campusguide.ingest.ocr_runtime import TransformersXiaomiBackend
        from campusguide.ingest.parse import load_parsed_document
        from campusguide.ingest.visual import VisionConfig, VisionRequestBudget

        config = RouterConfig()
        rows, input_sha = load_rows(
            args.input,
            PageImageEvidence,
            native_crosscheck=args.native_crosscheck,
        )
        layout_file = args.install_root / "layout_installation_manifest.json"
        layout_manifest = json.loads(layout_file.read_text(encoding="utf-8"))
        if not isinstance(layout_manifest, dict):
            raise ValueError("layout manifest must be an object")
        layout_revision = layout_manifest["revision"]
        layout_manifest_sha = sha256_file(layout_file)
        if not isinstance(layout_revision, str) or not layout_revision:
            raise ValueError("layout manifest missing revision")
        layout_model_dir = args.install_root / "models" / "PP-DocLayoutV3"
        verify_layout_install(layout_manifest, layout_model_dir)
        installation_manifest_sha = sha256_file(
            args.install_root / "installation_manifest.json"
        )
        if args.native_crosscheck:
            for row in rows:
                if row["source_format"] != "pdf":
                    continue
                parsed_path = row["native_parsed_file"]
                document = load_parsed_document(parsed_path)
                check_native_parsed_binding(row, document)
                native_parsed[row["sample_id"]] = document
                native_parsed_sha[row["sample_id"]] = sha256_file(parsed_path)
        if manifest_path.exists():
            existing = json.loads(manifest_path.read_text(encoding="utf-8"))
            if not isinstance(existing, dict):
                raise ValueError("existing output manifest must be an object")
            if (
                existing.get("native_crosscheck") != args.native_crosscheck
                or existing.get("native_parsed_sha256") != native_parsed_sha
            ):
                print("error: existing output settings differ", file=sys.stderr)
                return 2
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    gemini, budget = None, VisionRequestBudget(args.max_cloud_requests)
    try:
        local = TransformersXiaomiBackend(args.install_root, args.device)
        detector = PPDocLayoutV3Detector(
            model_path=layout_model_dir,
            model_revision=layout_revision,
            device=args.layout_device,
        )
        if args.allow_gemini:
            if args.dotenv:
                from campusguide.env import load_dotenv

                load_dotenv(args.dotenv)
            from campusguide.ingest.ocr_gemini import GeminiRegionBackend

            gemini = GeminiRegionBackend(
                config=VisionConfig.from_yaml(Path("configs/vision.yaml"))
            )
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    gemini_identity = None
    if gemini is not None:
        gemini_identity = {
            "model": gemini.model,
            "version": gemini.version,
            "prompt_revision": gemini.prompt_revision,
        }
    settings = {
        "input_sha256": input_sha,
        "allow_gemini": args.allow_gemini,
        "layout_model_revision": layout_revision,
        "layout_manifest_sha256": layout_manifest_sha,
        "installation_manifest_sha256": installation_manifest_sha,
        "layout_device": args.layout_device,
        "device": args.device,
        "max_cloud_requests": args.max_cloud_requests,
        "native_crosscheck": args.native_crosscheck,
        "native_parsed_sha256": native_parsed_sha,
        "config": config.model_dump(mode="json"),
        "local": {
            "backend_identity": local.backend_identity,
            "model": local.model,
            "model_revision": local.model_revision,
            "postprocess_revision": local.postprocess_revision,
        },
        "gemini": gemini_identity,
    }
    try:
        if manifest_path.exists():
            existing = json.loads(manifest_path.read_text(encoding="utf-8"))
            if existing != settings:
                print("error: existing output settings differ", file=sys.stderr)
                return 2
        else:
            write_json(manifest_path, settings)
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    totals = new_counts()
    pages_processed = pages_skipped = pages_without_regions = 0
    try:
        for row in rows:
            page_dir = (
                out
                / "pages"
                / hashlib.sha256(row["sample_id"].encode("utf-8")).hexdigest()[:20]
            )
            page_path = page_dir / "page.json"
            pages_processed += 1
            counts, regions = new_counts(), []
            layout_review, page_error = False, False
            row_is_pdf = row["source_format"] == "pdf"
            native_page = None
            if args.native_crosscheck and not row_is_pdf:
                native_page = {
                    "applicable": False,
                    "status": "unavailable",
                    "reason_codes": ["unsupported_source_format"],
                    "verification": "not_performed",
                    "diagnostic_only": True,
                    "not_truth": True,
                }
            try:
                layout = detector.detect(row["evidence"])
                layout_review = bool(layout.needs_review)
                write_json(page_dir / "layout.json", layout.model_dump(mode="json"))
            except Exception as exc:
                page_error, layout = True, None
                counts["error_count"] += 1
                write_json(
                    page_dir / "layout.json",
                    {"status": "error", "error": f"{type(exc).__name__}: {exc}"},
                )
            if layout is not None:
                for index, region in enumerate(layout.regions):
                    kind = str(getattr(region.kind, "value", region.kind))
                    region_id = str(region.region_id)
                    record = {
                        "sample_id": row["sample_id"],
                        "page_no": row["page_no"],
                        "region_id": region_id,
                        "kind": kind,
                    }
                    counts["region_count"] += 1
                    native_review = False
                    try:
                        crop = crop_layout_region(
                            row["evidence"], region, output_dir=out / "crops"
                        )
                        if kind in IGNORED:
                            record["status"] = "ignored"
                            counts["ignored_count"] += 1
                        else:
                            expectation = None
                            if (
                                args.native_crosscheck
                                and row_is_pdf
                                and kind == "table"
                            ):
                                try:
                                    native_audit = build_native_expectation(
                                        crop,
                                        source_path=row["source_file"],
                                        parsed=native_parsed[row["sample_id"]],
                                    )
                                except Exception as exc:
                                    record["native_crosscheck"] = {
                                        "applicable": True,
                                        "status": "error",
                                        "diagnostic_only": True,
                                        "not_truth": True,
                                        "error": f"{type(exc).__name__}: {exc}",
                                    }
                                    raise
                                record["native_crosscheck"] = (
                                    native_audit.model_dump(mode="json")
                                )
                                expectation = native_audit.expectation
                                native_review = native_audit.needs_review
                            result = route_region(
                                crop,
                                local_backend=local,
                                postprocess=local.postprocess,
                                local_model=local.model,
                                local_model_revision=local.model_revision,
                                postprocess_revision=local.postprocess_revision,
                                config=config,
                                gemini_backend=gemini,
                                budget=budget,
                                cache_dir=out / "route_cache",
                                expectation=expectation,
                            )
                            if native_review:
                                result = result.model_copy(
                                    update={
                                        "needs_review": True,
                                        "reason_codes": list(
                                            dict.fromkeys(
                                                [
                                                    *result.reason_codes,
                                                    "native_source_needs_review",
                                                ]
                                            )
                                        ),
                                    }
                                )
                            usage = result.usage
                            counts["cloud_requests"] += int(usage.cloud_requests)
                            counts["local_requests"] += int(usage.local_requests)
                            counts["selected_source_counts"][result.selected_source] += 1
                            if result.cache == "hit":
                                counts["cache_hits"] += 1
                            record["result"] = result.model_dump(mode="json")
                            if result.cache != "hit" and int(usage.local_requests) > 0:
                                record["runtime_audit"] = local.last_call_audit
                            pending = (
                                layout_review
                                or native_review
                                or bool(result.needs_review)
                                or not result.success
                                or kind in PENDING
                            )
                            record["status"] = "pending" if pending else "routed"
                            counts["pending_count" if pending else "routed_count"] += 1
                    except Exception as exc:
                        record.update(
                            status="error", error=f"{type(exc).__name__}: {exc}"
                        )
                        counts["error_count"] += 1
                        counts["pending_count"] += 1
                    write_json(page_dir / "regions" / f"{index:04d}.json", record)
                    regions.append(
                        {
                            "region_id": region_id,
                            "kind": kind,
                            "status": record["status"],
                            "file": f"regions/{index:04d}.json",
                        }
                    )
                if not layout.regions or (
                    counts["region_count"]
                    and counts["ignored_count"] == counts["region_count"]
                ):
                    counts["pending_count"] += 1
            if page_error or counts["error_count"]:
                page_status = "error"
            elif counts["region_count"] == 0:
                page_status = "no_regions"
                pages_without_regions += 1
            elif counts["pending_count"]:
                page_status = "pending"
            elif counts["routed_count"] == 0:
                page_status = "no_routed"
            else:
                page_status = "ok"
            page_payload = {
                "sample_id": row["sample_id"],
                "doc_id": row["doc_id"],
                "version_id": row["version_id"],
                "source_format": row["source_format"],
                "source_sha256": row["source_sha256"],
                "page_no": row["page_no"],
                "image_path": str(row["image_file"]),
                "image_sha256": row["image_sha256"],
                "layout": "layout.json",
                "layout_needs_review": layout_review,
                "status": page_status,
                "regions": regions,
                "counts": counts,
            }
            if native_page is not None:
                page_payload["native_crosscheck"] = native_page
            write_json(page_path, page_payload)
            merge_counts(totals, counts)
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    pending = totals["pending_count"]
    errors = totals["error_count"]
    routed = totals["routed_count"]
    if errors:
        status, code = "error", 2
    elif pending:
        status, code = "pending", 1
    elif routed == 0:
        status, code = "no_routed_regions", 2
    else:
        status, code = "ok", 0
    summary = {
        "shadow": True,
        "publishable": False,
        "status": status,
        "page_count": len(rows),
        "pages_processed": pages_processed,
        "pages_skipped": pages_skipped,
        "pages_without_regions": pages_without_regions,
        "region_count": totals["region_count"],
        "ignored_count": totals["ignored_count"],
        "routed_count": routed,
        "pending_count": pending,
        "error_count": errors,
        "cloud_requests": totals["cloud_requests"],
        "current_local_requests": totals["local_requests"],
        "cache_hits": totals["cache_hits"],
        "selected_source_counts": totals["selected_source_counts"],
        "wall_time_seconds": round(time.monotonic() - started, 3),
    }
    try:
        write_json(out / "summary.json", summary)
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    return code


if __name__ == "__main__":
    raise SystemExit(main())
