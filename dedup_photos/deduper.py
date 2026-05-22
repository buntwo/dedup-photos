from __future__ import annotations

import csv
import re
import shutil
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterable, TextIO

import xxhash

from dedup_photos.constants import HASH_CHUNK_SIZE, PRIMARY_IMAGE_EXTENSIONS
from dedup_photos.progress import Progress


DATE_DIRECTORY_TOKEN_RE = re.compile(r"(?<!\d)(?:\d{4}-\d{2}-\d{2}|\d{8}|\d{4})(?!\d)")


@dataclass(frozen=True)
class InputRoot:
    path: Path
    label: str


@dataclass(frozen=True)
class PrimaryFile:
    path: Path
    input_root: InputRoot
    relative_path: Path
    size_bytes: int
    sidecars: tuple[Path, ...]


@dataclass(frozen=True)
class DuplicateGroup:
    group_id: str
    files: tuple[PrimaryFile, ...]
    digest: str
    size_bytes: int


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


def normalize_input_roots(paths: Iterable[Path]) -> list[InputRoot]:
    roots = [InputRoot(path=path.resolve(), label=path.resolve().name) for path in paths]
    labels = [root.label.lower() for root in roots]
    duplicates = sorted({label for label in labels if labels.count(label) > 1})
    if duplicates:
        names = ", ".join(duplicates)
        raise ValueError(f"input root basenames must be unique; duplicates: {names}")
    for root in roots:
        if not root.path.is_dir():
            raise ValueError(f"input root is not a directory: {root.path}")
    return roots


def validate_output_root(output_root: Path, input_roots: Iterable[InputRoot]) -> Path:
    resolved_output = output_root.resolve(strict=False)
    for input_root in input_roots:
        if resolved_output == input_root.path or resolved_output.is_relative_to(input_root.path):
            raise ValueError(f"output root must not be inside input root: {input_root.path}")
    return resolved_output


def validate_dupe_root(dupe_root: Path, input_roots: Iterable[InputRoot]) -> Path:
    resolved_dupe = validate_output_root(dupe_root, input_roots)
    if not resolved_dupe.is_dir():
        raise ValueError(f"dupe/output root is not a directory: {resolved_dupe}")
    return resolved_dupe


def is_primary_image(path: Path) -> bool:
    return path.suffix.lower() in PRIMARY_IMAGE_EXTENSIONS


def scan_primary_files(input_roots: Iterable[InputRoot]) -> list[PrimaryFile]:
    primaries: list[PrimaryFile] = []
    for input_root in input_roots:
        for path in sorted(input_root.path.rglob("*")):
            if path.is_symlink() or not path.is_file() or not is_primary_image(path):
                continue
            stat = path.stat()
            sidecars = tuple(find_sidecars(path))
            primaries.append(
                PrimaryFile(
                    path=path,
                    input_root=input_root,
                    relative_path=path.relative_to(input_root.path),
                    size_bytes=stat.st_size,
                    sidecars=sidecars,
                )
            )
    return primaries


def find_sidecars(primary_path: Path) -> list[Path]:
    sidecars = []
    for candidate in sorted(primary_path.parent.iterdir()):
        if candidate == primary_path:
            continue
        if candidate.is_symlink() or not candidate.is_file():
            continue
        if candidate.stem != primary_path.stem:
            continue
        if is_primary_image(candidate):
            continue
        sidecars.append(candidate)
    return sidecars


def hash_file(path: Path) -> str:
    hasher = xxhash.xxh64()
    with path.open("rb", buffering=0) as file:
        while chunk := file.read(HASH_CHUNK_SIZE):
            hasher.update(chunk)
    return hasher.hexdigest()


def files_equal(left: Path, right: Path) -> bool:
    if left.stat().st_size != right.stat().st_size:
        return False
    with left.open("rb", buffering=0) as left_file, right.open("rb", buffering=0) as right_file:
        while True:
            left_chunk = left_file.read(HASH_CHUNK_SIZE)
            right_chunk = right_file.read(HASH_CHUNK_SIZE)
            if left_chunk != right_chunk:
                return False
            if not left_chunk:
                return True


