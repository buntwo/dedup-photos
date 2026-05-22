from __future__ import annotations

import csv
import re
from datetime import datetime
from pathlib import Path
from typing import TextIO

from dedup_photos.constants import PRIMARY_IMAGE_EXTENSIONS


DATE_DIRECTORY_TOKEN_RE = re.compile(r"(?<!\d)(?:\d{4}-\d{2}-\d{2}|\d{8}|\d{4})(?!\d)")

CSV_FIELDS = [
    "timestamp",
    "mode",
    "disposition",
    "event",
    "status",
    "group_id",
    "file_role",
    "input_root",
    "source_path",
    "destination_path",
    "keeper_path",
    "size_bytes",
    "xxh64",
    "reason",
    "message",
]


def default_log_path() -> Path:
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return Path.cwd() / f"dedup_photos_{stamp}.csv"


def is_primary_image(path: Path) -> bool:
    return path.suffix.lower() in PRIMARY_IMAGE_EXTENSIONS


def sidecar_belongs_to_primary(candidate: Path, primary_path: Path) -> bool:
    if candidate.stem == primary_path.stem:
        return True
    return candidate.name.lower().startswith(f"{primary_path.name}.".lower())


def date_directory_score(path: Path) -> int:
    return sum(1 for part in path.parent.parts if DATE_DIRECTORY_TOKEN_RE.search(part))


def has_takeout_segment(path: Path) -> bool:
    return any("takeout" in part.lower() for part in path.parts)


def has_mobilebackup_segment(path: Path) -> bool:
    return any("mobilebackup" in part.lower() for part in path.parts)


class CsvLogger:
    def __init__(self, file: TextIO, mode: str, hash_field: str = "xxh64") -> None:
        self._hash_field = hash_field
        fieldnames = [hash_field if field == "xxh64" else field for field in CSV_FIELDS]
        self._writer = csv.DictWriter(file, fieldnames=fieldnames)
        self._mode = mode
        self._writer.writeheader()

    def row(
        self,
        *,
        disposition: str,
        event: str,
        status: str,
        group_id: str = "",
        file_role: str = "",
        input_root: str = "",
        source_path: Path | str = "",
        destination_path: Path | str = "",
        keeper_path: Path | str = "",
        size_bytes: int | str = "",
        digest: str = "",
        reason: str = "",
        message: str = "",
    ) -> None:
        self._writer.writerow(
            {
                "timestamp": datetime.now().isoformat(timespec="seconds"),
                "mode": self._mode,
                "disposition": disposition,
                "event": event,
                "status": status,
                "group_id": group_id,
                "file_role": file_role,
                "input_root": input_root,
                "source_path": str(source_path),
                "destination_path": str(destination_path),
                "keeper_path": str(keeper_path),
                "size_bytes": size_bytes,
                self._hash_field: digest,
                "reason": reason,
                "message": message,
            }
        )
