CREATE SCHEMA IF NOT EXISTS raw;
CREATE SCHEMA IF NOT EXISTS clean;

CREATE TABLE IF NOT EXISTS raw.fao_trade_snapshots (
    snapshot_id text PRIMARY KEY,
    manifest jsonb NOT NULL,
    loaded_at timestamptz NOT NULL DEFAULT now()
);

-- Every source record with Item Code 223, all years, cells kept as printed.
CREATE TABLE IF NOT EXISTS raw.fao_trade_rows (
    snapshot_id text REFERENCES raw.fao_trade_snapshots,
    source_row integer,
    payload jsonb NOT NULL,
    PRIMARY KEY (snapshot_id, source_row)
);

-- Reporter country, partner country, and flag code lists shipped in the archive.
CREATE TABLE IF NOT EXISTS raw.fao_trade_code_lists (
    snapshot_id text REFERENCES raw.fao_trade_snapshots,
    code_list text,
    source_row integer,
    payload jsonb NOT NULL,
    PRIMARY KEY (snapshot_id, code_list, source_row)
);

CREATE TABLE IF NOT EXISTS clean.fao_pistachio_trade (
    snapshot_id text NOT NULL,
    quantity_source_row integer NOT NULL,
    value_source_row integer NOT NULL,
    year integer NOT NULL,
    flow text NOT NULL CHECK (flow IN ('export', 'import')),
    reporter_fao_code integer NOT NULL,
    reporter_m49 text NOT NULL,
    reporter_name text NOT NULL,
    partner_fao_code integer NOT NULL,
    partner_m49 text NOT NULL,
    partner_name text NOT NULL,
    item_code integer NOT NULL,
    item_cpc text NOT NULL,
    product_basis text NOT NULL,
    quantity_mt numeric NOT NULL CHECK (quantity_mt >= 0),
    value_kusd numeric NOT NULL CHECK (value_kusd >= 0),
    quantity_flag text NOT NULL,
    value_flag text NOT NULL,
    unit_value_usd_per_kg numeric,
    unit_value_status text NOT NULL
        CHECK (unit_value_status IN ('computed', 'not_computable', 'outlier')),
    is_self_trade boolean NOT NULL,
    CHECK ((unit_value_usd_per_kg IS NULL) = (unit_value_status = 'not_computable')),
    PRIMARY KEY (snapshot_id, year, flow, reporter_fao_code, partner_fao_code),
    FOREIGN KEY (snapshot_id, quantity_source_row) REFERENCES raw.fao_trade_rows,
    FOREIGN KEY (snapshot_id, value_source_row) REFERENCES raw.fao_trade_rows
);
