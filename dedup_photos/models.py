from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class ManifestEntry:
    manifest_path: Path
    batch_root: str
    nas_root: str
    nas_root_label: str
    nas_path: Path
    relative_path: Path
    size_bytes: int
    xxh128: str
    sidecar_paths: tuple[Path, ...]
    sidecar_relative_paths: tuple[Path, ...]
    sidecar_sizes: tuple[int, ...]
    sidecar_xxh128s: tuple[str, ...]


@dataclass(frozen=True)
class ManifestInventoryRow:
    manifest_path: Path
    batch_root: str
    nas_root: str
    nas_root_label: str
    group_id: str
    file_role: str
    status: str
    reason: str
    nas_path: Path
    relative_path: Path
    primary_nas_path: Path | None
    primary_relative_path: Path | None
    size_bytes: int
    xxh128: str


@dataclass(frozen=True)
class ManifestPlanResult:
    log_path: Path
    duplicate_groups: int
    duplicate_files: int
    skipped_groups: int


@dataclass(frozen=True)
class ManifestVerifyResult:
    log_path: Path
    checked_groups: int
    failed_groups: int


@dataclass(frozen=True)
class VerifyMoveResult:
    log_path: Path
    checked_paths: int
    matched_paths: int
    failed_paths: int
    unexpected_outputs: int


@dataclass(frozen=True)
class ExecutePlanResult:
    log_path: Path
    bundles: int
    moved_bundles: int
    planned_bundles: int
    already_moved_bundles: int
    skipped_bundles: int
    orphan_sidecars: int


@dataclass(frozen=True)
class PlanRow:
    row: dict[str, str]
    source_path: Path
    destination_path: Path
    size_bytes: int
    group_id: str
    file_role: str
    disposition: str
    hash_value: str
    primary_source_path: Path | None


@dataclass(frozen=True)
class PlanBundle:
    keeper: PlanRow | None
    primary: PlanRow
    sidecars: tuple[PlanRow, ...]
