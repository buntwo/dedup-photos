# dedup-photos

Find bit-exact duplicate photo files across one or more input directories.

Dry run is the default:

```bash
uv run dedup-photos INPUT_ROOT [INPUT_ROOT ...] --output DUPLICATE_OUTPUT_DIR
```

Move duplicates only with the explicit flag:

```bash
uv run dedup-photos INPUT_ROOT [INPUT_ROOT ...] --output DUPLICATE_OUTPUT_DIR --move
```

Verify a duplicate output directory after a move:

```bash
uv run dedup-photos INPUT_ROOT [INPUT_ROOT ...] --output DUPLICATE_OUTPUT_DIR --verify
```

## Batch manifest workflow

For slow NAS storage, copy batches to a faster local disk, hash them locally, and store the intended NAS paths in CSV manifests:

`local_batch_root` is the root of the local copy you want to hash. `--nas-root` is the original NAS root for that same tree. Relative paths are preserved. For example, if you copied `/my/nas/google_photos` to `/local/project/google_photos`, use:

```bash
uv run dedup-photos-manifest manifest /local/project/google_photos \
  --nas-root /my/nas/google_photos
```

This writes `/local/project/google_photos.manifest.csv` by default. Then `/local/project/google_photos/2021/img.jpg` is recorded in the manifest as `/my/nas/google_photos/2021/img.jpg`.

```bash
uv run dedup-photos-manifest manifest /local/batch/google-photos \
  --nas-root "/volume1/photo/google photos"

uv run dedup-photos-manifest manifest /local/batch/backups \
  --nas-root "/volume1/homes/btu/photos/backups"
```

After all batches are processed, compute the duplicate move plan without rereading file contents:

```bash
uv run dedup-photos-manifest plan /local/batch/google-photos.manifest.csv /local/batch/backups.manifest.csv \
  --output /volume1/photo/dupes \
  --log manifest_move_plan.csv
```

Execute the move plan. This is also a dry run by default:

```bash
uv run dedup-photos-manifest execute-plan manifest_move_plan.csv \
  --log manifest_execute_dry_run.csv
```

Move files only with the explicit flag:

```bash
uv run dedup-photos-manifest execute-plan manifest_move_plan.csv \
  --move \
  --log manifest_execute.csv
```

Manifest mode uses file size plus `xxh128` for primary image identity and records sidecar paths, sizes, and hashes so keeper precedence can be computed offline. Optional byte-level verification rereads the NAS paths referenced by the manifests:

```bash
uv run dedup-photos-manifest verify-bytes /local/batch/google-photos.manifest.csv /local/batch/backups.manifest.csv \
  --log manifest_verify.csv
```

After moving, verify that the expected files were moved and only expected files are present in the duplicate output tree. This checks paths and sizes, not file bytes:

```bash
uv run dedup-photos-manifest verify-move /local/batch/google-photos.manifest.csv /local/batch/backups.manifest.csv \
  --output /volume1/photo/dupes \
  --log manifest_move_verify.csv
```

Every run writes a CSV log. Pass `--log path/to/log.csv` to choose the path; otherwise a timestamped log is written in the current directory.

The CSV has a `disposition` column for one-column filtering, including values such as `kept_unique_primary`, `kept_duplicate_keeper`, `planned_duplicate_primary`, `moved_duplicate_primary`, `kept_error`, `verify_matched`, and `verify_failed`.

While running from the CLI, progress is printed on stderr with processed files, processed image count, percent done, moved/planned file count, errors, and the current kept-set size.

Moved files keep a parallel structure under the output directory, including the input root name:

```text
OUT/IN_1/foo/photo.jpg
OUT/IN_2/bar/photo.jpg
```
