from __future__ import annotations

import argparse
import random
import statistics
import time
from pathlib import Path
from typing import Callable

import xxhash

from dedup_photos.constants import HASH_CHUNK_SIZE


VARIANTS: dict[str, Callable[[], object]] = {
    "xxh32": xxhash.xxh32,
    "xxh64": xxhash.xxh64,
    "xxh3_64": xxhash.xxh3_64,
    "xxh3_128": xxhash.xxh3_128,
    "xxh128": xxhash.xxh128,
}


def iter_files(root: Path) -> list[Path]:
    return sorted(path for path in root.rglob("*") if path.is_file())


def hash_files(paths: list[Path], constructor: Callable[[], object], chunk_size: int) -> int:
    total_bytes = 0
    for path in paths:
        hasher = constructor()
        with path.open("rb", buffering=0) as file:
            while chunk := file.read(chunk_size):
                hasher.update(chunk)
                total_bytes += len(chunk)
        hasher.digest()
    return total_bytes


def read_files(paths: list[Path], chunk_size: int) -> int:
    total_bytes = 0
    for path in paths:
        with path.open("rb", buffering=0) as file:
            while chunk := file.read(chunk_size):
                total_bytes += len(chunk)
    return total_bytes


def profile(root: Path, rounds: int, chunk_size: int) -> None:
    paths = iter_files(root)
    total_bytes = sum(path.stat().st_size for path in paths)
    print(f"library={root}")
    print(f"files={len(paths)} bytes={total_bytes} mib={total_bytes / 1024 / 1024:.2f}")
    print(f"chunk_size={chunk_size}")
    print()

    results: dict[str, list[float]] = {"read_only": []} | {name: [] for name in VARIANTS}
    names = list(results)
    for round_number in range(1, rounds + 1):
        random.Random(round_number).shuffle(names)
        for name in names:
            started = time.perf_counter()
            if name == "read_only":
                seen_bytes = read_files(paths, chunk_size)
            else:
                seen_bytes = hash_files(paths, VARIANTS[name], chunk_size)
            elapsed = time.perf_counter() - started
            if seen_bytes != total_bytes:
                raise RuntimeError(f"byte count mismatch for {name}: {seen_bytes} != {total_bytes}")
            results[name].append(elapsed)

    print(f"{'variant':<10} {'digest_bits':>11} {'median_ms':>10} {'min_ms':>10} {'mib_s':>10}")
    for name, times in sorted(results.items(), key=lambda item: statistics.median(item[1])):
        median = statistics.median(times)
        best = min(times)
        mib_s = (total_bytes / 1024 / 1024) / median
        digest_bits = 0 if name == "read_only" else 8 * len(VARIANTS[name]().digest())
        print(f"{name:<10} {digest_bits:>11} {median * 1000:>10.2f} {best * 1000:>10.2f} {mib_s:>10.2f}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("library", type=Path)
    parser.add_argument("--rounds", type=int, default=7)
    parser.add_argument("--chunk-size", type=int, default=HASH_CHUNK_SIZE)
    args = parser.parse_args()

    if not args.library.is_dir():
        raise SystemExit(f"not a directory: {args.library}")
    profile(args.library, args.rounds, args.chunk_size)


if __name__ == "__main__":
    main()
