from __future__ import annotations

import csv
from pathlib import Path

import pytest

from dedup_photos.cli import default_manifest_output_path, manifest_main
from dedup_photos.constants import MANIFEST_VERSION
from dedup_photos.manifest import MANIFEST_FIELDS, execute_plan, generate_manifest, plan_from_manifests, verify_manifests, verify_move
from tests.helpers import make_conflict_manifests, make_manifest_move_case, make_move_plan, prepare_nas_root, rows, write, write_csv, write_rows


def test_execute_plan_dry_run_validates_without_moving(tmp_path: Path) -> None:
    plan_path, nas_one, nas_two, output_root = make_move_plan(tmp_path)
    log_path = tmp_path / "execute.csv"

    result = execute_plan(plan_path, log_path, move=False)

    assert result.bundles == 1
    assert result.planned_bundles == 1
    assert (nas_one / "photo.jpg").exists()
    assert (nas_two / "photo.jpg").exists()
    assert not output_root.exists()
    execute_rows = rows(log_path)
    assert execute_rows[0]["disposition"] == "planned_duplicate_primary"
    assert execute_rows[0]["event"] == "execute_plan_move"

def test_execute_plan_move_moves_primary_and_sidecar_bundle(tmp_path: Path) -> None:
    plan_path, nas_one, nas_two, output_root = make_move_plan(tmp_path, with_sidecars=True)
    log_path = tmp_path / "execute.csv"

    result = execute_plan(plan_path, log_path, move=True)

    assert result.moved_bundles == 1
    assert (nas_one / "photo.jpg").exists()
    assert (nas_one / "photo.mov").exists()
    assert not (nas_two / "photo.jpg").exists()
    assert not (nas_two / "photo.mov").exists()
    assert (output_root / "nas-two" / "photo.jpg").read_bytes() == b"same"
    assert (output_root / "nas-two" / "photo.mov").read_bytes() == b"live"
    execute_rows = rows(log_path)
    assert {row["disposition"] for row in execute_rows} == {"moved_duplicate_primary", "moved_sidecar"}

def test_execute_plan_moves_full_primary_prefix_sidecar(tmp_path: Path) -> None:
    plan_path, nas_one, nas_two, output_root = make_move_plan(
        tmp_path,
        with_sidecars=True,
        full_primary_prefix_sidecar=True,
    )
    log_path = tmp_path / "execute.csv"

    result = execute_plan(plan_path, log_path, move=True)

    assert result.moved_bundles == 1
    assert result.orphan_sidecars == 0
    assert (nas_one / "photo.jpg").exists()
    assert (nas_one / "photo.jpg.json").exists()
    assert not (nas_two / "photo.jpg").exists()
    assert not (nas_two / "photo.jpg.json").exists()
    assert (output_root / "nas-two" / "photo.jpg").read_bytes() == b"same"
    assert (output_root / "nas-two" / "photo.jpg.json").read_bytes() == b"live"
    assert "orphan_plan_sidecar" not in {row["disposition"] for row in rows(log_path)}

def test_execute_plan_destination_collision_skips_bundle(tmp_path: Path) -> None:
    plan_path, _nas_one, nas_two, output_root = make_move_plan(tmp_path, with_sidecars=True)
    write(output_root / "nas-two" / "photo.jpg", b"existing")
    log_path = tmp_path / "execute.csv"

    result = execute_plan(plan_path, log_path, move=True)

    assert result.skipped_bundles == 1
    assert (nas_two / "photo.jpg").exists()
    assert (nas_two / "photo.mov").exists()
    assert {row["reason"] for row in rows(log_path)} == {"destination_exists"}

def test_execute_plan_missing_keeper_skips_bundle(tmp_path: Path) -> None:
    plan_path, nas_one, nas_two, _output_root = make_move_plan(tmp_path)
    (nas_one / "photo.jpg").unlink()
    log_path = tmp_path / "execute.csv"

    result = execute_plan(plan_path, log_path, move=True)

    assert result.skipped_bundles == 1
    assert (nas_two / "photo.jpg").exists()
    assert rows(log_path)[0]["reason"] == "keeper_missing"

def test_execute_plan_keeper_size_mismatch_skips_bundle(tmp_path: Path) -> None:
    plan_path, nas_one, nas_two, _output_root = make_move_plan(tmp_path)
    (nas_one / "photo.jpg").write_bytes(b"different size")
    log_path = tmp_path / "execute.csv"

    result = execute_plan(plan_path, log_path, move=True)

    assert result.skipped_bundles == 1
    assert (nas_two / "photo.jpg").exists()
    assert rows(log_path)[0]["reason"] == "keeper_size_mismatch"

