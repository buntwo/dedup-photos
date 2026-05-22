from __future__ import annotations

import csv
import re
import shutil
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterable

import xxhash

from dedup_photos.constants import HASH_CHUNK_SIZE, MANIFEST_VERSION
from dedup_photos.common import (
    CsvLogger,
    date_directory_score,
    default_log_path,
    has_mobilebackup_segment,
    has_takeout_segment,
    is_primary_image,
)
from dedup_photos.progress import Progress
from dedup_photos.inventory import classify_manifest_files, regular_files_by_directory


MANIFEST_FIELDS = [
    "manifest_version",
    "created_at",
    "batch_root",
    "nas_root",
    "nas_root_label",
    "group_id",
    "file_role",
    "status",
    "reason",
    "nas_path",
    "relative_path",
    "primary_nas_path",
    "primary_relative_path",
    "size_bytes",
    "xxh128",
]

VERIFY_MOVE_DATE_DIRECTORY_TOKEN_RE = re.compile(r"(?<!\d)(?:\d{4}-\d{2}-\d{2}|\d{8}|\d{4})(?!\d)")


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


def hash_file_xxh128(path: Path, progress: Progress | None = None, file_role: str = "primary") -> str:
    hasher = xxhash.xxh128()
    with path.open("rb", buffering=0) as file:
        while chunk := file.read(HASH_CHUNK_SIZE):
            hasher.update(chunk)
            if progress is not None:
                progress.manifest_hash_bytes(len(chunk))
    if progress is not None:
        progress.manifest_file_hashed(file_role)
    return hasher.hexdigest()


def generate_manifest(
    local_batch_root: Path,
    nas_root: Path,
    manifest_path: Path,
    show_progress: bool = False,
) -> Path:
    local_root = local_batch_root.resolve()
    if not local_root.is_dir():
        raise ValueError(f"local batch root is not a directory: {local_batch_root}")
    if manifest_path.exists():
        raise ValueError(f"manifest already exists: {manifest_path}")
    validate_manifest_roots(local_root, nas_root)

    nas_root_str = str(nas_root)
    nas_root_label = nas_root.name
    created_at = datetime.now().isoformat(timespec="seconds")
    progress = Progress(mode="manifest", total_images=0, enabled=show_progress)

    try:
        files_by_directory = regular_files_by_directory(local_root)
        primary_paths = tuple(
            path
            for directory in sorted(files_by_directory, key=lambda item: item.relative_to(local_root).as_posix())
            for path in sorted(
                (file_path for file_path in files_by_directory[directory] if is_primary_image(file_path)),
                key=lambda item: item.name.lower(),
            )
        )
        sidecars_by_primary, uncategorized = classify_manifest_files(files_by_directory, primary_paths)
        regular_file_count = sum(len(paths) for paths in files_by_directory.values())
        group_ids = {
            path: f"f{index:06d}"
            for index, path in enumerate(
                primary_paths,
                start=1,
            )
        }
        progress.start_phase("manifest-hash", regular_file_count)
        manifest_path.parent.mkdir(parents=True, exist_ok=True)
        with manifest_path.open("w", newline="", encoding="utf-8") as file:
            writer = csv.DictWriter(file, fieldnames=MANIFEST_FIELDS)
            writer.writeheader()
            for directory in sorted(files_by_directory, key=lambda item: item.relative_to(local_root).as_posix()):
                directory_primaries = [
                    path
                    for path in sorted(files_by_directory[directory], key=lambda item: item.name.lower())
                    if is_primary_image(path)
                ]
                for path in directory_primaries:
                    write_manifest_file_row(
                        writer,
                        path=path,
                        local_root=local_root,
                        nas_root=nas_root,
                        manifest_version=MANIFEST_VERSION,
                        created_at=created_at,
                        batch_root=str(local_root),
                        nas_root_str=nas_root_str,
                        nas_root_label=nas_root_label,
                        group_id=group_ids[path],
                        file_role="primary",
                        status="included",
                        reason="",
                        primary_path=None,
                        progress=progress,
                    )
                    for sidecar in sorted(sidecars_by_primary[path], key=lambda item: item.name.lower()):
                        write_manifest_file_row(
                            writer,
                            path=sidecar,
                            local_root=local_root,
                            nas_root=nas_root,
                            manifest_version=MANIFEST_VERSION,
                            created_at=created_at,
                            batch_root=str(local_root),
                            nas_root_str=nas_root_str,
                            nas_root_label=nas_root_label,
                            group_id=group_ids[path],
                            file_role="sidecar",
                            status="included",
                            reason="",
                            primary_path=path,
                            progress=progress,
                        )
                directory_uncategorized = [
                    path
                    for path in sorted(files_by_directory[directory], key=lambda item: item.name.lower())
                    if path in uncategorized
                ]
                for path in directory_uncategorized:
                    write_manifest_file_row(
                        writer,
                        path=path,
                        local_root=local_root,
                        nas_root=nas_root,
                        manifest_version=MANIFEST_VERSION,
                        created_at=created_at,
                        batch_root=str(local_root),
                        nas_root_str=nas_root_str,
                        nas_root_label=nas_root_label,
                        group_id="",
                        file_role="uncategorized",
                        status="skipped",
                        reason=uncategorized[path],
                        primary_path=None,
                        progress=progress,
                    )
    finally:
        progress.finish()
    return manifest_path


def write_manifest_file_row(
    writer: csv.DictWriter,
    *,
    path: Path,
    local_root: Path,
    nas_root: Path,
    manifest_version: str,
    created_at: str,
    batch_root: str,
    nas_root_str: str,
    nas_root_label: str,
    group_id: str,
    file_role: str,
    status: str,
    reason: str,
    primary_path: Path | None,
    progress: Progress,
) -> None:
    progress.manifest_entry_scanned()
    relative_path = path.relative_to(local_root)
    primary_relative_path = primary_path.relative_to(local_root) if primary_path is not None else None
    writer.writerow(
        {
            "manifest_version": manifest_version,
            "created_at": created_at,
            "batch_root": batch_root,
            "nas_root": nas_root_str,
            "nas_root_label": nas_root_label,
            "group_id": group_id,
            "file_role": file_role,
            "status": status,
            "reason": reason,
            "nas_path": str(nas_root / relative_path),
            "relative_path": relative_path.as_posix(),
            "primary_nas_path": str(nas_root / primary_relative_path) if primary_relative_path is not None else "",
            "primary_relative_path": primary_relative_path.as_posix() if primary_relative_path is not None else "",
            "size_bytes": path.stat().st_size,
            "xxh128": hash_file_xxh128(path, progress, file_role),
        }
    )
    progress.manifest_row_written()


