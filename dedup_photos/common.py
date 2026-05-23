from __future__ import annotations

import csv
import re
from datetime import datetime
from pathlib import Path
from typing import TextIO

from dedup_photos.constants import PRIMARY_IMAGE_EXTENSIONS


DATE_DIRECTORY_TOKEN_RE = re.compile(r"(?<!\d)(?:\d{4}-\d{2}-\d{2}|\d{8}|\d{4})(?!\d)")

CSV_FIELDS = [
    "disposition",
    "status",
    "file_role",
    "source_path",
    "destination_path",
    "duplicate_output_path",
    "keeper_path",
    "primary_source_path",
    "group_id",
    "event",
    "reason",
    "message",
    "size_bytes",
    "xxh64",
    "validation_result",
    "action_taken",
    "hash_check",
    "observed_hash",
    "input_root",
    "mode",
    "timestamp",
]


def default_log_path(action: str) -> Path:
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return Path.cwd() / f"{action}_{stamp}.csv"


def prepare_new_output_path(path: Path, description: str) -> Path:
    if path.exists():
        raise ValueError(f"{description} already exists: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def is_primary_image(path: Path) -> bool:
    return path.suffix.lower() in PRIMARY_IMAGE_EXTENSIONS


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
        primary_source_path: Path | str = "",
        source_path: Path | str = "",
        destination_path: Path | str = "",
        duplicate_output_path: Path | str = "",
        keeper_path: Path | str = "",
        size_bytes: int | str = "",
        digest: str = "",
        observed_hash: str = "",
        hash_check: str = "",
        validation_result: str = "",
        action_taken: str = "",
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
                "primary_source_path": str(primary_source_path),
                "source_path": str(source_path),
                "destination_path": str(destination_path),
                "duplicate_output_path": str(duplicate_output_path),
                "keeper_path": str(keeper_path),
                "size_bytes": size_bytes,
                self._hash_field: digest,
                "observed_hash": observed_hash,
                "hash_check": hash_check,
                "validation_result": validation_result,
                "action_taken": action_taken,
                "reason": reason,
                "message": message,
            }
        )
