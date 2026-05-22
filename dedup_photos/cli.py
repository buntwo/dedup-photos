from __future__ import annotations

import argparse
import sys
from pathlib import Path

from dedup_photos.deduper import default_log_path, run_dedup
from dedup_photos.manifest import execute_plan, generate_manifest, plan_from_manifests, verify_manifests, verify_move
from dedup_photos.verifier import run_verify


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Deduplicate live photo directories by hashing files in place.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  dedup-photos /photos/google /photos/phone --output /photos/dupes\n"
            "  dedup-photos /photos/google /photos/phone --output /photos/dupes --move\n"
            "  dedup-photos /photos/google /photos/phone --output /photos/dupes --verify\n\n"
            "For the local-batch/NAS manifest workflow, use dedup-photos-manifest --help."
        ),
    )
    parser.add_argument("inputs", nargs="+", type=Path, help="One or more input photo library roots.")
    parser.add_argument("--output", required=True, type=Path, help="Directory where duplicate files are moved.")
    parser.add_argument("--log", type=Path, default=None, help="CSV log path. Defaults to a timestamped file.")
    parser.add_argument("--move", action="store_true", help="Actually move duplicate files. Default is dry run.")
    parser.add_argument("--verify", action="store_true", help="Verify output primaries are valid duplicates of input primaries.")
    return parser


def build_manifest_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Deduplicate NAS photos through batch manifests.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Typical workflow:\n"
            "  dedup-photos-manifest manifest /local/project/google_photos \\\n"
            "    --nas-root /my/nas/google_photos\n"
            "  dedup-photos-manifest plan /local/project/google_photos.manifest.csv phone.manifest.csv \\\n"
            "    --output /my/nas/dupes --log move_plan.csv\n"
            "  dedup-photos-manifest verify-bytes /local/project/google_photos.manifest.csv phone.manifest.csv \\\n"
            "    --log byte_verify.csv\n"
            "  dedup-photos-manifest execute-plan move_plan.csv --move --log execute.csv\n"
            "  dedup-photos-manifest verify-move /local/project/google_photos.manifest.csv phone.manifest.csv \\\n"
            "    --output /my/nas/dupes --log move_verify.csv"
        ),
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    manifest = subparsers.add_parser(
        "manifest",
        help="Hash a local batch and write a CSV manifest with NAS paths.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "local_batch_root and --nas-root must be matching tree roots.\n"
            "For each local file, the relative path under local_batch_root is appended to --nas-root.\n\n"
            "The manifest is a one-row-per-regular-file inventory. Rows are marked\n"
            "primary, sidecar, or uncategorized; primary/sidecar rows share group_id.\n\n"
            "--nas-root must already exist and be mounted/readable. Its basename must match\n"
            "local_batch_root, and directories through two levels are checked as a quick\n"
            "guard against pointing at the wrong NAS tree.\n\n"
            "Example:\n"
            "  copied NAS tree: /my/nas/google_photos\n"
            "  local copy:      /local/project/google_photos\n"
            "  command:         dedup-photos-manifest manifest /local/project/google_photos \\\n"
            "                     --nas-root /my/nas/google_photos\n\n"
            "Default manifest path: /local/project/google_photos.manifest.csv\n"
            "The manifest command refuses to overwrite an existing manifest file.\n"
            "A local file /local/project/google_photos/2021/img.jpg is recorded as\n"
            "/my/nas/google_photos/2021/img.jpg."
        ),
    )
    manifest.add_argument(
        "local_batch_root",
        type=Path,
        help="Root of the local copied batch to hash, e.g. /local/project/google_photos.",
    )
    manifest.add_argument(
        "--nas-root",
        required=True,
        type=Path,
        help="Original NAS root corresponding to local_batch_root, e.g. /my/nas/google_photos.",
    )
    manifest.add_argument(
        "--manifest",
        type=Path,
        help="CSV manifest path to create. Defaults to LOCAL_BATCH_ROOT.manifest.csv and must not already exist.",
    )

    plan = subparsers.add_parser(
        "plan",
        help="Compute a duplicate move plan from CSV manifests.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "--output is the duplicate holding directory on the NAS. Planned destinations are\n"
            "--output / nas_root_label / relative_path, where nas_root_label is the basename\n"
            "of the --nas-root used when creating each manifest.\n\n"
            "Example:\n"
            "  dedup-photos-manifest plan google_photos.manifest.csv phone.manifest.csv \\\n"
            "    --output /my/nas/dupes \\\n"
            "    --log move_plan.csv"
        ),
    )
    plan.add_argument("manifests", nargs="+", type=Path, help="Manifest CSVs created by the manifest subcommand.")
    plan.add_argument("--output", required=True, type=Path, help="NAS duplicate holding directory for planned moves.")
    plan.add_argument("--log", type=Path, default=None, help="Move-plan CSV to write.")

    verify = subparsers.add_parser(
        "verify-bytes",
        help="Byte-check duplicate groups referenced by manifests.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "This rereads only NAS files that are in same-size/same-hash manifest groups.\n"
            "Run it before execute-plan --move.\n\n"
            "Example:\n"
            "  dedup-photos-manifest verify-bytes google_photos.manifest.csv phone.manifest.csv --log byte_verify.csv"
        ),
    )
    verify.add_argument("manifests", nargs="+", type=Path, help="Manifest CSVs to verify against NAS files.")
    verify.add_argument("--log", type=Path, default=None, help="CSV verification log to write.")

    execute = subparsers.add_parser(
        "execute-plan",
        help="Validate or execute a manifest move plan.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Default is a dry run: the plan is validated and logged, but no files move.\n"
            "Add --move to move duplicate primaries and their sidecars.\n\n"
            "Examples:\n"
            "  dedup-photos-manifest execute-plan move_plan.csv --log execute_dry_run.csv\n"
            "  dedup-photos-manifest execute-plan move_plan.csv --move --log execute.csv"
        ),
    )
    execute.add_argument("plan", type=Path, help="Move-plan CSV created by the plan subcommand.")
    execute.add_argument("--log", type=Path, default=None, help="CSV execution log to write.")
    execute.add_argument("--move", action="store_true", help="Actually move files. Default is dry run.")

    verify_move_parser = subparsers.add_parser(
        "verify-move",
        help="Verify manifest-planned moves were executed.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Use the same manifests and --output root that were used for plan.\n"
            "This checks paths and sizes only; byte identity should be checked first with verify-bytes.\n\n"
            "Example:\n"
            "  dedup-photos-manifest verify-move google_photos.manifest.csv phone.manifest.csv \\\n"
            "    --output /my/nas/dupes \\\n"
            "    --log move_verify.csv"
        ),
    )
    verify_move_parser.add_argument("manifests", nargs="+", type=Path, help="Manifest CSVs used to create the move plan.")
    verify_move_parser.add_argument("--output", required=True, type=Path, help="Duplicate holding directory used by plan.")
    verify_move_parser.add_argument("--log", type=Path, default=None, help="CSV move-verification log to write.")
    return parser


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
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


