#!/usr/bin/env python3
from __future__ import annotations

import argparse
import concurrent.futures
import os
import re
import shutil
import subprocess
import sys
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

SUPPORTED_EXTENSIONS = {
    ".mp4",
    ".mkv",
    ".avi",
    ".mov",
    ".wmv",
    ".flv",
    ".webm",
    ".m4v",
}
CODEC_TOKENS = ("h264", "x264", "h265", "x265", "hevc", "avc")
DEFAULT_JOBS = max(1, (os.cpu_count() or 1))
PROCESS_NICENESS = 10
PRINT_LOCK = threading.Lock()


@dataclass(frozen=True)
class ConversionTask:
    source: Path
    output: Path


@dataclass(frozen=True)
class ConversionResult:
    task: ConversionTask
    success: bool
    error_message: str | None = None


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Convert a directory tree of home videos to AV1 using FFmpeg."
    )
    parser.add_argument("source_dir", help="Source directory containing video files")
    parser.add_argument(
        "--jobs",
        type=int,
        default=DEFAULT_JOBS,
        help=f"Number of parallel FFmpeg jobs (default: {DEFAULT_JOBS})",
    )
    return parser.parse_args()


def derive_destination_dir(source_arg: str) -> Path:
    cleaned = (
        source_arg[:-1]
        if source_arg.endswith("/") and source_arg != "/"
        else source_arg
    )
    return Path(f"{cleaned}-av1")


def normalize_stem(stem: str) -> str:
    if not stem:
        return stem

    parts = re.split(r"([_\-. ])", stem)
    normalized_parts: list[str] = []

    for part in parts:
        if not part:
            continue
        if re.fullmatch(r"[_\-. ]", part):
            normalized_parts.append(part)
            continue
        if part.casefold() in CODEC_TOKENS:
            continue
        normalized_parts.append(part)

    normalized = "".join(normalized_parts)
    normalized = re.sub(r"[_\-. ]{2,}", lambda match: match.group(0)[0], normalized)
    normalized = normalized.strip("_.- ")
    return normalized or stem


def iter_input_files(source_dir: Path) -> Iterable[Path]:
    for path in sorted(source_dir.rglob("*")):
        if path.is_file() and path.suffix.casefold() in SUPPORTED_EXTENSIONS:
            yield path


def can_ffmpeg_open(path: Path) -> bool:
    cmd = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-i",
        str(path),
        "-f",
        "null",
        "-",
    ]
    result = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return result.returncode == 0


def build_tasks(
    source_dir: Path, destination_dir: Path
) -> tuple[list[ConversionTask], list[str]]:
    tasks: list[ConversionTask] = []
    task_collisions: dict[Path, Path] = {}
    errors: list[str] = []

    for source_path in iter_input_files(source_dir):
        if not can_ffmpeg_open(source_path):
            errors.append(f"Unreadable by FFmpeg: {source_path}")
            continue

        relative = source_path.relative_to(source_dir)
        normalized_name = f"{normalize_stem(source_path.stem)}.mkv"
        output_path = destination_dir / relative.parent / normalized_name

        previous = task_collisions.get(output_path)
        if previous is not None:
            errors.append(
                f"Output path collision: {previous} and {source_path} -> {output_path}"
            )
            continue

        task_collisions[output_path] = source_path
        tasks.append(ConversionTask(source=source_path, output=output_path))

    return tasks, errors


def print_status(message: str) -> None:
    with PRINT_LOCK:
        print(message, flush=True)


def ffmpeg_command(source: Path, destination_tmp: Path) -> list[str]:
    return [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "warning",
        "-y",
        "-i",
        str(source),
        "-map",
        "0",
        "-c:v",
        "libsvtav1",
        "-crf",
        "30",
        "-threads",
        "0",
        "-preset",
        "6",
        "-pix_fmt",
        "yuv420p10le",
        "-c:a",
        "aac",
        "-b:a",
        "192k",
        "-c:s",
        "copy",
        "-f",
        "matroska",
        str(destination_tmp),
    ]


def convert_one(task: ConversionTask) -> ConversionResult:
    task.output.parent.mkdir(parents=True, exist_ok=True)
    temp_output = task.output.with_name(f"{task.output.name}.part")

    if temp_output.exists():
        temp_output.unlink()

    print_status(f"Encoding: {task.source} -> {task.output}")

    try:
        result = subprocess.run(
            ffmpeg_command(task.source, temp_output),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
        )

        if result.returncode != 0:
            if temp_output.exists():
                temp_output.unlink()
            error = (
                result.stderr.strip() or f"ffmpeg exited with code {result.returncode}"
            )
            return ConversionResult(task=task, success=False, error_message=error)

        temp_output.replace(task.output)
        return ConversionResult(task=task, success=True)
    except Exception as exc:
        if temp_output.exists():
            temp_output.unlink()
        return ConversionResult(task=task, success=False, error_message=str(exc))


def ensure_ffmpeg_available() -> None:
    if shutil.which("ffmpeg") is None:
        raise RuntimeError("ffmpeg was not found in PATH")


def lower_process_priority() -> None:
    os.nice(PROCESS_NICENESS)


def main() -> int:
    args = parse_args()

    if args.jobs < 1:
        print("Error: --jobs must be at least 1", file=sys.stderr)
        return 2

    source_dir = Path(args.source_dir).expanduser()
    if not source_dir.is_dir():
        print(f"Error: source directory does not exist: {source_dir}", file=sys.stderr)
        return 2

    destination_dir = derive_destination_dir(args.source_dir).expanduser()

    try:
        ensure_ffmpeg_available()
        lower_process_priority()
    except RuntimeError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2

    destination_dir.mkdir(parents=True, exist_ok=True)

    tasks, preflight_errors = build_tasks(source_dir, destination_dir)
    successes = 0
    failures = len(preflight_errors)

    for error in preflight_errors:
        print_status(f"ERROR: {error}")

    with concurrent.futures.ThreadPoolExecutor(max_workers=args.jobs) as executor:
        for result in executor.map(convert_one, tasks):
            if result.success:
                successes += 1
            else:
                failures += 1
                print_status(f"FAILED: {result.task.source}")
                if result.error_message:
                    print_status(result.error_message)

    total_found = len(tasks) + len(preflight_errors)

    print()
    print("Summary")
    print(f"Source directory: {source_dir}")
    print(f"Destination directory: {destination_dir}")
    print(f"Input files found: {total_found}")
    print(f"Successful conversions: {successes}")
    print(f"Failed conversions: {failures}")

    return 0 if failures == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
