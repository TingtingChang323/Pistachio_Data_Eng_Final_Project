CREATE SCHEMA IF NOT EXISTS raw;
CREATE SCHEMA IF NOT EXISTS clean;

CREATE TABLE IF NOT EXISTS raw.fao_prices_snapshots (
    snapshot_id text PRIMARY KEY,
    manifest jsonb NOT NULL,
    loaded_at timestamptz NOT NULL DEFAULT now()
);

-- One ordered array of the 16 source cells per data row, kept as printed.
CREATE TABLE IF NOT EXISTS raw.fao_prices_rows (
    snapshot_id text REFERENCES raw.fao_prices_snapshots,
    source_row integer,
    payload jsonb NOT NULL,
    PRIMARY KEY (snapshot_id, source_row)
);

CREATE TABLE IF NOT EXISTS clean.fao_pistachio_producer_prices (
    snapshot_id text NOT NULL,
    source_row integer NOT NULL,
    year integer NOT NULL,
    area_m49 text NOT NULL,
    area_name text NOT NULL,
    item_cpc text NOT NULL,
    product_basis text NOT NULL,
    producer_price_usd_per_tonne numeric NOT NULL CHECK (producer_price_usd_per_tonne > 0),
    producer_price_usd_per_kg numeric NOT NULL CHECK (producer_price_usd_per_kg > 0),
    flag text NOT NULL,
    same_as_previous_year boolean NOT NULL,
    PRIMARY KEY (snapshot_id, area_m49, year),
    FOREIGN KEY (snapshot_id, source_row) REFERENCES raw.fao_prices_rows
);
