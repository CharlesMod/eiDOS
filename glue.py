"""Behavioral glue computed OUTSIDE the model (BIBLE §2.3, §5, brain-map Insula/DMN/ACC).

The doctrine's central rule: behavior comes from deterministic glue signals, never from prose
pleas the model can ignore ("you seem stuck — try harder"). This module turns the per-tick typed
outcomes (phase-1 fail_kind) into two glue signals with TEETH:

  - STRAIN (Insula): chronic-failure accumulation. Repeated failures — especially the SAME failure
    signature (ACC: "this exact thing failed again") — raise strain; progress relieves it. Strain
    is fed into the objectives gate as extra frustration, so a repeated dead end mechanically
    accelerates the auto-park/rotate. The model does not get to keep grinding; the harness moves it.

  - CONDITION (DMN): a discrete label — STABLE / FOCUSED / STRAINED / RECOVERY — computed from the
    recent success/failure window. Injected into context in place of the XP-only persona mood, which
    was decorative. This is a behaviorally load-bearing label, not seasoning.

Outcomes persist to workspace/state/outcomes.jsonl (bounded) so the signals survive a restart.
"""

from __future__ import annotations

import json
from collections import deque

# Strain accounting (units are abstract "strain points").
STRAIN_FAIL = 2          # a failed tick
STRAIN_REPEAT = 2        # extra when this tick's failure SIGNATURE matches the previous failure
STRAIN_RELIEF = 3        # a tick that made real progress
STRAIN_CAP = 12
STRAIN_HIGH = 6          # at/above this, the condition is STRAINED and the gate gets the bump

# Rumination accounting (ACC/DMN: thinking-about instead of doing). A thought tick is a valid
# reflection beat, but a WINDOW dominated by them is analysis-paralysis — the observed #1
# time-sink once the syntax-spiral class was fixed. Windowed (not strictly consecutive) so
# thought-thought-note-thought patterns don't reset the counter by sneaking in bookkeeping.
RUMINATE_WINDOW = 6      # look at this many recent outcomes
RUMINATE_K = 4           # this many thought ticks within the window = ruminating
_THOUGHT_TOOLS = ("thought",)   # tools that are reflection, not action

_WINDOW = 12             # how many recent outcomes inform the signals
_PERSIST = 40            # how many to keep on disk


def _path(config):
    return config.state_dir / "outcomes.jsonl"


def record_outcome(config, *, success: bool, fail_kind: str = "", signature: str = "",
                   tool: str = "") -> None:
    """Append one tick's outcome. Best-effort; never raises into the loop."""
    try:
        config.state_dir.mkdir(parents=True, exist_ok=True)
        rows = _read(config)
        rows.append({"ok": bool(success), "kind": fail_kind or "", "sig": signature or "",
                     "tool": tool or ""})
        rows = rows[-_PERSIST:]
        tmp = _path(config).with_suffix(".tmp")
        tmp.write_text("\n".join(json.dumps(r) for r in rows), encoding="utf-8")
        tmp.replace(_path(config))
    except Exception:  # noqa: BLE001 - glue is best-effort
        pass


def _read(config) -> list[dict]:
    try:
        txt = _path(config).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    out = []
    for line in txt.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except Exception:  # noqa: BLE001
            continue
    return out


def recent_outcomes(config, n: int = _WINDOW) -> list[dict]:
    return _read(config)[-n:]


def compute_strain(outcomes: list[dict]) -> int:
    """Strain over the window: failures accumulate (repeats hit harder), progress relieves.
    Pure function of the outcome list — deterministic and replayable."""
    strain = 0
    prev_fail_sig = None
    for o in outcomes:
        if o.get("tool") in _THOUGHT_TOOLS:
            # A thought is neutral: it neither fails nor makes progress, so it must not
            # relieve strain (it was logged ok=True, which used to bleed off -3/thought —
            # letting the model think its way out of STRAINED without fixing anything).
            continue
        if o.get("ok"):
            strain = max(0, strain - STRAIN_RELIEF)
            prev_fail_sig = None
        else:
            add = STRAIN_FAIL
            sig = o.get("sig") or ""
            if sig and sig == prev_fail_sig:
                add += STRAIN_REPEAT      # ACC: the SAME thing failed again
            strain = min(STRAIN_CAP, strain + add)
            prev_fail_sig = sig
    return strain


