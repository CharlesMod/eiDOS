"""Application notebooks — the THIRD memory tier.

Three tiers (matching the embodiment doc's working / episodic / semantic hierarchy):
  - `remember`            : a one-line volatile scratch thought.
  - notebooks (THIS file) : named markdown working-notes the agent opens/appends/reads at will,
                            for LOTS of notes about the current task/environment.
  - `memorize`/`recall`   : durable, searchable, semantic facts.

The ACTIVE notebook is surfaced in context every tick, so working notes stay in front of the agent
instead of (a) re-memorizing the same fact over and over, or (b) writing hidden JSON files. Notebooks
are first-class and system-visible (workspace/notes/*.md), not ad-hoc files.
"""
import re
from pathlib import Path


def _notes_dir(config) -> Path:
    d = config.workspace / "notes"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _safe_name(name: str) -> str:
    n = re.sub(r"[^a-zA-Z0-9_\-]", "_", (name or "").strip())[:48].strip("_")
    return n or "scratch"


def _active_path(config) -> Path:
    return _notes_dir(config) / ".active"


def set_active(config, name: str) -> None:
    try:
        _active_path(config).write_text(_safe_name(name), encoding="utf-8")
    except OSError:
        pass


def get_active(config):
    try:
        n = _active_path(config).read_text(encoding="utf-8").strip()
        return n or None
    except OSError:
        return None


def close_active(config) -> None:
    try:
        _active_path(config).unlink()
    except OSError:
        pass


NOTE_DEDUP_SIM = 0.75   # a new line this token-similar to a recent line in the same notebook is a
#                         near-verbatim repeat — NOT written. The active notebook is re-injected every
#                         tick; without this it becomes a litany the creature then imitates ("the wall
#                         is my horizon" ×N → the morose loop). Verbatim repeats score 0.89–1.0, so this
#                         catches the loop while leaving genuine new notes (which score <0.3) alone.


def _recent_lines(config, name: str, n: int = 15) -> list:
    txt = read_note(config, name, max_chars=4000)
    return [ln.strip() for ln in txt.splitlines() if ln.strip()][-n:]


def append_note(config, name: str, text: str):
    """Append a line/block to a named notebook (creating it), and make it the active notebook.
    Returns (name, dropped): dropped=True when the line was a near-verbatim repeat of a recent line
    and was therefore NOT written — the de-echo that stops the notebook feeding a self-imitation loop."""
    name = _safe_name(name)
    body = (text or "").rstrip()
    if body:
        try:
            import knowledge as _k
            if any(_k.token_jaccard(body, prev) >= NOTE_DEDUP_SIM
                   for prev in _recent_lines(config, name)):
                set_active(config, name)          # they meant to work here — keep it open, skip the echo
                return name, True
        except Exception:  # noqa: BLE001 - dedup is best-effort; never block a real note
            pass
    p = _notes_dir(config) / f"{name}.md"
    with open(p, "a", encoding="utf-8") as f:
        f.write(body + "\n")
    set_active(config, name)
    return name, False


def read_note(config, name: str, max_chars: int = 4000) -> str:
    p = _notes_dir(config) / f"{_safe_name(name)}.md"
    try:
        t = p.read_text(encoding="utf-8")
        return t[-max_chars:] if len(t) > max_chars else t
    except OSError:
        return ""


def list_notes(config):
    out = []
    for p in sorted(_notes_dir(config).glob("*.md")):
        try:
            out.append((p.stem, p.stat().st_size))
        except OSError:
            pass
    return out


def read_active(config, max_chars: int = 1500):
    """Return (name, tail_text) of the active notebook, or (None, '')."""
    n = get_active(config)
    if not n:
        return None, ""
    return n, read_note(config, n, max_chars)
