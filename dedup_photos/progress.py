from __future__ import annotations

import sys
from dataclasses import dataclass, field
from typing import TextIO


@dataclass
class Progress:
    mode: str
    total_images: int
    enabled: bool = True
    stream: TextIO = field(default_factory=lambda: sys.stderr)
    files_processed: int = 0
    images_processed: int = 0
    moved_files: int = 0
    errors: int = 0
    kept_bytes: int = 0

    def file_processed(self, count: int = 1) -> None:
        self.files_processed += count
        self.render()

    def image_processed(self, count: int = 1) -> None:
        self.images_processed += count
        self.files_processed += count
        self.render()

    def moved(self, count: int = 1) -> None:
        self.moved_files += count
        self.render()

    def error(self, count: int = 1) -> None:
        self.errors += count
        self.render()

    def kept(self, size_bytes: int) -> None:
        self.kept_bytes += size_bytes
        self.render()

    def render(self) -> None:
        if not self.enabled:
            return
        percentage = 100.0 if self.total_images == 0 else min(100.0, self.images_processed / self.total_images * 100)
        line = (
            f"\r{self.mode}: files={self.files_processed} "
            f"images={self.images_processed}/{self.total_images} "
            f"done={percentage:5.1f}% moved={self.moved_files} "
            f"errors={self.errors} kept={format_bytes(self.kept_bytes)}"
        )
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
