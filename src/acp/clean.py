"""Assess ACP raw records, clean them, reconcile totals, and publish to PostgreSQL."""

import argparse
import csv
import json
import re
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

from config import CLEAN_CSV_DIR, QUALITY_LOG_PATH, REPORT_MONTH
from database import RAW_TABLES, json_value, query
from ingestion import FORMS
from quality_log import write_log

NUMBER = re.compile(r"(?:-?[0-9]+|-?[0-9]{1,3}(?:,[0-9]{3})+|\((?:[0-9]+|[0-9]{1,3}(?:,[0-9]{3})+)\))")
DETAIL_TYPES = ("country", "other_region")


def number(value):
    """Parse a PDF weight as a signed integer, preserving nulls."""
    if value is None:
        return None
    if not isinstance(value, str) or not NUMBER.fullmatch(value):
        raise ValueError(f"Invalid weight: {value!r}")
    if value.startswith("("):
        value = "-" + value[1:-1]
    return int(value.replace(",", ""))


def write_csv(path, fields, rows):
    """Write cleaned dictionaries to CSV in the given column order."""
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def check_destination_totals(raw_rows):
    """Confirm blank detail cells can be read as zero using row and region totals."""
    by_region = defaultdict(lambda: [0] * len(FORMS))
    subtotals = {}
    for row in raw_rows:
        values = [number(value) for value in row["current_year_cells"]]
        if row["row_type"] in ("country", "other_region"):
            if not row["region_raw"]:
                raise ValueError(f"Destination lacks region: {row['destination_raw']}")
            parts = sum(value or 0 for value in values[:4])
            if values[4] is not None and parts != values[4]:
                raise ValueError(f"Destination row total mismatch: {row['destination_raw']}")
            if values[4] is None and parts != 0:
                raise ValueError(f"Missing destination total with nonzero parts: {row['destination_raw']}")
            by_region[row["region_raw"]] = [a + (b or 0) for a, b in zip(by_region[row["region_raw"]], values)]
        elif row["row_type"] == "subtotal":
            subtotals[row["region_raw"]] = values
    if set(by_region) != set(subtotals):
        raise ValueError(f"Missing region subtotal: {set(by_region) ^ set(subtotals)}")
    for region, totals in by_region.items():
        if totals != [value or 0 for value in subtotals[region]]:
            raise ValueError(f"Region subtotal mismatch: {region}")


def check_sum(label: str, values: dict, checks: list[dict], critical: bool = True):
    """Append a check comparing product-form sums with the reported total."""
    parts = [values.get(form) for form in FORMS[:4]]
    total = values.get("total")
    if total is None:
        checks.append({"check": label, "status": "fail", "reason": "reported total missing", "severity": "critical"})
        return
    calculated = sum(value for value in parts if value is not None)
    checks.append({"check": label, "status": "pass" if calculated == total else "fail", "calculated_lb": calculated, "reported_lb": total, "difference_lb": calculated - total, "severity": "critical" if critical else "review"})


def validate(inventory, shipments, destinations, raw_destinations):
    """Return inventory, shipment, and destination reconciliation checks."""
    checks = []
    inv = defaultdict(dict)
    for row in inventory:
        inv[row["inventory_item"]][row["product_form"]] = row["quantity_lb"]
    for item in ("carryover", "crop_receipts", "gross_inventory", "adjusted_inventory", "domestic_shipments", "export_shipments", "total_shipments", "estimated_marketable_inventory"):
        check_sum(f"inventory_{item}_forms", inv[item], checks)
    ship = defaultdict(dict)
    for row in shipments:
        ship[(row["period_type"], row["flow"])][row["product_form"]] = row["quantity_lb"]
    for key, values in ship.items():
        check_sum(f"shipment_{key[0]}_{key[1]}_forms", values, checks)
    for period in ("monthly", "ytd"):
        for form in FORMS:
            actual = ship[(period, "domestic")][form] + ship[(period, "export")][form]
            expected = ship[(period, "total")][form]
            checks.append({"check": f"{period}_domestic_plus_export_{form}", "status": "pass" if actual == expected else "fail", "calculated_lb": actual, "reported_lb": expected, "difference_lb": actual - expected, "severity": "critical"})
    for subtotal in (row for row in raw_destinations if row["row_type"] == "subtotal"):
        region = subtotal["region_raw"]
        for index, form in enumerate(FORMS):
            expected = number(subtotal["current_year_cells"][index])
            actual = sum(row["quantity_lb"] for row in destinations if row["region_raw"] == region and row["product_form"] == form)
            if expected is None and actual == 0:
                continue
            checks.append({"check": f"destination_{region}_{form}", "status": "pass" if actual == expected else "fail", "calculated_lb": actual, "reported_lb": expected, "difference_lb": None if expected is None else actual - expected, "severity": "critical"})
    reported = next((row for row in raw_destinations if row["row_type"] == "grand_total"), None)
    if reported is None:
        checks.append({"check": "destination_report_total", "status": "fail", "severity": "critical", "reason": "grand total missing"})
    else:
        expected = number(reported["current_year_cells"][4])
        actual = sum(row["quantity_lb"] for row in destinations if row["product_form"] == "total")
        checks.append({"check": "destination_monthly_total", "status": "pass" if actual == expected else "fail", "calculated_lb": actual, "reported_lb": expected, "difference_lb": None if expected is None else actual - expected, "severity": "critical"})
        headline = ship[("monthly", "export")]["total"]
        checks.append({"check": "destination_vs_shipment_export", "status": "pass" if expected == headline else "fail", "calculated_lb": expected, "reported_lb": headline, "difference_lb": None if expected is None else expected - headline, "severity": "critical"})
    return checks


