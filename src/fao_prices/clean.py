"""Validate FAOSTAT pistachio producer prices and publish the clean table.

Reads one raw snapshot and runs critical checks on every row. Any critical
failure stops publication after all checks have run, so one run reports every
problem. Each row becomes one country and year with its price in USD per tonne
as reported and in USD per kg, which matches the FAOSTAT trade unit values.
Recoverable issues are kept and logged, and the result is reconciled against
the raw rows before it replaces the published snapshot. Every check, passing
or not, is written to the quality log.

Run from the repository root:
    python -m src.fao_prices.clean [--retrieved-on YYYY-MM-DD] [--export-csv]
"""

from __future__ import annotations

import argparse
import json
import re
from collections import defaultdict
from datetime import date
from decimal import Decimal

from src.acp.database import json_value, query
from src.fao_trade.clean import decimal_text, observe, require
from src.usda_psd.quality_log import Finding, write_csv, write_log

from .config import CLEAN_FILE, DOWNLOAD_DATE, QUALITY_LOG_PATH, batch_dir
from .extract import COLUMNS
from .ingestion import read_manifest

# Every row must carry these values; the export holds one annual price series.
FIXED_FIELDS = {
    "Domain Code": "PP",
    "Domain": "Producer Prices",
    "Element Code": "5532",
    "Element": "Producer Price (USD/tonne)",
    "Item Code (CPC)": "01375",
    "Item": "Pistachios, in shell",
    "Months Code": "7021",
    "Months": "Annual value",
    "Unit": "USD",
}
VALUE = re.compile(r"[0-9]+(?:\.[0-9]+)?")
YEAR = re.compile(r"[12][0-9]{3}")
M49 = re.compile(r"[0-9]{3}")
MIN_YEARS = 5
CSV_FIELDS = (
    "snapshot_id", "source_row", "year", "area_m49", "area_name", "item_cpc", "product_basis",
    "producer_price_usd_per_tonne", "producer_price_usd_per_kg", "flag", "same_as_previous_year",
)
CRITICAL_CHECKS = {
    "row_shape": "Stop publication if a row lacks the 16 source columns.",
    "fixed_fields": "Stop publication unless every row is the annual USD per tonne price of in-shell pistachios.",
    "year": "Stop publication unless Year is a four-digit year equal to Year Code.",
    "value": "Stop publication unless Value is a positive number.",
    "area_codes": "Stop publication unless each M49 code has three digits and one area name.",
    "flag_descriptions": "Stop publication if one flag has more than one description.",
    "duplicate_keys": "Stop publication if an area and year appear twice.",
}


def read_rows(snapshot_id: str, expected_count: int) -> list[dict]:
    """Read the registered snapshot in source order and verify its row count."""
    if not re.fullmatch(r"[0-9a-f]{64}", snapshot_id):
        raise ValueError("Invalid FAO price snapshot SHA-256")
    rows = json.loads(query(f"""
        SELECT COALESCE(jsonb_agg(to_jsonb(source) ORDER BY source_row), '[]'::jsonb)
        FROM (
            SELECT source_row, payload FROM raw.fao_prices_rows
            WHERE snapshot_id = '{snapshot_id}'
        ) AS source;
    """))
    if len(rows) != expected_count:
        raise ValueError(f"Expected {expected_count} FAO price raw rows, found {len(rows)}. Run ingestion.")
    return rows


