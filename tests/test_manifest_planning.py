from __future__ import annotations

from pathlib import Path

import pytest

from dedup_photos.manifest import generate_manifest, plan_from_manifests
from tests.helpers import make_move_case, make_sidecar_merge_case, prepare_nas_root, rows, write


def test_plan_from_manifests_uses_sidecar_precedence_and_nas_destinations(tmp_path: Path) -> None:
    local_one = tmp_path / "google photos"
    local_two = tmp_path / "backups"
    nas_parent = tmp_path / "nas"
    manifest_one = tmp_path / "google.csv"
    manifest_two = tmp_path / "backups.csv"
    log_path = tmp_path / "plan.csv"
    output_root = tmp_path / "dupes"
    write(local_one / "photo.jpg", b"same")
    write(local_two / "photo.jpg", b"same")
    write(local_two / "photo.mov", b"live")
    nas_one = prepare_nas_root(local_one, nas_parent)
    nas_two = prepare_nas_root(local_two, nas_parent)
    generate_manifest(local_one, nas_one, manifest_one)
    generate_manifest(local_two, nas_two, manifest_two)

    result = plan_from_manifests([manifest_one, manifest_two], output_root, log_path)

    assert result.duplicate_groups == 1
    assert result.duplicate_files == 1
    plan_rows = rows(log_path)
    keeper = [row for row in plan_rows if row["event"] == "keeper_primary"][0]
    move = [row for row in plan_rows if row["event"] == "duplicate_primary_move"][0]
    assert keeper["source_path"] == str(nas_two / "photo.jpg")
    assert move["source_path"] == str(nas_one / "photo.jpg")
    assert move["destination_path"] == str(output_root / "google photos" / "photo.jpg")
    assert move["xxh128"]

def test_plan_from_manifests_skips_sidecar_conflicts(tmp_path: Path) -> None:
    local_one = tmp_path / "one"
    local_two = tmp_path / "two"
    manifest_one = tmp_path / "one.csv"
    manifest_two = tmp_path / "two.csv"
    log_path = tmp_path / "plan.csv"
    write(local_one / "photo.jpg", b"same")
    write(local_one / "photo.json", b"left")
    write(local_two / "photo.jpg", b"same")
    write(local_two / "photo.json", b"right")
    nas_one = prepare_nas_root(local_one, tmp_path / "nas")
    nas_two = prepare_nas_root(local_two, tmp_path / "nas")
    generate_manifest(local_one, nas_one, manifest_one)
    generate_manifest(local_two, nas_two, manifest_two)

    result = plan_from_manifests([manifest_one, manifest_two], Path("/dupes"), log_path)

    assert result.skipped_groups == 1
    plan_rows = rows(log_path)
    conflict_rows = [row for row in plan_rows if row["disposition"] == "kept_sidecar_conflict"]
    assert len(conflict_rows) == 2
    assert {row["reason"] for row in conflict_rows} == {"unresolved_sidecar_conflict"}
    conflict_sidecar_rows = [row for row in plan_rows if row["disposition"] == "kept_sidecar_conflict_sidecar"]
    assert len(conflict_sidecar_rows) == 2
    assert {row["file_role"] for row in conflict_sidecar_rows} == {"sidecar"}
    assert {Path(row["source_path"]).name for row in conflict_sidecar_rows} == {"photo.json"}
    assert all(row["xxh128"] for row in conflict_sidecar_rows)
    assert all(row["source_path"] for row in plan_rows)
    assert not [row for row in plan_rows if row["event"] == "duplicate_group_skipped"]
    assert not [row for row in plan_rows if row["event"] == "duplicate_primary_move"]


