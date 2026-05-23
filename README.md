# dedup-photos

`dedup-photos` finds bit-exact duplicate photo files and moves only duplicate copies into a separate holding directory. It is designed for large NAS photo libraries where hashing directly on the NAS is slow: copy manageable batches to a local disk, hash them locally, then execute the resulting move plan against the NAS paths recorded in the manifests.

The tool never deletes files. `execute-plan` is a dry run unless `--move` is passed.

## What Gets Deduped

Primary image files participate in deduplication. The exact case-insensitive extension set is defined in `dedup_photos.constants.PRIMARY_IMAGE_EXTENSIONS`; print it with:

```bash
uv run python -c 'from dedup_photos.constants import PRIMARY_IMAGE_EXTENSIONS; print("\n".join(sorted(PRIMARY_IMAGE_EXTENSIONS)))'
```

Other regular files are still recorded in manifests, but they are marked as either `sidecar` or `uncategorized`.

Sidecars are files associated with a primary image, such as Live Photo videos or JSON metadata. They affect keeper selection and move together with duplicate primary images. Uncategorized files are deduped independently as simple single-file exact-hash duplicates.

## Install

With `uv`:

```bash
uv sync
uv run dedup-photos --help
```

Without `uv`, create a normal virtualenv and install the project:

```bash
python3.13 -m venv .venv
source .venv/bin/activate
pip install .
dedup-photos --help
```

## Workflow

Use the same manifests throughout the workflow.

1. Create one manifest per copied local batch.
2. Plan duplicate moves from all manifests.
3. Byte-check same-size/same-hash duplicate buckets.
4. Dry-run the move plan.
5. Execute the move plan with `--move`.
6. Verify that the expected moved files are present in the duplicate holding directory.

### 1. Create Manifests

`local_batch_root` is the local copy you want to hash. `--nas-root` is the original NAS root for that same tree.

If you copied:

```text
/my/nas/google_photos -> /local/project/google_photos
```

run:

```bash
uv run dedup-photos manifest /local/project/google_photos \
  --nas-root /my/nas/google_photos
```

By default this writes:

```text
/local/project/google_photos.manifest.csv
```

A file at:

```text
/local/project/google_photos/2021/img.jpg
```

is recorded with this NAS path:

```text
/my/nas/google_photos/2021/img.jpg
```

The manifest command refuses to overwrite an existing manifest. It also checks that `--nas-root` exists, has the same basename as `local_batch_root`, and has matching directory structure through two levels as a quick wrong-root guard.

For multiple batches:

```bash
uv run dedup-photos manifest /local/batches/google_photos \
  --nas-root /volume1/photo/google_photos

uv run dedup-photos manifest /local/batches/phone_backup \
  --nas-root /volume1/photo/phone_backup
```

### 2. Plan Moves

Planning reads manifest CSVs only; it does not reread file contents.

```bash
uv run dedup-photos plan \
  /local/batches/google_photos.manifest.csv \
  /local/batches/phone_backup.manifest.csv \
  --output /volume1/photo/dupes \
  --log move_plan.csv
```

Planned duplicate destinations preserve a parallel structure under `--output`:

```text
/volume1/photo/dupes/google_photos/2021/img.jpg
/volume1/photo/dupes/phone_backup/DCIM/img.jpg
```

The top directory under `--output` is the basename of the manifest's `--nas-root`.

If strict planning skips many groups due to differing Google Photos JSON sidecars, review those differences first:

```bash
uv run dedup-photos analyze-json-sidecars \
  /local/batches/google_photos.manifest.csv \
  /local/batches/phone_backup.manifest.csv \
  --log json_sidecar_analysis.csv
```

After review, you can explicitly treat JSON sidecars as equivalent:

```bash
uv run dedup-photos plan \
  /local/batches/google_photos.manifest.csv \
  /local/batches/phone_backup.manifest.csv \
  --output /volume1/photo/dupes \
  --log move_plan.csv \
  --ignore-json-sidecar-fields
```

