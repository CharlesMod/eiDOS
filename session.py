"""SSH session detection and standby/resume logic."""

import json
import os
import subprocess
import time
from pathlib import Path

from config import Config


def human_present() -> bool:
    """Check if any interactive SSH session is active.

    Parses `who` output. Returns True if any SSH session found.
    Console-only sessions (physical terminal) do not count.
    """
    try:
        result = subprocess.run(
            ["who"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        for line in result.stdout.strip().split("\n"):
            if not line.strip():
                continue
            # SSH sessions typically show as pts/N or have a remote host in parens
            # macOS shows "ttysNNN" for SSH
            lower = line.lower()
            if "pts/" in lower or "()" not in line:
                # Check for remote host indicator (IP or hostname in parens)
                if "(" in line and ")" in line:
                    # Has a remote host — this is SSH
                    return True
                # On some systems, pts without parens is also SSH
                if "pts/" in lower:
                    return True
        return False
    except (OSError, subprocess.TimeoutExpired):
        # If we can't check, assume no human (don't stall the agent)
        return False


def take_workspace_snapshot(config: Config) -> dict:
    """Capture workspace state for diff on resume.

    Records: file listing, pip freeze hash, running processes.
    """
    snapshot = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "files": _list_workspace_files(config),
    }

    # Installed packages (pip)
    try:
        result = subprocess.run(
            ["pip", "list", "--format=freeze"],
            capture_output=True, text=True, timeout=10,
        )
        snapshot["pip_packages"] = set(result.stdout.strip().split("\n")) if result.stdout.strip() else set()
    except (OSError, subprocess.TimeoutExpired):
        snapshot["pip_packages"] = set()

    return snapshot


def workspace_diff(config: Config, before: dict) -> str:
    """Compare current workspace state to a prior snapshot.

    Returns a human-readable diff string, or empty string if no changes.
    """
    changes = []

    # File diff
    current_files = set(_list_workspace_files(config))
    before_files = set(before.get("files", []))

    added = current_files - before_files
    removed = before_files - current_files

    if added:
        changes.append(f"New files: {', '.join(sorted(added)[:20])}")
        if len(added) > 20:
            changes.append(f"  ...and {len(added) - 20} more")
    if removed:
        changes.append(f"Removed files: {', '.join(sorted(removed)[:20])}")

    # Package diff
    try:
        result = subprocess.run(
            ["pip", "list", "--format=freeze"],
            capture_output=True, text=True, timeout=10,
        )
        current_pkgs = set(result.stdout.strip().split("\n")) if result.stdout.strip() else set()
    except (OSError, subprocess.TimeoutExpired):
        current_pkgs = set()

    before_pkgs = before.get("pip_packages", set())
    if isinstance(before_pkgs, list):
        before_pkgs = set(before_pkgs)

    new_pkgs = current_pkgs - before_pkgs
    removed_pkgs = before_pkgs - current_pkgs

    if new_pkgs:
        changes.append(f"New packages: {', '.join(sorted(new_pkgs)[:10])}")
    if removed_pkgs:
        changes.append(f"Removed packages: {', '.join(sorted(removed_pkgs)[:10])}")

    if not changes:
        return ""

    return "Changes detected during human session:\n" + "\n".join(changes)


def _list_workspace_files(config: Config) -> list[str]:
    """List files in workspace directory (non-recursive, top-level only)."""
    workspace = config.workspace
    if not workspace.exists():
        return []
    try:
        return sorted(
            str(p.relative_to(workspace))
            for p in workspace.iterdir()
            if p.is_file()
        )
    except OSError:
        return []
