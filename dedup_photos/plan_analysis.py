from __future__ import annotations

import csv
from collections import Counter
from pathlib import Path

from dedup_photos.models import PlanAnalysisResult


DUPLICATE_OUTPUT_DISPOSITIONS = {
    "planned_duplicate_primary",
    "planned_duplicate_sidecar",
    "planned_duplicate_uncategorized",
}
SIDECAR_MERGE_DISPOSITION = "planned_sidecar_merge"


def analyze_plan(plan_path: Path) -> PlanAnalysisResult:
    required_fields = {
        "disposition",
        "status",
        "file_role",
        "destination_path",
        "group_id",
        "input_root",
        "size_bytes",
    }
    duplicate_output_files = 0
    duplicate_output_bytes = 0
    duplicate_output_groups: set[str] = set()
    sidecar_merge_files = 0
    sidecar_merge_bytes = 0
    sidecar_merge_groups: set[str] = set()
    skipped_rows = 0
    invalid_size_rows = 0
    by_file_role: Counter[str] = Counter()
    bytes_by_file_role: Counter[str] = Counter()
    by_disposition: Counter[str] = Counter()
    bytes_by_disposition: Counter[str] = Counter()
    by_input_root: Counter[str] = Counter()
    bytes_by_input_root: Counter[str] = Counter()
    skipped_by_disposition: Counter[str] = Counter()
    duplicate_output_destinations: Counter[str] = Counter()
    sidecar_merge_targets: Counter[str] = Counter()

    with plan_path.open(newline="", encoding="utf-8") as file:
        reader = csv.DictReader(file)
        missing = required_fields - set(reader.fieldnames or [])
        if missing:
            raise ValueError(f"plan missing required fields {sorted(missing)}: {plan_path}")
        for row in reader:
            disposition = row["disposition"]
            size_bytes = parse_size_bytes(row)
            if size_bytes is None:
                invalid_size_rows += 1
                size_bytes = 0
            if disposition in DUPLICATE_OUTPUT_DISPOSITIONS:
                duplicate_output_files += 1
                duplicate_output_bytes += size_bytes
                if row["group_id"]:
                    duplicate_output_groups.add(row["group_id"])
                by_file_role[row["file_role"] or "(blank)"] += 1
                bytes_by_file_role[row["file_role"] or "(blank)"] += size_bytes
                by_disposition[disposition] += 1
                bytes_by_disposition[disposition] += size_bytes
                by_input_root[row["input_root"] or "(blank)"] += 1
                bytes_by_input_root[row["input_root"] or "(blank)"] += size_bytes
                if row["destination_path"]:
                    duplicate_output_destinations[row["destination_path"]] += 1
            elif disposition == SIDECAR_MERGE_DISPOSITION:
                sidecar_merge_files += 1
                sidecar_merge_bytes += size_bytes
                if row["group_id"]:
                    sidecar_merge_groups.add(row["group_id"])
                if row["destination_path"]:
                    sidecar_merge_targets[row["destination_path"]] += 1
            if row["status"] in {"skipped", "error"} or disposition.startswith("kept_sidecar_conflict"):
                skipped_rows += 1
                skipped_by_disposition[disposition] += 1

    return PlanAnalysisResult(
        plan_path=plan_path,
        duplicate_output_files=duplicate_output_files,
        duplicate_output_bytes=duplicate_output_bytes,
        duplicate_output_groups=len(duplicate_output_groups),
        sidecar_merge_files=sidecar_merge_files,
        sidecar_merge_bytes=sidecar_merge_bytes,
        sidecar_merge_groups=len(sidecar_merge_groups),
        skipped_rows=skipped_rows,
        duplicate_output_destination_conflicts=count_conflicting_keys(duplicate_output_destinations),
        sidecar_merge_target_conflicts=count_conflicting_keys(sidecar_merge_targets),
        invalid_size_rows=invalid_size_rows,
        by_file_role=by_file_role,
        bytes_by_file_role=bytes_by_file_role,
        by_disposition=by_disposition,
        bytes_by_disposition=bytes_by_disposition,
        by_input_root=by_input_root,
        bytes_by_input_root=bytes_by_input_root,
        skipped_by_disposition=skipped_by_disposition,
    )


def parse_size_bytes(row: dict[str, str]) -> int | None:
    try:
        size_bytes = int(row["size_bytes"])
    except ValueError:
        return None
    if size_bytes < 0:
        return None
    return size_bytes


def count_conflicting_keys(counter: Counter[str]) -> int:
    return sum(1 for count in counter.values() if count > 1)