def check_raw(rows: list[dict], findings: list[Finding]) -> list[dict]:
    """Run the critical checks on every raw row and return typed observations."""
    problems = defaultdict(list)
    area_names = {}
    flag_descriptions = {}
    seen = {}
    observations = []

    for row in rows:
        source_row, cells = row["source_row"], row["payload"]
        where = f"row {source_row}"
        if len(cells) != len(COLUMNS):
            problems["row_shape"].append(f"{where}: {len(cells)} cells")
            continue
        cell = dict(zip(COLUMNS, cells))
        unexpected = [f"{name} {cell[name]!r}" for name, value in FIXED_FIELDS.items() if cell[name] != value]
        if unexpected:
            problems["fixed_fields"].append(f"{where}: {', '.join(unexpected)}")
        if not YEAR.fullmatch(cell["Year"]) or cell["Year Code"] != cell["Year"]:
            problems["year"].append(f"{where}: Year {cell['Year']!r}, Year Code {cell['Year Code']!r}")
            continue
        if not VALUE.fullmatch(cell["Value"]) or Decimal(cell["Value"]) == 0:
            problems["value"].append(f"{where}: {cell['Value']!r}")
            continue
        m49, area = cell["Area Code (M49)"], cell["Area"]
        if not M49.fullmatch(m49) or area_names.setdefault(m49, area) != area:
            problems["area_codes"].append(f"{where}: {m49!r} {area!r}")
        flag, description = cell["Flag"], cell["Flag Description"]
        if flag_descriptions.setdefault(flag, description) != description:
            problems["flag_descriptions"].append(f"{where}: {flag!r} {description!r}")

        key = (m49, int(cell["Year"]))
        if key in seen:
            problems["duplicate_keys"].append(f"rows {seen[key]} and {source_row}: {key}")
            continue
        seen[key] = source_row
        observations.append({
            "source_row": source_row, "year": key[1], "m49": m49, "area": area,
            "price": Decimal(cell["Value"]), "flag": flag,
        })

    for check, decision in CRITICAL_CHECKS.items():
        require(findings, "pre_clean", check, problems[check], decision)
    if any(item.status == "fail" for item in findings):
        raise ValueError("FAO price raw quality checks failed")
    return observations


def build_clean(manifest: dict, observations: list[dict], findings: list[Finding]) -> list[dict]:
    """Convert each observation to a clean record and log recoverable issues.

    ``same_as_previous_year`` marks a price identical to the same area's price
    in the prior calendar year, which may be a carried-forward figure.
    """
    prices = {(obs["m49"], obs["year"]): obs["price"] for obs in observations}
    records = []
    repeated = []
    for obs in sorted(observations, key=lambda item: (item["m49"], item["year"])):
        same = prices.get((obs["m49"], obs["year"] - 1)) == obs["price"]
        if same:
            repeated.append(f"{obs['area']} {obs['year']}: {decimal_text(obs['price'])} USD/tonne")
        records.append({
            "snapshot_id": manifest["source_sha256"],
            "source_row": obs["source_row"],
            "year": obs["year"],
            "area_m49": obs["m49"],
            "area_name": obs["area"],
            "item_cpc": FIXED_FIELDS["Item Code (CPC)"],
            "product_basis": "inshell",
            "producer_price_usd_per_tonne": obs["price"],
            "producer_price_usd_per_kg": obs["price"].scaleb(-3),
            "flag": obs["flag"],
            "same_as_previous_year": same,
        })

    years = defaultdict(list)
    for record in records:
        years[record["area_name"]].append(record["year"])
    latest = max(record["year"] for record in records)
    sparse = [f"{area}: {len(found)} years" for area, found in sorted(years.items()) if len(found) < MIN_YEARS]
    ends_early = [f"{area}: last year {max(found)}" for area, found in sorted(years.items()) if max(found) < latest]

    findings.append(Finding(
        "clean", "byte_order_mark", "applied" if manifest["source_has_bom"] else "pass", "recoverable",
        int(manifest["source_has_bom"]), "UTF-8 BOM before 'Domain Code'" if manifest["source_has_bom"] else "",
        "Keep the file unchanged and remove the BOM only while parsing.",
    ))
    findings.append(Finding(
        "clean", "unit_label", "observed", "recoverable", len(records),
        f"Unit {FIXED_FIELDS['Unit']!r}, Element {FIXED_FIELDS['Element']!r}",
        "Read values as USD per tonne, as the element states.",
    ))
    observe(findings, "clean", "repeated_values", repeated,
            "Keep the price and set same_as_previous_year for review.")
    observe(findings, "clean", "sparse_coverage", sparse,
            f"Keep the prices. Areas with fewer than {MIN_YEARS} years cannot support trend analysis.")
    observe(findings, "clean", "series_ends_early", ends_early,
            f"Keep the series. It has no price for the latest year, {latest}.")
    return records


