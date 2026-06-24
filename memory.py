"""Three-tier memory management: goal.md, plan.md (working memory), observations.jsonl.

Historical note: plan.md was previously memory.md (file removed in v2 phase 0e).
functions are kept as aliases so existing callers continue to work during the
transition.
"""

import json
import os
import re
import tempfile
import time
from pathlib import Path

from config import Config
from atomicio import replace_with_retry
from typed_boundary import validate_chat_reply_record, validate_observation_record


def read_goal(config: Config) -> str:
    """Read goal.md. Returns empty string if missing."""
    try:
        return config.goal_path.read_text().strip()
    except FileNotFoundError:
        return ""


def read_plan(config: Config) -> str:
    """Read plan.md (working memory). Returns empty string if missing."""
    try:
        return config.plan_path.read_text().strip()
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
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)
        replace_with_retry(tmp_path, str(config.plan_path))
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise



def read_self_guide(config: Config) -> str:
    """Read self_guide.md — the operator-owned standing directives injected each tick.

    Resilient: one non-UTF-8 byte or a missing file must NEVER brick the tick loop.
    """
    try:
        return config.self_guide_path.read_text(encoding="utf-8", errors="replace").strip()
    except (FileNotFoundError, OSError, UnicodeError):
        return ""


def read_self_guide_proposed(config: Config) -> str:
    try:
        return config.self_guide_proposed_path.read_text(encoding="utf-8", errors="replace").strip()
    except (FileNotFoundError, OSError, UnicodeError):
        return ""


def write_self_guide(config: Config, content: str) -> None:
    """Atomically write the LIVE self_guide.md. Dashboard/operator path only."""
    config.workspace.mkdir(parents=True, exist_ok=True)
    content = (content or "")[: config.self_guide_max_bytes]
    fd, tmp_path = tempfile.mkstemp(dir=str(config.workspace), prefix=".self_guide_", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)
        replace_with_retry(tmp_path, str(config.self_guide_path))
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def write_self_guide_proposal(config: Config, content: str, rationale: str = "", tick=None) -> None:
    """eiDOS-side: stage a PROPOSED self-guide (never the live file) + audit-log it."""
    config.workspace.mkdir(parents=True, exist_ok=True)
    content = (content or "")[: config.self_guide_max_bytes]
    fd, tmp_path = tempfile.mkstemp(dir=str(config.workspace), prefix=".self_guide_prop_", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)
        replace_with_retry(tmp_path, str(config.self_guide_proposed_path))
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise
    try:
        pp = config.self_guide_proposals_path
        # Bound growth: keep the audit log to the last ~500 entries.
        try:
            if pp.exists() and pp.stat().st_size > 1_000_000:
                kept = pp.read_text(encoding="utf-8", errors="replace").splitlines()[-500:]
                pp.write_text("\n".join(kept) + "\n", encoding="utf-8")
        except OSError:
            pass
        with open(pp, "a", encoding="utf-8") as f:
            f.write(json.dumps({
                "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "tick": tick,
                "rationale": (rationale or "")[:300],
                "chars": len(content),
            }) + "\n")
    except OSError:
        pass


# --- Train of thought (continuous stream of consciousness) ---

def is_degenerate(text: str) -> bool:
    """True if `text` looks like a model degeneration loop (repeated junk), not real content — e.g.
    the ¥¥¡¥¥¡… byte-token collapse. Conservative: a long run with almost no character variety, or a
    short block tiling most of the string. Real prose clears these thresholds easily. This is the
    backstop that keeps a degenerate generation out of storage, the display, AND the next tick's
    context (where it would otherwise feed the loop), regardless of why the model degenerated."""
    t = (text or "").strip()
    if len(t) < 40:
        return False
    uniq = len(set(t))
    if uniq <= 6 and uniq / len(t) < 0.08:        # long string of only ~2-6 distinct characters
        return True
    for n in (1, 2, 3, 4):                          # the same ≤4-char block tiling ≥80% of the text
        block = t[:n]
        if block and t.count(block) * n >= len(t) * 0.8:
            return True
    return False


