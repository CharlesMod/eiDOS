"""Pillars 4.3 — mastery gates: levels are evidence, XP is just the bar (PILLARS_PLAN §6).

The old formula was a volume clock — `level = f(xp)` meant the creature leveled by existing.
Under the gates, XP remains the within-level progress bar (necessary), but CROSSING requires
glue-adjudicated evidence (sufficient): trusted skills in the level's capability tier
(automatization before advancement), calibration, a reuse ratio in band, mandatory sleep cycles
since the last level (the spacing effect as a hard floor — raising a child, not shipping a
sprint), and a closed quest line. Every check is deterministic code over typed state; the model
has no say in whether it passed (§0.5).

Two pitfall dampers live here:
  - #8 (leveling on the army's back): every evidence counter skips outcomes marked delegated —
    the fields arrive with Phases 6/7; the exclusion predicate is defined NOW so they cannot
    count by omission. Levels are personal.
  - #3 (depression spiral): the temperament setpoint springs are the companion change in
    nervous/temperament.py — sustained failure spikes caution, the spring relaxes it back toward
    the genome baseline instead of letting it ratchet.

Suspension, not de-leveling: sustained tier failure re-locks the tier pending a remedial quest
(proposed through the 5.1 System's `propose()` seam). Standing is recoverable; the scar stays
(an episode-shaped engram records the suspension).

LIVE behind `pillars_mastery_gates_enabled` (Phase 5.5 cutover DONE, not dark): the tick loop wires
`record_sleep_cycle` to the sleep boundary, `record_tier_outcome`/candidacy to glue's adjudication,
and the level-up path to `apply_level_up` (the only level mover under the flag). A sustained tier
failure suspends + proposes a remedial quest; standing is recoverable.
"""
from __future__ import annotations

import json
import logging
import uuid
from typing import Any, Optional

logger = logging.getLogger("eidos.level_gates")

# --- Declared knobs (§0.4: a constant is declared or derived, never a silent guess) -------------

LEVELS_PER_TIER = 2          # declared: a new capability tier every 2 levels — early tiers arrive
                             # fast (pedagogy), later ones slow with the sqrt XP curve.
TRUSTED_PER_TIER = 2         # declared: evidence floor — two independently-trusted skills in the
                             # tier proves the tier's competence wasn't one lucky script.
BRIER_MAX = 0.20             # declared: calibration ceiling (0.25 = coin-flip error); a creature
                             # must beat chance about its own predictions to advance.
REUSE_RATIO_MIN = 2.0        # declared: each authored live skill must average ≥2 uses — reuse as
                             # the resting state (D5) before advancement, not a library of one-offs.
REUSE_RATIO_MAX = 500.0      # declared: an upper rail only to flag a degenerate monoculture (one
                             # skill ground thousands of times); generous by design.
SUSPEND_AFTER_FAILURES = 5   # declared: consecutive non-delegated tier failures before the tier
                             # re-locks — sustained, not a bad afternoon.
STATE_NAME = "level_gates.json"


def tier_of_level(level: int) -> int:
    """The capability tier a given level sits in (declared LEVELS_PER_TIER curve)."""
    return 1 + max(0, int(level) - 1) // LEVELS_PER_TIER


def xp_for_level(level: int) -> int:
    """Inverse of persona.compute_level: the XP floor at which `level` becomes reachable.
    compute_level = 1 + floor(sqrt(xp/50))  =>  xp_for_level(L) = 50·(L−1)²."""
    return 50 * max(0, int(level) - 1) ** 2


def _counts(outcome: dict) -> bool:
    """Pitfall #8 exclusion predicate: an outcome marked delegated NEVER feeds a gate counter.
    Absent mark = personal (the fields ship with Phases 6/7; the predicate ships now)."""
    return not bool(outcome.get("delegated", False))


# --- Sidecar state (suspensions, tier-failure streaks, sleep counter) ---------------------------

