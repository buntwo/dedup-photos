from __future__ import annotations

from pathlib import Path

import pytest

import dedup_photos.execute as execute_module
from dedup_photos.manifest import execute_plan
from tests.helpers import make_move_case, make_sidecar_merge_case, rows, write, write_csv


def execute_rows_without_keeper(log_path: Path) -> list[dict[str, str]]:
    return [row for row in rows(log_path) if row["file_role"] != "keeper"]


def keeper_rows(log_path: Path) -> list[dict[str, str]]:
    return [row for row in rows(log_path) if row["file_role"] == "keeper"]


def test_execute_plan_dry_run_validates_without_moving(tmp_path: Path) -> None:
    case = make_move_case(tmp_path)
    log_path = tmp_path / "execute.csv"

    result = execute_plan(case.plan_path, log_path, move=False)

    assert result.bundles == 1
    assert result.planned_bundles == 1
    assert (case.nas_one / "photo.jpg").exists()
    assert (case.nas_two / "photo.jpg").exists()
    assert not case.output_root.exists()
    assert keeper_rows(log_path)[0]["disposition"] == "verified_keeper"
    execute_rows = execute_rows_without_keeper(log_path)
    assert execute_rows[0]["disposition"] == "planned_duplicate_primary"
    assert execute_rows[0]["event"] == "execute_plan_move"
    assert execute_rows[0]["action_taken"] == "planned"
    assert execute_rows[0]["hash_check"] == "not_checked"

def test_execute_plan_dry_run_does_not_hash_validate_by_default(tmp_path: Path) -> None:
    case = make_move_case(tmp_path)
    (case.nas_two / "photo.jpg").write_bytes(b"diff")
    log_path = tmp_path / "execute.csv"

    result = execute_plan(case.plan_path, log_path, move=False)

    assert result.planned_bundles == 1
    assert (case.nas_two / "photo.jpg").read_bytes() == b"diff"
    assert execute_rows_without_keeper(log_path)[0]["disposition"] == "planned_duplicate_primary"

def test_execute_plan_dry_run_can_force_hash_validation(tmp_path: Path) -> None:
    case = make_move_case(tmp_path)
    (case.nas_two / "photo.jpg").write_bytes(b"diff")
    log_path = tmp_path / "execute.csv"

    result = execute_plan(case.plan_path, log_path, move=False, verify_source_hashes=True)

    assert result.skipped_bundles == 1
    assert (case.nas_two / "photo.jpg").read_bytes() == b"diff"
    failure = execute_rows_without_keeper(log_path)[0]
    assert failure["reason"] == "source_hash_mismatch"
    assert failure["validation_result"] == "source_hash_mismatch"
    assert failure["hash_check"] == "mismatched"
    assert failure["observed_hash"]

def test_execute_plan_move_moves_primary_and_sidecar_bundle(tmp_path: Path) -> None:
    case = make_move_case(tmp_path, with_sidecars=True)
    log_path = tmp_path / "execute.csv"

    result = execute_plan(case.plan_path, log_path, move=True)

    assert result.moved_bundles == 1
    assert (case.nas_one / "photo.jpg").exists()
    assert (case.nas_one / "photo.mov").exists()
    assert not (case.nas_two / "photo.jpg").exists()
    assert not (case.nas_two / "photo.mov").exists()
    assert (case.output_root / "nas-two" / "photo.jpg").read_bytes() == b"same"
    assert (case.output_root / "nas-two" / "photo.mov").read_bytes() == b"live"
    execute_rows = rows(log_path)
    assert {row["disposition"] for row in execute_rows} == {
        "verified_keeper",
        "moved_duplicate_primary",
        "moved_duplicate_sidecar",
    }
    moved_rows = execute_rows_without_keeper(log_path)
    assert {row["action_taken"] for row in moved_rows} == {"moved"}
    assert {row["hash_check"] for row in moved_rows} == {"matched"}


def test_execute_plan_rolls_back_primary_when_sidecar_move_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = make_move_case(tmp_path, with_sidecars=True)
    log_path = tmp_path / "execute.csv"
    original_move = execute_module.shutil.move
    move_calls: list[tuple[Path, Path]] = []

    def fail_second_move(source: str, destination: str) -> str:
        move_calls.append((Path(source), Path(destination)))
        if len(move_calls) == 2:
            raise OSError("forced sidecar move failure")
        return original_move(source, destination)

    monkeypatch.setattr(execute_module.shutil, "move", fail_second_move)

    result = execute_plan(case.plan_path, log_path, move=True)

    assert result.moved_bundles == 0
    assert result.skipped_bundles == 1
    assert (case.nas_two / "photo.jpg").read_bytes() == b"same"
    assert (case.nas_two / "photo.mov").read_bytes() == b"live"
    assert not (case.output_root / "nas-two" / "photo.jpg").exists()
    assert not (case.output_root / "nas-two" / "photo.mov").exists()
    assert {row["reason"] for row in execute_rows_without_keeper(log_path)} == {"move_failed"}


