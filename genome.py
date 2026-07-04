"""Genome v1 — congenital personality as PRESSURE, never script (PILLARS_PLAN §0 doctrine).

A captivating persona cannot be written down ("witty = 0.8" in a prompt is scripting). What makes
two creatures with identical code become different beings is coherent, CONSEQUENTIAL differences in
how each one FEELS the same world. So the genome is not a personality sheet: it is four latent
trait factors, drawn once at first birth, expressed through a declared loading matrix as ~9
mechanical multipliers on the pressure constants that already run the mind. Coherence comes from
the factor structure (one latent moves several knobs in a believable direction); distinctiveness
compounds because the economies feed back (a creature whose recall surfaces more exploratory
memories has different experiences, which stamp different engrams, which shape different recalls…).

The four latents — each drawn ~N(0, LATENT_SD), clamped to ±LATENT_BOUND, from an os.urandom seed
that is SAVED for lineage (a descendant / rebirth can reproduce the exact draw):

    sensitivity — how hard the world hits: feelings burn memories deeper, affect lingers,
                  temperament is more impressionable, a touch more cautious.
    openness    — the pull of the new: wider exploration in recall and attention, restless,
                  slightly more initiative and slightly less caution.
    tenacity    — grip on what it's doing: steadier springs, longer grinds before the gate
                  rotates it, less flitting between domains.
    tempo       — metabolic rhythm: a (slightly) longer or shorter natural wake budget and a
                  touch more initiative. TIGHT by design — adenosine stays sovereign.

HARD RULE — the genome shapes DRIVES and PERCEPTION, never the LEDGER:
    Gene multipliers land on pressure constants (memory stamp gain, temperament drift/spring,
    explore shares, restlessness, wake budget). They must NEVER touch the earning rules — XP
    formulas, bet coin amounts, level-gate evidence, quest adjudication. Those are species law:
    every creature earns by the same rules, however differently it is driven to play. A gene on
    the ledger would be wireheading-by-birth (born rich). Mechanically: genome.py is never
    imported by persona.py / level_gates.py / quests.py / expectations.py, and no gene name
    references xp / levels / bets / coins — tests/test_genome.py enforces both.

The creature NEVER sees these floats: there is no context render of the genome — it does not know
it is sensitive, it LIVES sensitive. Charlie reads workspace/genome.json (or a future dashboard
panel) from the outside.

Fail-open contract: the module-level `gene(config, name)` accessor returns the default (1.0)
whenever no genome can be read — no config, no workspace, no file, corrupt file — and never
raises, so a consumer multiplying by it is byte-identical to pre-genome behavior until a genome
actually exists. Only constructing `Genome(config)` births one (load-or-birth); the accessor is
read-only.
"""
from __future__ import annotations

import json
import os
import random
import time
from pathlib import Path

GENOME_FILENAME = "genome.json"

LATENTS = ("sensitivity", "openness", "tenacity", "tempo")
LATENT_SD = 0.6      # declared: birth draw ~ N(0, 0.6) — most creatures are mild, strong trait tails are rare
LATENT_BOUND = 1.5   # declared: truncation at 2.5 SD — no latent can be born a monster (keeps every
                     # gene's pre-clamp expression inside a sane band before the hard clamps even apply)