def build_clean(manifest, raw_inventory, raw_shipments, raw_destinations):
    """Return cleaned rows after the raw assessment has passed."""
    report_month = manifest["report_month"]
    crop_year = manifest["crop_year"]
    source_file = manifest["source_file"]
    inventory = []
    for raw in raw_inventory:
        as_of_date = (datetime.strptime(raw["as_of_date"], "%m/%d/%Y").date().isoformat()
                      if raw["as_of_date"] else None)
        for form, value in raw["cells"].items():
            if value is None:
                continue  # Sparse inventory adjustments are not assumed to be zero.
            inventory.append({"report_month": report_month, "as_of_date": as_of_date, "crop_year": crop_year, "inventory_item": raw["item"], "product_form": form, "quantity_lb": number(value), "source_file": source_file, "source_page": raw["source_page"]})

    shipments = []
    for raw in raw_shipments:
        if any(value is None for value in raw["cells"].values()):
            raise ValueError(f"Missing required shipment value: {raw['flow']} {raw['period_type']}")
        for form, value in raw["cells"].items():
            shipments.append({"report_month": report_month, "observation_month": report_month, "crop_year": crop_year, "period_type": raw["period_type"], "flow": raw["flow"], "product_form": form, "quantity_lb": number(value), "source_file": source_file, "source_page": raw["source_page"]})

    destinations = []
    for raw in raw_destinations:
        if raw["row_type"] not in ("country", "other_region"):
            continue  # Published subtotals remain in raw and are used for checks.
        for form, value in zip(FORMS, raw["current_year_cells"]):
            destinations.append({"report_month": report_month, "observation_month": report_month, "crop_year": crop_year, "period_type": "monthly", "region_raw": raw["region_raw"], "destination_raw": raw["destination_raw"], "destination_type": raw["row_type"], "product_form": form, "quantity_lb": number(value) if value is not None else 0, "value_status": "reported" if value is not None else "inferred_zero", "source_file": source_file, "source_page": raw["source_page"]})

    return {"inventory": inventory, "shipments": shipments, "export_destinations": destinations}


def export_csv(clean_dir, tables):
    """Optionally export the committed data in the existing CSV formats."""
    clean_dir.mkdir(parents=True, exist_ok=True)
    write_csv(clean_dir / "inventory.csv", ["report_month", "as_of_date", "crop_year", "inventory_item", "product_form", "quantity_lb", "source_file", "source_page"], tables["inventory"])
    write_csv(clean_dir / "shipments.csv", ["report_month", "observation_month", "crop_year", "period_type", "flow", "product_form", "quantity_lb", "source_file", "source_page"], tables["shipments"])
    write_csv(clean_dir / "export_destinations.csv", ["report_month", "observation_month", "crop_year", "period_type", "region_raw", "destination_raw", "destination_type", "product_form", "quantity_lb", "value_status", "source_file", "source_page"], tables["export_destinations"])


