"""Load the original FAOSTAT producer price rows into PostgreSQL.

Verifies the export against its manifest, then loads every data row into
raw.fao_prices_rows in a single transaction. Each row is stored as an ordered
array of its 16 source cells, exactly as printed. ``source_row`` counts data
rows after the header. Reloading the same snapshot adds nothing, and any
difference from rows already loaded makes the whole load fail.

Run from the repository root:
    python -m src.fao_prices.ingestion [source] [--retrieved-on YYYY-MM-DD]
"""

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
        raise ValueError(f"Manifest not found: {path}. Run FAO prices extract first.")
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
        raise ValueError(f"{path} does not match the registered FAOSTAT price export")
    rows = parse_export(source_bytes)
    if len(rows) != manifest["source_row_count"]:
        raise ValueError("FAOSTAT price export row count does not match the manifest")

    payload = json_value(rows)
    query(f"""
        BEGIN;
        CREATE TEMP TABLE assertions (ok boolean NOT NULL CHECK (ok));

        INSERT INTO raw.fao_prices_snapshots AS stored (snapshot_id, manifest)
        VALUES ('{snapshot_id}', {json_value(manifest)})
        ON CONFLICT (snapshot_id) DO UPDATE SET manifest = stored.manifest || EXCLUDED.manifest
        WHERE stored.manifest->>'retrieved_on' = EXCLUDED.manifest->>'retrieved_on';

        SELECT snapshot_id FROM raw.fao_prices_snapshots
        WHERE snapshot_id = '{snapshot_id}' FOR UPDATE;

        INSERT INTO raw.fao_prices_rows (snapshot_id, source_row, payload)
        SELECT '{snapshot_id}', source_row, cells
        FROM jsonb_array_elements({payload}) WITH ORDINALITY AS source(cells, source_row)
        ON CONFLICT (snapshot_id, source_row) DO NOTHING;

        INSERT INTO assertions
        SELECT COALESCE(jsonb_agg(payload ORDER BY source_row), '[]') = {payload}
        FROM raw.fao_prices_rows
        WHERE snapshot_id = '{snapshot_id}';
        COMMIT;
    """)
    return len(rows)


def main() -> None:
    """Load the export corresponding to the supplied download date."""
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("source", nargs="?", type=Path, help="Path to the FAOSTAT CSV export")
    parser.add_argument("--retrieved-on", type=date.fromisoformat, default=DOWNLOAD_DATE)
    args = parser.parse_args()
    try:
        count = ingest(args.retrieved_on, args.source)
    except (OSError, ValueError, RuntimeError, KeyError) as error:
        parser.exit(1, f"{error}\n")
    print(f"FAO producer price raw rows: {count}")


if __name__ == "__main__":
    main()
