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
EXPOSURE_CAP = 2      # a SELF-set goal that re-parks after this many tested thaw-retrials WITHOUT ever
                      # making STRONG progress is released as DEAD — the gate's terminal state, so an
                      # impossible self-goal ("Map the Outside") can't be re-thawed into the creature's
                      # face forever (the 2026-07-13 doom loop). Each thaw is a controlled retrial;
                      # 2 buys ~20+ attempts across campaigns — not flighty. A single STRONG-progress
                      # tick refutes the block and resets exposures to 0 (a hard-but-doable goal keeps
                      # its full budget). Provenance-safe: only self-set objectives reach this gate —
                      # operator commissions / System quests / survival live in other systems untouched.
STRONG_STALL_PARK_TICKS = 60   # a goal that makes no STRONG progress (new knowledge/skill/completion)
                      # for this many ticks is force-parked — even if it keeps its frustration low by
                      # minting cosmetic files (WEAK progress relieves frustration by design, so an
                      # impossible file-minting self-goal would otherwise never park, never accrue
                      # exposures, and never reach the death gate). Parking (not death) routes it into
                      # the exposure/death cascade; a single strong-progress tick resets the clock, so
                      # a legitimately-deep goal that periodically lands real progress never trips it.
                      # Generous on purpose (~2.5 min of ticks) so genuine deep work is rarely parked;
                      # operator-tunable.


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


def _unique_id(data: dict, base: str) -> str:
    """A slug is title-derived, so re-articulating a title that matches a DONE/DEAD goal would
    collide (two objectives sharing an id — _by_id returns the wrong one). Suffix until unique."""
    existing = {o["id"] for o in data.get("objectives", [])}
    if base not in existing:
        return base
    n = 2
    while f"{base}_{n}" in existing:
        n += 1
    return f"{base}_{n}"


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
        "last_strong_progress_tick": tick,   # STRONG-only clock (see STRONG_STALL_PARK_TICKS)
        "last_active_tick": tick,
        "exposures": 0,          # times thawed from a block — an "impossibility" belief RE-TESTED
    }


def _thaw(o: dict, tick: int) -> None:
    """Re-activate a blocked objective for another attempt (exposure). A block is a belief — 'I
    can't make progress here' — and a belief earns its keep by being TESTED, not avoided. Each thaw
    counts as one exposure and tags the objective so the gate can recognise a REFUTATION (a
    formerly-blocked goal that then makes progress = the belief was wrong, a maximally-informative
    event that deserves surprise)."""
    o["state"] = "active"
    o["frustration"] = max(0, int(o.get("frustration", 0)) - FRUST_RELIEF)   # cooldown credit
    o["exposures"] = int(o.get("exposures", 0)) + 1
    o["_thawed_from_block"] = True
    o["ticks_since_progress"] = 0
    o["last_active_tick"] = tick


def _dead_if_exposure_spent(o: dict) -> Optional[dict]:
    """A blocked objective about to be re-thawed but whose exposure budget is already spent (thawed
    EXPOSURE_CAP times with no STRONG progress in between — strong progress resets exposures to 0) is
    RELEASED DEAD instead of thawed. This is the single choke point EVERY re-activation passes
    through, so a futile goal can't dodge the death gate by ping-ponging through the model's own
    objective_block tool (which re-blocks without ever routing through the frustration-park branch
    where the death check used to live exclusively). Returns the died-event dict, or None to thaw."""
    if int(o.get("exposures", 0)) >= EXPOSURE_CAP:
        o["state"] = "dead"
        o["blocked_reason"] = (f"released: {o.get('exposures')} tested retrials without real "
                               f"progress — accepted as not mine to do")
        return {"title": o["title"], "reason": o["blocked_reason"]}
    return None


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
MERGE_SIM = 0.6         # ≥ this Jaccard similarity → the same objective reworded (merge, don't
                        # spawn). Jaccard, NOT overlap-coefficient: a subset title ('Skill Library'
                        # ⊂ 'Skill Library Governance Board') must NOT merge a distinct larger goal.
STALE_ARCHIVE_TICKS = 400   # a blocked/parked objective with no progress this long auto-archives
                            # (revivable) — the holding-cost that mirrors skill auto-retire


def _overlap(existing: dict, title: str, why: str) -> float:
    """Goal similarity: symmetric Jaccard over the same content-token tokenizer the skill/knowledge
    economies use, scored on the TITLE (the goal's identity) OR the full title+why, whichever binds
    them tighter. Jaccard penalises divergent scope, so an elaboration ('Skill Library Foundation')
    merges with 'Skill Library' but a distinct larger commitment ('Skill Library Governance Board')
    does not — the false-positive direction (destroying a real goal) is the one to guard."""
    try:
        from knowledge import token_jaccard
        t = token_jaccard(existing["title"], title)
        f = token_jaccard(f"{existing['title']} {existing.get('why','')}", f"{title} {why}")
        return max(t, f)
    except Exception:  # noqa: BLE001 - a similarity fault must never block goal creation
        return 0.0


