"""The `remind` primitive — a persistent, restart-surviving timer (OPERATOR_DIRECTIVES §"The
`remind` primitive", invariants OD4/OD5).

A `bg_run "sleep 3600 && ..."` job dies the moment eidos restarts; a reminder does not. This is
the small durable book behind the `remind` tool: the creature (or the operator-directive path)
schedules a note to fire at an epoch second, and it fires on the FIRST tick whose clock has passed
that instant — surviving any number of restarts in between, because the schedule lives on disk, not
in a live sleeping subprocess.

Design (matches level_gates.GateState — atomic tmp+replace, fail-open, single writer per op):
  · store: state/reminders.json, a bounded list of reminder dicts.
  · a reminder = {id, note, fire_ts (epoch), created_ts, origin, source_key, fired}.
  · due(now) POPS (marks fired + persists) every reminder whose fire_ts has passed — so the loop's
    due-check both fires and consumes in one call, and a reminder can never fire twice.

Backward-clock safety (OD5): firing keys ONLY on `fire_ts <= now_ts`. A fire_ts in the future never
fires early no matter how the wall clock jitters; a clock that jumps BACKWARD simply leaves the
reminder pending (its fire_ts is now "in the future" again) — it is never lost, it just waits for
the clock to catch back up. Nothing here is computed from an elapsed-time delta, so a clock jump can
only delay a fire, never drop or duplicate one.

Fail-open (OD4): a missing or corrupt store reads as an empty list — a broken reminders file can
never crash the tick loop or make it hang; at worst a scheduled note is silently dropped, and the
next successful write heals the file.

Public API (the CONTRACT the tick loop + the operator-directive path build against — signatures are
frozen):
    set_reminder(config, note, *, fire_ts, origin="creature", source_key="") -> dict
    due(config, now_ts) -> list[dict]
    pending(config) -> list[dict]
    cancel(config, rid) -> bool
    parse_when(s) -> float | None
    render_pending(config, *, max_items=3) -> str   (lives in tools.py, per the task split)
"""
from __future__ import annotations

import json
import logging
import re
import time
import uuid
from datetime import datetime, timedelta
from typing import Optional

logger = logging.getLogger("eidos.reminders")

STATE_NAME = "reminders.json"
DEFAULT_MAX_PENDING = 32          # fallback when config lacks reminders_max_pending
_MAX_FUTURE_S = 366 * 86400       # a fire_ts more than ~1y out is almost certainly a parse/unit
                                  # blunder (ms-vs-s, a bad ISO year); reject it rather than seat a
                                  # reminder that would never realistically fire.


class ReminderError(Exception):
    """A typed refusal from the reminders store — the store is full or the time is invalid. The
    tool boundary turns this into an honest success=False result (ARCH #4); the loop path can catch
    it. `kind` is the fail taxonomy string ('blocked' for a full store, 'args' for a bad time)."""

    def __init__(self, message: str, *, kind: str = "blocked"):
        super().__init__(message)
        self.kind = kind


# --- store plumbing (atomic tmp+replace, fail-open — the GateState convention) ------------------

def _path(config):
    return config.state_dir / STATE_NAME


