from __future__ import annotations

import sys
from dataclasses import dataclass, field
from collections import defaultdict
from typing import TextIO


@dataclass
class Progress:
    mode: str
    total_images: int
    enabled: bool = True
    stream: TextIO = field(default_factory=lambda: sys.stderr)
    phase: str = "action"
    phase_current: int = 0
    phase_total: int = 0
    phase_files_processed: int = 0
    files_processed: int = 0
    images_processed: int = 0
    moved_files: int = 0
    moved_bytes: int = 0
    errors: int = 0
    kept_bytes: int = 0
    size_counts: dict[int, int] = field(default_factory=lambda: defaultdict(int))
    size_collision_groups: int = 0
    size_collision_files: int = 0
    hash_step: str = "HASHING"
    hash_match_groups: int = 0
    hash_match_files: int = 0
    confirmed_duplicate_groups: int = 0
    _hash_bucket_counts: dict[tuple[int, str], int] = field(default_factory=lambda: defaultdict(int))

    def start_phase(self, phase: str, total: int) -> None:
        self.phase = phase
        self.phase_current = 0
        self.phase_total = total
        self.phase_files_processed = 0
        self.render()

    def advance(self, count: int = 1) -> None:
        self.phase_current += count
        self.render()

    def file_processed(self, count: int = 1) -> None:
        self.files_processed += count
        self.phase_files_processed += count
        self.render()

    def image_processed(self, count: int = 1) -> None:
        self.images_processed += count
        self.files_processed += count
        self.phase_files_processed += count
        self.render()

    def moved(self, size_bytes: int = 0, count: int = 1) -> None:
        self.moved_files += count
        self.moved_bytes += size_bytes
        self.render()

    def error(self, count: int = 1) -> None:
        self.errors += count
        self.render()

    def kept(self, size_bytes: int) -> None:
        self.kept_bytes += size_bytes
        self.render()

    def primary_seen(self, size_bytes: int) -> None:
        current = self.size_counts[size_bytes]
        self.size_counts[size_bytes] = current + 1
        if current == 1:
            self.size_collision_groups += 1
            self.size_collision_files += 2
        elif current > 1:
            self.size_collision_files += 1
        self.render()

    def hash_started(self) -> None:
        self.hash_step = "HASHING"
        self.render()

    def hash_seen(self, size_bytes: int, digest: str) -> None:
        self.hash_step = "HASHING"
        key = (size_bytes, digest)
        current = self._hash_bucket_counts[key]
        self._hash_bucket_counts[key] = current + 1
        if current == 1:
            self.hash_match_groups += 1
            self.hash_match_files += 2
        elif current > 1:
            self.hash_match_files += 1
        self.advance()

    def byte_check_started(self) -> None:
        self.hash_step = "BYTE-CHECK"
        self.render()

    def duplicate_group_confirmed(self) -> None:
        self.confirmed_duplicate_groups += 1
        self.render()

    def render(self) -> None:
        if not self.enabled:
            return
        percentage = 100.0 if self.phase_total == 0 else min(100.0, self.phase_current / self.phase_total * 100)
        parts = [
            f"\r{self.phase}:",
            f"done={percentage:5.1f}%",
        ]
        if self.phase.startswith("scan"):
            parts.extend(
                [
                    f"entries_scanned={self.phase_current}/{self.phase_total}",
                    f"images_found={sum(self.size_counts.values())}",
                    f"size_collision_groups={self.size_collision_groups}",
                    f"size_collision_files={self.size_collision_files}",
                ]
            )
        elif self.phase == "hash":
            parts.extend(
                [
                    f"step={self.hash_step}",
                    f"hashed={self.phase_current}/{self.phase_total}",
                    f"hash_match_groups={self.hash_match_groups}",
                    f"hash_match_files={self.hash_match_files}",
                    f"confirmed_duplicate_groups={self.confirmed_duplicate_groups}",
                ]
            )
        else:
            parts.extend(
                [
                    f"images={self.images_processed}/{self.total_images}",
                    f"files={self.phase_files_processed}",
                    f"dupe_files={self.moved_files}",
                    f"dupe_size={format_bytes(self.moved_bytes)}",
                    f"errors={self.errors}",
                    f"kept={format_bytes(self.kept_bytes)}",
                ]
            )
        line = " ".join(parts)
        self.stream.write(line)
        self.stream.flush()

    def finish(self) -> None:
        if not self.enabled:
            return
        self.render()
        self.stream.write("\n")
        self.stream.flush()


def format_bytes(size_bytes: int) -> str:
    value = float(size_bytes)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if value < 1024 or unit == "TiB":
            if unit == "B":
                return f"{int(value)} {unit}"
            return f"{value:.2f} {unit}"
        value /= 1024
