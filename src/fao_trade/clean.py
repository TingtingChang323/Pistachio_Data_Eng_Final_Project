"""Validate FAOSTAT pistachio trade records and publish the clean table.

Reads one raw snapshot and runs critical checks on every record. Any critical
failure stops publication after all checks have run, so one run reports every
problem. Records in YEAR_RANGE are then reshaped so each row holds one
reporter, partner, year, and flow with its quantity, value, and unit value
side by side. Recoverable issues are kept and logged, and the result is
reconciled against the raw sums before it replaces the published snapshot.
Every check, passing or not, is written to the quality log.

Run from the repository root:
    python -m src.fao_trade.clean [--retrieved-on YYYY-MM-DD] [--export-csv]
"""

from __future__ import annotations

import argparse
import json
import re
import statistics
from collections import Counter, defaultdict
from datetime import date
from decimal import Decimal

from src.acp.database import json_value, query
from src.usda_psd.quality_log import Finding, write_csv, write_log

from .config import CLEAN_FILE, DOWNLOAD_DATE, ITEM_CODE, QUALITY_LOG_PATH, YEAR_RANGE, batch_dir
from .extract import TRADE_COLUMNS
from .ingestion import read_manifest

ITEM = (ITEM_CODE, "'01375", "Pistachios, in shell")
# Element code -> (flow, measure, element name, unit).
ELEMENTS = {
    "5610": ("import", "quantity", "Import quantity", "t"),
    "5622": ("import", "value", "Import value", "1000 USD"),
    "5910": ("export", "quantity", "Export quantity", "t"),
    "5922": ("export", "value", "Export value", "1000 USD"),
}
VALUE = re.compile(r"[0-9]+(?:\.[0-9]+)?")
YEAR = re.compile(r"[12][0-9]{3}")
UNIT_VALUE_PLACES = Decimal("0.0001")
MIRROR_GAP_SHARE = Decimal("0.25")
COVERAGE_DROP_SHARE = 0.10
CSV_FIELDS = (
    "snapshot_id", "quantity_source_row", "value_source_row", "year", "flow",
    "reporter_fao_code", "reporter_m49", "reporter_name",
    "partner_fao_code", "partner_m49", "partner_name",
    "item_code", "item_cpc", "product_basis", "quantity_mt", "value_kusd",
    "quantity_flag", "value_flag", "unit_value_usd_per_kg", "unit_value_status", "is_self_trade",
)
CRITICAL_CHECKS = {
    "row_shape": "Stop publication if a record lacks the 16 source columns.",
    "item": "Stop publication if a record is not Item Code 223, Pistachios, in shell.",
    "element_unit": "Stop publication unless the element is one of four trade elements in t or 1000 USD.",
    "year": "Stop publication unless Year is a four-digit year equal to Year Code.",
    "value": "Stop publication unless Value is a non-negative number.",
    "flag": "Stop publication if a flag is not in the FAOSTAT flag list.",
    "country_codes": "Stop publication if a country code or its M49 code is not in the code lists.",
    "duplicate_keys": "Stop publication if a reporter, partner, year, and element appear twice.",
    "unpaired_measures": "Stop publication if a quantity has no matching value or the reverse.",
}


def require(findings: list[Finding], phase: str, check: str, examples: list, decision: str) -> None:
    """Log a critical check, which fails when it has any example."""
    findings.append(Finding(
        phase, check, "fail" if examples else "pass", "critical",
        len(examples), next(iter(examples), ""), decision,
    ))


def observe(findings: list[Finding], phase: str, check: str, examples: list, decision: str,
            status: str = "observed") -> None:
    """Log a recoverable check, recording its count and first example."""
    findings.append(Finding(
        phase, check, status if examples else "pass", "recoverable",
        len(examples), next(iter(examples), ""), decision,
    ))


def decimal_text(value: Decimal) -> str:
    """Format a decimal without exponent notation or trailing zeros."""
    return format(value.normalize(), "f")


