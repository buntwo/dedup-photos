from __future__ import annotations

import csv
from pathlib import Path

import pytest

from dedup_photos.cli import default_manifest_output_path, manifest_main
from tests.helpers import make_move_case, prepare_nas_root, rows, write


def test_manifest_cli_refuses_existing_default_manifest_path(tmp_path: Path) -> None:
    local_root = tmp_path / "google_photos"
    nas_root = tmp_path / "nas" / "google_photos"
    default_manifest = tmp_path / "google_photos.manifest.csv"
    write(local_root / "2024" / "photo.jpg", b"image")
    prepare_nas_root(local_root, tmp_path / "nas")
    default_manifest.write_text("do not overwrite\n", encoding="utf-8")

    with pytest.raises(SystemExit) as exit_info:
        manifest_main(["manifest", str(local_root), "--nas-root", str(nas_root)])

    assert exit_info.value.code == 2
    assert default_manifest.read_text(encoding="utf-8") == "do not overwrite\n"

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

def test_analyze_plan_cli_prints_move_summary(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    case = make_move_case(tmp_path)

    assert manifest_main(["analyze-plan", str(case.plan_path)]) == 0

    output = capsys.readouterr().out
    assert "Duplicate-output moves:" in output
    assert "files: 1" in output
    assert "Sidecar merges into keeper directories:" in output
    assert "By file_role:" in output

def test_manifest_cli_subcommands_emit_progress(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    local_one = tmp_path / "one"
    local_two = tmp_path / "two"
    nas_root = tmp_path / "nas"
    manifest_one = tmp_path / "one.csv"
    manifest_two = tmp_path / "two.csv"
    plan_log = tmp_path / "plan.csv"
    execute_log = tmp_path / "execute.csv"
    verify_log = tmp_path / "verify.csv"
    move_verify_log = tmp_path / "move_verify.csv"
    output_root = tmp_path / "dupes"
    write(local_one / "a.jpg", b"same")
    write(local_two / "b.jpg", b"same")
    write(nas_root / "one" / "a.jpg", b"same")
    write(nas_root / "two" / "b.jpg", b"same")

    assert manifest_main(["manifest", str(local_one), "--nas-root", str(nas_root / "one"), "--manifest", str(manifest_one)]) == 0
    progress = capsys.readouterr().err
    assert "manifest: validating roots" in progress
    assert "manifest: checking NAS directory structure" in progress
    assert "manifest-scan" in progress
    assert "files_seen=1" in progress
    assert "manifest-hash" in progress
    assert "done=100.0%" in progress
    assert "primaries_hashed=1" in progress
    assert "planned_moves" not in progress
    assert "manifests_loaded" not in progress
    assert manifest_main(["manifest", str(local_two), "--nas-root", str(nas_root / "two"), "--manifest", str(manifest_two)]) == 0
    assert "primaries_hashed=1" in capsys.readouterr().err
    assert manifest_main(["plan", str(manifest_one), str(manifest_two), "--output", str(output_root), "--log", str(plan_log)]) == 0
    progress = capsys.readouterr().err
    assert "manifest-plan" in progress
    assert "done=100.0%" in progress
    assert "planned_moves=1" in progress
    assert "primaries_hashed" not in progress
    assert manifest_main(["verify-bytes", str(manifest_one), str(manifest_two), "--log", str(verify_log)]) == 0
    progress = capsys.readouterr().err
    assert "manifest-verify-bytes" in progress
    assert "done=100.0%" in progress
    assert "groups_checked=1" in progress
    assert "planned_moves" not in progress
    assert manifest_main(["execute-plan", str(plan_log), "--move", "--log", str(execute_log)]) == 0
    progress = capsys.readouterr().err
    assert "manifest-execute" in progress
    assert "done=100.0%" in progress
    assert "moved_files=1" in progress
    assert "bytes_hashed" not in progress
    assert manifest_main(["verify-move", str(manifest_one), str(manifest_two), "--output", str(output_root), "--log", str(move_verify_log)]) == 0
    progress = capsys.readouterr().err
    assert "manifest-verify-move" in progress
    assert "manifest-output-scan" in progress
    assert "done=100.0%" in progress
    assert "paths_checked=3" in progress
    assert "planned_moves" not in progress

def test_manifest_cli_default_manifest_path(tmp_path: Path) -> None:
    local_root = tmp_path / "google_photos"
    nas_root = tmp_path / "nas" / "google_photos"
    default_manifest = tmp_path / "google_photos.manifest.csv"
    write(local_root / "2021" / "img.jpg", b"image")
    prepare_nas_root(local_root, tmp_path / "nas")

    assert manifest_main(["manifest", str(local_root), "--nas-root", str(nas_root)]) == 0

    assert default_manifest.exists()
    assert rows(default_manifest)[0]["nas_path"] == str(nas_root / "2021" / "img.jpg")

def test_default_manifest_output_path_appends_manifest_suffix() -> None:
    assert default_manifest_output_path(Path("/local/project/google_photos")) == Path(
        "/local/project/google_photos.manifest.csv"
    )

def test_manifest_help_lists_manifest_commands(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exit_info:
        manifest_main(["--help"])

    assert exit_info.value.code == 0
    help_text = capsys.readouterr().out
    assert "manifest" in help_text
    assert "plan" in help_text
    assert "verify-bytes" in help_text
    assert "analyze-json-sidecars" in help_text
    assert "execute-plan" in help_text
    assert "verify-move" in help_text
    assert "Typical workflow" in help_text


def test_json_sidecar_cli_help_and_flags(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exit_info:
        manifest_main(["plan", "--help"])

    assert exit_info.value.code == 0
    assert "--ignore-json-sidecar-fields" in capsys.readouterr().out

    with pytest.raises(SystemExit) as exit_info:
        manifest_main(["analyze-json-sidecars", "--help"])

    assert exit_info.value.code == 0
    assert "JSON sidecar" in capsys.readouterr().out

    with pytest.raises(SystemExit) as exit_info:
        manifest_main(["verify-move", "--help"])

    assert exit_info.value.code == 0
    assert "--ignore-json-sidecar-fields" in capsys.readouterr().out


def test_analyze_json_sidecars_cli(tmp_path: Path) -> None:
    local_one = tmp_path / "one"
    local_two = tmp_path / "two"
    nas_root = tmp_path / "nas"
    manifest_one = tmp_path / "one.csv"
    manifest_two = tmp_path / "two.csv"
    log_path = tmp_path / "json_analysis.csv"
    write(local_one / "photo.jpg", b"same")
    write(local_one / "photo.jpg.json", b'{"description":"left"}')
    write(local_two / "photo.jpg", b"same")
    write(local_two / "photo.jpg.json", b'{"description":"right"}')
    write(nas_root / "one" / "photo.jpg", b"same")
    write(nas_root / "one" / "photo.jpg.json", b'{"description":"left"}')
    write(nas_root / "two" / "photo.jpg", b"same")
    write(nas_root / "two" / "photo.jpg.json", b'{"description":"right"}')

    assert manifest_main(["manifest", str(local_one), "--nas-root", str(nas_root / "one"), "--manifest", str(manifest_one)]) == 0
    assert manifest_main(["manifest", str(local_two), "--nas-root", str(nas_root / "two"), "--manifest", str(manifest_two)]) == 0
    assert manifest_main(["analyze-json-sidecars", str(manifest_one), str(manifest_two), "--log", str(log_path)]) == 0

    assert {row["json_key"] for row in rows(log_path)} == {"description"}


def test_execute_plan_help_lists_hash_verification_opt_out(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exit_info:
        manifest_main(["execute-plan", "--help"])

    assert exit_info.value.code == 0
    help_text = capsys.readouterr().out
    assert "--no-verify-source-hashes" in help_text
    assert "--verify-source-hashes" not in help_text

def test_verify_move_cli_returns_nonzero_on_failure(tmp_path: Path) -> None:
    case = make_move_case(tmp_path)
    log_path = tmp_path / "verify_move.csv"

    assert manifest_main(
        ["verify-move", *[str(path) for path in case.manifests], "--output", str(case.output_root), "--log", str(log_path)]
    ) == 1
    assert [row for row in rows(log_path) if row["disposition"] == "verify_move_failed"]

def test_execute_plan_cli_dry_run_and_move(tmp_path: Path) -> None:
    case = make_move_case(tmp_path)
    dry_log = tmp_path / "dry.csv"
    move_log = tmp_path / "move.csv"

    assert manifest_main(["execute-plan", str(case.plan_path), "--log", str(dry_log)]) == 0
    assert (case.nas_two / "photo.jpg").exists()
    assert manifest_main(["execute-plan", str(case.plan_path), "--log", str(move_log), "--move"]) == 0

    assert not (case.nas_two / "photo.jpg").exists()
    assert (case.output_root / "nas-two" / "photo.jpg").exists()
    dry_duplicate = [row for row in rows(dry_log) if row["file_role"] == "primary"][0]
    move_duplicate = [row for row in rows(move_log) if row["file_role"] == "primary"][0]
    assert rows(dry_log)[0]["disposition"] == "verified_keeper"
    assert dry_duplicate["disposition"] == "planned_duplicate_primary"
    assert move_duplicate["disposition"] == "moved_duplicate_primary"

def test_execute_plan_cli_move_verifies_source_hashes_by_default(tmp_path: Path) -> None:
    case = make_move_case(tmp_path)
    (case.nas_two / "photo.jpg").write_bytes(b"diff")
    log_path = tmp_path / "execute.csv"

    assert manifest_main(["execute-plan", str(case.plan_path), "--move", "--log", str(log_path)]) == 1

    assert (case.nas_two / "photo.jpg").exists()
    assert not (case.output_root / "nas-two" / "photo.jpg").exists()
    failure = [row for row in rows(log_path) if row["file_role"] == "primary"][0]
    assert failure["reason"] == "source_hash_mismatch"
    assert failure["hash_check"] == "mismatched"

def test_execute_plan_cli_hash_verification_can_be_opted_out(tmp_path: Path) -> None:
    case = make_move_case(tmp_path)
    (case.nas_two / "photo.jpg").write_bytes(b"diff")
    log_path = tmp_path / "execute.csv"

    assert manifest_main(
        ["execute-plan", str(case.plan_path), "--move", "--no-verify-source-hashes", "--log", str(log_path)]
    ) == 0

    assert not (case.nas_two / "photo.jpg").exists()
    assert (case.output_root / "nas-two" / "photo.jpg").read_bytes() == b"diff"
    moved = [row for row in rows(log_path) if row["file_role"] == "primary"][0]
    assert moved["disposition"] == "moved_duplicate_primary"
    assert moved["hash_check"] == "not_checked"