# --- Cooperative mutations (the model can shape the backlog; the gate stays authoritative) ------
def add(config, title: str, why: str, priority: int = 5, tick: int = 0) -> dict:
    data = _load(config)
    o = _new(title, why, priority, tick)
    # Dedup/merge only against LIVE goals (active/blocked). A DONE or DEAD goal is history — a
    # re-articulation ("Map the LAN again") is a fresh commitment, never folded into a finished one
    # (which _pick_next could never work again — it selects only active).
    live = [x for x in data["objectives"] if x.get("state") in ("active", "blocked")]
    # 1. exact-title dedup among live (a re-raised parked goal thaws)
    for x in live:
        if x["title"].lower() == o["title"].lower():
            if x.get("state") == "blocked":
                x["state"] = "active"
                x["blocked_reason"] = None
                x["frustration"] = max(0, int(x.get("frustration", 0)) - FRUST_RELIEF)
                x["last_active_tick"] = tick
                _save(config, data)
            return x
    # 2. similarity merge — a reworded goal folds into the nearest LIVE one. Re-articulating a
    #    PARKED goal is a signal it still matters, so the merge THAWS it and absorbs any new 'why'.
    ranked = sorted(((_overlap(x, o["title"], o["why"]), x) for x in live),
                    key=lambda t: t[0], reverse=True)
    if ranked and ranked[0][0] >= MERGE_SIM:
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
    # 3. genuinely new — ensure the id is unique (a done/dead goal may own this title's slug)
    o["id"] = _unique_id(data, o["id"])
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
                        if _overlap(s, o["title"], o.get("why", "")) >= MERGE_SIM), None)
            if hit is None:
                survivors.append(o)
                continue
            if o.get("why") and o["why"].lower() not in (hit.get("why") or "").lower():
                hit["why"] = ((hit.get("why") or "") + " / " + o["why"]).strip(" /")[:400]
            hit["attempts"] = int(hit.get("attempts", 0)) + int(o.get("attempts", 0))
            o["state"] = "dead"
            o["blocked_reason"] = f"merged into '{hit['title']}'"
            merged.append(o["id"])

        exposed = []
        for o in objs:
            if o.get("state") == "blocked" and o["id"] not in merged:
                idle = tick - int(o.get("last_progress_tick", tick))
                if idle < STALE_ARCHIVE_TICKS:
                    continue
                # A belief may not be BURIED untested. A stale block that was never re-tested
                # (exposures == 0) gets force-thawed at the nap instead of archived — exposure
                # therapy: "you've avoided this for a long time; wake and test it." Only a block
                # that HAS been re-tested and stayed stuck (exposures ≥ 1) earns the archive.
                if int(o.get("exposures", 0)) < 1:
                    _thaw(o, tick)
                    exposed.append(o["id"])
                else:
                    o["state"] = "dead"
                    o["blocked_reason"] = f"archived: {idle} ticks without progress (revivable)"
                    archived.append(o["id"])

        if merged or archived or exposed:
            act = _by_id(data, data.get("active_id"))
            if act is None or act.get("state") == "dead":
                # Prefer an active goal; else a workable BLOCKED survivor (record_tick thaws it) —
                # only null the pointer when nothing is workable, never while a survivor exists.
                nxt = (next((o for o in objs if o.get("state") == "active"), None)
                       or next((o for o in objs if o.get("state") == "blocked"), None))
                data["active_id"] = nxt["id"] if nxt else None
            _save(config, data)
        return {"merged": merged, "archived": archived, "exposed": exposed}
    except Exception:  # noqa: BLE001 - hygiene must never wound the nap boundary
        return {"merged": [], "archived": [], "exposed": []}


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
                extra_frustration: int = 0, park_threshold: Optional[int] = None,
                progress_strong: bool = True) -> dict:
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
                died = _dead_if_exposure_spent(nxt)   # spent its budget → release dead, don't re-thaw
                if died:
                    data["active_id"] = None          # the released goal must not remain the active id
                    esc = _maybe_escalate(data, tick_number)
                    _save(config, data)
                    return {"rotated": False, "parked": False, "escalate": esc, "active": None,
                            "died": died, "refuted_block": None}
                _thaw(nxt, tick_number)             # exposure: re-test the belief
            nxt["ticks_since_progress"] = 0
            nxt["last_active_tick"] = tick_number
            data["active_id"] = nxt["id"]
            _save(config, data)
            return {"rotated": True, "parked": False, "escalate": False, "active": nxt,
                    "refuted_block": None}
        esc = _maybe_escalate(data, tick_number)
        _save(config, data)
        return {"rotated": False, "parked": False, "escalate": esc, "active": None}

    active["last_active_tick"] = tick_number
    active["attempts"] += 1
    # Grandfather objectives created before this field existed: treat "unknown" as "strong progress
    # just happened" so the upgrade never force-parks an in-flight goal on its first observed tick.
    active.setdefault("last_strong_progress_tick", tick_number)

    # Update frustration from this tick's outcome.
    refuted_block = None
    if made_progress:
        # Two-tier progress: WEAK (a new workspace file) relieves frustration and resets the stall
        # counter — writing things is real work for an ordinary goal. But only STRONG progress (new
        # knowledge / new skill / a completion) proves CONTROLLABILITY: it alone refutes a block and
        # resets the exposure budget. This closes the 2026-07-13 hole where a despairing DIARY entry
        # about an impossible goal counted as progress, relieved its frustration, and even "refuted"
        # its own correct impossibility-belief — brooding was the optimal policy.
        if progress_strong:
            if active.pop("_thawed_from_block", False):
                refuted_block = {"title": active["title"],
                                 "reason": str(active.get("blocked_reason") or "")}
            active["exposures"] = 0                     # controllability proven → full budget restored
            active["last_strong_progress_tick"] = tick_number   # reset the strong-stall clock
        active["frustration"] = max(0, active["frustration"] - FRUST_RELIEF)
        active["ticks_since_progress"] = 0
        active["last_progress_tick"] = tick_number
    else:
        active["frustration"] += (FRUST_FAIL if tool_failed else FRUST_STALL) + max(0, extra_frustration)
        active["ticks_since_progress"] += 1

    # Has it earned a park? Two independent triggers, then the same block/DIE decision:
    #   · frustration over threshold (the ordinary stall/strain path), OR
    #   · STRONG-progress stall — no new knowledge/skill/completion for STRONG_STALL_PARK_TICKS.
    # The second closes the "immortal impossible goal" hole: WEAK progress (a cosmetic file) relieves
    # frustration by design, so a goal that mints files forever would never trip the frustration gate;
    # the strong-stall clock parks it anyway and routes it into the exposure/death cascade.
    rotated = False
    parked = False
    escalate = False
    died = None
    strong_stall = tick_number - int(active.get("last_strong_progress_tick", tick_number))
    if active["frustration"] >= park_at or strong_stall >= STRONG_STALL_PARK_TICKS:
        active.pop("_thawed_from_block", None)   # re-parking without progress confirms the belief
        if int(active.get("exposures", 0)) >= EXPOSURE_CAP:
            # RELEASE: tested EXPOSURE_CAP times, never any strong progress → futile. Dead is terminal
            # (never re-picked, never re-thawed), so the loop ends instead of the thaw ping-pong
            # shoving it back into focus. This is the gate's EVIDENCE-COMPLETE call, not the model's —
            # which keeps 'letting go' out of reach of reward-hacking (no tool, no reward for it).
            active["state"] = "dead"
            active["blocked_reason"] = (f"released: {active.get('exposures')} tested retrials without "
                                        f"real progress — accepted as not mine to do")
            died = {"title": active["title"], "reason": active["blocked_reason"]}
        else:
            parked = True
            active["state"] = "blocked"
            active["blocked_reason"] = active.get("blocked_reason") or (
                f"stalled — {active['ticks_since_progress']} ticks without progress"
                if active["frustration"] >= park_at
                else f"no real (strong) progress in {strong_stall} ticks — parking to try other work")
            # leave wake_condition as set by the model if any
        nxt = _pick_next(data, exclude_id=active["id"])
        if nxt is None:
            nxt = _thaw_candidate(data, tick_number)
            if nxt:
                _cand_died = _dead_if_exposure_spent(nxt)   # spent → release dead instead of thaw
                if _cand_died:
                    if died is None:
                        died = _cand_died
                    nxt = None                              # nothing workable → escalate below
                else:
                    _thaw(nxt, tick_number)             # exposure: re-test the belief
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
            # Nothing else workable. An EMPTY backlog is valid — the creature simply has no open
            # commitment right now (goal-tension discharges to zero: that relief IS the reward for
            # letting go, never a credited event). We do NOT un-park-and-grind an impossible lone
            # goal (the old doom pump); we surface it to Charlie ONCE and let the moment be free.
            data["active_id"] = None
            escalate = _maybe_escalate(data, tick_number)

    _save(config, data)
    return {"rotated": rotated, "parked": parked, "escalate": escalate, "died": died,
            "active": _by_id(data, data["active_id"]), "refuted_block": refuted_block}