def _load(config) -> list[dict]:
    """Every reminder on disk. Missing/corrupt file → [] (fail-open, OD4)."""
    try:
        d = json.loads(_path(config).read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 - missing/corrupt file => no reminders
        return []
    items = d.get("reminders") if isinstance(d, dict) else d
    if not isinstance(items, list):
        return []
    out: list[dict] = []
    for it in items:
        if isinstance(it, dict) and "fire_ts" in it:
            out.append(it)
    return out


def _save(config, items: list[dict]) -> None:
    """Persist atomically. Best-effort like GateState.save — a failed write must not crash a tick."""
    try:
        config.state_dir.mkdir(parents=True, exist_ok=True)
        p = _path(config)
        tmp = p.with_suffix(".json.tmp")
        tmp.write_text(json.dumps({"reminders": items}, ensure_ascii=False), encoding="utf-8")
        try:
            import atomicio
            atomicio.replace_with_retry(tmp, p)
        except Exception:  # noqa: BLE001 - atomicio unavailable => plain replace (POSIX is fine)
            tmp.replace(p)
    except Exception:  # noqa: BLE001 - best-effort persistence
        logger.warning("reminders save failed", exc_info=True)


def _max_pending(config) -> int:
    try:
        return int(getattr(config, "reminders_max_pending", DEFAULT_MAX_PENDING))
    except Exception:  # noqa: BLE001 - a garbage override falls back to the declared default
        return DEFAULT_MAX_PENDING


# --- public API (frozen signatures) -------------------------------------------------------------

def set_reminder(config, note: str, *, fire_ts: float, origin: str = "creature",
                 source_key: str = "") -> dict:
    """Persist one reminder; return the stored dict. Raises ReminderError on a bad time
    (kind='args') or a full store (kind='blocked').

    Bounded by reminders_max_pending, counting only UNFIRED reminders (a fired-but-not-yet-swept
    entry never blocks a new one). Dedupe is EXACT on (note, fire_ts, source_key): re-setting the
    same reminder returns the existing one unchanged — set is idempotent, so an operator-directive
    that re-asserts the same reminder every tick seats exactly one."""
    note = (note or "").strip()
    if not note:
        raise ReminderError("a reminder needs a note — what to remember", kind="args")
    try:
        fire_ts = float(fire_ts)
    except (TypeError, ValueError):
        raise ReminderError("fire_ts must be an epoch second", kind="args")
    if not (fire_ts == fire_ts) or fire_ts in (float("inf"), float("-inf")):  # NaN/inf guard
        raise ReminderError("fire_ts must be a finite epoch second", kind="args")
    now = time.time()
    if fire_ts > now + _MAX_FUTURE_S:
        raise ReminderError("fire_ts is implausibly far in the future (>1y) — check the units",
                            kind="args")

    origin = origin if origin in ("creature", "operator") else "creature"
    source_key = (source_key or "").strip()

    items = _load(config)

    # Idempotent exact dedupe: same (note, fire_ts, source_key) among unfired => return it unchanged.
    for it in items:
        if (not it.get("fired")
                and it.get("note") == note
                and _same_ts(it.get("fire_ts"), fire_ts)
                and (it.get("source_key") or "") == source_key):
            return dict(it)

    pending_ct = sum(1 for it in items if not it.get("fired"))
    if pending_ct >= _max_pending(config):
        raise ReminderError(
            f"reminder store full ({pending_ct}/{_max_pending(config)} pending) — cancel or let "
            f"one fire before setting another", kind="blocked")

    rem = {
        "id": uuid.uuid4().hex[:12],
        "note": note,
        "fire_ts": fire_ts,
        "created_ts": now,
        "origin": origin,
        "source_key": source_key,
        "fired": False,
    }
    items.append(rem)
    _save(config, items)
    return dict(rem)


def due(config, now_ts: float) -> list[dict]:
    """Return AND pop every reminder whose fire_ts has passed (fire_ts <= now_ts). Popping = mark
    fired + persist, so a reminder fires exactly once. Fired reminders are also SWEPT from the store
    on this pass (they've served their purpose; the book stays bounded). Fail-open: a corrupt/missing
    store → [] and no write.

    Backward-clock safe (OD5): a future fire_ts is compared, never counted-down — it cannot fire
    early; a clock that jumps backward just re-futures a pending reminder, which then waits rather
    than being lost."""
    try:
        now_ts = float(now_ts)
    except (TypeError, ValueError):
        return []
    items = _load(config)
    if not items:
        return []

    fired_now: list[dict] = []
    keep: list[dict] = []
    changed = False
    for it in items:
        if it.get("fired"):
            # An already-fired straggler: sweep it (it fired on a prior pass).
            changed = True
            continue
        ts = it.get("fire_ts")
        try:
            ts = float(ts)
        except (TypeError, ValueError):
            # Unparseable fire_ts: drop it rather than let it wedge the store forever.
            changed = True
            continue
        if ts <= now_ts:
            it = dict(it)
            it["fired"] = True
            fired_now.append(it)
            changed = True   # consumed, not kept
        else:
            keep.append(it)

    if changed:
        _save(config, keep)
    return fired_now


def pending(config) -> list[dict]:
    """Unfired reminders, soonest first — for rendering/management. Read-only, fail-open."""
    items = [dict(it) for it in _load(config) if not it.get("fired")]
    items.sort(key=lambda it: _as_float(it.get("fire_ts")))
    return items


def cancel(config, rid: str) -> bool:
    """Remove the reminder with id `rid`. Returns True if one was removed, else False. Fail-open."""
    rid = (rid or "").strip()
    if not rid:
        return False
    items = _load(config)
    kept = [it for it in items if it.get("id") != rid]
    if len(kept) == len(items):
        return False
    _save(config, kept)
    return True


# --- when-parsing --------------------------------------------------------------------------------

# Accepted forms for parse_when (all resolved RELATIVE to time.time() at call):
#   RELATIVE durations, composable, unit-suffixed integers (whitespace optional between parts):
#       "90s"  "10m"  "2h"  "3d"  "1h30m"  "2h15m30s"  "1d12h"
#       units: s(econds) m(inutes) h(ours) d(ays); at least one unit required.
#   CLOCK time today (or tomorrow if already past): "at 22:15"  "22:15"  "at 9:05"
#       24-hour HH:MM; picks the next occurrence of that wall time.
#   ISO-8601 absolute timestamp: "2026-07-20T22:15:00"  "2026-07-20 22:15"  "2026-07-20T22:15:00Z"
#       (a bare date "2026-07-20" resolves to that date at 00:00 local).
# Anything else (empty, prose, a bad number, "0m", a negative offset) → None.

_DUR_RE = re.compile(r"(\d+(?:\.\d+)?)\s*([smhd])", re.IGNORECASE)
_DUR_UNITS = {"s": 1.0, "m": 60.0, "h": 3600.0, "d": 86400.0}
_CLOCK_RE = re.compile(r"^(?:at\s+)?([01]?\d|2[0-3]):([0-5]\d)$", re.IGNORECASE)


def parse_when(s: str) -> Optional[float]:
    """Turn a when-string into an epoch fire_ts (relative to time.time()), or None if unparseable.
    Accepted forms are documented in the module comment above `_DUR_RE`."""
    if not isinstance(s, str):
        return None
    raw = s.strip()
    if not raw:
        return None
    now = time.time()

    # 1) clock time — "at 22:15" / "22:15" (next occurrence today or tomorrow).
    m = _CLOCK_RE.match(raw)
    if m:
        hh, mm = int(m.group(1)), int(m.group(2))
        base = datetime.fromtimestamp(now)
        target = base.replace(hour=hh, minute=mm, second=0, microsecond=0)
        if target.timestamp() <= now:
            target = target + timedelta(days=1)   # already past today → next occurrence
        return target.timestamp()

    # 2) relative composite duration — "1h30m", "90s", "2h". The whole string must be durations.
    low = raw.lower()
    if _is_pure_duration(low):
        total = 0.0
        for num, unit in _DUR_RE.findall(low):
            total += float(num) * _DUR_UNITS[unit.lower()]
        if total > 0:
            return now + total
        return None   # "0s" / "0m" — a non-future offset is not a reminder

    # 3) ISO-8601 absolute timestamp (or bare date).
    ts = _parse_iso(raw)
    if ts is not None:
        return ts

    return None


def _is_pure_duration(low: str) -> bool:
    """True iff `low` is composed ENTIRELY of duration tokens (so "meet at 3pm" doesn't parse as
    3 somethings). Strips every duration match; what remains must be whitespace only, and there must
    have been at least one match."""
    if not _DUR_RE.search(low):
        return False
    remainder = _DUR_RE.sub("", low)
    return remainder.strip() == ""


def _parse_iso(raw: str) -> Optional[float]:
    """Parse an ISO-8601 timestamp to an epoch second, or None. Tolerates a trailing 'Z' (UTC), a
    space instead of 'T', and a bare date (→ midnight local)."""
    cand = raw.strip()
    tz_utc = False
    if cand.endswith(("Z", "z")):
        cand = cand[:-1]
        tz_utc = True
    cand = cand.replace(" ", "T", 1) if ("T" not in cand and " " in cand) else cand
    try:
        dt = datetime.fromisoformat(cand)
    except (ValueError, TypeError):
        return None
    try:
        if tz_utc and dt.tzinfo is None:
            from datetime import timezone
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except (ValueError, OverflowError, OSError):
        return None


# --- helpers -------------------------------------------------------------------------------------

def _as_float(v) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return float("inf")


def _same_ts(a, b) -> bool:
    """Exact-enough fire_ts equality for dedupe — epoch seconds within 1ms are 'the same instant'."""
    try:
        return abs(float(a) - float(b)) < 1e-3
    except (TypeError, ValueError):
        return False