def test_execute_plan_moves_full_primary_prefix_sidecar(tmp_path: Path) -> None:
    case = make_move_case(
        tmp_path,
        with_sidecars=True,
        full_primary_prefix_sidecar=True,
    )
    log_path = tmp_path / "execute.csv"

    result = execute_plan(case.plan_path, log_path, move=True)

    assert result.moved_bundles == 1
    assert result.orphan_sidecars == 0
    assert (case.nas_one / "photo.jpg").exists()
    assert (case.nas_one / "photo.jpg.json").exists()
    assert not (case.nas_two / "photo.jpg").exists()
    assert not (case.nas_two / "photo.jpg.json").exists()
    assert (case.output_root / "nas-two" / "photo.jpg").read_bytes() == b"same"
    assert (case.output_root / "nas-two" / "photo.jpg.json").read_bytes() == b"live"
    assert "orphan_plan_sidecar" not in {row["disposition"] for row in rows(log_path)}


def test_execute_plan_merges_sidecar_into_keeper_bundle(tmp_path: Path) -> None:
    case = make_sidecar_merge_case(tmp_path)
    log_path = tmp_path / "execute.csv"

    result = execute_plan(case.plan_path, log_path, move=True)

    assert result.moved_bundles == 1
    assert (case.nas_one / "foo.jpg").exists()
    assert (case.nas_one / "foo.mov").exists()
    assert (case.nas_one / "foo.jpg.json").read_bytes() == b"metadata"
    assert not (case.nas_two / "bar.jpg").exists()
    assert not (case.nas_two / "bar.jpg.json").exists()
    assert (case.output_root / "nas-two" / "bar.jpg").read_bytes() == b"same"
    assert not (case.output_root / "nas-two" / "bar.jpg.json").exists()
    execute_rows = rows(log_path)
    assert "merged_sidecar" in {row["disposition"] for row in execute_rows}
    merge = [row for row in execute_rows if row["disposition"] == "merged_sidecar"][0]
    assert merge["destination_path"] == str(case.nas_one / "foo.jpg.json")
    assert merge["action_taken"] == "merged"


def test_execute_plan_existing_same_hash_merge_target_moves_source_sidecar_to_output(tmp_path: Path) -> None:
    case = make_sidecar_merge_case(tmp_path)
    write(case.nas_one / "foo.jpg.json", b"metadata")
    log_path = tmp_path / "execute.csv"

    result = execute_plan(case.plan_path, log_path, move=True)

    assert result.moved_bundles == 1
    assert (case.nas_one / "foo.jpg.json").read_bytes() == b"metadata"
    assert not (case.nas_two / "bar.jpg.json").exists()
    assert (case.output_root / "nas-two" / "bar.jpg.json").read_bytes() == b"metadata"
    moved_sidecar = [row for row in rows(log_path) if row["source_path"] == str(case.nas_two / "bar.jpg.json")][0]
    assert moved_sidecar["disposition"] == "moved_duplicate_sidecar"
    assert moved_sidecar["destination_path"] == str(case.output_root / "nas-two" / "bar.jpg.json")


def test_execute_plan_existing_different_hash_merge_target_skips_bundle(tmp_path: Path) -> None:
    case = make_sidecar_merge_case(tmp_path)
    write(case.nas_one / "foo.jpg.json", b"different")
    log_path = tmp_path / "execute.csv"

    result = execute_plan(case.plan_path, log_path, move=True)

    assert result.skipped_bundles == 1
    assert (case.nas_two / "bar.jpg").exists()
    assert (case.nas_two / "bar.jpg.json").exists()
    assert not (case.output_root / "nas-two" / "bar.jpg").exists()
    assert {row["reason"] for row in execute_rows_without_keeper(log_path)} == {"merge_destination_hash_mismatch"}


