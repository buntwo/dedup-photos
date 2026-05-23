from __future__ import annotations

from pathlib import Path

import pytest

from dedup_photos.manifest import execute_plan, generate_manifest, plan_from_manifests, verify_manifests, verify_move
from tests.helpers import make_conflict_manifests, make_move_case, make_sidecar_merge_case, prepare_nas_root, rows, write, write_rows


def test_verify_manifests_byte_checks_same_hash_groups(tmp_path: Path) -> None:
    nas_root = tmp_path / "nas"
    local_one = tmp_path / "one"
    local_two = tmp_path / "two"
    manifest_one = tmp_path / "one.csv"
    manifest_two = tmp_path / "two.csv"
    log_path = tmp_path / "verify.csv"
    write(local_one / "a.jpg", b"same")
    write(local_two / "b.jpg", b"same")
    write(nas_root / "one" / "a.jpg", b"same")
    write(nas_root / "two" / "b.jpg", b"same")
    generate_manifest(local_one, nas_root / "one", manifest_one)
    generate_manifest(local_two, nas_root / "two", manifest_two)

    result = verify_manifests([manifest_one, manifest_two], log_path, byte_check=True)

    assert result.checked_groups == 1
    assert result.failed_groups == 0
    assert rows(log_path)[0]["disposition"] == "verify_matched"


def test_verify_manifests_byte_checks_same_hash_sidecar_groups(tmp_path: Path) -> None:
    nas_root = tmp_path / "nas"
    local_one = tmp_path / "one"
    local_two = tmp_path / "two"
    manifest_one = tmp_path / "one.csv"
    manifest_two = tmp_path / "two.csv"
    log_path = tmp_path / "verify.csv"
    write(local_one / "a.jpg", b"same")
    write(local_one / "a.mov", b"live")
    write(local_two / "b.jpg", b"same")
    write(local_two / "b.mp4", b"live")
    write(nas_root / "one" / "a.jpg", b"same")
    write(nas_root / "one" / "a.mov", b"live")
    write(nas_root / "two" / "b.jpg", b"same")
    write(nas_root / "two" / "b.mp4", b"live")
    generate_manifest(local_one, nas_root / "one", manifest_one)
    generate_manifest(local_two, nas_root / "two", manifest_two)

    result = verify_manifests([manifest_one, manifest_two], log_path, byte_check=True)

    assert result.checked_groups == 2
    assert result.failed_groups == 0
    verify_rows = rows(log_path)
    assert {row["event"] for row in verify_rows} == {"verify_manifest_primary_group", "verify_manifest_sidecar_group"}
    sidecar = [row for row in verify_rows if row["file_role"] == "sidecar"][0]
    assert sidecar["disposition"] == "verify_matched"


def test_verify_manifests_requires_byte_check_flag(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="byte_check=True"):
        verify_manifests([], tmp_path / "verify.csv", byte_check=False)

def test_verify_manifests_byte_check_fails_same_hash_different_bytes(tmp_path: Path) -> None:
    nas_root = tmp_path / "nas"
    local_one = tmp_path / "one"
    local_two = tmp_path / "two"
    manifest_one = tmp_path / "one.csv"
    manifest_two = tmp_path / "two.csv"
    log_path = tmp_path / "verify.csv"
    write(local_one / "a.jpg", b"aaaa")
    write(local_two / "b.jpg", b"bbbb")
    write(nas_root / "one" / "a.jpg", b"aaaa")
    write(nas_root / "two" / "b.jpg", b"bbbb")
    generate_manifest(local_one, nas_root / "one", manifest_one)
    generate_manifest(local_two, nas_root / "two", manifest_two)
    one_rows = rows(manifest_one)
    two_rows = rows(manifest_two)
    two_rows[0]["xxh128"] = one_rows[0]["xxh128"]
    write_rows(manifest_two, two_rows)

    result = verify_manifests([manifest_one, manifest_two], log_path, byte_check=True)

    assert result.checked_groups == 1
    assert result.failed_groups == 1
    failure = [row for row in rows(log_path) if row["disposition"] == "verify_failed"][0]
    assert failure["reason"] == "same_manifest_hash_but_bytes_differ"