def test_plan_from_manifests_skips_three_copy_sidecar_conflict_group(tmp_path: Path) -> None:
    local_one = tmp_path / "one"
    local_two = tmp_path / "two"
    local_three = tmp_path / "three"
    manifests = [tmp_path / "one.csv", tmp_path / "two.csv", tmp_path / "three.csv"]
    log_path = tmp_path / "plan.csv"
    write(local_one / "photo.jpg", b"same")
    write(local_one / "photo.mov", b"left")
    write(local_two / "photo.jpg", b"same")
    write(local_two / "photo.mov", b"right")
    write(local_three / "photo.jpg", b"same")
    nas_one = prepare_nas_root(local_one, tmp_path / "nas")
    nas_two = prepare_nas_root(local_two, tmp_path / "nas")
    nas_three = prepare_nas_root(local_three, tmp_path / "nas")
    generate_manifest(local_one, nas_one, manifests[0])
    generate_manifest(local_two, nas_two, manifests[1])
    generate_manifest(local_three, nas_three, manifests[2])

    result = plan_from_manifests(manifests, Path("/dupes"), log_path)

    plan_rows = rows(log_path)
    assert result.skipped_groups == 1
    assert result.duplicate_files == 0
    assert len([row for row in plan_rows if row["disposition"] == "kept_sidecar_conflict"]) == 3
    assert len([row for row in plan_rows if row["disposition"] == "kept_sidecar_conflict_sidecar"]) == 2
    assert not [row for row in plan_rows if row["event"] == "duplicate_primary_move"]


def test_plan_from_manifests_resolves_sidecar_superset_without_conflict(tmp_path: Path) -> None:
    local_one = tmp_path / "one"
    local_two = tmp_path / "two"
    manifest_one = tmp_path / "one.csv"
    manifest_two = tmp_path / "two.csv"
    log_path = tmp_path / "plan.csv"
    write(local_one / "foo.jpg", b"same image")
    write(local_one / "foo.mov", b"same video")
    write(local_one / "foo.jpg.json", b"extra metadata")
    write(local_two / "bar.jpg", b"same image")
    write(local_two / "bar.mp4", b"same video")
    nas_one = prepare_nas_root(local_one, tmp_path / "nas")
    nas_two = prepare_nas_root(local_two, tmp_path / "nas")
    generate_manifest(local_one, nas_one, manifest_one)
    generate_manifest(local_two, nas_two, manifest_two)

    result = plan_from_manifests([manifest_one, manifest_two], Path("/dupes"), log_path)

    plan_rows = rows(log_path)
    keeper = [row for row in plan_rows if row["event"] == "keeper_primary"][0]
    move = [row for row in plan_rows if row["event"] == "duplicate_primary_move"][0]
    sidecar_move = [row for row in plan_rows if row["event"] == "sidecar_move"][0]
    assert result.skipped_groups == 0
    assert result.duplicate_files == 1
    assert keeper["source_path"] == str(nas_one / "foo.jpg")
    assert move["source_path"] == str(nas_two / "bar.jpg")
    assert sidecar_move["source_path"] == str(nas_two / "bar.mp4")
    assert not [row for row in plan_rows if row["disposition"] == "kept_sidecar_conflict"]


def test_plan_from_manifests_merges_complementary_sidecar_classes(tmp_path: Path) -> None:
    case = make_sidecar_merge_case(tmp_path)

    plan_rows = rows(case.plan_path)
    keeper = [row for row in plan_rows if row["event"] == "keeper_primary"][0]
    primary_move = [row for row in plan_rows if row["disposition"] == "planned_duplicate_primary"][0]
    merge = [row for row in plan_rows if row["disposition"] == "planned_sidecar_merge"][0]
    assert keeper["source_path"] == str(case.nas_one / "foo.jpg")
    assert primary_move["source_path"] == str(case.nas_two / "bar.jpg")
    assert merge["source_path"] == str(case.nas_two / "bar.jpg.json")
    assert merge["destination_path"] == str(case.nas_one / "foo.jpg.json")
    assert merge["duplicate_output_path"] == str(case.output_root / "nas-two" / "bar.jpg.json")


