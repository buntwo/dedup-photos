from __future__ import annotations

import csv
import json
from pathlib import Path

import pytest

from dedup_photos.constants import MANIFEST_VERSION
from dedup_photos.cli import main, manifest_main
from dedup_photos.manifest import MANIFEST_FIELDS
from dedup_photos.manifest import execute_plan, generate_manifest, plan_from_manifests, verify_manifests, verify_move


def write(path: Path, contents: bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(contents)
    return path


def rows(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as file:
        return list(csv.DictReader(file))


def write_rows(path: Path, manifest_rows: list[dict[str, str]]) -> None:
    with path.open("w", newline="", encoding="utf-8") as file:
        writer = csv.DictWriter(file, fieldnames=MANIFEST_FIELDS)
        writer.writeheader()
        writer.writerows(manifest_rows)


def write_csv(path: Path, fieldnames: list[str], csv_rows: list[dict[str, str]]) -> None:
    with path.open("w", newline="", encoding="utf-8") as file:
        writer = csv.DictWriter(file, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(csv_rows)


def make_move_plan(tmp_path: Path, *, with_sidecars: bool = False) -> tuple[Path, Path, Path, Path]:
    nas_one = tmp_path / "nas-one"
    nas_two = tmp_path / "nas-two"
    output_root = tmp_path / "dupes"
    manifest_one = tmp_path / "one.csv"
    manifest_two = tmp_path / "two.csv"
    plan_path = tmp_path / "plan.csv"
    write(nas_one / "photo.jpg", b"same")
    write(nas_two / "photo.jpg", b"same")
    if with_sidecars:
        write(nas_one / "photo.mov", b"live")
        write(nas_two / "photo.mov", b"live")
    generate_manifest(nas_one, nas_one, manifest_one)
    generate_manifest(nas_two, nas_two, manifest_two)
    plan_from_manifests([manifest_one, manifest_two], output_root, plan_path)
    return plan_path, nas_one, nas_two, output_root


def make_manifest_move_case(
    tmp_path: Path,
    *,
    with_sidecars: bool = False,
) -> tuple[list[Path], Path, Path, Path, Path]:
    nas_one = tmp_path / "nas-one"
    nas_two = tmp_path / "nas-two"
    output_root = tmp_path / "dupes"
    manifest_one = tmp_path / "one.csv"
    manifest_two = tmp_path / "two.csv"
    plan_path = tmp_path / "plan.csv"
    write(nas_one / "photo.jpg", b"same")
    write(nas_two / "photo.jpg", b"same")
    if with_sidecars:
        write(nas_one / "photo.mov", b"live")
        write(nas_two / "photo.mov", b"live")
    generate_manifest(nas_one, nas_one, manifest_one)
    generate_manifest(nas_two, nas_two, manifest_two)
    plan_from_manifests([manifest_one, manifest_two], output_root, plan_path)
    return [manifest_one, manifest_two], plan_path, nas_one, nas_two, output_root


def make_conflict_manifests(tmp_path: Path) -> tuple[list[Path], Path, Path, Path]:
    nas_one = tmp_path / "nas-one"
    nas_two = tmp_path / "nas-two"
    manifest_one = tmp_path / "one.csv"
    manifest_two = tmp_path / "two.csv"
    write(nas_one / "photo.jpg", b"same")
    write(nas_one / "photo.json", b"left")
    write(nas_two / "photo.jpg", b"same")
    write(nas_two / "photo.json", b"right")
    generate_manifest(nas_one, nas_one, manifest_one)
    generate_manifest(nas_two, nas_two, manifest_two)
    return [manifest_one, manifest_two], nas_one, nas_two, tmp_path / "dupes"


def test_generate_manifest_stores_nas_paths_and_sidecars(tmp_path: Path) -> None:
    local_root = tmp_path / "batch"
    nas_root = tmp_path / "nas" / "google photos"
    manifest_path = tmp_path / "batch.csv"
    write(local_root / "2024" / "photo.HEIC", b"image")
    write(local_root / "2024" / "photo.MOV", b"video")

    generate_manifest(local_root, nas_root, manifest_path)

    manifest_rows = rows(manifest_path)
    assert len(manifest_rows) == 1
    row = manifest_rows[0]
    assert row["nas_path"] == str(nas_root / "2024" / "photo.HEIC")
    assert row["relative_path"] == "2024/photo.HEIC"
    assert row["size_bytes"] == "5"
    assert row["xxh128"]
    assert row["sidecar_count"] == "1"
    assert json.loads(row["sidecar_paths"]) == [str(nas_root / "2024" / "photo.MOV")]
    assert json.loads(row["sidecar_relative_paths"]) == ["2024/photo.MOV"]
    assert json.loads(row["sidecar_sizes"]) == [5]
    assert len(json.loads(row["sidecar_xxh128s"])[0]) == 32


def test_plan_from_manifests_uses_sidecar_precedence_and_nas_destinations(tmp_path: Path) -> None:
    local_one = tmp_path / "local-google"
    local_two = tmp_path / "local-backups"
    nas_one = Path("/volume1/photo/google photos")
    nas_two = Path("/volume1/homes/btu/photos/backups")
    manifest_one = tmp_path / "google.csv"
    manifest_two = tmp_path / "backups.csv"
    log_path = tmp_path / "plan.csv"
    output_root = Path("/volume1/photo/dupes")
    write(local_one / "photo.jpg", b"same")
    write(local_two / "photo.jpg", b"same")
    write(local_two / "photo.mov", b"live")
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
    generate_manifest(local_one, Path("/nas/one"), manifest_one)
    generate_manifest(local_two, Path("/nas/two"), manifest_two)

    result = plan_from_manifests([manifest_one, manifest_two], Path("/dupes"), log_path)

    assert result.skipped_groups == 1
    plan_rows = rows(log_path)
    assert [row for row in plan_rows if row["event"] == "duplicate_group_skipped"][0]["reason"] == "unresolved_sidecar_conflict"
    assert not [row for row in plan_rows if row["event"] == "duplicate_primary_move"]


def test_verify_manifests_byte_checks_same_hash_groups(tmp_path: Path) -> None:
    nas_root = tmp_path / "nas"
    local_one = tmp_path / "local-one"
    local_two = tmp_path / "local-two"
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


def test_verify_manifests_requires_byte_check_flag(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="byte_check=True"):
        verify_manifests([], tmp_path / "verify.csv", byte_check=False)


def test_same_size_different_hashes_remain_unique(tmp_path: Path) -> None:
    local_one = tmp_path / "one"
    local_two = tmp_path / "two"
    manifest_one = tmp_path / "one.csv"
    manifest_two = tmp_path / "two.csv"
    log_path = tmp_path / "plan.csv"
    write(local_one / "a.jpg", b"aaaa")
    write(local_two / "b.jpg", b"bbbb")
    generate_manifest(local_one, Path("/nas/one"), manifest_one)
    generate_manifest(local_two, Path("/nas/two"), manifest_two)

    result = plan_from_manifests([manifest_one, manifest_two], Path("/dupes"), log_path)

    assert result.duplicate_groups == 0
    assert result.duplicate_files == 0
    assert len([row for row in rows(log_path) if row["event"] == "unique_primary"]) == 2


def test_duplicate_identical_nas_paths_are_collapsed(tmp_path: Path) -> None:
    local_root = tmp_path / "local"
    nas_root = Path("/nas/photos")
    manifest_path = tmp_path / "manifest.csv"
    log_path = tmp_path / "plan.csv"
    write(local_root / "photo.jpg", b"only-copy")
    generate_manifest(local_root, nas_root, manifest_path)

    result = plan_from_manifests([manifest_path, manifest_path], Path("/dupes"), log_path)

    assert result.duplicate_groups == 0
    assert len([row for row in rows(log_path) if row["event"] == "unique_primary"]) == 1


def test_conflicting_duplicate_nas_paths_are_rejected(tmp_path: Path) -> None:
    local_one = tmp_path / "one"
    local_two = tmp_path / "two"
    nas_root = Path("/nas/photos")
    manifest_one = tmp_path / "one.csv"
    manifest_two = tmp_path / "two.csv"
    write(local_one / "photo.jpg", b"left")
    write(local_two / "photo.jpg", b"right")
    generate_manifest(local_one, nas_root, manifest_one)
    generate_manifest(local_two, nas_root, manifest_two)

    with pytest.raises(ValueError, match="conflicting duplicate nas_path"):
        plan_from_manifests([manifest_one, manifest_two], Path("/dupes"), tmp_path / "plan.csv")


def test_verify_manifests_byte_check_fails_same_hash_different_bytes(tmp_path: Path) -> None:
    nas_root = tmp_path / "nas"
    local_one = tmp_path / "local-one"
    local_two = tmp_path / "local-two"
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
    generate_manifest(local_one, Path("/nas/one"), manifest_one)
    generate_manifest(local_two, Path("/nas/two"), manifest_two)

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
    generate_manifest(local_one, Path("/nas/one"), manifest_one)
    generate_manifest(local_two, Path("/nas/two"), manifest_two)

    plan_from_manifests([manifest_one, manifest_two], output_root, log_path)

    sidecar_rows = [row for row in rows(log_path) if row["event"] == "sidecar_move"]
    assert len(sidecar_rows) == 1
    assert sidecar_rows[0]["source_path"] == "/nas/two/a/photo.mov"
    assert sidecar_rows[0]["destination_path"] == "/dupes/two/a/photo.mov"


def test_plan_from_manifests_preserves_date_takeout_mobilebackup_precedence(tmp_path: Path) -> None:
    local_one = tmp_path / "one"
    local_two = tmp_path / "two"
    local_three = tmp_path / "three"
    manifests = [tmp_path / "one.csv", tmp_path / "two.csv", tmp_path / "three.csv"]
    log_path = tmp_path / "plan.csv"
    write(local_one / "photo.jpg", b"same")
    write(local_two / "photo.jpg", b"same")
    write(local_three / "photo.jpg", b"same")
    generate_manifest(local_one, Path("/nas/takeout/Sunday Funday"), manifests[0])
    generate_manifest(local_two, Path("/nas/takeout/20240101/Photos from 2023"), manifests[1])
    generate_manifest(local_three, Path("/nas/mobilebackup"), manifests[2])

    plan_from_manifests(manifests, Path("/dupes"), log_path)

    keeper = [row for row in rows(log_path) if row["event"] == "keeper_primary"][0]
    assert keeper["source_path"] == "/nas/takeout/Sunday Funday/photo.jpg"


def test_equivalent_sidecars_on_both_sides_allow_planning(tmp_path: Path) -> None:
    local_one = tmp_path / "one"
    local_two = tmp_path / "two"
    manifest_one = tmp_path / "one.csv"
    manifest_two = tmp_path / "two.csv"
    log_path = tmp_path / "plan.csv"
    write(local_one / "photo.jpg", b"same")
    write(local_one / "photo.json", b"metadata")
    write(local_two / "photo.jpg", b"same")
    write(local_two / "photo.json", b"metadata")
    generate_manifest(local_one, Path("/nas/one"), manifest_one)
    generate_manifest(local_two, Path("/nas/two"), manifest_two)

    result = plan_from_manifests([manifest_one, manifest_two], Path("/dupes"), log_path)

    assert result.skipped_groups == 0
    assert len([row for row in rows(log_path) if row["event"] == "duplicate_primary_move"]) == 1


def test_load_manifest_rejects_missing_fields(tmp_path: Path) -> None:
    manifest_path = tmp_path / "bad.csv"
    manifest_path.write_text("manifest_version,nas_path\n1,/nas/photo.jpg\n", encoding="utf-8")

    with pytest.raises(ValueError, match="missing required fields"):
        plan_from_manifests([manifest_path], Path("/dupes"), tmp_path / "plan.csv")


def test_load_manifest_rejects_unsupported_version(tmp_path: Path) -> None:
    manifest_path = tmp_path / "bad.csv"
    row = {
        field: ""
        for field in MANIFEST_FIELDS
    }
    row.update(
        {
            "manifest_version": "999",
            "batch_root": "/local",
            "nas_root": "/nas",
            "nas_root_label": "nas",
            "nas_path": "/nas/photo.jpg",
            "relative_path": "photo.jpg",
            "size_bytes": "1",
            "xxh128": "abc",
            "sidecar_count": "0",
            "sidecar_paths": "[]",
            "sidecar_relative_paths": "[]",
            "sidecar_sizes": "[]",
            "sidecar_xxh128s": "[]",
        }
    )
    write_rows(manifest_path, [row])

    with pytest.raises(ValueError, match="unsupported manifest version"):
        plan_from_manifests([manifest_path], Path("/dupes"), tmp_path / "plan.csv")


def test_load_manifest_rejects_sidecar_length_mismatch(tmp_path: Path) -> None:
    manifest_path = tmp_path / "bad.csv"
    row = {
        field: ""
        for field in MANIFEST_FIELDS
    }
    row.update(
        {
            "manifest_version": MANIFEST_VERSION,
            "batch_root": "/local",
            "nas_root": "/nas",
            "nas_root_label": "nas",
            "nas_path": "/nas/photo.jpg",
            "relative_path": "photo.jpg",
            "size_bytes": "1",
            "xxh128": "abc",
            "sidecar_count": "1",
            "sidecar_paths": "[]",
            "sidecar_relative_paths": "[]",
            "sidecar_sizes": "[]",
            "sidecar_xxh128s": "[]",
        }
    )
    write_rows(manifest_path, [row])

    with pytest.raises(ValueError, match="sidecar field length mismatch"):
        plan_from_manifests([manifest_path], Path("/dupes"), tmp_path / "plan.csv")


def test_empty_manifest_writes_empty_plan(tmp_path: Path) -> None:
    manifest_path = tmp_path / "empty.csv"
    with manifest_path.open("w", newline="", encoding="utf-8") as file:
        csv.DictWriter(file, fieldnames=MANIFEST_FIELDS).writeheader()

    result = plan_from_manifests([manifest_path], Path("/dupes"), tmp_path / "plan.csv")

    assert result.duplicate_groups == 0
    assert result.duplicate_files == 0
    assert rows(tmp_path / "plan.csv") == []


def test_manifest_cli_subcommands(tmp_path: Path) -> None:
    local_one = tmp_path / "one"
    local_two = tmp_path / "two"
    nas_root = tmp_path / "nas"
    manifest_one = tmp_path / "one.csv"
    manifest_two = tmp_path / "two.csv"
    plan_log = tmp_path / "plan.csv"
    verify_log = tmp_path / "verify.csv"
    write(local_one / "a.jpg", b"same")
    write(local_two / "b.jpg", b"same")
    write(nas_root / "one" / "a.jpg", b"same")
    write(nas_root / "two" / "b.jpg", b"same")

    assert manifest_main(["manifest", str(local_one), "--nas-root", str(nas_root / "one"), "--manifest", str(manifest_one)]) == 0
    assert manifest_main(["manifest", str(local_two), "--nas-root", str(nas_root / "two"), "--manifest", str(manifest_two)]) == 0
    assert manifest_main(["plan", str(manifest_one), str(manifest_two), "--output", str(tmp_path / "dupes"), "--log", str(plan_log)]) == 0
    assert manifest_main(["verify-bytes", str(manifest_one), str(manifest_two), "--log", str(verify_log)]) == 0
    assert manifest_one.exists()
    assert manifest_two.exists()
    assert [row for row in rows(plan_log) if row["event"] == "duplicate_primary_move"]
    assert rows(verify_log)[0]["disposition"] == "verify_matched"


def test_direct_help_does_not_list_manifest_commands(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exit_info:
        main(["--help"])

    assert exit_info.value.code == 0
    help_text = capsys.readouterr().out
    assert "input photo library roots" in help_text
    assert "manifest" not in help_text


def test_manifest_help_lists_manifest_commands(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exit_info:
        manifest_main(["--help"])

    assert exit_info.value.code == 0
    help_text = capsys.readouterr().out
    assert "manifest" in help_text
    assert "plan" in help_text
    assert "verify-bytes" in help_text
    assert "execute-plan" in help_text
    assert "verify-move" in help_text


def test_verify_move_passes_after_manifest_move(tmp_path: Path) -> None:
    manifests, plan_path, _nas_one, _nas_two, output_root = make_manifest_move_case(tmp_path, with_sidecars=True)
    execute_plan(plan_path, tmp_path / "execute.csv", move=True)
    log_path = tmp_path / "verify_move.csv"

    result = verify_move(manifests, output_root, log_path)

    assert result.failed_paths == 0
    assert result.unexpected_outputs == 0
    assert result.checked_paths == 6
    assert {row["disposition"] for row in rows(log_path)} == {"verify_move_matched"}


def test_verify_move_fails_when_move_was_not_executed(tmp_path: Path) -> None:
    manifests, _plan_path, _nas_one, _nas_two, output_root = make_manifest_move_case(tmp_path)
    log_path = tmp_path / "verify_move.csv"

    result = verify_move(manifests, output_root, log_path)

    assert result.failed_paths == 2
    assert {
        row["reason"]
        for row in rows(log_path)
        if row["disposition"] == "verify_move_failed"
    } == {"duplicate_source_still_exists", "expected_file_missing"}


def test_verify_move_fails_when_keeper_is_missing(tmp_path: Path) -> None:
    manifests, plan_path, nas_one, _nas_two, output_root = make_manifest_move_case(tmp_path)
    execute_plan(plan_path, tmp_path / "execute.csv", move=True)
    (nas_one / "photo.jpg").unlink()
    log_path = tmp_path / "verify_move.csv"

    result = verify_move(manifests, output_root, log_path)

    assert result.failed_paths == 1
    assert rows(log_path)[0]["reason"] == "expected_file_missing"


def test_verify_move_fails_when_destination_size_mismatches(tmp_path: Path) -> None:
    manifests, plan_path, _nas_one, _nas_two, output_root = make_manifest_move_case(tmp_path)
    execute_plan(plan_path, tmp_path / "execute.csv", move=True)
    (output_root / "nas-two" / "photo.jpg").write_bytes(b"wrong-size")
    log_path = tmp_path / "verify_move.csv"

    result = verify_move(manifests, output_root, log_path)

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
    manifests, plan_path, _nas_one, _nas_two, output_root = make_manifest_move_case(tmp_path)
    execute_plan(plan_path, tmp_path / "execute.csv", move=True)
    write(output_root / "extra.txt", b"extra")
    log_path = tmp_path / "verify_move.csv"

    result = verify_move(manifests, output_root, log_path)

    assert result.failed_paths == 0
    assert result.unexpected_outputs == 1
    assert rows(log_path)[-1]["disposition"] == "verify_move_unexpected_output"


def test_verify_move_cli_returns_nonzero_on_failure(tmp_path: Path) -> None:
    manifests, _plan_path, _nas_one, _nas_two, output_root = make_manifest_move_case(tmp_path)
    log_path = tmp_path / "verify_move.csv"

    assert manifest_main(["verify-move", *[str(path) for path in manifests], "--output", str(output_root), "--log", str(log_path)]) == 1
    assert [row for row in rows(log_path) if row["disposition"] == "verify_move_failed"]


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


def test_execute_plan_cli_dry_run_and_move(tmp_path: Path) -> None:
    plan_path, _nas_one, nas_two, output_root = make_move_plan(tmp_path)
    dry_log = tmp_path / "dry.csv"
    move_log = tmp_path / "move.csv"

    assert manifest_main(["execute-plan", str(plan_path), "--log", str(dry_log)]) == 0
    assert (nas_two / "photo.jpg").exists()
    assert manifest_main(["execute-plan", str(plan_path), "--log", str(move_log), "--move"]) == 0

    assert not (nas_two / "photo.jpg").exists()
    assert (output_root / "nas-two" / "photo.jpg").exists()
    assert rows(dry_log)[0]["disposition"] == "planned_duplicate_primary"
    assert rows(move_log)[0]["disposition"] == "moved_duplicate_primary"