def repeated_failure_signature(outcomes: list[dict], k: int = 3) -> str:
    """The signature that just failed k+ times IN A ROW (ACC teeth), or '' if none.
    The trailing run only — an old streak that since recovered doesn't count."""
    run_sig, run = "", 0
    for o in reversed(outcomes):
        if o.get("ok"):
            break
        sig = o.get("sig") or ""
        if run == 0:
            run_sig, run = sig, 1
        elif sig == run_sig and sig:
            run += 1
        else:
            break
    return run_sig if (run >= k and run_sig) else ""


def escalation_hint(outcomes: list[dict], k: int = 3) -> str:
    """Non-empty when the SAME failure signature has run k+ times in a row (ACC teeth).
    The gate already rotates focus mechanically; this tells the model the RIGHT pivot —
    change method entirely or hand the problem to the delegate — never another retry.
    (Doctrine note: the teeth stay mechanical; this only steers the pivot the gate forces.)"""
    if not repeated_failure_signature(outcomes, k):
        return ""
    return ("⚠ The exact same action has failed repeatedly in a row. Do NOT run it again. "
            "Either change METHOD entirely, or hand the whole problem to your coding agent: "
            'delegate {"task":"<the goal + everything you tried + the exact error>", '
            '"mode":"code"} — it investigates and fixes multi-step problems in the '
            "background and reports back.")


def rumination_streak(outcomes: list[dict], window: int = RUMINATE_WINDOW) -> int:
    """How many of the last `window` outcomes were thought-only ticks — but only while the
    model is STILL in its head (last outcome is a thought). One real action clears the nag
    instantly; we don't keep scolding after it has started doing things again."""
    recent = outcomes[-window:]
    if not recent or recent[-1].get("tool") not in _THOUGHT_TOOLS:
        return 0
    return sum(1 for o in recent if o.get("tool") in _THOUGHT_TOOLS)


MOTIF_WINDOW = 10       # recent reflection bodies (thoughts + notes) the motif brake scans
MOTIF_DOMINANCE = 0.6   # fraction of them sharing the single most-common content-token PAIR that reads
#                         as "circling ONE theme" — the morose burrower loop measured ~0.8; healthy
#                         exploration <0.3. This is content-aware where rumination_streak is not: the
#                         creature journals its loop THROUGH note_append (thought-only counting misses
#                         it), and token_jaccard is blind to elaborated rephrasing (0.10–0.26) while a
#                         dominant token-pair is not.
MOTIF_BUMP = 1          # small: only nudges rotation, applied on no-progress ticks only, so it can
#                         never combine with the objective exposure-cap to kill a WORKING goal.


def motif_dominance(bodies: list) -> float:
    """Fraction of recent reflection bodies (thought/note text) that share the single most common
    CONTENT-TOKEN PAIR — the fingerprint of circling one theme even while rephrasing it every tick."""
    import itertools
    from collections import Counter
    import knowledge as _k
    bs = [b for b in (bodies or []) if b and str(b).strip()]
    if len(bs) < 4:
        return 0.0
    counts: Counter = Counter()
    for b in bs:
        toks = sorted(_k._content_toks(b))
        for pair in {p for p in itertools.combinations(toks, 2)}:
            counts[pair] += 1
    if not counts:
        return 0.0
    return counts.most_common(1)[0][1] / len(bs)


def motif_bump(bodies: list) -> int:
    """+MOTIF_BUMP frustration when recent reflection is dominated by one motif — the content-aware
    rumination brake that catches a loop journaling through action tools."""
    return MOTIF_BUMP if motif_dominance(bodies) >= MOTIF_DOMINANCE else 0


def rumination_bump(outcomes: list[dict], window: int = RUMINATE_WINDOW,
                    k: int = RUMINATE_K) -> int:
    """Extra gate frustration when the recent window is dominated by thought ticks.
    +1 at k thoughts/window (analysis-paralysis), +2 when the ENTIRE window is thoughts
    (fully stalled). Windowed, not consecutive — interleaved bookkeeping doesn't hide it."""
    streak = rumination_streak(outcomes, window)
    if streak >= window:
        return 2
    if streak >= k:
        return 1
    return 0


