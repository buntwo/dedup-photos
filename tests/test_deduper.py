from __future__ import annotations

import csv
from pathlib import Path

import pytest

import dedup_photos.deduper as deduper
import dedup_photos.verifier as verifier
from dedup_photos.deduper import run_dedup
from dedup_photos.verifier import run_verify


def write(path: Path, contents: bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(contents)
    return path


def rows(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as file:
        return list(csv.DictReader(file))


def run(tmp_path: Path, inputs: list[Path], output: Path, *, move: bool = False) -> list[dict[str, str]]:
    log_path = tmp_path / "log.csv"
    run_dedup(inputs, output, log_path, move)
    return rows(log_path)


def events(log_rows: list[dict[str, str]], event: str) -> list[dict[str, str]]:
    return [row for row in log_rows if row["event"] == event]


def test_dry_run_logs_moves_without_touching_files(tmp_path: Path) -> None:
    input_root = tmp_path / "IN_1"
    output_root = tmp_path / "OUT"
    original = write(input_root / "a" / "one.JPG", b"same")
    duplicate = write(input_root / "b" / "two.jpg", b"same")

    log_rows = run(tmp_path, [input_root], output_root)

    move_rows = events(log_rows, "duplicate_primary_move")
    assert len(move_rows) == 1
    assert move_rows[0]["disposition"] == "planned_duplicate_primary"
    assert move_rows[0]["status"] == "planned"
    assert move_rows[0]["destination_path"] == str(output_root / "IN_1" / "b" / "two.jpg")
    assert original.exists()
    assert duplicate.exists()
    assert not output_root.exists()


def test_move_mode_moves_duplicate_and_sidecars_under_input_label(tmp_path: Path) -> None:
    input_one = tmp_path / "IN_1"
    input_two = tmp_path / "IN_2"
    output_root = tmp_path / "OUT"
    keeper = write(input_one / "foo" / "keep.heic", b"image")
    write(input_one / "foo" / "keep.MOV", b"live video")
    duplicate = write(input_two / "baz" / "dup.HEIC", b"image")
    sidecar = write(input_two / "baz" / "dup.MOV", b"live video")

    log_rows = run(tmp_path, [input_one, input_two], output_root, move=True)

    assert keeper.exists()
    assert not duplicate.exists()
    assert not sidecar.exists()
    assert (output_root / "IN_2" / "baz" / "dup.HEIC").read_bytes() == b"image"
    assert (output_root / "IN_2" / "baz" / "dup.MOV").read_bytes() == b"live video"
    assert events(log_rows, "duplicate_primary_move")[0]["status"] == "moved"
    assert events(log_rows, "sidecar_move")[0]["file_role"] == "sidecar"


def test_same_size_different_content_is_not_deduped(tmp_path: Path) -> None:
    input_root = tmp_path / "IN"
    output_root = tmp_path / "OUT"
    write(input_root / "one.jpg", b"aaaa")
    write(input_root / "two.jpg", b"bbbb")

    log_rows = run(tmp_path, [input_root], output_root, move=True)

    assert len(events(log_rows, "duplicate_primary_move")) == 0
    assert len(events(log_rows, "unique_primary")) == 2
    assert not output_root.exists()


def test_sidecar_priority_keeps_file_with_sidecar(tmp_path: Path) -> None:
    input_root = tmp_path / "IN"
    output_root = tmp_path / "OUT"
    with_sidecar = write(input_root / "with_sidecar" / "photo.heic", b"same")
    write(input_root / "with_sidecar" / "photo.json", b"{}")
    without_sidecar = write(input_root / "without_sidecar" / "photo.heic", b"same")

    log_rows = run(tmp_path, [input_root], output_root, move=True)

    assert with_sidecar.exists()
    assert not without_sidecar.exists()
    keeper_rows = events(log_rows, "keeper_primary")
    assert keeper_rows[0]["source_path"] == str(with_sidecar)


def test_different_sidecars_skip_whole_group(tmp_path: Path) -> None:
    input_root = tmp_path / "IN"
    output_root = tmp_path / "OUT"
    first = write(input_root / "a" / "photo.heic", b"same")
    second = write(input_root / "b" / "photo.heic", b"same")
    write(input_root / "a" / "photo.mov", b"left")
    write(input_root / "b" / "photo.mov", b"right")

    log_rows = run(tmp_path, [input_root], output_root, move=True)

    assert first.exists()
    assert second.exists()
    assert len(events(log_rows, "duplicate_group_skipped")) == 1
    assert events(log_rows, "duplicate_group_skipped")[0]["reason"] == "unresolved_sidecar_conflict"
    assert not output_root.exists()


def test_dedup_sidecar_comparison_byte_checks_after_hash_match(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    input_root = tmp_path / "IN"
    output_root = tmp_path / "OUT"
    first = write(input_root / "a" / "photo.heic", b"same")
    second = write(input_root / "b" / "photo.heic", b"same")
    write(input_root / "a" / "photo.mov", b"left")
    write(input_root / "b" / "photo.mov", b"LEFT")
    original_hash_file = deduper.hash_file

    def fake_hash_file(path: Path) -> str:
        if path.suffix.lower() == ".mov":
            return "forced-sidecar-collision"
        return original_hash_file(path)

    monkeypatch.setattr(deduper, "hash_file", fake_hash_file)

    log_rows = run(tmp_path, [input_root], output_root, move=True)

    assert first.exists()
    assert second.exists()
    assert events(log_rows, "duplicate_group_skipped")[0]["reason"] == "unresolved_sidecar_conflict"
    assert not output_root.exists()


def test_takeout_non_date_album_beats_photos_from_year(tmp_path: Path) -> None:
    input_root = tmp_path / "IN"
    output_root = tmp_path / "OUT"
    album = write(input_root / "Google Photos" / "20240101 Takeout" / "Sunday Funday" / "photo.jpg", b"same")
    yearly = write(input_root / "Google Photos" / "20240101 Takeout" / "Photos from 2023" / "photo.jpg", b"same")

    log_rows = run(tmp_path, [input_root], output_root, move=True)

    assert album.exists()
    assert not yearly.exists()
    assert events(log_rows, "keeper_primary")[0]["source_path"] == str(album)


def test_fewer_date_like_directory_segments_win(tmp_path: Path) -> None:
    input_root = tmp_path / "IN"
    output_root = tmp_path / "OUT"
    fewer_dates = write(input_root / "Takeout" / "Sunday Funday" / "photo.jpg", b"same")
    more_dates = write(input_root / "Takeout" / "20240101" / "Photos from 2023" / "photo.jpg", b"same")

    log_rows = run(tmp_path, [input_root], output_root, move=True)

    assert fewer_dates.exists()
    assert not more_dates.exists()
    assert events(log_rows, "keeper_primary")[0]["source_path"] == str(fewer_dates)


def test_sidecar_priority_beats_date_directory_penalty(tmp_path: Path) -> None:
    input_root = tmp_path / "IN"
    output_root = tmp_path / "OUT"
    with_sidecar_in_dated_dir = write(input_root / "Takeout" / "20240101" / "photo.jpg", b"same")
    write(input_root / "Takeout" / "20240101" / "photo.mov", b"live")
    without_sidecar = write(input_root / "Takeout" / "Sunday Funday" / "photo.jpg", b"same")

    log_rows = run(tmp_path, [input_root], output_root, move=True)

    assert with_sidecar_in_dated_dir.exists()
    assert not without_sidecar.exists()
    assert events(log_rows, "keeper_primary")[0]["source_path"] == str(with_sidecar_in_dated_dir)


def test_destination_collision_skips_bundle(tmp_path: Path) -> None:
    input_root = tmp_path / "IN"
    output_root = tmp_path / "OUT"
    keep = write(input_root / "a" / "keep.jpg", b"same")
    write(input_root / "a" / "keep.json", b"metadata")
    duplicate = write(input_root / "b" / "dup.jpg", b"same")
    sidecar = write(input_root / "b" / "dup.json", b"metadata")
    write(output_root / "IN" / "b" / "dup.jpg", b"existing")

    log_rows = run(tmp_path, [input_root], output_root, move=True)

    assert keep.exists()
    assert duplicate.exists()
    assert sidecar.exists()
    error_rows = events(log_rows, "move_skipped_destination_exists")
    assert {row["file_role"] for row in error_rows} == {"primary", "sidecar"}
    assert {row["disposition"] for row in error_rows} == {"kept_error"}


def test_duplicate_input_basenames_abort(tmp_path: Path) -> None:
    one = tmp_path / "left" / "Photos"
    two = tmp_path / "right" / "Photos"
    one.mkdir(parents=True)
    two.mkdir(parents=True)

    with pytest.raises(ValueError, match="basenames"):
        run_dedup([one, two], tmp_path / "OUT", tmp_path / "log.csv", move=False)


def test_output_inside_input_aborts(tmp_path: Path) -> None:
    input_root = tmp_path / "IN"
    input_root.mkdir()

    with pytest.raises(ValueError, match="inside input root"):
        run_dedup([input_root], input_root / "OUT", tmp_path / "log.csv", move=False)


def test_symlinked_files_are_ignored(tmp_path: Path) -> None:
    input_root = tmp_path / "IN"
    output_root = tmp_path / "OUT"
    target = write(input_root / "target.jpg", b"same")
    (input_root / "link.jpg").symlink_to(target)

    log_rows = run(tmp_path, [input_root], output_root, move=True)

    assert len(events(log_rows, "unique_primary")) == 1
    assert len(events(log_rows, "duplicate_primary_move")) == 0
    assert target.exists()
    assert not output_root.exists()


def test_verify_succeeds_for_moved_duplicate_with_respected_sidecar_precedence(tmp_path: Path) -> None:
    input_one = tmp_path / "IN_1"
    input_two = tmp_path / "IN_2"
    output_root = tmp_path / "OUT"
    write(input_one / "foo" / "keep.heic", b"image")
    write(input_one / "foo" / "keep.MOV", b"live video")
    write(input_two / "baz" / "dup.HEIC", b"image")
    write(input_two / "baz" / "dup.MOV", b"live video")

    run(tmp_path, [input_one, input_two], output_root, move=True)
    verify_log = tmp_path / "verify.csv"
    result = run_verify([input_one, input_two], output_root, verify_log)

    assert result.checked == 1
    assert result.matched == 1
    assert result.failed == 0
    verify_rows = rows(verify_log)
    assert verify_rows[0]["disposition"] == "verify_matched"


def test_verify_fails_when_output_file_should_have_won_by_sidecar_precedence(tmp_path: Path) -> None:
    input_root = tmp_path / "IN"
    output_root = tmp_path / "OUT"
    write(input_root / "keep.jpg", b"image")
    write(output_root / "IN" / "dup.jpg", b"image")
    write(output_root / "IN" / "dup.mov", b"live video")

    verify_log = tmp_path / "verify.csv"
    result = run_verify([input_root], output_root, verify_log)

    assert result.checked == 1
    assert result.matched == 0
    assert result.failed == 1
    verify_rows = rows(verify_log)
    assert verify_rows[0]["disposition"] == "verify_failed"
    assert verify_rows[0]["reason"] == "moved_file_should_have_been_keeper"


def test_verify_succeeds_when_output_file_lost_by_date_directory_precedence(tmp_path: Path) -> None:
    input_root = tmp_path / "IN"
    output_root = tmp_path / "OUT"
    write(input_root / "Takeout" / "Sunday Funday" / "photo.jpg", b"image")
    write(output_root / "IN" / "Takeout" / "Photos from 2023" / "photo.jpg", b"image")

    verify_log = tmp_path / "verify.csv"
    result = run_verify([input_root], output_root, verify_log)

    assert result.checked == 1
    assert result.matched == 1
    assert result.failed == 0
    assert rows(verify_log)[0]["disposition"] == "verify_matched"


def test_verify_fails_when_output_file_should_have_won_by_date_directory_precedence(tmp_path: Path) -> None:
    input_root = tmp_path / "IN"
    output_root = tmp_path / "OUT"
    write(input_root / "Takeout" / "Photos from 2023" / "photo.jpg", b"image")
    write(output_root / "IN" / "Takeout" / "Sunday Funday" / "photo.jpg", b"image")

    verify_log = tmp_path / "verify.csv"
    result = run_verify([input_root], output_root, verify_log)

    assert result.checked == 1
    assert result.matched == 0
    assert result.failed == 1
    assert rows(verify_log)[0]["reason"] == "moved_file_should_have_been_keeper"


def test_verify_fails_when_output_primary_has_no_equal_input_primary(tmp_path: Path) -> None:
    input_root = tmp_path / "IN"
    output_root = tmp_path / "OUT"
    write(input_root / "keep.jpg", b"input")
    write(output_root / "IN" / "dup.jpg", b"output")

    verify_log = tmp_path / "verify.csv"
    result = run_verify([input_root], output_root, verify_log)

    assert result.checked == 1
    assert result.failed == 1
    assert rows(verify_log)[0]["reason"] == "no_equal_input_primary"


def test_verify_fails_when_sidecars_conflict(tmp_path: Path) -> None:
    input_root = tmp_path / "IN"
    output_root = tmp_path / "OUT"
    write(input_root / "keep.jpg", b"image")
    write(input_root / "keep.json", b"left")
    write(output_root / "IN" / "dup.jpg", b"image")
    write(output_root / "IN" / "dup.json", b"right")

    verify_log = tmp_path / "verify.csv"
    result = run_verify([input_root], output_root, verify_log)

    assert result.checked == 1
    assert result.failed == 1
    assert rows(verify_log)[0]["reason"] == "sidecar_conflict_should_not_have_moved"


def test_verify_sidecar_comparison_byte_checks_after_hash_match(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    input_root = tmp_path / "IN"
    output_root = tmp_path / "OUT"
    write(input_root / "keep.jpg", b"image")
    write(input_root / "keep.json", b"left")
    write(output_root / "IN" / "dup.jpg", b"image")
    write(output_root / "IN" / "dup.json", b"LEFT")
    original_hash_file = verifier.hash_file

    def fake_hash_file(path: Path) -> str:
        if path.suffix.lower() == ".json":
            return "forced-sidecar-collision"
        return original_hash_file(path)

    monkeypatch.setattr(verifier, "hash_file", fake_hash_file)

    verify_log = tmp_path / "verify.csv"
    result = run_verify([input_root], output_root, verify_log)

    assert result.checked == 1
    assert result.failed == 1
    assert rows(verify_log)[0]["reason"] == "sidecar_conflict_should_not_have_moved"
