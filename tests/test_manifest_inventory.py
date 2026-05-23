from __future__ import annotations

import csv
from pathlib import Path

import pytest

from dedup_photos.cli import default_manifest_output_path, manifest_main
from dedup_photos.constants import MANIFEST_VERSION
from dedup_photos.manifest import MANIFEST_FIELDS, execute_plan, generate_manifest, plan_from_manifests, verify_manifests, verify_move
from tests.helpers import make_conflict_manifests, make_manifest_move_case, make_move_plan, prepare_nas_root, rows, write, write_csv, write_rows


def test_generate_manifest_stores_nas_paths_and_sidecars(tmp_path: Path) -> None:
    local_root = tmp_path / "google photos"
    nas_root = tmp_path / "nas" / "google photos"
    manifest_path = tmp_path / "batch.csv"
    write(local_root / "2024" / "photo.HEIC", b"image")
    write(local_root / "2024" / "photo.MOV", b"video")
    write(local_root / "2024" / "photo.HEIC.json", b"metadata")
    write(local_root / "2024" / "photo.supplemental-metadata.json", b"not yet")
    prepare_nas_root(local_root, tmp_path / "nas")

    generate_manifest(local_root, nas_root, manifest_path)

    manifest_rows = rows(manifest_path)
    assert len(manifest_rows) == 4
    by_relative = {row["relative_path"]: row for row in manifest_rows}
    primary = by_relative["2024/photo.HEIC"]
    assert primary["file_role"] == "primary"
    assert primary["status"] == "included"
    assert primary["reason"] == ""
    assert primary["group_id"] == "f000001"
    assert primary["nas_path"] == str(nas_root / "2024" / "photo.HEIC")
    assert primary["size_bytes"] == "5"
    assert primary["xxh128"]

    takeout_json = by_relative["2024/photo.HEIC.json"]
    live_video = by_relative["2024/photo.MOV"]
    assert takeout_json["file_role"] == "sidecar"
    assert live_video["file_role"] == "sidecar"
    assert takeout_json["group_id"] == primary["group_id"]
    assert live_video["group_id"] == primary["group_id"]
    assert takeout_json["primary_nas_path"] == primary["nas_path"]
    assert live_video["primary_relative_path"] == "2024/photo.HEIC"
    assert takeout_json["xxh128"]
    assert live_video["xxh128"]

    uncategorized = by_relative["2024/photo.supplemental-metadata.json"]
    assert uncategorized["file_role"] == "uncategorized"
    assert uncategorized["status"] == "skipped"
    assert uncategorized["reason"] == "unrecognized_non_primary"
    assert uncategorized["group_id"] == ""
    assert uncategorized["xxh128"]

def test_generate_manifest_writes_debug_friendly_header_order(tmp_path: Path) -> None:
    local_root = tmp_path / "google_photos"
    nas_root = tmp_path / "nas" / "google_photos"
    manifest_path = tmp_path / "manifest.csv"
    write(local_root / "photo.jpg", b"image")
    prepare_nas_root(local_root, tmp_path / "nas")

    generate_manifest(local_root, nas_root, manifest_path)

    header = manifest_path.read_text(encoding="utf-8").splitlines()[0].split(",")
    assert header == [
        "relative_path",
        "file_role",
        "status",
        "reason",
        "group_id",
        "primary_relative_path",
        "size_bytes",
        "xxh128",
        "nas_path",
        "primary_nas_path",
        "manifest_version",
        "created_at",
        "nas_root_label",
        "batch_root",
        "nas_root",
    ]

def test_generate_manifest_marks_ambiguous_sidecar_as_uncategorized(tmp_path: Path) -> None:
    local_root = tmp_path / "google_photos"
    nas_root = tmp_path / "nas" / "google_photos"
    manifest_path = tmp_path / "manifest.csv"
    write(local_root / "photo.jpg", b"image-one")
    write(local_root / "photo.jpg.png", b"image-two")
    write(local_root / "photo.jpg.json", b"metadata")
    prepare_nas_root(local_root, tmp_path / "nas")

    generate_manifest(local_root, nas_root, manifest_path)

    ambiguous = [row for row in rows(manifest_path) if row["relative_path"] == "photo.jpg.json"][0]
    assert ambiguous["file_role"] == "uncategorized"
    assert ambiguous["status"] == "skipped"
    assert ambiguous["reason"] == "ambiguous_sidecar_multiple_primaries"
    assert ambiguous["group_id"] == ""
    assert ambiguous["xxh128"]

def test_generate_manifest_marks_unrelated_non_primary_as_uncategorized(tmp_path: Path) -> None:
    local_root = tmp_path / "google_photos"
    nas_root = tmp_path / "nas" / "google_photos"
    manifest_path = tmp_path / "manifest.csv"
    write(local_root / "photo.jpg", b"image")
    write(local_root / "unrelated.json", b"metadata")
    prepare_nas_root(local_root, tmp_path / "nas")

    generate_manifest(local_root, nas_root, manifest_path)

    unrelated = [row for row in rows(manifest_path) if row["relative_path"] == "unrelated.json"][0]
    assert unrelated["file_role"] == "uncategorized"
    assert unrelated["status"] == "skipped"
    assert unrelated["reason"] == "unrecognized_non_primary"
    assert unrelated["group_id"] == ""