def compute_condition(outcomes: list[dict]) -> str:
    """Discrete condition label from the recent window (DMN). One of:
    RECOVERY (just climbed out of a failure streak), STRAINED (chronic failure / high strain),
    RUMINATING (thinking instead of acting), FOCUSED (recent run of successes),
    STABLE (default / idle)."""
    if not outcomes:
        return "STABLE"
    window = outcomes[-_WINDOW:]
    last = window[-1]
    recent = window[-4:]
    fails = sum(1 for o in recent if not o.get("ok"))
    # Thought ticks log ok=True but are NOT successes for condition purposes — 4 musings in a
    # row must not read as FOCUSED (that's the analysis-paralysis state, the opposite).
    succ = sum(1 for o in recent if o.get("ok") and o.get("tool") not in _THOUGHT_TOOLS)
    strain = compute_strain(window)

    # Just recovered: this tick succeeded but the immediately preceding 2+ failed.
    # (A thought doesn't recover anything — it has to actually DO something.)
    prior = window[-3:-1]
    if (last.get("ok") and last.get("tool") not in _THOUGHT_TOOLS
            and len(prior) >= 2 and all(not o.get("ok") for o in prior)):
        return "RECOVERY"
    if strain >= STRAIN_HIGH or fails >= 3:
        return "STRAINED"
    if rumination_bump(window) > 0:
        return "RUMINATING"
    if succ >= 3:
        return "FOCUSED"
    return "STABLE"


def gate_frustration_bump(outcomes: list[dict]) -> int:
    """Extra frustration to feed the objectives gate this tick — the mechanical teeth.
    When strained, a stalled objective parks/rotates FASTER; a hard repeated-failure run pushes
    harder still. 0 when healthy (the gate behaves normally)."""
    strain = compute_strain(outcomes)
    if strain < STRAIN_HIGH:
        return 0
    bump = 1
    if repeated_failure_signature(outcomes, k=3):
        bump += 1                 # the exact same dead end, 3+ in a row → push to pivot now
    return bump


# ============================================================================================
# Pillars 4.1: prediction settlement — GLUE is the only closer of expectations (M-4, §4 CA1)
# ============================================================================================
def settle_predictions(config, *, event_text: str = "", tick: int = 0,
                       reward=None, curiosity=None, stats=None) -> list:
    """Settle open predictions MECHANICALLY (Dean, confirmed 2026-07-03: settlement is mechanical
    only). Two grounds, both outside the model: a bet's DEADLINE passed (the future arrived — it did
    not come true), or a MATCHING EVENT was observed (it did). The LLM saying "that came true"
    settles nothing — this glue call is the only closure path, and expectations.close_prediction
    rejects any reason that is not deadline/event ground truth.

    Each closure's surprise (f(confidence, wrongness) — a confident-wrong bet scores highest) feeds
    the SAME intrinsic channel the world model uses: `curiosity.observe(surprise)` turns it into the
    bounded intrinsic bonus + restlessness, and `reward.observe(..., intrinsic=bonus)` folds that
    into this beat's RPE (success = whether the bet came true). Both hooks are optional + duck-typed
    (the eidos.py cutover passes the live CuriosityDrive / RewardLearner) so glue stays free of the
    nervous package. Each closure has already birthed its residue episode engram (the §M-4
    highest-value episodic input) inside expectations.close_prediction.

    DARK behind `config.pillars_expectations_enabled` — returns [] untouched when off. Best-effort:
    settlement failures never raise into the loop (glue convention). Returns the list of
    expectations.Closure settled by this call."""
    if not getattr(config, "pillars_expectations_enabled", False):
        return []
    try:
        import expectations
        ledger = expectations.ExpectationLedger(config)
        closures = []
        # Claim-bearing bets first: measured against the same glue-checked stats dict quest
        # criteria use (verdicts, not text-matching). `stats` comes from the caller's
        # _quest_stats; with none provided, claim bets simply defer to a later pass.
        closures += expectations.close_claim_predictions(config, ledger, stats, tick=tick)
        if event_text:
            closures += expectations.close_event_predictions(config, ledger, event_text, tick=tick)
        closures += expectations.close_due_predictions(config, ledger, tick=tick)
    except Exception:  # noqa: BLE001 - glue is best-effort
        return []
    for c in closures:
        try:
            intrinsic = 0.0
            if curiosity is not None and hasattr(curiosity, "observe"):
                intrinsic = float(curiosity.observe(c.surprise) or 0.0)
            if reward is not None and hasattr(reward, "observe"):
                reward.observe(situation=f"prediction:{c.prediction.domain}",
                               action="prediction_settled", success=bool(c.outcome),
                               made_progress=bool(c.outcome), intrinsic=intrinsic, tick=tick)
        except Exception:  # noqa: BLE001 - the closure stands even if a hook hiccups
            pass
        # 4.3b: a bet the world proved RIGHT is mastery evidence — but only a real one:
        # confidence in the declared band and closed at its DEADLINE (claim/event closures can
        # settle the instant they're placed — the self-fulfilling 'my own file exists' farm).
        # Novelty scoring on the TARGET collapses repeated same-shape bets to dup weight.
        # Load-award-save persona (skills.py's seam precedent); best-effort, flag-gated inside.
        try:
            import mastery
            if bool(c.outcome) and mastery.prediction_counts(c.prediction.confidence, c.reason):
                import persona as _persona_mod
                p = _persona_mod.load_persona(config.workspace)
                if mastery.record_evidence(config, p, "prediction_settled",
                                           c.prediction.id or f"pred-{tick}-{c.prediction.target[:40]}",
                                           title=c.prediction.target, tick=tick):
                    _persona_mod.save_persona(config.workspace, p)
        except Exception:  # noqa: BLE001 - evidence is best-effort; the closure stands
            pass
    return closures


