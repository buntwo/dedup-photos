from __future__ import annotations

import csv
import shutil
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

from dedup_photos.common import CsvLogger, default_log_path
from dedup_photos.hashing import hash_file_xxh128
from dedup_photos.models import ExecutePlanResult, PlanBundle, PlanRow
from dedup_photos.progress import Progress


@dataclass(frozen=True)
class FileDiagnostic:
    hash_check: str = "not_checked"
    observed_hash: str = ""
    validation_result: str = "validated"
    action_destination: Path | None = None


@dataclass(frozen=True)
class BundleValidation:
    result: str | None
    keeper: FileDiagnostic | None
    rows: dict[int, FileDiagnostic]


def execute_plan(
    plan_path: Path,
    log_path: Path | None,
    move: bool,
    show_progress: bool = False,
    verify_source_hashes: bool | None = None,
) -> ExecutePlanResult:
    progress = Progress(mode="execute_plan", total_images=0, enabled=show_progress)
    try:
        progress.start_phase("manifest-load-plan", count_csv_data_rows(plan_path))
        bundles, orphan_sidecars, hash_field = load_plan_bundles(plan_path, progress)
        actual_log_path = log_path or default_log_path()
        duplicate_destinations = duplicate_destination_paths(bundles)
        effective_verify_source_hashes = move if verify_source_hashes is None else verify_source_hashes
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
                    status="error",
                    group_id=sidecar.group_id,
                    file_role="sidecar",
                    input_root=sidecar.row.get("input_root", ""),
                    primary_source_path=sidecar.primary_source_path or "",
                    source_path=sidecar.source_path,
                    destination_path=sidecar.destination_path,
                    keeper_path=sidecar.row.get("keeper_path", ""),
                    size_bytes=sidecar.size_bytes,
                    hash_check="not_applicable",
                    validation_result="orphan_plan_sidecar",
                    action_taken="skipped",
                    reason="orphan_plan_sidecar",
                    message="planned sidecar row has no matching planned primary row",
                )
                progress.error()
                progress.manifest_bundle_processed()

            for bundle in bundles:
                validation = validate_plan_bundle(bundle, duplicate_destinations, effective_verify_source_hashes)
                if bundle.keeper is not None:
                    log_keeper(logger, bundle, validation)
                if validation.result == "already_moved":
                    already_moved_bundles += 1
                    log_bundle(
                        logger,
                        bundle,
                        validation,
                        disposition_prefix="already_moved",
                        status="kept",
                        reason="already_moved",
                    )
                    progress.manifest_bundle_processed()
                    continue
                if validation.result is not None:
                    skipped_bundles += 1
                    log_bundle(
                        logger,
                        bundle,
                        validation,
                        disposition_prefix="skipped_error",
                        status="error",
                        reason=validation.result,
                    )
                    progress.error()
                    progress.manifest_bundle_processed()
                    continue
                if not move:
                    planned_bundles += 1
                    progress.manifest_planned_move(len(bundle_rows(bundle)))
                    log_bundle(logger, bundle, validation, disposition_prefix="planned", status="planned", reason="validated")
                    progress.manifest_bundle_processed()
                    continue

                moved_sources: list[tuple[Path, Path]] = []
                try:
                    for row in bundle_rows(bundle):
                        move_destination = validation.rows.get(id(row), FileDiagnostic()).action_destination
                        if move_destination is None:
                            move_destination = row.destination_path
                        move_destination.parent.mkdir(parents=True, exist_ok=True)
                        shutil.move(str(row.source_path), str(move_destination))
                        progress.moved(row.size_bytes)
                        moved_sources.append((row.source_path, move_destination))
                except OSError as error:
                    rollback_errors = rollback_moves(moved_sources)
                    skipped_bundles += 1
                    message = f"move failed: {error}"
                    if rollback_errors:
                        message += f"; rollback errors: {'; '.join(rollback_errors)}"
                    move_validation = validation_with_result(validation, bundle, "move_failed")
                    log_bundle(
                        logger,
                        bundle,
                        move_validation,
                        disposition_prefix="skipped_error",
                        status="error",
                        reason="move_failed",
                        message=message,
                    )
                    progress.error()
                    progress.manifest_bundle_processed()
                    continue

                moved_bundles += 1
                log_bundle(logger, bundle, validation, disposition_prefix="moved", status="moved", reason="executed")
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
            "duplicate_output_path",
            "size_bytes",
            "primary_source_path",
        }
        missing = required - set(fieldnames)
        if missing:
            raise ValueError(f"plan missing required fields {sorted(missing)}: {plan_path}")
        hash_field = "xxh128" if "xxh128" in fieldnames else "xxh64"
        primaries: list[PlanRow] = []
        sidecars: list[PlanRow] = []
        merges: list[PlanRow] = []
        keepers: dict[str, PlanRow] = {}
        for row in reader:
            if progress is not None:
                progress.manifest_plan_row_loaded()
                progress.advance()
            disposition = row["disposition"]
            if disposition not in {
                "planned_duplicate_primary",
                "planned_duplicate_sidecar",
                "planned_sidecar_merge",
                "kept_duplicate_keeper",
            }:
                continue
            plan_row = parse_plan_row(row, hash_field, require_destination=disposition != "kept_duplicate_keeper")
            if plan_row.file_role == "primary" and disposition == "planned_duplicate_primary":
                primaries.append(plan_row)
            elif plan_row.file_role == "sidecar" and disposition == "planned_duplicate_sidecar":
                sidecars.append(plan_row)
            elif plan_row.file_role == "sidecar" and disposition == "planned_sidecar_merge":
                merges.append(plan_row)
            elif plan_row.file_role == "primary" and disposition == "kept_duplicate_keeper":
                if plan_row.group_id in keepers:
                    raise ValueError(f"multiple keeper rows for group_id {plan_row.group_id}: {plan_path}")
                keepers[plan_row.group_id] = plan_row

    sidecars_by_key: dict[tuple[str, Path], list[PlanRow]] = defaultdict(list)
    for sidecar in sidecars:
        sidecars_by_key[sidecar_bundle_key(sidecar)].append(sidecar)
    merges_by_key: dict[tuple[str, Path], list[PlanRow]] = defaultdict(list)
    for merge in merges:
        merges_by_key[sidecar_bundle_key(merge)].append(merge)

    bundles = []
    matched_sidecars: set[int] = set()
    matched_merges: set[int] = set()
    for primary in primaries:
        key = primary_bundle_key(primary)
        bundle_sidecars = tuple(sorted(sidecars_by_key.get(key, []), key=lambda row: str(row.source_path)))
        bundle_merges = tuple(sorted(merges_by_key.get(key, []), key=lambda row: str(row.source_path)))
        matched_sidecars.update(id(sidecar) for sidecar in bundle_sidecars)
        matched_merges.update(id(merge) for merge in bundle_merges)
        bundles.append(
            PlanBundle(keeper=keepers.get(primary.group_id), primary=primary, sidecars=bundle_sidecars, merges=bundle_merges)
        )
    orphans = [sidecar for sidecar in sidecars if id(sidecar) not in matched_sidecars]
    orphans.extend(merge for merge in merges if id(merge) not in matched_merges)
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
    duplicate_output_path = Path(row["duplicate_output_path"]) if row.get("duplicate_output_path", "") else None
    if row["file_role"] == "sidecar" and primary_source_path is None:
        raise ValueError(f"planned sidecar row must include primary_source_path: {row}")
    if row["file_role"] != "sidecar" and primary_source_path is not None:
        raise ValueError(f"non-sidecar plan row must not include primary_source_path: {row}")
    if row["disposition"] == "planned_sidecar_merge" and duplicate_output_path is None:
        raise ValueError(f"planned sidecar merge row must include duplicate_output_path: {row}")
    if row["disposition"] != "planned_sidecar_merge" and duplicate_output_path is not None:
        raise ValueError(f"non-merge plan row must not include duplicate_output_path: {row}")
    return PlanRow(
        row=row,
        source_path=Path(row["source_path"]),
        destination_path=Path(row["destination_path"]) if row["destination_path"] else Path(),
        duplicate_output_path=duplicate_output_path,
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
            if row.duplicate_output_path is not None:
                counts[row.duplicate_output_path] += 1
    return {path for path, count in counts.items() if count > 1}


def validate_plan_bundle(
    bundle: PlanBundle,
    duplicate_destinations: set[Path],
    verify_source_hashes: bool = False,
) -> BundleValidation:
    row_diagnostics: dict[int, FileDiagnostic] = {}
    rows = bundle_rows(bundle)
    keeper_validation = validate_keeper(bundle)
    if keeper_validation is not None:
        return BundleValidation(
            result=keeper_validation,
            keeper=FileDiagnostic(hash_check="not_applicable", validation_result=keeper_validation),
            rows=row_diagnostics,
        )
    keeper_diagnostic = FileDiagnostic()
    if verify_source_hashes and bundle.keeper is not None:
        keeper_diagnostic = hash_diagnostic(bundle.keeper.source_path, bundle.keeper.hash_value, "keeper_hash_mismatch")
        if keeper_diagnostic.validation_result != "validated":
            return BundleValidation(result=keeper_diagnostic.validation_result, keeper=keeper_diagnostic, rows=row_diagnostics)
    if any(row.source_path == bundle.keeper.source_path for row in rows if bundle.keeper is not None):
        return BundleValidation(result="duplicate_source_is_keeper", keeper=keeper_diagnostic, rows=row_diagnostics)
    if any(plan_row_has_duplicate_destination(row, duplicate_destinations) for row in rows):
        for row in rows:
            if plan_row_has_duplicate_destination(row, duplicate_destinations):
                row_diagnostics[id(row)] = FileDiagnostic(
                    hash_check="not_applicable",
                    validation_result="duplicate_destination_in_plan",
                )
        return BundleValidation(result="duplicate_destination_in_plan", keeper=keeper_diagnostic, rows=row_diagnostics)

    states = []
    for row in rows:
        state, diagnostic = plan_row_state(row)
        states.append(state)
        row_diagnostics[id(row)] = diagnostic
    if all(state == "already_moved" for state in states):
        if verify_source_hashes:
            for row in rows:
                diagnostic = hash_diagnostic(plan_row_completed_path(row), row.hash_value, "destination_hash_mismatch")
                row_diagnostics[id(row)] = (
                    FileDiagnostic(
                        hash_check=diagnostic.hash_check,
                        observed_hash=diagnostic.observed_hash,
                        validation_result="already_moved",
                    )
                    if diagnostic.validation_result == "validated"
                    else diagnostic
                )
                if diagnostic.validation_result != "validated":
                    return BundleValidation(result=diagnostic.validation_result, keeper=keeper_diagnostic, rows=row_diagnostics)
        else:
            for row in rows:
                row_diagnostics[id(row)] = FileDiagnostic(
                    validation_result="already_moved",
                    action_destination=row_diagnostics[id(row)].action_destination,
                )
        return BundleValidation(result="already_moved", keeper=keeper_diagnostic, rows=row_diagnostics)
    if any(state in {"destination_exists", "merge_destination_hash_mismatch"} for state in states):
        result = "merge_destination_hash_mismatch" if "merge_destination_hash_mismatch" in states else "destination_exists"
        for row, state in zip(rows, states, strict=True):
            if state in {"destination_exists", "merge_destination_hash_mismatch"}:
                row_diagnostics[id(row)] = FileDiagnostic(
                    hash_check=row_diagnostics[id(row)].hash_check,
                    observed_hash=row_diagnostics[id(row)].observed_hash,
                    validation_result=state,
                    action_destination=row_diagnostics[id(row)].action_destination,
                )
        return BundleValidation(result=result, keeper=keeper_diagnostic, rows=row_diagnostics)
    if any(state == "source_size_mismatch" for state in states):
        for row, state in zip(rows, states, strict=True):
            if state == "source_size_mismatch":
                row_diagnostics[id(row)] = FileDiagnostic(hash_check="not_applicable", validation_result="source_size_mismatch")
        return BundleValidation(result="source_size_mismatch", keeper=keeper_diagnostic, rows=row_diagnostics)
    if any(state == "source_missing" for state in states):
        result = "partial_bundle_state" if any(state == "ready" for state in states) or any(state == "already_moved" for state in states) else "source_missing"
        for row, state in zip(rows, states, strict=True):
            if state in {"source_missing", "already_moved"}:
                row_diagnostics[id(row)] = FileDiagnostic(hash_check="not_applicable", validation_result=result)
        return BundleValidation(result=result, keeper=keeper_diagnostic, rows=row_diagnostics)
    if any(state == "already_moved" for state in states):
        for row, state in zip(rows, states, strict=True):
            if state == "already_moved":
                row_diagnostics[id(row)] = FileDiagnostic(hash_check="not_applicable", validation_result="partial_bundle_state")
        return BundleValidation(result="partial_bundle_state", keeper=keeper_diagnostic, rows=row_diagnostics)
    if verify_source_hashes:
        for row in rows:
            diagnostic = hash_diagnostic(row.source_path, row.hash_value, "source_hash_mismatch")
            row_diagnostics[id(row)] = FileDiagnostic(
                hash_check=diagnostic.hash_check,
                observed_hash=diagnostic.observed_hash,
                validation_result=diagnostic.validation_result,
                action_destination=row_diagnostics[id(row)].action_destination,
            )
            if diagnostic.validation_result != "validated":
                return BundleValidation(result=diagnostic.validation_result, keeper=keeper_diagnostic, rows=row_diagnostics)
    return BundleValidation(result=None, keeper=keeper_diagnostic, rows=row_diagnostics)


def validate_keeper(bundle: PlanBundle) -> str | None:
    if bundle.keeper is None:
        return "keeper_missing_from_plan"
    if not bundle.keeper.source_path.exists():
        return "keeper_missing"
    if bundle.keeper.source_path.stat().st_size != bundle.keeper.size_bytes:
        return "keeper_size_mismatch"
    return None


def hash_diagnostic(path: Path, expected_hash: str, mismatch_result: str) -> FileDiagnostic:
    if not expected_hash:
        return FileDiagnostic(hash_check="not_applicable", validation_result=mismatch_result)
    observed_hash = hash_file_xxh128(path)
    if observed_hash != expected_hash:
        return FileDiagnostic(
            hash_check="mismatched",
            observed_hash=observed_hash,
            validation_result=mismatch_result,
        )
    return FileDiagnostic(hash_check="matched", observed_hash=observed_hash)


def plan_row_has_duplicate_destination(row: PlanRow, duplicate_destinations: set[Path]) -> bool:
    return row.destination_path in duplicate_destinations or (
        row.duplicate_output_path is not None and row.duplicate_output_path in duplicate_destinations
    )


def plan_row_state(row: PlanRow) -> tuple[str, FileDiagnostic]:
    if row.disposition == "planned_sidecar_merge":
        return merge_plan_row_state(row)
    return duplicate_plan_row_state(row)


def duplicate_plan_row_state(row: PlanRow) -> tuple[str, FileDiagnostic]:
    source_exists = row.source_path.exists()
    destination_exists = row.destination_path.exists()
    if source_exists and destination_exists:
        return "destination_exists", FileDiagnostic(validation_result="destination_exists")
    if source_exists:
        if row.source_path.stat().st_size != row.size_bytes:
            return "source_size_mismatch", FileDiagnostic(validation_result="source_size_mismatch")
        return "ready", FileDiagnostic(action_destination=row.destination_path)
    if destination_exists:
        if row.destination_path.stat().st_size != row.size_bytes:
            return "destination_exists", FileDiagnostic(validation_result="destination_exists")
        return "already_moved", FileDiagnostic(validation_result="already_moved", action_destination=row.destination_path)
    return "source_missing", FileDiagnostic(validation_result="source_missing")


def merge_plan_row_state(row: PlanRow) -> tuple[str, FileDiagnostic]:
    if row.duplicate_output_path is None:
        return "destination_exists", FileDiagnostic(validation_result="destination_exists")
    source_exists = row.source_path.exists()
    merge_target_exists = row.destination_path.exists()
    duplicate_output_exists = row.duplicate_output_path.exists()
    if merge_target_exists:
        merge_target_diagnostic = existing_hash_diagnostic(
            row.destination_path,
            row.size_bytes,
            row.hash_value,
            "merge_destination_hash_mismatch",
        )
        if merge_target_diagnostic.validation_result != "validated":
            return "merge_destination_hash_mismatch", merge_target_diagnostic
        if source_exists:
            if duplicate_output_exists:
                return "destination_exists", FileDiagnostic(validation_result="destination_exists")
            if row.source_path.stat().st_size != row.size_bytes:
                return "source_size_mismatch", FileDiagnostic(validation_result="source_size_mismatch")
            return "ready", FileDiagnostic(action_destination=row.duplicate_output_path)
        if duplicate_output_exists:
            output_diagnostic = existing_hash_diagnostic(
                row.duplicate_output_path,
                row.size_bytes,
                row.hash_value,
                "destination_hash_mismatch",
            )
            if output_diagnostic.validation_result != "validated":
                return "destination_exists", output_diagnostic
        return "already_moved", FileDiagnostic(validation_result="already_moved", action_destination=row.destination_path)
    if source_exists:
        if row.source_path.stat().st_size != row.size_bytes:
            return "source_size_mismatch", FileDiagnostic(validation_result="source_size_mismatch")
        return "ready", FileDiagnostic(action_destination=row.destination_path)
    return "source_missing", FileDiagnostic(validation_result="source_missing")


def existing_hash_diagnostic(path: Path, expected_size: int, expected_hash: str, mismatch_result: str) -> FileDiagnostic:
    if not path.is_file() or path.stat().st_size != expected_size:
        return FileDiagnostic(hash_check="not_applicable", validation_result=mismatch_result)
    return hash_diagnostic(path, expected_hash, mismatch_result)


def plan_row_completed_path(row: PlanRow) -> Path:
    if row.disposition == "planned_sidecar_merge" and row.destination_path.exists():
        return row.destination_path
    return row.destination_path


def bundle_rows(bundle: PlanBundle) -> tuple[PlanRow, ...]:
    return (bundle.primary, *bundle.sidecars, *bundle.merges)


def validation_with_result(validation: BundleValidation, bundle: PlanBundle, result: str) -> BundleValidation:
    rows = {}
    for row in bundle_rows(bundle):
        diagnostic = validation.rows.get(id(row), FileDiagnostic())
        rows[id(row)] = FileDiagnostic(
            hash_check=diagnostic.hash_check,
            observed_hash=diagnostic.observed_hash,
            validation_result=result,
        )
    return BundleValidation(result=result, keeper=validation.keeper, rows=rows)


def log_keeper(logger: CsvLogger, bundle: PlanBundle, validation: BundleValidation) -> None:
    if bundle.keeper is None:
        return
    diagnostic = validation.keeper or FileDiagnostic()
    keeper_is_error = validation.result in {"keeper_missing", "keeper_size_mismatch", "keeper_hash_mismatch"}
    logger.row(
        disposition="keeper_error" if keeper_is_error else "verified_keeper",
        event="execute_plan_keeper",
        status="error" if keeper_is_error else "kept",
        group_id=bundle.keeper.group_id,
        file_role="keeper",
        input_root=bundle.keeper.row.get("input_root", ""),
        source_path=bundle.keeper.source_path,
        keeper_path=bundle.keeper.source_path,
        size_bytes=bundle.keeper.size_bytes,
        digest=bundle.keeper.hash_value,
        observed_hash=diagnostic.observed_hash,
        hash_check=diagnostic.hash_check,
        validation_result=diagnostic.validation_result,
        action_taken="skipped" if keeper_is_error else "keeper_verified",
        reason=diagnostic.validation_result if keeper_is_error else "keeper_verified",
    )


def log_bundle(
    logger: CsvLogger,
    bundle: PlanBundle,
    validation: BundleValidation,
    *,
    disposition_prefix: str,
    status: str,
    reason: str,
    message: str = "",
) -> None:
    for row in bundle_rows(bundle):
        diagnostic = validation.rows.get(id(row), FileDiagnostic(validation_result=reason if status == "error" else "validated"))
        if disposition_prefix == "planned":
            disposition = row.disposition
            action_taken = "planned"
        elif disposition_prefix == "moved":
            disposition = moved_disposition(row, diagnostic)
            action_taken = "merged" if disposition == "merged_sidecar" else "moved"
        elif disposition_prefix == "already_moved":
            disposition = already_moved_disposition(row)
            action_taken = "already_moved"
        elif disposition_prefix == "skipped_error":
            disposition = "skipped_error_primary" if row.file_role == "primary" else "skipped_error_sidecar"
            action_taken = "skipped"
        else:
            disposition = disposition_prefix
            action_taken = "skipped" if status == "error" else status
        logger.row(
            disposition=disposition,
            event=execute_event(row),
            status=status,
            group_id=row.group_id,
            file_role=row.file_role,
            input_root=row.row.get("input_root", ""),
            primary_source_path=row.primary_source_path or "",
            source_path=row.source_path,
            destination_path=diagnostic.action_destination or row.destination_path,
            duplicate_output_path=row.duplicate_output_path or "",
            keeper_path=row.row.get("keeper_path", ""),
            size_bytes=row.size_bytes,
            digest=row.hash_value,
            observed_hash=diagnostic.observed_hash,
            hash_check=diagnostic.hash_check,
            validation_result=diagnostic.validation_result,
            action_taken=action_taken,
            reason=reason,
            message=message,
        )


def moved_disposition(row: PlanRow, diagnostic: FileDiagnostic) -> str:
    if row.file_role == "primary":
        return "moved_duplicate_primary"
    if row.disposition == "planned_sidecar_merge" and diagnostic.action_destination == row.destination_path:
        return "merged_sidecar"
    return "moved_duplicate_sidecar"


def already_moved_disposition(row: PlanRow) -> str:
    if row.file_role == "primary":
        return "already_moved_duplicate_primary"
    if row.disposition == "planned_sidecar_merge":
        return "already_merged_sidecar"
    return "already_moved_sidecar"


def execute_event(row: PlanRow) -> str:
    if row.file_role == "primary":
        return "execute_plan_move"
    if row.disposition == "planned_sidecar_merge":
        return "execute_plan_sidecar_merge"
    return "execute_plan_sidecar_move"


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
