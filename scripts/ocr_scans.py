"""为无文本层的扫描版 PDF 生成 OCR 旁路缓存。

只处理 data/raw 中文本层过短的 PDF；原始 PDF 不修改，识别结果写入
data/processed/ocr/{doc_id}/{version_id}.json，供 parse_corpus 按 SHA-256
匹配后使用。

    python -m uv run --group ocr python scripts/ocr_scans.py
    python -m uv run --group ocr python scripts/ocr_scans.py --only info-grad-cs-plan@v2020
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

import pymupdf

from campusguide.domain.text import count_cjk
from campusguide.ingest.ocr import (
    OCR_MIN_CJK,
    OCR_PASSES,
    load_ocr_document,
    ocr_output_path,
    ocr_pdf,
    ocr_pdf_multiscale,
    write_ocr_document,
)

ROOT = Path(__file__).resolve().parents[1]


def _selected(doc_id: str, version_id: str, only: set[str] | None) -> bool:
    if not only:
        return True
    return doc_id in only or f"{doc_id}@{version_id}" in only


def _text_layer_cjk(path: Path) -> int:
    document = pymupdf.open(path)
    try:
        return sum(count_cjk(page.get_text("text")) for page in document)
    finally:
        document.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--raw-dir", default=str(ROOT / "data" / "raw"))
    parser.add_argument("--processed-dir", default=str(ROOT / "data" / "processed"))
    parser.add_argument(
        "--only",
        action="append",
        default=None,
        help="只处理指定 doc_id 或 doc_id@version_id，可重复",
    )
    parser.add_argument(
        "--dpi",
        type=int,
        default=None,
        help="强制使用单一 DPI；省略时使用 200/240 多尺度逐页择优",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="忽略已有缓存和文本层判断，强制重新识别",
    )
    args = parser.parse_args()

    raw_dir = Path(args.raw_dir).resolve()
    ocr_dir = Path(args.processed_dir).resolve() / "ocr"
    if not raw_dir.exists():
        print(f"raw 目录不存在：{raw_dir}")
        return 1

    only_set = set(args.only) if args.only else None
    counts = {"ocr": 0, "cached": 0, "text_layer": 0, "error": 0}
    for meta_path in sorted(raw_dir.glob("*/*/meta.json")):
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            print(f"[error  ] {meta_path}: 读取 meta.json 失败：{exc}")
            counts["error"] += 1
            continue

        doc_id = str(meta.get("doc_id") or meta_path.parent.parent.name)
        version_id = str(meta.get("version_id") or meta_path.parent.name)
        if not _selected(doc_id, version_id, only_set):
            continue

        version_dir = meta_path.parent
        source_name = meta.get("source_file")
        source_path = version_dir / str(source_name) if source_name else None
        if source_path is None or not source_path.exists():
            candidates = sorted(version_dir.glob("source.*"))
            source_path = candidates[0] if candidates else None
        if source_path is None or not source_path.exists():
            print(f"[error  ] {doc_id}@{version_id} 找不到原始快照")
            counts["error"] += 1
            continue

        source_format = str(meta.get("format") or source_path.suffix.lstrip("."))
        if source_format != "pdf":
            continue

        source_sha256 = hashlib.sha256(source_path.read_bytes()).hexdigest()
        label = f"{doc_id}@{version_id}"
        output_path = ocr_output_path(ocr_dir, doc_id, version_id)

        if not args.force and output_path.exists():
            try:
                cached = load_ocr_document(output_path)
            except (OSError, ValueError):
                cached = None
            if cached is not None and cached.source_sha256 == source_sha256:
                print(f"[cached ] {label} {output_path}")
                counts["cached"] += 1
                continue

        if not args.force:
            cjk = _text_layer_cjk(source_path)
            if cjk >= OCR_MIN_CJK:
                print(f"[skip   ] {label} 文本层 cjk={cjk}，无需 OCR")
                counts["text_layer"] += 1
                continue

        if args.dpi is None:
            pass_label = "passes=" + "/".join(str(item.dpi) for item in OCR_PASSES)
        else:
            pass_label = f"dpi={args.dpi}"
        print(f"[ocr    ] {label} {source_path.name} {pass_label}")

        def report(page_no: int, total: int) -> None:
            print(f"          第 {page_no}/{total} 页完成")

        try:
            if args.dpi is None:
                document = ocr_pdf_multiscale(
                    source_path,
                    doc_id=doc_id,
                    version_id=version_id,
                    on_page=report,
                )
            else:
                document = ocr_pdf(
                    source_path,
                    doc_id=doc_id,
                    version_id=version_id,
                    dpi=args.dpi,
                    on_page=report,
                )
        except Exception as exc:
            print(f"[error  ] {label} {type(exc).__name__}: {exc}")
            counts["error"] += 1
            continue

        path = write_ocr_document(ocr_dir, document)
        print(
            f"[done   ] {label} pages={document.page_count} "
            f"confidence={document.mean_confidence:.3f} -> {path}"
        )
        for warning in document.warnings:
            print(f"          warning: {warning}")
        counts["ocr"] += 1

    print(
        "Summary: "
        + ", ".join(f"{key}={value}" for key, value in counts.items())
        + f", ocr_dir={ocr_dir}"
    )
    return 1 if counts["error"] else 0


if __name__ == "__main__":
    sys.exit(main())
