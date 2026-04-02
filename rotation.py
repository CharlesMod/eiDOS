"""Log rotation for observations.jsonl."""

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
