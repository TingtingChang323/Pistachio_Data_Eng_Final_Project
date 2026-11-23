"""Store USDA PSD quality findings and complete CSV exports."""

import csv
import fcntl
from dataclasses import asdict, dataclass
from pathlib import Path
from tempfile import NamedTemporaryFile


@dataclass(frozen=True)
class Finding:
    """A check result, its observed evidence, and the handling decision."""

    phase: str
    check: str
    status: str
    severity: str
    count: int
    example: str
    decision: str


FIELDS = (
    "retrieved_on", "snapshot_id", "source_file",
    "phase", "check", "status", "severity", "count", "example", "decision",
)


def write_csv(path: Path, fields, rows) -> None:
    """Write a whole CSV before replacing any previous export."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with NamedTemporaryFile(
        mode="w", encoding="utf-8", newline="", dir=path.parent, delete=False
    ) as file:
        temporary_path = Path(file.name)
        try:
            writer = csv.DictWriter(file, fieldnames=fields)
            writer.writeheader()
            writer.writerows(rows)
            file.close()
            temporary_path.replace(path)
        finally:
            temporary_path.unlink(missing_ok=True)


def write_log(path: Path, retrieved_on: str, manifest: dict, findings: list[Finding]) -> None:
    """Replace the findings for one download date and retain other batches."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.with_suffix(".lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        previous = []
        if path.exists():
            with path.open(newline="", encoding="utf-8") as file:
                previous = [row for row in csv.DictReader(file) if row["retrieved_on"] != retrieved_on]
        context = {
            "retrieved_on": retrieved_on,
            "snapshot_id": manifest.get("source_sha256", ""),
            "source_file": manifest.get("source_file", ""),
        }
        write_csv(path, FIELDS, previous + [{**context, **asdict(item)} for item in findings])