def validate_manifest_roots(local_root: Path, nas_root: Path, structure_depth: int = 2) -> None:
    if not nas_root.exists():
        raise ValueError(f"NAS root does not exist: {nas_root}")
    if not nas_root.is_dir():
        raise ValueError(f"NAS root is not a directory: {nas_root}")
    if local_root.name != nas_root.name:
        raise ValueError(
            f"local batch root basename must match NAS root basename: {local_root.name!r} != {nas_root.name!r}"
        )

    local_dirs = directory_relative_paths(local_root, structure_depth)
    nas_dirs = directory_relative_paths(nas_root, structure_depth)
    missing_on_nas = sorted(local_dirs - nas_dirs)
    extra_on_nas = sorted(nas_dirs - local_dirs)
    if missing_on_nas:
        sample = ", ".join(path.as_posix() for path in missing_on_nas[:5])
        raise ValueError(f"NAS root is missing local directories within depth {structure_depth}: {sample}")
    if extra_on_nas:
        sample = ", ".join(path.as_posix() for path in extra_on_nas[:5])
        raise ValueError(f"NAS root has extra directories within depth {structure_depth}: {sample}")


def directory_relative_paths(root: Path, max_depth: int) -> set[Path]:
    directories: set[Path] = set()
    stack = [(root, 0)]
    while stack:
        current, depth = stack.pop()
        if depth >= max_depth:
            continue
        for child in current.iterdir():
            if child.is_symlink() or not child.is_dir():
                continue
            relative = child.relative_to(root)
            directories.add(relative)
            stack.append((child, depth + 1))
    return directories


def load_manifests(manifest_paths: Iterable[Path], progress: Progress | None = None) -> list[ManifestEntry]:
    entries: list[ManifestEntry] = []
    for manifest_path in manifest_paths:
        primaries: dict[tuple[Path, str], ManifestInventoryRow] = {}
        sidecars: dict[tuple[Path, str], list[ManifestInventoryRow]] = defaultdict(list)
        with manifest_path.open(newline="", encoding="utf-8") as file:
            reader = csv.DictReader(file)
            missing = set(MANIFEST_FIELDS) - set(reader.fieldnames or [])
            if missing:
                raise ValueError(f"manifest missing required fields {sorted(missing)}: {manifest_path}")
            for row in reader:
                inventory_row = parse_manifest_inventory_row(row, manifest_path)
                if inventory_row.file_role == "primary":
                    if not inventory_row.group_id:
                        raise ValueError(f"primary manifest row missing group_id: {manifest_path}")
                    key = (manifest_path, inventory_row.group_id)
                    if key in primaries:
                        raise ValueError(f"multiple primary rows for group_id {inventory_row.group_id}: {manifest_path}")
                    primaries[key] = inventory_row
                elif inventory_row.file_role == "sidecar":
                    if not inventory_row.group_id:
                        raise ValueError(f"sidecar manifest row missing group_id: {manifest_path}")
                    sidecars[(manifest_path, inventory_row.group_id)].append(inventory_row)
                if progress is not None:
                    progress.manifest_entry_loaded()
        for key in sidecars:
            if key not in primaries:
                raise ValueError(f"sidecar manifest rows have no primary for group_id {key[1]}: {manifest_path}")
        for key, primary in primaries.items():
            grouped_sidecars = tuple(sorted(sidecars.get(key, []), key=lambda item: item.relative_path.as_posix()))
            for sidecar in grouped_sidecars:
                if (
                    sidecar.primary_nas_path != primary.nas_path
                    or sidecar.primary_relative_path != primary.relative_path
                ):
                    raise ValueError(f"sidecar manifest row points at a different primary for group_id {key[1]}: {manifest_path}")
            entries.append(
                ManifestEntry(
                    manifest_path=manifest_path,
                    batch_root=primary.batch_root,
                    nas_root=primary.nas_root,
                    nas_root_label=primary.nas_root_label,
                    nas_path=primary.nas_path,
                    relative_path=primary.relative_path,
                    size_bytes=primary.size_bytes,
                    xxh128=primary.xxh128,
                    sidecar_paths=tuple(sidecar.nas_path for sidecar in grouped_sidecars),
                    sidecar_relative_paths=tuple(sidecar.relative_path for sidecar in grouped_sidecars),
                    sidecar_sizes=tuple(sidecar.size_bytes for sidecar in grouped_sidecars),
                    sidecar_xxh128s=tuple(sidecar.xxh128 for sidecar in grouped_sidecars),
                )
            )
        if progress is not None:
            progress.manifest_manifest_loaded()
    return collapse_duplicate_manifest_paths(entries)


def parse_manifest_inventory_row(row: dict[str, str], manifest_path: Path) -> ManifestInventoryRow:
    if row["manifest_version"] != MANIFEST_VERSION:
        raise ValueError(f"unsupported manifest version {row['manifest_version']}: {manifest_path}")
    file_role = row["file_role"]
    if file_role not in {"primary", "sidecar", "uncategorized"}:
        raise ValueError(f"unsupported manifest file_role {file_role!r}: {manifest_path}")
    status = row["status"]
    if status not in {"included", "skipped"}:
        raise ValueError(f"unsupported manifest status {status!r}: {manifest_path}")
    try:
        size_bytes = int(row["size_bytes"])
    except ValueError as error:
        raise ValueError(f"invalid manifest size_bytes {row['size_bytes']!r}: {manifest_path}") from error
    primary_nas_path = Path(row["primary_nas_path"]) if row["primary_nas_path"] else None
    primary_relative_path = Path(row["primary_relative_path"]) if row["primary_relative_path"] else None
    if file_role == "sidecar" and (primary_nas_path is None or primary_relative_path is None):
        raise ValueError(f"sidecar manifest row missing primary path fields: {manifest_path}")
    if file_role != "sidecar" and (primary_nas_path is not None or primary_relative_path is not None):
        raise ValueError(f"non-sidecar manifest row has primary path fields: {manifest_path}")
    return ManifestInventoryRow(
        manifest_path=manifest_path,
        batch_root=row["batch_root"],
        nas_root=row["nas_root"],
        nas_root_label=row["nas_root_label"],
        group_id=row["group_id"],
        file_role=file_role,
        status=status,
        reason=row["reason"],
        nas_path=Path(row["nas_path"]),
        relative_path=Path(row["relative_path"]),
        primary_nas_path=primary_nas_path,
        primary_relative_path=primary_relative_path,
        size_bytes=size_bytes,
        xxh128=row["xxh128"],
    )


