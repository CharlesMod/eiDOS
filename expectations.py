"""Pillars 4.1: the expectation ledger — the future dimension's first organ (PILLARS_PLAN §2 M-4,
§4 comparator; PILLARS_TODO 4.1).

The plan's comparator principle (§4, brain-map CA1): *expectation vs input → novelty*. A creature
that only records what happened has no future dimension; one that commits a TYPED, measurable BET
about what WILL happen — "backup done by 02:30", "Dean home ~18:00" — and is later scored on it,
gains one. That residue ("what I got wrong") is, per §M-4, the single highest-value input to the
episodic store, because a confident-wrong closure is exactly the memory worth keeping.

The organ, in three parts:

  1. A prediction IS an engram (engram.KINDS has 'prediction'). Creating one via the grammar-
     constrained `predict` tool writes a kind='prediction' engram into the episodic ring — no new
     store, no parallel bookkeeping. The ledger is a VIEW over those open prediction engrams.

  2. Closure is adjudicated by GLUE, never self-report (the doctrine's central rule, glue.py: an
     outcome is settled by the deterministic layer OUTSIDE the model, not by prose the model emits).
     A prediction closes on its DEADLINE (the future arrived and it was / wasn't met) or on a MATCHING
     EVENT (the thing it bet on was observed). The LLM saying "yeah that came true" closes nothing.

  3. Closure scores `surprise = f(confidence, wrongness)` — a confident-wrong bet is maximally
     surprising; an unconfident-wrong one barely moves the needle; a confident-right one is quietly
     confirming. That surprise feeds reward RPE + curiosity (the same scalar the world model emits),
     and BIRTHS an episode engram carrying the residue.

Discipline (PILLARS_PLAN §0):
  §0.2  No line here names the behavior it wants. This builds the MECHANISM — a bounded ledger of
        typed bets, glue-adjudicated closure, a surprise score, a residue engram — and "anticipation"
        / "calibration" is what a creature running this loop over time does.
  §0.4  Every constant is derived or a DECLARED knob with a one-line justification. The two policy
        flags (enable, max-open) already live in config; extra tuning is named module constants below.

Ships DARK behind `config.pillars_expectations_enabled` (default False). This module is a pure
LIBRARY: with the flag off the `predict` tool is not registered (tools.py gates it) and
`close_due_predictions` / `close_prediction` are no-ops, so the running system is unchanged. Wiring
the "awaiting" context block and the Brier→temperament sleep job into the live loop is a later
cutover; this module only EXPOSES the renderer and the calibration function.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Optional

import engram
from engram import Engram, EpisodicRing, _episodic_path, _overlap


# --- Declared knobs (§0.4: each a labeled design knob with its one-line justification) -----------
SURPRISE_MAX = 6.0          # declared: cap on the closure surprise scalar, in the SAME units as
                            # nervous.worldmodel.SURPRISE_MAX (-log2 p, ~6 bits = maximally novel), so
                            # a closure's surprise composes with world-model surprise on the one
                            # reward/curiosity scale without a rescaling seam. Kept as a local copy
                            # (not imported) so expectations.py stays free of the nervous package —
                            # it is a pure library and the nervous system imports IT, never the reverse.
DEFAULT_CONFIDENCE = 0.6    # declared: a prediction with no stated confidence is treated as mildly-
                            # committed — above a coin flip (the creature bothered to bet) but well
                            # short of certain, leaving headroom for a wrong-but-tentative bet to
                            # score low surprise.
PREDICTION_STRENGTH = 0.5   # declared: a fresh prediction engram starts at neutral usefulness
                            # (engram.STRENGTH_DEFAULT) — it has neither earned recall nor been shown
                            # useless; its closure residue is what the bet ledger later scores.
RESIDUE_STRENGTH_FLOOR = 0.5  # declared: the episode engram born at closure starts at least neutral,
                            # and is lifted toward 1.0 by the closure's surprise — a confident-wrong
                            # residue is born STRONG (§M-4: the highest-value episodic input), so it
                            # resists forgetting before the bet ledger has scored it.
DEFAULT_DOMAIN = "general"  # declared: predictions with no explicit domain bucket here, so Brier
                            # calibration still has a home for un-tagged bets.


# ============================================================================================
# The prediction — a typed bet, stored AS an engram (kind='prediction')
# ============================================================================================
# A prediction's structured fields (statement / target / deadline / confidence / domain / status)
# live in a small json object inside the engram BODY, so a prediction needs no schema change to the
# engram atom — it is one more kind of memory. `body` stays a human-readable string (the statement),
# and the machine fields ride in a `pred:{...}` suffix that the ledger parses back out.

_PRED_TAG = "pred:"   # the body suffix marker that carries a prediction's machine-readable fields


def _now_epoch() -> float:
    return time.time()


@dataclass
class Prediction:
    """A typed, measurable bet awaiting glue closure. `statement` is what the creature expects in
    words; `target` is the MEASURABLE claim glue adjudicates against; `deadline` is the epoch second
    by which it must resolve; `confidence` in [0,1] is how sure the creature is. `domain` buckets it
    for per-domain calibration. Round-trips through an engram (to_engram / from_engram)."""
    statement: str
    target: str
    deadline: float
    confidence: float = DEFAULT_CONFIDENCE
    domain: str = DEFAULT_DOMAIN
    status: str = "open"            # open | closed
    outcome: Optional[bool] = None  # set by glue at closure: True=came true, False=wrong
    closed_tick: Optional[int] = None
    id: Optional[str] = None        # the backing engram's id (set once persisted)

    def _fields(self) -> dict:
        return {"statement": self.statement, "target": self.target, "deadline": self.deadline,
                "confidence": self.confidence, "domain": self.domain, "status": self.status,
                "outcome": self.outcome, "closed_tick": self.closed_tick}

    def to_engram(self, *, encoded_at: Optional[engram.EncodedAt] = None) -> Engram:
        """Materialize this bet as a kind='prediction' engram. The statement is the readable body;
        the machine fields ride in the `pred:{...}` suffix so recall/consolidation see plain text.
        Re-materializing an ALREADY-PERSISTED bet keeps its engram id (identity is stable across the
        open→closed rewrite; a fresh bet gets a fresh id)."""
        body = f"{self.statement.strip()} {_PRED_TAG}{json.dumps(self._fields(), ensure_ascii=False)}"
        eg = Engram(
            kind="prediction", body=body,
            provenance="experienced",            # the creature made this bet first-hand
            confidence=float(self.confidence),   # the engram's confidence mirrors the bet's
            strength=PREDICTION_STRENGTH,
            encoded_at=encoded_at or engram.EncodedAt(),
        )
        if self.id:
            eg.id = self.id                      # in-place update path: same atom, new body
        eg.validate()
        self.id = eg.id
        return eg

    @staticmethod
    def from_engram(eg: Engram) -> Optional["Prediction"]:
        """Parse a prediction engram back into a Prediction, or None if it is not one / is malformed.
        Best-effort: a corrupt suffix yields None rather than raising (house convention)."""
        if eg.kind != "prediction":
            return None
        body = eg.body or ""
        marker = body.rfind(_PRED_TAG)
        if marker < 0:
            return None
        try:
            f = json.loads(body[marker + len(_PRED_TAG):])
        except (ValueError, json.JSONDecodeError):
            return None
        if not isinstance(f, dict):
            return None
        try:
            p = Prediction(
                statement=str(f.get("statement", body[:marker].strip())),
                target=str(f.get("target", "")),
                deadline=float(f.get("deadline", 0.0)),
                confidence=float(f.get("confidence", DEFAULT_CONFIDENCE)),
                domain=str(f.get("domain", DEFAULT_DOMAIN)),
                status=str(f.get("status", "open")),
                outcome=f.get("outcome"),
                closed_tick=(int(f["closed_tick"]) if f.get("closed_tick") is not None else None),
            )
        except (ValueError, TypeError):
            return None
        p.id = eg.id
        return p


# ============================================================================================
# The surprise score — the residue's magnitude (§M-4 / §4 comparator)
# ============================================================================================
def surprise_of(confidence: float, outcome: bool) -> float:
    """`surprise = f(confidence, wrongness)`, in SURPRISE_MAX units (bits), for a closed bet.

    Wrongness is the gap between what the creature believed the probability of the outcome was and
    what actually happened: a bet held at confidence c that comes TRUE was believed with prob c, so
    wrongness = 1 - c; one that comes FALSE was believed with prob c of true, so wrongness = c.
    Surprise is the self-information of that error, -log2(1 - wrongness), scaled to SURPRISE_MAX — a
    Shannon surprisal, the same currency the world model emits.

    This makes the ordering the acceptance test demands STRUCTURAL: a confident-WRONG bet (c high,
    outcome False → wrongness ≈ c ≈ 1) scores near SURPRISE_MAX; an unconfident-wrong bet (c low →
    wrongness small) scores near 0; a confident-RIGHT bet is quietly confirming (low surprise)."""
    import math
    c = max(0.0, min(1.0, float(confidence)))
    wrongness = (1.0 - c) if outcome else c
    # self-information of the error, clamped so wrongness→1 doesn't blow to +inf.
    wrongness = min(wrongness, 1.0 - 1e-6)
    bits = -math.log2(1.0 - wrongness)
    return max(0.0, min(SURPRISE_MAX, bits))


@dataclass
class Closure:
    """The result of glue closing one prediction: the surprise scalar (for reward RPE + curiosity),
    the residue episode engram (born into the ring), and the settled prediction. Callers wire the
    surprise onto reward/curiosity; this module does not import the nervous system."""
    prediction: Prediction
    outcome: bool
    surprise: float
    residue: Engram
    reason: str          # "deadline" | "event" — why glue closed it


# ============================================================================================
# The expectation ledger — a VIEW over open prediction engrams (bounded)
# ============================================================================================
class ExpectationLedger:
    """Tracks OPEN predictions as engrams in the episodic ring, bounded by
    `config.pillars_max_open_predictions` (§0.4: no unbounded growth). `predict()` refuses a new bet
    past the bound (or, with evict_oldest, retires the oldest open bet to make room); `render()`
    surfaces the open bets as a small "awaiting" context block for a LATER cutover into context.py
    (this module only exposes the renderer, per the phase's discipline)."""

    def __init__(self, config, *, ring: Optional[EpisodicRing] = None):
        self.config = config
        self.ring = ring or EpisodicRing(config)

    # --- read the ledger ----------------------------------------------------------------------
    def _all_predictions(self) -> list[Prediction]:
        out: list[Prediction] = []
        for eg in self.ring.load():
            p = Prediction.from_engram(eg)
            if p is not None:
                out.append(p)
        return out

    def open_predictions(self) -> list[Prediction]:
        """Every currently-open bet, oldest-first (ring order)."""
        return [p for p in self._all_predictions() if p.status == "open"]

    @property
    def max_open(self) -> int:
        # The declared bound. Read live from config so a config edit takes effect without a reload.
        return int(getattr(self.config, "pillars_max_open_predictions", 12))

    # --- make a bet ---------------------------------------------------------------------------
    def predict(self, *, statement: str, target: str, deadline: float,
                confidence: float = DEFAULT_CONFIDENCE, domain: str = DEFAULT_DOMAIN,
                encoded_at: Optional[engram.EncodedAt] = None,
                evict_oldest: bool = False) -> Prediction:
        """Commit a new prediction (writes a kind='prediction' engram to the ring). BOUNDED: if the
        open count is already at the cap, refuse (raise) — or, when evict_oldest is set, close the
        OLDEST open bet as unmet to make room (a bet you never resolved is a bet you got wrong). This
        is the "no unbounded growth" gate the acceptance test drives directly."""
        statement = (statement or "").strip()
        if not statement:
            raise ValueError("prediction statement must be a non-empty string")
        open_now = self.open_predictions()
        if len(open_now) >= self.max_open:
            if not evict_oldest:
                raise ValueError(
                    f"expectation ledger is full ({len(open_now)}/{self.max_open} open predictions); "
                    "refuse a new bet until one closes (bounded, no unbounded growth)")
            # Evict the oldest open bet — retire it (it went unresolved, so it did not come true).
            self._retire(open_now[0])
        p = Prediction(statement=statement, target=(target or "").strip(),
                       deadline=float(deadline), confidence=float(confidence),
                       domain=(domain or DEFAULT_DOMAIN).strip() or DEFAULT_DOMAIN)
        eg = p.to_engram(encoded_at=encoded_at)
        self.ring.encode(eg)
        return p

    def _retire(self, p: Prediction) -> None:
        """Mark an open bet closed-as-unmet in the ring (eviction path). Rewrites its engram in place
        so the open count drops; no residue/surprise (an evicted-for-room bet is not a scored close)."""
        p.status = "closed"
        p.outcome = False
        self._rewrite(p)

    # --- persistence: rewrite a prediction engram in place ------------------------------------
    def _rewrite(self, p: Prediction) -> None:
        """Persist a mutated prediction back into the ring. The ring is append-only jsonl, so we
        rewrite the whole file with this prediction's engram line's body replaced (the ring stays
        small and bounded, so a whole-file rewrite is cheap — the same pattern EpisodicRing._trim
        uses). Only the body changes; created / encoded_at / stats are preserved."""
        if not p.id:
            return
        fp = _episodic_path(self.config)
        try:
            lines = fp.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            return
        new_engram = p.to_engram()   # regenerates body from fields; keeps p.id (in-place replacement)
        out: list[str] = []
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
            except (ValueError, json.JSONDecodeError):
                out.append(line)
                continue
            if d.get("id") == p.id:
                d["body"] = new_engram.body      # swap only the body; preserve the rest
                out.append(json.dumps(d, ensure_ascii=False))
            else:
                out.append(line)
        tmp = fp.with_suffix(".jsonl.tmp")
        tmp.write_text("\n".join(out) + ("\n" if out else ""), encoding="utf-8")
        tmp.replace(fp)

    # --- the "awaiting" context block (renderer only — NOT wired into context.py here) --------
    def render(self, *, now: Optional[float] = None, limit: int = 6) -> str:
        """A small human-readable block of the open bets, newest-first, for a LATER cutover into the
        context assembly. Returns "" when there is nothing awaiting (so the caller can omit the block
        entirely). Kept terse — this is a status strip, not a report."""
        now = _now_epoch() if now is None else now
        opens = list(reversed(self.open_predictions()))[:limit]
        if not opens:
            return ""
        lines = ["AWAITING (open predictions — glue will score these):"]
        for p in opens:
            when = p.deadline - now
            due = f"in {_fmt_dur(when)}" if when >= 0 else f"OVERDUE {_fmt_dur(-when)}"
            lines.append(f"  • {p.statement}  (target: {p.target or '—'}; conf {p.confidence:.0%}; {due})")
        return "\n".join(lines)


def _fmt_dur(seconds: float) -> str:
    seconds = abs(float(seconds))
    if seconds < 90:
        return f"{int(seconds)}s"
    if seconds < 5400:
        return f"{int(seconds / 60)}m"
    if seconds < 172800:
        return f"{seconds / 3600:.1f}h"
    return f"{seconds / 86400:.1f}d"


# ============================================================================================
# Closure — adjudicated by GLUE, never self-report (glue.py doctrine)
# ============================================================================================
# The only grounds on which a bet may settle. This whitelist is the MECHANICAL form of "LLM
# self-report never closes a prediction" (Dean, confirmed 2026-07-03: settlement is mechanical
# only): there is no 'self_report' reason, no closing tool in tools.TOOLS, and close_prediction
# raises on any reason outside this set — the model's prose has no path to a settled bet.
CLOSE_REASONS = frozenset({
    "deadline",   # the future arrived and no matching event had settled it → it did not come true
    "event",      # a matching observation landed → it came true
})


def close_prediction(config, ledger: ExpectationLedger, p: Prediction, *,
                     outcome: bool, reason: str, tick: int = 0,
                     encoded_at: Optional[engram.EncodedAt] = None) -> Optional[Closure]:
    """GLUE closes one prediction (deadline arrived, or a matching event was observed). Computes the
    surprise, marks the bet closed in the ring, and BIRTHS an episode engram (the residue) whose
    strength is lifted by the surprise — a confident-wrong close is born strong (§M-4). Returns the
    Closure (surprise + residue) so the CALLER wires the surprise onto reward RPE + curiosity; this
    module never imports the nervous system.

    DARK GATE: a no-op returning None when `pillars_expectations_enabled` is off — the running system
    is unchanged. This is the flag the acceptance tests drive around."""
    if reason not in CLOSE_REASONS:
        raise ValueError(
            f"a prediction settles only on glue ground truth {sorted(CLOSE_REASONS)}; "
            f"{reason!r} (e.g. the model's own say-so) cannot close a bet")
    if not getattr(config, "pillars_expectations_enabled", False):
        return None
    if p.status != "open":
        return None
    surprise = surprise_of(p.confidence, outcome)

    # Mark the prediction closed in the ring (glue's settlement — not the model's word).
    p.status = "closed"
    p.outcome = bool(outcome)
    p.closed_tick = int(tick)
    ledger._rewrite(p)

    # The residue: an episode engram carrying "what I bet vs what happened". Its strength is lifted
    # toward 1.0 by the surprise (confident-wrong → strong, resists forgetting before it is scored).
    verdict = "CAME TRUE" if outcome else "WRONG"
    residue_body = (f"I predicted: {p.statement} (target: {p.target or '—'}; "
                    f"held at {p.confidence:.0%} confidence). Outcome: {verdict}. "
                    f"[closed by {reason}]")
    strength = max(RESIDUE_STRENGTH_FLOOR, min(1.0, RESIDUE_STRENGTH_FLOOR
                    + (1.0 - RESIDUE_STRENGTH_FLOOR) * (surprise / SURPRISE_MAX)))
    residue = Engram(
        kind="episode", body=residue_body, provenance="experienced",
        strength=strength, encoded_at=encoded_at or engram.EncodedAt(tick=tick),
        links=[p.id] if p.id else [],
    )
    residue.validate()
    ledger.ring.encode(residue)

    return Closure(prediction=p, outcome=bool(outcome), surprise=surprise,
                   residue=residue, reason=reason)


def close_due_predictions(config, ledger: ExpectationLedger, *, now: Optional[float] = None,
                          tick: int = 0) -> list[Closure]:
    """Close every open prediction whose DEADLINE has passed (the future arrived and the bet was not
    met by an event → it did not come true). This is the deadline half of glue closure; the event
    half is `close_prediction(..., reason='event', outcome=...)` called from glue when a matching
    observation lands.

    DARK GATE: returns [] when the flag is off (no-op — the running system is unchanged)."""
    if not getattr(config, "pillars_expectations_enabled", False):
        return []
    now = _now_epoch() if now is None else now
    closures: list[Closure] = []
    for p in ledger.open_predictions():
        if p.deadline and p.deadline <= now:
            c = close_prediction(config, ledger, p, outcome=False, reason="deadline", tick=tick)
            if c is not None:
                closures.append(c)
    return closures


EVENT_MATCH_OVERLAP = 0.5   # declared: token-overlap coefficient (engram._overlap — the memory
                            # economy's one embedding-free similarity) at/above which an OBSERVED
                            # event settles a bet's measurable target as come-true. 0.5 = at least
                            # half the smaller token set shared — well above accidental collision on
                            # a typed target, low enough that a paraphrased observation still lands.


def close_event_predictions(config, ledger: ExpectationLedger, event_text: str, *,
                            tick: int = 0) -> list[Closure]:
    """Close every open prediction whose measurable TARGET matches an OBSERVED event (the event half
    of glue closure; glue.settle_predictions is the caller). The match is mechanical — token overlap
    between the bet's target (falling back to its statement) and the event text — so what settles a
    bet is an observation the harness saw, never prose the model emitted about itself.

    DARK GATE: returns [] when the flag is off (no-op — the running system is unchanged)."""
    if not getattr(config, "pillars_expectations_enabled", False):
        return []
    event_text = (event_text or "").strip()
    if not event_text:
        return []
    closures: list[Closure] = []
    for p in ledger.open_predictions():
        probe = (p.target or p.statement).strip()
        if probe and _overlap(probe, event_text) >= EVENT_MATCH_OVERLAP:
            c = close_prediction(config, ledger, p, outcome=True, reason="event", tick=tick)
            if c is not None:
                closures.append(c)
    return closures


# ============================================================================================
# Brier calibration by domain — the sleep-job seam (§M-4: calibration → temperament caution)
# ============================================================================================
def brier_calibration_by_domain(config, ledger: Optional[ExpectationLedger] = None) -> dict[str, dict]:
    """Per-domain Brier score over CLOSED predictions — the SEAM a sleep job calls to turn the
    creature's calibration into a bounded temperament-caution adjustment (a chronically over-confident
    domain should widen caution; a well-calibrated one should not). This module EXPOSES the function
    cleanly; wiring it into the sleep engine is another agent's job this wave (see the note below).

    Brier score = mean((forecast_prob_of_true − actual)²) over a domain's closed bets, where the
    forecast prob of TRUE is the stated confidence and actual ∈ {0,1}. Lower is better-calibrated
    (0 = perfect, 0.25 = a coin flip's worth of error, →1 = confidently wrong). Returns
    {domain: {"brier": float, "n": int, "mean_confidence": float}} over domains with ≥1 closed bet.

    NOTE TO THE SLEEP-ENGINE OWNER: call this at a sleep beat, read `brier` per domain, and map a
    HIGH brier (poor calibration) to a small BOUNDED increase in the temperament caution setpoint for
    that domain — spring it back toward the genome baseline as calibration recovers (pitfall #3). Do
    NOT let one bad night ratchet caution; clamp the step. This function is pure / read-only and never
    raises into the sleep loop."""
    ledger = ledger or ExpectationLedger(config)
    buckets: dict[str, list[Prediction]] = {}
    for p in ledger._all_predictions():
        if p.status == "closed" and p.outcome is not None:
            buckets.setdefault(p.domain or DEFAULT_DOMAIN, []).append(p)
    out: dict[str, dict] = {}
    for domain, preds in buckets.items():
        n = len(preds)
        if n == 0:
            continue
        se = 0.0
        conf_sum = 0.0
        for p in preds:
            c = max(0.0, min(1.0, float(p.confidence)))
            actual = 1.0 if p.outcome else 0.0
            se += (c - actual) ** 2
            conf_sum += c
        out[domain] = {"brier": round(se / n, 4), "n": n,
                       "mean_confidence": round(conf_sum / n, 4)}
    return out