class GateState:
    """The gates' own bounded books, persisted to state_dir/level_gates.json (atomic tmp+replace,
    fail-open like temperament.json — gate state is adjudicator bookkeeping, never load-bearing
    for the tick)."""

    def __init__(self, config):
        self.config = config
        self.suspended: dict[str, dict] = {}      # tier(str) -> {since, failures, remedial_id}
        self.failures: dict[str, int] = {}        # tier(str) -> consecutive non-delegated failures
        self.sleeps_since_level: int = 0
        self.sleeps_total: int = 0                # MONOTONIC completed-sleep count — the honest
                                                  # lifetime fact quest glue adjudicates
                                                  # (`sleeps.total`); level-ups reset since_level,
                                                  # never this. Single writer: record_sleep_cycle.
        self.load()

    def _path(self):
        return self.config.state_dir / STATE_NAME

    def load(self) -> None:
        try:
            d = json.loads(self._path().read_text(encoding="utf-8"))
            self.suspended = dict(d.get("suspended", {}))
            self.failures = {k: int(v) for k, v in dict(d.get("failures", {})).items()}
            self.sleeps_since_level = int(d.get("sleeps_since_level", 0))
            # Migration floor: books written before the total existed have slept AT LEAST
            # since_level times (fail-open re-seed from evidence, never to empty).
            self.sleeps_total = int(d.get("sleeps_total", self.sleeps_since_level))
        except Exception:  # noqa: BLE001 - missing/corrupt file => fresh books
            pass

    def save(self) -> None:
        try:
            self.config.state_dir.mkdir(parents=True, exist_ok=True)
            p = self._path()
            tmp = p.with_suffix(".json.tmp")
            tmp.write_text(json.dumps({
                "suspended": self.suspended,
                "failures": self.failures,
                "sleeps_since_level": self.sleeps_since_level,
                "sleeps_total": self.sleeps_total,
            }, ensure_ascii=False), encoding="utf-8")
            tmp.replace(p)
        except Exception:  # noqa: BLE001 - best-effort persistence
            pass


def _enabled(config) -> bool:
    return bool(getattr(config, "pillars_mastery_gates_enabled", False))


# --- Evidence collection (each source read mechanically; a missing source can only block or
#     pass-by-absence per its documented rule, never guess) --------------------------------------

def _trusted_in_tier(config, tier: int) -> int:
    """Trusted, non-delegated skills whose tier matches. A skill with no tier mark is tier 1
    (the manifest predates tiers; stamping arrives with the cutover)."""
    try:
        import skills as _skills
        manifest = _skills._load_manifest(config)
    except Exception:  # noqa: BLE001 - no manifest => no evidence
        return 0
    n = 0
    for ent in (manifest.get("skills") or {}).values():
        if ent.get("status") != "trusted":
            continue
        if not _counts(ent):
            continue
        if int(ent.get("tier", 1) or 1) == tier:
            n += 1
    return n


def _reuse_ratio(config) -> Optional[float]:
    """Total invocations across live skills / count of live skills. None = no live skills yet
    (pass-by-absence: with zero skills the trusted-count check is already the blocker; a ratio
    over an empty library proves nothing either way)."""
    try:
        import skills as _skills
        manifest = _skills._load_manifest(config)
    except Exception:  # noqa: BLE001
        return None
    live = [e for e in (manifest.get("skills") or {}).values()
            if e.get("status") in ("active", "trusted") and _counts(e)]
    if not live:
        return None
    total_inv = sum(int(e.get("invocations", 0) or 0) for e in live)
    return total_inv / len(live)


def _calibration_brier(config) -> Optional[float]:
    """Prediction-count-weighted mean Brier over all domains. None = no closed predictions yet
    (pass-by-absence: a newborn has no calibration history; the sleeps + trusted-skills floors
    carry the early gates, and the Administrator's calibration drills create the history)."""
    try:
        import expectations as _exp
        by_domain = _exp.brier_calibration_by_domain(config)
    except Exception:  # noqa: BLE001
        return None
    if not by_domain:
        return None
    total_n = sum(int(d.get("n", 0)) for d in by_domain.values())
    if total_n <= 0:
        return None
    return sum(float(d.get("brier", 0.0)) * int(d.get("n", 0)) for d in by_domain.values()) / total_n


