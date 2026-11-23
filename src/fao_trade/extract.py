"""Register the FAOSTAT trade archive and extract its pistachio records.

The Detailed Trade Matrix is an 8.5 GB CSV covering every agricultural item.
This step streams it out of the zip without unpacking it, keeps each record
whose Item Code is 223 (Pistachios, in shell) with its cells unchanged, and
copies the reporter, partner, and flag code lists. The manifest identifies the
archive and every extracted file by SHA-256 and records how many source
records were scanned and kept, so the scope filter can be audited.

Run from the repository root:
    python -m src.fao_trade.extract [archive] [--retrieved-on YYYY-MM-DD] [--upload-s3]
"""

from __future__ import annotations

import argparse
import csv
import fcntl
import hashlib
import io
import json
import zipfile
from datetime import date
from pathlib import Path

from src.usda_psd.extract import write_manifest
from src.usda_psd.quality_log import write_csv

from .config import (
    ARCHIVE_FILE, CODE_LIST_FILES, DATASET, DOWNLOAD_DATE, EXTRACT_FILE, ITEM_CODE,
    S3_BUCKET, S3_REGION, SOURCE_URL, TRADE_FILE, batch_dir,
)

TRADE_COLUMNS = (
    "Reporter Country Code", "Reporter Country Code (M49)", "Reporter Countries",
    "Partner Country Code", "Partner Country Code (M49)", "Partner Countries",
    "Item Code", "Item Code (CPC)", "Item", "Element Code", "Element",
    "Year Code", "Year", "Unit", "Value", "Flag",
)
ITEM_INDEX = TRADE_COLUMNS.index("Item Code")
CHUNK_SIZE = 1 << 20


class HashingReader(io.RawIOBase):
    """Pass bytes through from a binary stream while computing their SHA-256.

    Wrapping the zip member lets one pass over the 8.5 GB file both parse the
    CSV and fingerprint the exact source bytes.
    """

    def __init__(self, stream) -> None:
        super().__init__()
        self.stream = stream
        self.digest = hashlib.sha256()
        self.size = 0

    def readable(self) -> bool:
        return True

    def readinto(self, buffer) -> int:
        data = self.stream.read(len(buffer))
        buffer[:len(data)] = data
        self.digest.update(data)
        self.size += len(data)
        return len(data)


def file_sha256(path: Path) -> str:
    """Hash a file in chunks so large archives never load fully into memory."""
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(CHUNK_SIZE), b""):
            digest.update(chunk)
    return digest.hexdigest()


def find_members(archive: zipfile.ZipFile) -> dict[str, zipfile.ZipInfo]:
    """Map each required file name to its archive entry, ignoring macOS metadata."""
    wanted = {TRADE_FILE, *CODE_LIST_FILES.values()}
    found = {}
    for info in archive.infolist():
        name = Path(info.filename).name
        if name not in wanted or info.filename.startswith("__MACOSX/"):
            continue
        if name in found:
            raise ValueError(f"Archive contains {name} more than once")
        found[name] = info
    missing = wanted - found.keys()
    if missing:
        raise ValueError(f"Archive is missing {sorted(missing)}")
    return found


def extract_trade_rows(archive: zipfile.ZipFile, info: zipfile.ZipInfo, destination: Path) -> dict:
    """Stream the trade matrix, write its pistachio records, and describe the scan.

    Records are filtered by parsing the Item Code column, never by text search.
    ``source_row`` counts data records after the header, so each extracted row
    points back to its position in the original file.
    """
    kept = []
    total = 0
    with archive.open(info) as member:
        hashing = HashingReader(member)
        text = io.TextIOWrapper(io.BufferedReader(hashing, CHUNK_SIZE), encoding="utf-8", newline="")
        reader = csv.reader(text)
        header = tuple(next(reader, ()))
        if header != TRADE_COLUMNS:
            raise ValueError(f"Unexpected trade matrix header: {header}")
        for total, cells in enumerate(reader, start=1):
            if len(cells) != len(TRADE_COLUMNS):
                raise ValueError(f"Source row {total}: expected {len(TRADE_COLUMNS)} cells, found {len(cells)}")
            if cells[ITEM_INDEX] == ITEM_CODE:
                kept.append({"source_row": total, **dict(zip(TRADE_COLUMNS, cells))})
    if not kept:
        raise ValueError(f"No records with Item Code {ITEM_CODE}")
    write_csv(destination, ("source_row", *TRADE_COLUMNS), kept)
    return {
        "source_file": TRADE_FILE,
        "source_size_bytes": hashing.size,
        "source_sha256": hashing.digest.hexdigest(),
        "columns": list(TRADE_COLUMNS),
        "source_row_count": total,
        "item_code": ITEM_CODE,
        "item_row_count": len(kept),
        "extract_file": destination.name,
        "extract_sha256": file_sha256(destination),
    }


