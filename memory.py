"""Three-tier memory management: goal.md, plan.md (working memory), observations.jsonl.

Historical note: plan.md was previously memory.md.  The read_memory / write_memory
functions are kept as aliases so existing callers continue to work during the
transition.
"""

import json
import os
import tempfile
import time
from pathlib import Path

from config import Config


def read_goal(config: Config) -> str:
    """Read goal.md. Returns empty string if missing."""
    try:
        return config.goal_path.read_text().strip()
    except FileNotFoundError:
        return ""


def read_plan(config: Config) -> str:
    """Read plan.md (or memory.md as fallback). Returns empty string if missing."""
    try:
        return config.plan_path.read_text().strip()
    except FileNotFoundError:
        pass
    # Fallback: read legacy memory.md during transition
    try:
        return config.memory_path.read_text().strip()
    except FileNotFoundError:
        return ""


def write_plan(config: Config, content: str) -> None:
    """Atomically write plan.md (temp file + rename)."""
    config.workspace.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(
        dir=str(config.workspace),
        prefix=".plan_",
        suffix=".tmp",
    )
    try:
        with os.fdopen(fd, "w") as f:
            f.write(content)
        os.rename(tmp_path, str(config.plan_path))
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def read_subgoals(config: Config) -> str:
    """Read subgoals.md. Returns empty string if missing."""
    try:
        return config.subgoals_path.read_text().strip()
    except FileNotFoundError:
        return ""


def write_subgoals(config: Config, content: str) -> None:
    """Atomically write subgoals.md (temp file + rename)."""
    config.workspace.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(
        dir=str(config.workspace),
        prefix=".subgoals_",
        suffix=".tmp",
    )
    try:
        with os.fdopen(fd, "w") as f:
            f.write(content)
        os.rename(tmp_path, str(config.subgoals_path))
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


import re
from typing import Optional

def current_subtask(config: Config) -> Optional[str]:
    """Return the first unchecked subtask from subgoals.md, or None."""
    text = read_subgoals(config)
    if not text:
        return None
    for line in text.splitlines():
        if re.match(r"^\s*-\s*\[ \]", line):
            return re.sub(r"^\s*-\s*\[ \]\s*", "", line)
    return None


# --- Aliases for backward compatibility (used by eidos.py, compaction.py, tools.py) ---

def read_memory(config: Config) -> str:
    """Read memory.md. Returns empty string if missing.

    Note: new code should prefer read_plan().
    """
    try:
        return config.memory_path.read_text().strip()
    except FileNotFoundError:
        return ""


def write_memory(config: Config, content: str) -> None:
    """Atomically write memory.md (temp file + rename).

    Note: new code should prefer write_plan().
    """
    config.workspace.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(
        dir=str(config.workspace),
        prefix=".memory_",
        suffix=".tmp",
    )
    try:
        with os.fdopen(fd, "w") as f:
            f.write(content)
        os.rename(tmp_path, str(config.memory_path))
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def append_observation(config: Config, entry: dict) -> None:
    """Append a single observation entry to observations.jsonl.

    Adds a timestamp if not present.
    """
    config.workspace.mkdir(parents=True, exist_ok=True)
    if "ts" not in entry:
        entry["ts"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    line = json.dumps(entry, ensure_ascii=False) + "\n"
    with open(config.observations_path, "a") as f:
        f.write(line)


def read_recent_observations(
    config: Config,
    max_chars: int = None,
    max_count: int = None,
) -> list[dict]:
    """Read the most recent observations, newest-first.

    Respects both character budget and count limit.
    """
    if max_chars is None:
        max_chars = config.context_obs_max_chars
    if max_count is None:
        max_count = config.context_obs_max_count

    try:
        with open(config.observations_path) as f:
            lines = f.readlines()
    except FileNotFoundError:
        return []

    # Read from end, newest first
    result = []
    total_chars = 0
    for line in reversed(lines):
        line = line.strip()
        if not line:
            continue
        if len(result) >= max_count:
            break
        if total_chars + len(line) > max_chars:
            break
        try:
            entry = json.loads(line)
            result.append(entry)
            total_chars += len(line)
        except json.JSONDecodeError:
            # Skip malformed lines (possible crash corruption)
            continue

    return result


def count_observation_chars(config: Config) -> int:
    """Approximate token count via character count of observations.jsonl."""
    try:
        return config.observations_path.stat().st_size
    except FileNotFoundError:
        return 0


def truncate_observations(config: Config) -> int:
    """Clear observations.jsonl after successful compaction.

    The distilled content now lives in memory/plan, so the raw
    observations are no longer needed.  Without this, the file grows
    monotonically and should_compact() fires every tick.

    Returns the number of lines removed.
    """
    try:
        with open(config.observations_path) as f:
            lines = f.readlines()
    except FileNotFoundError:
        return 0

    removed = len(lines)
    # Atomic rewrite: empty the file
    with open(config.observations_path, "w") as f:
        pass
    return removed


def count_observation_lines(config: Config) -> int:
    """Count lines in observations.jsonl."""
    try:
        with open(config.observations_path) as f:
            return sum(1 for _ in f)
    except FileNotFoundError:
        return 0


def validate_observations(config: Config) -> int:
    """Validate observations.jsonl, truncating the last line if malformed.

    Returns number of lines truncated (0 or 1).
    """
    if not config.observations_path.exists():
        return 0

    with open(config.observations_path) as f:
        lines = f.readlines()

    if not lines:
        return 0

    # Check last line
    last = lines[-1].strip()
    if not last:
        return 0

    try:
        json.loads(last)
        return 0
    except json.JSONDecodeError:
        # Last line is malformed — truncate it
        with open(config.observations_path, "w") as f:
            f.writelines(lines[:-1])
        return 1


def read_interventions(config: Config) -> list[dict]:
    """Read pending intervention files from interventions/ dir.

    Each file is read, its content returned, and the file renamed to .done.
    Returns list of {"filename": str, "content": str}.
    """
    interventions_dir = config.interventions_dir
    if not interventions_dir.exists():
        return []

    results = []
    for path in sorted(interventions_dir.iterdir()):
        if path.suffix == ".done" or path.name.startswith("."):
            continue
        try:
            content = path.read_text().strip()
            if content:
                results.append({"filename": path.name, "content": content})
            path.rename(path.with_suffix(path.suffix + ".done"))
        except OSError:
            continue

    return results


def read_pending_questions(config: Config) -> list[dict]:
    """Read pending questions from workspace/pending_questions.jsonl."""
    questions_path = config.workspace / "pending_questions.jsonl"
    if not questions_path.exists():
        return []
    try:
        with open(questions_path) as f:
            return [json.loads(line) for line in f if line.strip()]
    except (OSError, json.JSONDecodeError):
        return []
