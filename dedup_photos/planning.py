from __future__ import annotations

from collections import Counter, defaultdict
from pathlib import Path

from dedup_photos.common import (
    CsvLogger,
    date_directory_score,
    default_log_path,
    has_mobilebackup_segment,
    has_takeout_segment,
)
from dedup_photos.manifest_io import load_manifests
from dedup_photos.models import ManifestEntry, ManifestPlanResult
from dedup_photos.progress import Progress

VIDEO_SIDECAR_EXTENSIONS = {".mov", ".mp4"}
JSON_SIDECAR_EXTENSIONS = {".json"}


def plan_from_manifests(
    manifest_paths: list[Path],
    output_root: Path,
    log_path: Path | None,
    show_progress: bool = False,
    ignore_json_sidecar_fields: bool = False,
) -> ManifestPlanResult:
    progress = Progress(mode="manifest_plan", total_images=0, enabled=show_progress)
    try:
        progress.start_phase("manifest-load", len(manifest_paths))
        entries = load_manifests(manifest_paths, progress)
        actual_log_path = log_path or default_log_path()
        groups, uniques = manifest_duplicate_groups(entries)
        progress.manifest_group_stats(len(groups), len(uniques))
        progress.start_phase("manifest-plan", len(uniques) + len(groups))
        duplicate_files = 0
        skipped_groups = 0

        actual_log_path.parent.mkdir(parents=True, exist_ok=True)
        with actual_log_path.open("w", newline="", encoding="utf-8") as file:
            logger = CsvLogger(file, mode="manifest_plan", hash_field="xxh128")
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
                log_manifest_kept_sidecars(
                    logger,
                    group_id="",
                    entry=unique,
                    disposition="kept_unique_sidecar",
                    event="unique_sidecar",
                    status="unique",
                    reason="primary_is_unique",
                )
                progress.advance()
            for index, group in enumerate(groups, start=1):
                group_id = f"m{index:06d}"
                keeper = choose_manifest_keeper(group, ignore_json_sidecar_fields=ignore_json_sidecar_fields)
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
                log_manifest_kept_sidecars(
                    logger,
                    group_id=group_id,
                    entry=keeper,
                    disposition="kept_duplicate_keeper_sidecar",
                    event="keeper_sidecar",
                    status="kept",
                    reason="selected_with_keeper",
                )
                merge_needed = manifest_required_sidecar_keys(group, ignore_json_sidecar_fields) - manifest_entry_sidecar_key_counter(
                    keeper,
                    ignore_json_sidecar_fields,
                )
                for duplicate in sorted(group, key=manifest_sort_key):
                    if duplicate == keeper:
                        continue
                    duplicate_files += 1
                    progress.manifest_planned_move()
                    log_manifest_duplicate_plan(
                        logger,
                        group_id,
                        duplicate,
                        keeper,
                        merge_needed,
                        output_root,
                        ignore_json_sidecar_fields=ignore_json_sidecar_fields,
                    )
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


def choose_manifest_keeper(group: list[ManifestEntry], ignore_json_sidecar_fields: bool = False) -> ManifestEntry | None:
    with_sidecars = [entry for entry in group if entry.sidecar_paths]
    if with_sidecars:
        candidates = manifest_sidecar_superset_candidates(
            with_sidecars,
            ignore_json_sidecar_fields=ignore_json_sidecar_fields,
        )
        if candidates:
            return sorted(candidates, key=manifest_keeper_key)[0]
        if not manifest_sidecars_are_class_merge_compatible(
            group,
            ignore_json_sidecar_fields=ignore_json_sidecar_fields,
        ):
            return None
    return sorted(group, key=manifest_keeper_key)[0]


def manifest_sidecar_sets_equivalent(entries: list[ManifestEntry]) -> bool:
    first = manifest_sidecar_signature(entries[0])
    return all(manifest_sidecar_signature(entry) == first for entry in entries[1:])


def manifest_sidecar_signature(entry: ManifestEntry) -> tuple[str, ...]:
    return tuple(sorted(entry.sidecar_xxh128s))


def manifest_sidecar_superset_candidates(
    entries: list[ManifestEntry],
    ignore_json_sidecar_fields: bool = False,
) -> list[ManifestEntry]:
    counters = [(entry, manifest_entry_sidecar_key_counter(entry, ignore_json_sidecar_fields)) for entry in entries]
    return [
        entry
        for entry, counter in counters
        if all(counter >= other_counter for _other_entry, other_counter in counters)
    ]


def manifest_sidecars_are_class_merge_compatible(
    entries: list[ManifestEntry],
    ignore_json_sidecar_fields: bool = False,
) -> bool:
    class_counters = manifest_sidecar_class_counters(
        entries,
        ignore_json_sidecar_fields=ignore_json_sidecar_fields,
    )
    if class_counters is None:
        return False
    for counters in class_counters.values():
        if not counters:
            continue
        if not any(all(candidate >= other for other in counters) for candidate in counters):
            return False
    return True


def manifest_sidecar_class_counters(
    entries: list[ManifestEntry],
    ignore_json_sidecar_fields: bool = False,
) -> dict[str, list[Counter[str]]] | None:
    counters_by_class: dict[str, list[Counter[str]]] = defaultdict(list)
    for entry in entries:
        entry_counters: dict[str, Counter[str]] = defaultdict(Counter)
        for path, digest in zip(entry.sidecar_paths, entry.sidecar_xxh128s, strict=True):
            sidecar_class = manifest_sidecar_class(path)
            if sidecar_class is None:
                return None
            entry_counters[sidecar_class][manifest_sidecar_key(path, digest, ignore_json_sidecar_fields)] += 1
        for sidecar_class in ("video", "json"):
            counter = entry_counters.get(sidecar_class, Counter())
            if counter:
                counters_by_class[sidecar_class].append(counter)
    return counters_by_class


