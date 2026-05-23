from __future__ import annotations

import csv
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Iterable

from dedup_photos.constants import MANIFEST_VERSION
from dedup_photos.hashing import hash_file_xxh128
from dedup_photos.inventory import classify_manifest_files, regular_files_by_directory
from dedup_photos.common import is_primary_image
from dedup_photos.models import ManifestEntry, ManifestInventoryRow
from dedup_photos.progress import Progress


MANIFEST_FIELDS = [
    "relative_path",
    "file_role",
    "status",
    "reason",
    "group_id",
    "primary_relative_path",
    "size_bytes",
    "xxh128",
    "nas_path",
    "primary_nas_path",
    "manifest_version",
    "created_at",
    "nas_root_label",
    "batch_root",
    "nas_root",
]


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
        primary_by_sidecar = {
            sidecar: primary
            for primary, sidecars in sidecars_by_primary.items()
            for sidecar in sidecars
        }
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
                for path in sorted(files_by_directory[directory], key=lambda item: item.name.lower()):
                    if is_primary_image(path):
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
                    elif path in primary_by_sidecar:
                        continue
                    elif path in uncategorized:
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


def load_uncategorized_manifest_rows(manifest_paths: Iterable[Path]) -> list[ManifestInventoryRow]:
    rows: list[ManifestInventoryRow] = []
    for manifest_path in manifest_paths:
        with manifest_path.open(newline="", encoding="utf-8") as file:
            reader = csv.DictReader(file)
            missing = set(MANIFEST_FIELDS) - set(reader.fieldnames or [])
            if missing:
                raise ValueError(f"manifest missing required fields {sorted(missing)}: {manifest_path}")
            for row in reader:
                inventory_row = parse_manifest_inventory_row(row, manifest_path)
                if inventory_row.file_role == "uncategorized":
                    rows.append(inventory_row)
    return collapse_duplicate_uncategorized_paths(rows)


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


def collapse_duplicate_uncategorized_paths(rows: list[ManifestInventoryRow]) -> list[ManifestInventoryRow]:
    by_path: dict[Path, ManifestInventoryRow] = {}
    for row in rows:
        existing = by_path.get(row.nas_path)
        if existing is None:
            by_path[row.nas_path] = row
            continue
        if manifest_inventory_rows_match(existing, row):
            continue
        raise ValueError(f"conflicting duplicate nas_path in manifests: {row.nas_path}")
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


def manifest_inventory_rows_match(left: ManifestInventoryRow, right: ManifestInventoryRow) -> bool:
    return (
        left.batch_root == right.batch_root
        and left.nas_root == right.nas_root
        and left.nas_root_label == right.nas_root_label
        and left.file_role == right.file_role
        and left.status == right.status
        and left.reason == right.reason
        and left.relative_path == right.relative_path
        and left.size_bytes == right.size_bytes
        and left.xxh128 == right.xxh128
        and left.primary_nas_path == right.primary_nas_path
        and left.primary_relative_path == right.primary_relative_path
    )
