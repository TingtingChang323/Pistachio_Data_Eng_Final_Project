"""Register a USDA PSD export and optionally archive the original on S3."""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import re
from datetime import date
from html.parser import HTMLParser
from pathlib import Path
from tempfile import NamedTemporaryFile

from .config import DATASET, DOWNLOAD_DATE, S3_BUCKET, S3_REGION, SOURCE_FILE, SOURCE_URL, batch_dir

IDENTIFIER_HEADERS = ("Cm_Code", "Commodity", "Attribute_Id", "Attribute", "Cn_Code", "Country")
UNIT_HEADER = "Unit Description"
YEAR_LABEL = re.compile(r"([1-9][0-9]{3})/([1-9][0-9]{3})")


class ExportTables(HTMLParser):
    """Read table cells as text, preserving the export's row and column order."""

    def __init__(self) -> None:
        super().__init__()
        self.tables: list[list[list[str]]] = []
        self.table: list[list[str]] | None = None
        self.row: list[str] | None = None
        self.cell: list[str] | None = None

    def handle_starttag(self, tag, attrs):
        if tag == "table":
            if self.table is not None:
                raise ValueError("Unexpected nested table in USDA PSD export")
            self.table = []
        elif tag == "tr" and self.table is not None:
            self.row = []
        elif tag in ("td", "th") and self.row is not None:
            self.cell = []

    def handle_data(self, data):
        if self.cell is not None:
            self.cell.append(data)

    def handle_endtag(self, tag):
        if tag in ("td", "th") and self.row is not None and self.cell is not None:
            self.row.append("".join(self.cell))
            self.cell = None
        elif tag == "tr" and self.table is not None and self.row is not None:
            self.table.append(self.row)
            self.row = None
        elif tag == "table" and self.table is not None:
            self.tables.append(self.table)
            self.table = None


def parse_export(source_bytes: bytes) -> tuple[list[str], list[list[str]]]:
    """Find the PSD year-column table and return its headers and original cells."""
    parser = ExportTables()
    parser.feed(source_bytes.decode("utf-8-sig"))
    parser.close()
    matches = []
    for table in parser.tables:
        if not table:
            continue
        headers = [cell.strip() for cell in table[0]]
        if tuple(headers[:len(IDENTIFIER_HEADERS)]) == IDENTIFIER_HEADERS:
            matches.append((headers, table[1:]))
    if len(matches) != 1:
        raise ValueError(f"Expected one USDA PSD HTML table, found {len(matches)}")

    headers, rows = matches[0]
    years = headers[len(IDENTIFIER_HEADERS):-1]
    if headers[-1] != UNIT_HEADER or not years or len(years) != len(set(years)):
        raise ValueError("Expected unique market-year columns and one final unit column")
    for year in years:
        match = YEAR_LABEL.fullmatch(year)
        if not match or int(match[2]) != int(match[1]) + 1:
            raise ValueError(f"Invalid market-year column: {year!r}")
    if not rows:
        raise ValueError("The USDA PSD table has no data rows")
    return headers, rows


def build_manifest(path: Path, source_bytes: bytes, retrieved_on: date) -> dict:
    """Describe the original export and identify it by its full SHA-256."""
    headers, rows = parse_export(source_bytes)
    return {
        "retrieved_on": retrieved_on.isoformat(),
        "source_url": SOURCE_URL,
        "source_file": path.name,
        "source_format": "html",
        "source_size_bytes": len(source_bytes),
        "source_sha256": hashlib.sha256(source_bytes).hexdigest(),
        "query": {"column": "Year", "include_codes": True},
        "columns": headers,
        "commodities": sorted({row[1].strip() for row in rows if len(row) > 1}),
        "market_years": sorted(headers[len(IDENTIFIER_HEADERS):-1]),
        "source_row_count": len(rows),
    }


def write_manifest(path: Path, manifest: dict) -> None:
    """Replace the manifest atomically so readers see a complete JSON document."""
    with NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as file:
        temporary_path = Path(file.name)
        try:
            json.dump(manifest, file, indent=2)
            file.write("\n")
            file.close()
            temporary_path.replace(path)
        finally:
            temporary_path.unlink(missing_ok=True)


def register(path: Path, retrieved_on: date, *, upload_s3: bool = False) -> Path:
    """Save source metadata, verify an existing batch, and optionally archive the export."""
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
            from botocore.exceptions import BotoCoreError, ClientError

            from src.acp.s3_upload import upload_raw_object

            try:
                result = upload_raw_object(
                    source_bytes,
                    bucket=S3_BUCKET,
                    region=S3_REGION,
                    key=f"raw/{DATASET}/{retrieved_on.isoformat()}/{path.name}",
                    content_type="application/octet-stream",
                )
            except (BotoCoreError, ClientError, ValueError) as error:
                raise ValueError(f"S3 upload failed: {error}") from error
            manifest["s3_uri"] = result.uri
            manifest.pop("s3_version_id", None)
            if result.version_id:
                manifest["s3_version_id"] = result.version_id
            write_manifest(destination, manifest)
            status = "Uploaded" if result.created else "Skipped (identical source)"
            print(f"{status}: {result.uri}")
    return destination


def main() -> None:
    """Register the export selected by command-line arguments."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", nargs="?", type=Path)
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
