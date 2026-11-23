"""Editable defaults for the local ACP pipeline."""

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
REPORT_MONTH = "2026-09"
RAW_DIR = REPO_ROOT / "data/raw/acp"
EXTRACT_DIR = REPO_ROOT / "data/extract/acp"
PDF_PATH = RAW_DIR / REPORT_MONTH / f"{REPORT_MONTH.replace('-', '_')}.pdf"
CLEAN_CSV_DIR = REPO_ROOT / "data/clean/acp" / REPORT_MONTH
QUALITY_LOG_PATH = REPO_ROOT / "data/quality/acp/quality_log.csv"
S3_BUCKET = "pistachio-project"
S3_REGION = "us-east-1"
