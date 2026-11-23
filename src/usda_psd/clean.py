"""Validate and publish annual USDA PSD pistachio observations."""

from __future__ import annotations

import argparse
import json
import re
from collections import defaultdict
from datetime import date

from src.acp.database import json_value, query

from .config import DOWNLOAD_DATE, QUALITY_LOG_PATH, batch_dir
from .extract import IDENTIFIER_HEADERS
from .ingestion import read_manifest
from .quality_log import Finding, write_csv, write_log

COMMODITY_CODE = "577907"
COMMODITY_NAME = "Pistachios, Inshell Basis"
ATTRIBUTES = {
    "20": "Beginning Stocks",
    "28": "Production",
    "57": "Imports",
    "86": "Total Supply",
    "88": "Exports",
    "125": "Domestic Consumption",
    "176": "Ending Stocks",
    "178": "Total Distribution",
}
TOTALS = {
    "total_supply": ((20, 28, 57), 86),
    "total_distribution": ((88, 125, 176), 178),
    "supply_equals_distribution": ((86,), 178),
}
QUANTITY = re.compile(r"-?(?:[0-9]+|[0-9]{1,3}(?:,[0-9]{3})+)")
CSV_FIELDS = (
    "snapshot_id", "source_row", "commodity_code", "commodity_name", "product_basis",
    "geo_code", "geo_name", "geo_type", "market_year", "market_year_start",
    "attribute_id", "attribute_name", "quantity_mt", "value_status",
)


def parse_quantity(cell: str) -> int | None:
    """Parse an integer in metric tons and preserve blank cells as null."""
    value = cell.strip()
    if not value:
        return None
    if not QUANTITY.fullmatch(value):
        raise ValueError(f"invalid quantity {cell!r}")
    return int(value.replace(",", ""))


def read_rows(snapshot_id: str, expected_count: int) -> list[dict]:
    """Read the registered snapshot in source order and verify its row count."""
    if not re.fullmatch(r"[0-9a-f]{64}", snapshot_id):
        raise ValueError("Invalid USDA PSD snapshot SHA-256")
    rows = json.loads(query(f"""
        SELECT COALESCE(jsonb_agg(to_jsonb(source) ORDER BY source_row), '[]'::jsonb)
        FROM (
            SELECT source_row, payload FROM raw.usda_psd_rows
            WHERE snapshot_id = '{snapshot_id}'
        ) AS source;
    """))
    if len(rows) != expected_count:
        raise ValueError(f"Expected {expected_count} USDA PSD raw rows, found {len(rows)}. Run ingestion.")
    return rows


def build_clean(manifest: dict, raw_rows: list[dict], findings: list[Finding]) -> list[dict]:
    """Convert each wide row to yearly observations and track source anomalies."""
    headers = manifest["columns"]
    year_columns = headers[len(IDENTIFIER_HEADERS):-1]
    geography = {}
    seen = {}
    records = []
    issues = defaultdict(list)

    for row in raw_rows:
        source_row, cells = row["source_row"], row["payload"]
        if len(cells) != len(headers):
            raise ValueError(f"Raw row {source_row}: expected {len(headers)} cells, found {len(cells)}")
        code, commodity, attribute, metric, geo_code, geo_name = (
            cell.strip() for cell in cells[:len(IDENTIFIER_HEADERS)]
        )
        commodity_code = code.replace(",", "")
        if commodity_code != COMMODITY_CODE or commodity != COMMODITY_NAME:
            raise ValueError(f"Raw row {source_row}: unexpected commodity {code!r}, {commodity!r}")
        if ATTRIBUTES.get(attribute) != metric:
            raise ValueError(f"Raw row {source_row}: unexpected metric {attribute!r}, {metric!r}")
        if cells[-1].strip() != "(MT)":
            raise ValueError(f"Raw row {source_row}: expected (MT) unit, found {cells[-1]!r}")
        if not geo_code or not geo_name or geography.setdefault(geo_code, geo_name) != geo_name:
            raise ValueError(
                f"Raw row {source_row}: missing or conflicting geography {geo_code!r}, {geo_name!r}"
            )

        key = (geo_code, attribute)
        if key in seen:
            previous_row, previous_cells = seen[key]
            if cells != previous_cells:
                raise ValueError(f"Raw rows {previous_row} and {source_row}: conflicting data for {key}")
            issues["duplicate_rows"].append(f"row {source_row} repeats row {previous_row}")
            continue
        seen[key] = (source_row, cells)
        base = {
            "snapshot_id": manifest["source_sha256"],
            "source_row": source_row,
            "commodity_code": commodity_code,
            "commodity_name": commodity,
            "product_basis": "inshell",
            "geo_code": geo_code,
            "geo_name": geo_name,
            "geo_type": {"European Union": "region", "World": "world"}.get(geo_name, "individual"),
            "attribute_id": int(attribute),
            "attribute_name": metric,
        }
        for market_year, cell in zip(year_columns, cells[len(IDENTIFIER_HEADERS):-1]):
            location = f"row {source_row}, {geo_code}, {market_year}, {metric}"
            try:
                quantity_mt = parse_quantity(cell)
            except ValueError as error:
                raise ValueError(f"{location}: {error}") from error
            if "," in cell:
                issues["formatted_values"].append(f"{location}: {cell!r}")
            if quantity_mt is None:
                issues["missing_values"].append(location)
            elif quantity_mt == 0:
                issues["reported_zeros"].append(location)
            elif quantity_mt < 0:
                issues["negative_values"].append(f"{location}: {cell!r}")
            records.append({
                **base,
                "market_year": market_year,
                "market_year_start": int(market_year[:4]),
                "quantity_mt": quantity_mt,
                "value_status": "missing" if quantity_mt is None else "reported",
            })

    decisions = {
        "formatted_values": "Remove grouping commas and store integer metric tons.",
        "missing_values": "Keep null and skip equations that depend on the value.",
        "reported_zeros": "Keep source zeros as reported observations.",
        "negative_values": "Keep the signed source value and review its context.",
        "duplicate_rows": "Retain all raw rows and publish the first identical record.",
    }
    for check, decision in decisions.items():
        examples = issues[check]
        findings.append(Finding(
            "clean", check, "observed" if examples else "pass", "recoverable",
            len(examples), next(iter(examples), ""), decision,
        ))
    return records