def build_duplicate_groups(primaries: Iterable[PrimaryFile]) -> tuple[list[DuplicateGroup], list[PrimaryFile], list[tuple[str, tuple[PrimaryFile, ...]]]]:
    by_size: dict[int, list[PrimaryFile]] = defaultdict(list)
    for primary in primaries:
        by_size[primary.size_bytes].append(primary)

    uniques: list[PrimaryFile] = []
    duplicate_groups: list[DuplicateGroup] = []
    split_groups: list[tuple[str, tuple[PrimaryFile, ...]]] = []
    group_number = 1

    for size_bytes, size_group in sorted(by_size.items()):
        if len(size_group) == 1:
            uniques.extend(size_group)
            continue

        by_hash: dict[str, list[PrimaryFile]] = defaultdict(list)
        for primary in size_group:
            by_hash[hash_file(primary.path)].append(primary)

        for digest, hash_group in sorted(by_hash.items()):
            if len(hash_group) == 1:
                uniques.extend(hash_group)
                continue

            equal_sets = split_equal_files(hash_group)
            if len(equal_sets) > 1:
                split_groups.append((digest, tuple(hash_group)))
            for equal_set in equal_sets:
                if len(equal_set) == 1:
                    uniques.extend(equal_set)
                else:
                    duplicate_groups.append(
                        DuplicateGroup(
                            group_id=f"g{group_number:06d}",
                            files=tuple(sorted(equal_set, key=sort_key)),
                            digest=digest,
                            size_bytes=size_bytes,
                        )
                    )
                    group_number += 1

    return duplicate_groups, sorted(uniques, key=sort_key), split_groups


def split_equal_files(files: list[PrimaryFile]) -> list[list[PrimaryFile]]:
    groups: list[list[PrimaryFile]] = []
    for file in sorted(files, key=sort_key):
        for group in groups:
            if files_equal(file.path, group[0].path):
                group.append(file)
                break
        else:
            groups.append([file])
    return groups


def choose_keeper(group: DuplicateGroup) -> tuple[PrimaryFile | None, str]:
    with_sidecars = [primary for primary in group.files if primary.sidecars]
    if len(with_sidecars) > 1 and not sidecar_sets_equivalent(with_sidecars):
        return None, "unresolved_sidecar_conflict"
    return sorted(group.files, key=keeper_key)[0], "selected_by_priority"


def sidecar_sets_equivalent(files: list[PrimaryFile]) -> bool:
    first = files[0]
    for other in files[1:]:
        if not sidecars_equivalent(first.sidecars, other.sidecars):
            return False
    return True


def sidecars_equivalent(left: tuple[Path, ...], right: tuple[Path, ...]) -> bool:
    left_sorted = sorted(left, key=lambda path: (path.suffix.lower(), path.name.lower()))
    right_sorted = sorted(right, key=lambda path: (path.suffix.lower(), path.name.lower()))
    if [path.suffix.lower() for path in left_sorted] != [path.suffix.lower() for path in right_sorted]:
        return False
    return all(
        files_have_same_signature(left_path, right_path) and files_equal(left_path, right_path)
        for left_path, right_path in zip(left_sorted, right_sorted, strict=True)
    )


def files_have_same_signature(left: Path, right: Path) -> bool:
    return left.stat().st_size == right.stat().st_size and hash_file(left) == hash_file(right)


def keeper_key(primary: PrimaryFile) -> tuple[int, int, int, int, str, str]:
    return (
        0 if primary.sidecars else 1,
        date_directory_score(primary.path),
        0 if has_takeout_segment(primary.path) else 1,
        1 if has_mobilebackup_segment(primary.path) else 0,
        primary.input_root.label.lower(),
        primary.relative_path.as_posix().lower(),
    )


def sort_key(primary: PrimaryFile) -> tuple[str, str]:
    return primary.input_root.label.lower(), primary.relative_path.as_posix().lower()


def has_takeout_segment(path: Path) -> bool:
    return any("takeout" in part.lower() for part in path.parts)


def has_mobilebackup_segment(path: Path) -> bool:
    return any("mobilebackup" in part.lower() for part in path.parts)


def date_directory_score(path: Path) -> int:
    return sum(1 for part in path.parent.parts if DATE_DIRECTORY_TOKEN_RE.search(part))


class CsvLogger:
    def __init__(self, file: TextIO, mode: str) -> None:
        self._writer = csv.DictWriter(file, fieldnames=CSV_FIELDS)
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
                "xxh64": digest,
                "reason": reason,
                "message": message,
            }
        )


def run_dedup(
    input_paths: list[Path],
    output_path: Path,
    log_path: Path | None,
    move: bool,
    show_progress: bool = False,
) -> Path:
    input_roots = normalize_input_roots(input_paths)
    output_root = validate_output_root(output_path, input_roots)
    actual_log_path = log_path or default_log_path()
    mode = "move" if move else "dry_run"

    primaries = scan_primary_files(input_roots)
    progress = Progress(mode=f"dedup {mode}", total_images=len(primaries), enabled=show_progress)
    groups, uniques, split_groups = build_duplicate_groups(primaries)

    try:
        actual_log_path.parent.mkdir(parents=True, exist_ok=True)
        with actual_log_path.open("w", newline="", encoding="utf-8") as file:
            logger = CsvLogger(file, mode=mode)
            for unique in uniques:
                logger.row(
                    disposition="kept_unique_primary",
                    event="unique_primary",
                    status="unique",
                    file_role="primary",
                    input_root=unique.input_root.label,
                    source_path=unique.path,
                    size_bytes=unique.size_bytes,
                    message="no equal primary image found",
                )
                progress.kept(unique.size_bytes)
                progress.image_processed()
            for digest, split_files in split_groups:
                logger.row(
                    disposition="skipped_hash_split",
                    event="hash_collision_or_hash_group_split",
                    status="skipped",
                    digest=digest,
                    size_bytes=split_files[0].size_bytes if split_files else "",
                    reason="same size and xxh64 digest but byte comparison split the group",
                    message="unequal files were not deduped together",
                )
            for group in groups:
                process_duplicate_group(group, output_root, move, logger, progress)
    finally:
        progress.finish()

    return actual_log_path