def finding(check, count, example, decision, severity="recoverable", table=""):
    """Describe one observed issue and its handling."""
    return {
        "phase": "pre_clean", "table": table, "check": check,
        "status": "fail" if severity == "critical" else "observed",
        "observed_count": count, "example": example,
        "severity": severity, "decision": decision,
    }


def passed(check, severity, table=""):
    """Record a check that ran and found no issue, as evidence the data is clean."""
    return {
        "phase": "pre_clean", "table": table, "check": check, "status": "pass",
        "observed_count": 0, "severity": severity, "decision": "No issue found.",
    }


def assess_raw(tables, crop_year):
    """Find missing, invalid, duplicate, and explicitly zero weights before changes."""
    findings = []
    keys = {
        "inventory": ("item",),
        "shipments": ("source_crop_year", "period_type", "flow"),
        "export_destinations": ("region_raw", "destination_raw", "row_type"),
    }
    for kind, records in tables.items():
        counts = Counter(tuple(row[key] for key in keys[kind]) for row in records)
        duplicates = [key for key, count in counts.items() if count > 1]
        if duplicates:
            findings.append(finding("duplicate_observations",
                                    sum(counts[key] - 1 for key in duplicates),
                                    repr(duplicates[0]), "Stop and investigate duplicate keys.",
                                    "critical", kind))
        else:
            findings.append(passed("duplicate_observations", "critical", kind))
        missing, invalid, formatted, zeros, invalid_dates = [], [], [], [], []
        for row in records:
            if kind == "inventory" and row["as_of_date"]:
                try:
                    datetime.strptime(row["as_of_date"], "%m/%d/%Y")
                except ValueError:
                    invalid_dates.append(row["as_of_date"])
            if kind == "export_destinations":
                cells = list(zip(FORMS, row["current_year_cells"]))
                label = row["destination_raw"]
                record_blank = row["row_type"] in DETAIL_TYPES
            else:
                cells = row["cells"].items()
                label = row.get("item") or f"{row['flow']} {row['period_type']}"
                record_blank = kind == "inventory" or row["source_crop_year"] == crop_year
            for form, value in cells:
                example = f"page {row['source_page']}, {label}, {form}: {value!r}"
                if value is None:
                    if record_blank:
                        missing.append(example)
                    continue
                try:
                    parsed = number(value)
                except ValueError:
                    invalid.append(example)
                    continue
                if str(parsed) != value:
                    formatted.append(example)
                if parsed == 0:
                    zeros.append(example)
        blank_severity = "critical" if kind == "shipments" else "recoverable"
        if missing:
            decisions = {
                "inventory": "Keep the source null and omit the unstated weight from clean.",
                "shipments": "Stop because a current-year shipment weight is required.",
                "export_destinations": "Fill with inferred zero only after published totals reconcile.",
            }
            findings.append(finding("blank_weights", len(missing), missing[0],
                                    decisions[kind], blank_severity, kind))
        else:
            findings.append(passed("blank_weights", blank_severity, kind))
        if invalid:
            findings.append(finding("invalid_weights", len(invalid), invalid[0],
                                    "Stop and review the PDF cell.", "critical", kind))
        else:
            findings.append(passed("invalid_weights", "critical", kind))
        if invalid_dates:
            findings.append(finding("invalid_dates", len(invalid_dates), invalid_dates[0],
                                    "Stop and review the inventory date.", "critical", kind))
        elif kind == "inventory":
            findings.append(passed("invalid_dates", "critical", kind))
        if formatted:
            findings.append(finding("formatted_weights", len(formatted), formatted[0],
                                    "Parse signed integer pounds and preserve negative adjustments.",
                                    table=kind))
        if zeros:
            findings.append(finding("reported_zeros", len(zeros), zeros[0],
                                    "Preserve source-reported zeros.", table=kind))
    shipments = [row for row in tables["shipments"] if row["source_crop_year"] == crop_year]
    expected = {(period, flow) for period in ("monthly", "ytd")
                for flow in ("domestic", "export", "total")}
    actual = {(row["period_type"], row["flow"]) for row in shipments}
    if actual != expected:
        findings.append(finding("shipment_rows", len(actual ^ expected), repr(actual ^ expected),
                                "Stop and review current-year shipment rows.", "critical", "shipments"))
    else:
        findings.append(passed("shipment_rows", "critical", "shipments"))
    return findings, shipments


