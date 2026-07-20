"""The reflex rung of the crystallization ladder (WISDOM_PLAN §1) — trigger→action rules the
PLATFORM executes with NO model call.

Today's ladder is episode → lesson/guardrail (prose) → skill (code, model-invoked). Each rung
still costs the model a decision. The REFLEX is the rung below the model: when the adjudicated
ledger shows the SAME situation answered by the SAME action with a run of clean successes, the
platform crystallizes that into a `{situation → action}` rule it can fire itself. The mind is
freed; the joule is saved.

Doctrine this module obeys (WISDOM_PLAN §W):

  - WIS1 (adjudicated-only): promotion reads GLUE-checked outcomes — the episodic ledger, which
    is one row per acting tick recording {situation key, action signature, adjudicated success}.
    Guards reuse `quests.Criterion` — NO new predicate language.
  - WIS2 (never farms the economy): a reflex-handled outcome carries `"automated": true` and pays
    NO XP, NO portfolio evidence, NO skill-trust, NO reward learning. Enforcement lives at the
    call site (eidos.py): the reflex path does not call the economy feeds at all. This module
    additionally EXCLUDES automated episodes from the promotion streak so a reflex can never
    promote itself (mirrors pitfall-#8's `delegated` exclusion in level_gates.py).
  - WIS3 (honesty): the eidos hook renders "[REFLEX] handled X via Y" into the stream; a reflex
    whose action fails adjudication demotes immediately (disarmed + error engram).
  - WIS7 (flag-dark): every path here is inert unless the caller has already checked
    `wisdom_reflexes_enabled`. With the flag off nothing registers, nothing fires, no file is
    written — the caller must not call in that case. This module never reads the flag itself.
  - WIS8 (bounded): the registry is a bounded, atomically-written, fail-open store.

The streak book:  we DO NOT invent a parallel outcome log. `episodes.jsonl` already records every
acting tick as {tick, key, tool, sig, success, ...} — the exact tuple the promotion scanner needs
(same key + same action-signature + N consecutive adjudicated successes, zero interleaved failures
in that situation). The scanner reads that ledger directly. The only book this module OWNS is the
reflex registry itself (state/reflexes.json) and the below-model daily fraction
(state/reflex_fraction.json) for the wave-2 growth panel.
"""

from __future__ import annotations

import hashlib
import json
import time
from typing import Optional

import quests  # Criterion — the ONLY guard vocabulary (WIS1)

# --- Store shape / bounds (WIS8) ------------------------------------------------------------------
_STATE_NAME = "reflexes.json"
_FRACTION_NAME = "reflex_fraction.json"
_MAX_REFLEXES = 64          # total registry ring (proposed + armed + demoted); armed subset is
                            # separately capped by wisdom_reflex_max_armed at the caller. Whole-file
                            # rewrite, so this bounds the file too.
_MAX_FRACTION_DAYS = 30     # daily below-model fraction: a month of history, then oldest drops.

# Status values.
PROPOSED = "proposed"
ARMED = "armed"
DEMOTED = "demoted"

# The action signature is what the loop detector already computes and episodes.py already stores as
# `sig` (bash v3/v4/v5 collapse to one). A reflex's action MUST reproduce that same signature or the
# streak that promoted it and the fire it performs would be about different actions.


# =================================================================================================
# Persistence — single bounded, atomic, fail-open store (mirrors level_gates.GateState discipline)
# =================================================================================================
def _path(config):
    return config.state_dir / _STATE_NAME


def _load_raw(config) -> dict:
    """The whole registry: {"reflexes": [ {...}, ... ]}. Fail-open to empty on any error."""
    try:
        d = json.loads(_path(config).read_text(encoding="utf-8"))
        if isinstance(d, dict) and isinstance(d.get("reflexes"), list):
            return {"reflexes": [r for r in d["reflexes"] if isinstance(r, dict)]}
    except Exception:  # noqa: BLE001 - missing/corrupt => fresh registry
        pass
    return {"reflexes": []}


def _save_raw(config, data: dict) -> None:
    """Atomic tmp+replace, bounded to _MAX_REFLEXES (oldest-first drop). Best-effort — the registry
    is adjudicator bookkeeping; a failed write must never wound the tick."""
    try:
        reflexes = list(data.get("reflexes") or [])[-_MAX_REFLEXES:]
        config.state_dir.mkdir(parents=True, exist_ok=True)
        p = _path(config)
        tmp = p.with_suffix(".json.tmp")
        tmp.write_text(json.dumps({"reflexes": reflexes}, ensure_ascii=False), encoding="utf-8")
        tmp.replace(p)
    except Exception:  # noqa: BLE001 - best-effort persistence
        pass


