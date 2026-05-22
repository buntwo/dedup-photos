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
        elif self.phase.startswith("manifest"):
            parts.extend(self.manifest_progress_parts())
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

    def manifest_progress_parts(self) -> list[str]:
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
