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
psql -X -v ON_ERROR_STOP=1 -f sql/usda_psd.sql
psql -X -v ON_ERROR_STOP=1 -f sql/fao_trade.sql
```

For another PostgreSQL server, set `PGHOST`, `PGPORT`, `PGUSER`, and `PGDATABASE`. Use `~/.pgpass` for password authentication.

## Run ACP Dataset

Run from the repository root. Set `REPORT_MONTH` in `src/acp/config.py` and
adjust `PDF_PATH` if the filename differs.

```bash
python -m src.acp.extract &&
python -m src.acp.ingestion &&
python -m src.acp.clean
```

## Run USDA PSD Dataset

Run from the repository root. The default download date is `2026-11-22`.

```bash
python -m src.usda_psd.extract &&
python -m src.usda_psd.ingestion &&
python -m src.usda_psd.clean
```

- **Other dates** Put `psd.xls` in `data/raw/usda_psd/YYYY-MM-DD/` and pass `--retrieved-on YYYY-MM-DD` to each command.
- **CSV export** Add `--export-csv` to the clean command.

See the [USDA PSD data dictionary](docs/usda_psd_data_dictionary.md).

## Run FAOSTAT Trade Dataset

Run from the repository root. The default download date is `2026-11-23`.
Keep the downloaded archive zipped. Extract streams the 8.5 GB CSV inside it
and takes about a minute.

```bash
python -m src.fao_trade.extract &&
python -m src.fao_trade.ingestion &&
python -m src.fao_trade.clean
```

- **Other dates** Put `Trade_DetailedTradeMatrix_E_All_Data_(Normalized).zip` in `data/raw/fao_trade/YYYY-MM-DD/` and pass `--retrieved-on YYYY-MM-DD` to each command.
- **CSV export** Add `--export-csv` to the clean command.

See the [FAOSTAT trade data dictionary](docs/fao_trade_data_dictionary.md).

## Optional S3 Upload

Set your AWS profile once per terminal session.

```bash
export AWS_PROFILE=pistachio
```

Add `--upload-s3` to any dataset's extract command. Reruns skip identical
objects and fail if an existing key contains different content.

To check bucket versioning, run
`aws s3api get-bucket-versioning --bucket pistachio-project --region us-east-1`.

## Layout

```text
Pistachio_Data_Eng_Final_Project/
├── .gitignore
├── README.md
├── requirements.txt
├── docs/
│   ├── acp_data_dictionary.md
│   ├── usda_psd_data_dictionary.md
│   └── fao_trade_data_dictionary.md
├── sql/
│   ├── acp.sql
│   ├── usda_psd.sql
│   └── fao_trade.sql
├── src/
│   ├── acp/
│   │   ├── extract.py
│   │   ├── ingestion.py
│   │   ├── clean.py
│   │   ├── config.py
│   │   ├── quality_log.py
│   │   ├── s3_upload.py
│   │   └── database.py
│   ├── usda_psd/
│   └── fao_trade/
│       ├── extract.py
│       ├── ingestion.py
│       ├── clean.py
│       └── config.py
└── data/          # Local data
    ├── raw/
    │   ├── acp/
    │   ├── usda_psd/2026-11-22/psd.xls
    │   └── fao_trade/2026-11-23/Trade_DetailedTradeMatrix_E_All_Data_(Normalized).zip
    ├── extract/
    │   ├── acp/
    │   ├── usda_psd/2026-11-22/manifest.json
    │   └── fao_trade/2026-11-23/
    │       ├── manifest.json
    │       ├── pistachio_rows.csv
    │       └── Trade_DetailedTradeMatrix_E_*Countries.csv, *_Flags.csv
    ├── clean/
    │   ├── acp/
    │   ├── usda_psd/2026-11-22/pistachio_psd.csv
    │   └── fao_trade/2026-11-23/fao_pistachio_trade.csv
    └── quality/
        ├── acp/quality_log.csv
        ├── usda_psd/quality_log.csv
        └── fao_trade/quality_log.csv
```

The JSONL files are from the earlier workflow. The current loader writes raw
records directly to PostgreSQL. Clean CSV exports are optional.
