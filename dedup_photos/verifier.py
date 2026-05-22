from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

from dedup_photos.deduper import (
    CsvLogger,
    InputRoot,
    default_log_path,
    files_equal,
    hash_file,
    is_primary_image,
    normalize_input_roots,
    sidecar_belongs_to_primary,
    validate_dupe_root,
)
from dedup_photos.progress import Progress


VERIFY_DATE_DIRECTORY_TOKEN_RE = re.compile(r"(?<!\d)(?:\d{4}-\d{2}-\d{2}|\d{8}|\d{4})(?!\d)")


@dataclass(frozen=True)
class VerifyPrimary:
    path: Path
    source: str
    input_label: str
    relative_path: Path
    logical_parts: tuple[str, ...]
    size_bytes: int
    digest: str
    sidecars: tuple[Path, ...]


@dataclass(frozen=True)
class VerificationResult:
    checked: int
    matched: int
    failed: int
    log_path: Path


def run_verify(
    input_paths: list[Path],
    dupe_path: Path,
    log_path: Path | None,
    show_progress: bool = False,
) -> VerificationResult:
    input_roots = normalize_input_roots(input_paths)
    dupe_root = validate_dupe_root(dupe_path, input_roots)
    actual_log_path = log_path or default_log_path()

    progress = Progress(mode="verify", total_images=0, enabled=show_progress)
    progress.start_phase("scan-inputs", count_input_entries(input_roots))
    input_primaries = scan_verify_inputs(input_roots, progress)
    progress.start_phase("scan-dupes", count_dupe_entries(dupe_root))
    dupe_primaries = scan_verify_dupes(dupe_root, progress)
    progress.total_images = len(dupe_primaries)
    progress.start_phase("index", len(input_primaries) + len(dupe_primaries))
    index = build_verify_index([*input_primaries, *dupe_primaries], progress)
    progress.start_phase("verify", len(dupe_primaries))

    matched = 0
    failed = 0
    counted_keeper_paths: set[Path] = set()
    try:
        actual_log_path.parent.mkdir(parents=True, exist_ok=True)
        with actual_log_path.open("w", newline="", encoding="utf-8") as file:
            logger = CsvLogger(file, mode="verify")
            for dupe in dupe_primaries:
                equal_group = equal_verify_group(dupe, index)
                input_matches = [candidate for candidate in equal_group if candidate.source == "input"]
                if not input_matches:
                    failed += 1
                    progress.error()
                    progress.image_processed()
                    progress.advance()
                    log_verify_failure(
                        logger,
                        dupe,
                        reason="no_equal_input_primary",
                        message="output primary image has no byte-equal primary image in the input roots",
                    )
                    continue

                conflict = independently_detect_sidecar_conflict(equal_group)
                if conflict:
                    failed += 1
                    progress.error()
                    progress.image_processed()
                    progress.advance()
                    log_verify_failure(
                        logger,
                        dupe,
                        reason="sidecar_conflict_should_not_have_moved",
                        message="equal group has conflicting sidecars, so dedup move should have skipped it",
                    )
                    continue

                expected_keeper = independently_choose_keeper(equal_group)
                if expected_keeper.source != "input":
                    failed += 1
                    progress.error()
                    progress.image_processed()
                    progress.advance()
                    log_verify_failure(
                        logger,
                        dupe,
                        reason="moved_file_should_have_been_keeper",
                        keeper_path=expected_keeper.path,
                        message="independent precedence rules selected an output file as the keeper",
                    )
                    continue

                matched += 1
                if expected_keeper.path not in counted_keeper_paths:
                    counted_keeper_paths.add(expected_keeper.path)
                    progress.kept(expected_keeper.size_bytes)
                progress.image_processed()
                progress.advance()
                logger.row(
                    disposition="verify_matched",
                    event="verify_output_primary",
                    status="kept",
                    file_role="primary",
                    input_root=dupe.input_label,
                    source_path=dupe.path,
                    keeper_path=expected_keeper.path,
                    size_bytes=dupe.size_bytes,
                    digest=dupe.digest,
                    reason="equal_input_primary_and_precedence_verified",
                )
    finally:
        progress.finish()

    return VerificationResult(
        checked=len(dupe_primaries),
        matched=matched,
        failed=failed,
        log_path=actual_log_path,
    )


def scan_verify_inputs(input_roots: list[InputRoot], progress: Progress | None = None) -> list[VerifyPrimary]:
    primaries: list[VerifyPrimary] = []
    for input_root in input_roots:
        for path in sorted(input_root.path.rglob("*")):
            if progress is not None:
                progress.file_processed()
                progress.advance()
            if path.is_symlink() or not path.is_file() or not is_primary_image(path):
                continue
            relative_path = path.relative_to(input_root.path)
            stat = path.stat()
            if progress is not None:
                progress.primary_seen(stat.st_size)
            primaries.append(
                VerifyPrimary(
                    path=path,
                    source="input",
                    input_label=input_root.label,
                    relative_path=relative_path,
                    logical_parts=(input_root.label, *relative_path.parts),
                    size_bytes=stat.st_size,
                    digest=hash_file(path),
                    sidecars=tuple(independently_find_sidecars(path)),
                )
            )
    return primaries