def reconcile(records: list[dict], rows: list[dict], findings: list[Finding]) -> None:
    """Check that the clean table carries every raw price exactly once."""
    raw_total = sum(Decimal(dict(zip(COLUMNS, row["payload"]))["Value"]) for row in rows)
    clean_total = sum(record["producer_price_usd_per_tonne"] for record in records)
    mismatches = []
    if len(records) != len(rows):
        mismatches.append(f"clean {len(records)} rows, raw {len(rows)} rows")
    if clean_total != raw_total:
        mismatches.append(f"clean total {clean_total}, raw total {raw_total} USD/tonne")
    require(findings, "validate", "rows_and_totals_match_raw", mismatches,
            "Stop publication unless the clean row count and price total equal the raw rows.")
    if mismatches:
        raise ValueError("FAO price clean table does not reconcile with raw rows")


def publish(snapshot_id: str, records: list[dict]) -> None:
    """Replace one validated snapshot in a single PostgreSQL transaction."""
    fields = ", ".join(CSV_FIELDS)
    payload = json_value([
        {key: decimal_text(value) if isinstance(value, Decimal) else value for key, value in record.items()}
        for record in records
    ])
    query(f"""
        BEGIN;
        SELECT snapshot_id FROM raw.fao_prices_snapshots
        WHERE snapshot_id = '{snapshot_id}' FOR UPDATE;

        DELETE FROM clean.fao_pistachio_producer_prices WHERE snapshot_id = '{snapshot_id}';
        INSERT INTO clean.fao_pistachio_producer_prices ({fields})
        SELECT {fields}
        FROM jsonb_populate_recordset(NULL::clean.fao_pistachio_producer_prices, {payload});
        COMMIT;
    """)


def export_csv(snapshot_id: str, retrieved_on: date) -> None:
    """Write the published snapshot from PostgreSQL to the clean CSV."""
    published = json.loads(query(f"""
        SELECT jsonb_agg(to_jsonb(published) ORDER BY area_name, year)
        FROM clean.fao_pistachio_producer_prices AS published
        WHERE snapshot_id = '{snapshot_id}';
    """), parse_float=Decimal)
    write_csv(batch_dir("clean", retrieved_on) / CLEAN_FILE, CSV_FIELDS, published)


def run_clean(retrieved_on: date, *, export: bool = False) -> int:
    """Validate one batch, publish it, and record findings even on failure."""
    findings = []
    manifest = {}
    phase = "read"
    try:
        manifest = read_manifest(retrieved_on)
        snapshot_id = manifest["source_sha256"]
        rows = read_rows(snapshot_id, manifest["source_row_count"])
        phase = "pre_clean"
        observations = check_raw(rows, findings)
        phase = "clean"
        records = build_clean(manifest, observations, findings)
        phase = "validate"
        reconcile(records, rows, findings)
        phase = "publish"
        publish(snapshot_id, records)
        if export:
            phase = "csv_export"
            export_csv(snapshot_id, retrieved_on)
        return len(records)
    except (OSError, ValueError, RuntimeError, KeyError) as error:
        decision = (
            "The database update committed. Retry the CSV export."
            if phase == "csv_export" else "Review this batch before publishing clean data."
        )
        findings.append(Finding(phase, "pipeline_error", "fail", "critical", 1, str(error), decision))
        raise
    finally:
        write_log(QUALITY_LOG_PATH, retrieved_on.isoformat(), manifest, findings)


def main() -> None:
    """Publish the clean table for a download date and optionally export a CSV."""
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--retrieved-on", type=date.fromisoformat, default=DOWNLOAD_DATE)
    parser.add_argument("--export-csv", action="store_true", help="Also export the PostgreSQL clean rows")
    args = parser.parse_args()
    try:
        count = run_clean(args.retrieved_on, export=args.export_csv)
    except (OSError, ValueError, RuntimeError, KeyError) as error:
        parser.exit(1, f"{error}\n")
    print(f"FAO producer price clean rows: {count}")


if __name__ == "__main__":
    main()
