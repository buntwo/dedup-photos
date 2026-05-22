from __future__ import annotations

import argparse
from pathlib import Path

from dedup_photos.deduper import default_log_path, run_dedup
from dedup_photos.verifier import run_verify


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Move bit-exact duplicate photos into a parallel output tree.")
    parser.add_argument("inputs", nargs="+", type=Path, help="One or more input photo library roots.")
    parser.add_argument("--output", required=True, type=Path, help="Directory where duplicate files are moved.")
    parser.add_argument("--log", type=Path, default=None, help="CSV log path. Defaults to a timestamped file.")
    parser.add_argument("--move", action="store_true", help="Actually move duplicate files. Default is dry run.")
    parser.add_argument("--verify", action="store_true", help="Verify output primaries are valid duplicates of input primaries.")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    log_path = args.log or default_log_path()
    try:
        if args.verify:
            if args.move:
                parser.error("--verify cannot be combined with --move")
            result = run_verify(args.inputs, args.output, log_path)
            print(
                f"Completed verify; checked={result.checked} matched={result.matched} "
                f"failed={result.failed}; wrote CSV log to {result.log_path}"
            )
            return 1 if result.failed else 0
        written_log = run_dedup(args.inputs, args.output, log_path, args.move)
    except ValueError as error:
        parser.error(str(error))
    mode = "move" if args.move else "dry run"
    print(f"Completed {mode}; wrote CSV log to {written_log}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