This flag is not the default. It does not edit JSON files; it keeps the keeper bundle's JSON in place and moves duplicate JSON sidecars to the duplicate holding tree.

### 3. Verify Bytes

`verify-bytes` rereads NAS files in same-size/same-hash manifest buckets and performs byte-level comparisons. It checks primary images, sidecars, and uncategorized files.

```bash
uv run dedup-photos verify-bytes \
  /local/batches/google_photos.manifest.csv \
  /local/batches/phone_backup.manifest.csv \
  --log byte_verify.csv
```

Run this before moving if you want proof that hash-equivalent buckets are byte-identical. It can be slower on a NAS because it rereads the relevant files.

### 4. Dry-Run Execution

Execution is a dry run by default. This validates the plan and writes a row-by-row execution log without moving files.

```bash
uv run dedup-photos execute-plan move_plan.csv \
  --log execute_dry_run.csv
```

### 5. Move Duplicates

Move mode requires the explicit `--move` flag. By default, move mode rehashes each planned source file immediately before moving and skips bundles whose current hashes differ from the plan.

```bash
uv run dedup-photos execute-plan move_plan.csv \
  --move \
  --log execute.csv
```

To skip this extra hash verification, pass `--no-verify-source-hashes`:

```bash
uv run dedup-photos execute-plan move_plan.csv \
  --move \
  --no-verify-source-hashes \
  --log execute.csv
```

`--no-verify-source-hashes` is faster, but less safe on a mutable NAS tree because same-size content changes will not be caught before moving.

### 6. Verify Moves

After moving, verify that expected duplicate files are present in the duplicate holding directory and that unexpected files are not present.

```bash
uv run dedup-photos verify-move \
  /local/batches/google_photos.manifest.csv \
  /local/batches/phone_backup.manifest.csv \
  --output /volume1/photo/dupes \
  --log move_verify.csv
```

This checks paths and sizes only. Pair it with `verify-bytes` for byte-level duplicate proof.

If the plan was created with `--ignore-json-sidecar-fields`, use the same flag for move verification:

```bash
uv run dedup-photos verify-move \
  /local/batches/google_photos.manifest.csv \
  /local/batches/phone_backup.manifest.csv \
  --output /volume1/photo/dupes \
  --log move_verify.csv \
  --ignore-json-sidecar-fields
```

## Sanity Checks

After creating a manifest, the number of manifest data rows should equal the number of regular files under the local batch root. Set these variables first:

```bash
LOCAL_ROOT=/local/project/google_photos
MANIFEST=/local/project/google_photos.manifest.csv
```

Then compare the counts:

```bash
local_count=$(find "$LOCAL_ROOT" -type f | wc -l | tr -d ' ')
manifest_count=$(python3 -c 'import csv, sys; print(sum(1 for _ in csv.DictReader(open(sys.argv[1], newline="", encoding="utf-8"))))' "$MANIFEST")
printf 'local files:   %s\nmanifest rows: %s\n' "$local_count" "$manifest_count"
test "$local_count" = "$manifest_count"
```

The number of primary image files should also equal the number of manifest rows marked `file_role=primary`:

```bash
image_count=$(uv run python -c 'from pathlib import Path; import sys; from dedup_photos.constants import PRIMARY_IMAGE_EXTENSIONS; root=Path(sys.argv[1]); print(sum(1 for path in root.rglob("*") if path.is_file() and not path.is_symlink() and path.suffix.lower() in PRIMARY_IMAGE_EXTENSIONS))' "$LOCAL_ROOT")
primary_rows=$(python3 -c 'import csv, sys; print(sum(row["file_role"] == "primary" for row in csv.DictReader(open(sys.argv[1], newline="", encoding="utf-8"))))' "$MANIFEST")
printf 'image files:  %s\nprimary rows: %s\n' "$image_count" "$primary_rows"
test "$image_count" = "$primary_rows"
```

