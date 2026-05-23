from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

from dedup_photos.common import CsvLogger, default_log_path
from dedup_photos.hashing import files_equal_by_path
from dedup_photos.manifest_io import load_manifests
from dedup_photos.models import ManifestEntry, ManifestVerifyResult, VerifyMoveResult
from dedup_photos.progress import Progress


VERIFY_MOVE_DATE_DIRECTORY_TOKEN_RE = re.compile(r"(?<!\d)(?:\d{4}-\d{2}-\d{2}|\d{8}|\d{4})(?!\d)")


@dataclass(frozen=True)
class ByteCheckItem:
    path: Path
    size_bytes: int
    digest: str
    input_root: str
    file_role: str


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
        primary_groups, uniques = manifest_duplicate_groups(entries)
        sidecar_groups = manifest_sidecar_duplicate_groups(entries)
        byte_check_groups = [
            ("primary", f"m{index:06d}", manifest_entry_byte_check_items(group))
            for index, group in enumerate(primary_groups, start=1)
        ]
        byte_check_groups.extend(
            ("sidecar", f"s{index:06d}", group)
            for index, group in enumerate(sidecar_groups, start=1)
        )
        progress.manifest_group_stats(len(byte_check_groups), len(uniques))
        progress.start_phase("manifest-verify-bytes", len(byte_check_groups))
        actual_log_path = log_path or default_log_path()
        failed_groups = 0

        actual_log_path.parent.mkdir(parents=True, exist_ok=True)
        with actual_log_path.open("w", newline="", encoding="utf-8") as file:
            logger = CsvLogger(file, mode="manifest_verify", hash_field="xxh128")
            for file_role, group_id, group in byte_check_groups:
                failures = byte_check_item_group(group, progress)
                failed = bool(failures)
                if failed:
                    failed_groups += 1
                    for item in failures:
                        logger.row(
                            disposition="verify_failed",
                            event=f"verify_manifest_{file_role}",
                            status="error",
                            group_id=group_id,
                            file_role=file_role,
                            input_root=item.input_root,
                            source_path=item.path,
                            size_bytes=item.size_bytes,
                            digest=item.digest,
                            reason="same_manifest_hash_but_bytes_differ",
                        )
                else:
                    logger.row(
                        disposition="verify_matched",
                        event=f"verify_manifest_{file_role}_group",
                        status="kept",
                        group_id=group_id,
                        file_role=file_role,
                        size_bytes=group[0].size_bytes,
                        digest=group[0].digest,
                        reason="all_manifest_hash_matches_are_byte_equal",
                    )
                progress.manifest_group_checked(failed)

        return ManifestVerifyResult(
            log_path=actual_log_path,
            checked_groups=len(byte_check_groups),
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


def byte_check_item_group(group: list[ByteCheckItem], progress: Progress | None = None) -> list[ByteCheckItem]:
    reference = group[0]
    failures: list[ByteCheckItem] = []
    for item in group[1:]:
        if not files_equal_by_path(reference.path, item.path, progress):
            failures.append(item)
    return failures


def manifest_entry_byte_check_items(group: list[ManifestEntry]) -> list[ByteCheckItem]:
    return [
        ByteCheckItem(
            path=entry.nas_path,
            size_bytes=entry.size_bytes,
            digest=entry.xxh128,
            input_root=entry.nas_root_label,
            file_role="primary",
        )
        for entry in group
    ]


def manifest_sidecar_duplicate_groups(entries: list[ManifestEntry]) -> list[list[ByteCheckItem]]:
    buckets: dict[tuple[int, str], list[ByteCheckItem]] = defaultdict(list)
    for entry in entries:
        for path, size_bytes, digest in zip(entry.sidecar_paths, entry.sidecar_sizes, entry.sidecar_xxh128s, strict=True):
            buckets[(size_bytes, digest)].append(
                ByteCheckItem(
                    path=path,
                    size_bytes=size_bytes,
                    digest=digest,
                    input_root=entry.nas_root_label,
                    file_role="sidecar",
                )
            )
    return [bucket for bucket in buckets.values() if len(bucket) > 1]


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
