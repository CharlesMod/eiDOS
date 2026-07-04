"""Pillars 4.2: learning-progress XP — pay the slope, not the volume (PILLARS_PLAN §6; pitfall #1).

Decision #5b (CONFIRMED 2026-07-03): XP = learning-progress-weighted adjudicated success. The old
formula was a volume clock — level by existing — and the two obvious replacements are both traps:
raw success is grindable (repeat the mastered action forever), raw surprise is noise-farmable (the
noisy-TV trap, pitfall #1: stare at irreducible randomness and every glance pays). The fix, per §6,
is to pay for the *downward slope* of prediction error per domain:

    high-and-FALLING error  = the frontier — learning is happening      → pays richly
    high-and-flat  error    = noise — unlearnable, nothing is happening → pays ~0
    low-and-flat   error    = mastered — nothing LEFT to happen         → pays ~0

Grind-proof and noise-proof BY CONSTRUCTION: a grind drives its domain low-and-flat (weight → 0), a
noise domain never develops a statistically real trend (the R² gate zeroes it), and the only way to
keep earning is to keep finding domains where error is actually falling — i.e. to keep learning.

The organ, in three parts:

  1. `ProgressTracker` — a per-domain, bounded series of recent prediction errors (persisted as
     json under workspace/state, the glue.py/outcomes convention; fail-open like every other state
     file: a corrupt or missing file is an empty tracker, never a crash). `observe(domain, error)`
     is the feed; domains seed from objective / skill-tier / world-model situation keys
     (`domain_key()` normalizes them). Errors arrive in whatever unit the caller has — expectation
     closure surprise (bits), world-model surprise, Brier-ish wrongness — the weighting is
     SCALE-FREE (progress is measured relative to the domain's own mean error), so units only need
     to be consistent *within* a domain.

  2. `progress_weight(domain)` — the XP multiplier in [0, WEIGHT_MAX]: a least-squares trend is fit
     over the domain's recent window; the weight is (fitted total fall ÷ mean error) × R², gated to
     zero when the trend is statistically indistinguishable from noise. See the knobs below for why
     each factor exists.

  3. `restlessness_signal(domain)` — the SAME learning-progress signal shaped [0,1] for the
     curiosity organ (see the cutover note at the function).

Discipline (PILLARS_PLAN §0):
  §0.2  No line here names the behavior it wants. This builds the MECHANISM — a bounded error
        series, a trend fit, a multiplicative pay rule — and "seeks the frontier / abandons the
        mastered / ignores the unlearnable" is what a creature paid this way does over time.
  §0.4  Every constant is a DECLARED knob with a one-line justification (below).

Ships DARK behind `config.pillars_learning_xp_enabled` (default False). This module is a pure
LIBRARY: nothing in the live loop calls it yet, and the one live seam — persona.award_xp — only
routes through the weighting when the flag is on AND the caller names a domain, so with the flag
off award_xp is byte-identical to today (tests assert this on the persona dict). This module never
imports the nervous package (the nervous system may later import IT, never the reverse — the same
one-way rule expectations.py declares).
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Optional

logger = logging.getLogger("eidos.learning_progress")


# --- Declared knobs (§0.4: each a labeled design knob with its one-line justification) -----------
WINDOW = 24                 # declared: errors kept per domain — long enough that a least-squares
                            # trend over it is stable (the R² gate needs ~20+ points to separate
                            # signal from noise), short enough that mastery registers within about a
                            # day's worth of adjudicated outcomes rather than dragging weeks of tail.
MIN_OBSERVATIONS = 6        # declared: below this a slope fit is astrology, not statistics — an
                            # unpriceable domain pays NOVICE_WEIGHT (first contact is not punished),
                            # and the exposure is bounded (≤ 5 base-rate awards before the trend
                            # takes over — a grind or noise domain caps its lifetime take there).
NOVICE_WEIGHT = 1.0         # declared: an unmeasured domain pays exactly the old volume rate — you
                            # cannot price a slope you cannot fit, and neither punishing nor
                            # jackpotting first contact is defensible, so it is pay-neutral.
WEIGHT_MAX = 2.0            # declared: the frontier ceiling — a domain in full measured fall pays
                            # at most 2× the old volume rate, so XP inflation is bounded by
                            # construction (no weighting bug can mint more than 2× legacy pay).
R2_GATE = 0.4               # declared: the noise gate — a fitted trend explaining < 40% of the
                            # window's variance is statistically indistinguishable from chance at
                            # this window length (for n=24 random series, clearing 0.4 needs
                            # |t|≈3.8, p≈0.001 — coin-flip domains essentially never pay), while a
                            # genuine learning curve fits at R² near 1 and sails over it.
MAX_DOMAINS = 64            # declared: bound on tracked domains (no unbounded growth, house rule);
                            # past it the least-recently-observed domain is evicted — a domain the
                            # creature no longer acts in no longer needs a priced frontier.
STATE_FILENAME = "learning_progress.json"   # lives in workspace/state (the outcomes.jsonl convention)
_EPS = 1e-9                 # numeric guard for the scale-free division; not a tuning knob.


# ============================================================================================
# Domain keys — seeded from objective / skill-tier / world-model situation keys
# ============================================================================================
def domain_key(*parts: Optional[str]) -> str:
    """Normalize objective / skill-tier / situation fragments into one domain key: non-empty parts
    lowercased, inner whitespace collapsed, joined with '/', truncated (the world model truncates
    its context keys the same way — a key is a bucket label, not a document). Empty input buckets
    to 'general', mirroring expectations.DEFAULT_DOMAIN so the two ledgers key alike."""
    cleaned = []
    for p in parts:
        p = "_".join(str(p or "").strip().lower().split())
        if p:
            cleaned.append(p)
    return ("/".join(cleaned) or "general")[:120]


# ============================================================================================
# The trend fit — least squares over the recent window (slope + R²)
# ============================================================================================
def _fit(series: list[float]) -> tuple[float, float]:
    """Least-squares (slope, R²) of error vs observation index. A constant series has no trend by
    definition: (0, 0) — which is exactly what makes flat-anything pay nothing downstream. Fewer
    than 2 points likewise (0, 0)."""
    n = len(series)
    if n < 2:
        return 0.0, 0.0
    xm = (n - 1) / 2.0
    ym = sum(series) / n
    sxx = sum((i - xm) ** 2 for i in range(n))
    sxy = sum((i - xm) * (series[i] - ym) for i in range(n))
    ss_tot = sum((v - ym) ** 2 for v in series)
    if ss_tot < _EPS or sxx < _EPS:
        return 0.0, 0.0
    slope = sxy / sxx
    ss_res = sum((series[i] - (ym + slope * (i - xm))) ** 2 for i in range(n))
    r2 = max(0.0, min(1.0, 1.0 - ss_res / ss_tot))
    return slope, r2


# ============================================================================================
# The tracker — bounded per-domain error series, persisted fail-open
# ============================================================================================
class ProgressTracker:
    """Per-domain prediction-error series (bounded to WINDOW each, MAX_DOMAINS total), persisted to
    workspace/state/learning_progress.json. Every read/write is fail-open: a missing, corrupt, or
    unwritable state file degrades to an empty tracker and a warning — the XP path (persona.award_xp)
    must never crash on the weighting's account."""

    def __init__(self, config):
        self.config = config
        self._domains: dict[str, dict] = self._load()   # domain -> {"errors": [...], "t": epoch}

    @property
    def path(self) -> Path:
        return self.config.state_dir / STATE_FILENAME

    # --- persistence (fail-open both directions) -----------------------------------------------
    def _load(self) -> dict[str, dict]:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        out: dict[str, dict] = {}
        if isinstance(raw, dict):
            for name, d in (raw.get("domains") or {}).items():
                if not isinstance(d, dict):
                    continue
                try:
                    errors = [max(0.0, float(v)) for v in (d.get("errors") or [])][-WINDOW:]
                    out[str(name)] = {"errors": errors, "t": float(d.get("t", 0.0))}
                except (TypeError, ValueError):
                    continue        # one corrupt domain never poisons the rest
        return out

    def _save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps({"domains": self._domains}, ensure_ascii=False),
                           encoding="utf-8")
            tmp.replace(self.path)
        except OSError as e:
            logger.warning("could not persist learning-progress state: %s", e)

    # --- the feed -------------------------------------------------------------------------------
    def observe(self, domain: str, error: float, *, now: Optional[float] = None) -> None:
        """Record one adjudicated outcome's prediction error for a domain (any non-negative unit,
        consistent within the domain — closure surprise, world-model surprise, Brier wrongness).
        Bounded: the series trims to WINDOW; the domain table evicts least-recently-observed past
        MAX_DOMAINS. Persists immediately (an award may follow in another process)."""
        domain = domain_key(domain)
        d = self._domains.setdefault(domain, {"errors": [], "t": 0.0})
        d["errors"].append(max(0.0, float(error)))
        del d["errors"][:-WINDOW]
        d["t"] = time.time() if now is None else float(now)
        while len(self._domains) > MAX_DOMAINS:
            oldest = min(self._domains, key=lambda k: self._domains[k].get("t", 0.0))
            self._domains.pop(oldest, None)
        self._save()

    def series(self, domain: str) -> list[float]:
        """The recent error series for a domain (a copy; oldest first)."""
        return list(self._domains.get(domain_key(domain), {}).get("errors", []))

    def slope(self, domain: str) -> float:
        """The fitted trend of recent prediction error (error units per observation; negative =
        falling = learning). Diagnostic view of the same fit the weight uses."""
        return _fit(self.series(domain))[0]

    # --- the learning-progress core (shared by XP weight and restlessness) -----------------------
    def _progress(self, domain: str) -> Optional[float]:
        """Normalized learning progress in [0,1], or None when the domain has too few observations
        to price (< MIN_OBSERVATIONS). The construction, factor by factor:

          drop = max(0, -slope) × (n-1)   the fitted TOTAL fall across the window — only a downward
                                          trend counts; rising error is not progress, it is damage.
          rel  = min(1, drop / mean)      scale-free: progress is the fall relative to the domain's
                                          own error level, so bits-valued and Brier-valued domains
                                          price identically and a near-mastered domain still earning
                                          real proportional improvement is not starved.
          × R², gated at R2_GATE          the noise damper (pitfall #1): the trend only pays insofar
                                          as it actually explains the series, and pays NOTHING when
                                          chance explains it just as well — a coin-flip domain shows
                                          drop ≠ 0 half the time but never a real fit, so it earns 0.

        high-and-falling → drop ≈ mean, R² ≈ 1 → ≈1. high-and-flat / low-and-flat → drop ≈ 0 (and a
        constant series is defined to R²=0) → 0."""
        errors = self.series(domain)
        n = len(errors)
        if n < MIN_OBSERVATIONS:
            return None
        slope, r2 = _fit(errors)
        if r2 < R2_GATE:
            return 0.0
        drop = max(0.0, -slope) * (n - 1)
        mean = sum(errors) / n
        rel = min(1.0, drop / (mean + _EPS))
        return rel * r2

    # --- the two shaped outputs ------------------------------------------------------------------
    def progress_weight(self, domain: str) -> float:
        """The XP multiplier in [0, WEIGHT_MAX] for an adjudicated success in this domain: the
        frontier (high-and-falling error) pays up to WEIGHT_MAX; noise and mastery pay ~0; an
        unpriceable (barely-seen) domain pays NOVICE_WEIGHT."""
        p = self._progress(domain)
        if p is None:
            return NOVICE_WEIGHT
        return WEIGHT_MAX * p

    def restlessness_signal(self, domain: str) -> float:
        """The learning-progress signal shaped [0,1] for the CURIOSITY organ: 1 = this domain is
        actively teaching (stay), 0 = mastered or unlearnable (move on), and an unexplored domain
        reads 1.0 — optimism in the face of no data is what makes the creature sample new ground at
        all (the explore side of the same coin NOVICE_WEIGHT covers for pay).

        CUTOVER NOTE (the 4.2 cutover owner, NOT this module): nervous/curiosity.py today EMAs raw
        per-tick surprise into its restlessness drive — exactly the noisy-TV-farmable input pitfall
        #1 forbids. The cutover points curiosity's per-domain input at THIS function, so
        restlessness follows learning progress rather than raw surprise. No nervous/ file is edited
        this wave (other agents own that tree); this module deliberately does not import nervous/.

        The genome's restlessness gene (openness up, tenacity down — dilettante vs deep-driller)
        multiplies the shaped signal here, where it is read; the result is re-clamped to [0,1] so
        the gene can never push restlessness outside the curiosity organ's contract. Fail-open ×1.0
        — a genome shapes the DRIVE to move on, never the XP the ledger pays."""
        p = self._progress(domain)
        base = 1.0 if p is None else p
        return max(0.0, min(1.0, base * self._restlessness_gene()))

    def _restlessness_gene(self) -> float:
        """genome.py multiplier on the restlessness signal — FAIL-OPEN 1.0 (no genome file / no
        module → byte-identical pre-genome behavior); never raises."""
        try:
            from genome import gene
            return float(gene(self.config, "restlessness"))
        except Exception:  # noqa: BLE001 - a genome must never break the curiosity input
            return 1.0


# ============================================================================================
# The XP seam — what persona.award_xp routes through when the flag is on
# ============================================================================================
def weighted_xp(config, base_xp: int, domain: str, *,
                tracker: Optional[ProgressTracker] = None) -> int:
    """base_xp × progress_weight(domain), rounded to whole XP. Pure of the flag — the flag gate
    lives at the ONE live seam (persona.award_xp), so there is exactly one place the dark switch is
    checked. Non-positive base pays itself (nothing to weight)."""
    if base_xp <= 0:
        return int(base_xp)
    tracker = tracker or ProgressTracker(config)
    return int(round(int(base_xp) * tracker.progress_weight(domain)))


def award_adjudicated_xp(config, persona_dict: dict, base_xp: int, domain: str,
                         reason: str = "") -> int:
    """Convenience for the later glue cutover: award one glue-ADJUDICATED success's XP, routed
    through the learning-progress weighting via persona.award_xp (which holds the flag gate — flag
    off, this degrades to the plain volume award). Returns the new level. Self-report never lands
    here: callers are glue settlement paths, by the same doctrine expectations.CLOSE_REASONS
    enforces."""
    import persona
    return persona.award_xp(persona_dict, int(base_xp), reason, domain=domain, config=config)
