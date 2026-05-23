from __future__ import annotations

import csv
from pathlib import Path

import pytest

from dedup_photos.cli import default_manifest_output_path, manifest_main
from dedup_photos.constants import MANIFEST_VERSION
from dedup_photos.manifest import MANIFEST_FIELDS, execute_plan, generate_manifest, plan_from_manifests, verify_manifests, verify_move
from tests.helpers import make_conflict_manifests, make_manifest_move_case, make_move_plan, prepare_nas_root, rows, write, write_csv, write_rows


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
    plan_path, _nas_one, nas_two, _output_root = make_move_plan(
        tmp_path,
        with_sidecars=True,
        full_primary_prefix_sidecar=True,
    )

    sidecar = [row for row in rows(plan_path) if row["disposition"] == "planned_sidecar"][0]
    assert sidecar["source_path"] == str(nas_two / "photo.jpg.json")
    assert sidecar["primary_source_path"] == str(nas_two / "photo.jpg")