def test_verify_manifests_byte_check_fails_same_hash_different_sidecar_bytes(tmp_path: Path) -> None:
    nas_root = tmp_path / "nas"
    local_one = tmp_path / "one"
    local_two = tmp_path / "two"
    manifest_one = tmp_path / "one.csv"
    manifest_two = tmp_path / "two.csv"
    log_path = tmp_path / "verify.csv"
    write(local_one / "a.jpg", b"same")
    write(local_one / "a.mov", b"aaaa")
    write(local_two / "b.jpg", b"same")
    write(local_two / "b.mp4", b"bbbb")
    write(nas_root / "one" / "a.jpg", b"same")
    write(nas_root / "one" / "a.mov", b"aaaa")
    write(nas_root / "two" / "b.jpg", b"same")
    write(nas_root / "two" / "b.mp4", b"bbbb")
    generate_manifest(local_one, nas_root / "one", manifest_one)
    generate_manifest(local_two, nas_root / "two", manifest_two)
    one_rows = rows(manifest_one)
    two_rows = rows(manifest_two)
    one_sidecar = [row for row in one_rows if row["file_role"] == "sidecar"][0]
    two_sidecar = [row for row in two_rows if row["file_role"] == "sidecar"][0]
    two_sidecar["xxh128"] = one_sidecar["xxh128"]
    write_rows(manifest_two, two_rows)

    result = verify_manifests([manifest_one, manifest_two], log_path, byte_check=True)

    assert result.checked_groups == 2
    assert result.failed_groups == 1
    failure = [row for row in rows(log_path) if row["disposition"] == "verify_failed"][0]
    assert failure["event"] == "verify_manifest_sidecar"
    assert failure["file_role"] == "sidecar"
    assert failure["reason"] == "same_manifest_hash_but_bytes_differ"


def test_verify_move_passes_after_manifest_move(tmp_path: Path) -> None:
    case = make_move_case(tmp_path, with_sidecars=True)
    execute_plan(case.plan_path, tmp_path / "execute.csv", move=True)
    log_path = tmp_path / "verify_move.csv"

    result = verify_move(case.manifests, case.output_root, log_path)

    assert result.failed_paths == 0
    assert result.unexpected_outputs == 0
    assert result.checked_paths == 6
    assert {row["disposition"] for row in rows(log_path)} == {"verify_move_matched"}

def test_verify_move_fails_when_move_was_not_executed(tmp_path: Path) -> None:
    case = make_move_case(tmp_path)
    log_path = tmp_path / "verify_move.csv"

    result = verify_move(case.manifests, case.output_root, log_path)

    assert result.failed_paths == 2
    assert {
        row["reason"]
        for row in rows(log_path)
        if row["disposition"] == "verify_move_failed"
    } == {"duplicate_source_still_exists", "expected_file_missing"}

def test_verify_move_fails_when_keeper_is_missing(tmp_path: Path) -> None:
    case = make_move_case(tmp_path)
    execute_plan(case.plan_path, tmp_path / "execute.csv", move=True)
    (case.nas_one / "photo.jpg").unlink()
    log_path = tmp_path / "verify_move.csv"

    result = verify_move(case.manifests, case.output_root, log_path)

    assert result.failed_paths == 1
    assert rows(log_path)[0]["reason"] == "expected_file_missing"

def test_verify_move_fails_when_destination_size_mismatches(tmp_path: Path) -> None:
    case = make_move_case(tmp_path)
    execute_plan(case.plan_path, tmp_path / "execute.csv", move=True)
    (case.output_root / "nas-two" / "photo.jpg").write_bytes(b"wrong-size")
    log_path = tmp_path / "verify_move.csv"

    result = verify_move(case.manifests, case.output_root, log_path)

    assert result.failed_paths == 1
    failure = [row for row in rows(log_path) if row["disposition"] == "verify_move_failed"][0]
    assert failure["event"] == "verify_move_duplicate_destination"
    assert failure["reason"] == "expected_file_size_mismatch"

def test_verify_move_catches_unique_file_moved_to_output(tmp_path: Path) -> None:
    nas_one = tmp_path / "nas-one"
    nas_two = tmp_path / "nas-two"
    output_root = tmp_path / "dupes"
    manifest_one = tmp_path / "one.csv"
    manifest_two = tmp_path / "two.csv"
    write(nas_one / "photo.jpg", b"same")
    write(nas_two / "photo.jpg", b"same")
    write(nas_one / "unique.jpg", b"unique")
    generate_manifest(nas_one, nas_one, manifest_one)
    generate_manifest(nas_two, nas_two, manifest_two)
    plan_path = tmp_path / "plan.csv"
    plan_from_manifests([manifest_one, manifest_two], output_root, plan_path)
    execute_plan(plan_path, tmp_path / "execute.csv", move=True)
    moved_unique = output_root / "nas-one" / "unique.jpg"
    moved_unique.parent.mkdir(parents=True)
    (nas_one / "unique.jpg").rename(moved_unique)
    log_path = tmp_path / "verify_move.csv"

    result = verify_move([manifest_one, manifest_two], output_root, log_path)

    assert result.failed_paths == 1
    assert result.unexpected_outputs == 1
    dispositions = {row["disposition"] for row in rows(log_path)}
    assert "verify_move_failed" in dispositions
    assert "verify_move_unexpected_output" in dispositions

