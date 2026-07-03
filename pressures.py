"""The causal ledger (Pillars 0.3, PILLARS_PLAN §8 pitfall #12).

Unscripted/emergent behavior means a failure is *unattributable* unless the field that
produced it was recorded. eiDOS's behavior is the read-out of a deterministic "pressure
field" computed each tick in the glue (arousal + per-source drive floors, valence, strain,
condition, goal-tension, curiosity restlessness, energy reserve, the active objective and
its frustration, admitted afferents, the XP the tick paid). This module appends exactly one
record of that whole field per tick, so any action can be replayed as "show me the field
that produced this" — retrievable by tick number here and via the dashboard's /api/why.

Design (single writer, I6): the ledger accepts a PLAIN DICT. It never reaches into the
organ objects itself — the caller (eidos.py, end of tick) reads the live signals and hands
over a snapshot. That keeps the ledger decoupled from the nervous-system internals and
makes it trivially testable with synthetic records. `collect_field()` is an optional
convenience that builds that dict from the live objects, kept here so the wiring hunk in
eidos.py stays a one-liner and the signal→source mapping lives in one place.

Storage follows the house observations pattern (memory.truncate_observations): a bounded
live file `workspace/state/pressures.jsonl`; when it crosses the configured byte threshold
it is rolled into a dated monthly archive `state/pressures_archive_YYYYMM.jsonl` and the
live file starts fresh. Archives are forensic only; nothing on the hot path reads them.
Best-effort throughout — a ledger error must never break the tick (I5).
"""

from __future__ import annotations

import json
import time
from pathlib import Path


LEDGER_NAME = "pressures.jsonl"
ARCHIVE_PREFIX = "pressures_archive_"   # + YYYYMM.jsonl


def _ledger_path(config) -> Path:
    return config.state_dir / LEDGER_NAME


def _archive_path(config) -> Path:
    return config.state_dir / f"{ARCHIVE_PREFIX}{time.strftime('%Y%m')}.jsonl"


def _num(x, default=0.0):
    """Coerce a possibly-None organ attribute to a float, else the default."""
    try:
        return float(x)
    except (TypeError, ValueError):
        return default


def collect_field(
    *,
    tick: int,
    neuromod=None,
    goaltension=None,
    curiosity=None,
    metabolism=None,
    active_objective: dict | None = None,
    condition: str = "",
    strain: int = 0,
    admitted_events: int = 0,
    xp_delta: int = 0,
    xp_source: str = "",
) -> dict:
    """Build the plain pressure-field dict from the live organ objects.

    Every field is read defensively (organs may be disabled / None). The floors are the
    per-source tonic arousal floors the slow drives set on neuromod (curiosity restlessness,
    goal-tension incompletion); `drive_floor` is their max — the itch the body settles at.
    Kept here so the signal→source mapping is declared in ONE place, not smeared through the
    tick loop. The ledger's writer takes whatever dict it is handed — this is just the map.
    """
    floors = {}
    arousal = valence = drive_floor = 0.0
    if neuromod is not None:
        arousal = _num(getattr(neuromod, "arousal", 0.0))
        valence = _num(getattr(neuromod, "valence", 0.0))
        drive_floor = _num(getattr(neuromod, "drive_floor", 0.0))
        raw_floors = getattr(neuromod, "_drive_floors", None)
        if isinstance(raw_floors, dict):
            floors = {str(k): round(_num(v), 4) for k, v in raw_floors.items()}

    obj = active_objective or {}
    return {
        "tick": int(tick),
        "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        # Neuromod (arousal + affect) — the modulatory field.
        "arousal": round(arousal, 4),
        "valence": round(valence, 4),
        "drive_floor": round(drive_floor, 4),   # max of the per-source floors below
        "floors": floors,                        # per-source tonic arousal floors
        # Glue signals (Insula/DMN/ACC).
        "strain": int(strain),
        "condition": str(condition or ""),
        # Slow drives (Ventral Striatum incompletion pressure, curiosity restlessness).
        "goal_tension": round(_num(getattr(goaltension, "level", 0.0)), 4),
        "restlessness": round(_num(getattr(curiosity, "level", 0.0)), 4),
        # Metabolism — the energy reserve (0 empty .. 1 full).
        "energy_reserve": round(_num(getattr(metabolism, "energy", 0.0)), 4),
        # Objectives gate — what the creature was pushing on and how hard it hurt.
        "active_objective": (obj.get("title") or "") if isinstance(obj, dict) else "",
        "objective_frustration": int(obj.get("frustration", 0)) if isinstance(obj, dict) else 0,
        # Afferent intake — how many admitted sensory events fed this tick.
        "admitted_events": int(admitted_events),
        # The tick's payout — XP earned this tick and (derived) why.
        "xp_delta": int(xp_delta),
        "xp_source": str(xp_source or ""),
    }


