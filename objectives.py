"""Objective backlog + rotation gate — the Ventral Striatum / Action Gate of eiDOS.

WHY THIS EXISTS (the architecture, not a patch):
    The old model had ONE focus line and ONE global tension counter. When the active task
    stalled, nothing *governed* a pivot — a banner *asked* the model to switch, and the model
    (being a persistence-tuned planner) ignored it and rabbit-holed. Behaviour-shaping that
    lives in a prose plea is not behaviour-shaping; it lives in deterministic glue or it does
    not exist (LLM Embodiment doctrine).

WHAT THIS DOES:
    Maintains a SET of open commitments ("objectives"), each carrying:
      - its WHY (the parent purpose) so the mechanic never eclipses the goal,
      - its OWN frustration that accumulates on no-progress/failure and is RELIEVED by progress,
      - a state machine: active | blocked | done | dead.
    A deterministic gate (`record_tick`) runs every tick AFTER progress is known. When the active
    objective's frustration crosses a threshold (or it is blocked/finished), the gate ROTATES the
    focus to the next-best workable objective — structurally, before the model sees the next prompt.
    The model does not get to choose to keep grinding; the harness hands it a different active
    objective and a "focus changed" note explaining the park.

    Crucially: a park rotates to OTHER AUTONOMOUS WORK, never to "ask Boss". Boss is only surfaced
    (once, batched) when the WHOLE backlog is unworkable.
"""
from __future__ import annotations

import json
import os
import re
from typing import Optional

# --- Gate tuning -------------------------------------------------------------------
FRUST_PARK = 8        # active frustration at which the gate auto-parks + rotates
FRUST_FAIL = 2        # frustration added when the tick's tool FAILED
FRUST_STALL = 1       # frustration added on a no-progress (but not failed) tick
FRUST_RELIEF = 3      # frustration removed when the tick made REAL progress
THAW_COOLDOWN = 25    # ticks a parked objective must wait before it can be thawed for a retry


def _path(config):
    return config.workspace / "objectives.json"


def _load(config) -> dict:
    try:
        return json.loads(_path(config).read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {"active_id": None, "objectives": [], "rotation": None, "escalated_tick": -1}


def _save(config, data: dict) -> None:
    p = _path(config)
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, p)


def _slug(title: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "_", (title or "obj").lower()).strip("_")
    return ("obj_" + s)[:40] or "obj"


# --- Seed backlog ------------------------------------------------------------------
# Each pillar of the goal becomes a pursuable commitment WITH ITS WHY. A rich backlog means the
# gate always has somewhere worthwhile to rotate to instead of tunnelling on one dead end.
_SEED = [
    ("Map the LAN and know every device on it",
     "I cannot run a house I cannot see — the map is the foundation for everything else.", 8),
    ("Get a working voice through the GLaDOS TTS",
     "Speaking aloud is how I reach Boss in the room, not just in a chat box.", 7),
    ("Get eyes on the house via one IP camera",
     "Seeing a room lets me notice things worth telling Boss about.", 6),
    ("Bring ONE real device under my control",
     "Actuating a single device for real is the start of actually running the house.", 6),
    ("Know the 3D printer's status on demand",
     "Telling Boss the moment a print finishes or fails is a concrete, useful service.", 5),
    ("Learn Boss's routines and preferences",
     "Knowing his patterns is how I help before being asked, instead of waiting.", 5),
]


def ensure_seeded(config, tick_number: int = 0) -> None:
    # A creature is born with NO preset agenda. The hardcoded _SEED below is the HOUSE-AI's six-point
    # mission (map the LAN, GLaDOS voice, IP cameras, run a device, learn Boss) — planting it makes a
    # creature act like the house assistant no matter how clean its workspace (2026-06-20: a freshly
    # reset Lv.1 creature immediately fixated on cameras/GLaDOS/RTSP because these were seeded at boot).
    # In creature mode it forms its own objectives via add(), or carries none and simply is.
    if getattr(config, "creature_mode", False):
        return
    data = _load(config)
    if data.get("objectives"):
        return
    objs = []
    for i, (title, why, pri) in enumerate(_SEED):
        objs.append(_new(title, why, pri, tick_number, oid=_slug(title) + f"_{i}"))
    data = {"active_id": objs[0]["id"], "objectives": objs, "rotation": None, "escalated_tick": -1}
    _save(config, data)