def test_execute_plan_missing_keeper_row_skips_bundle(tmp_path: Path) -> None:
    plan_path, _nas_one, nas_two, _output_root = make_move_plan(tmp_path)
    plan_rows = rows(plan_path)
    fieldnames = list(plan_rows[0])
    planned_only = [row for row in plan_rows if row["disposition"] == "planned_duplicate_primary"]
    write_csv(plan_path, fieldnames, planned_only)
    log_path = tmp_path / "execute.csv"

    result = execute_plan(plan_path, log_path, move=True)

    assert result.skipped_bundles == 1
    assert (nas_two / "photo.jpg").exists()
    assert rows(log_path)[0]["reason"] == "keeper_missing_from_plan"

def test_execute_plan_refuses_to_move_keeper_source(tmp_path: Path) -> None:
    plan_path, nas_one, _nas_two, _output_root = make_move_plan(tmp_path)
    plan_rows = rows(plan_path)
    fieldnames = list(plan_rows[0])
    keeper = [row for row in plan_rows if row["disposition"] == "kept_duplicate_keeper"][0]
    duplicate = [row for row in plan_rows if row["disposition"] == "planned_duplicate_primary"][0]
    duplicate["source_path"] = keeper["source_path"]
    duplicate["size_bytes"] = keeper["size_bytes"]
    write_csv(plan_path, fieldnames, plan_rows)
    log_path = tmp_path / "execute.csv"

    result = execute_plan(plan_path, log_path, move=True)

    assert result.skipped_bundles == 1
    assert (nas_one / "photo.jpg").exists()
    assert rows(log_path)[0]["reason"] == "duplicate_source_is_keeper"

def test_execute_plan_already_moved_bundle_is_resumable(tmp_path: Path) -> None:
    plan_path, _nas_one, nas_two, output_root = make_move_plan(tmp_path)
    destination = output_root / "nas-two" / "photo.jpg"
    destination.parent.mkdir(parents=True)
    destination.write_bytes((nas_two / "photo.jpg").read_bytes())
    (nas_two / "photo.jpg").unlink()
    log_path = tmp_path / "execute.csv"

    result = execute_plan(plan_path, log_path, move=True)

    assert result.already_moved_bundles == 1
    execute_rows = rows(log_path)
    assert execute_rows[0]["disposition"] == "already_moved_duplicate_primary"
    assert execute_rows[0]["reason"] == "already_moved"

def test_execute_plan_missing_source_without_destination_skips_bundle(tmp_path: Path) -> None:
    plan_path, _nas_one, nas_two, _output_root = make_move_plan(tmp_path)
    (nas_two / "photo.jpg").unlink()
    log_path = tmp_path / "execute.csv"

    result = execute_plan(plan_path, log_path, move=True)

    assert result.skipped_bundles == 1
    assert rows(log_path)[0]["reason"] == "source_missing"

def test_execute_plan_source_size_mismatch_skips_bundle(tmp_path: Path) -> None:
    plan_path, _nas_one, nas_two, _output_root = make_move_plan(tmp_path)
    (nas_two / "photo.jpg").write_bytes(b"different size")
    log_path = tmp_path / "execute.csv"

    result = execute_plan(plan_path, log_path, move=True)

    assert result.skipped_bundles == 1
    assert rows(log_path)[0]["reason"] == "source_size_mismatch"

def test_execute_plan_default_does_not_hash_validate_same_size_source_change(tmp_path: Path) -> None:
    plan_path, _nas_one, nas_two, output_root = make_move_plan(tmp_path)
    (nas_two / "photo.jpg").write_bytes(b"diff")
    log_path = tmp_path / "execute.csv"

    result = execute_plan(plan_path, log_path, move=True)

    assert result.moved_bundles == 1
    assert not (nas_two / "photo.jpg").exists()
    assert (output_root / "nas-two" / "photo.jpg").read_bytes() == b"diff"

def test_execute_plan_hash_validation_catches_same_size_source_change(tmp_path: Path) -> None:
    plan_path, _nas_one, nas_two, output_root = make_move_plan(tmp_path)
    (nas_two / "photo.jpg").write_bytes(b"diff")
    log_path = tmp_path / "execute.csv"

    result = execute_plan(plan_path, log_path, move=True, verify_source_hashes=True)

    assert result.skipped_bundles == 1
    assert result.moved_bundles == 0
    assert (nas_two / "photo.jpg").exists()
    assert not (output_root / "nas-two" / "photo.jpg").exists()
    assert rows(log_path)[0]["reason"] == "source_hash_mismatch"