To inspect how the manifest classified every file:

```bash
python3 -c 'import collections, csv, sys; counts=collections.Counter(row["file_role"] for row in csv.DictReader(open(sys.argv[1], newline="", encoding="utf-8"))); [print(role, counts[role]) for role in sorted(counts)]' "$MANIFEST"
```

For plan and execution logs, the main thing to inspect is `disposition`:

```bash
python3 -c 'import collections, csv, sys; counts=collections.Counter(row["disposition"] for row in csv.DictReader(open(sys.argv[1], newline="", encoding="utf-8"))); [print(disposition, counts[disposition]) for disposition in sorted(counts)]' move_plan.csv
```

For any log, print only error rows:

```bash
python3 -c 'import csv, sys; rows=[row for row in csv.DictReader(open(sys.argv[1], newline="", encoding="utf-8")) if row.get("status") == "error"]; [print(row.get("disposition", ""), row.get("event", ""), row.get("reason", ""), row.get("source_path", "") or row.get("destination_path", "")) for row in rows]; sys.exit(1 if rows else 0)' LOG.csv
```

For `verify-bytes`, any error is a byte-identity failure in a same-size/same-hash group. Check these before moving:

```bash
python3 -c 'import csv, sys; rows=[row for row in csv.DictReader(open(sys.argv[1], newline="", encoding="utf-8")) if row["disposition"] == "verify_failed"]; [print(row["event"], row["file_role"], row["reason"], row["source_path"]) for row in rows]; sys.exit(1 if rows else 0)' byte_verify.csv
```

For `execute-plan`, inspect rows where execution deviated from the plan:

```bash
python3 -c 'import csv, sys; rows=[row for row in csv.DictReader(open(sys.argv[1], newline="", encoding="utf-8")) if row.get("status") == "error" or row.get("validation_result") not in ("", "validated", "already_moved")]; [print(row.get("disposition", ""), row.get("validation_result", ""), row.get("reason", ""), row.get("source_path", "") or row.get("destination_path", "")) for row in rows]; sys.exit(1 if rows else 0)' execute.csv
```

For `verify-move`, check both failed expected paths and unexpected output files:

```bash
python3 -c 'import csv, sys; rows=[row for row in csv.DictReader(open(sys.argv[1], newline="", encoding="utf-8")) if row["disposition"] in ("verify_move_failed", "verify_move_unexpected_output")]; [print(row["disposition"], row["reason"], row.get("source_path", "") or row.get("destination_path", "")) for row in rows]; sys.exit(1 if rows else 0)' move_verify.csv
```

## Keeper Rules

For files with identical primary image size and hash, the kept copy is selected by precedence:

1. Prefer primaries with associated sidecars.
2. If sidecar hashes form a superset relationship, keep the superset bundle.
3. If sidecars differ only by class, merge the missing `.mov`/`.mp4` or `.json` sidecar into the keeper bundle.
4. If candidates have different sidecar hashes within the same class, skip the group and log a sidecar conflict.
5. Prefer Google Takeout paths over mobile backup paths.
6. Within Takeout paths, prefer album/event-style folders over generic `Photos from YYYY` folders.
7. Use sorted path order as the final deterministic tie-breaker.

Only non-keeper duplicates are planned for movement. Unique primary images are not moved.

By default, JSON sidecars are compared strictly like other sidecars. `plan --ignore-json-sidecar-fields` treats `.json` sidecars as equivalent regardless of JSON field/content differences, while keeping motion sidecars such as `.mov` and `.mp4` strict.

Uncategorized files use the same exact `(size_bytes, xxh128)` grouping and path precedence, but no sidecar rules apply. Sidecar rows are never deduped independently.

## CSV Outputs

Every command writes a CSV log. Pass `--log path/to/log.csv` to choose the path; otherwise the command writes a timestamped log in the current directory. Output files are never overwritten; if the target log or manifest already exists, the command exits with an error.