def read_raw(manifest: dict) -> tuple[list, dict]:
    """Read a snapshot's records and code lists in source order and check their counts.

    Returns ``[source_row, cells]`` pairs and a mapping from code list name to
    its rows of cells.
    """
    snapshot_id = manifest["source_sha256"]
    if not re.fullmatch(r"[0-9a-f]{64}", snapshot_id):
        raise ValueError("Invalid FAO snapshot SHA-256")
    batch = json.loads(query(f"""
        SELECT jsonb_build_object(
            'rows', (
                SELECT COALESCE(jsonb_agg(jsonb_build_array(source_row, payload) ORDER BY source_row), '[]')
                FROM raw.fao_trade_rows WHERE snapshot_id = '{snapshot_id}'
            ),
            'code_lists', (
                SELECT COALESCE(jsonb_object_agg(code_list, entries), '{{}}')
                FROM (
                    SELECT code_list, jsonb_agg(payload ORDER BY source_row) AS entries
                    FROM raw.fao_trade_code_lists WHERE snapshot_id = '{snapshot_id}'
                    GROUP BY code_list
                ) AS lists
            )
        );
    """))
    if len(batch["rows"]) != manifest["item_row_count"]:
        raise ValueError(f"Expected {manifest['item_row_count']} FAO raw rows, found {len(batch['rows'])}. Run ingestion.")
    for name, entry in manifest["code_lists"].items():
        if len(batch["code_lists"].get(name, [])) != entry["row_count"]:
            raise ValueError(f"FAO code list {name} is incomplete. Run ingestion.")
    return batch["rows"], batch["code_lists"]


def check_raw(rows: list, code_lists: dict, findings: list[Finding]) -> dict:
    """Run the critical checks on every raw record and pair its measures.

    Returns ``{(reporter, partner, year, flow): {"quantity": obs, "value": obs}}``
    where each observation holds the typed fields of one raw record.
    """
    reporters = {cells[0]: (cells[1], cells[2]) for cells in code_lists["reporter_countries"]}
    partners = {cells[0]: (cells[1], cells[2]) for cells in code_lists["partner_countries"]}
    flags = {cells[0].strip() for cells in code_lists["flags"]}
    problems = defaultdict(list)
    name_variants = {}
    seen = {}
    pairs = defaultdict(dict)

    for source_row, cells in rows:
        where = f"row {source_row}"
        if len(cells) != len(TRADE_COLUMNS):
            problems["row_shape"].append(f"{where}: {len(cells)} cells")
            continue
        cell = dict(zip(TRADE_COLUMNS, cells))
        if (cell["Item Code"], cell["Item Code (CPC)"], cell["Item"]) != ITEM:
            problems["item"].append(f"{where}: {cell['Item Code']} {cell['Item']!r}")
        element = ELEMENTS.get(cell["Element Code"])
        if element is None or element[2:] != (cell["Element"], cell["Unit"]):
            problems["element_unit"].append(
                f"{where}: {cell['Element Code']} {cell['Element']!r} in {cell['Unit']!r}")
            continue
        if not YEAR.fullmatch(cell["Year"]) or cell["Year Code"] != cell["Year"]:
            problems["year"].append(f"{where}: Year {cell['Year']!r}, Year Code {cell['Year Code']!r}")
            continue
        if not VALUE.fullmatch(cell["Value"]):
            problems["value"].append(f"{where}: {cell['Value']!r}")
            continue
        if cell["Flag"] not in flags:
            problems["flag"].append(f"{where}: {cell['Flag']!r}")

        countries = {}
        for role, listed_codes in (("Reporter", reporters), ("Partner", partners)):
            code = cell[f"{role} Country Code"]
            m49 = cell[f"{role} Country Code (M49)"]
            name = cell[f"{role} Countries"]
            listed = listed_codes.get(code)
            if listed is None or listed[0] != m49:
                problems["country_codes"].append(f"{where}: {role.lower()} {code} with M49 {m49}")
            elif listed[1] != name:
                name_variants[(role, code)] = f"{role.lower()} {code}: file {name!r}, code list {listed[1]!r}"
            countries[role] = (int(code), m49.lstrip("'"), name)

        key = (cell["Reporter Country Code"], cell["Partner Country Code"], cell["Year"], cell["Element Code"])
        if key in seen:
            problems["duplicate_keys"].append(f"rows {seen[key]} and {source_row}: {key}")
            continue
        seen[key] = source_row
        flow, measure = element[:2]
        reporter, partner = countries["Reporter"], countries["Partner"]
        pairs[(reporter[0], partner[0], int(cell["Year"]), flow)][measure] = {
            "source_row": source_row, "reporter": reporter, "partner": partner,
            "amount": Decimal(cell["Value"]), "flag": cell["Flag"],
        }

    problems["unpaired_measures"] = [
        f"reporter {key[0]}, partner {key[1]}, {key[2]} {key[3]}: only {', '.join(pair)}"
        for key, pair in pairs.items() if len(pair) != 2
    ]
    for check, decision in CRITICAL_CHECKS.items():
        require(findings, "pre_clean", check, problems[check], decision)
    observe(findings, "pre_clean", "country_name_variants", sorted(name_variants.values()),
            "Keep the name printed in the trade file and match countries by code.")
    if any(item.status == "fail" for item in findings):
        raise ValueError("FAO raw quality checks failed")
    return pairs