def _quest_line_closed(config) -> bool:
    """True when no quest is currently ACTIVE (the line is closed; the System may be holding the
    next one back on cadence — that still counts as closed here)."""
    try:
        import quests as _quests
        return _quests.QuestStore(config).active() is None
    except Exception:  # noqa: BLE001 - no quest store yet => nothing open
        return True


# --- The gate ------------------------------------------------------------------------------------

def can_level(persona: dict, config) -> tuple[bool, dict]:
    """Glue-adjudicated level-up check. Returns (ok, evidence_report) — the report carries every
    check's value and verdict so the dashboard (and the Administrator's dossier) can show WHY,
    not just whether. All checks must pass; XP is necessary but never sufficient."""
    if not _enabled(config):
        return False, {"enabled": False}

    state = GateState(config)
    cur = int(persona.get("level", 1))
    nxt = cur + 1
    tier = tier_of_level(nxt)
    checks: dict[str, dict] = {}

    xp = int(persona.get("xp", 0))
    checks["xp_floor"] = {"value": xp, "need": xp_for_level(nxt), "ok": xp >= xp_for_level(nxt)}

    trusted = _trusted_in_tier(config, tier)
    checks["trusted_in_tier"] = {"value": trusted, "need": TRUSTED_PER_TIER, "tier": tier,
                                 "ok": trusted >= TRUSTED_PER_TIER}

    brier = _calibration_brier(config)
    checks["calibration"] = {"value": brier, "max": BRIER_MAX,
                             "ok": True if brier is None else brier <= BRIER_MAX,
                             "note": "no closed predictions — pass-by-absence" if brier is None else ""}

    ratio = _reuse_ratio(config)
    checks["reuse_ratio"] = {"value": ratio, "band": [REUSE_RATIO_MIN, REUSE_RATIO_MAX],
                             "ok": True if ratio is None else REUSE_RATIO_MIN <= ratio <= REUSE_RATIO_MAX,
                             "note": "no live skills — pass-by-absence" if ratio is None else ""}

    need_sleeps = int(getattr(config, "pillars_min_sleeps_per_level", 3))
    checks["sleep_cycles"] = {"value": state.sleeps_since_level, "need": need_sleeps,
                              "ok": state.sleeps_since_level >= need_sleeps}

    checks["quest_line_closed"] = {"ok": _quest_line_closed(config)}

    checks["no_suspensions"] = {"suspended": sorted(state.suspended.keys()),
                                "ok": not state.suspended}

    ok = all(c["ok"] for c in checks.values())
    return ok, {"enabled": True, "level": cur, "next": nxt, "tier": tier, "ok": ok,
                "checks": checks}


def render_standing(persona: dict, config) -> str:
    """ONE terse line of growth proprioception for the System window: level, XP, and what stands
    between the creature and the next level — unmet gate NAMES, or the suspension. Rendered from
    the same can_level evidence the dashboard reads (glue facts, never self-report). A creature
    that cannot feel its own growth cannot pursue it: before this line existed, level/XP/gates
    were entirely invisible in-context, and the only coupling (quests) had been broken bricks.
    Returns '' when the gates are off."""
    if not _enabled(config):
        return ""
    try:
        ok, report = can_level(persona, config)
    except Exception:  # noqa: BLE001 - proprioception is best-effort; the tick must not care
        return ""
    lv = int(persona.get("level", 1))
    xp = int(persona.get("xp", 0))
    state = GateState(config)
    if state.suspended:
        tiers = ", ".join(sorted(state.suspended.keys()))
        return (f"LV.{lv} · XP {xp} · ADVANCEMENT SUSPENDED (tier {tiers}) — "
                f"clear the remedial quest to restore it")
    if ok:
        return f"LV.{lv} · XP {xp} · all gates open — advancement imminent"
    unmet = [name for name, c in (report.get("checks") or {}).items() if not c.get("ok")]
    return f"LV.{lv} · XP {xp} · gates unmet: {', '.join(unmet) if unmet else '—'}"


def apply_level_up(persona: dict, config) -> dict:
    """Cross the gate: verifies can_level, advances the level by EXACTLY one, resets the sleep
    counter. Returns the evidence report (with `applied`). The only path that moves the level
    while the gates are on — persona.award_xp holds the level still under the flag."""
    ok, report = can_level(persona, config)
    report["applied"] = bool(ok)
    if not ok:
        return report
    persona["level"] = int(persona.get("level", 1)) + 1
    state = GateState(config)
    state.sleeps_since_level = 0
    state.save()
    return report


