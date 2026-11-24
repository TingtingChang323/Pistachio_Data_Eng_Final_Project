"""Paths and defaults for the FAOSTAT pistachio producer price pipeline."""

from datetime import date
from pathlib import Path

from src.acp.config import REPO_ROOT, S3_BUCKET, S3_REGION

DATA_DIR = REPO_ROOT / "data"
DATASET = "fao_prices"
DOWNLOAD_DATE = date(2026, 11, 23)
SOURCE_FILE = "FAOSTAT_data_en_11-23-2026.csv"
SOURCE_URL = "https://www.fao.org/faostat/en/#data/PP"
CLEAN_FILE = "fao_pistachio_producer_prices.csv"
QUALITY_LOG_PATH = DATA_DIR / "quality" / DATASET / "quality_log.csv"


def batch_dir(layer: str, retrieved_on: date) -> Path:
    """Locate a data layer by the date the source file was downloaded."""
    return DATA_DIR / layer / DATASET / retrieved_on.isoformat()
