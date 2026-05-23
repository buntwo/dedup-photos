from __future__ import annotations

import csv
import shutil
from collections import defaultdict
from pathlib import Path

from dedup_photos.common import CsvLogger, default_log_path
from dedup_photos.hashing import hash_file_xxh128
from dedup_photos.models import ExecutePlanResult, PlanBundle, PlanRow
from dedup_photos.progress import Progress


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
