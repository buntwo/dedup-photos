from __future__ import annotations

from pathlib import Path

import xxhash

from dedup_photos.constants import HASH_CHUNK_SIZE
from dedup_photos.progress import Progress


def hash_file_xxh128(path: Path, progress: Progress | None = None, file_role: str = "primary") -> str:
    hasher = xxhash.xxh128()
    with path.open("rb", buffering=0) as file:
        while chunk := file.read(HASH_CHUNK_SIZE):
            hasher.update(chunk)
            if progress is not None:
                progress.manifest_hash_bytes(len(chunk))
    if progress is not None:
        progress.manifest_file_hashed(file_role)
    return hasher.hexdigest()


def files_equal_by_path(left: Path, right: Path, progress: Progress | None = None) -> bool:
    if left.stat().st_size != right.stat().st_size:
        return False
    with left.open("rb", buffering=0) as left_file, right.open("rb", buffering=0) as right_file:
        while True:
            left_chunk = left_file.read(HASH_CHUNK_SIZE)
            right_chunk = right_file.read(HASH_CHUNK_SIZE)
            if progress is not None:
                progress.manifest_compare_bytes(len(left_chunk) + len(right_chunk))
            if left_chunk != right_chunk:
                return False
            if not left_chunk:
                return True