# --- Tier outcomes → suspension / remedial (recoverable standing, permanent scar) ----------------

def record_tier_outcome(config, tier: int, success: bool, *, delegated: bool = False) -> Optional[str]:
    """Feed one adjudicated outcome in a tier. Delegated outcomes are dropped whole (pitfall #8).
    SUSPEND_AFTER_FAILURES consecutive failures re-locks the tier and proposes a remedial quest
    through the System's seam; returns the remedial quest id when a suspension fires, else None."""
    if not _enabled(config) or not _counts({"delegated": delegated}):
        return None
    state = GateState(config)
    key = str(int(tier))
    if success:
        state.failures[key] = 0
        state.save()
        return None
    state.failures[key] = state.failures.get(key, 0) + 1
    if state.failures[key] < SUSPEND_AFTER_FAILURES or key in state.suspended:
        state.save()
        return None

    # Sustained failure: suspend the tier, propose the remedial, scar the episode.
    remedial_id = f"remedial-tier{key}-{uuid.uuid4().hex[:8]}"
    state.suspended[key] = {"failures": state.failures[key], "remedial_id": remedial_id}
    state.save()
    try:
        import quests as _quests
        # The remedial's criteria MUST come from the same glue-checkable vocabulary every quest
        # uses (quests.ADJUDICATABLE_PATHS) — the original `remedial.tier_N_passed` path had NO
        # writer anywhere, so the remedial could never pass and the suspension was permanent
        # (exactly the brick quests.py warns about). And it must be achievable WHILE the remedial
        # itself holds the one-active-quest slot (a `quests.passed` target would deadlock).
        # "Demonstrate the failed competence on a fresh problem" made mechanical: forge one more
        # skill and carry it all the way to TRUSTED — the tier's own competence currency, earned
        # through the skill economy's adjudicated record, checkable any tick.
        try:
            import skills as _skills
            ents = (_skills._load_manifest(config).get("skills") or {}).values()
            trusted_now = sum(1 for e in ents if e.get("status") == "trusted")
        except Exception:  # noqa: BLE001 - no manifest reads as zero; the target stays honest
            trusted_now = 0
        _quests.System(config).propose(_quests.Quest(
            id=remedial_id,
            directive=(f"Re-earn tier {key}: forge a NEW skill and prove it to trusted "
                       f"through real use."),
            success_criteria=_quests.Criterion(path="skills.trusted_count", op=">=",
                                               value=int(trusted_now) + 1),
            tier=int(tier), kind="quest",
        ))
    except Exception as e:  # noqa: BLE001 - the seam is best-effort; suspension holds regardless
        logger.warning("remedial quest proposal failed for tier %s: %s", key, e)
    try:
        from engram import Engram, Consolidator
        Consolidator(config).commit(Engram(
            kind="error",
            body=f"tier {key} suspended after {state.failures[key]} consecutive failures; "
                 f"remedial `{remedial_id}` pending",
            provenance="experienced"))
    except Exception:  # noqa: BLE001 - the scar is best-effort; standing is the load-bearing part
        pass
    return remedial_id


def record_remedial_completion(config, tier: int) -> bool:
    """A passed remedial quest restores the tier (standing recoverable; the episode scar stays).
    Returns True when a suspension was lifted."""
    if not _enabled(config):
        return False
    state = GateState(config)
    key = str(int(tier))
    if key not in state.suspended:
        return False
    del state.suspended[key]
    state.failures[key] = 0
    state.save()
    return True


def record_sleep_cycle(config) -> None:
    """Advance the sleep counters — the mandatory-digestion counter AND the monotonic lifetime
    total (the `sleeps.total` fact quest glue adjudicates). One writer, one boundary: the cutover
    calls this at each COMPLETED sleep."""
    if not _enabled(config):
        return
    state = GateState(config)
    state.sleeps_since_level += 1
    state.sleeps_total += 1
    state.save()
