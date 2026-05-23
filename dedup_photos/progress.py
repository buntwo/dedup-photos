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
    phase: str = "action"
    phase_current: int = 0
    phase_total: int = 0
    moved_files: int = 0
    moved_bytes: int = 0
    errors: int = 0
    manifest_entries_scanned: int = 0
    manifest_primaries_hashed: int = 0
    manifest_sidecars_hashed: int = 0
    manifest_uncategorized_hashed: int = 0
    manifest_bytes_hashed: int = 0
    manifest_bytes_compared: int = 0
    manifest_rows_written: int = 0
    manifest_manifests_loaded: int = 0
    manifest_entries_loaded: int = 0
    manifest_plan_rows_loaded: int = 0
    manifest_duplicate_groups: int = 0
    manifest_unique_files: int = 0
    manifest_planned_moves: int = 0
    manifest_skipped_groups: int = 0
    manifest_groups_checked: int = 0
    manifest_failed_groups: int = 0
    manifest_paths_checked: int = 0
    manifest_paths_matched: int = 0
    manifest_unexpected_outputs: int = 0
    manifest_bundles_processed: int = 0

    def note(self, message: str) -> None:
        if not self.enabled:
            return
        self.stream.write(f"{message}\n")
        self.stream.flush()

    def start_indeterminate_phase(self, phase: str) -> None:
        self.phase = phase
        self.phase_current = 0
        self.phase_total = -1
        self.render()

    def start_phase(self, phase: str, total: int) -> None:
        self.phase = phase
        self.phase_current = 0
        self.phase_total = total
        self.render()

    def advance(self, count: int = 1) -> None:
        self.phase_current += count
        self.render()

    def moved(self, size_bytes: int = 0, count: int = 1) -> None:
        self.moved_files += count
        self.moved_bytes += size_bytes
        self.render()

    def error(self, count: int = 1) -> None:
        self.errors += count
        self.render()

    def manifest_entry_scanned(self) -> None:
        self.manifest_entries_scanned += 1
        self.advance()

    def manifest_hash_bytes(self, size_bytes: int) -> None:
        self.manifest_bytes_hashed += size_bytes
        self.render()

    def manifest_file_hashed(self, file_role: str) -> None:
        if file_role == "sidecar":
            self.manifest_sidecars_hashed += 1
        elif file_role == "uncategorized":
            self.manifest_uncategorized_hashed += 1
        else:
            self.manifest_primaries_hashed += 1
        self.render()

    def manifest_row_written(self) -> None:
        self.manifest_rows_written += 1
        self.render()

    def manifest_entry_loaded(self) -> None:
        self.manifest_entries_loaded += 1
        self.render()

    def manifest_manifest_loaded(self) -> None:
        self.manifest_manifests_loaded += 1
        self.advance()

    def manifest_plan_row_loaded(self) -> None:
        self.manifest_plan_rows_loaded += 1
        self.render()

    def manifest_group_stats(self, duplicate_groups: int, unique_files: int) -> None:
        self.manifest_duplicate_groups = duplicate_groups
        self.manifest_unique_files = unique_files
        self.render()

    def manifest_planned_move(self, count: int = 1) -> None:
        self.manifest_planned_moves += count
        self.render()

    def manifest_skipped_group(self) -> None:
        self.manifest_skipped_groups += 1
        self.error()

    def manifest_compare_bytes(self, size_bytes: int) -> None:
        self.manifest_bytes_compared += size_bytes
        self.render()

    def manifest_group_checked(self, failed: bool = False) -> None:
        self.manifest_groups_checked += 1
        if failed:
            self.manifest_failed_groups += 1
            self.error()
        self.advance()

    def manifest_path_checked(self, matched: bool) -> None:
        self.manifest_paths_checked += 1
        if matched:
            self.manifest_paths_matched += 1
        else:
            self.error()
        self.advance()

    def manifest_unexpected_output(self) -> None:
        self.manifest_unexpected_outputs += 1
        self.error()

    def manifest_bundle_processed(self) -> None:
        self.manifest_bundles_processed += 1
        self.advance()

    def render(self) -> None:
        if not self.enabled:
            return
        if self.phase_total < 0:
            done = "done=  n/a"
        else:
            percentage = 100.0 if self.phase_total == 0 else min(100.0, self.phase_current / self.phase_total * 100)
            done = f"done={percentage:5.1f}%"
        parts = [
            f"\r{self.phase}:",
            done,
            *self.progress_parts(),
        ]
        self.stream.write(" ".join(parts))
        self.stream.flush()

    def progress_parts(self) -> list[str]:
        if self.phase == "manifest-scan":
            return [
                f"files_seen={self.manifest_entries_scanned}",
            ]
        if self.phase == "manifest-hash":
            return [
                f"entries_scanned={self.manifest_entries_scanned}",
                f"primaries_hashed={self.manifest_primaries_hashed}",
                f"sidecars_hashed={self.manifest_sidecars_hashed}",
                f"uncategorized_hashed={self.manifest_uncategorized_hashed}",
                f"bytes_hashed={format_bytes(self.manifest_bytes_hashed)}",
                f"rows_written={self.manifest_rows_written}",
            ]
        if self.phase == "manifest-load":
            return [
                f"manifests_loaded={self.manifest_manifests_loaded}",
                f"entries_loaded={self.manifest_entries_loaded}",
            ]
        if self.phase == "manifest-plan":
            return [
                f"duplicate_groups={self.manifest_duplicate_groups}",
                f"unique_files={self.manifest_unique_files}",
                f"planned_moves={self.manifest_planned_moves}",
                f"skipped_groups={self.manifest_skipped_groups}",
                f"errors={self.errors}",
            ]
        if self.phase == "manifest-verify-bytes":
            return [
                f"duplicate_groups={self.manifest_duplicate_groups}",
                f"groups_checked={self.manifest_groups_checked}",
                f"bytes_compared={format_bytes(self.manifest_bytes_compared)}",
                f"failed_groups={self.manifest_failed_groups}",
                f"errors={self.errors}",
            ]
        if self.phase == "manifest-load-plan":
            return [
                f"plan_rows_loaded={self.manifest_plan_rows_loaded}",
            ]
        if self.phase == "manifest-execute":
            return [
                f"bundles={self.manifest_bundles_processed}",
                f"planned_files={self.manifest_planned_moves}",
                f"moved_files={self.moved_files}",
                f"moved_size={format_bytes(self.moved_bytes)}",
                f"errors={self.errors}",
            ]
        if self.phase == "manifest-verify-move":
            return [
                f"paths_checked={self.manifest_paths_checked}",
                f"paths_matched={self.manifest_paths_matched}",
                f"errors={self.errors}",
            ]
        if self.phase == "manifest-output-scan":
            return [
                f"output_entries_scanned={self.phase_current}",
                f"unexpected_outputs={self.manifest_unexpected_outputs}",
                f"errors={self.errors}",
            ]
        return [
            f"manifests_loaded={self.manifest_manifests_loaded}",
            f"entries_loaded={self.manifest_entries_loaded}",
            f"errors={self.errors}",
        ]

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