def list_reflexes(config, *, status: Optional[str] = None) -> list[dict]:
    """All reflexes (optionally filtered by status). Copies out so callers can't mutate the store."""
    out = [dict(r) for r in _load_raw(config)["reflexes"]]
    if status is not None:
        out = [r for r in out if r.get("status") == status]
    return out


def get(config, reflex_id: str) -> Optional[dict]:
    for r in _load_raw(config)["reflexes"]:
        if r.get("id") == reflex_id:
            return dict(r)
    return None


def _reflex_id(situation_key: str, tool: str, sig: str) -> str:
    """A stable id for a (situation, action) pair — one reflex per pair, so re-scanning the same
    winning streak UPDATES the existing reflex instead of spawning duplicates."""
    h = hashlib.md5(f"{situation_key}\x00{tool}\x00{sig}".encode("utf-8", "ignore")).hexdigest()
    return f"rfx_{h[:12]}"


# =================================================================================================
# Guard evaluation (WIS1 — quests.Criterion vocabulary ONLY, no new predicate language)
# =================================================================================================
def _guard_ok(guard, stats: dict) -> bool:
    """A reflex fires only when its guard is TRUE over the typed stats. An empty/absent guard is a
    permissive guard (situation-key match alone) — but a MALFORMED guard can never pass (Criterion
    contract). stats is the same typed dict quest criteria are checked against."""
    if not guard:
        return True
    try:
        return quests.Criterion.from_dict(guard).check(stats or {})
    except Exception:  # noqa: BLE001 - a broken guard never fires (fail-closed on the guard)
        return False


def match(config, situation_key: str, stats: dict) -> Optional[dict]:
    """The one armed reflex whose situation-key matches the current situation AND whose guard is
    true over `stats`. Returns None when nothing matches (fail-open). At most one is returned — the
    caller fires exactly one reflex per tick (the §1 cap). Ties break to the highest success count
    (the most-earned rule wins)."""
    if not situation_key:
        return None
    candidates = [r for r in _load_raw(config)["reflexes"]
                  if r.get("status") == ARMED
                  and r.get("trigger", {}).get("situation_key") == situation_key
                  and _guard_ok(r.get("trigger", {}).get("guard"), stats)]
    if not candidates:
        return None
    candidates.sort(key=lambda r: int((r.get("provenance") or {}).get("successes", 0)),
                    reverse=True)
    return dict(candidates[0])


# =================================================================================================
# Promotion scanner — reads the adjudicated episodic ledger (WIS1), NOT a parallel log
# =================================================================================================
def _clean_streaks(episodes: list[dict]) -> dict[tuple, dict]:
    """For each (situation key, tool, action-signature), the length of the CURRENT trailing run of
    consecutive adjudicated successes with ZERO interleaved failures IN THAT SITUATION.

    'Interleaved' is judged per situation-key: a FAILURE under situation S (any action) breaks every
    streak keyed on S — a situation that started failing again is no longer solved, whatever the
    action. Automated episodes (reflex-handled, `automated: true`) are SKIPPED entirely (WIS2): a
    reflex must never count its own firings toward its own re-promotion.
    """
    # streaks[(key, tool, sig)] = {"successes": n, "last_tick": t}
    streaks: dict[tuple, dict] = {}
    for ep in episodes:
        if ep.get("automated"):
            continue  # WIS2: crystallized firings don't feed crystallization
        key = ep.get("key") or ""
        if not key:
            continue
        tool = ep.get("tool") or ""
        sig = str(ep.get("sig") or tool)
        success = bool(ep.get("success"))
        if not success:
            # A failure under this situation resets every action-streak keyed on this situation.
            for k in list(streaks):
                if k[0] == key:
                    streaks.pop(k, None)
            continue
        ident = (key, tool, sig)
        cur = streaks.get(ident) or {"successes": 0, "last_tick": 0}
        cur["successes"] = int(cur["successes"]) + 1
        cur["last_tick"] = int(ep.get("tick", cur["last_tick"]) or cur["last_tick"])
        streaks[ident] = cur
    return streaks


