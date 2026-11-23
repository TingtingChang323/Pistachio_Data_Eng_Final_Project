# FAOSTAT pistachio trade matrix

The [FAOSTAT Detailed Trade Matrix](https://www.fao.org/faostat/en/#data/TM)
bulk download (All Data, Normalized) was retrieved on November 23, 2026. The
498.7 MB zip holds an 8.5 GB CSV with 52,410,630 bilateral trade records for
every agricultural item, plus code lists. FAOSTAT's only pistachio item is
`223` Pistachios, in shell (CPC `01375`), which has 103,838 records from
1986 through 2024. Each record is one reporter country's export or import
quantity or value with one partner country in one calendar year. This source
adds global country-to-country flows and unit values to the US-centered ACP
and USDA PSD data.

## Storage and grain

| Location | Contents |
| --- | --- |
| `raw/fao_trade/YYYY-MM-DD/` zip in S3 | Original archive for team access and reproducibility |
| `raw.fao_trade_snapshots` in PostgreSQL | One manifest per trade CSV SHA-256 with the archive digest, download date, columns, scanned and kept record counts, and provenance |
| `raw.fao_trade_rows` in PostgreSQL | Every Item Code 223 record from all years as an ordered JSONB array of the 16 source cells, keyed by snapshot and source row |
| `raw.fao_trade_code_lists` in PostgreSQL | Reporter country (232), partner country (255), and flag (4) code lists, one JSONB array per source row |
| `clean.fao_pistachio_trade` in PostgreSQL | One row per snapshot, year, flow, reporter, and partner for 2000 to 2024 |
| `data/quality/fao_trade/quality_log.csv` | Latest quality findings for each download date, with counts, examples, severity, and decisions |

Extraction streams the CSV out of the zip and keeps records by parsing the
`Item Code` column. `source_row` is the one-based position of a data record
after the header in the original CSV, so every raw and clean row points back
to the source file. Raw cells keep their source text, such as `'840` and
`92063.290000`. The other 99.8% of records cover unrelated items and stay in
the archive on S3. The optional CSV exports the published PostgreSQL table.

## Clean fields

| Fields | Meaning |
| --- | --- |
| `snapshot_id`, `quantity_source_row`, `value_source_row` | Trade CSV SHA-256 and the raw rows holding the quantity and value |
| `year` | Calendar year, 2000 to 2024 |
| `flow` | `export` or `import`, from the reporter's side. For exports the partner is the destination. For imports it is the origin. |
| `reporter_fao_code`, `reporter_m49`, `reporter_name` | FAO country code, UN M49 code without the leading `'`, and name as printed in the trade file |
| `partner_fao_code`, `partner_m49`, `partner_name` | The same fields for the partner country |
| `item_code`, `item_cpc`, `product_basis` | `223`, `01375`, and `inshell`. Shelled kernels are not part of this item. |
| `quantity_mt`, `value_kusd` | Metric tons and thousand US dollars, as reported |
| `quantity_flag`, `value_flag` | `A` official, `E` estimated, `I` imputed by a receiving agency, `X` from an external organization |
| `unit_value_usd_per_kg`, `unit_value_status` | `value_kusd / quantity_mt`, which equals USD per kg, rounded to four places. It is null and `not_computable` when either amount is zero. It is `outlier` outside the same year's 1st to 99th percentile, and `computed` otherwise. |
| `is_self_trade` | Reporter and partner are the same country |

China, mainland, Hong Kong SAR, Macao SAR, and Taiwan Province of stay
separate. Historical entities such as Sudan (former) keep their own codes.

## Validation

- Any of these critical issues stops clean publication: a record without 16
  columns, an item other than 223, an element or unit outside the four trade
  elements, an invalid year, a non-numeric or negative value, an unknown flag,
  a country or M49 code missing from the code lists, a duplicate reporter,
  partner, year, and element, or a quantity without a matching value. All
  pass for the November 23 download.
- After cleaning, quantity and value totals per year and flow must equal the
  raw sums, and each reporter, partner, year, and flow must appear once.
- The November 23 download publishes 42,919 rows. The quality log records
  18,000 raw records before 2000 kept in raw only, 3,582 zero quantities with
  a positive value, 2,741 positive quantities with a zero value, 4,190 zero
  quantities and values, 39 self-trade rows, 571 rows with an `E`, `I`, or `X`
  flag, and 659 unit value outliers such as 9,760 USD/kg.
- The code lists print names with semicolons, such as `China; mainland`, while
  the trade file uses commas. Eight such variants are logged, and countries are
  matched by code.
- Exporter and importer reports of the same flow often disagree. 19,430 flows
  are reported by one side only, and 7,966 differ by more than 25%. Iran does
  not report its own trade in several recent years, so its exports must be
  read from partners' imports. Analysis should state which side it uses.
- Only 111 countries reported for 2024, against 141 for 2023. Compare recent
  years only across countries that report in both.
- FAOSTAT years are calendar years. ACP and USDA PSD use September to August
  crop or market years, so align periods before comparing them.