def collapse_duplicate_manifest_paths(entries: list[ManifestEntry]) -> list[ManifestEntry]:
    by_path: dict[Path, ManifestEntry] = {}
    for entry in entries:
        existing = by_path.get(entry.nas_path)
        if existing is None:
            by_path[entry.nas_path] = entry
            continue
        if manifest_entries_match(existing, entry):
            continue
        raise ValueError(f"conflicting duplicate nas_path in manifests: {entry.nas_path}")
    return list(by_path.values())


def manifest_entries_match(left: ManifestEntry, right: ManifestEntry) -> bool:
    return (
        left.nas_root == right.nas_root
        and left.nas_root_label == right.nas_root_label
        and left.relative_path == right.relative_path
        and left.size_bytes == right.size_bytes
        and left.xxh128 == right.xxh128
        and left.sidecar_paths == right.sidecar_paths
        and left.sidecar_relative_paths == right.sidecar_relative_paths
        and left.sidecar_sizes == right.sidecar_sizes
        and left.sidecar_xxh128s == right.sidecar_xxh128s
    )


def plan_from_manifests(
    manifest_paths: list[Path],
    output_root: Path,
    log_path: Path | None,
    show_progress: bool = False,
) -> ManifestPlanResult:
    progress = Progress(mode="manifest_plan", total_images=0, enabled=show_progress)
    try:
        progress.start_phase("manifest-load", len(manifest_paths))
        entries = load_manifests(manifest_paths, progress)
        actual_log_path = log_path or default_log_path()
        groups, uniques = manifest_duplicate_groups(entries)
        progress.manifest_group_stats(len(groups), len(uniques))
        progress.start_phase("manifest-plan", len(uniques) + len(groups))
        mode = "manifest_plan"
        duplicate_files = 0
        skipped_groups = 0

        actual_log_path.parent.mkdir(parents=True, exist_ok=True)
        with actual_log_path.open("w", newline="", encoding="utf-8") as file:
            logger = CsvLogger(file, mode=mode, hash_field="xxh128")
            for unique in sorted(uniques, key=manifest_sort_key):
                logger.row(
                    disposition="kept_unique_primary",
                    event="unique_primary",
                    status="unique",
                    file_role="primary",
                    input_root=unique.nas_root_label,
                    source_path=unique.nas_path,
                    size_bytes=unique.size_bytes,
                    digest=unique.xxh128,
                    message="no equal primary image found in manifests",
                )
                progress.advance()
            for index, group in enumerate(groups, start=1):
                group_id = f"m{index:06d}"
                keeper = choose_manifest_keeper(group)
                if keeper is None:
                    skipped_groups += 1
                    progress.manifest_skipped_group()
                    log_manifest_sidecar_conflict(logger, group_id, group)
                    progress.advance()
                    continue
                logger.row(
                    disposition="kept_duplicate_keeper",
                    event="keeper_primary",
                    status="kept",
                    group_id=group_id,
                    file_role="primary",
                    input_root=keeper.nas_root_label,
                    source_path=keeper.nas_path,
                    keeper_path=keeper.nas_path,
                    size_bytes=keeper.size_bytes,
                    digest=keeper.xxh128,
                    reason="selected_by_manifest_priority",
                )
                for duplicate in sorted(group, key=manifest_sort_key):
                    if duplicate == keeper:
                        continue
                    duplicate_files += 1
                    progress.manifest_planned_move()
                    log_manifest_duplicate_plan(logger, group_id, duplicate, keeper, output_root)
                progress.advance()

        return ManifestPlanResult(
            log_path=actual_log_path,
            duplicate_groups=len(groups),
            duplicate_files=duplicate_files,
            skipped_groups=skipped_groups,
        )
    finally:
        progress.finish()


def manifest_duplicate_groups(entries: list[ManifestEntry]) -> tuple[list[list[ManifestEntry]], list[ManifestEntry]]:
    buckets: dict[tuple[int, str], list[ManifestEntry]] = defaultdict(list)
    for entry in entries:
        buckets[(entry.size_bytes, entry.xxh128)].append(entry)

    groups: list[list[ManifestEntry]] = []
    uniques: list[ManifestEntry] = []
    for bucket in buckets.values():
        if len(bucket) == 1:
            uniques.extend(bucket)
        else:
            groups.append(bucket)
    return groups, uniques


def choose_manifest_keeper(group: list[ManifestEntry]) -> ManifestEntry | None:
    with_sidecars = [entry for entry in group if entry.sidecar_paths]
    if len(with_sidecars) > 1 and not manifest_sidecar_sets_equivalent(with_sidecars):
        return None
    return sorted(group, key=manifest_keeper_key)[0]


def manifest_sidecar_sets_equivalent(entries: list[ManifestEntry]) -> bool:
    first = manifest_sidecar_signature(entries[0])
    return all(manifest_sidecar_signature(entry) == first for entry in entries[1:])


def manifest_sidecar_signature(entry: ManifestEntry) -> tuple[str, ...]:
    return tuple(sorted(entry.sidecar_xxh128s))


def manifest_keeper_key(entry: ManifestEntry) -> tuple[int, int, int, int, str, str]:
    return (
        0 if entry.sidecar_paths else 1,
        date_directory_score(entry.nas_path),
        0 if has_takeout_segment(entry.nas_path) else 1,
        1 if has_mobilebackup_segment(entry.nas_path) else 0,
        entry.nas_root_label.lower(),
        entry.relative_path.as_posix().lower(),
    )


def manifest_sort_key(entry: ManifestEntry) -> tuple[str, str]:
    return entry.nas_root_label.lower(), entry.relative_path.as_posix().lower()