# ============================================================================================
# Pillars 2.3: bet settlement — GLUE is the only settler of memory bets (M-1, decision #5)
# ============================================================================================
def settle_bets(config, *, tick: int = 0, action_tool: str = "", action_args=None,
                ledger=None) -> list:
    """Settle this tick's open memory bets MECHANICALLY (decision #5). The outcome comes from
    glue's OWN adjudicated record (recent_outcomes — the typed fail_kind channel written by the
    harness), and the action signature is computed from the EXECUTED tool call the harness passes
    in. There is deliberately no parameter through which a narrated outcome could arrive — the LLM
    saying "that worked" settles nothing. Call this right after record_outcome() for the same
    tick (the 4.1 settle_predictions call-site pattern); the cutover phase wires it into eidos.py.

    DARK behind `config.pillars_bet_ledger_enabled` — returns [] untouched when off. Best-effort:
    settlement failures never raise into the loop (glue convention). Returns the list of
    settlement dicts from bets.BetLedger.settle."""
    if not getattr(config, "pillars_bet_ledger_enabled", False):
        return []
    try:
        import bets
        led = ledger if ledger is not None else bets.BetLedger(config)
        outs = recent_outcomes(config, 1)
        if not outs:
            return []
        success = bool(outs[-1].get("ok"))
        sig = bets.action_signature(action_tool or str(outs[-1].get("tool", "")), action_args)
        return led.settle(tick=tick, success=success, action_sig=sig)
    except Exception:  # noqa: BLE001 - glue is best-effort
        return []


# ============================================================================================
# Pillars 4.4: the surfaced-news accessor — where the dashboard fetches the digest
# ============================================================================================
def surfaced_news(config) -> list:
    """The most recently SURFACED news items (Pillars 4.4 cutover seam). The loop's presence
    handler (the listening hold) surfaces the ranked digest through NewsQueue.surface and
    snapshots it to `state/news_surfaced.json`; this read-only accessor is where the dashboard
    (a separate process — it reads files, never the loop's objects) fetches that digest without
    touching dashboard.py in this pass.

    DARK behind `config.pillars_news_enabled` — returns [] untouched when off. Best-effort:
    a missing/corrupt snapshot is an empty digest, never a raise (glue convention)."""
    if not getattr(config, "pillars_news_enabled", False):
        return []
    try:
        raw = (config.state_dir / "news_surfaced.json").read_text(encoding="utf-8")
        items = json.loads(raw)
        return items if isinstance(items, list) else []
    except Exception:  # noqa: BLE001 - glue is best-effort
        return []