def process_duplicate_group(
    group: DuplicateGroup,
    output_root: Path,
    move: bool,
    logger: CsvLogger,
    progress: Progress,
) -> None:
    keeper, reason = choose_keeper(group)
    if keeper is None:
        logger.row(
            disposition="skipped_sidecar_conflict_group",
            event="duplicate_group_skipped",
            status="skipped",
            group_id=group.group_id,
            size_bytes=group.size_bytes,
            digest=group.digest,
            reason=reason,
            message="duplicate group has conflicting sidecars",
        )
        for primary in group.files:
            progress.kept(primary.size_bytes)
            progress.image_processed()
            logger.row(
                disposition="kept_sidecar_conflict",
                event="duplicate_primary_kept_due_to_sidecar_conflict",
                status="skipped",
                group_id=group.group_id,
                file_role="primary",
                input_root=primary.input_root.label,
                source_path=primary.path,
                size_bytes=primary.size_bytes,
                digest=group.digest,
                reason=reason,
                message="primary left in place because duplicate group has conflicting sidecars",
            )
            for sidecar in primary.sidecars:
                progress.file_processed()
                logger.row(
                    disposition="kept_sidecar_conflict",
                    event="sidecar_kept_due_to_sidecar_conflict",
                    status="skipped",
                    group_id=group.group_id,
                    file_role="sidecar",
                    input_root=primary.input_root.label,
                    source_path=sidecar,
                    size_bytes=sidecar.stat().st_size,
                    reason=reason,
                    message="sidecar left in place because duplicate group has conflicting sidecars",
                )
        return

    logger.row(
        disposition="kept_duplicate_keeper",
        event="keeper_primary",
        status="kept",
        group_id=group.group_id,
        file_role="primary",
        input_root=keeper.input_root.label,
        source_path=keeper.path,
        keeper_path=keeper.path,
        size_bytes=keeper.size_bytes,
        digest=group.digest,
        reason=reason,
    )
    progress.kept(keeper.size_bytes)
    progress.image_processed()

    for duplicate in group.files:
        if duplicate == keeper:
            continue
        process_duplicate_file(duplicate, keeper, group, output_root, move, logger, progress)


def process_duplicate_file(
    duplicate: PrimaryFile,
    keeper: PrimaryFile,
    group: DuplicateGroup,
    output_root: Path,
    move: bool,
    logger: CsvLogger,
    progress: Progress,
) -> None:
    bundle = [(duplicate.path, "primary")] + [(sidecar, "sidecar") for sidecar in duplicate.sidecars]
    destinations = [(source, destination_for(output_root, duplicate.input_root, source), role) for source, role in bundle]
    collision = next((destination for _, destination, _ in destinations if destination.exists()), None)
    if collision is not None:
        for source, destination, role in destinations:
            progress.error()
            progress.kept(source.stat().st_size if role == "primary" else 0)
            if role == "primary":
                progress.image_processed()
            else:
                progress.file_processed()
            logger.row(
                disposition="kept_error",
                event="move_skipped_destination_exists",
                status="error",
                group_id=group.group_id,
                file_role=role,
                input_root=duplicate.input_root.label,
                source_path=source,
                destination_path=destination,
                keeper_path=keeper.path,
                size_bytes=source.stat().st_size,
                digest=group.digest if role == "primary" else "",
                reason="destination_exists",
                message=f"bundle skipped because destination exists: {collision}",
            )
        return

    for source, destination, role in destinations:
        status = "moved" if move else "planned"
        event = "duplicate_primary_move" if role == "primary" else "sidecar_move"
        disposition = f"{status}_duplicate_primary" if role == "primary" else f"{status}_sidecar"
        if move:
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(source), str(destination))
        progress.moved()
        if role == "primary":
            progress.image_processed()
        else:
            progress.file_processed()
        logger.row(
            disposition=disposition,
            event=event,
            status=status,
            group_id=group.group_id,
            file_role=role,
            input_root=duplicate.input_root.label,
            source_path=source,
            destination_path=destination,
            keeper_path=keeper.path,
            size_bytes=destination.stat().st_size if move else source.stat().st_size,
            digest=group.digest if role == "primary" else "",
            reason="duplicate_of_keeper",
        )


def destination_for(output_root: Path, input_root: InputRoot, source: Path) -> Path:
    return output_root / input_root.label / source.relative_to(input_root.path)