def log_manifest_sidecar_conflict(logger: CsvLogger, group_id: str, group: list[ManifestEntry]) -> None:
    for entry in group:
        logger.row(
            disposition="kept_sidecar_conflict",
            event="duplicate_primary_kept_due_to_sidecar_conflict",
            status="skipped",
            group_id=group_id,
            file_role="primary",
            input_root=entry.nas_root_label,
            source_path=entry.nas_path,
            size_bytes=entry.size_bytes,
            digest=entry.xxh128,
            reason="unresolved_sidecar_conflict",
        )
        for sidecar_path, sidecar_size, sidecar_hash in zip(
            entry.sidecar_paths,
            entry.sidecar_sizes,
            entry.sidecar_xxh128s,
            strict=True,
        ):
            logger.row(
                disposition="kept_sidecar_conflict_sidecar",
                event="sidecar_kept_due_to_sidecar_conflict",
                status="skipped",
                group_id=group_id,
                file_role="sidecar",
                input_root=entry.nas_root_label,
                source_path=sidecar_path,
                size_bytes=sidecar_size,
                digest=sidecar_hash,
                reason="unresolved_sidecar_conflict",
            )


def log_manifest_duplicate_plan(
    logger: CsvLogger,
    group_id: str,
    duplicate: ManifestEntry,
    keeper: ManifestEntry,
    output_root: Path,
) -> None:
    logger.row(
        disposition="planned_duplicate_primary",
        event="duplicate_primary_move",
        status="planned",
        group_id=group_id,
        file_role="primary",
        input_root=duplicate.nas_root_label,
        source_path=duplicate.nas_path,
        destination_path=manifest_destination_for(output_root, duplicate.nas_root_label, duplicate.relative_path),
        keeper_path=keeper.nas_path,
        size_bytes=duplicate.size_bytes,
        digest=duplicate.xxh128,
        reason="duplicate_of_keeper",
    )
    for path, relative_path, size_bytes, digest in zip(
        duplicate.sidecar_paths,
        duplicate.sidecar_relative_paths,
        duplicate.sidecar_sizes,
        duplicate.sidecar_xxh128s,
        strict=True,
    ):
        logger.row(
            disposition="planned_sidecar",
            event="sidecar_move",
            status="planned",
            group_id=group_id,
            file_role="sidecar",
            input_root=duplicate.nas_root_label,
            primary_source_path=duplicate.nas_path,
            source_path=path,
            destination_path=manifest_destination_for(output_root, duplicate.nas_root_label, relative_path),
            keeper_path=keeper.nas_path,
            size_bytes=size_bytes,
            digest=digest,
            reason="duplicate_of_keeper",
        )


def manifest_destination_for(output_root: Path, nas_root_label: str, relative_path: Path) -> Path:
    return output_root / nas_root_label / relative_path


def verify_manifests(
    manifest_paths: list[Path],
    log_path: Path | None,
    byte_check: bool,
    show_progress: bool = False,
) -> ManifestVerifyResult:
    if not byte_check:
        raise ValueError("verify_manifests requires byte_check=True")
    progress = Progress(mode="manifest_verify", total_images=0, enabled=show_progress)
    try:
        progress.start_phase("manifest-load", len(manifest_paths))
        entries = load_manifests(manifest_paths, progress)
        groups, uniques = manifest_duplicate_groups(entries)
        progress.manifest_group_stats(len(groups), len(uniques))
        progress.start_phase("manifest-verify-bytes", len(groups))
        actual_log_path = log_path or default_log_path()
        failed_groups = 0

        actual_log_path.parent.mkdir(parents=True, exist_ok=True)
        with actual_log_path.open("w", newline="", encoding="utf-8") as file:
            logger = CsvLogger(file, mode="manifest_verify", hash_field="xxh128")
            for index, group in enumerate(groups, start=1):
                group_id = f"m{index:06d}"
                failures = byte_check_manifest_group(group, progress)
                failed = bool(failures)
                if failed:
                    failed_groups += 1
                    for entry in failures:
                        logger.row(
                            disposition="verify_failed",
                            event="verify_manifest_primary",
                            status="error",
                            group_id=group_id,
                            file_role="primary",
                            input_root=entry.nas_root_label,
                            source_path=entry.nas_path,
                            size_bytes=entry.size_bytes,
                            digest=entry.xxh128,
                            reason="same_manifest_hash_but_bytes_differ",
                        )
                else:
                    logger.row(
                        disposition="verify_matched",
                        event="verify_manifest_group",
                        status="kept",
                        group_id=group_id,
                        size_bytes=group[0].size_bytes,
                        digest=group[0].xxh128,
                        reason="all_manifest_hash_matches_are_byte_equal",
                    )
                progress.manifest_group_checked(failed)

        return ManifestVerifyResult(
            log_path=actual_log_path,
            checked_groups=len(groups),
            failed_groups=failed_groups,
        )
    finally:
        progress.finish()


def byte_check_manifest_group(group: list[ManifestEntry], progress: Progress | None = None) -> list[ManifestEntry]:
    reference = group[0]
    failures: list[ManifestEntry] = []
    for entry in group[1:]:
        if not files_equal_by_path(reference.nas_path, entry.nas_path, progress):
            failures.append(entry)
    return failures


def files_equal_by_path(left: Path, right: Path, progress: Progress | None = None) -> bool:
    if left.stat().st_size != right.stat().st_size:
        return False
    with left.open("rb", buffering=0) as left_file, right.open("rb", buffering=0) as right_file:
        while True:
            left_chunk = left_file.read(HASH_CHUNK_SIZE)
            right_chunk = right_file.read(HASH_CHUNK_SIZE)
            if progress is not None:
                progress.manifest_compare_bytes(len(left_chunk) + len(right_chunk))
            if left_chunk != right_chunk:
                return False
            if not left_chunk:
                return True


