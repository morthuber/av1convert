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
import time
from dataclasses import dataclass
from datetime import datetime, timezone
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
CPU_COUNT = os.cpu_count() or 1
DEFAULT_JOBS = max(1, CPU_COUNT // 2)
PROCESS_NICENESS = 10
PRINT_LOCK = threading.Lock()
LOG_LOCK = threading.Lock()
ACTIVE_JOBS_LOCK = threading.Lock()
ACTIVE_JOBS = 0
SYSTEM_LOG_INTERVAL_SECONDS = 10


@dataclass(frozen=True)
class ConversionTask:
    index: int
    total: int
    source: Path
    output: Path


@dataclass(frozen=True)
class ConversionResult:
    task: ConversionTask
    success: bool
    error_message: str | None = None
    elapsed_seconds: float = 0.0


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
    parser.add_argument(
        "--log-file",
        help="Optional log file path for timestamps, timings, and system snapshots",
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


def input_probe_ok(path: Path) -> bool:
    metadata = ffprobe_json(path)
    if metadata is None:
        return False

    streams = metadata.get("streams") or []
    return any(
        isinstance(stream, dict) and stream.get("codec_type") == "video"
        for stream in streams
    )


def build_tasks(
    source_dir: Path, destination_dir: Path
) -> tuple[list[ConversionTask], list[str]]:
    tasks: list[ConversionTask] = []
    task_collisions: dict[Path, Path] = {}
    errors: list[str] = []

    for source_path in iter_input_files(source_dir):
        if not input_probe_ok(source_path):
            errors.append(
                f"Unreadable by ffprobe or missing video stream: {source_path}"
            )
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
        tasks.append(
            ConversionTask(index=0, total=0, source=source_path, output=output_path)
        )

    total = len(tasks)
    numbered_tasks = [
        ConversionTask(index=i, total=total, source=task.source, output=task.output)
        for i, task in enumerate(tasks, start=1)
    ]

    return numbered_tasks, errors


def utc_timestamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def print_status(message: str) -> None:
    with PRINT_LOCK:
        print(message, flush=True)


def log_event(log_file: Path | None, message: str) -> None:
    if log_file is None:
        return
    line = f"{utc_timestamp()} {message}\n"
    with LOG_LOCK:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        with log_file.open("a", encoding="utf-8") as handle:
            handle.write(line)


def mem_available_mb() -> int | None:
    meminfo = Path("/proc/meminfo")
    if not meminfo.exists():
        return None
    try:
        for line in meminfo.read_text(encoding="utf-8").splitlines():
            if line.startswith("MemAvailable:"):
                parts = line.split()
                if len(parts) >= 2:
                    return int(parts[1]) // 1024
    except Exception:
        return None
    return None


def get_active_jobs() -> int:
    with ACTIVE_JOBS_LOCK:
        return ACTIVE_JOBS


def update_active_jobs(delta: int) -> None:
    global ACTIVE_JOBS
    with ACTIVE_JOBS_LOCK:
        ACTIVE_JOBS += delta


def system_log_worker(stop_event: threading.Event, log_file: Path | None) -> None:
    while not stop_event.wait(SYSTEM_LOG_INTERVAL_SECONDS):
        active_jobs = get_active_jobs()
        try:
            load1, load5, load15 = os.getloadavg()
            load_part = f"load1={load1:.2f} load5={load5:.2f} load15={load15:.2f}"
        except OSError:
            load_part = "load=unavailable"

        mem_mb = mem_available_mb()
        mem_part = (
            f"mem_avail_mb={mem_mb}"
            if mem_mb is not None
            else "mem_avail_mb=unavailable"
        )
        log_event(log_file, f"system {load_part} {mem_part} active_jobs={active_jobs}")


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


def convert_one(task: ConversionTask, log_file: Path | None) -> ConversionResult:
    started_at = time.monotonic()
    task.output.parent.mkdir(parents=True, exist_ok=True)
    temp_output = task.output.with_name(f"{task.output.name}.part")

    if temp_output.exists():
        temp_output.unlink()

    print_status(f"[{task.index}/{task.total}] START {task.source} -> {task.output}")
    log_event(
        log_file,
        f"job.start index={task.index} total={task.total} source={task.source} output={task.output}",
    )
    update_active_jobs(1)

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
            elapsed_seconds = time.monotonic() - started_at
            log_event(
                log_file,
                f"job.end index={task.index} status=fail elapsed={elapsed_seconds:.2f} reason={error!r}",
            )
            return ConversionResult(
                task=task,
                success=False,
                error_message=error,
                elapsed_seconds=elapsed_seconds,
            )

        temp_output.replace(task.output)

        validation_error = validate_output(task.output)
        if validation_error is not None:
            if task.output.exists():
                task.output.unlink()
            elapsed_seconds = time.monotonic() - started_at
            log_event(
                log_file,
                f"job.end index={task.index} status=fail elapsed={elapsed_seconds:.2f} reason={validation_error!r}",
            )
            return ConversionResult(
                task=task,
                success=False,
                error_message=validation_error,
                elapsed_seconds=elapsed_seconds,
            )

        elapsed_seconds = time.monotonic() - started_at
        log_event(
            log_file,
            f"job.end index={task.index} status=ok elapsed={elapsed_seconds:.2f}",
        )
        return ConversionResult(
            task=task, success=True, elapsed_seconds=elapsed_seconds
        )
    except Exception as exc:
        if temp_output.exists():
            temp_output.unlink()
        elapsed_seconds = time.monotonic() - started_at
        log_event(
            log_file,
            f"job.end index={task.index} status=fail elapsed={elapsed_seconds:.2f} reason={str(exc)!r}",
        )
        return ConversionResult(
            task=task,
            success=False,
            error_message=str(exc),
            elapsed_seconds=elapsed_seconds,
        )
    finally:
        update_active_jobs(-1)


def ffprobe_json(path: Path) -> dict | None:
    cmd = [
        "ffprobe",
        "-v",
        "error",
        "-print_format",
        "json",
        "-show_format",
        "-show_streams",
        str(path),
    ]
    result = subprocess.run(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
    )
    if result.returncode != 0:
        return None
    try:
        import json

        return json.loads(result.stdout)
    except Exception:
        return None


def validate_output(path: Path) -> str | None:
    if not path.exists():
        return "output file is missing"

    if path.stat().st_size <= 0:
        return "output file has zero size"

    metadata = ffprobe_json(path)
    if metadata is None:
        return "ffprobe could not read output file"

    format_info = metadata.get("format") or {}
    duration_raw = format_info.get("duration")
    try:
        duration = float(duration_raw)
    except (TypeError, ValueError):
        duration = 0.0

    if duration <= 0:
        return "output duration is zero or missing"

    streams = metadata.get("streams") or []
    has_video_stream = any(
        isinstance(stream, dict) and stream.get("codec_type") == "video"
        for stream in streams
    )
    if not has_video_stream:
        return "output file does not contain a video stream"

    return None


def ensure_ffmpeg_available() -> None:
    if shutil.which("ffmpeg") is None:
        raise RuntimeError("ffmpeg was not found in PATH")
    if shutil.which("ffprobe") is None:
        raise RuntimeError("ffprobe was not found in PATH")


def lower_process_priority() -> None:
    os.nice(PROCESS_NICENESS)


def main() -> int:
    args = parse_args()
    log_file = Path(args.log_file).expanduser() if args.log_file else None
    started_at = time.monotonic()

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

    log_event(
        log_file,
        f"start source={source_dir} destination={destination_dir} jobs={args.jobs} nice={PROCESS_NICENESS}",
    )

    destination_dir.mkdir(parents=True, exist_ok=True)

    discovery_started = time.monotonic()
    log_event(log_file, "preflight.begin")
    tasks, preflight_errors = build_tasks(source_dir, destination_dir)
    log_event(
        log_file,
        f"preflight.end scheduled={len(tasks)} errors={len(preflight_errors)} elapsed={time.monotonic() - discovery_started:.2f}",
    )
    successes = 0
    failures = len(preflight_errors)

    for error in preflight_errors:
        print_status(f"ERROR: {error}")

    total_found = len(tasks) + len(preflight_errors)
    completed = 0

    if total_found > 0:
        print_status(
            f"Discovered {total_found} input file(s); {len(tasks)} scheduled, {len(preflight_errors)} preflight error(s)"
        )

    stop_event = threading.Event()
    system_thread = threading.Thread(
        target=system_log_worker,
        args=(stop_event, log_file),
        daemon=True,
    )
    if log_file is not None:
        system_thread.start()

    with concurrent.futures.ThreadPoolExecutor(max_workers=args.jobs) as executor:
        future_to_task = {
            executor.submit(convert_one, task, log_file): task for task in tasks
        }
        for future in concurrent.futures.as_completed(future_to_task):
            result = future.result()
            completed += 1
            if result.success:
                successes += 1
                print_status(
                    f"[done {completed}/{total_found}] OK   {result.task.output}"
                )
            else:
                failures += 1
                print_status(
                    f"[done {completed}/{total_found}] FAIL {result.task.source}"
                )
                if result.error_message:
                    print_status(result.error_message)

    stop_event.set()
    if log_file is not None:
        system_thread.join(timeout=1)

    total_elapsed = time.monotonic() - started_at
    log_event(
        log_file,
        f"summary total_found={total_found} successes={successes} failures={failures} elapsed={total_elapsed:.2f}",
    )

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
