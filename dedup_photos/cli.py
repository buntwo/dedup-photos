from __future__ import annotations

import argparse
import sys
from pathlib import Path

from dedup_photos.deduper import default_log_path, run_dedup
from dedup_photos.manifest import execute_plan, generate_manifest, plan_from_manifests, verify_manifests, verify_move
from dedup_photos.verifier import run_verify


COMMANDS = {"manifest", "plan-from-manifests", "verify-manifests", "execute-plan", "verify-move"}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Move bit-exact duplicate photos into a parallel output tree.")
    parser.add_argument("inputs", nargs="+", type=Path, help="One or more input photo library roots.")
    parser.add_argument("--output", required=True, type=Path, help="Directory where duplicate files are moved.")
    parser.add_argument("--log", type=Path, default=None, help="CSV log path. Defaults to a timestamped file.")
    parser.add_argument("--move", action="store_true", help="Actually move duplicate files. Default is dry run.")
    parser.add_argument("--verify", action="store_true", help="Verify output primaries are valid duplicates of input primaries.")
    return parser


def build_command_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Deduplicate photos directly or through batch manifests.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Direct dedup mode is also supported:\n"
            "  dedup-photos INPUT_ROOT [INPUT_ROOT ...] --output DUPLICATE_OUTPUT_DIR [--move]\n"
            "  dedup-photos INPUT_ROOT [INPUT_ROOT ...] --output DUPLICATE_OUTPUT_DIR --verify"
        ),
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    manifest = subparsers.add_parser("manifest", help="Hash a local batch and write a CSV manifest with NAS paths.")
    manifest.add_argument("local_batch_root", type=Path)
    manifest.add_argument("--nas-root", required=True, type=Path)
    manifest.add_argument("--manifest", required=True, type=Path)

    plan = subparsers.add_parser("plan-from-manifests", help="Compute a duplicate move plan from CSV manifests.")
    plan.add_argument("manifests", nargs="+", type=Path)
    plan.add_argument("--output", required=True, type=Path)
    plan.add_argument("--log", type=Path, default=None)

    verify = subparsers.add_parser("verify-manifests", help="Byte-check duplicate groups referenced by manifests.")
    verify.add_argument("manifests", nargs="+", type=Path)
    verify.add_argument("--log", type=Path, default=None)
    verify.add_argument("--byte-check", action="store_true", help="Read NAS files and byte-check same-hash groups.")

    execute = subparsers.add_parser("execute-plan", help="Validate or execute a manifest move plan.")
    execute.add_argument("plan", type=Path)
    execute.add_argument("--log", type=Path, default=None)
    execute.add_argument("--move", action="store_true", help="Actually move files. Default is dry run.")

    verify_move_parser = subparsers.add_parser("verify-move", help="Verify manifest-planned moves were executed.")
    verify_move_parser.add_argument("manifests", nargs="+", type=Path)
    verify_move_parser.add_argument("--output", required=True, type=Path)
    verify_move_parser.add_argument("--log", type=Path, default=None)
    return parser


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and (argv[0] in COMMANDS or argv[0] in {"-h", "--help"}):
        return command_main(argv)

    parser = build_parser()
    args = parser.parse_args(argv)
    log_path = args.log or default_log_path()
    try:
        if args.verify:
            if args.move:
                parser.error("--verify cannot be combined with --move")
            result = run_verify(args.inputs, args.output, log_path, show_progress=True)
            print(
                f"Completed verify; checked={result.checked} matched={result.matched} "
                f"failed={result.failed}; wrote CSV log to {result.log_path}"
            )
            return 1 if result.failed else 0
        written_log = run_dedup(args.inputs, args.output, log_path, args.move, show_progress=True)
    except ValueError as error:
        parser.error(str(error))
    mode = "move" if args.move else "dry run"
    print(f"Completed {mode}; wrote CSV log to {written_log}")
    return 0


def command_main(argv: list[str]) -> int:
    parser = build_command_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "manifest":
            manifest_path = generate_manifest(args.local_batch_root, args.nas_root, args.manifest)
            print(f"Wrote manifest to {manifest_path}")
            return 0
        if args.command == "plan-from-manifests":
            result = plan_from_manifests(args.manifests, args.output, args.log)
            print(
                f"Wrote manifest move plan to {result.log_path}; "
                f"duplicate_groups={result.duplicate_groups} "
                f"duplicate_files={result.duplicate_files} skipped_groups={result.skipped_groups}"
            )
            return 0
        if args.command == "verify-manifests":
            result = verify_manifests(args.manifests, args.log, args.byte_check)
            print(
                f"Completed manifest verify; checked_groups={result.checked_groups} "
                f"failed_groups={result.failed_groups}; wrote CSV log to {result.log_path}"
            )
            return 1 if result.failed_groups else 0
        if args.command == "execute-plan":
            result = execute_plan(args.plan, args.log, args.move)
            print(
                f"Completed execute-plan; bundles={result.bundles} "
                f"planned={result.planned_bundles} moved={result.moved_bundles} "
                f"already_moved={result.already_moved_bundles} skipped={result.skipped_bundles} "
                f"orphan_sidecars={result.orphan_sidecars}; wrote CSV log to {result.log_path}"
            )
            return 1 if result.skipped_bundles or result.orphan_sidecars else 0
        if args.command == "verify-move":
            result = verify_move(args.manifests, args.output, args.log)
            print(
                f"Completed verify-move; checked_paths={result.checked_paths} "
                f"matched_paths={result.matched_paths} failed_paths={result.failed_paths} "
                f"unexpected_outputs={result.unexpected_outputs}; wrote CSV log to {result.log_path}"
            )
            return 1 if result.failed_paths or result.unexpected_outputs else 0
    except ValueError as error:
        parser.error(str(error))
    parser.error(f"unknown command: {args.command}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