def verify_move(
    manifest_paths: list[Path],
    output_root: Path,
    log_path: Path | None,
    show_progress: bool = False,
) -> VerifyMoveResult:
    progress = Progress(mode="manifest_verify_move", total_images=0, enabled=show_progress)
    try:
        progress.start_phase("manifest-load", len(manifest_paths))
        entries = load_manifests(manifest_paths, progress)
        actual_log_path = log_path or default_log_path()
        expected_destinations: set[Path] = set()
        checked_paths = 0
        matched_paths = 0
        failed_paths = 0
        unexpected_outputs = 0

        if output_root.exists() and not output_root.is_dir():
            raise ValueError(f"output root is not a directory: {output_root}")

        groups, uniques = verify_move_groups(entries)
        progress.manifest_group_stats(len(groups), len(uniques))
        progress.start_phase("manifest-verify-move", verify_move_expected_check_count(groups, uniques))
        actual_log_path.parent.mkdir(parents=True, exist_ok=True)
        with actual_log_path.open("w", newline="", encoding="utf-8") as file:
            logger = CsvLogger(file, mode="verify_move", hash_field="xxh128")
            for unique in sorted(uniques, key=verify_move_sort_key):
                for path, size_bytes, digest, file_role in verify_move_entry_source_checks(unique):
                    matched = verify_move_log_source_intact(
                        logger,
                        entry=unique,
                        path=path,
                        size_bytes=size_bytes,
                        digest=digest,
                        file_role=file_role,
                        disposition="verify_move_unique_intact",
                        event="verify_move_unique",
                        reason="unique_source_intact",
                    )
                    checked_paths += 1
                    if matched:
                        matched_paths += 1
                    else:
                        failed_paths += 1
                    progress.manifest_path_checked(matched)

            for index, group in enumerate(groups, start=1):
                group_id = f"m{index:06d}"
                if verify_move_sidecar_conflict(group):
                    for entry in sorted(group, key=verify_move_sort_key):
                        for path, size_bytes, digest, file_role in verify_move_entry_source_checks(entry):
                            matched = verify_move_log_source_intact(
                                logger,
                                entry=entry,
                                path=path,
                                size_bytes=size_bytes,
                                digest=digest,
                                file_role=file_role,
                                disposition="verify_move_skipped_conflict_intact",
                                event="verify_move_sidecar_conflict",
                                reason="sidecar_conflict_source_intact",
                                group_id=group_id,
                            )
                            checked_paths += 1
                            if matched:
                                matched_paths += 1
                            else:
                                failed_paths += 1
                            progress.manifest_path_checked(matched)
                    continue

                keeper = verify_move_choose_keeper(group)
                for path, size_bytes, digest, file_role in verify_move_entry_source_checks(keeper):
                    matched = verify_move_log_source_intact(
                        logger,
                        entry=keeper,
                        path=path,
                        size_bytes=size_bytes,
                        digest=digest,
                        file_role=file_role,
                        disposition="verify_move_matched",
                        event="verify_move_keeper",
                        reason="keeper_source_intact",
                        group_id=group_id,
                        keeper_path=keeper.nas_path,
                    )
                    checked_paths += 1
                    if matched:
                        matched_paths += 1
                    else:
                        failed_paths += 1
                    progress.manifest_path_checked(matched)

                for duplicate in sorted(group, key=verify_move_sort_key):
                    if duplicate == keeper:
                        continue
                    source_checks = verify_move_entry_source_checks(duplicate)
                    for path, size_bytes, digest, file_role in source_checks:
                        matched = verify_move_log_source_missing(
                            logger,
                            entry=duplicate,
                            path=path,
                            size_bytes=size_bytes,
                            digest=digest,
                            file_role=file_role,
                            group_id=group_id,
                            keeper_path=keeper.nas_path,
                        )
                        checked_paths += 1
                        if matched:
                            matched_paths += 1
                        else:
                            failed_paths += 1
                        progress.manifest_path_checked(matched)

                    for path, size_bytes, digest, file_role in verify_move_entry_destination_checks(duplicate, output_root):
                        expected_destinations.add(path)
                        matched = verify_move_log_destination_present(
                            logger,
                            entry=duplicate,
                            path=path,
                            size_bytes=size_bytes,
                            digest=digest,
                            file_role=file_role,
                            group_id=group_id,
                            keeper_path=keeper.nas_path,
                        )
                        checked_paths += 1
                        if matched:
                            matched_paths += 1
                        else:
                            failed_paths += 1
                        progress.manifest_path_checked(matched)

            output_entries = sorted(output_root.rglob("*")) if output_root.is_dir() else []
            progress.start_phase("manifest-output-scan", len(output_entries))
            for path in verify_move_unexpected_outputs(output_entries, expected_destinations, actual_log_path, progress):
                unexpected_outputs += 1
                progress.manifest_unexpected_output()
                logger.row(
                    disposition="verify_move_unexpected_output",
                    event="verify_move_output_scan",
                    status="error",
                    source_path=path,
                    size_bytes=path.stat().st_size,
                    reason="unexpected_output_file",
                    message="output file is not an expected moved duplicate or sidecar",
                )

        return VerifyMoveResult(
            log_path=actual_log_path,
            checked_paths=checked_paths,
            matched_paths=matched_paths,
            failed_paths=failed_paths,
            unexpected_outputs=unexpected_outputs,
        )
    finally:
        progress.finish()


def verify_move_groups(entries: list[ManifestEntry]) -> tuple[list[list[ManifestEntry]], list[ManifestEntry]]:
    buckets: dict[tuple[int, str], list[ManifestEntry]] = defaultdict(list)
    for entry in entries:
        buckets[(entry.size_bytes, entry.xxh128)].append(entry)

    groups: list[list[ManifestEntry]] = []
    uniques: list[ManifestEntry] = []
    for bucket in buckets.values():
        if len(bucket) == 1:
            uniques.extend(bucket)
        else:
            groups.append(bucket)
    return groups, uniques


def verify_move_expected_check_count(groups: list[list[ManifestEntry]], uniques: list[ManifestEntry]) -> int:
    count = sum(len(verify_move_entry_source_checks(entry)) for entry in uniques)
    for group in groups:
        if verify_move_sidecar_conflict(group):
            count += sum(len(verify_move_entry_source_checks(entry)) for entry in group)
            continue
        keeper = verify_move_choose_keeper(group)
        count += len(verify_move_entry_source_checks(keeper))
        for duplicate in group:
            if duplicate == keeper:
                continue
            count += len(verify_move_entry_source_checks(duplicate))
            count += len(verify_move_entry_destination_checks(duplicate, Path()))
    return count


def verify_move_sidecar_conflict(group: list[ManifestEntry]) -> bool:
    with_sidecars = [entry for entry in group if entry.sidecar_paths]
    if len(with_sidecars) <= 1:
        return False
    first = verify_move_sidecar_signature(with_sidecars[0])
    return any(verify_move_sidecar_signature(entry) != first for entry in with_sidecars[1:])