def test_execute_plan_hash_validation_catches_same_size_keeper_change(tmp_path: Path) -> None:
    plan_path, nas_one, nas_two, output_root = make_move_plan(tmp_path)
    (nas_one / "photo.jpg").write_bytes(b"diff")
    log_path = tmp_path / "execute.csv"

    result = execute_plan(plan_path, log_path, move=True, verify_source_hashes=True)

    assert result.skipped_bundles == 1
    assert result.moved_bundles == 0
    assert (nas_one / "photo.jpg").exists()
    assert (nas_two / "photo.jpg").exists()
    assert not (output_root / "nas-two" / "photo.jpg").exists()
    assert rows(log_path)[0]["reason"] == "source_hash_mismatch"

def test_execute_plan_partial_already_moved_bundle_skips_bundle(tmp_path: Path) -> None:
    plan_path, _nas_one, nas_two, output_root = make_move_plan(tmp_path, with_sidecars=True)
    destination = output_root / "nas-two" / "photo.jpg"
    destination.parent.mkdir(parents=True)
    destination.write_bytes((nas_two / "photo.jpg").read_bytes())
    (nas_two / "photo.jpg").unlink()
    log_path = tmp_path / "execute.csv"

    result = execute_plan(plan_path, log_path, move=True)

    assert result.skipped_bundles == 1
    assert (nas_two / "photo.mov").exists()
    assert {row["reason"] for row in rows(log_path)} == {"partial_bundle_state"}

def test_execute_plan_orphan_sidecar_is_logged_and_ignored(tmp_path: Path) -> None:
    plan_path, _nas_one, _nas_two, _output_root = make_move_plan(tmp_path, with_sidecars=True)
    plan_rows = rows(plan_path)
    fieldnames = list(plan_rows[0])
    sidecar_only = [row for row in plan_rows if row["disposition"] == "planned_sidecar"]
    write_csv(plan_path, fieldnames, sidecar_only)
    log_path = tmp_path / "execute.csv"

    result = execute_plan(plan_path, log_path, move=True)

    assert result.bundles == 0
    assert result.orphan_sidecars == 1
    assert rows(log_path)[0]["disposition"] == "orphan_plan_sidecar"

def test_execute_plan_duplicate_destination_skips_affected_bundles(tmp_path: Path) -> None:
    plan_path = tmp_path / "plan.csv"
    first_plan, _one_a, _two_a, _out_a = make_move_plan(tmp_path / "a")
    second_plan, _one_b, _two_b, _out_b = make_move_plan(tmp_path / "b")
    first_rows = rows(first_plan)
    second_rows = rows(second_plan)
    fieldnames = list(first_rows[0])
    first_move = [row for row in first_rows if row["disposition"] == "planned_duplicate_primary"][0]
    second_move = [row for row in second_rows if row["disposition"] == "planned_duplicate_primary"][0]
    first_keeper = [row for row in first_rows if row["disposition"] == "kept_duplicate_keeper"][0]
    second_keeper = [row for row in second_rows if row["disposition"] == "kept_duplicate_keeper"][0]
    second_keeper["group_id"] = "m000002"
    second_move["group_id"] = "m000002"
    second_move["destination_path"] = first_move["destination_path"]
    write_csv(plan_path, fieldnames, [first_keeper, first_move, second_keeper, second_move])
    log_path = tmp_path / "execute.csv"

    result = execute_plan(plan_path, log_path, move=True)

    assert result.skipped_bundles == 2
    assert {row["reason"] for row in rows(log_path)} == {"duplicate_destination_in_plan"}

def test_execute_plan_ignores_non_planned_rows(tmp_path: Path) -> None:
    plan_path, _nas_one, _nas_two, _output_root = make_move_plan(tmp_path)
    plan_rows = rows(plan_path)
    fieldnames = list(plan_rows[0])
    non_planned = [row for row in plan_rows if row["event"] == "keeper_primary"]
    write_csv(plan_path, fieldnames, non_planned)
    log_path = tmp_path / "execute.csv"

    result = execute_plan(plan_path, log_path, move=True)

    assert result.bundles == 0
    assert rows(log_path) == []