def test_execute_plan_rolls_back_primary_when_sidecar_merge_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = make_sidecar_merge_case(tmp_path)
    log_path = tmp_path / "execute.csv"
    original_move = execute_module.shutil.move
    move_calls: list[tuple[Path, Path]] = []

    def fail_second_move(source: str, destination: str) -> str:
        move_calls.append((Path(source), Path(destination)))
        if len(move_calls) == 2:
            raise OSError("forced merge failure")
        return original_move(source, destination)

    monkeypatch.setattr(execute_module.shutil, "move", fail_second_move)

    result = execute_plan(case.plan_path, log_path, move=True)

    assert result.moved_bundles == 0
    assert result.skipped_bundles == 1
    assert (case.nas_two / "bar.jpg").read_bytes() == b"same"
    assert (case.nas_two / "bar.jpg.json").read_bytes() == b"metadata"
    assert not (case.nas_one / "foo.jpg.json").exists()
    assert not (case.output_root / "nas-two" / "bar.jpg").exists()
    assert {row["reason"] for row in execute_rows_without_keeper(log_path)} == {"move_failed"}


def test_execute_plan_destination_collision_skips_bundle(tmp_path: Path) -> None:
    case = make_move_case(tmp_path, with_sidecars=True)
    write(case.output_root / "nas-two" / "photo.jpg", b"existing")
    log_path = tmp_path / "execute.csv"

    result = execute_plan(case.plan_path, log_path, move=True)

    assert result.skipped_bundles == 1
    assert (case.nas_two / "photo.jpg").exists()
    assert (case.nas_two / "photo.mov").exists()
    assert {row["reason"] for row in execute_rows_without_keeper(log_path)} == {"destination_exists"}
    assert {row["disposition"] for row in execute_rows_without_keeper(log_path)} == {
        "skipped_error_primary",
        "skipped_error_sidecar",
    }

def test_execute_plan_missing_keeper_skips_bundle(tmp_path: Path) -> None:
    case = make_move_case(tmp_path)
    (case.nas_one / "photo.jpg").unlink()
    log_path = tmp_path / "execute.csv"

    result = execute_plan(case.plan_path, log_path, move=True)

    assert result.skipped_bundles == 1
    assert (case.nas_two / "photo.jpg").exists()
    assert rows(log_path)[0]["reason"] == "keeper_missing"

def test_execute_plan_keeper_size_mismatch_skips_bundle(tmp_path: Path) -> None:
    case = make_move_case(tmp_path)
    (case.nas_one / "photo.jpg").write_bytes(b"different size")
    log_path = tmp_path / "execute.csv"

    result = execute_plan(case.plan_path, log_path, move=True)

    assert result.skipped_bundles == 1
    assert (case.nas_two / "photo.jpg").exists()
    assert rows(log_path)[0]["reason"] == "keeper_size_mismatch"

def test_execute_plan_missing_keeper_row_skips_bundle(tmp_path: Path) -> None:
    case = make_move_case(tmp_path)
    plan_rows = rows(case.plan_path)
    fieldnames = list(plan_rows[0])
    planned_only = [row for row in plan_rows if row["disposition"] == "planned_duplicate_primary"]
    write_csv(case.plan_path, fieldnames, planned_only)
    log_path = tmp_path / "execute.csv"

    result = execute_plan(case.plan_path, log_path, move=True)

    assert result.skipped_bundles == 1
    assert (case.nas_two / "photo.jpg").exists()
    assert rows(log_path)[0]["reason"] == "keeper_missing_from_plan"

def test_execute_plan_refuses_to_move_keeper_source(tmp_path: Path) -> None:
    case = make_move_case(tmp_path)
    plan_rows = rows(case.plan_path)
    fieldnames = list(plan_rows[0])
    keeper = [row for row in plan_rows if row["disposition"] == "kept_duplicate_keeper"][0]
    duplicate = [row for row in plan_rows if row["disposition"] == "planned_duplicate_primary"][0]
    duplicate["source_path"] = keeper["source_path"]
    duplicate["size_bytes"] = keeper["size_bytes"]
    write_csv(case.plan_path, fieldnames, plan_rows)
    log_path = tmp_path / "execute.csv"

    result = execute_plan(case.plan_path, log_path, move=True)

    assert result.skipped_bundles == 1
    assert (case.nas_one / "photo.jpg").exists()
    assert execute_rows_without_keeper(log_path)[0]["reason"] == "duplicate_source_is_keeper"