def verify_move_sidecar_signature(entry: ManifestEntry) -> tuple[str, ...]:
    return tuple(sorted(entry.sidecar_xxh128s))


def verify_move_choose_keeper(group: list[ManifestEntry]) -> ManifestEntry:
    return sorted(group, key=verify_move_keeper_key)[0]


def verify_move_keeper_key(entry: ManifestEntry) -> tuple[int, int, int, int, str, str]:
    return (
        0 if entry.sidecar_paths else 1,
        verify_move_date_directory_score(entry.nas_path),
        0 if verify_move_has_takeout_segment(entry.nas_path) else 1,
        1 if verify_move_has_mobilebackup_segment(entry.nas_path) else 0,
        entry.nas_root_label.lower(),
        entry.relative_path.as_posix().lower(),
    )


def verify_move_date_directory_score(path: Path) -> int:
    return sum(1 for part in path.parent.parts if VERIFY_MOVE_DATE_DIRECTORY_TOKEN_RE.search(part))


def verify_move_has_takeout_segment(path: Path) -> bool:
    return any("takeout" in part.lower() for part in path.parts)


def verify_move_has_mobilebackup_segment(path: Path) -> bool:
    return any("mobilebackup" in part.lower() for part in path.parts)


def verify_move_sort_key(entry: ManifestEntry) -> tuple[str, str]:
    return entry.nas_root_label.lower(), entry.relative_path.as_posix().lower()


def verify_move_entry_source_checks(entry: ManifestEntry) -> tuple[tuple[Path, int, str, str], ...]:
    checks = [(entry.nas_path, entry.size_bytes, entry.xxh128, "primary")]
    for path, size_bytes, digest in zip(entry.sidecar_paths, entry.sidecar_sizes, entry.sidecar_xxh128s, strict=True):
        checks.append((path, size_bytes, digest, "sidecar"))
    return tuple(checks)


def verify_move_entry_destination_checks(
    entry: ManifestEntry,
    output_root: Path,
) -> tuple[tuple[Path, int, str, str], ...]:
    checks = [
        (
            verify_move_destination_for(output_root, entry.nas_root_label, entry.relative_path),
            entry.size_bytes,
            entry.xxh128,
            "primary",
        )
    ]
    for relative_path, size_bytes, digest in zip(
        entry.sidecar_relative_paths,
        entry.sidecar_sizes,
        entry.sidecar_xxh128s,
        strict=True,
    ):
        checks.append(
            (
                verify_move_destination_for(output_root, entry.nas_root_label, relative_path),
                size_bytes,
                digest,
                "sidecar",
            )
        )
    return tuple(checks)


def verify_move_destination_for(output_root: Path, nas_root_label: str, relative_path: Path) -> Path:
    return output_root / nas_root_label / relative_path


def verify_move_log_source_intact(
    logger: CsvLogger,
    *,
    entry: ManifestEntry,
    path: Path,
    size_bytes: int,
    digest: str,
    file_role: str,
    disposition: str,
    event: str,
    reason: str,
    group_id: str = "",
    keeper_path: Path | str = "",
) -> bool:
    status, failure_reason = verify_move_existing_file_status(path, size_bytes)
    matched = status == "matched"
    logger.row(
        disposition=disposition if matched else "verify_move_failed",
        event=event,
        status="kept" if matched else "error",
        group_id=group_id,
        file_role=file_role,
        input_root=entry.nas_root_label,
        source_path=path,
        keeper_path=keeper_path,
        size_bytes=size_bytes,
        digest=digest,
        reason=reason if matched else failure_reason,
    )
    return matched


def verify_move_log_source_missing(
    logger: CsvLogger,
    *,
    entry: ManifestEntry,
    path: Path,
    size_bytes: int,
    digest: str,
    file_role: str,
    group_id: str,
    keeper_path: Path,
) -> bool:
    matched = not path.exists()
    logger.row(
        disposition="verify_move_matched" if matched else "verify_move_failed",
        event="verify_move_duplicate_source",
        status="moved" if matched else "error",
        group_id=group_id,
        file_role=file_role,
        input_root=entry.nas_root_label,
        source_path=path,
        keeper_path=keeper_path,
        size_bytes=size_bytes,
        digest=digest,
        reason="duplicate_source_missing" if matched else "duplicate_source_still_exists",
    )
    return matched


def verify_move_log_destination_present(
    logger: CsvLogger,
    *,
    entry: ManifestEntry,
    path: Path,
    size_bytes: int,
    digest: str,
    file_role: str,
    group_id: str,
    keeper_path: Path,
) -> bool:
    status, failure_reason = verify_move_existing_file_status(path, size_bytes)
    matched = status == "matched"
    logger.row(
        disposition="verify_move_matched" if matched else "verify_move_failed",
        event="verify_move_duplicate_destination",
        status="moved" if matched else "error",
        group_id=group_id,
        file_role=file_role,
        input_root=entry.nas_root_label,
        destination_path=path,
        keeper_path=keeper_path,
        size_bytes=size_bytes,
        digest=digest,
        reason="duplicate_destination_present" if matched else failure_reason,
    )
    return matched


def verify_move_existing_file_status(path: Path, size_bytes: int) -> tuple[str, str]:
    if not path.exists():
        return "missing", "expected_file_missing"
    if not path.is_file():
        return "not_file", "expected_path_not_file"
    if path.stat().st_size != size_bytes:
        return "size_mismatch", "expected_file_size_mismatch"
    return "matched", ""


def verify_move_unexpected_outputs(
    output_entries: list[Path],
    expected_destinations: set[Path],
    log_path: Path,
    progress: Progress | None = None,
) -> list[Path]:
    resolved_log_path = log_path.resolve(strict=False)
    unexpected = []
    for path in output_entries:
        if progress is not None:
            progress.advance()
        if not path.is_file():
            continue
        if path.resolve(strict=False) == resolved_log_path:
            continue
        if path not in expected_destinations:
            unexpected.append(path)
    return unexpected