@pytest.mark.parametrize(
    ("sidecar_name", "expected_name"),
    [
        ("bar.mp4", "foo.mp4"),
        ("bar.mov", "foo.mov"),
        ("bar.json", "foo.json"),
        ("bar.jpg.json", "foo.jpg.json"),
    ],
)
def test_plan_from_manifests_names_merged_sidecars_from_keeper_primary(
    tmp_path: Path,
    sidecar_name: str,
    expected_name: str,
) -> None:
    local_one = tmp_path / "one"
    local_two = tmp_path / "two"
    manifest_one = tmp_path / "one.csv"
    manifest_two = tmp_path / "two.csv"
    plan_path = tmp_path / "plan.csv"
    write(local_one / "foo.jpg", b"same")
    if Path(sidecar_name).suffix.lower() in {".mov", ".mp4"}:
        write(local_one / "foo.json", b"keeper metadata")
    else:
        write(local_one / "foo.mov", b"keeper video")
    write(local_two / "bar.jpg", b"same")
    write(local_two / sidecar_name, b"sidecar")
    nas_one = prepare_nas_root(local_one, tmp_path / "nas")
    nas_two = prepare_nas_root(local_two, tmp_path / "nas")
    generate_manifest(local_one, nas_one, manifest_one)
    generate_manifest(local_two, nas_two, manifest_two)

    plan_from_manifests([manifest_one, manifest_two], tmp_path / "dupes", plan_path)

    merge = [row for row in rows(plan_path) if row["disposition"] == "planned_sidecar_merge"][0]
    assert Path(merge["destination_path"]).name == expected_name


def test_plan_from_manifests_skips_same_class_sidecar_mismatch(tmp_path: Path) -> None:
    local_one = tmp_path / "one"
    local_two = tmp_path / "two"
    manifest_one = tmp_path / "one.csv"
    manifest_two = tmp_path / "two.csv"
    log_path = tmp_path / "plan.csv"
    write(local_one / "foo.jpg", b"same")
    write(local_one / "foo.mov", b"left video")
    write(local_two / "bar.jpg", b"same")
    write(local_two / "bar.mp4", b"right video")
    nas_one = prepare_nas_root(local_one, tmp_path / "nas")
    nas_two = prepare_nas_root(local_two, tmp_path / "nas")
    generate_manifest(local_one, nas_one, manifest_one)
    generate_manifest(local_two, nas_two, manifest_two)

    result = plan_from_manifests([manifest_one, manifest_two], Path("/dupes"), log_path)

    assert result.skipped_groups == 1
    assert not [row for row in rows(log_path) if row["event"] == "duplicate_primary_move"]


def test_plan_from_manifests_skips_unsupported_sidecar_merge_without_superset(tmp_path: Path) -> None:
    local_one = tmp_path / "one"
    local_two = tmp_path / "two"
    manifest_one = tmp_path / "one.csv"
    manifest_two = tmp_path / "two.csv"
    log_path = tmp_path / "plan.csv"
    write(local_one / "foo.jpg", b"same")
    write(local_one / "foo.mov", b"video")
    write(local_two / "bar.jpg", b"same")
    write(local_two / "bar.xmp", b"metadata")
    nas_one = prepare_nas_root(local_one, tmp_path / "nas")
    nas_two = prepare_nas_root(local_two, tmp_path / "nas")
    generate_manifest(local_one, nas_one, manifest_one)
    generate_manifest(local_two, nas_two, manifest_two)

    result = plan_from_manifests([manifest_one, manifest_two], Path("/dupes"), log_path)

    assert result.skipped_groups == 1
    assert not [row for row in rows(log_path) if row["event"] == "duplicate_primary_move"]


def test_same_size_different_hashes_remain_unique(tmp_path: Path) -> None:
    local_one = tmp_path / "one"
    local_two = tmp_path / "two"
    manifest_one = tmp_path / "one.csv"
    manifest_two = tmp_path / "two.csv"
    log_path = tmp_path / "plan.csv"
    write(local_one / "a.jpg", b"aaaa")
    write(local_two / "b.jpg", b"bbbb")
    nas_one = prepare_nas_root(local_one, tmp_path / "nas")
    nas_two = prepare_nas_root(local_two, tmp_path / "nas")
    generate_manifest(local_one, nas_one, manifest_one)
    generate_manifest(local_two, nas_two, manifest_two)

    result = plan_from_manifests([manifest_one, manifest_two], Path("/dupes"), log_path)

    assert result.duplicate_groups == 0
    assert result.duplicate_files == 0
    assert len([row for row in rows(log_path) if row["event"] == "unique_primary"]) == 2