def manifest_main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    parser = build_manifest_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "manifest":
            manifest_path = generate_manifest(
                args.local_batch_root,
                args.nas_root,
                args.manifest or default_manifest_output_path(args.local_batch_root),
                show_progress=True,
            )
            print(f"Wrote manifest to {manifest_path}")
            return 0
        if args.command == "plan":
            result = plan_from_manifests(args.manifests, args.output, args.log, show_progress=True)
            print(
                f"Wrote manifest move plan to {result.log_path}; "
                f"duplicate_groups={result.duplicate_groups} "
                f"duplicate_files={result.duplicate_files} skipped_groups={result.skipped_groups}"
            )
            return 0
        if args.command == "verify-bytes":
            result = verify_manifests(args.manifests, args.log, byte_check=True, show_progress=True)
            print(
                f"Completed manifest verify; checked_groups={result.checked_groups} "
                f"failed_groups={result.failed_groups}; wrote CSV log to {result.log_path}"
            )
            return 1 if result.failed_groups else 0
        if args.command == "execute-plan":
            result = execute_plan(args.plan, args.log, args.move, show_progress=True)
            print(
                f"Completed execute-plan; bundles={result.bundles} "
                f"planned={result.planned_bundles} moved={result.moved_bundles} "
                f"already_moved={result.already_moved_bundles} skipped={result.skipped_bundles} "
                f"orphan_sidecars={result.orphan_sidecars}; wrote CSV log to {result.log_path}"
            )
            return 1 if result.skipped_bundles or result.orphan_sidecars else 0
        if args.command == "verify-move":
            result = verify_move(args.manifests, args.output, args.log, show_progress=True)
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


def default_manifest_output_path(local_batch_root: Path) -> Path:
    return Path(f"{local_batch_root}.manifest.csv")


if __name__ == "__main__":
    raise SystemExit(main())