def test_verify_move_sidecar_conflict_group_must_remain_in_place(tmp_path: Path) -> None:
    manifests, _nas_one, _nas_two, output_root = make_conflict_manifests(tmp_path)
    log_path = tmp_path / "verify_move.csv"

    result = verify_move(manifests, output_root, log_path)

    assert result.failed_paths == 0
    assert result.unexpected_outputs == 0
    assert {row["disposition"] for row in rows(log_path)} == {"verify_move_skipped_conflict_intact"}


def test_verify_move_uses_sidecar_superset_as_keeper(tmp_path: Path) -> None:
    local_one = tmp_path / "one"
    local_two = tmp_path / "two"
    manifest_one = tmp_path / "one.csv"
    manifest_two = tmp_path / "two.csv"
    output_root = tmp_path / "dupes"
    write(local_one / "foo.jpg", b"same image")
    write(local_one / "foo.mov", b"same video")
    write(local_one / "foo.jpg.json", b"extra metadata")
    write(local_two / "bar.jpg", b"same image")
    write(local_two / "bar.mp4", b"same video")
    nas_one = prepare_nas_root(local_one, tmp_path / "nas")
    nas_two = prepare_nas_root(local_two, tmp_path / "nas")
    write(nas_one / "foo.jpg", b"same image")
    write(nas_one / "foo.mov", b"same video")
    write(nas_one / "foo.jpg.json", b"extra metadata")
    write(nas_two / "bar.jpg", b"same image")
    write(nas_two / "bar.mp4", b"same video")
    generate_manifest(local_one, nas_one, manifest_one)
    generate_manifest(local_two, nas_two, manifest_two)
    plan_path = tmp_path / "plan.csv"
    plan_from_manifests([manifest_one, manifest_two], output_root, plan_path)
    execute_plan(plan_path, tmp_path / "execute.csv", move=True)
    log_path = tmp_path / "verify_move.csv"

    result = verify_move([manifest_one, manifest_two], output_root, log_path)

    assert result.failed_paths == 0
    keeper_rows = [row for row in rows(log_path) if row["event"] == "verify_move_keeper"]
    assert {row["source_path"] for row in keeper_rows} == {
        str(nas_one / "foo.jpg"),
        str(nas_one / "foo.mov"),
        str(nas_one / "foo.jpg.json"),
    }


def test_verify_move_passes_after_sidecar_merge(tmp_path: Path) -> None:
    case = make_sidecar_merge_case(tmp_path)
    execute_plan(case.plan_path, tmp_path / "execute.csv", move=True)
    log_path = tmp_path / "verify_move.csv"

    result = verify_move(case.manifests, case.output_root, log_path)

    assert result.failed_paths == 0
    assert result.unexpected_outputs == 0
    merge_rows = [row for row in rows(log_path) if row["event"] == "verify_move_sidecar_merge"]
    assert merge_rows[0]["destination_path"] == str(case.nas_one / "foo.jpg.json")


def test_verify_move_fails_when_merged_sidecar_left_in_source(tmp_path: Path) -> None:
    case = make_sidecar_merge_case(tmp_path)
    execute_plan(case.plan_path, tmp_path / "execute.csv", move=True)
    write(case.nas_two / "bar.jpg.json", b"metadata")
    log_path = tmp_path / "verify_move.csv"

    result = verify_move(case.manifests, case.output_root, log_path)

    assert result.failed_paths == 1
    assert "duplicate_source_still_exists" in {row["reason"] for row in rows(log_path)}


def test_verify_move_fails_when_merged_sidecar_destination_missing(tmp_path: Path) -> None:
    case = make_sidecar_merge_case(tmp_path)
    execute_plan(case.plan_path, tmp_path / "execute.csv", move=True)
    (case.nas_one / "foo.jpg.json").unlink()
    log_path = tmp_path / "verify_move.csv"

    result = verify_move(case.manifests, case.output_root, log_path)

    assert result.failed_paths == 1
    assert "expected_file_missing" in {row["reason"] for row in rows(log_path)}


