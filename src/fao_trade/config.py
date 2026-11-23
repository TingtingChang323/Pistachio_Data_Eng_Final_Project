"""Paths and defaults for the FAOSTAT Detailed Trade Matrix pistachio pipeline."""

from datetime import date
from pathlib import Path

from src.acp.config import REPO_ROOT, S3_BUCKET, S3_REGION

DATA_DIR = REPO_ROOT / "data"
DATASET = "fao_trade"
DOWNLOAD_DATE = date(2026, 11, 23)
ARCHIVE_FILE = "Trade_DetailedTradeMatrix_E_All_Data_(Normalized).zip"
SOURCE_URL = "https://www.fao.org/faostat/en/#data/TM"
QUALITY_LOG_PATH = DATA_DIR / "quality" / DATASET / "quality_log.csv"

# Archive members used by the pipeline, located by file name inside the zip.
TRADE_FILE = "Trade_DetailedTradeMatrix_E_All_Data_(Normalized).csv"
CODE_LIST_FILES = {
    "reporter_countries": "Trade_DetailedTradeMatrix_E_ReporterCountries.csv",
    "partner_countries": "Trade_DetailedTradeMatrix_E_PartnerCountries.csv",
    "flags": "Trade_DetailedTradeMatrix_E_Flags.csv",
}
EXTRACT_FILE = "pistachio_rows.csv"
CLEAN_FILE = "fao_pistachio_trade.csv"

# FAOSTAT item 223 is "Pistachios, in shell" (CPC 01375), the only pistachio item.
ITEM_CODE = "223"
YEAR_RANGE = (2000, 2024)


def batch_dir(layer: str, retrieved_on: date) -> Path:
    """Locate a data layer by the date the source archive was downloaded."""
    return DATA_DIR / layer / DATASET / retrieved_on.isoformat()
