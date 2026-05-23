from __future__ import annotations

from collections import defaultdict
from pathlib import Path

from dedup_photos.common import is_primary_image
from dedup_photos.progress import Progress


def regular_files_by_directory(root: Path, progress: Progress | None = None) -> dict[Path, list[Path]]:
    files_by_directory: dict[Path, list[Path]] = defaultdict(list)
    for path in root.rglob("*"):
        if path.is_symlink() or not path.is_file():
            continue
        files_by_directory[path.parent].append(path)
        if progress is not None:
            progress.manifest_entry_scanned()
    return files_by_directory


def classify_manifest_files(
    files_by_directory: dict[Path, list[Path]],
    primary_paths: tuple[Path, ...],
) -> tuple[dict[Path, list[Path]], dict[Path, str]]:
    sidecars_by_primary: dict[Path, list[Path]] = {path: [] for path in primary_paths}
    uncategorized: dict[Path, str] = {}
    for paths in files_by_directory.values():
        directory_primaries = [path for path in paths if is_primary_image(path)]
        primary_set = set(directory_primaries)
        stem_index: dict[str, list[Path]] = defaultdict(list)
        full_name_index: dict[str, list[Path]] = defaultdict(list)
        for primary in directory_primaries:
            stem_index[primary.stem].append(primary)
            full_name_index[primary.name.lower()].append(primary)
        for path in paths:
            if path in primary_set:
                continue
            matches = sidecar_matches_for(path, stem_index, full_name_index)
            if len(matches) == 1:
                sidecars_by_primary[matches[0]].append(path)
            elif len(matches) > 1:
                uncategorized[path] = "ambiguous_sidecar_multiple_primaries"
            else:
                uncategorized[path] = "unrecognized_non_primary"
    return sidecars_by_primary, uncategorized


def sidecar_matches_for(
    candidate: Path,
    stem_index: dict[str, list[Path]],
    full_name_index: dict[str, list[Path]],
) -> list[Path]:
    matches: dict[Path, None] = {}
    for primary in stem_index.get(candidate.stem, []):
        matches[primary] = None
    for prefix in candidate_full_name_prefixes(candidate.name):
        for primary in full_name_index.get(prefix.lower(), []):
            matches[primary] = None
    return list(matches)


def candidate_full_name_prefixes(name: str) -> list[str]:
    prefixes = []
    current = name
    while "." in current:
        current = current.rsplit(".", 1)[0]
        prefixes.append(current)
    return prefixes
