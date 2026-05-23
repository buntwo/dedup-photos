from __future__ import annotations

import csv
from pathlib import Path

from dedup_photos.manifest import MANIFEST_FIELDS, generate_manifest, plan_from_manifests


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


def prepare_nas_root(local_root: Path, nas_parent: Path) -> Path:
    nas_root = nas_parent / local_root.name
    nas_root.mkdir(parents=True, exist_ok=True)
    for path in local_root.rglob("*"):
        if path.is_dir() and not path.is_symlink():
            (nas_root / path.relative_to(local_root)).mkdir(parents=True, exist_ok=True)
    return nas_root


def make_move_plan(
    tmp_path: Path,
    *,
    with_sidecars: bool = False,
    full_primary_prefix_sidecar: bool = False,
) -> tuple[Path, Path, Path, Path]:
    nas_one = tmp_path / "nas-one"
    nas_two = tmp_path / "nas-two"
    output_root = tmp_path / "dupes"
    manifest_one = tmp_path / "one.csv"
    manifest_two = tmp_path / "two.csv"
    plan_path = tmp_path / "plan.csv"
    write(nas_one / "photo.jpg", b"same")
    write(nas_two / "photo.jpg", b"same")
    if with_sidecars:
        sidecar_name = "photo.jpg.json" if full_primary_prefix_sidecar else "photo.mov"
        write(nas_one / sidecar_name, b"live")
        write(nas_two / sidecar_name, b"live")
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