def reconcile(records: list[dict], findings: list[Finding]) -> None:
    """Check supply and distribution totals for each geography and market year."""
    groups = defaultdict(dict)
    for record in records:
        key = (record["geo_code"], record["market_year_start"])
        groups[key][record["attribute_id"]] = record["quantity_mt"]

    zero_groups = []
    stock_gaps = []
    results = {check: defaultdict(list) for check in TOTALS}
    for (geo_code, year), metrics in groups.items():
        label = f"{geo_code} {year}/{year + 1}"
        if len(metrics) == len(ATTRIBUTES) and set(metrics.values()) == {0}:
            zero_groups.append(label)
        previous_end = groups.get((geo_code, year - 1), {}).get(176)
        current_start = metrics.get(20)
        if previous_end is not None and current_start is not None and previous_end != current_start:
            stock_gaps.append(f"{label}: previous ending {previous_end}, beginning {current_start}")
        for check, (parts, total) in TOTALS.items():
            values = [metrics.get(part) for part in parts]
            reported = metrics.get(total)
            if reported is None or None in values:
                results[check]["skipped"].append(label)
                continue
            calculated = sum(values)
            if calculated == reported:
                results[check]["pass"].append(label)
            else:
                results[check]["fail"].append(f"{label}: calculated {calculated}, reported {reported} MT")

    findings.append(Finding(
        "validate", "all_zero_groups", "observed" if zero_groups else "pass", "recoverable",
        len(zero_groups), next(iter(zero_groups), ""), "Keep the reported values and flag coverage.",
    ))
    findings.append(Finding(
        "validate", "stock_continuity", "observed" if stock_gaps else "pass", "recoverable",
        len(stock_gaps), next(iter(stock_gaps), ""), "Review gaps between adjacent market years.",
    ))
    for check, statuses in results.items():
        for status, decision in (
            ("pass", "Published totals agree."),
            ("skipped", "A required operand is unavailable."),
            ("fail", "Keep the previous clean data and review the source totals."),
        ):
            examples = statuses.get(status, [])
            if examples or status == "pass":
                severity = "recoverable" if status == "skipped" else "critical"
                findings.append(Finding(
                    "validate", check, status, severity, len(examples), next(iter(examples), ""), decision,
                ))
    if any(item.status == "fail" for item in findings):
        raise ValueError("USDA PSD supply and distribution reconciliation failed")


def publish(snapshot_id: str, records: list[dict]) -> None:
    """Replace one validated snapshot in a single PostgreSQL transaction."""
    fields = ", ".join(CSV_FIELDS)
    payload = json_value(records)
    query(f"""
        BEGIN;
        SELECT snapshot_id FROM raw.usda_psd_snapshots
        WHERE snapshot_id = '{snapshot_id}' FOR UPDATE;

        DELETE FROM clean.usda_psd_pistachios WHERE snapshot_id = '{snapshot_id}';
        INSERT INTO clean.usda_psd_pistachios ({fields})
        SELECT {fields}
        FROM jsonb_populate_recordset(NULL::clean.usda_psd_pistachios, {payload});
        COMMIT;
    """)


def run_clean(retrieved_on: date, *, export_csv: bool = False) -> int:
    """Validate one batch, publish it, and record findings even on failure."""
    findings = []
    manifest = {}
    phase = "read"
    try:
        manifest = read_manifest(retrieved_on)
        snapshot_id = manifest["source_sha256"]
        raw_rows = read_rows(snapshot_id, manifest["source_row_count"])
        phase = "clean"
        records = build_clean(manifest, raw_rows, findings)
        phase = "validate"
        reconcile(records, findings)
        phase = "publish"
        publish(snapshot_id, records)
        if export_csv:
            phase = "csv_export"
            published = json.loads(query(f"""
                SELECT jsonb_agg(to_jsonb(published) ORDER BY geo_code, market_year, attribute_id)
                FROM clean.usda_psd_pistachios AS published
                WHERE snapshot_id = '{snapshot_id}';
            """))
            path = batch_dir("clean", retrieved_on) / "pistachio_psd.csv"
            write_csv(path, CSV_FIELDS, published)
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
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--retrieved-on", type=date.fromisoformat, default=DOWNLOAD_DATE)
    parser.add_argument("--export-csv", action="store_true", help="Also export the PostgreSQL clean rows")
    args = parser.parse_args()
    try:
        count = run_clean(args.retrieved_on, export_csv=args.export_csv)
    except (OSError, ValueError, RuntimeError, KeyError) as error:
        parser.exit(1, f"{error}\n")
    print(f"USDA PSD clean observations: {count}")


if __name__ == "__main__":
    main()