def scan_verify_dupes(dupe_root: Path, progress: Progress | None = None) -> list[VerifyPrimary]:
    primaries: list[VerifyPrimary] = []
    for path in sorted(dupe_root.rglob("*")):
        if progress is not None:
            progress.file_processed()
            progress.advance()
        if path.is_symlink() or not path.is_file() or not is_primary_image(path):
            continue
        relative_path = path.relative_to(dupe_root)
        input_label = relative_path.parts[0] if relative_path.parts else dupe_root.name
        stat = path.stat()
        if progress is not None:
            progress.primary_seen(stat.st_size)
        primaries.append(
            VerifyPrimary(
                path=path,
                source="dupe",
                input_label=input_label,
                relative_path=relative_path,
                logical_parts=relative_path.parts,
                size_bytes=stat.st_size,
                digest=hash_file(path),
                sidecars=tuple(independently_find_sidecars(path)),
            )
        )
    return primaries


def independently_find_sidecars(primary_path: Path) -> list[Path]:
    sidecars: list[Path] = []
    for candidate in sorted(primary_path.parent.iterdir()):
        if candidate == primary_path:
            continue
        if candidate.is_symlink() or not candidate.is_file():
            continue
        if not sidecar_belongs_to_primary(candidate, primary_path):
            continue
        if is_primary_image(candidate):
            continue
        sidecars.append(candidate)
    return sidecars


def build_verify_index(
    primaries: list[VerifyPrimary],
    progress: Progress | None = None,
) -> dict[tuple[int, str], list[VerifyPrimary]]:
    index: dict[tuple[int, str], list[VerifyPrimary]] = defaultdict(list)
    for primary in primaries:
        index[(primary.size_bytes, primary.digest)].append(primary)
        if progress is not None:
            progress.advance()
    return index


def equal_verify_group(
    primary: VerifyPrimary,
    index: dict[tuple[int, str], list[VerifyPrimary]],
) -> list[VerifyPrimary]:
    return [
        candidate
        for candidate in index[(primary.size_bytes, primary.digest)]
        if files_equal(primary.path, candidate.path)
    ]


def independently_detect_sidecar_conflict(group: list[VerifyPrimary]) -> bool:
    with_sidecars = [primary for primary in group if primary.sidecars]
    if len(with_sidecars) <= 1:
        return False
    first_sidecars = with_sidecars[0].sidecars
    return any(not independently_sidecars_equal(primary.sidecars, first_sidecars) for primary in with_sidecars[1:])


def independently_sidecars_equal(left: tuple[Path, ...], right: tuple[Path, ...]) -> bool:
    return sorted(hash_file(path) for path in left) == sorted(hash_file(path) for path in right)


def independently_choose_keeper(group: list[VerifyPrimary]) -> VerifyPrimary:
    return sorted(group, key=independent_keeper_key)[0]


def independent_keeper_key(primary: VerifyPrimary) -> tuple[int, int, int, int, str]:
    lowered_parts = tuple(part.lower() for part in primary.logical_parts)
    return (
        0 if primary.sidecars else 1,
        independent_date_directory_score(primary.logical_parts),
        0 if any("takeout" in part for part in lowered_parts) else 1,
        1 if any("mobilebackup" in part for part in lowered_parts) else 0,
        "/".join(lowered_parts),
    )


def independent_date_directory_score(logical_parts: tuple[str, ...]) -> int:
    directory_parts = logical_parts[:-1]
    return sum(1 for part in directory_parts if VERIFY_DATE_DIRECTORY_TOKEN_RE.search(part))


def log_verify_failure(
    logger: CsvLogger,
    dupe: VerifyPrimary,
    *,
    reason: str,
    message: str,
    keeper_path: Path | str = "",
) -> None:
    logger.row(
        disposition="verify_failed",
        event="verify_output_primary",
        status="error",
        file_role="primary",
        input_root=dupe.input_label,
        source_path=dupe.path,
        keeper_path=keeper_path,
        size_bytes=dupe.size_bytes,
        digest=dupe.digest,
        reason=reason,
        message=message,
    )


def count_input_entries(input_roots: list[InputRoot]) -> int:
    return sum(1 for input_root in input_roots for _ in input_root.path.rglob("*"))


def count_dupe_entries(dupe_root: Path) -> int:
    return sum(1 for _ in dupe_root.rglob("*"))