def build_clean(snapshot_id: str, pairs: dict, findings: list[Finding]) -> list[dict]:
    """Combine each in-scope quantity and value into one record and log anomalies.

    Zero quantities or values are kept as reported. The unit value, value_kusd
    divided by quantity_mt, equals USD per kg and is left null when either is zero.
    """
    first, last = YEAR_RANGE
    records = []
    issues = defaultdict(list)
    out_of_scope = 0
    for (_, _, year, flow), pair in pairs.items():
        if not first <= year <= last:
            out_of_scope += len(pair)
            continue
        quantity, value = pair["quantity"], pair["value"]
        reporter, partner = quantity["reporter"], quantity["partner"]
        amount, worth = quantity["amount"], value["amount"]
        label = f"{year} {flow}, reporter {reporter[2]}, partner {partner[2]}"
        zero_kind = {
            (True, False): "zero_quantity_positive_value",
            (False, True): "positive_quantity_zero_value",
            (True, True): "zero_quantity_and_value",
        }.get((amount == 0, worth == 0))
        if zero_kind:
            issues[zero_kind].append(f"{label}: {decimal_text(amount)} t, {decimal_text(worth)} thousand USD")
        if reporter[0] == partner[0]:
            issues["self_trade"].append(label)
        if (quantity["flag"], value["flag"]) != ("A", "A"):
            issues["non_official_flags"].append(f"{label}: flags {quantity['flag']}, {value['flag']}")
        unit_value = (worth / amount).quantize(UNIT_VALUE_PLACES) if amount and worth else None
        records.append({
            "snapshot_id": snapshot_id,
            "quantity_source_row": quantity["source_row"],
            "value_source_row": value["source_row"],
            "year": year,
            "flow": flow,
            "reporter_fao_code": reporter[0],
            "reporter_m49": reporter[1],
            "reporter_name": reporter[2],
            "partner_fao_code": partner[0],
            "partner_m49": partner[1],
            "partner_name": partner[2],
            "item_code": int(ITEM_CODE),
            "item_cpc": ITEM[1].lstrip("'"),
            "product_basis": "inshell",
            "quantity_mt": amount,
            "value_kusd": worth,
            "quantity_flag": quantity["flag"],
            "value_flag": value["flag"],
            "unit_value_usd_per_kg": unit_value,
            "unit_value_status": "computed" if unit_value is not None else "not_computable",
            "is_self_trade": reporter[0] == partner[0],
        })

    findings.append(Finding(
        "clean", "years_out_of_scope", "applied" if out_of_scope else "pass", "recoverable",
        out_of_scope, f"raw records with a year outside {first}-{last}" if out_of_scope else "",
        f"Keep these records in raw and publish only {first}-{last}.",
    ))
    observe(findings, "clean", "zero_quantity_positive_value", issues["zero_quantity_positive_value"],
            "Keep both reported numbers and leave the unit value null.")
    observe(findings, "clean", "positive_quantity_zero_value", issues["positive_quantity_zero_value"],
            "Keep both reported numbers and leave the unit value null.")
    observe(findings, "clean", "zero_quantity_and_value", issues["zero_quantity_and_value"],
            "Keep the reported zeros and leave the unit value null.")
    observe(findings, "clean", "self_trade", issues["self_trade"],
            "Keep and set is_self_trade so bilateral analysis can exclude it.")
    observe(findings, "clean", "non_official_flags", issues["non_official_flags"],
            "Keep the values and carry the E, I, or X flags into the clean table.")
    flag_unit_value_outliers(records, findings)
    describe_mirror_flows(records, findings)
    describe_reporter_coverage(records, findings)
    return records