def scan_promotions(config, *, promote_at: int, auto_arm: bool = False,
                    episodes: Optional[list[dict]] = None) -> list[str]:
    """Scan the episodic ledger; PROPOSE a reflex for every (situation, action) whose current clean
    success streak has reached `promote_at`. Returns the ids of reflexes newly created or bumped to
    proposed. Idempotent per streak: re-scanning the same run updates the existing reflex's
    provenance, never spawns a duplicate. A reflex currently DEMOTED is re-proposed only from a
    FRESH run (its recorded `last_adjudicated` marks the last tick it acted; a streak that predates
    that has already been spent).

    `auto_arm` (wisdom_reflex_auto_arm) arms on proposal — the trust ladder's future default, off
    today. The armed-count cap is enforced here so auto-arm can never exceed the bound.
    """
    if episodes is None:
        try:
            import episodes as _ep
            episodes = _ep._read(config)
        except Exception:  # noqa: BLE001 - no ledger => nothing to promote
            episodes = []
    streaks = _clean_streaks(episodes or [])
    data = _load_raw(config)
    by_id = {r.get("id"): r for r in data["reflexes"]}
    changed: list[str] = []
    for (key, tool, sig), st in streaks.items():
        if int(st["successes"]) < int(promote_at):
            continue
        if not tool:
            continue
        rid = _reflex_id(key, tool, sig)
        existing = by_id.get(rid)
        if existing is not None:
            status = existing.get("status")
            if status == ARMED:
                continue  # already earned and armed — nothing to do
            if status == DEMOTED:
                # Re-propose only from a run STRICTLY newer than the demotion (fresh successes).
                last_adj = int((existing.get("provenance") or {}).get("last_adjudicated", 0) or 0)
                if int(st["last_tick"]) <= last_adj:
                    continue
            # Refresh the streak evidence and (re)propose.
            existing["status"] = PROPOSED
            existing.setdefault("provenance", {})
            existing["provenance"]["successes"] = int(st["successes"])
            existing["provenance"]["last_adjudicated"] = int(st["last_tick"])
            existing["failed_count"] = 0
            changed.append(rid)
            continue
        reflex = {
            "id": rid,
            "trigger": {"situation_key": key, "guard": {}},   # guard defaults permissive; the
                                                              # situation key IS the primary trigger
            "action": {"tool": tool, "args": {}, "sig": sig},
            "provenance": {"successes": int(st["successes"]),
                           "last_adjudicated": int(st["last_tick"]),
                           "episode_ids": []},
            "status": PROPOSED,
            "fired_count": 0,
            "failed_count": 0,
            "created_ts": time.time(),
        }
        data["reflexes"].append(reflex)
        by_id[rid] = reflex
        changed.append(rid)
    if auto_arm:
        # Arm the freshly-proposed reflexes up to the bound (the authoritative cap; a save happens
        # once below). arm() re-checks independently for the operator-gated path.
        max_armed = int(getattr(config, "wisdom_reflex_max_armed", 12) or 12)
        armed_now = sum(1 for r in data["reflexes"] if r.get("status") == ARMED)
        for rid in changed:
            if armed_now >= max_armed:
                break
            r = by_id.get(rid)
            if r is not None and r.get("status") == PROPOSED:
                r["status"] = ARMED
                armed_now += 1
    if changed:
        _save_raw(config, data)
    return changed


# =================================================================================================
# Lifecycle — propose / arm / demote (operator-facing arm; auto_arm is the flagged shortcut)
# =================================================================================================
def propose(config, *, situation_key: str, tool: str, sig: str = "", guard: Optional[dict] = None,
            successes: int = 0, last_adjudicated: int = 0,
            episode_ids: Optional[list] = None) -> str:
    """Hand-register a PROPOSED reflex (used by tests and the lineage importer; the live path is
    scan_promotions). Returns the reflex id. Re-proposing an existing pair refreshes it."""
    rid = _reflex_id(situation_key, tool, str(sig or tool))
    data = _load_raw(config)
    for r in data["reflexes"]:
        if r.get("id") == rid:
            r["status"] = PROPOSED
            r.setdefault("provenance", {})
            r["provenance"]["successes"] = int(successes)
            r["provenance"]["last_adjudicated"] = int(last_adjudicated)
            if guard is not None:
                r["trigger"]["guard"] = guard
            _save_raw(config, data)
            return rid
    data["reflexes"].append({
        "id": rid,
        "trigger": {"situation_key": situation_key, "guard": guard or {}},
        "action": {"tool": tool, "args": {}, "sig": str(sig or tool)},
        "provenance": {"successes": int(successes),
                       "last_adjudicated": int(last_adjudicated),
                       "episode_ids": list(episode_ids or [])},
        "status": PROPOSED,
        "fired_count": 0,
        "failed_count": 0,
        "created_ts": time.time(),
    })
    _save_raw(config, data)
    return rid