def test_execute_plan_already_moved_bundle_is_resumable(tmp_path: Path) -> None:
    case = make_move_case(tmp_path)
    destination = case.output_root / "nas-two" / "photo.jpg"
    destination.parent.mkdir(parents=True)
    destination.write_bytes((case.nas_two / "photo.jpg").read_bytes())
    (case.nas_two / "photo.jpg").unlink()
    log_path = tmp_path / "execute.csv"

    result = execute_plan(case.plan_path, log_path, move=True)

    assert result.already_moved_bundles == 1
    assert keeper_rows(log_path)[0]["disposition"] == "verified_keeper"
    execute_rows = execute_rows_without_keeper(log_path)
    assert execute_rows[0]["disposition"] == "already_moved_duplicate_primary"
    assert execute_rows[0]["reason"] == "already_moved"
    assert execute_rows[0]["validation_result"] == "already_moved"
    assert execute_rows[0]["hash_check"] == "matched"

def test_execute_plan_already_moved_destination_hash_mismatch_skips_bundle(tmp_path: Path) -> None:
    case = make_move_case(tmp_path)
    destination = case.output_root / "nas-two" / "photo.jpg"
    destination.parent.mkdir(parents=True)
    destination.write_bytes(b"diff")
    (case.nas_two / "photo.jpg").unlink()
    log_path = tmp_path / "execute.csv"

    result = execute_plan(case.plan_path, log_path, move=True)

    assert result.skipped_bundles == 1
    assert result.already_moved_bundles == 0
    assert destination.read_bytes() == b"diff"
    failure = execute_rows_without_keeper(log_path)[0]
    assert failure["disposition"] == "skipped_error_primary"
    assert failure["reason"] == "destination_hash_mismatch"
    assert failure["validation_result"] == "destination_hash_mismatch"
    assert failure["hash_check"] == "mismatched"
    assert failure["observed_hash"]

def test_execute_plan_missing_source_without_destination_skips_bundle(tmp_path: Path) -> None:
    case = make_move_case(tmp_path)
    (case.nas_two / "photo.jpg").unlink()
    log_path = tmp_path / "execute.csv"

    result = execute_plan(case.plan_path, log_path, move=True)

    assert result.skipped_bundles == 1
    assert execute_rows_without_keeper(log_path)[0]["reason"] == "source_missing"

def test_execute_plan_source_size_mismatch_skips_bundle(tmp_path: Path) -> None:
    case = make_move_case(tmp_path)
    (case.nas_two / "photo.jpg").write_bytes(b"different size")
    log_path = tmp_path / "execute.csv"

    result = execute_plan(case.plan_path, log_path, move=True)

    assert result.skipped_bundles == 1
    assert execute_rows_without_keeper(log_path)[0]["reason"] == "source_size_mismatch"

def test_execute_plan_move_hash_validates_same_size_source_change_by_default(tmp_path: Path) -> None:
    case = make_move_case(tmp_path)
    (case.nas_two / "photo.jpg").write_bytes(b"diff")
    log_path = tmp_path / "execute.csv"

    result = execute_plan(case.plan_path, log_path, move=True)

    assert result.skipped_bundles == 1
    assert result.moved_bundles == 0
    assert (case.nas_two / "photo.jpg").exists()
    assert not (case.output_root / "nas-two" / "photo.jpg").exists()
    failure = execute_rows_without_keeper(log_path)[0]
    assert failure["reason"] == "source_hash_mismatch"
    assert failure["validation_result"] == "source_hash_mismatch"
    assert failure["hash_check"] == "mismatched"
    assert failure["observed_hash"]

def test_execute_plan_hash_validation_can_be_opted_out_for_move(tmp_path: Path) -> None:
    case = make_move_case(tmp_path)
    (case.nas_two / "photo.jpg").write_bytes(b"diff")
    log_path = tmp_path / "execute.csv"

    result = execute_plan(case.plan_path, log_path, move=True, verify_source_hashes=False)

    assert result.moved_bundles == 1
    assert not (case.nas_two / "photo.jpg").exists()
    assert (case.output_root / "nas-two" / "photo.jpg").read_bytes() == b"diff"

def test_execute_plan_hash_validation_catches_same_size_keeper_change(tmp_path: Path) -> None:
    case = make_move_case(tmp_path)
    (case.nas_one / "photo.jpg").write_bytes(b"diff")
    log_path = tmp_path / "execute.csv"

    result = execute_plan(case.plan_path, log_path, move=True, verify_source_hashes=True)

    assert result.skipped_bundles == 1
    assert result.moved_bundles == 0
    assert (case.nas_one / "photo.jpg").exists()
    assert (case.nas_two / "photo.jpg").exists()
    assert not (case.output_root / "nas-two" / "photo.jpg").exists()
    failure = keeper_rows(log_path)[0]
    assert failure["disposition"] == "keeper_error"
    assert failure["reason"] == "keeper_hash_mismatch"
    assert failure["validation_result"] == "keeper_hash_mismatch"
    assert failure["hash_check"] == "mismatched"
    assert failure["observed_hash"]

