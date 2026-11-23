"""Register an existing ACP PDF and write its source manifest."""

import argparse
import hashlib
import json
import re
from io import BytesIO
from pathlib import Path

import pdfplumber

if __package__:
    from .config import EXTRACT_DIR, PDF_PATH
else:
    from config import EXTRACT_DIR, PDF_PATH

MONTH_NAMES = {"2026-09": "September", "2026-10": "October"}
SOURCE_PAGE = "https://acpistachios.org/industry-resources/shipment-reports/"


def build_manifest(filename, pdf_bytes, source_url=None, retrieved_at=None):
    """Read report metadata and record the identity of the untouched PDF."""
    with pdfplumber.open(BytesIO(pdf_bytes)) as pdf:
        heading = pdf.pages[0].extract_text()
    month = next((key for key, name in MONTH_NAMES.items()
                  if f"{name} 2026 Year to Date Summary - Pounds" in heading), None)
    crop_year = re.search(r"\b(\d{4})-(\d{4}) Crop Year\b", heading)
    if not month or not crop_year:
        raise ValueError("Unrecognized ACP report heading or crop year")
    return {
        "report_month": month, "crop_year": "/".join(crop_year.groups()),
        "source_page": SOURCE_PAGE, "source_url": source_url,
        "source_file": filename, "source_size_bytes": len(pdf_bytes),
        "source_sha256": hashlib.sha256(pdf_bytes).hexdigest(),
        "retrieved_at": retrieved_at,
    }


def main():
    """Write a local manifest and optionally upload its original PDF."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("pdf", nargs="?", type=Path, default=PDF_PATH)
    parser.add_argument("--source-url")
    parser.add_argument("--retrieved-at", help="Download timestamp, if recorded")
    parser.add_argument("--upload-s3", action="store_true", help="Upload the original PDF to S3")
    args = parser.parse_args()
    pdf_bytes = args.pdf.read_bytes()
    manifest = build_manifest(args.pdf.name, pdf_bytes)
    path = EXTRACT_DIR / manifest["report_month"] / "manifest.json"
    previous = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    if previous.get("source_sha256") != manifest["source_sha256"]:
        previous = {}
    manifest["source_url"] = args.source_url or previous.get("source_url")
    manifest["retrieved_at"] = args.retrieved_at or previous.get("retrieved_at")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    print(path)

    if args.upload_s3:
        from botocore.exceptions import BotoCoreError, ClientError

        if __package__:
            from .s3_upload import upload_pdf
        else:
            from s3_upload import upload_pdf

        try:
            print(upload_pdf(pdf_bytes, args.pdf.name, manifest["report_month"]))
        except (BotoCoreError, ClientError, ValueError) as error:
            parser.exit(1, f"S3 upload failed: {error}\n")


if __name__ == "__main__":
    main()
