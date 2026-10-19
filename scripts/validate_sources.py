"""校验 configs/sources.yaml 的完整性与 W1 覆盖目标。"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from campusguide.config import SourcesConfigError, load_sources

ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default=str(ROOT / "configs" / "sources.yaml"))
    parser.add_argument("--report", default=str(ROOT / "data" / "review" / "sources-report.json"))
    parser.add_argument(
        "--strict-target",
        action="store_true",
        help="未达到 W1 覆盖目标（>=30 个制度、>=50 个版本）时返回非零",
    )
    args = parser.parse_args()

    try:
        config = load_sources(Path(args.config))
    except (SourcesConfigError, FileNotFoundError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    documents_without_dates: list[str] = []
    versions_without_dates: list[str] = []
    versions_without_source: list[str] = []
    audience_values: list[str] = []

    for document in config.documents:
        for audience in document.audience:
            if audience.value not in audience_values:
                audience_values.append(audience.value)
        if not document.versions:
            documents_without_dates.append(document.doc_id)
        for version in document.versions:
            label = f"{document.doc_id}@{version.version_id}"
            if version.published_at is None or version.effective_from is None:
                versions_without_dates.append(label)
                if document.doc_id not in documents_without_dates:
                    documents_without_dates.append(document.doc_id)
            if version.url is None:
                versions_without_source.append(label)

    report = {
        "document_count": len(config.documents),
        "version_count": config.version_count,
        "meets_w1_target": len(config.documents) >= 30 and config.version_count >= 50,
        "documents_without_dates": sorted(documents_without_dates),
        "versions_without_dates": sorted(versions_without_dates),
        "versions_without_source": sorted(versions_without_source),
        "audience_values": sorted(audience_values),
    }

    report_path = Path(args.report)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    print(
        f"documents={report['document_count']} (target>=30), "
        f"versions={report['version_count']} (target>=50), "
        f"w1_target={'met' if report['meets_w1_target'] else 'not met'}"
    )
    if documents_without_dates:
        print("missing published_at/effective_from: " + ", ".join(documents_without_dates))
    if versions_without_source:
        print("missing source url: " + ", ".join(versions_without_source))
    print(f"report: {report_path}")

    if args.strict_target and not report["meets_w1_target"]:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