# ==================================================================================================
# The loading matrix — gene value = clamp(1.0 + Σ latent × loading, lo, hi)
# Each row: gene_name -> ({latent: loading, …}, lo, hi). Every loading is a declared design choice.
# ==================================================================================================
GENE_LOADINGS: dict[str, tuple[dict[str, float], float, float]] = {
    # How strongly feelings burn memories: multiplies the emotional-stamp GAIN in the bet ledger's
    # credit math (bets.emotional_multiplier) — a sensitive creature's high-arousal moments scar
    # and shine deeper in both directions. sensitivity×0.30 → full-tail range ≈ [0.55, 1.45] pre-clamp.
    "emotional_stamp": ({"sensitivity": 0.30}, 0.6, 1.6),
    # Multiplier on temperament SPRING_STEP (the pull back toward the congenital baseline):
    # sensitive = feelings LINGER (weaker spring, −0.25), tenacious = steadier (a slightly firmer
    # spring, +0.10 — grip shows up as emotional stability too, not just task stability).
    "spring_return": ({"sensitivity": -0.25, "tenacity": 0.10}, 0.5, 1.6),
    # Multiplier on temperament STEP (how far one tick's experience drags a setpoint):
    # impressionable (sensitive) vs stubborn. sensitivity×0.25.
    "drift_rate": ({"sensitivity": 0.25}, 0.6, 1.5),
    # Multiplier on config.pillars_recall_explore_ratio (memory_manager's anti-Matthew exploration
    # seat): an open creature's recall keeps digging up the buried and the half-forgotten. openness×0.35.
    "explore_recall": ({"openness": 0.35}, 0.5, 1.8),
    # Multiplier on the salience gate's EXPLORATION_SHARE (attention's sampled slots): an open
    # creature literally NOTICES more of the low-bias world. openness×0.30.
    "explore_salience": ({"openness": 0.30}, 0.5, 1.8),
    # Multiplier on learning_progress.restlessness_signal (the curiosity organ's per-domain "move
    # on" pressure): openness raises it (dilettante), tenacity lowers it (deep-driller). The shaped
    # signal is re-clamped to [0,1] at the consumer so the gene can never break curiosity's contract.
    "restlessness": ({"openness": 0.30, "tenacity": -0.25}, 0.6, 1.6),
    # Multiplier on temperament.park_threshold's persistence effect (the objectives gate's teeth):
    # a tenacious creature grinds longer before the gate rotates it off a hard objective. tenacity×0.30.
    "grip": ({"tenacity": 0.30}, 0.7, 1.4),
    # Multiplier on the adenosine wake ceiling (config.pillars_max_wake_hours). TIGHT clamp
    # [0.9, 1.1] BY DESIGN: adenosine is a damper against the insomnia death-spiral, and a genome
    # must never disable a damper — tempo flavors the rhythm (±10%), sovereignty stays with sleep.
    "wake_budget": ({"tempo": 0.10}, 0.9, 1.1),
}

# ==================================================================================================
# stamp_baselines — the temperament setpoints become latent-derived at birth (the 9th "gene").
# baseline = clamp(0.5 + Σ latent × loading, BASELINE_LO, BASELINE_HI). The clamp band is chosen
# to sit strictly inside every temperament.disposition() threshold (wary/eager/etc. need ≥0.66 or
# ≤0.34), so NO newborn starts pre-labeled by disposition() — nature biases where life pulls each
# axis back to, it never hands out a personality badge on day one.
# ==================================================================================================
BASELINE_NEUTRAL = 0.5
BASELINE_LOADINGS: dict[str, dict[str, float]] = {
    # openness pushes toward acting on the new (+0.06); tempo adds a little forward lean (+0.04).
    "initiative": {"openness": 0.06, "tempo": 0.04},
    # tenacity IS the persistence setpoint's congenital tilt (+0.08).
    "persistence": {"tenacity": 0.08},
    # a sensitive creature hedges a little more (+0.06); an open one a little less (−0.03).
    "caution": {"sensitivity": 0.06, "openness": -0.03},
}
BASELINE_LO, BASELINE_HI = 0.38, 0.62   # declared: strictly inside the disposition() bands (0.34/0.66)


def _clamp(x, lo, hi) -> float:
    return max(float(lo), min(float(hi), float(x)))


# ==================================================================================================
# Expression — pure functions of the latents (module-level so tests can force latents directly)
# ==================================================================================================
def express_genes(latents: dict) -> dict:
    """The loading matrix applied: gene = clamp(1.0 + Σ latent×loading, lo, hi) per GENE_LOADINGS."""
    genes = {}
    for name, (loadings, lo, hi) in GENE_LOADINGS.items():
        v = 1.0 + sum(float(latents.get(l, 0.0)) * w for l, w in loadings.items())
        genes[name] = round(_clamp(v, lo, hi), 4)
    return genes


def express_baselines(latents: dict) -> dict:
    """The congenital temperament setpoints: 0.5 + Σ latent×loading, clamped to [0.38, 0.62]."""
    out = {}
    for ax, loadings in BASELINE_LOADINGS.items():
        v = BASELINE_NEUTRAL + sum(float(latents.get(l, 0.0)) * w for l, w in loadings.items())
        out[ax] = round(_clamp(v, BASELINE_LO, BASELINE_HI), 4)
    return out


# ==================================================================================================
# The genome itself — load-or-birth on construction; workspace/genome.json is the record of birth
# ==================================================================================================
def _path(config) -> Path:
    return Path(config.workspace) / GENOME_FILENAME