def read_raw(month):
    """Read the manifest and all raw rows for a month in one query."""
    groups = ", ".join(
        f"'{kind}', (SELECT jsonb_agg(payload ORDER BY source_row) FROM {table} WHERE report_month = '{month}')"
        for kind, table in RAW_TABLES.items()
    )
    result = query(f"SELECT jsonb_build_object('manifest', manifest, {groups}) "
                   f"FROM raw.acp_reports WHERE report_month = '{month}';")
    if not result:
        raise ValueError(f"No raw ACP report for {month}. Run ingestion.py first.")
    batch = json.loads(result)
    manifest = batch.pop("manifest")
    if any(not rows for rows in batch.values()):
        raise ValueError(f"Raw report {month} has missing tables")
    return manifest, batch


def publish(month, tables):
    """Replace all clean tables for the selected month in one transaction."""
    sql = ["BEGIN;", f"SELECT pg_advisory_xact_lock(hashtext('acp-clean-{month}'));"]
    for kind, rows in tables.items():
        table = f"clean.acp_{kind}"
        columns = ", ".join(rows[0])
        sql.extend([
            f"DELETE FROM {table} WHERE report_month = '{month}';",
            f"INSERT INTO {table} ({columns}) SELECT {columns} "
            f"FROM jsonb_populate_recordset(NULL::{table}, {json_value(rows)});",
        ])
    query("\n".join([*sql, "COMMIT;"]))


def run_clean(month, log_path, csv_dir=None):
    """Run pre-checks, cleaning, reconciliation, and database publication."""
    if not re.fullmatch(r"[0-9]{4}-(0[1-9]|1[0-2])", month):
        raise ValueError("Use a report month in YYYY-MM format")
    findings = []
    source_file = ""
    phase = "pre_clean"
    try:
        manifest, raw = read_raw(month)
        source_file = manifest["source_file"]
        findings, shipments = assess_raw(raw, manifest["crop_year"])
        if any(row["status"] == "fail" for row in findings):
            raise ValueError("Raw quality checks failed")
        check_destination_totals(raw["export_destinations"])
        findings.append(passed("destination_row_and_region_totals", "critical",
                               "export_destinations"))

        phase = "clean"
        tables = build_clean(manifest, raw["inventory"], shipments, raw["export_destinations"])
        inferred = sum(row["value_status"] == "inferred_zero" for row in tables["export_destinations"])
        if inferred:
            findings.append({
                **finding("inferred_zero_weights", inferred, "value_status=inferred_zero",
                          "Filled destination weights after available totals reconciled.",
                          table="export_destinations"),
                "phase": phase, "status": "applied",
            })

        phase = "post_clean"
        checks = validate(tables["inventory"], tables["shipments"],
                          tables["export_destinations"], raw["export_destinations"])
        failures = [check for check in checks if check["status"] == "fail"]
        for check in failures:
            findings.append({
                **check, "phase": phase,
                "example": check.get("reason", ""),
                "decision": "Keep previous clean data and review the PDF or raw records.",
            })
        if failures:
            raise ValueError("Published totals did not reconcile")
        findings.append({
            "check": "published_totals", "phase": phase, "status": "pass",
            "observed_count": len(checks), "decision": "All reconciliation checks passed.",
        })

        phase = "publish"
        publish(month, tables)
        if csv_dir:
            phase = "csv_export"
            export_csv(csv_dir, tables)
        return {kind: len(rows) for kind, rows in tables.items()}
    except (ValueError, RuntimeError, KeyError, OSError) as error:
        findings.append({
            "check": "pipeline_error", "phase": phase, "status": "fail",
            "severity": "critical", "example": str(error),
            "decision": ("Clean tables committed; review CSV export." if phase == "csv_export"
                         else "Review this run before updating clean tables."),
        })
        raise
    finally:
        write_log(log_path, month, source_file, findings)


def main():
    """Clean one loaded report month and optionally export its CSVs."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("month", nargs="?", help="Report month (YYYY-MM)")
    parser.add_argument("--log", type=Path, default=QUALITY_LOG_PATH)
    parser.add_argument("--csv-dir", type=Path)
    args = parser.parse_args()
    month = args.month or REPORT_MONTH
    csv_dir = args.csv_dir if args.month or args.csv_dir is not None else CLEAN_CSV_DIR
    print(json.dumps(run_clean(month, args.log, csv_dir), indent=2))


if __name__ == "__main__":
    main()