def test_duplicate_identical_nas_paths_are_collapsed(tmp_path: Path) -> None:
    local_root = tmp_path / "photos"
    nas_root = tmp_path / "nas" / "photos"
    manifest_path = tmp_path / "manifest.csv"
    log_path = tmp_path / "plan.csv"
    write(local_root / "photo.jpg", b"only-copy")
    prepare_nas_root(local_root, tmp_path / "nas")
    generate_manifest(local_root, nas_root, manifest_path)

    result = plan_from_manifests([manifest_path, manifest_path], Path("/dupes"), log_path)

    assert result.duplicate_groups == 0
    assert len([row for row in rows(log_path) if row["event"] == "unique_primary"]) == 1

def test_conflicting_duplicate_nas_paths_are_rejected(tmp_path: Path) -> None:
    local_one = tmp_path / "batch-one" / "photos"
    local_two = tmp_path / "batch-two" / "photos"
    nas_root = tmp_path / "nas" / "photos"
    manifest_one = tmp_path / "one.csv"
    manifest_two = tmp_path / "two.csv"
    write(local_one / "photo.jpg", b"left")
    write(local_two / "photo.jpg", b"right")
    prepare_nas_root(local_one, tmp_path / "nas")
    generate_manifest(local_one, nas_root, manifest_one)
    generate_manifest(local_two, nas_root, manifest_two)

    with pytest.raises(ValueError, match="conflicting duplicate nas_path"):
        plan_from_manifests([manifest_one, manifest_two], Path("/dupes"), tmp_path / "plan.csv")

def test_plan_from_manifests_handles_multiple_duplicate_groups(tmp_path: Path) -> None:
    local_one = tmp_path / "one"
    local_two = tmp_path / "two"
    manifest_one = tmp_path / "one.csv"
    manifest_two = tmp_path / "two.csv"
    log_path = tmp_path / "plan.csv"
    write(local_one / "a.jpg", b"same-a")
    write(local_two / "a.jpg", b"same-a")
    write(local_one / "b.jpg", b"same-b")
    write(local_two / "b.jpg", b"same-b")
    nas_one = prepare_nas_root(local_one, tmp_path / "nas")
    nas_two = prepare_nas_root(local_two, tmp_path / "nas")
    generate_manifest(local_one, nas_one, manifest_one)
    generate_manifest(local_two, nas_two, manifest_two)

    result = plan_from_manifests([manifest_one, manifest_two], Path("/dupes"), log_path)

    assert result.duplicate_groups == 2
    assert result.duplicate_files == 2
    assert len([row for row in rows(log_path) if row["event"] == "duplicate_primary_move"]) == 2

def test_plan_from_manifests_emits_sidecar_move_destinations(tmp_path: Path) -> None:
    local_one = tmp_path / "one"
    local_two = tmp_path / "two"
    manifest_one = tmp_path / "one.csv"
    manifest_two = tmp_path / "two.csv"
    log_path = tmp_path / "plan.csv"
    output_root = Path("/dupes")
    write(local_one / "a" / "photo.jpg", b"same")
    write(local_one / "a" / "photo.mov", b"live")
    write(local_two / "a" / "photo.jpg", b"same")
    write(local_two / "a" / "photo.mov", b"live")
    nas_one = prepare_nas_root(local_one, tmp_path / "nas")
    nas_two = prepare_nas_root(local_two, tmp_path / "nas")
    generate_manifest(local_one, nas_one, manifest_one)
    generate_manifest(local_two, nas_two, manifest_two)

    plan_from_manifests([manifest_one, manifest_two], output_root, log_path)

    sidecar_rows = [row for row in rows(log_path) if row["event"] == "sidecar_move"]
    assert len(sidecar_rows) == 1
    assert sidecar_rows[0]["source_path"] == str(nas_two / "a" / "photo.mov")
    assert sidecar_rows[0]["destination_path"] == "/dupes/two/a/photo.mov"
    assert sidecar_rows[0]["xxh128"]

