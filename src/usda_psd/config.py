"""Paths and defaults for USDA PSD pistachio exports."""

from datetime import date
from pathlib import Path

from src.acp.config import REPO_ROOT, S3_BUCKET, S3_REGION

DATA_DIR = REPO_ROOT / "data"
DATASET = "usda_psd"
DOWNLOAD_DATE = date(2026, 11, 22)
SOURCE_FILE = "psd.xls"
SOURCE_URL = "https://apps.fas.usda.gov/PSDOnline/app/index.html#/app/advQuery"
QUALITY_LOG_PATH = DATA_DIR / "quality" / DATASET / "quality_log.csv"


def batch_dir(layer: str, retrieved_on: date) -> Path:
    """Locate a data layer by the date the source file was downloaded."""
    return DATA_DIR / layer / DATASET / retrieved_on.isoformat()