def extract_code_lists(archive: zipfile.ZipFile, members: dict, directory: Path) -> dict:
    """Copy each code list byte for byte and record its columns, size, and digest."""
    code_lists = {}
    for name, file_name in CODE_LIST_FILES.items():
        data = archive.read(members[file_name])
        rows = list(csv.reader(io.StringIO(data.decode("utf-8"), newline="")))
        if len(rows) < 2:
            raise ValueError(f"{file_name} contains no codes")
        (directory / file_name).write_bytes(data)
        code_lists[name] = {
            "file": file_name,
            "sha256": hashlib.sha256(data).hexdigest(),
            "columns": rows[0],
            "row_count": len(rows) - 1,
        }
    return code_lists


def upload_archive(path: Path, retrieved_on: date, manifest: dict) -> None:
    """Archive the original zip on S3 and record its location in the manifest."""
    from botocore.exceptions import BotoCoreError, ClientError

    from src.acp.s3_upload import upload_raw_object

    try:
        result = upload_raw_object(
            path.read_bytes(),
            bucket=S3_BUCKET,
            region=S3_REGION,
            key=f"raw/{DATASET}/{retrieved_on.isoformat()}/{path.name}",
            content_type="application/zip",
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
    """Extract one archive into its batch directory and write the manifest.

    A batch directory belongs to one archive: rerunning with a different file
    for the same download date fails instead of overwriting the earlier batch.
    """
    directory = batch_dir("extract", retrieved_on)
    directory.mkdir(parents=True, exist_ok=True)
    destination = directory / "manifest.json"
    with destination.with_suffix(".lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        previous = json.loads(destination.read_text(encoding="utf-8")) if destination.exists() else {}
        archive_sha256 = file_sha256(path)
        if previous and previous["archive_sha256"] != archive_sha256:
            raise ValueError(f"{destination} already describes a different archive")
        with zipfile.ZipFile(path) as archive:
            members = find_members(archive)
            manifest = {
                "retrieved_on": retrieved_on.isoformat(),
                "source_url": SOURCE_URL,
                "archive_file": path.name,
                "archive_size_bytes": path.stat().st_size,
                "archive_sha256": archive_sha256,
                **extract_trade_rows(archive, members[TRADE_FILE], directory / EXTRACT_FILE),
                "code_lists": extract_code_lists(archive, members, directory),
            }
        for field in ("s3_uri", "s3_version_id"):
            if field in previous:
                manifest[field] = previous[field]
        write_manifest(destination, manifest)
        if upload_s3:
            upload_archive(path, retrieved_on, manifest)
            write_manifest(destination, manifest)
    return destination


def main() -> None:
    """Register the archive selected by command-line arguments."""
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("source", nargs="?", type=Path, help="Path to the FAOSTAT zip")
    parser.add_argument("--retrieved-on", type=date.fromisoformat, default=DOWNLOAD_DATE)
    parser.add_argument("--upload-s3", action="store_true", help="Archive the original zip on S3")
    args = parser.parse_args()
    path = args.source or batch_dir("raw", args.retrieved_on) / ARCHIVE_FILE
    try:
        print(register(path, args.retrieved_on, upload_s3=args.upload_s3))
    except (OSError, ValueError, KeyError, ImportError, zipfile.BadZipFile) as error:
        parser.exit(1, f"{error}\n")


if __name__ == "__main__":
    main()