def execute_plan(
    plan_path: Path,
    log_path: Path | None,
    move: bool,
    show_progress: bool = False,
    verify_source_hashes: bool = False,
) -> ExecutePlanResult:
    progress = Progress(mode="execute_plan", total_images=0, enabled=show_progress)
    try:
        progress.start_phase("manifest-load-plan", count_csv_data_rows(plan_path))
        bundles, orphan_sidecars, hash_field = load_plan_bundles(plan_path, progress)
        actual_log_path = log_path or default_log_path()
        duplicate_destinations = duplicate_destination_paths(bundles)
        mode = "execute_plan_move" if move else "execute_plan_dry_run"
        moved_bundles = 0
        planned_bundles = 0
        already_moved_bundles = 0
        skipped_bundles = 0

        progress.start_phase("manifest-execute", len(orphan_sidecars) + len(bundles))
        actual_log_path.parent.mkdir(parents=True, exist_ok=True)
        with actual_log_path.open("w", newline="", encoding="utf-8") as file:
            logger = CsvLogger(file, mode=mode, hash_field=hash_field)
            for sidecar in orphan_sidecars:
                logger.row(
                    disposition="orphan_plan_sidecar",
                    event="execute_plan_orphan_sidecar",
                    status="skipped",
                    group_id=sidecar.group_id,
                    file_role="sidecar",
                    input_root=sidecar.row.get("input_root", ""),
                    primary_source_path=sidecar.primary_source_path or "",
                    source_path=sidecar.source_path,
                    destination_path=sidecar.destination_path,
                    keeper_path=sidecar.row.get("keeper_path", ""),
                    size_bytes=sidecar.size_bytes,
                    reason="orphan_plan_sidecar",
                    message="planned sidecar row has no matching planned primary row",
                )
                progress.error()
                progress.manifest_bundle_processed()

            for bundle in bundles:
                validation = validate_plan_bundle(bundle, duplicate_destinations, verify_source_hashes)
                if validation == "already_moved":
                    already_moved_bundles += 1
                    log_bundle(logger, bundle, disposition_prefix="already_moved", status="kept", reason="already_moved")
                    progress.manifest_bundle_processed()
                    continue
                if validation is not None:
                    skipped_bundles += 1
                    log_bundle(logger, bundle, disposition_prefix="kept_error", status="error", reason=validation)
                    progress.error()
                    progress.manifest_bundle_processed()
                    continue
                if not move:
                    planned_bundles += 1
                    progress.manifest_planned_move(len(bundle_rows(bundle)))
                    log_bundle(logger, bundle, disposition_prefix="planned", status="planned", reason="validated")
                    progress.manifest_bundle_processed()
                    continue

                moved_sources: list[tuple[Path, Path]] = []
                try:
                    for row in bundle_rows(bundle):
                        row.destination_path.parent.mkdir(parents=True, exist_ok=True)
                        shutil.move(str(row.source_path), str(row.destination_path))
                        progress.moved(row.size_bytes)
                        moved_sources.append((row.source_path, row.destination_path))
                except OSError as error:
                    rollback_errors = rollback_moves(moved_sources)
                    skipped_bundles += 1
                    message = f"move failed: {error}"
                    if rollback_errors:
                        message += f"; rollback errors: {'; '.join(rollback_errors)}"
                    log_bundle(
                        logger,
                        bundle,
                        disposition_prefix="kept_error",
                        status="error",
                        reason="move_failed",
                        message=message,
                    )
                    progress.error()
                    progress.manifest_bundle_processed()
                    continue

                moved_bundles += 1
                log_bundle(logger, bundle, disposition_prefix="moved", status="moved", reason="executed")
                progress.manifest_bundle_processed()

        return ExecutePlanResult(
            log_path=actual_log_path,
            bundles=len(bundles),
            moved_bundles=moved_bundles,
            planned_bundles=planned_bundles,
            already_moved_bundles=already_moved_bundles,
            skipped_bundles=skipped_bundles,
            orphan_sidecars=len(orphan_sidecars),
        )
    finally:
        progress.finish()


def load_plan_bundles(plan_path: Path, progress: Progress | None = None) -> tuple[list[PlanBundle], list[PlanRow], str]:
    with plan_path.open(newline="", encoding="utf-8") as file:
        reader = csv.DictReader(file)
        fieldnames = reader.fieldnames or []
        required = {
            "disposition",
            "group_id",
            "file_role",
            "source_path",
            "destination_path",
            "size_bytes",
            "primary_source_path",
        }
        missing = required - set(fieldnames)
        if missing:
            raise ValueError(f"plan missing required fields {sorted(missing)}: {plan_path}")
        hash_field = "xxh128" if "xxh128" in fieldnames else "xxh64"
        primaries: list[PlanRow] = []
        sidecars: list[PlanRow] = []
        keepers: dict[str, PlanRow] = {}
        for row in reader:
            if progress is not None:
                progress.manifest_plan_row_loaded()
                progress.advance()
            disposition = row["disposition"]
            if disposition not in {"planned_duplicate_primary", "planned_sidecar", "kept_duplicate_keeper"}:
                continue
            plan_row = parse_plan_row(row, hash_field, require_destination=disposition != "kept_duplicate_keeper")
            if plan_row.file_role == "primary" and disposition == "planned_duplicate_primary":
                primaries.append(plan_row)
            elif plan_row.file_role == "sidecar" and disposition == "planned_sidecar":
                sidecars.append(plan_row)
            elif plan_row.file_role == "primary" and disposition == "kept_duplicate_keeper":
                if plan_row.group_id in keepers:
                    raise ValueError(f"multiple keeper rows for group_id {plan_row.group_id}: {plan_path}")
                keepers[plan_row.group_id] = plan_row

    sidecars_by_key: dict[tuple[str, Path], list[PlanRow]] = defaultdict(list)
    for sidecar in sidecars:
        sidecars_by_key[sidecar_bundle_key(sidecar)].append(sidecar)

    bundles = []
    matched_sidecars: set[int] = set()
    for primary in primaries:
        key = primary_bundle_key(primary)
        bundle_sidecars = tuple(sorted(sidecars_by_key.get(key, []), key=lambda row: str(row.source_path)))
        matched_sidecars.update(id(sidecar) for sidecar in bundle_sidecars)
        bundles.append(PlanBundle(keeper=keepers.get(primary.group_id), primary=primary, sidecars=bundle_sidecars))
    orphans = [sidecar for sidecar in sidecars if id(sidecar) not in matched_sidecars]
    return bundles, orphans, hash_field


