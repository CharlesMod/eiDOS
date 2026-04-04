"""Log rotation for observations.jsonl, llm_log.jsonl, and snapshots."""

import gzip
import os
import time
from pathlib import Path

from config import Config


def rotate_if_needed(config: Config) -> bool:
    """Rotate observations.jsonl if it exceeds the line limit.

    Keeps the most recent obs_max_lines lines in the live file.
    Archives older lines to a gzipped file in the workspace.
    Returns True if rotation was performed.
    """
    obs_path = config.observations_path
    if not obs_path.exists():
        return False

    with open(obs_path) as f:
        lines = f.readlines()

    if len(lines) <= config.obs_max_lines:
        return False

    # Split: archive older lines, keep recent
    keep = lines[-config.obs_max_lines:]
    archive = lines[:-config.obs_max_lines]

    # Write archive
    ts = time.strftime("%Y%m%d_%H%M%S", time.gmtime())
    archive_path = config.workspace / f"observations_archive_{ts}.jsonl.gz"
    with gzip.open(archive_path, "wt", encoding="utf-8") as f:
        f.writelines(archive)

    # Rewrite live file with recent lines only
    with open(obs_path, "w") as f:
        f.writelines(keep)

    return True


def cleanup_old_archives(config: Config) -> int:
    """Delete observation archives older than obs_archive_days.

    Returns count of archives deleted.
    """
    if not config.workspace.exists():
        return 0

    cutoff = time.time() - (config.obs_archive_days * 86400)
    deleted = 0

    for path in config.workspace.glob("observations_archive_*.jsonl.gz"):
        try:
            if path.stat().st_mtime < cutoff:
                path.unlink()
                deleted += 1
        except OSError:
            continue

    return deleted


def rotate_llm_log(config: Config) -> bool:
    """Rotate llm_log.jsonl if it exceeds llm_log_max_bytes.

    Gzips the current log and starts a fresh one.
    Keeps the last llm_log_archive_count archives.
    Returns True if rotation was performed.
    """
    log_path = config.workspace / "llm_log.jsonl"
    if not log_path.exists():
        return False

    try:
        size = log_path.stat().st_size
    except OSError:
        return False

    if size < config.llm_log_max_bytes:
        return False

    # Archive current log
    ts = time.strftime("%Y%m%d_%H%M%S", time.gmtime())
    archive_path = config.workspace / f"llm_log_{ts}.jsonl.gz"
    with open(log_path, "rb") as f_in:
        with gzip.open(archive_path, "wb") as f_out:
            f_out.write(f_in.read())

    # Truncate live log
    log_path.write_text("")

    # Prune old archives
    archives = sorted(
        config.workspace.glob("llm_log_*.jsonl.gz"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    for old in archives[config.llm_log_archive_count:]:
        try:
            old.unlink()
        except OSError:
            continue

    return True


def cleanup_old_snapshots(config: Config) -> int:
    """Keep only the most recent snapshot_max_count memory snapshots.

    Returns count of snapshots deleted.
    """
    snap_dir = config.snapshots_dir
    if not snap_dir.exists():
        return 0

    snapshots = sorted(
        snap_dir.glob("memory_snapshot_*"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )

    to_delete = snapshots[config.snapshot_max_count:]
    deleted = 0
    for path in to_delete:
        try:
            path.unlink()
            deleted += 1
        except OSError:
            continue

    return deleted
