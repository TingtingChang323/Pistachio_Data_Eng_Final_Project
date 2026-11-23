"""Load the extracted FAOSTAT pistachio records and code lists into PostgreSQL.

Reads the files written by extract.py, verifies each one against the SHA-256
recorded in the manifest, and loads them into the raw.fao_trade_* tables in a
single transaction. Cells stay exactly as printed in the source, for example
"'840" and "92063.290000". Reloading the same snapshot adds nothing, and any
difference from rows already loaded makes the whole load fail.

Run from the repository root:
    python -m src.fao_trade.ingestion [--retrieved-on YYYY-MM-DD]
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
from datetime import date
from pathlib import Path

from src.acp.database import json_value, query

from .config import DOWNLOAD_DATE, batch_dir


def read_manifest(retrieved_on: date) -> dict:
    """Read the extraction manifest for a download date."""
    path = batch_dir("extract", retrieved_on) / "manifest.json"
    if not path.is_file():
        raise ValueError(f"Manifest not found: {path}. Run FAO extract first.")
    manifest = json.loads(path.read_text(encoding="utf-8"))
    if manifest["retrieved_on"] != retrieved_on.isoformat():
        raise ValueError(f"Download date does not match {path}")
    return manifest


def read_verified_csv(path: Path, sha256: str) -> list[list[str]]:
    """Return a CSV's rows only if its bytes still match the extraction digest."""
    data = path.read_bytes()
    if hashlib.sha256(data).hexdigest() != sha256:
        raise ValueError(f"{path} changed after extraction. Rerun FAO extract.")
    return list(csv.reader(io.StringIO(data.decode("utf-8"), newline="")))


def read_extract(manifest: dict, directory: Path) -> tuple[list, list]:
    """Read the pistachio records and code lists and check them against the manifest.

    Returns ``[source_row, cells]`` pairs for the trade records and
    ``[code_list, source_row, cells]`` triples for the code lists, both in the
    order PostgreSQL will return them, so the load can be compared exactly.
    """
    header, *rows = read_verified_csv(directory / manifest["extract_file"], manifest["extract_sha256"])
    if header != ["source_row", *manifest["columns"]] or len(rows) != manifest["item_row_count"]:
        raise ValueError("Extracted pistachio rows do not match the manifest")
    trade = [[int(row[0]), row[1:]] for row in rows]

    codes = []
    for name, entry in sorted(manifest["code_lists"].items()):
        header, *entries = read_verified_csv(directory / entry["file"], entry["sha256"])
        if header != entry["columns"] or len(entries) != entry["row_count"]:
            raise ValueError(f"Code list {name} does not match the manifest")
        codes.extend([name, number, cells] for number, cells in enumerate(entries, start=1))
    return trade, codes


def ingest(retrieved_on: date) -> dict[str, int]:
    """Load one extracted batch atomically and confirm the stored rows match it."""
    manifest = read_manifest(retrieved_on)
    trade, codes = read_extract(manifest, batch_dir("extract", retrieved_on))
    snapshot_id = manifest["source_sha256"]
    query(f"""
        BEGIN;
        CREATE TEMP TABLE assertions (ok boolean NOT NULL CHECK (ok));
        CREATE TEMP TABLE incoming ON COMMIT DROP AS
        SELECT {json_value(trade)} AS trade, {json_value(codes)} AS codes;

        INSERT INTO raw.fao_trade_snapshots AS stored (snapshot_id, manifest)
        VALUES ('{snapshot_id}', {json_value(manifest)})
        ON CONFLICT (snapshot_id) DO UPDATE SET manifest = stored.manifest || EXCLUDED.manifest
        WHERE stored.manifest->>'retrieved_on' = EXCLUDED.manifest->>'retrieved_on';

        SELECT snapshot_id FROM raw.fao_trade_snapshots
        WHERE snapshot_id = '{snapshot_id}' FOR UPDATE;

        INSERT INTO raw.fao_trade_rows (snapshot_id, source_row, payload)
        SELECT '{snapshot_id}', (item->>0)::integer, item->1
        FROM incoming, jsonb_array_elements(incoming.trade) AS source(item)
        ON CONFLICT (snapshot_id, source_row) DO NOTHING;

        INSERT INTO raw.fao_trade_code_lists (snapshot_id, code_list, source_row, payload)
        SELECT '{snapshot_id}', item->>0, (item->>1)::integer, item->2
        FROM incoming, jsonb_array_elements(incoming.codes) AS source(item)
        ON CONFLICT (snapshot_id, code_list, source_row) DO NOTHING;

        INSERT INTO assertions
        SELECT COALESCE(jsonb_agg(jsonb_build_array(source_row, payload) ORDER BY source_row), '[]')
               = (SELECT trade FROM incoming)
        FROM raw.fao_trade_rows WHERE snapshot_id = '{snapshot_id}';

        INSERT INTO assertions
        SELECT COALESCE(jsonb_agg(jsonb_build_array(code_list, source_row, payload)
                                  ORDER BY code_list COLLATE "C", source_row), '[]')
               = (SELECT codes FROM incoming)
        FROM raw.fao_trade_code_lists WHERE snapshot_id = '{snapshot_id}';
        COMMIT;
    """)
    return {"trade_rows": len(trade), "code_list_rows": len(codes)}


def main() -> None:
    """Load the batch for the supplied download date."""
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--retrieved-on", type=date.fromisoformat, default=DOWNLOAD_DATE)
    args = parser.parse_args()
    try:
        counts = ingest(args.retrieved_on)
    except (OSError, ValueError, RuntimeError, KeyError) as error:
        parser.exit(1, f"{error}\n")
    print(f"FAO raw rows: {counts['trade_rows']}, code list rows: {counts['code_list_rows']}")


if __name__ == "__main__":
    main()
