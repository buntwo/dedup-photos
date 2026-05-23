# dedup-photos

`dedup-photos` finds bit-exact duplicate photo files and moves only duplicate copies into a separate holding directory. It is designed for large NAS photo libraries where hashing directly on the NAS is slow: copy manageable batches to a local disk, hash them locally, then execute the resulting move plan against the NAS paths recorded in the manifests.

The tool never deletes files. `execute-plan` is a dry run unless `--move` is passed.

## What Gets Deduped

Primary image files participate in deduplication:

- `.jpg`
- `.jpeg`
- `.heic`
- `.png`

Extension matching is case-insensitive. Other regular files are still recorded in manifests, but they are marked as either `sidecar` or `uncategorized`.

Sidecars are files associated with a primary image, such as Live Photo videos or JSON metadata. They affect keeper selection and move together with duplicate primary images. Uncategorized files are hashed for audit/debugging, but are not deduped.

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

### 3. Verify Bytes

`verify-bytes` rereads NAS files in same-size/same-hash manifest buckets and performs byte-level comparisons. It checks primary images and sidecars.

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

Move mode requires the explicit `--move` flag.

```bash
uv run dedup-photos execute-plan move_plan.csv \
  --move \
  --log execute.csv
```

For extra safety on a mutable NAS tree, rehash each planned source file immediately before moving:

```bash
uv run dedup-photos execute-plan move_plan.csv \
  --move \
  --verify-source-hashes \
  --log execute.csv
```

`--verify-source-hashes` is slower, but catches planned source files whose contents changed after the manifest was created.

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

## Keeper Rules

For files with identical primary image size and hash, the kept copy is selected by precedence:

1. Prefer primaries with associated sidecars.
2. If both candidates have sidecars and the sidecar sets are incompatible, skip the group and log a sidecar conflict.
3. Prefer Google Takeout paths over mobile backup paths.
4. Within Takeout paths, prefer album/event-style folders over generic `Photos from YYYY` folders.
5. Use sorted path order as the final deterministic tie-breaker.

Only non-keeper duplicates are planned for movement. Unique primary images are not moved.

## CSV Outputs

Every command writes a CSV log. Pass `--log path/to/log.csv` to choose the path; otherwise the command writes a timestamped log in the current directory.

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
- `kept_duplicate_keeper`
- `planned_duplicate_primary`
- `planned_duplicate_sidecar`
- `moved_duplicate_primary`
- `moved_duplicate_sidecar`
- `kept_error`
- `verify_matched`
- `verify_failed`

Sidecar rows include `primary_source_path` so they can be traced back to the primary image without relying on filename stems.

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
uv run dedup-photos verify-bytes --help
uv run dedup-photos execute-plan --help
uv run dedup-photos verify-move --help
```

Count local primary image files under a root:

```bash
find /local/project/google_photos -type f \( \
  -iname '*.jpg' -o \
  -iname '*.jpeg' -o \
  -iname '*.heic' -o \
  -iname '*.png' \
\) | wc -l
```

Print which primary image extensions are present:

```bash
find /local/project/google_photos -type f \( \
  -iname '*.jpg' -o \
  -iname '*.jpeg' -o \
  -iname '*.heic' -o \
  -iname '*.png' \
\) -print | awk -F. 'NF > 1 { ext=tolower($NF); count[ext]++ } END { for (ext in count) print ext, count[ext] }' | sort
```