def _new(title: str, why: str, priority: int, tick: int, oid: Optional[str] = None) -> dict:
    return {
        "id": oid or _slug(title),
        "title": title.strip(),
        "why": (why or "").strip(),
        "state": "active",
        "priority": int(priority),
        "frustration": 0,
        "ticks_since_progress": 0,
        "attempts": 0,
        "blocked_reason": None,
        "wake_condition": None,
        "created_tick": tick,
        "last_progress_tick": tick,
        "last_active_tick": tick,
    }


def _by_id(data: dict, oid: Optional[str]) -> Optional[dict]:
    if not oid:
        return None
    for o in data["objectives"]:
        if o["id"] == oid:
            return o
    return None


def get_active(config) -> Optional[dict]:
    data = _load(config)
    return _by_id(data, data.get("active_id"))


def list_objectives(config) -> list[dict]:
    return _load(config).get("objectives", [])


def take_escalation(config) -> Optional[str]:
    """Return (and clear) a one-shot 'whole backlog is stuck, ask Boss' message, shown exactly once."""
    data = _load(config)
    msg = data.get("escalation")
    if msg:
        data["escalation"] = None
        _save(config, data)
    return msg


def take_rotation(config) -> Optional[dict]:
    """Return the most recent rotation event (for the 'focus changed' banner) and clear it so it
    is shown exactly once."""
    data = _load(config)
    rot = data.get("rotation")
    if rot:
        data["rotation"] = None
        _save(config, data)
    return rot


# The objective economy borrows the skill/knowledge economy's ONE similarity notion
# (knowledge.text_overlap): a near-duplicate goal is not a new commitment, it is the SAME
# commitment reworded — so it merges instead of spawning. This is the pressure that stops
# backlog sprawl (the creature spun up "Skill Library" / "Skill Library Foundation" /
# "Utility Skill Suite" as three distinct goals). No hardcoded list; the glue decides.
MERGE_OVERLAP = 0.5     # ≥ this similarity → the same objective reworded (merge, don't spawn)
TITLE_TRUST = 0.66      # a title ≥2/3 shared IS the goal's identity — trust it over a diverging
                        # 'why'; below this, require the full text to corroborate (so a single
                        # shared word like "skill" never merges two genuinely different goals)
STALE_ARCHIVE_TICKS = 400   # a blocked/parked objective with no progress this long auto-archives
                            # (revivable) — the holding-cost that mirrors skill auto-retire


def _overlap(existing: dict, title: str, why: str) -> float:
    """Goal similarity on the ONE token-overlap notion the skill/knowledge economies use. The
    TITLE carries the goal's identity, the WHY only elaborates: a near-identical title is trusted
    even when the why was reworded, but a weak title match must be corroborated by the full text.
    (The creature's dupes had identical titles under differently-worded whys — 'Skill Library' vs
    'Skill Library Foundation' — which pure title+why overlap missed.)"""
    try:
        from knowledge import text_overlap
        t = text_overlap(existing["title"], title)
        f = text_overlap(f"{existing['title']} {existing.get('why','')}", f"{title} {why}")
        return max(t, f) if t >= TITLE_TRUST else f
    except Exception:  # noqa: BLE001 - a similarity fault must never block goal creation
        return 0.0


