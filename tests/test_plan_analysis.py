from __future__ import annotations

from pathlib import Path

from dedup_photos.plan_analysis import analyze_plan
from tests.helpers import write_csv


PLAN_FIELDS = [
    "disposition",
    "status",
    "file_role",
    "source_path",
    "destination_path",
    "duplicate_output_path",
    "keeper_path",
    "primary_source_path",
    "group_id",
    "event",
    "reason",
    "message",
    "size_bytes",
    "xxh128",
    "input_root",
]


def test_analyze_plan_counts_duplicate_output_moves_separately_from_merges(tmp_path: Path) -> None:
    plan_path = tmp_path / "plan.csv"
    write_csv(
        plan_path,
        PLAN_FIELDS,
        [
            plan_row(
                disposition="planned_duplicate_primary",
                file_role="primary",
                group_id="m000001",
                input_root="takeout",
                destination_path="/dupes/takeout/a.jpg",
                size_bytes="100",
            ),
            plan_row(
                disposition="planned_duplicate_sidecar",
                file_role="sidecar",
                group_id="m000001",
                input_root="takeout",
                destination_path="/dupes/takeout/a.mov",
                size_bytes="40",
            ),
            plan_row(
                disposition="planned_duplicate_uncategorized",
                file_role="uncategorized",
                group_id="u000001",
                input_root="mobilebackup",
                destination_path="/dupes/mobilebackup/video.mov",
                size_bytes="200",
            ),
            plan_row(
                disposition="planned_duplicate_primary",
                file_role="primary",
                group_id="m000002",
                input_root="takeout",
                destination_path="/dupes/takeout/a.jpg",
                size_bytes="25",
            ),
            plan_row(
                disposition="planned_duplicate_primary",
                file_role="primary",
                group_id="m000003",
                input_root="takeout",
                destination_path="/dupes/takeout/b.jpg",
                size_bytes="not-an-int",
            ),
            plan_row(
                disposition="planned_sidecar_merge",
                file_role="sidecar",
                group_id="m000004",
                input_root="mobilebackup",
                destination_path="/keepers/photo.mov",
                duplicate_output_path="/dupes/mobilebackup/photo.mov",
                size_bytes="50",
            ),
            plan_row(
                disposition="planned_sidecar_merge",
                file_role="sidecar",
                group_id="m000005",
                input_root="mobilebackup",
                destination_path="/keepers/photo.mov",
                duplicate_output_path="/dupes/mobilebackup/other.mov",
                size_bytes="60",
            ),
            plan_row(
                disposition="kept_sidecar_conflict",
                status="skipped",
                file_role="primary",
                group_id="m000006",
                input_root="mobilebackup",
                size_bytes="10",
            ),
        ],
    )

    result = analyze_plan(plan_path)

    assert result.duplicate_output_files == 5
    assert result.duplicate_output_bytes == 365
    assert result.duplicate_output_groups == 4
    assert result.sidecar_merge_files == 2
    assert result.sidecar_merge_bytes == 110
    assert result.sidecar_merge_groups == 2
    assert result.skipped_rows == 1
    assert result.duplicate_output_destination_conflicts == 1
    assert result.sidecar_merge_target_conflicts == 1
    assert result.invalid_size_rows == 1
    assert result.by_file_role["primary"] == 3
    assert result.bytes_by_file_role["primary"] == 125
    assert result.by_file_role["sidecar"] == 1
    assert result.by_file_role["uncategorized"] == 1
    assert result.by_disposition["planned_duplicate_primary"] == 3
    assert result.by_input_root["takeout"] == 4
    assert result.bytes_by_input_root["takeout"] == 165
    assert result.skipped_by_disposition["kept_sidecar_conflict"] == 1


def plan_row(
    *,
    disposition: str,
    file_role: str,
    group_id: str,
    input_root: str,
    size_bytes: str,
    status: str = "planned",
    destination_path: str = "",
    duplicate_output_path: str = "",
) -> dict[str, str]:
    return {
        "disposition": disposition,
        "status": status,
        "file_role": file_role,
        "source_path": f"/source/{group_id}",
        "destination_path": destination_path,
        "duplicate_output_path": duplicate_output_path,
        "keeper_path": "",
        "primary_source_path": "",
        "group_id": group_id,
        "event": "",
        "reason": "",
        "message": "",
        "size_bytes": size_bytes,
        "xxh128": "",
        "input_root": input_root,
    }