def test_verify_move_uses_takeout_album_precedence(tmp_path: Path) -> None:
    local_one = tmp_path / "Trip to Turkey"
    local_two = tmp_path / "Photos from 2021"
    manifest_one = tmp_path / "trip.csv"
    manifest_two = tmp_path / "date.csv"
    output_root = tmp_path / "dupes"
    write(local_one / "photo.jpg", b"same")
    write(local_two / "photo.jpg", b"same")
    nas_one = prepare_nas_root(local_one, tmp_path / "nas" / "takeout")
    nas_two = prepare_nas_root(local_two, tmp_path / "nas" / "takeout" / "20240101")
    write(nas_one / "photo.jpg", b"same")
    write(nas_two / "photo.jpg", b"same")
    generate_manifest(local_one, nas_one, manifest_one)
    generate_manifest(local_two, nas_two, manifest_two)
    plan_path = tmp_path / "plan.csv"
    plan_from_manifests([manifest_one, manifest_two], output_root, plan_path)
    execute_plan(plan_path, tmp_path / "execute.csv", move=True)
    log_path = tmp_path / "verify_move.csv"

    result = verify_move([manifest_one, manifest_two], output_root, log_path)

    assert result.failed_paths == 0
    keeper_rows = [row for row in rows(log_path) if row["event"] == "verify_move_keeper"]
    assert keeper_rows[0]["source_path"] == str(nas_one / "photo.jpg")


def test_verify_move_fails_when_mobilebackup_was_kept_over_takeout_date_folder(tmp_path: Path) -> None:
    local_takeout = tmp_path / "Photos from 2021"
    local_mobilebackup = tmp_path / "mobilebackup"
    manifest_takeout = tmp_path / "takeout.csv"
    manifest_mobilebackup = tmp_path / "mobilebackup.csv"
    output_root = tmp_path / "dupes"
    write(local_takeout / "photo.jpg", b"same")
    write(local_mobilebackup / "DCIM" / "photo.jpg", b"same")
    nas_takeout = prepare_nas_root(local_takeout, tmp_path / "nas" / "takeout")
    nas_mobilebackup = prepare_nas_root(local_mobilebackup, tmp_path / "nas")
    write(nas_takeout / "photo.jpg", b"same")
    write(nas_mobilebackup / "DCIM" / "photo.jpg", b"same")
    generate_manifest(local_takeout, nas_takeout, manifest_takeout)
    generate_manifest(local_mobilebackup, nas_mobilebackup, manifest_mobilebackup)
    wrong_destination = output_root / "Photos from 2021" / "photo.jpg"
    wrong_destination.parent.mkdir(parents=True)
    (nas_takeout / "photo.jpg").rename(wrong_destination)
    log_path = tmp_path / "verify_move.csv"

    result = verify_move([manifest_takeout, manifest_mobilebackup], output_root, log_path)

    assert result.failed_paths > 0
    keeper_rows = [row for row in rows(log_path) if row["event"] == "verify_move_keeper"]
    assert keeper_rows[0]["source_path"] == str(nas_takeout / "photo.jpg")
    assert keeper_rows[0]["disposition"] == "verify_move_failed"
    assert "unexpected_output_file" in {row["reason"] for row in rows(log_path)}


def test_verify_move_fails_when_sidecar_conflict_file_was_moved(tmp_path: Path) -> None:
    manifests, _nas_one, nas_two, output_root = make_conflict_manifests(tmp_path)
    destination = output_root / "nas-two" / "photo.json"
    destination.parent.mkdir(parents=True)
    (nas_two / "photo.json").rename(destination)
    log_path = tmp_path / "verify_move.csv"

    result = verify_move(manifests, output_root, log_path)

    assert result.failed_paths == 1
    assert result.unexpected_outputs == 1
    assert "expected_file_missing" in {row["reason"] for row in rows(log_path)}

def test_verify_move_fails_on_extra_output_file(tmp_path: Path) -> None:
    case = make_move_case(tmp_path)
    execute_plan(case.plan_path, tmp_path / "execute.csv", move=True)
    write(case.output_root / "extra.txt", b"extra")
    log_path = tmp_path / "verify_move.csv"

    result = verify_move(case.manifests, case.output_root, log_path)

    assert result.failed_paths == 0
    assert result.unexpected_outputs == 1
    assert rows(log_path)[-1]["disposition"] == "verify_move_unexpected_output"