# A run of ≥4 byte-fallback / symbol characters — Latin-1 supplement symbols (¥=A5, ¡=A1), spacing-
# modifier/combining marks, and the general-punct→symbols/dingbats blocks (√=221A). Real prose almost
# never strings 4+ of these together; a degenerate generation does (the ¥¥¡¥¥¡¥√ collapse). This
# catches the PARTIAL case is_degenerate misses: a junk PREFIX followed by recovered text — that whole
# thought came from a degenerate generation and shouldn't be trusted or fed back. Common accented
# letters (À-ÿ, ≥ À) are deliberately outside the class so é/ñ/ü never trip it.
_JUNK_RUN_RE = re.compile(r"[-¿ʰ-ͯ -⯿]{4,}")


def has_junk_run(text: str) -> bool:
    """True if `text` contains a run of byte-fallback/symbol junk — the signature of a (possibly
    partial) degenerate generation, even when real text surrounds it."""
    return bool(_JUNK_RUN_RE.search(text or ""))


def log_degeneration(config: Config, tick, raw_response: str, reason: str = "") -> None:
    """Capture a degenerate generation (raw response + a context fingerprint) to degeneration_log.jsonl
    for analysis — the trigger doesn't reproduce synthetically, so we record it when it happens live."""
    try:
        rec = {"tick": tick, "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
               "reason": reason, "raw": (raw_response or "")[:4000]}
        with open(config.workspace / "degeneration_log.jsonl", "a", encoding="utf-8") as f:
            f.write(json.dumps(rec) + "\n")
    except OSError:
        pass


def append_thought(config: Config, tick, text: str) -> None:
    """Append one entry to the agent's train of thought (thoughts.jsonl). Degenerate output is
    dropped (never stored or fed back) — see is_degenerate / has_junk_run."""
    text = (text or "").strip()
    if not text or is_degenerate(text) or has_junk_run(text):
        return
    rec = {"tick": tick,
           "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
           "text": text[:600]}
    try:
        with open(config.workspace / "thoughts.jsonl", "a", encoding="utf-8") as f:
            f.write(json.dumps(rec) + "\n")
    except OSError:
        pass


_CHAT_MERGE_WINDOW_S = 20.0  # a reply and its spoken twin land in the same tick → well within this


def append_chat_line(config: Config, text: str, *, spoken: bool = False, tick=None) -> None:
    """Append one eiDOS→operator line to chat_replies.jsonl, with same-utterance dedup.

    eiDOS may both `<reply>` a line (silent, logged) AND `speak` it (voiced) in one tick — that's two
    writes for ONE utterance, which used to show as a duplicate. So before appending, look at the
    previous line: if it's the SAME utterance (identical, or one is a prefix of the other — e.g. a
    spoken opener of a longer reply) and recent (within the merge window), MERGE instead of appending
    — keep the longer text, and mark it spoken if either was. Deterministic: the model can freely
    reply-and-speak without producing duplicates (mechanism, not a "please don't repeat yourself").
    """
    text = (text or "").strip()
    if not text or is_degenerate(text) or has_junk_run(text):   # never surface a degeneration loop
        return
    path = config.workspace / "chat_replies.jsonl"
    try:
        lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    except OSError:
        lines = []

    if lines:
        try:
            last = json.loads(lines[-1])
        except Exception:  # noqa: BLE001
            last = None
        if isinstance(last, dict):
            lt = (last.get("text") or "").strip()
            same_utterance = lt and (lt == text or lt.startswith(text) or text.startswith(lt))
            recent = True
            try:  # bound the merge so two genuinely separate identical lines don't fold together
                import calendar
                age = time.time() - calendar.timegm(time.strptime(last.get("ts", ""), "%Y-%m-%dT%H:%M:%SZ"))
                recent = age <= _CHAT_MERGE_WINDOW_S
            except (ValueError, KeyError):
                recent = True
            if same_utterance and recent:
                last["text"] = (text if len(text) >= len(lt) else lt)[:2000]
                last["spoken"] = bool(spoken or last.get("spoken"))
                last = validate_chat_reply_record(last)
                lines[-1] = json.dumps(last)
                try:
                    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
                except OSError:
                    pass
                return

    entry = {"ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
             "text": text[:2000], "spoken": bool(spoken)}
    if tick is not None:
        entry["tick"] = int(tick)
    entry = validate_chat_reply_record(entry)
    try:
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")
    except OSError:
        pass