def arm(config, reflex_id: str) -> dict:
    """Operator-gated arming (the propose/apply doctrine). Returns a typed result dict; NEVER a
    success-lie (ARCH #4) — a cap hit or missing id is a visible typed failure carrying the rule.
    Enforces wisdom_reflex_max_armed (WIS8)."""
    data = _load_raw(config)
    target = None
    for r in data["reflexes"]:
        if r.get("id") == reflex_id:
            target = r
            break
    if target is None:
        return {"ok": False, "reason": "no_such_reflex", "id": reflex_id}
    if target.get("status") == ARMED:
        return {"ok": True, "reason": "already_armed", "id": reflex_id}
    if target.get("status") == DEMOTED:
        # A demoted reflex must EARN re-proposal from fresh successes before it can arm.
        return {"ok": False, "reason": "demoted_needs_reproposal", "id": reflex_id}
    max_armed = int(getattr(config, "wisdom_reflex_max_armed", 12) or 12)
    armed_now = sum(1 for r in data["reflexes"] if r.get("status") == ARMED)
    if armed_now >= max_armed:
        return {"ok": False, "reason": "max_armed", "id": reflex_id,
                "armed": armed_now, "cap": max_armed}
    target["status"] = ARMED
    _save_raw(config, data)
    return {"ok": True, "reason": "armed", "id": reflex_id}


def demote(config, reflex_id: str, *, tick: int = 0, reason: str = "") -> dict:
    """Demote + DISARM a reflex (WIS3): one adjudicated failure retires it. Records the failing tick
    as `last_adjudicated` so re-proposal requires a run strictly newer than this. Returns a typed
    result. Committing the error engram is the caller's job (it owns the memory hub); this only
    moves the registry state."""
    data = _load_raw(config)
    for r in data["reflexes"]:
        if r.get("id") == reflex_id:
            r["status"] = DEMOTED
            r["failed_count"] = int(r.get("failed_count", 0)) + 1
            r["consecutive_fires"] = 0
            r.setdefault("provenance", {})
            if tick:
                r["provenance"]["last_adjudicated"] = int(tick)
            r["demoted_reason"] = reason or "adjudication_failure"
            _save_raw(config, data)
            return {"ok": True, "id": reflex_id, "status": DEMOTED}
    return {"ok": False, "reason": "no_such_reflex", "id": reflex_id}


def record_fire(config, reflex_id: str, *, success: bool, tick: int = 0) -> dict:
    """Book a reflex firing on the registry: bump fired_count and last_adjudicated. On a FAILURE the
    caller should also call demote() (WIS3) — this function only keeps the counters honest so the
    per-tick loop bound (below) can see them. Returns the updated fired_count and the consecutive
    same-situation fire count for the loop bound."""
    data = _load_raw(config)
    for r in data["reflexes"]:
        if r.get("id") == reflex_id:
            r["fired_count"] = int(r.get("fired_count", 0)) + 1
            r.setdefault("provenance", {})
            if tick:
                r["provenance"]["last_adjudicated"] = int(tick)
            if success:
                r["consecutive_fires"] = int(r.get("consecutive_fires", 0)) + 1
            else:
                r["consecutive_fires"] = 0
            _save_raw(config, data)
            return {"ok": True, "id": reflex_id,
                    "fired_count": r["fired_count"],
                    "consecutive_fires": int(r.get("consecutive_fires", 0))}
    return {"ok": False, "reason": "no_such_reflex", "id": reflex_id}


def consecutive_fires(config, reflex_id: str) -> int:
    """How many times this reflex has fired in a row without an intervening non-fire tick — the
    rabbit-hole counter the caller checks against wisdom_reflex_loop_bound. Reset by the caller
    (reset_consecutive) whenever a different reflex fires or no reflex fires."""
    r = get(config, reflex_id)
    return int((r or {}).get("consecutive_fires", 0)) if r else 0