def manifest_required_sidecar_hashes(entries: list[ManifestEntry]) -> Counter[str]:
    return manifest_required_sidecar_keys(entries, ignore_json_sidecar_fields=False)


def manifest_required_sidecar_keys(entries: list[ManifestEntry], ignore_json_sidecar_fields: bool) -> Counter[str]:
    required: Counter[str] = Counter()
    class_counters = manifest_sidecar_class_counters(entries, ignore_json_sidecar_fields=ignore_json_sidecar_fields) or {}
    for counters in class_counters.values():
        if not counters:
            continue
        required.update(sorted(counters, key=lambda counter: sum(counter.values()), reverse=True)[0])
    return required


def manifest_entry_sidecar_key_counter(entry: ManifestEntry, ignore_json_sidecar_fields: bool) -> Counter[str]:
    counter: Counter[str] = Counter()
    for path, digest in zip(entry.sidecar_paths, entry.sidecar_xxh128s, strict=True):
        counter[manifest_sidecar_key(path, digest, ignore_json_sidecar_fields)] += 1
    return counter


def manifest_sidecar_key(path: Path, digest: str, ignore_json_sidecar_fields: bool) -> str:
    if ignore_json_sidecar_fields and path.suffix.lower() in JSON_SIDECAR_EXTENSIONS:
        return "__json_sidecar__"
    return digest


def manifest_sidecar_class(path: Path) -> str | None:
    suffix = path.suffix.lower()
    if suffix in VIDEO_SIDECAR_EXTENSIONS:
        return "video"
    if suffix in JSON_SIDECAR_EXTENSIONS:
        return "json"
    return None


def manifest_keeper_key(entry: ManifestEntry) -> tuple[int, int, int, str, str]:
    return (
        0 if entry.sidecar_paths else 1,
        manifest_path_class(entry.nas_path),
        date_directory_score(entry.nas_path),
        entry.nas_root_label.lower(),
        entry.relative_path.as_posix().lower(),
    )


def manifest_path_class(path: Path) -> int:
    if has_takeout_segment(path):
        return 0
    if has_mobilebackup_segment(path):
        return 2
    return 1


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
                primary_source_path=entry.nas_path,
                source_path=sidecar_path,
                size_bytes=sidecar_size,
                digest=sidecar_hash,
                reason="unresolved_sidecar_conflict",
            )


def log_manifest_kept_sidecars(
    logger: CsvLogger,
    *,
    group_id: str,
    entry: ManifestEntry,
    disposition: str,
    event: str,
    status: str,
    reason: str,
) -> None:
    for sidecar_path, sidecar_size, sidecar_hash in zip(
        entry.sidecar_paths,
        entry.sidecar_sizes,
        entry.sidecar_xxh128s,
        strict=True,
    ):
        logger.row(
            disposition=disposition,
            event=event,
            status=status,
            group_id=group_id,
            file_role="sidecar",
            input_root=entry.nas_root_label,
            primary_source_path=entry.nas_path,
            source_path=sidecar_path,
            keeper_path=entry.nas_path if status == "kept" else "",
            size_bytes=sidecar_size,
            digest=sidecar_hash,
            reason=reason,
        )


def log_manifest_duplicate_plan(
    logger: CsvLogger,
    group_id: str,
    duplicate: ManifestEntry,
    keeper: ManifestEntry,
    merge_needed: Counter[str],
    output_root: Path,
    *,
    ignore_json_sidecar_fields: bool = False,
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
        sidecar_key = manifest_sidecar_key(path, digest, ignore_json_sidecar_fields)
        if merge_needed[sidecar_key] > 0:
            merge_needed[sidecar_key] -= 1
            logger.row(
                disposition="planned_sidecar_merge",
                event="sidecar_merge",
                status="planned",
                group_id=group_id,
                file_role="sidecar",
                input_root=duplicate.nas_root_label,
                primary_source_path=duplicate.nas_path,
                source_path=path,
                destination_path=manifest_merged_sidecar_path(path, duplicate.nas_path, keeper.nas_path),
                duplicate_output_path=manifest_destination_for(output_root, duplicate.nas_root_label, relative_path),
                keeper_path=keeper.nas_path,
                size_bytes=size_bytes,
                digest=digest,
                reason="merge_into_keeper",
            )
            continue
        logger.row(
            disposition="planned_duplicate_sidecar",
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


def manifest_merged_sidecar_path(sidecar_path: Path, duplicate_primary_path: Path, keeper_primary_path: Path) -> Path:
    sidecar_name = sidecar_path.name
    duplicate_name = duplicate_primary_path.name
    duplicate_stem = duplicate_primary_path.stem
    keeper_name = keeper_primary_path.name
    keeper_stem = keeper_primary_path.stem
    sidecar_lower = sidecar_name.lower()
    duplicate_name_lower = duplicate_name.lower()
    duplicate_stem_lower = duplicate_stem.lower()
    if sidecar_lower.startswith(duplicate_name_lower):
        return keeper_primary_path.parent / f"{keeper_name}{sidecar_name[len(duplicate_name):]}"
    if sidecar_lower.startswith(duplicate_stem_lower):
        return keeper_primary_path.parent / f"{keeper_stem}{sidecar_name[len(duplicate_stem):]}"
    return keeper_primary_path.parent / f"{keeper_stem}{sidecar_path.suffix}"


def manifest_destination_for(output_root: Path, nas_root_label: str, relative_path: Path) -> Path:
    return output_root / nas_root_label / relative_path