class PressureLedger:
    """Single writer of the per-tick pressure field. Accepts a plain dict (decoupled, I6).

    `max_bytes` is the rotation threshold (config.pillars_causal_ledger_max_bytes): when the
    live file is at/over it, roll to a dated monthly archive and start fresh — the same
    bounded-file + monthly-archive shape as observations.jsonl.
    """

    def __init__(self, config, *, max_bytes: int):
        self.config = config
        self.max_bytes = int(max_bytes)

    def append(self, field: dict) -> None:
        """Append one field record (a plain dict). Best-effort; never raises into the loop."""
        try:
            self.config.state_dir.mkdir(parents=True, exist_ok=True)
            self._rotate_if_needed()
            line = json.dumps(field, ensure_ascii=False) + "\n"
            with open(_ledger_path(self.config), "a", encoding="utf-8") as f:
                f.write(line)
        except Exception:  # noqa: BLE001 - the ledger is best-effort (I5)
            pass

    def _rotate_if_needed(self) -> bool:
        """Roll the live file into this month's archive once it crosses max_bytes.

        Same shape as memory.truncate_observations: append the live lines to a dated archive,
        then empty the live file. Returns True if a rotation happened.
        """
        path = _ledger_path(self.config)
        try:
            if path.stat().st_size < self.max_bytes:
                return False
        except OSError:
            return False
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                lines = f.readlines()
            with open(_archive_path(self.config), "a", encoding="utf-8", errors="replace") as f:
                f.writelines(lines)
            with open(path, "w", encoding="utf-8"):
                pass
            return True
        except OSError:
            return False


def _iter_records(path: Path):
    """Yield parsed field records from a jsonl file, skipping malformed lines."""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    yield json.loads(line)
                except (ValueError, json.JSONDecodeError):
                    continue
    except OSError:
        return


def read_field_by_tick(config, tick: int) -> dict | None:
    """Retrieve the pressure field recorded for `tick` (the gate: replay by tick number).

    Searches the live ledger first (the common case — recent ticks), then the monthly
    archives newest-first, so a field rotated out of the live file is still retrievable.
    Returns the most recent record for that tick, or None if none was logged.
    """
    want = int(tick)
    # Live file first: scan in reverse so a re-logged tick returns its latest record.
    for rec in reversed(list(_iter_records(_ledger_path(config)))):
        if int(rec.get("tick", -1)) == want:
            return rec
    try:
        archives = sorted(config.state_dir.glob(f"{ARCHIVE_PREFIX}*.jsonl"), reverse=True)
    except OSError:
        archives = []
    for arc in archives:
        for rec in reversed(list(_iter_records(arc))):
            if int(rec.get("tick", -1)) == want:
                return rec
    return None


def read_recent_fields(config, n: int = 30) -> list[dict]:
    """The most recent `n` field records from the live ledger, newest-first (for a panel)."""
    recs = list(_iter_records(_ledger_path(config)))
    return list(reversed(recs[-n:]))