def reset_consecutive(config, *, except_id: Optional[str] = None) -> None:
    """Zero the consecutive-fire counter on every reflex except `except_id`. The caller invokes this
    each tick BEFORE firing so only an uninterrupted run of the SAME reflex accumulates — mirroring
    the loop detector's 'same action repeating' spirit for the below-model path."""
    data = _load_raw(config)
    dirty = False
    for r in data["reflexes"]:
        if r.get("id") != except_id and int(r.get("consecutive_fires", 0)) != 0:
            r["consecutive_fires"] = 0
            dirty = True
    if dirty:
        _save_raw(config, data)


# =================================================================================================
# Automated-episode recording — file the reflex's outcome in the SAME episodic ledger the model's
# ticks use, marked `automated: true` (WIS2). The scanner (_clean_streaks) SKIPS automated rows, so
# a reflex can never count its own firings toward its own re-promotion. We write the row here rather
# than through episodes.record_episode because that function's fixed schema has no automated flag —
# same schema (tick, key, tool, sig, fail_kind, success, summary, ts), plus `automated`.
# =================================================================================================
def record_automated_episode(config, *, tick: int, situation_key: str, tool: str, sig: str,
                             fail_kind: str, success: bool, summary: str = "") -> None:
    """Append one reflex-handled tick to episodes.jsonl marked automated. Best-effort (episodic
    recording never raises into the loop); reuses episodes.py's own path/clean/trim so the row is
    byte-shaped exactly like a model tick's, plus the exclusion flag."""
    if not tool:
        return
    try:
        import episodes as _ep
        row = {"tick": int(tick), "key": situation_key or "", "tool": tool,
               "sig": str(sig or tool), "fail_kind": fail_kind or "", "success": bool(success),
               "summary": _ep.clean_fragment(summary, _ep.SUMMARY_CHARS),
               "ts": time.time(), "automated": True}
        config.workspace.mkdir(parents=True, exist_ok=True)
        with open(_ep._path(config), "a", encoding="utf-8") as f:
            f.write(json.dumps(row) + "\n")
        _ep._trim(config)
    except Exception:  # noqa: BLE001 - episodic recording is best-effort
        pass


# =================================================================================================
# Below-the-model fraction (§1 metric) — persisted daily for the wave-2 growth panel (WIS8)
# =================================================================================================
def _fraction_path(config):
    return config.state_dir / _FRACTION_NAME


def record_tick_outcome(config, *, handled_by_reflex: bool, day: Optional[str] = None) -> None:
    """Tally one tick into today's below-model fraction: reflex-handled / total. Bounded ring of
    _MAX_FRACTION_DAYS days. Best-effort; a bookkeeping failure never wounds the tick."""
    try:
        day = day or time.strftime("%Y-%m-%d")
        try:
            d = json.loads(_fraction_path(config).read_text(encoding="utf-8"))
            if not isinstance(d, dict):
                d = {}
        except Exception:  # noqa: BLE001
            d = {}
        days = d.get("days") if isinstance(d.get("days"), dict) else {}
        row = days.get(day) if isinstance(days.get(day), dict) else {"reflex": 0, "total": 0}
        row["total"] = int(row.get("total", 0)) + 1
        if handled_by_reflex:
            row["reflex"] = int(row.get("reflex", 0)) + 1
        days[day] = row
        # Bound to the most recent _MAX_FRACTION_DAYS days (sorted by date string).
        if len(days) > _MAX_FRACTION_DAYS:
            for old in sorted(days)[:-_MAX_FRACTION_DAYS]:
                days.pop(old, None)
        config.state_dir.mkdir(parents=True, exist_ok=True)
        p = _fraction_path(config)
        tmp = p.with_suffix(".json.tmp")
        tmp.write_text(json.dumps({"days": days}, ensure_ascii=False), encoding="utf-8")
        tmp.replace(p)
    except Exception:  # noqa: BLE001 - metric bookkeeping is best-effort
        pass


def fraction_today(config, *, day: Optional[str] = None) -> float:
    """Today's below-model fraction (reflex / total), 0.0 when no ticks recorded. Read for the
    growth panel; fail-open to 0.0."""
    try:
        day = day or time.strftime("%Y-%m-%d")
        d = json.loads(_fraction_path(config).read_text(encoding="utf-8"))
        row = (d.get("days") or {}).get(day) or {}
        total = int(row.get("total", 0))
        return (int(row.get("reflex", 0)) / total) if total else 0.0
    except Exception:  # noqa: BLE001
        return 0.0
