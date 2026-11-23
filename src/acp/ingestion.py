"""Parse ACP reports and load source cells into PostgreSQL."""

import argparse
import json
import re
from io import BytesIO
from pathlib import Path

import pdfplumber

from config import EXTRACT_DIR, PDF_PATH
from database import RAW_TABLES, query, json_value
from extract import MONTH_NAMES, build_manifest

FORMS = ("open_inshell", "closed_shell", "shelling_stock", "kernels", "total")


def rows_by_y(page, tolerance: float = 2.5):
    """Group pdfplumber words into visual rows using their vertical positions."""
    rows = []
    for word in sorted(page.extract_words(), key=lambda w: (w["top"], w["x0"])):
        match = next((row for row in rows if abs(row[0] - word["top"]) <= tolerance), None)
        if match is None:
            rows.append([word["top"], [word]])
        else:
            match[1].append(word)
    return [(y, sorted(words, key=lambda w: w["x0"])) for y, words in sorted(rows)]


def extract_inventory(page, raw_rows: list[dict]):
    """Append source inventory cells from PDF page 1 to raw_rows."""
    labels = {
        "Carryover": "carryover", "Crop Receipts": "crop_receipts",
        "Gross Inventory": "gross_inventory", "Inshell Shrinkage": "inshell_shrinkage",
        "Shelling Adjustments": "shelling_adjustments", "Other Adjustments": "other_adjustments",
        "Adjusted Inventory**": "adjusted_inventory", "Domestic Shipments": "domestic_shipments",
        "Export Shipments": "export_shipments", "Total Shipments": "total_shipments",
        "As of": "estimated_marketable_inventory",
    }
    bounds = (290, 360, 435, 505, 590)
    for y, words in rows_by_y(page):
        left = " ".join(w["text"] for w in words if w["x0"] < 205)
        item = next((code for label, code in labels.items() if label in left), None)
        if item is None or left.startswith("**") or y > 300:
            continue
        cells = dict(zip(FORMS, position_cells(words, 205, bounds, left)))
        as_of = next((w["text"] for w in words if re.fullmatch(r"\d{1,2}/\d{1,2}/\d{4}", w["text"])), None)
        raw_rows.append({"source_page": 1, "label": left, "item": item, "as_of_date": as_of, "cells": cells})


def extract_shipments(page, raw_rows: list[dict], month_name: str):
    """Preserve monthly, YTD, and comparative shipment rows and blank cells."""
    section = None
    period = None
    for _, words in rows_by_y(page):
        label = " ".join(w["text"] for w in words if w["x0"] < 205)
        if label in ("Domestic", "Export", "Total Shipments"):
            section = {"Domestic": "domestic", "Export": "export", "Total Shipments": "total"}[label]
            period = None
            continue
        match = re.fullmatch(rf"(?:({month_name})( YTD)? )?(\d{{4}}/\d{{4}})", label)
        if not section or not match:
            continue
        if match[1]:
            period = "ytd" if match[2] else "monthly"
        if period is None:
            raise ValueError(f"Shipment row has no period heading: {label}")
        cells = position_cells(words, 205, (290, 360, 435, 505, 590), label)
        raw_rows.append({
            "source_page": 1, "flow": section, "period_type": period,
            "source_crop_year": match[3], "source_line": " ".join(w["text"] for w in words),
            "cells": dict(zip(FORMS, cells)),
        })


def extract_destinations(pdf, raw_rows: list[dict]):
    """Append ACP source destination rows from PDF pages 3–5."""
    region = None
    regions = {"North America", "Central America", "South America", "Europe", "Asia", "Middle East & Africa", "Australia/Oceania"}
    # Right edges of the ten year/product columns, alternating prior/current year.
    bounds = (221, 287, 347, 407, 467, 527, 587, 647, 709, 777)
    for page_index in (2, 3, 4):
        for y, words in rows_by_y(pdf.pages[page_index]):
            if y < 75 or y > 575:
                continue
            label = " ".join(w["text"] for w in words if w["x0"] < 155).strip()
            if not label:
                continue
            if label in regions:
                region = label
                continue
            if label.startswith("Subtotal") or label == "Totals":
                row_type = "subtotal" if label.startswith("Subtotal") else "grand_total"
            elif label.startswith("Other "):
                row_type = "other_region"
            else:
                row_type = "country"
            cells = position_cells(words, 155, bounds, label)
            raw_rows.append({"source_page": page_index + 1, "region_raw": region, "destination_raw": label, "row_type": row_type, "prior_year_cells": cells[::2], "current_year_cells": cells[1::2]})