def test_execute_plan_partial_already_moved_bundle_skips_bundle(tmp_path: Path) -> None:
    case = make_move_case(tmp_path, with_sidecars=True)
    destination = case.output_root / "nas-two" / "photo.jpg"
    destination.parent.mkdir(parents=True)
    destination.write_bytes((case.nas_two / "photo.jpg").read_bytes())
    (case.nas_two / "photo.jpg").unlink()
    log_path = tmp_path / "execute.csv"

    result = execute_plan(case.plan_path, log_path, move=True)

    assert result.skipped_bundles == 1
    assert (case.nas_two / "photo.mov").exists()
    assert {row["reason"] for row in execute_rows_without_keeper(log_path)} == {"partial_bundle_state"}

def test_execute_plan_orphan_sidecar_is_logged_and_ignored(tmp_path: Path) -> None:
    case = make_move_case(tmp_path, with_sidecars=True)
    plan_rows = rows(case.plan_path)
    fieldnames = list(plan_rows[0])
    sidecar_only = [row for row in plan_rows if row["disposition"] == "planned_duplicate_sidecar"]
    write_csv(case.plan_path, fieldnames, sidecar_only)
    log_path = tmp_path / "execute.csv"

    result = execute_plan(case.plan_path, log_path, move=True)

    assert result.bundles == 0
    assert result.orphan_sidecars == 1
    assert rows(log_path)[0]["disposition"] == "orphan_plan_sidecar"

def test_execute_plan_duplicate_destination_skips_affected_bundles(tmp_path: Path) -> None:
    plan_path = tmp_path / "plan.csv"
    first_case = make_move_case(tmp_path / "a")
    second_case = make_move_case(tmp_path / "b")
    first_rows = rows(first_case.plan_path)
    second_rows = rows(second_case.plan_path)
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
    assert {row["reason"] for row in execute_rows_without_keeper(log_path)} == {"duplicate_destination_in_plan"}

def test_execute_plan_ignores_non_planned_rows(tmp_path: Path) -> None:
    case = make_move_case(tmp_path)
    plan_rows = rows(case.plan_path)
    fieldnames = list(plan_rows[0])
    non_planned = [row for row in plan_rows if row["event"] == "keeper_primary"]
    write_csv(case.plan_path, fieldnames, non_planned)
    log_path = tmp_path / "execute.csv"

    result = execute_plan(case.plan_path, log_path, move=True)

    assert result.bundles == 0
    assert rows(log_path) == []


@pytest.mark.parametrize(
    ("mutate_plan", "message"),
    [
        (
            lambda plan_rows: plan_rows[0].update({"size_bytes": "not-an-int"}),
            "invalid size_bytes",
        ),
        (
            lambda plan_rows: plan_rows.append(
                dict([row for row in plan_rows if row["disposition"] == "kept_duplicate_keeper"][0])
            ),
            "multiple keeper rows",
        ),
        (
            lambda plan_rows: [row for row in plan_rows if row["disposition"] == "planned_duplicate_sidecar"][0].update(
                {"primary_source_path": ""}
            ),
            "planned sidecar row must include primary_source_path",
        ),
        (
            lambda plan_rows: [row for row in plan_rows if row["disposition"] == "planned_duplicate_primary"][0].update(
                {"primary_source_path": "/nas/keeper.jpg"}
            ),
            "non-sidecar plan row must not include primary_source_path",
        ),
    ],
)
def test_execute_plan_rejects_malformed_plan_rows(
    tmp_path: Path,
    mutate_plan: object,
    message: str,
) -> None:
    case = make_move_case(tmp_path, with_sidecars=True)
    plan_rows = rows(case.plan_path)
    fieldnames = list(plan_rows[0])
    mutate_plan(plan_rows)
    write_csv(case.plan_path, fieldnames, plan_rows)

    with pytest.raises(ValueError, match=message):
        execute_plan(case.plan_path, tmp_path / "execute.csv", move=True)


def test_execute_plan_rejects_plan_missing_required_fields(tmp_path: Path) -> None:
    plan_path = tmp_path / "bad_plan.csv"
    write_csv(plan_path, ["disposition", "source_path"], [{"disposition": "planned_duplicate_primary", "source_path": "/x"}])

    with pytest.raises(ValueError, match="plan missing required fields"):
        execute_plan(plan_path, tmp_path / "execute.csv", move=True)