Manifest CSVs contain one data row per regular non-symlink file under the local batch root. Rows include:

- `relative_path`
- `file_role`
- `status`
- `reason`
- `group_id`
- `primary_relative_path`
- `size_bytes`
- `xxh128`
- `nas_path`
- `primary_nas_path`

Batch-level fields such as `batch_root`, `nas_root`, and `nas_root_label` are at the end of the manifest rows because they are usually constant for the whole file.

Plan and execution CSVs include a `disposition` column intended for easy filtering. Values include:

- `kept_unique_primary`
- `kept_unique_sidecar`
- `kept_unique_uncategorized`
- `kept_duplicate_keeper`
- `kept_duplicate_keeper_sidecar`
- `kept_duplicate_uncategorized`
- `planned_duplicate_primary`
- `planned_duplicate_sidecar`
- `planned_sidecar_merge`
- `planned_duplicate_uncategorized`
- `verified_keeper`
- `verified_uncategorized_keeper`
- `keeper_error`
- `uncategorized_keeper_error`
- `moved_duplicate_primary`
- `moved_duplicate_sidecar`
- `merged_sidecar`
- `moved_duplicate_uncategorized`
- `already_moved_duplicate_primary`
- `already_moved_sidecar`
- `already_moved_duplicate_uncategorized`
- `already_merged_sidecar`
- `skipped_error_primary`
- `skipped_error_sidecar`
- `skipped_error_uncategorized`
- `orphan_plan_sidecar`
- `verify_matched`
- `verify_failed`

Execution logs also include `observed_hash`, `hash_check`, `validation_result`, and `action_taken`. These make move-mode deviations easier to filter: `status=error` identifies rows that did not follow the plan, `validation_result` gives the specific cause, and `observed_hash` records the current hash when a source or already-moved destination was rehashed.

Sidecar rows include `primary_source_path` so they can be traced back to the primary image without relying on filename stems. Merge rows use `destination_path` for the keeper-side sidecar target and `duplicate_output_path` for the fallback duplicate holding path when the target already exists with the same hash.

Plan and execution logs put debugging columns first: `disposition`, `status`, `file_role`, `source_path`, destination fields, keeper fields, group/event, reason/message, then size/hash and validation details. Constant or bookkeeping fields such as `input_root`, `mode`, and `timestamp` are at the end.

## Progress Output

Progress is printed to stderr. Each progress line includes a phase-local percentage, so `manifest`, `plan`, `verify-bytes`, `execute-plan`, and `verify-move` each report their own progress rather than sharing one global percentage.

## Useful Commands

Show all available subcommands:

```bash
uv run dedup-photos --help
```

Show help for one step:

```bash
uv run dedup-photos manifest --help
uv run dedup-photos plan --help
uv run dedup-photos analyze-json-sidecars --help
uv run dedup-photos verify-bytes --help
uv run dedup-photos execute-plan --help
uv run dedup-photos verify-move --help
```

Count local primary image files under a root:

```bash
uv run python -c 'from pathlib import Path; import sys; from dedup_photos.constants import PRIMARY_IMAGE_EXTENSIONS; root=Path(sys.argv[1]); print(sum(1 for path in root.rglob("*") if path.is_file() and not path.is_symlink() and path.suffix.lower() in PRIMARY_IMAGE_EXTENSIONS))' /local/project/google_photos
```

Print which primary image extensions are present:

```bash
uv run python -c 'from collections import Counter; from pathlib import Path; import sys; from dedup_photos.constants import PRIMARY_IMAGE_EXTENSIONS; root=Path(sys.argv[1]); counts=Counter(path.suffix.lower() for path in root.rglob("*") if path.is_file() and not path.is_symlink() and path.suffix.lower() in PRIMARY_IMAGE_EXTENSIONS); [print(ext, counts[ext]) for ext in sorted(counts)]' /local/project/google_photos
```
