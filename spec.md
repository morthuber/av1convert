# Specification for av1convert

`av1convert` converts a directory tree of home videos to AV1 using FFmpeg.

## Invocation

The script is called with exactly one argument: the source directory.

```sh
av1convert.py SOURCE_DIR [--jobs N] [--log-file PATH]
```

If the source directory contains spaces or other shell-special characters, it must be quoted by the caller.

Examples:

```sh
av1convert.sh 2021/
av1convert.sh "/mnt/media/videos/year 2021/"
```

## Source and destination directories

The source argument must be an existing directory.

The output directory is created by removing one trailing `/` from the source path if present and then appending `-av1`.

Examples:

- `2021` -> `2021-av1`
- `2021/` -> `2021-av1`
- `/mnt/media/videos/year 2021/` -> `/mnt/media/videos/year 2021-av1`

The relative directory structure inside the source directory is preserved in the output directory.

## Input files

The script processes video files found recursively under the source directory.

A file is considered an input video if:
- it has one of the configured video filename extensions, and
- FFmpeg can open it successfully

The initial supported extensions are:

- `.mp4`
- `.mkv`
- `.avi`
- `.mov`
- `.wmv`
- `.flv`
- `.webm`
- `.m4v`

Extension matching is case-insensitive.

## Output files

Each input file produces one output file in the destination directory at the corresponding relative path.

The output container format is Matroska, and output files use the `.mkv` extension.

Example:

- source: `events/birthday/garden_june_26.mp4`
- output: `events/birthday/garden_june_26.mkv`

## Filename normalization

Before writing the output file, the base filename may be normalized by removing codec indicator tokens from the filename stem.

This normalization applies only to the filename stem, not to directory names.

Examples:

- `garden_june_26.mp4` -> `garden_june_26.mkv`
- `garden_july_h264.mp4` -> `garden_july.mkv`

Initial codec indicator tokens to remove:

- `h264`
- `x264`
- `h265`
- `x265`
- `hevc`
- `avc`

Rules:

- matching is case-insensitive
- a token is removed only when it appears as a whole segment separated by `_`, `-`, `.`, or spaces
- after removal, repeated separators are collapsed
- leading and trailing separators are removed
- if normalization would produce an empty filename stem, the original stem is kept

Examples:

- `trip-h264.mp4` -> `trip.mkv`
- `trip_H265_final.mp4` -> `trip_final.mkv`
- `myh264test.mp4` -> `myh264test.mkv`

## Logging

Logging is optional.

If a log file path is provided, the script writes timestamped log entries including:

- script start settings
- preflight start and end times
- per-job start and end times
- per-job elapsed time and failure reason, if any
- periodic system snapshots during conversion, including load average, available memory, and number of active jobs
- final summary totals and overall elapsed time

## Conversion behavior

For each input file:

- video is re-encoded to AV1
- the output extension is `.mkv`
- the script should avoid leaving partial output files behind if conversion fails
- the process runs at a lower-than-default CPU scheduling priority so interactive desktop use stays responsive while conversion runs in the background
- the default number of parallel jobs is half the number of CPU cores, rounded down, with a minimum of 1

The default FFmpeg progress output should not be shown during normal operation.

The script may print one concise status line per file.

FFmpeg error output should be shown for failed files.

## Output validation

After each conversion completes, the output file is validated.

For the initial version, validation must check all of the following:

- the output file exists
- the output file size is greater than zero
- `ffprobe` reports a duration greater than zero
- `ffprobe` reports at least one video stream

If validation fails, the file is treated as a failed conversion and must be reported in the summary.

## Existing files and conflicts

For the initial version:

- existing output files are overwritten
- if two different source files map to the same output path after normalization, that is treated as an error for those files and must be reported in the summary

## Summary and exit status

After processing all files, the script prints a summary including at least:

- source directory
- destination directory
- number of input files found
- number of successful conversions
- number of failed conversions
- number of skipped files, if skipping is implemented

The script should continue processing remaining files even if one file fails.

Exit status:

- `0` if all files were converted successfully
- non-zero if any file failed or if startup validation failed