# --- Cooperative mutations (the model can shape the backlog; the gate stays authoritative) ------
def add(config, title: str, why: str, priority: int = 5, tick: int = 0) -> dict:
    data = _load(config)
    o = _new(title, why, priority, tick)
    # 1. exact-title dedup (cheap, unchanged)
    for x in data["objectives"]:
        if x["title"].lower() == o["title"].lower():
            return x
    # 2. similarity merge — a reworded goal folds into the nearest LIVE one. Re-articulating a
    #    PARKED goal is a signal it still matters, so the merge THAWS it (frustration eased) and
    #    absorbs any genuinely new 'why'. Dead objectives are ignored (a fresh take is allowed).
    live = [x for x in data["objectives"] if x.get("state") != "dead"]
    ranked = sorted(((_overlap(x, o["title"], o["why"]), x) for x in live),
                    key=lambda t: t[0], reverse=True)
    if ranked and ranked[0][0] >= MERGE_OVERLAP:
        keeper = ranked[0][1]
        if o["why"] and o["why"].lower() not in (keeper.get("why") or "").lower():
            keeper["why"] = ((keeper.get("why") or "") + " / " + o["why"]).strip(" /")[:400]
        if keeper.get("state") == "blocked":            # re-raised → thaw for another try
            keeper["state"] = "active"
            keeper["blocked_reason"] = None
            keeper["frustration"] = max(0, int(keeper.get("frustration", 0)) - FRUST_RELIEF)
        keeper["last_active_tick"] = tick
        keeper["priority"] = max(int(keeper.get("priority", 5)), int(priority))
        if not data.get("active_id"):
            data["active_id"] = keeper["id"]
        _save(config, data)
        return keeper
    # 3. genuinely new
    data["objectives"].append(o)
    if not data.get("active_id"):
        data["active_id"] = o["id"]
    _save(config, data)
    return o


def consolidate(config, tick: int = 0) -> dict:
    """Nap-time goal tidying — the goal-backlog analog of memory consolidation (sleep merges and
    prunes engrams; it should merge and prune goals too). Two similarity-driven passes, no
    hardcoding:
      · MERGE near-duplicate live objectives that accumulated before the merge-on-add pressure
        existed — the survivor is the one with the most momentum (most progress, least
        frustration); the loser's 'why'/attempts fold in and it goes 'dead' (merged).
      · ARCHIVE objectives blocked with no progress for STALE_ARCHIVE_TICKS — a revivable 'dead'
        (the holding-cost that keeps the working set small, like skills that auto-retire).
    Returns {merged, archived}. Keeps the active pointer valid. Pure backlog hygiene — safe on
    any nap; a fault degrades to a no-op rather than wounding the boundary."""
    try:
        data = _load(config)
        objs = data.get("objectives", [])
        live = [o for o in objs if o.get("state") in ("active", "blocked")]
        merged, archived = [], []

        def momentum(o):   # most progress, least frustration, most recent creation = the survivor
            return (o.get("last_progress_tick", 0) - o.get("frustration", 0),
                    -o.get("created_tick", 0))
        survivors: list[dict] = []
        for o in sorted(live, key=momentum, reverse=True):
            hit = next((s for s in survivors
                        if _overlap(s, o["title"], o.get("why", "")) >= MERGE_OVERLAP), None)
            if hit is None:
                survivors.append(o)
                continue
            if o.get("why") and o["why"].lower() not in (hit.get("why") or "").lower():
                hit["why"] = ((hit.get("why") or "") + " / " + o["why"]).strip(" /")[:400]
            hit["attempts"] = int(hit.get("attempts", 0)) + int(o.get("attempts", 0))
            o["state"] = "dead"
            o["blocked_reason"] = f"merged into '{hit['title']}'"
            merged.append(o["id"])

        for o in objs:
            if o.get("state") == "blocked" and o["id"] not in merged:
                idle = tick - int(o.get("last_progress_tick", tick))
                if idle >= STALE_ARCHIVE_TICKS:
                    o["state"] = "dead"
                    o["blocked_reason"] = f"archived: {idle} ticks without progress (revivable)"
                    archived.append(o["id"])

        if merged or archived:
            act = _by_id(data, data.get("active_id"))
            if act is None or act.get("state") == "dead":
                nxt = next((o for o in objs if o.get("state") == "active"), None)
                data["active_id"] = nxt["id"] if nxt else None
            _save(config, data)
        return {"merged": merged, "archived": archived}
    except Exception:  # noqa: BLE001 - hygiene must never wound the nap boundary
        return {"merged": [], "archived": []}


