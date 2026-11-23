# USDA PSD pistachio export

The [USDA FAS PSD Custom Query](https://apps.fas.usda.gov/PSDOnline/app/index.html#/app/advQuery)
export downloaded on November 22, 2026 contains 224 rows and 20 columns. It
covers eight supply and distribution measures for 28 geographic entities and
13 market years from `2013/2014` through `2025/2026`. The 144,316-byte file is
an HTML table distributed as `psd.xls`. The query used year columns, File
format, and Include Codes. This source supports annual comparisons of
production, stocks, trade volumes, and consumption.

## Storage and grain

| Location | Contents |
| --- | --- |
| `raw/usda_psd/YYYY-MM-DD/psd.xls` in S3 | Original file for team access and reproducibility |
| `raw.usda_psd_snapshots` in PostgreSQL | One manifest per source SHA-256 with the download date, original column order, and provenance |
| `raw.usda_psd_rows` in PostgreSQL | One ordered JSONB array of cell text per source row, keyed by snapshot and row position |
| `clean.usda_psd_pistachios` in PostgreSQL | One observation per snapshot, commodity, geography, market year, and attribute |
| `data/quality/usda_psd/quality_log.csv` | Latest quality findings for each download date, with counts, examples, severity, and decisions |

The manifest columns map directly to positions in each raw array. Raw numeric
cells keep formatting such as `716,676` and empty cells keep their empty
text. The `source_row` field is one-based and counts data rows after the
header. The clean table links each observation back to its raw row. The
optional CSV exports the published PostgreSQL table.

## Clean fields

| Fields | Meaning |
| --- | --- |
| `snapshot_id`, `source_row` | Source SHA-256 and data row position |
| `commodity_code`, `commodity_name`, `product_basis` | USDA commodity `577907`, Pistachios, Inshell Basis, tracked as `inshell` |
| `geo_code`, `geo_name`, `geo_type` | USDA code and label. EU `E4` is a region. Any World result is a separate geography type. |
| `market_year`, `market_year_start` | Source split-year label and its first year |
| `attribute_id`, `attribute_name` | Beginning Stocks (20), Production (28), Imports (57), Total Supply (86), Exports (88), Domestic Consumption (125), Ending Stocks (176), Total Distribution (178) |
| `quantity_mt`, `value_status` | Integer metric tons. Source values, including zero, are `reported`. A blank yields SQL `NULL` and `missing`. |

## Validation

- The November 22 export expands to 2,912 observations. The quality log
  records 1,421 values with thousands separators, 1,403 reported zeros, and
  42 geography and market-year groups with eight zeros. It records zero
  blank values, negative values, and duplicate raw business keys.
- The following equations each reconcile across 364 geography and market-year
  groups in this export.
  - Beginning Stocks + Production + Imports = Total Supply.
  - Exports + Domestic Consumption + Ending Stocks = Total Distribution.
  - Total Supply = Total Distribution.
- Identical duplicate rows are retained in raw and collapsed in clean.
  Conflicting duplicates, unexpected units or quantities, and complete
  equations that fail reconciliation stop clean publication. Equations
  with missing operands and gaps in adjacent-year stocks are logged.
- Summing the EU and its member countries can double-count overlapping
  coverage. This export contains no World row. ACP shipments and USDA
  annual supply or trade also have different scopes and units. Compare them
  after checking time coverage, geography, and product basis.