def flag_unit_value_outliers(records: list[dict], findings: list[Finding]) -> None:
    """Mark unit values outside their year's 1st to 99th percentile as outliers.

    Comparing within a year keeps 25 years of price change from being read as
    outliers. The value stays in the table; only its status changes.
    """
    by_year = defaultdict(list)
    for record in records:
        if record["unit_value_usd_per_kg"] is not None:
            by_year[record["year"]].append(record)
    outliers = []
    for year, group in by_year.items():
        if len(group) < 2:
            continue
        cuts = statistics.quantiles([r["unit_value_usd_per_kg"] for r in group], n=100, method="inclusive")
        for record in group:
            unit_value = record["unit_value_usd_per_kg"]
            if not cuts[0] <= unit_value <= cuts[-1]:
                record["unit_value_status"] = "outlier"
                outliers.append((unit_value, f"{year} {record['flow']}, reporter {record['reporter_name']}, "
                                             f"partner {record['partner_name']}: "
                                             f"{decimal_text(unit_value)} USD/kg"))
    outliers.sort(reverse=True)
    observe(findings, "clean", "unit_value_outliers", [label for _, label in outliers],
            "Keep the value and set unit_value_status to outlier.")


def describe_mirror_flows(records: list[dict], findings: list[Finding]) -> None:
    """Compare each flow as reported by its exporter and by its importer."""
    exports, imports = {}, {}
    for record in records:
        if record["is_self_trade"]:
            continue
        if record["flow"] == "export":
            exports[(record["reporter_name"], record["partner_name"], record["year"])] = record
        else:
            imports[(record["partner_name"], record["reporter_name"], record["year"])] = record

    one_sided = [f"{year}: {seller} to {buyer}, reported by exporter only"
                 for seller, buyer, year in sorted(exports.keys() - imports.keys())]
    one_sided += [f"{year}: {seller} to {buyer}, reported by importer only"
                  for seller, buyer, year in sorted(imports.keys() - exports.keys())]
    gaps = []
    for seller, buyer, year in exports.keys() & imports.keys():
        exported = exports[(seller, buyer, year)]["quantity_mt"]
        imported = imports[(seller, buyer, year)]["quantity_mt"]
        if abs(exported - imported) > MIRROR_GAP_SHARE * max(exported, imported):
            gaps.append((abs(exported - imported),
                         f"{year}: {seller} to {buyer}, exporter {decimal_text(exported)} t, "
                         f"importer {decimal_text(imported)} t"))
    gaps.sort(reverse=True)
    observe(findings, "clean", "one_sided_flows", one_sided,
            "Keep each side as reported. Analysis must state whose report it uses.")
    observe(findings, "clean", "mirror_quantity_gaps", [label for _, label in gaps],
            f"Keep both reports where exporter and importer differ by more than {MIRROR_GAP_SHARE:.0%}.")


