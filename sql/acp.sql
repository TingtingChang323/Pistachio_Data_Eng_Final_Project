CREATE SCHEMA IF NOT EXISTS raw;
CREATE SCHEMA IF NOT EXISTS clean;

CREATE TABLE IF NOT EXISTS raw.acp_reports (
    report_month text PRIMARY KEY,
    manifest jsonb NOT NULL,
    loaded_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS raw.acp_inventory (
    report_month text REFERENCES raw.acp_reports,
    source_row integer,
    payload jsonb NOT NULL,
    PRIMARY KEY (report_month, source_row)
);
CREATE TABLE IF NOT EXISTS raw.acp_shipments (LIKE raw.acp_inventory INCLUDING ALL);
CREATE TABLE IF NOT EXISTS raw.acp_export_destinations (LIKE raw.acp_inventory INCLUDING ALL);

CREATE TABLE IF NOT EXISTS clean.acp_inventory (
    report_month text NOT NULL,
    as_of_date date,
    crop_year text NOT NULL,
    inventory_item text NOT NULL,
    product_form text NOT NULL,
    quantity_lb bigint NOT NULL,
    source_file text NOT NULL,
    source_page integer NOT NULL,
    PRIMARY KEY (report_month, crop_year, inventory_item, product_form)
);

CREATE TABLE IF NOT EXISTS clean.acp_shipments (
    report_month text NOT NULL,
    observation_month text NOT NULL,
    crop_year text NOT NULL,
    period_type text NOT NULL,
    flow text NOT NULL,
    product_form text NOT NULL,
    quantity_lb bigint NOT NULL,
    source_file text NOT NULL,
    source_page integer NOT NULL,
    PRIMARY KEY (report_month, crop_year, period_type, flow, product_form)
);

CREATE TABLE IF NOT EXISTS clean.acp_export_destinations (
    report_month text NOT NULL,
    observation_month text NOT NULL,
    crop_year text NOT NULL,
    period_type text NOT NULL,
    region_raw text NOT NULL,
    destination_raw text NOT NULL,
    destination_type text NOT NULL,
    product_form text NOT NULL,
    quantity_lb bigint NOT NULL,
    value_status text NOT NULL CHECK (value_status IN ('reported', 'inferred_zero')),
    source_file text NOT NULL,
    source_page integer NOT NULL,
    PRIMARY KEY (report_month, crop_year, period_type, region_raw, destination_raw, product_form)
);