class Genome:
    """One creature's congenital draw. `Genome(config)` loads workspace/genome.json or, when none
    exists (first birth), draws the latents ONCE from an os.urandom seed, expresses the genes and
    stamp_baselines through the declared loadings, and persists everything atomically — including
    the seed, for lineage. Loaded values are re-clamped to the declared bounds, so even a
    hand-edited genome.json can never push a gene outside its clamp (a genome must never disable a
    damper)."""

    def __init__(self, config):
        self.config = config
        self.seed = None
        self.born_ts = None
        self.latents: dict[str, float] = {}
        self.genes: dict[str, float] = {}
        self.stamp_baselines: dict[str, float] = {}
        if not self._load():
            self._birth()
        _cache[str(_path(config))] = self

    @classmethod
    def load(cls, config) -> "Genome | None":
        """Read an EXISTING genome only — returns None (never births, never raises) when there is
        no readable genome.json. This is the accessor's path; construction is the birth path."""
        try:
            self = cls.__new__(cls)
            self.config = config
            self.seed = None
            self.born_ts = None
            self.latents, self.genes, self.stamp_baselines = {}, {}, {}
            return self if self._load() else None
        except Exception:  # noqa: BLE001 - fail-open by contract
            return None

    # --- persistence ------------------------------------------------------------------------------
    def _load(self) -> bool:
        try:
            d = json.loads(_path(self.config).read_text(encoding="utf-8"))
            lat = d.get("latents") or {}
            self.latents = {n: _clamp(lat.get(n, 0.0), -LATENT_BOUND, LATENT_BOUND) for n in LATENTS}
            g = d.get("genes") or {}
            self.genes = {name: _clamp(g.get(name, 1.0), lo, hi)
                          for name, (_l, lo, hi) in GENE_LOADINGS.items()}
            b = d.get("stamp_baselines") or {}
            self.stamp_baselines = {ax: _clamp(b.get(ax, BASELINE_NEUTRAL), BASELINE_LO, BASELINE_HI)
                                    for ax in BASELINE_LOADINGS}
            self.seed = d.get("seed")
            self.born_ts = d.get("born_ts")
            return True
        except Exception:  # noqa: BLE001 - missing/corrupt file => not loaded
            return False

    def _birth(self) -> None:
        """The once-only draw. RNG seeded from os.urandom; the seed is persisted for lineage."""
        self.seed = int.from_bytes(os.urandom(8), "big")
        rng = random.Random(self.seed)
        # Fixed LATENTS order so the same seed always reproduces the same creature.
        self.latents = {n: round(_clamp(rng.gauss(0.0, LATENT_SD), -LATENT_BOUND, LATENT_BOUND), 4)
                        for n in LATENTS}
        self.genes = express_genes(self.latents)
        self.stamp_baselines = express_baselines(self.latents)
        self.born_ts = time.time()
        self.save()

    def save(self) -> bool:
        """Atomic temp+replace (house convention); best-effort — an unwritable workspace degrades
        to an in-memory genome rather than a crash."""
        try:
            p = _path(self.config)
            p.parent.mkdir(parents=True, exist_ok=True)
            tmp = p.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(self.snapshot(), ensure_ascii=False, indent=2),
                           encoding="utf-8")
            tmp.replace(p)
            return True
        except Exception:  # noqa: BLE001 - the genome must never break a birth
            return False

    def snapshot(self) -> dict:
        return {"v": 1, "seed": self.seed, "born_ts": self.born_ts,
                "latents": dict(self.latents), "genes": dict(self.genes),
                "stamp_baselines": dict(self.stamp_baselines)}


# ==================================================================================================
# The fail-open accessor — what every consumer multiplies by (read-only: it NEVER births)
# ==================================================================================================
_cache: dict[str, Genome] = {}


def _read_existing(config) -> Genome | None:
    """Cached read of an existing genome. None config / no file / unreadable → None. Never births,
    never creates a directory, never raises."""
    if config is None:
        return None
    try:
        p = _path(config)
        key = str(p)
        g = _cache.get(key)
        if g is not None:
            return g
        if not p.is_file():
            return None
        g = Genome.load(config)
        if g is not None:
            _cache[key] = g
        return g
    except Exception:  # noqa: BLE001 - fail-open by contract
        return None


def gene(config, name: str, default: float = 1.0) -> float:
    """The multiplier consumers apply where a pressure constant is READ. FAIL-OPEN: with no genome
    readable (no config / no workspace / no file / corrupt file) it returns `default` (1.0) and
    never raises — pre-genome behavior is byte-identical until a genome exists."""
    try:
        g = _read_existing(config)
        if g is None:
            return float(default)
        return float(g.genes.get(name, default))
    except Exception:  # noqa: BLE001 - fail-open by contract
        return float(default)


def stamp_baselines(config) -> dict | None:
    """The congenital temperament setpoints from an EXISTING genome, or None (fail-open) — the
    temperament birth path falls back to its own uniform draw when this returns None."""
    try:
        g = _read_existing(config)
        return dict(g.stamp_baselines) if g is not None else None
    except Exception:  # noqa: BLE001 - fail-open by contract
        return None