def position_cells(words, label_edge, bounds, label):
    """Place source tokens into PDF columns without coercing cell contents."""
    cells = [None] * len(bounds)
    for word in words:
        if word["x0"] < label_edge:
            continue
        index = next((i for i, edge in enumerate(bounds) if word["x1"] <= edge), None)
        if index is None:
            raise ValueError(f"Token outside the table on {label}: {word['text']}")
        cells[index] = " ".join(filter(None, (cells[index], word["text"])))
    return cells


def parse_report(pdf_bytes, manifest):
    """Return raw rows from the three selected ACP report sections."""
    rows = {kind: [] for kind in RAW_TABLES}
    month_name = MONTH_NAMES[manifest["report_month"]]
    with pdfplumber.open(BytesIO(pdf_bytes)) as pdf:
        if len(pdf.pages) not in (12, 13):
            raise ValueError("Unexpected ACP report page count")
        extract_inventory(pdf.pages[0], rows["inventory"])
        extract_shipments(pdf.pages[0], rows["shipments"], month_name)
        extract_destinations(pdf, rows["export_destinations"])
    if any(not records for records in rows.values()):
        raise ValueError("At least one selected report section produced no rows")
    return rows


def ingest(manifest, rows):
    """Load a new source atomically and verify counts and payloads on retries."""
    month = manifest["report_month"]
    sql = [
        "BEGIN;",
        "LOCK TABLE raw.acp_reports IN SHARE ROW EXCLUSIVE MODE;",
        "CREATE TEMP TABLE assertions (ok boolean NOT NULL CHECK (ok));",
        f"INSERT INTO raw.acp_reports (report_month, manifest) VALUES ('{month}', "
        f"{json_value(manifest)}) ON CONFLICT (report_month) DO NOTHING;",
        "INSERT INTO assertions SELECT manifest->>'source_sha256' = "
        f"'{manifest['source_sha256']}' FROM raw.acp_reports WHERE report_month = '{month}';",
    ]
    for kind, table in RAW_TABLES.items():
        payload = json_value(rows[kind])
        sql.extend([
            f"INSERT INTO {table} SELECT '{month}', ord, row "
            f"FROM jsonb_array_elements({payload}) WITH ORDINALITY AS src(row, ord) "
            "ON CONFLICT (report_month, source_row) DO NOTHING;",
            "INSERT INTO assertions SELECT "
            f"COALESCE(jsonb_agg(payload ORDER BY source_row) = {payload}, false) "
            f"FROM {table} WHERE report_month = '{month}';",
        ])
    query("\n".join([*sql, "COMMIT;"]))
    return {kind: len(records) for kind, records in rows.items()}


def main():
    """Verify the registered PDF and load its selected sections."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("pdf", nargs="?", type=Path, default=PDF_PATH)
    args = parser.parse_args()
    pdf_bytes = args.pdf.read_bytes()
    source = build_manifest(args.pdf.name, pdf_bytes)
    manifest_path = EXTRACT_DIR / source["report_month"] / "manifest.json"
    if not manifest_path.is_file():
        parser.error(f"Manifest not found: {manifest_path}. Run extract.py first.")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if any(source[key] != manifest[key] for key in
           ("source_file", "source_sha256", "report_month", "crop_year")):
        raise ValueError("PDF differs from manifest.json. Rerun extract.py for the correct file.")
    rows = parse_report(pdf_bytes, manifest)
    print(json.dumps(ingest(manifest, rows), indent=2))


if __name__ == "__main__":
    main()
