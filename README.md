# Pistachio Data Engineering

## Setup (macOS)

```bash
brew install postgresql@17
brew services start postgresql@17
export PATH="$(brew --prefix postgresql@17)/bin:$PATH"

python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt

export PGDATABASE=pistachio
# First-time setup only, if the database does not already exist:
# createdb "$PGDATABASE"
psql -X -v ON_ERROR_STOP=1 -f sql/acp.sql
```

For another PostgreSQL server, set `PGHOST`, `PGPORT`, `PGUSER`, and `PGDATABASE`. Use `~/.pgpass` for password authentication.

## Run ACP

Set `REPORT_MONTH` in `src/acp/config.py`. Adjust `PDF_PATH` there if the filename differs.

```bash
cd src/acp
python extract.py && python ingestion.py && python clean.py
```

## Layout

```text
Pistachio_Data_Eng_Final_Project/
├── .gitignore
├── README.md
├── requirements.txt
├── docs/
│   └── acp_data_dictionary.md
├── sql/
│   └── acp.sql
├── src/
│   └── acp/
│       ├── extract.py
│       ├── ingestion.py
│       ├── clean.py
│       ├── config.py
│       ├── quality_log.py
│       └── database.py
└── data/          # Local data
    ├── raw/
    │   └── acp/
    │       ├── 2026-09/
    │       └── 2026-10/
    ├── extract/
    │   └── acp/
    │       ├── 2026-09/
    │       └── 2026-10/
    ├── clean/
    │   └── acp/
    │       ├── 2026-09/
    │       │   ├── inventory.csv
    │       │   ├── shipments.csv
    │       │   └── export_destinations.csv
    │       └── 2026-10/
    │           ├── inventory.csv
    │           ├── shipments.csv
    │           └── export_destinations.csv
    └── quality/
        └── acp/
            ├── quality_log.csv
            └── quality_log.lock
```

The JSONL files are from the earlier workflow. The current loader writes raw
records directly to PostgreSQL. Clean CSV exports are optional.