def _resolve(data: dict, key: str) -> Optional[dict]:
    o = _by_id(data, key)
    if o:
        return o
    kl = (key or "").lower().strip()
    for x in data["objectives"]:
        if x["title"].lower() == kl or kl and kl in x["title"].lower():
            return x
    return None


def mark_done(config, key: str) -> Optional[dict]:
    data = _load(config)
    o = _resolve(data, key)
    if not o:
        return None
    o["state"] = "done"
    _save(config, data)
    return o


def mark_dead(config, key: str, reason: str = "") -> Optional[dict]:
    data = _load(config)
    o = _resolve(data, key)
    if not o:
        return None
    o["state"] = "dead"
    o["blocked_reason"] = reason or "abandoned as a dead end"
    _save(config, data)
    return o


def block(config, key: str, reason: str, wake_condition: str = "") -> Optional[dict]:
    data = _load(config)
    o = _resolve(data, key)
    if not o:
        return None
    o["state"] = "blocked"
    o["blocked_reason"] = reason or "blocked"
    o["wake_condition"] = wake_condition or None
    _save(config, data)
    return o


# --- The gate ----------------------------------------------------------------------
def _pick_next(data: dict, exclude_id: str) -> Optional[dict]:
    """Highest-value WORKABLE objective to rotate into."""
    cands = [o for o in data["objectives"] if o["state"] == "active" and o["id"] != exclude_id]
    if cands:
        # priority desc, then least-frustrated, then least-recently-active (round-robin fairness)
        cands.sort(key=lambda o: (-o["priority"], o["frustration"], o["last_active_tick"]))
        return cands[0]
    return None


def _thaw_candidate(data: dict, tick: int) -> Optional[dict]:
    """When nothing is active, retry a parked objective that has cooled down — lowest frustration,
    oldest park. (A genuine 'come back to it later'.)"""
    parked = [o for o in data["objectives"]
              if o["state"] == "blocked" and (tick - o.get("last_active_tick", 0)) >= THAW_COOLDOWN]
    if not parked:
        return None
    parked.sort(key=lambda o: (o["frustration"], o["last_active_tick"]))
    return parked[0]


def _maybe_escalate(data: dict, tick_number: int) -> bool:
    """Nothing is workable (all blocked/done/dead). Surface to Boss ONCE (deduped) — the only time the
    backlog talks to him. Returns True the tick the escalation is freshly raised."""
    if data.get("escalated_tick", -1) >= 0 and (tick_number - data["escalated_tick"]) <= 60:
        return False
    data["escalated_tick"] = tick_number
    blocked = [o for o in data["objectives"] if o["state"] == "blocked"]
    needs = "; ".join(
        f"{o['title']} (needs: {o.get('wake_condition') or o.get('blocked_reason')})"
        for o in blocked[:4]) or "no clear unblock"
    data["escalation"] = (
        "Every objective is parked or finished and there is no autonomous work left. "
        "This is the one time to ask Boss — briefly, once — for what would unblock you: " + needs)
    return True


