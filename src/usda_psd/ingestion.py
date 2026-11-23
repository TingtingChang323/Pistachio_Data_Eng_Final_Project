"""Load the original USDA PSD table rows into PostgreSQL."""

from __future__ import annotations

import argparse
import hashlib
import json
from datetime import date
from pathlib import Path

from src.acp.database import json_value, query

from .config import DOWNLOAD_DATE, batch_dir
from .extract import parse_export


def read_manifest(retrieved_on: date) -> dict:
    """Read the local registration for a download date."""
    path = batch_dir("extract", retrieved_on) / "manifest.json"
    if not path.is_file():
        raise ValueError(f"Manifest not found: {path}. Run USDA extract first.")
    manifest = json.loads(path.read_text(encoding="utf-8"))
    if manifest["retrieved_on"] != retrieved_on.isoformat():
        raise ValueError(f"Download date does not match {path}")
    return manifest


def ingest(retrieved_on: date, source: Path | None = None) -> int:
    """Verify the registered export, then load and check the raw rows atomically."""
    manifest = read_manifest(retrieved_on)
    path = source or batch_dir("raw", retrieved_on) / manifest["source_file"]
    source_bytes = path.read_bytes()
    snapshot_id = hashlib.sha256(source_bytes).hexdigest()
    if snapshot_id != manifest["source_sha256"] or path.name != manifest["source_file"]:
        raise ValueError(f"{path} does not match the registered USDA source")
    headers, rows = parse_export(source_bytes)
    if headers != manifest["columns"] or len(rows) != manifest["source_row_count"]:
        raise ValueError("USDA PSD table shape does not match the manifest")

    metadata = json_value(manifest)
    payload = json_value(rows)
    query(f"""
        BEGIN;
        CREATE TEMP TABLE assertions (ok boolean NOT NULL CHECK (ok));

        INSERT INTO raw.usda_psd_snapshots AS stored (snapshot_id, manifest)
        VALUES ('{snapshot_id}', {metadata})
        ON CONFLICT (snapshot_id) DO UPDATE SET manifest = stored.manifest || EXCLUDED.manifest
        WHERE stored.manifest->>'retrieved_on' = EXCLUDED.manifest->>'retrieved_on';

        SELECT snapshot_id FROM raw.usda_psd_snapshots
        WHERE snapshot_id = '{snapshot_id}' FOR UPDATE;

        INSERT INTO raw.usda_psd_rows (snapshot_id, source_row, payload)
        SELECT '{snapshot_id}', source_row, cells
        FROM jsonb_array_elements({payload}) WITH ORDINALITY AS source(cells, source_row)
        ON CONFLICT (snapshot_id, source_row) DO NOTHING;

        INSERT INTO assertions
        SELECT jsonb_agg(payload ORDER BY source_row) = {payload}
        FROM raw.usda_psd_rows
        WHERE snapshot_id = '{snapshot_id}';
        COMMIT;
    """)
    return len(rows)


def main() -> None:
    """Load the export corresponding to the supplied download date."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", nargs="?", type=Path)
    parser.add_argument("--retrieved-on", type=date.fromisoformat, default=DOWNLOAD_DATE)
    args = parser.parse_args()
    try:
        count = ingest(args.retrieved_on, args.source)
    except (OSError, ValueError, RuntimeError, KeyError) as error:
        parser.exit(1, f"{error}\n")
    print(f"USDA PSD raw rows: {count}")


if __name__ == "__main__":
    main()
