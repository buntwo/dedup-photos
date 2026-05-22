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

Every run writes a CSV log. Pass `--log path/to/log.csv` to choose the path; otherwise a timestamped log is written in the current directory.

The CSV has a `disposition` column for one-column filtering, including values such as `kept_unique_primary`, `kept_duplicate_keeper`, `planned_duplicate_primary`, `moved_duplicate_primary`, `kept_error`, `verify_matched`, and `verify_failed`.

Moved files keep a parallel structure under the output directory, including the input root name:

```text
OUT/IN_1/foo/photo.jpg
OUT/IN_2/bar/photo.jpg
```
