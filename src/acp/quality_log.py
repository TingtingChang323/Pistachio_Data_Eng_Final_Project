"""Record ACP quality findings and validation summaries in a shared CSV."""

import csv
import fcntl
from pathlib import Path
from tempfile import NamedTemporaryFile

FIELDS = (
    "report_month", "phase", "table", "check_name", "status", "observed_count",
    "example", "severity", "decision", "calculated_lb", "reported_lb",
    "difference_lb", "source_file",
)


def write_log(path, month, source_file, findings):
    """Replace one month's findings atomically while preserving other months."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.with_suffix(".lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        prior = []
        if path.exists():
            with path.open(newline="", encoding="utf-8") as handle:
                prior = [row for row in csv.DictReader(handle) if row["report_month"] != month]
        with NamedTemporaryFile(mode="w", newline="", encoding="utf-8",
                                dir=path.parent, delete=False) as handle:
            temp_path = Path(handle.name)
            writer = csv.DictWriter(handle, fieldnames=FIELDS, extrasaction="ignore")
            writer.writeheader()
            for row in prior:
                writer.writerow(row)
            for finding in findings:
                writer.writerow({"report_month": month, "source_file": source_file,
                                 **finding, "check_name": finding["check"]})
        temp_path.replace(path)
