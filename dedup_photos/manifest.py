from __future__ import annotations

from dedup_photos.execute import execute_plan
from dedup_photos.hashing import files_equal_by_path, hash_file_xxh128
from dedup_photos.json_analysis import analyze_json_sidecars
from dedup_photos.manifest_io import (
    MANIFEST_FIELDS,
    collapse_duplicate_manifest_paths,
    directory_relative_paths,
    generate_manifest,
    load_manifests,
    manifest_entries_match,
    parse_manifest_inventory_row,
    validate_manifest_roots,
    write_manifest_file_row,
)
from dedup_photos.models import (
    ExecutePlanResult,
    JsonSidecarAnalysisResult,
    ManifestEntry,
    ManifestInventoryRow,
    ManifestPlanResult,
    ManifestVerifyResult,
    PlanBundle,
    PlanRow,
    VerifyMoveResult,
)
from dedup_photos.planning import (
    choose_manifest_keeper,
    log_manifest_duplicate_plan,
    log_manifest_sidecar_conflict,
    manifest_destination_for,
    manifest_duplicate_groups,
    manifest_keeper_key,
    manifest_sidecar_sets_equivalent,
    manifest_sidecar_signature,
    manifest_sort_key,
    plan_from_manifests,
)
from dedup_photos.verify import (
    byte_check_manifest_group,
    verify_manifests,
    verify_move,
    verify_move_groups,
)

__all__ = [
    "ExecutePlanResult",
    "JsonSidecarAnalysisResult",
    "MANIFEST_FIELDS",
    "ManifestEntry",
    "ManifestInventoryRow",
    "ManifestPlanResult",
    "ManifestVerifyResult",
    "PlanBundle",
    "PlanRow",
    "VerifyMoveResult",
    "analyze_json_sidecars",
    "byte_check_manifest_group",
    "choose_manifest_keeper",
    "collapse_duplicate_manifest_paths",
    "directory_relative_paths",
    "execute_plan",
    "files_equal_by_path",
    "generate_manifest",
    "hash_file_xxh128",
    "load_manifests",
    "log_manifest_duplicate_plan",
    "log_manifest_sidecar_conflict",
    "manifest_destination_for",
    "manifest_duplicate_groups",
    "manifest_entries_match",
    "manifest_keeper_key",
    "manifest_sidecar_sets_equivalent",
    "manifest_sidecar_signature",
    "manifest_sort_key",
    "parse_manifest_inventory_row",
    "plan_from_manifests",
    "validate_manifest_roots",
    "verify_manifests",
    "verify_move",
    "verify_move_groups",
    "write_manifest_file_row",
]