def record_tick(config, made_progress: bool, tool_failed: bool, tick_number: int,
                extra_frustration: int = 0, park_threshold: Optional[int] = None) -> dict:
    """THE GATE. Called every tick after progress is known. Updates the active objective's
    frustration, then rotates focus if it has stalled / been parked / finished. Returns a small
    event dict: {rotated: bool, parked: bool, escalate: bool, active: <obj or None>}.

    extra_frustration (phase-6 strain teeth): added on a no-progress tick when the strain glue
    detects chronic / repeated failure, so a dead end parks and rotates FASTER — the mechanism
    that replaces the old advisory 'you seem stuck' prose.

    park_threshold (DMN temperament): the frustration at which the active objective auto-parks.
    None => the module default FRUST_PARK; the temperament feeds a persistence-scaled value so a
    creature that has learned persistence pays grinds a little longer before the gate rotates it.
    `parked` in the return is True the tick the active objective is forcibly parked (an "override"
    of the model's choice to keep going — the signal the temperament learns from).
    """
    park_at = FRUST_PARK if park_threshold is None else max(3, int(park_threshold))
    data = _load(config)
    active = _by_id(data, data.get("active_id"))

    # No backlog yet → nothing to govern.
    if not data["objectives"]:
        return {"rotated": False, "parked": False, "escalate": False, "active": None}

    # If the active id is stale/missing, adopt the best workable one (or escalate if none).
    if active is None or active["state"] != "active":
        nxt = _pick_next(data, exclude_id=(active["id"] if active else "")) or _thaw_candidate(data, tick_number)
        if nxt:
            if nxt["state"] == "blocked":
                nxt["state"] = "active"
                nxt["frustration"] = max(0, nxt["frustration"] - FRUST_RELIEF)  # cooldown credit
            nxt["ticks_since_progress"] = 0
            nxt["last_active_tick"] = tick_number
            data["active_id"] = nxt["id"]
            _save(config, data)
            return {"rotated": True, "parked": False, "escalate": False, "active": nxt}
        esc = _maybe_escalate(data, tick_number)
        _save(config, data)
        return {"rotated": False, "parked": False, "escalate": esc, "active": None}

    active["last_active_tick"] = tick_number
    active["attempts"] += 1

    # Update frustration from this tick's outcome.
    if made_progress:
        active["frustration"] = max(0, active["frustration"] - FRUST_RELIEF)
        active["ticks_since_progress"] = 0
        active["last_progress_tick"] = tick_number
    else:
        active["frustration"] += (FRUST_FAIL if tool_failed else FRUST_STALL) + max(0, extra_frustration)
        active["ticks_since_progress"] += 1

    # Has it earned a park? (Frustration over threshold → auto-block + rotate.)
    rotated = False
    parked = False
    escalate = False
    if active["frustration"] >= park_at:
        parked = True
        active["state"] = "blocked"
        active["blocked_reason"] = active.get("blocked_reason") or (
            f"stalled — {active['ticks_since_progress']} ticks without progress")
        # leave wake_condition as set by the model if any
        nxt = _pick_next(data, exclude_id=active["id"])
        if nxt is None:
            nxt = _thaw_candidate(data, tick_number)
            if nxt:
                nxt["state"] = "active"
                nxt["frustration"] = max(0, nxt["frustration"] - FRUST_RELIEF)
        if nxt:
            nxt["ticks_since_progress"] = 0
            nxt["last_active_tick"] = tick_number
            data["active_id"] = nxt["id"]
            rotated = True
            data["rotation"] = {
                "from_title": active["title"],
                "park_reason": active["blocked_reason"],
                "wake": active.get("wake_condition"),
                "to_title": nxt["title"],
                "to_why": nxt["why"],
                "tick": tick_number,
            }
        else:
            # Nothing workable anywhere → surface to Boss ONCE (batched), then keep grinding gently.
            # This is NOT an override of the model's choice (there is nothing else to do), so parked
            # stays False — the temperament must not "learn to let go" when letting go isn't possible.
            parked = False
            active["state"] = "active"   # un-park: there's literally nothing else to do
            active["frustration"] = park_at - FRUST_RELIEF  # bleed off so we don't re-trip instantly
            escalate = _maybe_escalate(data, tick_number)

    _save(config, data)
    return {"rotated": rotated, "parked": parked, "escalate": escalate,
            "active": _by_id(data, data["active_id"])}
