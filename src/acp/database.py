"""Run PostgreSQL commands using psql and standard PG environment variables."""

import base64
import json
import os
import subprocess

RAW_TABLES = {
    "inventory": "raw.acp_inventory",
    "shipments": "raw.acp_shipments",
    "export_destinations": "raw.acp_export_destinations",
}


def json_value(value):
    """Encode data as a SQL JSONB expression without interpolating raw text."""
    payload = base64.b64encode(json.dumps(value).encode("utf-8")).decode("ascii")
    return f"convert_from(decode('{payload}', 'base64'), 'UTF8')::jsonb"


def query(sql):
    """Execute SQL and return unaligned results, stopping at the first error."""
    env = {"PGDATABASE": "pistachio", **os.environ}
    result = subprocess.run(
        ["psql", "-X", "-qAt", "-w", "-v", "ON_ERROR_STOP=1"],
        input=sql, text=True, capture_output=True, env=env,
    )
    if result.returncode:
        raise RuntimeError(result.stderr.strip() or "PostgreSQL command failed")
    return result.stdout.strip()
