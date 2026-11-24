"""Register a FAOSTAT producer price export and optionally archive it on S3.

The export is a CSV downloaded from the FAOSTAT Producer Prices (PP) domain
for Pistachios, in shell. The file is never modified. Its byte order mark is
removed only while parsing. The manifest identifies the file by SHA-256 and
summarizes its columns, domains, elements, items, years, and areas so later
steps can confirm they read the same export.

Run from the repository root:
    python -m src.fao_prices.extract [source] [--retrieved-on YYYY-MM-DD] [--upload-s3]
"""

from __future__ import annotations

import argparse
import codecs
import csv
import fcntl
import hashlib
import io
import json
from datetime import date
from pathlib import Path

from src.usda_psd.extract import write_manifest

from .config import DATASET, DOWNLOAD_DATE, S3_BUCKET, S3_REGION, SOURCE_FILE, SOURCE_URL, batch_dir

COLUMNS = (
    "Domain Code", "Domain", "Area Code (M49)", "Area", "Element Code", "Element",
    "Item Code (CPC)", "Item", "Year Code", "Year", "Months Code", "Months",
    "Unit", "Value", "Flag", "Flag Description",
)


def parse_export(source_bytes: bytes) -> list[list[str]]:
    """Check the header and return the data rows as source text cells."""
    rows = list(csv.reader(io.StringIO(source_bytes.decode("utf-8-sig"), newline="")))
    if not rows or tuple(rows[0]) != COLUMNS:
        raise ValueError(f"Unexpected FAOSTAT producer price header: {rows[0] if rows else []}")
    if len(rows) < 2:
        raise ValueError("The FAOSTAT producer price export has no data rows")
    return rows[1:]


def build_manifest(path: Path, source_bytes: bytes, retrieved_on: date) -> dict:
    """Describe the original export and identify it by its full SHA-256."""
    source_rows = parse_export(source_bytes)
    rows = [row for row in source_rows if len(row) == len(COLUMNS)]

    def distinct(column: str) -> list[str]:
        index = COLUMNS.index(column)
        return sorted({row[index] for row in rows})

    years = distinct("Year")
    return {
        "retrieved_on": retrieved_on.isoformat(),
        "source_url": SOURCE_URL,
        "source_file": path.name,
        "source_format": "csv",
        "source_has_bom": source_bytes.startswith(codecs.BOM_UTF8),
        "source_size_bytes": len(source_bytes),
        "source_sha256": hashlib.sha256(source_bytes).hexdigest(),
        "columns": list(COLUMNS),
        "domains": distinct("Domain"),
        "elements": distinct("Element"),
        "items": distinct("Item"),
        "years": [years[0], years[-1]] if years else [],
        "area_count": len(distinct("Area Code (M49)")),
        "source_row_count": len(source_rows),
    }


def upload_source(path: Path, source_bytes: bytes, retrieved_on: date, manifest: dict) -> None:
    """Archive the original export on S3 and record its location in the manifest."""
    from botocore.exceptions import BotoCoreError, ClientError

    from src.acp.s3_upload import upload_raw_object

    try:
        result = upload_raw_object(
            source_bytes,
            bucket=S3_BUCKET,
            region=S3_REGION,
            key=f"raw/{DATASET}/{retrieved_on.isoformat()}/{path.name}",
            content_type="text/csv",
        )
    except (BotoCoreError, ClientError, ValueError) as error:
        raise ValueError(f"S3 upload failed: {error}") from error
    manifest["s3_uri"] = result.uri
    manifest.pop("s3_version_id", None)
    if result.version_id:
        manifest["s3_version_id"] = result.version_id
    status = "Uploaded" if result.created else "Skipped (identical source)"
    print(f"{status}: {result.uri}")


def register(path: Path, retrieved_on: date, *, upload_s3: bool = False) -> Path:
    """Write the manifest for one export and optionally archive the original file.

    A download date belongs to one export: registering a different file for
    the same date fails instead of overwriting the earlier manifest.
    """
    source_bytes = path.read_bytes()
    manifest = build_manifest(path, source_bytes, retrieved_on)
    destination = batch_dir("extract", retrieved_on) / "manifest.json"
    destination.parent.mkdir(parents=True, exist_ok=True)
    with destination.with_suffix(".lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if destination.exists():
            previous = json.loads(destination.read_text(encoding="utf-8"))
            if (previous["source_sha256"], previous["source_file"]) != (
                manifest["source_sha256"], manifest["source_file"]
            ):
                raise ValueError(f"{destination} already describes a different source")
            for field in ("s3_uri", "s3_version_id"):
                if field in previous:
                    manifest[field] = previous[field]
        write_manifest(destination, manifest)
        if upload_s3:
            upload_source(path, source_bytes, retrieved_on, manifest)
            write_manifest(destination, manifest)
    return destination


def main() -> None:
    """Register the export selected by command-line arguments."""
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("source", nargs="?", type=Path, help="Path to the FAOSTAT CSV export")
    parser.add_argument("--retrieved-on", type=date.fromisoformat, default=DOWNLOAD_DATE)
    parser.add_argument("--upload-s3", action="store_true", help="Archive the original file on S3")
    args = parser.parse_args()
    path = args.source or batch_dir("raw", args.retrieved_on) / SOURCE_FILE
    try:
        print(register(path, args.retrieved_on, upload_s3=args.upload_s3))
    except (OSError, ValueError, KeyError, ImportError) as error:
        parser.exit(1, f"{error}\n")


if __name__ == "__main__":
    main()