def test_generate_manifest_dedupes_sidecar_match_paths(tmp_path: Path) -> None:
    local_root = tmp_path / "google_photos"
    nas_root = tmp_path / "nas" / "google_photos"
    manifest_path = tmp_path / "manifest.csv"
    write(local_root / "photo.jpg", b"image")
    write(local_root / "photo.jpg.json", b"metadata")
    prepare_nas_root(local_root, tmp_path / "nas")

    generate_manifest(local_root, nas_root, manifest_path)

    manifest_rows = rows(manifest_path)
    primary = [row for row in manifest_rows if row["file_role"] == "primary"][0]
    sidecars = [row for row in manifest_rows if row["file_role"] == "sidecar"]
    assert len(sidecars) == 1
    assert sidecars[0]["relative_path"] == "photo.jpg.json"
    assert sidecars[0]["group_id"] == primary["group_id"]

def test_generate_manifest_refuses_existing_manifest_path(tmp_path: Path) -> None:
    local_root = tmp_path / "google_photos"
    nas_root = tmp_path / "nas" / "google_photos"
    manifest_path = tmp_path / "google_photos.manifest.csv"
    write(local_root / "2024" / "photo.jpg", b"image")
    prepare_nas_root(local_root, tmp_path / "nas")
    manifest_path.write_text("do not overwrite\n", encoding="utf-8")

    with pytest.raises(ValueError, match="manifest already exists"):
        generate_manifest(local_root, nas_root, manifest_path)

    assert manifest_path.read_text(encoding="utf-8") == "do not overwrite\n"

def test_generate_manifest_requires_existing_nas_root(tmp_path: Path) -> None:
    local_root = tmp_path / "google_photos"
    write(local_root / "photo.jpg", b"image")

    with pytest.raises(ValueError, match="NAS root does not exist"):
        generate_manifest(local_root, tmp_path / "nas" / "google_photos", tmp_path / "manifest.csv")

def test_generate_manifest_requires_nas_root_directory(tmp_path: Path) -> None:
    local_root = tmp_path / "google_photos"
    nas_root = write(tmp_path / "nas" / "google_photos", b"not a directory")
    write(local_root / "photo.jpg", b"image")

    with pytest.raises(ValueError, match="NAS root is not a directory"):
        generate_manifest(local_root, nas_root, tmp_path / "manifest.csv")

def test_generate_manifest_requires_matching_root_basename(tmp_path: Path) -> None:
    local_root = tmp_path / "google_photos"
    nas_root = tmp_path / "nas" / "not_google_photos"
    write(local_root / "photo.jpg", b"image")
    nas_root.mkdir(parents=True)

    with pytest.raises(ValueError, match="basename must match"):
        generate_manifest(local_root, nas_root, tmp_path / "manifest.csv")

def test_generate_manifest_requires_matching_directory_structure_to_depth_two(tmp_path: Path) -> None:
    local_root = tmp_path / "google_photos"
    nas_root = tmp_path / "nas" / "google_photos"
    write(local_root / "2024" / "May" / "photo.jpg", b"image")
    (nas_root / "2024").mkdir(parents=True)

    with pytest.raises(ValueError, match="missing local directories"):
        generate_manifest(local_root, nas_root, tmp_path / "manifest.csv")

    (nas_root / "2024" / "May").mkdir(parents=True)
    (nas_root / "2025").mkdir()
    with pytest.raises(ValueError, match="extra directories"):
        generate_manifest(local_root, nas_root, tmp_path / "manifest.csv")

def test_generate_manifest_ignores_structure_differences_below_depth_two(tmp_path: Path) -> None:
    local_root = tmp_path / "google_photos"
    nas_root = tmp_path / "nas" / "google_photos"
    manifest_path = tmp_path / "manifest.csv"
    write(local_root / "2024" / "May" / "local_only" / "photo.jpg", b"image")
    (nas_root / "2024" / "May" / "nas_only").mkdir(parents=True)

    generate_manifest(local_root, nas_root, manifest_path)

    assert rows(manifest_path)[0]["relative_path"] == "2024/May/local_only/photo.jpg"

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
            "group_id": "f000001",
            "file_role": "primary",
            "status": "included",
            "nas_path": "/nas/photo.jpg",
            "relative_path": "photo.jpg",
            "size_bytes": "1",
            "xxh128": "abc",
        }
    )
    write_rows(manifest_path, [row])

    with pytest.raises(ValueError, match="unsupported manifest version"):
        plan_from_manifests([manifest_path], Path("/dupes"), tmp_path / "plan.csv")

def test_load_manifest_rejects_sidecar_without_primary(tmp_path: Path) -> None:
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
            "group_id": "f000001",
            "file_role": "sidecar",
            "status": "included",
            "nas_path": "/nas/photo.mov",
            "relative_path": "photo.mov",
            "primary_nas_path": "/nas/photo.jpg",
            "primary_relative_path": "photo.jpg",
            "size_bytes": "1",
            "xxh128": "abc",
        }
    )
    write_rows(manifest_path, [row])

    with pytest.raises(ValueError, match="no primary"):
        plan_from_manifests([manifest_path], Path("/dupes"), tmp_path / "plan.csv")

def test_empty_manifest_writes_empty_plan(tmp_path: Path) -> None:
    manifest_path = tmp_path / "empty.csv"
    with manifest_path.open("w", newline="", encoding="utf-8") as file:
        csv.DictWriter(file, fieldnames=MANIFEST_FIELDS).writeheader()

    result = plan_from_manifests([manifest_path], Path("/dupes"), tmp_path / "plan.csv")

    assert result.duplicate_groups == 0
    assert result.duplicate_files == 0
    assert rows(tmp_path / "plan.csv") == []