def describe_reporter_coverage(records: list[dict], findings: list[Finding]) -> None:
    """Flag years in which noticeably fewer countries reported than the year before."""
    reporters = defaultdict(set)
    for record in records:
        reporters[record["year"]].add(record["reporter_fao_code"])
    drops = [
        f"{year}: {len(reporters[year])} reporters, {year - 1}: {len(reporters[year - 1])}"
        for year in sorted(reporters)
        if year - 1 in reporters
        and len(reporters[year]) < (1 - COVERAGE_DROP_SHARE) * len(reporters[year - 1])
    ]
    observe(findings, "clean", "reporter_coverage_drops", drops,
            "Keep the data and compare years only across countries that report in both.")


def reconcile(records: list[dict], rows: list, findings: list[Finding]) -> None:
    """Check that the clean table carries exactly the in-scope raw quantities and values."""
    first, last = YEAR_RANGE
    expected = defaultdict(Decimal)
    for _, cells in rows:
        cell = dict(zip(TRADE_COLUMNS, cells))
        if first <= int(cell["Year"]) <= last:
            flow, measure = ELEMENTS[cell["Element Code"]][:2]
            expected[(int(cell["Year"]), flow, measure)] += Decimal(cell["Value"])
    actual = defaultdict(Decimal)
    for record in records:
        actual[(record["year"], record["flow"], "quantity")] += record["quantity_mt"]
        actual[(record["year"], record["flow"], "value")] += record["value_kusd"]
    mismatches = [
        f"{key}: clean {actual[key]}, raw {expected[key]}"
        for key in sorted(expected.keys() | actual.keys()) if actual[key] != expected[key]
    ]
    keys = Counter(
        (r["year"], r["flow"], r["reporter_fao_code"], r["partner_fao_code"]) for r in records
    )
    duplicates = [str(key) for key, count in keys.items() if count > 1]
    require(findings, "validate", "totals_match_raw", mismatches,
            "Stop publication unless quantity and value totals per year and flow equal the raw sums.")
    require(findings, "validate", "unique_clean_keys", duplicates,
            "Stop publication if a reporter, partner, year, and flow appear twice.")
    if mismatches or duplicates:
        raise ValueError("FAO clean table does not reconcile with raw records")


def publish(snapshot_id: str, records: list[dict]) -> None:
    """Replace one validated snapshot in a single PostgreSQL transaction."""
    fields = ", ".join(CSV_FIELDS)
    payload = json_value([
        {key: decimal_text(value) if isinstance(value, Decimal) else value for key, value in record.items()}
        for record in records
    ])
    query(f"""
        BEGIN;
        SELECT snapshot_id FROM raw.fao_trade_snapshots
        WHERE snapshot_id = '{snapshot_id}' FOR UPDATE;

        DELETE FROM clean.fao_pistachio_trade WHERE snapshot_id = '{snapshot_id}';
        INSERT INTO clean.fao_pistachio_trade ({fields})
        SELECT {fields}
        FROM jsonb_populate_recordset(NULL::clean.fao_pistachio_trade, {payload});
        COMMIT;
    """)


def export_csv(snapshot_id: str, retrieved_on: date) -> None:
    """Write the published snapshot from PostgreSQL to the clean CSV."""
    published = json.loads(query(f"""
        SELECT jsonb_agg(to_jsonb(published)
                         ORDER BY year, flow, reporter_name, partner_name)
        FROM clean.fao_pistachio_trade AS published
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
        rows, code_lists = read_raw(manifest)
        phase = "pre_clean"
        pairs = check_raw(rows, code_lists, findings)
        phase = "clean"
        records = build_clean(snapshot_id, pairs, findings)
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
    print(f"FAO clean trade rows: {count}")


if __name__ == "__main__":
    main()
