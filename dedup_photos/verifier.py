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
    validate_dupe_root,
)


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


def run_verify(input_paths: list[Path], dupe_path: Path, log_path: Path | None) -> VerificationResult:
    input_roots = normalize_input_roots(input_paths)
    dupe_root = validate_dupe_root(dupe_path, input_roots)
    actual_log_path = log_path or default_log_path()

    input_primaries = scan_verify_inputs(input_roots)
    dupe_primaries = scan_verify_dupes(dupe_root)
    index = build_verify_index([*input_primaries, *dupe_primaries])

    matched = 0
    failed = 0
    actual_log_path.parent.mkdir(parents=True, exist_ok=True)
    with actual_log_path.open("w", newline="", encoding="utf-8") as file:
        logger = CsvLogger(file, mode="verify")
        for dupe in dupe_primaries:
            equal_group = equal_verify_group(dupe, index)
            input_matches = [candidate for candidate in equal_group if candidate.source == "input"]
            if not input_matches:
                failed += 1
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
                log_verify_failure(
                    logger,
                    dupe,
                    reason="moved_file_should_have_been_keeper",
                    keeper_path=expected_keeper.path,
                    message="independent precedence rules selected an output file as the keeper",
                )
                continue

            matched += 1
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

    return VerificationResult(
        checked=len(dupe_primaries),
        matched=matched,
        failed=failed,
        log_path=actual_log_path,
    )


def scan_verify_inputs(input_roots: list[InputRoot]) -> list[VerifyPrimary]:
    primaries: list[VerifyPrimary] = []
    for input_root in input_roots:
        for path in sorted(input_root.path.rglob("*")):
            if path.is_symlink() or not path.is_file() or not is_primary_image(path):
                continue
            relative_path = path.relative_to(input_root.path)
            primaries.append(
                VerifyPrimary(
                    path=path,
                    source="input",
                    input_label=input_root.label,
                    relative_path=relative_path,
                    logical_parts=(input_root.label, *relative_path.parts),
                    size_bytes=path.stat().st_size,
                    digest=hash_file(path),
                    sidecars=tuple(independently_find_sidecars(path)),
                )
            )
    return primaries


def scan_verify_dupes(dupe_root: Path) -> list[VerifyPrimary]:
    primaries: list[VerifyPrimary] = []
    for path in sorted(dupe_root.rglob("*")):
        if path.is_symlink() or not path.is_file() or not is_primary_image(path):
            continue
        relative_path = path.relative_to(dupe_root)
        input_label = relative_path.parts[0] if relative_path.parts else dupe_root.name
        primaries.append(
            VerifyPrimary(
                path=path,
                source="dupe",
                input_label=input_label,
                relative_path=relative_path,
                logical_parts=relative_path.parts,
                size_bytes=path.stat().st_size,
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
        if candidate.stem != primary_path.stem:
            continue
        if is_primary_image(candidate):
            continue
        sidecars.append(candidate)
    return sidecars


def build_verify_index(primaries: list[VerifyPrimary]) -> dict[tuple[int, str], list[VerifyPrimary]]:
    index: dict[tuple[int, str], list[VerifyPrimary]] = defaultdict(list)
    for primary in primaries:
        index[(primary.size_bytes, primary.digest)].append(primary)
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
    left_sorted = sorted(left, key=lambda path: (path.suffix.lower(), path.name.lower()))
    right_sorted = sorted(right, key=lambda path: (path.suffix.lower(), path.name.lower()))
    if [path.suffix.lower() for path in left_sorted] != [path.suffix.lower() for path in right_sorted]:
        return False
    for left_path, right_path in zip(left_sorted, right_sorted, strict=True):
        if left_path.stat().st_size != right_path.stat().st_size:
            return False
        if hash_file(left_path) != hash_file(right_path):
            return False
        if not files_equal(left_path, right_path):
            return False
    return True


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
