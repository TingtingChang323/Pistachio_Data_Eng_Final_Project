CREATE SCHEMA IF NOT EXISTS raw;
CREATE SCHEMA IF NOT EXISTS clean;

CREATE TABLE IF NOT EXISTS raw.usda_psd_snapshots (
    snapshot_id text PRIMARY KEY,
    manifest jsonb NOT NULL,
    loaded_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS raw.usda_psd_rows (
    snapshot_id text REFERENCES raw.usda_psd_snapshots,
    source_row integer,
    payload jsonb NOT NULL,
    PRIMARY KEY (snapshot_id, source_row)
);

CREATE TABLE IF NOT EXISTS clean.usda_psd_pistachios (
    snapshot_id text NOT NULL,
    source_row integer NOT NULL,
    commodity_code text NOT NULL,
    commodity_name text NOT NULL,
    product_basis text NOT NULL,
    geo_code text NOT NULL,
    geo_name text NOT NULL,
    geo_type text NOT NULL CHECK (geo_type IN ('individual', 'region', 'world')),
    market_year text NOT NULL,
    market_year_start integer NOT NULL,
    attribute_id integer NOT NULL,
    attribute_name text NOT NULL,
    quantity_mt bigint,
    value_status text NOT NULL CHECK (value_status IN ('reported', 'missing')),
    CHECK ((quantity_mt IS NULL) = (value_status = 'missing')),
    PRIMARY KEY (snapshot_id, commodity_code, geo_code, market_year, attribute_id),
    FOREIGN KEY (snapshot_id, source_row) REFERENCES raw.usda_psd_rows
);