def test_plan_from_manifests_prefers_takeout_album_over_date_folder(tmp_path: Path) -> None:
    local_one = tmp_path / "Trip to Turkey"
    local_two = tmp_path / "Photos from 2023"
    local_three = tmp_path / "mobilebackup"
    manifests = [tmp_path / "one.csv", tmp_path / "two.csv", tmp_path / "three.csv"]
    log_path = tmp_path / "plan.csv"
    write(local_one / "photo.jpg", b"same")
    write(local_two / "photo.jpg", b"same")
    write(local_three / "photo.jpg", b"same")
    nas_one = prepare_nas_root(local_one, tmp_path / "nas" / "takeout")
    nas_two = prepare_nas_root(local_two, tmp_path / "nas" / "takeout" / "20240101")
    nas_three = prepare_nas_root(local_three, tmp_path / "nas")
    generate_manifest(local_one, nas_one, manifests[0])
    generate_manifest(local_two, nas_two, manifests[1])
    generate_manifest(local_three, nas_three, manifests[2])

    plan_from_manifests(manifests, Path("/dupes"), log_path)

    keeper = [row for row in rows(log_path) if row["event"] == "keeper_primary"][0]
    assert keeper["source_path"] == str(nas_one / "photo.jpg")


def test_plan_from_manifests_prefers_takeout_date_folder_over_mobilebackup(tmp_path: Path) -> None:
    local_takeout = tmp_path / "Photos from 2021"
    local_mobilebackup = tmp_path / "mobilebackup"
    manifests = [tmp_path / "takeout.csv", tmp_path / "mobilebackup.csv"]
    log_path = tmp_path / "plan.csv"
    write(local_takeout / "photo.jpg", b"same")
    write(local_mobilebackup / "DCIM" / "photo.jpg", b"same")
    nas_takeout = prepare_nas_root(local_takeout, tmp_path / "nas" / "takeout")
    nas_mobilebackup = prepare_nas_root(local_mobilebackup, tmp_path / "nas")
    generate_manifest(local_takeout, nas_takeout, manifests[0])
    generate_manifest(local_mobilebackup, nas_mobilebackup, manifests[1])

    plan_from_manifests(manifests, Path("/dupes"), log_path)

    keeper = [row for row in rows(log_path) if row["event"] == "keeper_primary"][0]
    assert keeper["source_path"] == str(nas_takeout / "photo.jpg")


def test_equivalent_sidecars_on_both_sides_allow_planning(tmp_path: Path) -> None:
    local_one = tmp_path / "one"
    local_two = tmp_path / "two"
    manifest_one = tmp_path / "one.csv"
    manifest_two = tmp_path / "two.csv"
    log_path = tmp_path / "plan.csv"
    write(local_one / "photo.jpg", b"same")
    write(local_one / "photo.mov", b"video")
    write(local_two / "photo.jpg", b"same")
    write(local_two / "photo.mp4", b"video")
    nas_one = prepare_nas_root(local_one, tmp_path / "nas")
    nas_two = prepare_nas_root(local_two, tmp_path / "nas")
    generate_manifest(local_one, nas_one, manifest_one)
    generate_manifest(local_two, nas_two, manifest_two)

    result = plan_from_manifests([manifest_one, manifest_two], Path("/dupes"), log_path)

    assert result.skipped_groups == 0
    assert len([row for row in rows(log_path) if row["event"] == "duplicate_primary_move"]) == 1
    sidecar_move = [row for row in rows(log_path) if row["event"] == "sidecar_move"][0]
    assert sidecar_move["source_path"] == str(nas_two / "photo.mp4")

def test_plan_sidecar_rows_include_primary_source_path(tmp_path: Path) -> None:
    case = make_move_case(
        tmp_path,
        with_sidecars=True,
        full_primary_prefix_sidecar=True,
    )

    sidecar = [row for row in rows(case.plan_path) if row["disposition"] == "planned_duplicate_sidecar"][0]
    assert sidecar["source_path"] == str(case.nas_two / "photo.jpg.json")
    assert sidecar["primary_source_path"] == str(case.nas_two / "photo.jpg")
