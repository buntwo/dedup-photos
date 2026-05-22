# dedup-photos

Find bit-exact duplicate photo files through CSV manifests.

For slow NAS storage, copy batches to a faster local disk, hash them locally, and store the intended NAS paths in CSV manifests:

`local_batch_root` is the root of the local copy you want to hash. `--nas-root` is the original NAS root for that same tree. Relative paths are preserved. For example, if you copied `/my/nas/google_photos` to `/local/project/google_photos`, use:

```bash
uv run dedup-photos-manifest manifest /local/project/google_photos \
  --nas-root /my/nas/google_photos
```

This writes `/local/project/google_photos.manifest.csv` by default. Then `/local/project/google_photos/2021/img.jpg` is recorded in the manifest as `/my/nas/google_photos/2021/img.jpg`.

The manifest output file must not already exist; the command exits instead of overwriting it. The NAS root must also be mounted/readable, have the same basename as the local batch root, and have matching directories through two levels as a quick wrong-root check.

The manifest is a file inventory: it has one data row per regular non-symlink file under the local batch root. Rows are marked as `primary`, `sidecar`, or `uncategorized`; primary rows and their recognized sidecar rows share a `group_id`. Uncategorized rows are hashed and kept for debugging, but do not participate in duplicate planning.

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

For extra safety on a mutable NAS tree, rehash planned source files immediately before moving. This is slower and rereads the files being moved, but catches same-size source changes since the manifest was created:

```bash
uv run dedup-photos-manifest execute-plan manifest_move_plan.csv \
  --move \
  --verify-source-hashes \
  --log manifest_execute.csv
```

Manifest mode uses file size plus `xxh128` for primary image identity. Sidecar rows are used for keeper precedence and for moving duplicate bundles. Optional byte-level verification rereads the NAS paths referenced by primary rows in the manifests:

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

The CSV has a `disposition` column for one-column filtering, including values such as `kept_unique_primary`, `kept_duplicate_keeper`, `planned_duplicate_primary`, `moved_duplicate_primary`, `kept_error`, `verify_matched`, and `verify_failed`. Plan and execution logs also include `primary_source_path` for sidecar rows, so sidecars such as `photo.jpg.json` can be tied back to their primary image without relying on filename stems.

While running from the CLI, progress is printed on stderr. Manifest mode shows manifest hashing, CSV loading/planning, byte verification, plan execution, and move verification counters. Every progress line includes a phase-local percentage.

Moved files keep a parallel structure under the output directory, including the NAS root label from the manifest:

```text
OUT/IN_1/foo/photo.jpg
OUT/IN_2/bar/photo.jpg
```
