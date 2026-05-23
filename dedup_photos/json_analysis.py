from __future__ import annotations

import csv
import hashlib
import json
from collections import Counter
from pathlib import Path
from typing import Any

from dedup_photos.common import default_log_path
from dedup_photos.manifest_io import load_manifests
from dedup_photos.models import JsonSidecarAnalysisResult, ManifestEntry
from dedup_photos.planning import choose_manifest_keeper, manifest_duplicate_groups
from dedup_photos.progress import Progress


JSON_ANALYSIS_FIELDS = [
    "group_id",
    "json_key",
    "status",
    "primary_source_path",
    "source_path",
    "json_value",
    "value_sha256",
    "value_count_in_group",
    "json_sidecar_count",
    "reason",
    "message",
]


def analyze_json_sidecars(
    manifest_paths: list[Path],
    log_path: Path | None,
    show_progress: bool = False,
) -> JsonSidecarAnalysisResult:
    progress = Progress(mode="json_sidecar_analysis", total_images=0, enabled=show_progress)
    try:
        progress.start_phase("manifest-load", len(manifest_paths))
        entries = load_manifests(manifest_paths, progress)
        groups, _uniques = manifest_duplicate_groups(entries)
        conflict_groups = [
            (f"m{index:06d}", group)
            for index, group in enumerate(groups, start=1)
            if choose_manifest_keeper(group) is None and manifest_group_has_json_sidecars(group)
        ]
        progress.start_phase("json-sidecar-analysis", len(conflict_groups))
        actual_log_path = log_path or default_log_path()
        differing_groups = 0
        differing_key_count = 0
        parse_errors = 0

        actual_log_path.parent.mkdir(parents=True, exist_ok=True)
        with actual_log_path.open("w", newline="", encoding="utf-8") as file:
            writer = csv.DictWriter(file, fieldnames=JSON_ANALYSIS_FIELDS)
            writer.writeheader()
            for group_id, group in conflict_groups:
                result = analyze_json_sidecar_group(writer, group_id, group)
                if result.differing_keys:
                    differing_groups += 1
                    differing_key_count += result.differing_keys
                parse_errors += result.parse_errors
                progress.advance()

        return JsonSidecarAnalysisResult(
            log_path=actual_log_path,
            analyzed_groups=len(conflict_groups),
            differing_groups=differing_groups,
            differing_keys=differing_key_count,
            parse_errors=parse_errors,
        )
    finally:
        progress.finish()


class JsonGroupResult:
    def __init__(self, differing_keys: int, parse_errors: int) -> None:
        self.differing_keys = differing_keys
        self.parse_errors = parse_errors


def analyze_json_sidecar_group(writer: csv.DictWriter, group_id: str, group: list[ManifestEntry]) -> JsonGroupResult:
    sidecars = json_sidecars_for_group(group)
    parsed: list[tuple[ManifestEntry, Path, dict[str, Any]]] = []
    parse_errors = 0
    for entry, path in sidecars:
        try:
            parsed_json = load_json_object(path)
        except (OSError, json.JSONDecodeError, ValueError) as error:
            parse_errors += 1
            writer.writerow(
                analysis_row(
                    group_id=group_id,
                    status="error",
                    entry=entry,
                    path=path,
                    reason="json_parse_error",
                    message=str(error),
                    json_sidecar_count=len(sidecars),
                )
            )
            continue
        parsed.append((entry, path, parsed_json))

    differing_keys = json_differing_top_level_keys([data for _entry, _path, data in parsed])
    for key in differing_keys:
        values = [json_value_text(data.get(key)) for _entry, _path, data in parsed]
        value_counts = Counter(values)
        for (entry, path, _data), value_text in zip(parsed, values, strict=True):
            writer.writerow(
                analysis_row(
                    group_id=group_id,
                    status="differing",
                    entry=entry,
                    path=path,
                    json_key=key,
                    json_value=value_text,
                    value_sha256=hashlib.sha256(value_text.encode("utf-8")).hexdigest(),
                    value_count_in_group=value_counts[value_text],
                    json_sidecar_count=len(sidecars),
                    reason="json_key_differs",
                )
            )
    return JsonGroupResult(differing_keys=len(differing_keys), parse_errors=parse_errors)


def manifest_group_has_json_sidecars(group: list[ManifestEntry]) -> bool:
    return any(path.suffix.lower() == ".json" for entry in group for path in entry.sidecar_paths)


def json_sidecars_for_group(group: list[ManifestEntry]) -> list[tuple[ManifestEntry, Path]]:
    return [
        (entry, path)
        for entry in sorted(group, key=lambda item: (item.nas_root_label.lower(), item.relative_path.as_posix().lower()))
        for path in entry.sidecar_paths
        if path.suffix.lower() == ".json"
    ]


def load_json_object(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as file:
        data = json.load(file)
    if not isinstance(data, dict):
        raise ValueError("JSON sidecar root is not an object")
    return data


def json_differing_top_level_keys(items: list[dict[str, Any]]) -> list[str]:
    if len(items) < 2:
        return []
    keys = sorted(set().union(*(item.keys() for item in items)))
    return [key for key in keys if len({json_value_text(item.get(key)) for item in items}) > 1]


def json_value_text(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def analysis_row(
    *,
    group_id: str,
    status: str,
    entry: ManifestEntry,
    path: Path,
    json_key: str = "",
    json_value: str = "",
    value_sha256: str = "",
    value_count_in_group: int | str = "",
    json_sidecar_count: int,
    reason: str,
    message: str = "",
) -> dict[str, str]:
    return {
        "group_id": group_id,
        "json_key": json_key,
        "status": status,
        "primary_source_path": str(entry.nas_path),
        "source_path": str(path),
        "json_value": json_value,
        "value_sha256": value_sha256,
        "value_count_in_group": str(value_count_in_group),
        "json_sidecar_count": str(json_sidecar_count),
        "reason": reason,
        "message": message,
    }