def read_recent_thoughts(config: Config, n: int = 6) -> list:
    """Return the last n thoughts (oldest first), each a dict with tick/ts/text."""
    try:
        lines = (config.workspace / "thoughts.jsonl").read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    out = []
    for ln in lines[-n:]:
        ln = ln.strip()
        if not ln:
            continue
        try:
            out.append(json.loads(ln))
        except (ValueError, json.JSONDecodeError):
            pass
    return out


# --- Aliases for backward compatibility (used by eidos.py, compaction.py, tools.py) ---



def append_observation(config: Config, entry: dict) -> None:
    """Append a single observation entry to observations.jsonl.

    Adds a timestamp if not present.
    """
    config.workspace.mkdir(parents=True, exist_ok=True)
    if "ts" not in entry:
        entry["ts"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    entry = validate_observation_record(entry)
    line = json.dumps(entry, ensure_ascii=False) + "\n"
    with open(config.observations_path, "a", encoding="utf-8") as f:
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
        with open(config.observations_path, encoding="utf-8") as f:
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
            entry = validate_observation_record(entry)
            result.append(entry)
            total_chars += len(line)
        except (json.JSONDecodeError, ValueError):
            # Skip malformed lines (possible crash corruption)
            continue

    return result


def count_observation_chars(config: Config) -> int:
    """Approximate token count via character count of observations.jsonl."""
    try:
        return config.observations_path.stat().st_size
    except FileNotFoundError:
        return 0


_OBS_ARCHIVE_MAX_BYTES = 4 * 1024 * 1024   # per archive file; ~weeks of compactions


def truncate_observations(config: Config) -> int:
    """Clear observations.jsonl after successful compaction.

    The distilled content now lives in memory/plan, so the raw
    observations are no longer needed for the loop.  Without this, the
    file grows monotonically and should_compact() fires every tick.

    The cleared lines are first appended to a dated archive
    (state/observations_archive_YYYYMM.jsonl) — dream extraction is
    lossy, and before this the raw record of what actually happened was
    gone forever the moment the dream finished.  Archives are for
    forensics/recovery only; nothing on the hot path reads them.

    Returns the number of lines removed.
    """
    try:
        with open(config.observations_path, encoding="utf-8") as f:
            lines = f.readlines()
    except FileNotFoundError:
        return 0

    removed = len(lines)
    # Archive before clearing — best-effort: an archive failure must never block compaction.
    try:
        import time as _t
        config.state_dir.mkdir(parents=True, exist_ok=True)
        arc = config.state_dir / f"observations_archive_{_t.strftime('%Y%m')}.jsonl"
        if not arc.exists() or arc.stat().st_size < _OBS_ARCHIVE_MAX_BYTES:
            with open(arc, "a", encoding="utf-8", errors="replace") as f:
                f.writelines(lines)
    except Exception:  # noqa: BLE001 - archive is best-effort
        pass
    # Atomic rewrite: empty the file
    with open(config.observations_path, "w", encoding="utf-8") as f:
        pass
    return removed


def count_observation_lines(config: Config) -> int:
    """Count lines in observations.jsonl."""
    try:
        with open(config.observations_path, encoding="utf-8") as f:
            return sum(1 for _ in f)
    except FileNotFoundError:
        return 0


def validate_observations(config: Config) -> int:
    """Validate observations.jsonl, truncating the last line if malformed.

    Returns number of lines truncated (0 or 1).
    """
    if not config.observations_path.exists():
        return 0

    with open(config.observations_path, encoding="utf-8") as f:
        lines = f.readlines()

    if not lines:
        return 0

    # Check last line
    last = lines[-1].strip()
    if not last:
        return 0

    try:
        validate_observation_record(json.loads(last))
        return 0
    except (json.JSONDecodeError, ValueError):
        # Last line is malformed — truncate it
        with open(config.observations_path, "w", encoding="utf-8") as f:
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
            replace_with_retry(path, path.with_suffix(path.suffix + ".done"))
        except OSError:
            continue

    return results