def count_csv_data_rows(path: Path) -> int:
    with path.open(newline="", encoding="utf-8") as file:
        reader = csv.reader(file)
        next(reader, None)
        return sum(1 for _ in reader)


def parse_plan_row(row: dict[str, str], hash_field: str, require_destination: bool) -> PlanRow:
    try:
        size_bytes = int(row["size_bytes"])
    except ValueError as error:
        raise ValueError(f"invalid size_bytes in plan row: {row}") from error
    if not row["source_path"] or (require_destination and not row["destination_path"]):
        raise ValueError(f"planned move row must include source_path and destination_path: {row}")
    primary_source_path = Path(row["primary_source_path"]) if row["primary_source_path"] else None
    if row["file_role"] == "sidecar" and primary_source_path is None:
        raise ValueError(f"planned sidecar row must include primary_source_path: {row}")
    if row["file_role"] != "sidecar" and primary_source_path is not None:
        raise ValueError(f"non-sidecar plan row must not include primary_source_path: {row}")
    return PlanRow(
        row=row,
        source_path=Path(row["source_path"]),
        destination_path=Path(row["destination_path"]) if row["destination_path"] else Path(),
        size_bytes=size_bytes,
        group_id=row["group_id"],
        file_role=row["file_role"],
        disposition=row["disposition"],
        hash_value=row.get(hash_field, ""),
        primary_source_path=primary_source_path,
    )


def primary_bundle_key(row: PlanRow) -> tuple[str, Path]:
    return row.group_id, row.source_path


def sidecar_bundle_key(row: PlanRow) -> tuple[str, Path]:
    if row.primary_source_path is None:
        raise ValueError(f"planned sidecar row missing primary_source_path: {row.row}")
    return row.group_id, row.primary_source_path


def duplicate_destination_paths(bundles: list[PlanBundle]) -> set[Path]:
    counts: dict[Path, int] = defaultdict(int)
    for bundle in bundles:
        for row in bundle_rows(bundle):
            counts[row.destination_path] += 1
    return {path for path, count in counts.items() if count > 1}


def validate_plan_bundle(
    bundle: PlanBundle,
    duplicate_destinations: set[Path],
    verify_source_hashes: bool = False,
) -> str | None:
    rows = bundle_rows(bundle)
    keeper_validation = validate_keeper(bundle)
    if keeper_validation is not None:
        return keeper_validation
    if any(row.source_path == bundle.keeper.source_path for row in rows if bundle.keeper is not None):
        return "duplicate_source_is_keeper"
    if any(row.destination_path in duplicate_destinations for row in rows):
        return "duplicate_destination_in_plan"

    states = [plan_row_state(row) for row in rows]
    if all(state == "already_moved" for state in states):
        return "already_moved"
    if any(state == "destination_exists" for state in states):
        return "destination_exists"
    if any(state == "source_size_mismatch" for state in states):
        return "source_size_mismatch"
    if any(state == "source_missing" for state in states):
        return "partial_bundle_state" if any(state == "ready" for state in states) or any(state == "already_moved" for state in states) else "source_missing"
    if any(state == "already_moved" for state in states):
        return "partial_bundle_state"
    if verify_source_hashes and not bundle_source_hashes_match(bundle):
        return "source_hash_mismatch"
    return None


def validate_keeper(bundle: PlanBundle) -> str | None:
    if bundle.keeper is None:
        return "keeper_missing_from_plan"
    if not bundle.keeper.source_path.exists():
        return "keeper_missing"
    if bundle.keeper.source_path.stat().st_size != bundle.keeper.size_bytes:
        return "keeper_size_mismatch"
    return None


def bundle_source_hashes_match(bundle: PlanBundle) -> bool:
    rows_to_check = (bundle.keeper, *bundle_rows(bundle))
    return all(row_hash_matches(row) for row in rows_to_check if row is not None)


def row_hash_matches(row: PlanRow) -> bool:
    if not row.hash_value:
        return False
    return hash_file_xxh128(row.source_path) == row.hash_value


def plan_row_state(row: PlanRow) -> str:
    source_exists = row.source_path.exists()
    destination_exists = row.destination_path.exists()
    if source_exists and destination_exists:
        return "destination_exists"
    if source_exists:
        return "ready" if row.source_path.stat().st_size == row.size_bytes else "source_size_mismatch"
    if destination_exists:
        return "already_moved" if row.destination_path.stat().st_size == row.size_bytes else "destination_exists"
    return "source_missing"


def bundle_rows(bundle: PlanBundle) -> tuple[PlanRow, ...]:
    return (bundle.primary, *bundle.sidecars)


def log_bundle(
    logger: CsvLogger,
    bundle: PlanBundle,
    *,
    disposition_prefix: str,
    status: str,
    reason: str,
    message: str = "",
) -> None:
    for row in bundle_rows(bundle):
        if disposition_prefix == "planned":
            disposition = "planned_duplicate_primary" if row.file_role == "primary" else "planned_sidecar"
        elif disposition_prefix == "moved":
            disposition = "moved_duplicate_primary" if row.file_role == "primary" else "moved_sidecar"
        elif disposition_prefix == "already_moved":
            disposition = "already_moved_duplicate_primary" if row.file_role == "primary" else "already_moved_sidecar"
        else:
            disposition = disposition_prefix
        logger.row(
            disposition=disposition,
            event="execute_plan_move" if row.file_role == "primary" else "execute_plan_sidecar_move",
            status=status,
            group_id=row.group_id,
            file_role=row.file_role,
            input_root=row.row.get("input_root", ""),
            primary_source_path=row.primary_source_path or "",
            source_path=row.source_path,
            destination_path=row.destination_path,
            keeper_path=row.row.get("keeper_path", ""),
            size_bytes=row.size_bytes,
            digest=row.hash_value,
            reason=reason,
            message=message,
        )


def rollback_moves(moved_sources: list[tuple[Path, Path]]) -> list[str]:
    errors: list[str] = []
    for source, destination in reversed(moved_sources):
        if not destination.exists():
            continue
        if source.exists():
            errors.append(f"cannot roll back {destination}: source exists")
            continue
        try:
            source.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(destination), str(source))
        except OSError as error:
            errors.append(f"cannot roll back {destination}: {error}")
    return errors
