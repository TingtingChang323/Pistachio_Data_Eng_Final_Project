# FAOSTAT pistachio producer prices

The [FAOSTAT Producer Prices](https://www.fao.org/faostat/en/#data/PP) export
downloaded on November 23, 2026 contains 108 rows and 16 columns. Every row is
the annual producer price of Pistachios, in shell (CPC `01375`) in USD per
tonne for one of 12 areas and one calendar year from 2011 through 2024. The
19,878-byte file is a UTF-8 CSV with a byte order mark. This source supplies
the producer prices that Q3 and Q4 compare with trade volumes, export unit
values, harvests, and stocks.

## Storage and grain

| Location | Contents |
| --- | --- |
| `raw/fao_prices/YYYY-MM-DD/FAOSTAT_data_en_11-23-2026.csv` in S3 | Original file for team access and reproducibility |
| `raw.fao_prices_snapshots` in PostgreSQL | One manifest per source SHA-256 with the download date, columns, coverage summary, and provenance |
| `raw.fao_prices_rows` in PostgreSQL | One ordered JSONB array of the 16 source cells per data row, keyed by snapshot and row position |
| `clean.fao_pistachio_producer_prices` in PostgreSQL | One observation per snapshot, area, and year |
| `data/quality/fao_prices/quality_log.csv` | Latest quality findings for each download date, with counts, examples, severity, and decisions |

The original file is never modified. Its byte order mark is removed only while
parsing. `source_row` is one-based and counts data rows after the header. The
clean table links each observation back to its raw row. The optional CSV
exports the published PostgreSQL table.

## Clean fields

| Fields | Meaning |
| --- | --- |
| `snapshot_id`, `source_row` | Source SHA-256 and data row position |
| `year` | Calendar year |
| `area_m49`, `area_name` | UN M49 code and area name. The code joins to `reporter_m49` and `partner_m49` in `clean.fao_pistachio_trade`. |
| `item_cpc`, `product_basis` | `01375`, tracked as `inshell` like the trade and USDA PSD tables |
| `producer_price_usd_per_tonne` | Price as reported |
| `producer_price_usd_per_kg` | The same price divided by 1,000, in the unit of the trade table's `unit_value_usd_per_kg` |
| `flag` | FAOSTAT flag. Every row in this export is `A`, an official value. |
| `same_as_previous_year` | The area reported exactly the same price for the prior calendar year |

## Validation

- Any of these critical issues stops clean publication: a row without 16
  columns, a domain, element, item, period, or unit other than the annual
  USD per tonne producer price of in-shell pistachios, an invalid year, a
  value that is not a positive number, an M49 code that is malformed or has
  two names, a flag with two descriptions, or a duplicate area and year. All
  pass for the November 23 export.
- After cleaning, the row count and price total must equal the raw rows.
- The `Unit` column says `USD` while the element says `USD/tonne`. Values are
  read as USD per tonne.
- Azerbaijan repeats a price in 2019, 2022, and 2023, which may be a
  carried-forward figure. These rows are kept and marked with
  `same_as_previous_year`.
- Coverage is uneven. The United States, Türkiye, Greece, Cyprus, and Mexico
  have all 14 years and Iran has 13. Australia, Cameroon, China, mainland, and
  Uzbekistan have fewer than five years. Azerbaijan, Cameroon, China, mainland,
  and Iran have no 2024 price. Cameroon is not a known pistachio producer, so
  its two prices need review before use.
- Producer prices are farm-gate prices for in-shell nuts. Trade unit values
  include processing and shipping, so the two differ in level. For example,
  the 2023 US producer price is 4.12 USD/kg, while US exports average
  7.22 USD/kg.
- Prices end in 2024. Next-year price models therefore have about a dozen
  annual observations per country.
