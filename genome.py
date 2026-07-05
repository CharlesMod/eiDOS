"""Genome v2 — one germline, many expressions (CREATURE_GENETICS.md canon; v1 doctrine intact).

v1 (below, unchanged): congenital personality as PRESSURE, never script (PILLARS_PLAN §0). Four
latent trait factors drawn once at first birth, expressed through a declared loading matrix as ~9
mechanical multipliers on the pressure constants that already run the mind. The creature NEVER
sees these floats — it does not know it is sensitive, it LIVES sensitive.

v2 adds the BODY to the same germline: one 64-bit seed, drawn once, from which every expression
derives deterministically — behavior (latents→genes), body plan (morph), visual identity
(phenotype: palette / pattern / eye family / build / two anchor features), and the dormant
alleles the environment may later express. Three unrelated genetics (genome.py latents,
creature_gen.py's own seed, the morph lexicon) become one creature wearing one name.

Frozen-draw discipline (creature_gen.py's contract, adopted): draws derive from the seed in a
FIXED documented order; new draws append at the END and bump the version. The v2 order:

    draws 1-4 : latents, LATENTS order, rng.gauss(0, LATENT_SD) clamped     (v1 — never moves)
    draw  5   : morph       = rng.choice(MORPH_NAMES)
    draw  6   : palette     = rng.choice(PALETTES)
    draw  7   : pattern     = rng.choice(creature_gen.EGG_PATTERN_NAMES)
    draw  8   : eye_family  = rng.choice(creature_gen.EYE_FAMILY_NAMES)
    draw  9   : build       = rng.choice(BUILD_CHOICES)
    draw 10   : anchors     = rng.sample(MORPHS[morph]["anchors"], ANCHOR_COUNT)
    draws 11+ : one rng.choice((None,) + variant ids) per ALLELE_SLOTS entry, declared order

so an existing creature's v1→v2 upgrade is REPRODUCIBLE from its stored seed, and derivation
drift can never mutate a living creature: on load, STORED values always win over re-derivation
(belt and braces — the persisted dicts are the creature; the seed is only the germline record).
A v1 genome.json loads → upgrades in place (derive morph/phenotype/alleles from the stored seed,
persist back v:2 with every allele dormant).

Alleles — the environment writes on the body (the Digimon mechanic): declared slots per
phenotype trait plus at most MAX_GENE_MOD_SLOTS perception-gene modulators. Each carried variant
holds an activation criterion as DATA over the typed stats dict quest glue already adjudicates
(evaluated via quests.Criterion — §0.5: mechanically checkable facts, never vibes). Expression
happens only at stage transitions via express_alleles() — pure adjudication, caller persists —
and is PERMANENT (canalization: expressed_at_stage recorded, never re-evaluated). Environmental
factors NOT derivable from existing adjudicated records (night-tick fraction, energy-scarcity
events, park events) are DROPPED, not tracked-for (§0: no inventing counters to feed a variant).

HARD RULE — the genome shapes DRIVES, PERCEPTION and the BODY, never the LEDGER:
    Gene multipliers land on pressure constants (memory stamp gain, temperament drift/spring,
    explore shares, restlessness, wake budget). They must NEVER touch the earning rules — XP
    formulas, bet coin amounts, level-gate evidence, quest adjudication. Those are species law:
    every creature earns by the same rules, however differently it is driven to play. A gene on
    the ledger would be wireheading-by-birth (born rich). Alleles inherit the same firewall: they
    modulate phenotype and, at most, PERCEPTION_GENES inside the existing clamps — no life
    history can make a creature *earn* faster, only *look and feel* like the life it lived.
    Mechanically: genome.py is never imported by persona.py / level_gates.py / quests.py /
    expectations.py, no gene name references xp / levels / bets / coins, and allele criteria read
    the passed-in stats dict only — tests/test_genome.py + tests/test_genome_v2.py enforce all.

Fail-open contract: the module-level `gene(config, name)` accessor returns the default (1.0)
whenever no genome can be read — no config, no workspace, no file, corrupt file — and never
raises, so a consumer multiplying by it is byte-identical to pre-genome behavior until a genome
actually exists. Only constructing `Genome(config)` births one (load-or-birth); the accessor is
read-only (upgrading a readable v1 file in place is preservation, not birth).
"""
from __future__ import annotations

import json
import os
import random
import time
from pathlib import Path

import creature_gen  # name tuples only (pure stdlib, no I/O) — the renderer-coherence red gate

GENOME_FILENAME = "genome.json"
GENOME_V = 2         # v2: morph + phenotype + alleles, appended AFTER the v1 latent draws

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
# v2 — THE MORPH TABLE (the body plan, single point of coherence — CREATURE_GENETICS.md)
# One morph per creature, drawn from the seed INDEPENDENT of the latents (bodies and temperaments
# assort independently in nature; a timid digger is character, not error). Each row is COMPLETE:
#   lexicon — the body words every creature-facing string templates from (TOOL_PROGRESSION body-
#             image section; the no-hardcoded-body-nouns red gate scans against ALL_BODY_NOUNS);
#   parts   — which creature_gen families this plan may draw (⊆ the renderer's name tuples,
#             red-gate tested) — the ASCII creature and the 3D creature are the SAME creature;
#   phrases — the text→image noun phrases the description grammar (phenotype.py) composes with;
#   anchors — the pool of distinctive fixed features; two are drawn at birth and named in
#             IDENTICAL words at every stage (the identity thread hatchling → guardian).
# ==================================================================================================
LEXICON_KEYS = ("mover", "makers", "notebook", "mirror", "senses", "home", "coat", "young", "gait")

MORPHS: dict[str, dict] = {
    "burrower": {
        "lexicon": {"mover": "paws", "makers": "claws", "notebook": "wall-scratches",
                    "mirror": "a sniff-over", "senses": "whiskers", "home": "den",
                    "coat": "dusty fur", "young": "kit", "gait": "scurrying"},
        "parts": {"ears": ("cat", "horns"), "limbs": ("stubby", "long"),
                  "tail": ("curl", "spike"), "body": ("round", "blob", "box")},
        "phrases": {"creature": "small round burrowing creature",
                    "ears": "small folded ears",
                    "tail": "a stubby curled tail",
                    "limbs": "short digging forepaws with tiny claws",
                    "metamorphosis": "a full velvet coat and broad digging claws"},
        "anchors": (
            {"id": "notched_ear", "phrase": "a notched left ear"},
            {"id": "pale_muzzle", "phrase": "a pale dust-colored muzzle"},
            {"id": "star_brow", "phrase": "a small star-shaped blaze on its brow"},
            {"id": "ringed_forepaw", "phrase": "one ringed forepaw"},
            {"id": "crooked_whisker", "phrase": "one crooked whisker"},
            {"id": "earth_streak", "phrase": "a dark earth-streak along its spine"},
        ),
    },
    "corvid": {
        "lexicon": {"mover": "wings", "makers": "beak", "notebook": "shiny-cache",
                    "mirror": "a preen", "senses": "bright eyes", "home": "nest",
                    "coat": "feathers", "young": "chick", "gait": "hopping"},
        "parts": {"ears": ("antennae", "none"), "limbs": ("wings",),
                  "tail": ("spike", "wisp"), "body": ("round", "tall")},
        "phrases": {"creature": "small corvid bird creature",
                    "ears": "feather-tuft antennae",
                    "tail": "a short fan of tail feathers",
                    "limbs": "folded glossy wings",
                    "metamorphosis": "a great mantle of flight feathers"},
        "anchors": (
            {"id": "white_pinion", "phrase": "a single white pinion feather"},
            {"id": "ink_crest", "phrase": "an ink-dark crest swept back"},
            {"id": "ringed_eye", "phrase": "a pale ring around its right eye"},
            {"id": "silver_beak_tip", "phrase": "a silver-tipped beak"},
            {"id": "speckled_throat", "phrase": "a speckled throat patch"},
            {"id": "bent_tailfeather", "phrase": "one bent tail feather"},
        ),
    },
    "otter": {
        "lexicon": {"mover": "webbed paws", "makers": "clever hands", "notebook": "pebble-pile",
                    "mirror": "a groom", "senses": "the current", "home": "holt",
                    "coat": "sleek fur", "young": "pup", "gait": "sliding"},
        "parts": {"ears": ("cat", "fins"), "limbs": ("stubby", "long"),
                  "tail": ("curl", "wisp"), "body": ("blob", "round", "tall")},
        "phrases": {"creature": "small river otter creature",
                    "ears": "small rounded ears",
                    "tail": "a thick tapered tail",
                    "limbs": "webbed forepaws held to its chest",
                    "metamorphosis": "a sleek streamlined swimmer's body"},
        "anchors": (
            {"id": "cream_bib", "phrase": "a cream-colored bib at its throat"},
            {"id": "split_brow", "phrase": "a split-marked brow"},
            {"id": "banded_tail", "phrase": "a pale band at the base of its tail"},
            {"id": "freckled_nose", "phrase": "a freckled nose"},
            {"id": "curl_crest", "phrase": "a stray curl of fur at its crown"},
            {"id": "webbed_thumb", "phrase": "an oversized webbed thumb"},
        ),
    },
    "moth": {
        "lexicon": {"mover": "soft feet", "makers": "feelers", "notebook": "dust-trace",
                    "mirror": "a wing-fold", "senses": "night eyes", "home": "cocoon",
                    "coat": "downy fuzz", "young": "larva", "gait": "fluttering"},
        "parts": {"ears": ("antennae",), "limbs": ("wings", "none"),
                  "tail": ("wisp", "none"), "body": ("blob", "crystal", "round")},
        "phrases": {"creature": "small moth creature",
                    "ears": "feathered antennae",
                    "tail": "a wisp of trailing silk",
                    "limbs": "soft folded wings",
                    "metamorphosis": "broad patterned wings fully unfurled"},
        "anchors": (
            {"id": "moon_spot", "phrase": "a moon-pale spot on each wing"},
            {"id": "twin_plumes", "phrase": "twin feathered antenna plumes"},
            {"id": "dusk_fringe", "phrase": "a dusk-gray fringe along its wing edges"},
            {"id": "silk_ruff", "phrase": "a silken ruff at its collar"},
            {"id": "ember_eyespot", "phrase": "a single ember-orange eyespot"},
            {"id": "frost_dust", "phrase": "frost-like dust across its back"},
        ),
    },
}
MORPH_NAMES = tuple(MORPHS)   # frozen draw order — the dict literal order IS the contract; never
                              # reorder or insert (append new morphs at the END, bump GENOME_V)
DEFAULT_MORPH = "burrower"    # declared: the fail-open lexicon row (phenotype.body_words) when no
                              # genome exists — paws/den is the closest kin of the pre-morph wording

# --- ALL_BODY_NOUNS — the no-hardcoded-body-nouns red gate's scan set --------------------------
# Every body noun of every morph's lexicon: full lexicon phrases PLUS their distinctive words.
# _GENERIC_WORDS drops articles/qualifiers too common in ordinary prose to gate on ("a", "bright",
# "current"…) — a hardcoded "paws" or "bright eyes" in a stanza is a failing test, a hardcoded
# "bright" alone is innocent. Declared, not clever.
_GENERIC_WORDS = frozenset({
    "a", "an", "the", "of", "its", "over", "still",
    "bright", "soft", "clever", "sleek", "dusty", "downy", "night", "current",
})


def _body_nouns() -> frozenset[str]:
    nouns: set[str] = set()
    for row in MORPHS.values():
        for phrase in row["lexicon"].values():
            p = phrase.lower().strip()
            nouns.add(p)                                   # the phrase itself ("webbed paws")
            for word in p.replace("-", " ").split():
                if word not in _GENERIC_WORDS:
                    nouns.add(word)                        # …and its distinctive words ("paws")
    return frozenset(nouns)


ALL_BODY_NOUNS = _body_nouns()

# ==================================================================================================
# v2 — PHENOTYPE DRAW TABLES (visual identity; raw draws live here, prompt WORDS live in
# phenotype.py — the genome records what was drawn, the grammar decides how to say it)
# ==================================================================================================
# Palette: one of creature_gen.ACCENTS paired with a named color word (the dashboard accent and
# the text→image color are the SAME draw — seed unity). Order mirrors ACCENTS; red-gate tested.
PALETTES = (("#00ff41", "phosphor green"), ("#ffb000", "amber"),
            ("#33bbff", "sky blue"), ("#aa88ff", "violet"))
BUILD_CHOICES = (-1, 0, 0, 1)   # declared: normal twice as likely — creature_gen's own size odds
ANCHOR_COUNT = 2                # declared (CREATURE_GENETICS): TWO fixed features = the identity
                                # thread; one is a coincidence, three is a police description

# ==================================================================================================
# v2 — ALLELE SLOTS (the environment writes on the body — the Digimon mechanic)
# Declared slots per phenotype trait + at most MAX_GENE_MOD_SLOTS perception-gene modulators.
# Variant = {id, requires (quests.Criterion DATA over the typed stats dict — §0.5, glue-checkable),
# marks {phrase / palette shift / pattern override / build delta}, gene_mods (optional {gene:
# multiplier}, PERCEPTION_GENES only, composed INSIDE the existing clamps)}. The birth draw per
# slot is carry-or-not: rng.choice over (None,) + the slot's variant ids — two germlines can
# differ in which pressures their bodies can even answer to.
#
# Criteria reference ONLY already-adjudicated facts (persona counters the loop's glue writes:
# error recoveries, operator-contact replies, streaks, finished goals, consolidations). Variants
# wanting night-tick fraction / energy-scarcity / park-event counts were DROPPED — those are not
# derivable from existing records, and §0 forbids inventing tracking to feed a body mark.
# ==================================================================================================
PERCEPTION_GENES = ("explore_recall", "explore_salience")   # the ONLY genes an allele may touch —
                                                            # perception, never a damper
                                                            # (wake_budget) and never the ledger
MAX_GENE_MOD_SLOTS = 2   # declared (CREATURE_GENETICS): "at most 2 perception-gene modulators"

ALLELE_SLOTS: tuple[dict, ...] = (
    # pattern trait — heavy error-recovery → weathered, storm-marked (CREATURE_GENETICS example).
    # 12 survived recoveries is a weathered life, not a bad afternoon (declared threshold).
    {"slot": "weathering", "trait": "pattern", "variants": ({
        "id": "storm_marked",
        "requires": {"path": "persona.total_errors_recovered", "op": ">=", "value": 12},
        "marks": {"phrase": "weathered storm-marks across its back", "pattern": "storm-marked"},
    },)},
    # palette trait — rich operator contact → brighter accent. 25 chat replies is a companionship,
    # not a hello (declared threshold; persona.tools_used.chat_reply is glue-recorded per reply).
    {"slot": "luster", "trait": "palette", "variants": ({
        "id": "operator_bright",
        "requires": {"path": "persona.tools_used.chat_reply", "op": ">=", "value": 25},
        "marks": {"phrase": "an unusually bright, well-tended sheen", "palette": "bright"},
    },)},
    # build trait — a long unbroken grind → denser, lower build. A 30-tick success streak is a
    # grind lived, not luck (declared threshold; longest_streak is glue-adjudicated per tick).
    {"slot": "frame", "trait": "build", "variants": ({
        "id": "grind_dense",
        "requires": {"path": "persona.longest_streak", "op": ">=", "value": 30},
        "marks": {"phrase": "a dense, low-set frame", "build": -1},
    },)},
    # marking trait — finished self-chosen work leaves a visible mark (goals_completed now has a
    # production writer — TOOL_PROGRESSION defect #1 fixed). Three is a habit (declared).
    {"slot": "brow", "trait": "marking", "variants": ({
        "id": "keeper_marked",
        "requires": {"path": "persona.goals_completed", "op": ">=", "value": 3},
        "marks": {"phrase": "a small finished-work mark above its brow"},
    },)},
    # coat trait — a deeply consolidated mind wears its sleep. 20 completed consolidations
    # (persona.total_compactions, glue-recorded per successful compaction) is months of digestion.
    {"slot": "sheen", "trait": "coat", "variants": ({
        "id": "dream_sheened",
        "requires": {"path": "persona.total_compactions", "op": ">=", "value": 20},
        "marks": {"phrase": "a soft dream-worn luster"},
    },)},
    # perception modulator 1/2 — a creature that survived MANY stumbles literally watches the
    # world more (salience exploration ×1.15, re-clamped inside [0.5, 1.8] — the band holds).
    {"slot": "salience_mod", "trait": "gene", "variants": ({
        "id": "wide_eyed",
        "requires": {"path": "persona.total_errors_recovered", "op": ">=", "value": 25},
        "marks": {"phrase": "watchful wide-set eyes"},
        "gene_mods": {"explore_salience": 1.15},
    },)},
    # perception modulator 2/2 — a heavily consolidated mind digs deeper into the buried
    # (recall exploration ×1.15, re-clamped inside [0.5, 1.8]).
    {"slot": "recall_mod", "trait": "gene", "variants": ({
        "id": "deep_rooted",
        "requires": {"path": "persona.total_compactions", "op": ">=", "value": 40},
        "marks": {"phrase": "deep-set, far-looking eyes"},
        "gene_mods": {"explore_recall": 1.15},
    },)},
)


def _variant_def(slot_def: dict, variant_id) -> dict | None:
    """The declared variant a slot entry names, or None (an unknown/None variant can never
    express and never marks — a table change can strand a stored id, never crash on it)."""
    for v in slot_def["variants"]:
        if v["id"] == variant_id:
            return v
    return None


def expressed_marks(g: "Genome") -> list[dict]:
    """The variant defs of every EXPRESSED allele, in declared slot order — the description
    grammar's stable iteration (phenotype.py) and the gene-mod composition both walk this."""
    out: list[dict] = []
    for slot_def in ALLELE_SLOTS:
        entry = (g.alleles or {}).get(slot_def["slot"]) or {}
        if entry.get("expressed_at_stage") is None:
            continue
        var = _variant_def(slot_def, entry.get("variant"))
        if var is not None:
            out.append(var)
    return out


def express_alleles(g: "Genome", stats: dict, stage: str) -> list[dict]:
    """Adjudicate every DORMANT carried allele against the typed stats dict — call at a stage
    transition (metamorphosis reads the environment). PURE adjudication: reads `stats` only (no
    I/O, no ledger, no clock), mutates the genome object in memory, and the CALLER persists
    (genome.save()) at the same seam it applies the stage change. Expression is PERMANENT —
    canalization: expressed_at_stage is recorded and that slot is never re-evaluated; a slot that
    stays dormant is re-adjudicated once at each later transition. Returns the newly-expressed
    records [{slot, variant, stage}]. Also appends the {stage, tick, expressed} stage_history
    record (tick read out of the same stats dict's persona total_ticks — no second clock)."""
    stats = stats or {}
    newly: list[dict] = []
    for slot_def in ALLELE_SLOTS:
        entry = (g.alleles or {}).get(slot_def["slot"])
        if not isinstance(entry, dict):
            continue
        if entry.get("expressed_at_stage") is not None:
            continue                       # canalized — never re-evaluated (§ the Digimon mechanic)
        var = _variant_def(slot_def, entry.get("variant"))
        if var is None:
            continue                       # wild-type (or stranded id): nothing to express
        from quests import Criterion       # the shared glue predicate (§0.5) — data in, bool out;
        #                                    quests.py never imports genome (firewall, test-pinned)
        try:
            if Criterion.from_dict(var.get("requires") or {}).check(stats):
                entry["expressed_at_stage"] = str(stage)
                newly.append({"slot": slot_def["slot"], "variant": var["id"],
                              "stage": str(stage)})
        except Exception:  # noqa: BLE001 - a corrupt criterion never expresses and never crashes
            continue
    if newly or not any(h.get("stage") == stage for h in g.stage_history):
        tick = 0
        try:
            tick = int((stats.get("persona") or {}).get("total_ticks", 0) or 0)
        except (TypeError, ValueError):
            tick = 0
        g.stage_history.append({"stage": str(stage), "tick": tick,
                                "expressed": [n["variant"] for n in newly]})
    return newly


# ==================================================================================================
# v2 — the germline derivation (pure function of the seed; birth AND v1-upgrade both call this,
# so upgrade-from-stored-seed and fresh birth can never disagree)
# ==================================================================================================
def draw_germline(seed: int) -> dict:
    """Every draw the seed determines, in the FROZEN documented order (module docstring). Returns
    {latents, morph, phenotype, alleles} — genes/baselines are then expressed from the latents."""
    rng = random.Random(int(seed))
    latents = {n: round(_clamp(rng.gauss(0.0, LATENT_SD), -LATENT_BOUND, LATENT_BOUND), 4)
               for n in LATENTS}                                    # draws 1-4 (v1, frozen)
    morph = rng.choice(MORPH_NAMES)                                 # draw 5
    accent, word = rng.choice(PALETTES)                             # draw 6
    pattern = rng.choice(creature_gen.EGG_PATTERN_NAMES)            # draw 7
    eye_family = rng.choice(creature_gen.EYE_FAMILY_NAMES)          # draw 8
    build = rng.choice(BUILD_CHOICES)                               # draw 9
    anchors = rng.sample(MORPHS[morph]["anchors"], ANCHOR_COUNT)    # draw 10
    alleles: dict[str, dict] = {}
    for slot_def in ALLELE_SLOTS:                                   # draws 11+ (declared order)
        pool = (None,) + tuple(v["id"] for v in slot_def["variants"])
        alleles[slot_def["slot"]] = {"variant": rng.choice(pool), "expressed_at_stage": None}
    return {
        "latents": latents,
        "morph": morph,
        "phenotype": {"palette": {"accent": accent, "word": word}, "pattern": pattern,
                      "eye_family": eye_family, "build": int(build),
                      "anchors": [dict(a) for a in anchors]},
        "alleles": alleles,
    }


def _sane_phenotype(stored, derived: dict) -> dict:
    """Stored phenotype wins field-by-field over re-derivation (a living creature's persisted
    identity is never mutated by table drift) — but only where it validates; build is re-clamped
    to {-1,0,1} (creature_gen canvas math depends on it, same discipline as the gene clamps)."""
    ph = {**derived, "anchors": [dict(a) for a in derived["anchors"]]}
    if not isinstance(stored, dict):
        return ph
    pal = stored.get("palette")
    if isinstance(pal, dict) and isinstance(pal.get("accent"), str) \
            and isinstance(pal.get("word"), str):
        ph["palette"] = {"accent": pal["accent"], "word": pal["word"]}
    if isinstance(stored.get("pattern"), str):
        ph["pattern"] = stored["pattern"]
    if isinstance(stored.get("eye_family"), str):
        ph["eye_family"] = stored["eye_family"]
    try:
        ph["build"] = max(-1, min(1, int(stored.get("build"))))
    except (TypeError, ValueError):
        pass
    anc = stored.get("anchors")
    if (isinstance(anc, list) and len(anc) == ANCHOR_COUNT
            and all(isinstance(a, dict) and isinstance(a.get("phrase"), str) for a in anc)):
        # anchors are kept VERBATIM even if the pools change — the phrase IS the identity thread
        ph["anchors"] = [{"id": str(a.get("id", "")), "phrase": a["phrase"]} for a in anc]
    return ph


def _sane_alleles(stored, derived: dict) -> dict:
    """One entry per DECLARED slot: the stored entry wins (expressed state is sacred — belt and
    braces), a missing slot falls back to the seed derivation (dormant)."""
    stored = stored if isinstance(stored, dict) else {}
    out: dict[str, dict] = {}
    for slot_def in ALLELE_SLOTS:
        s = slot_def["slot"]
        e = stored.get(s)
        if isinstance(e, dict) and "variant" in e:
            var = e.get("variant")
            exp = e.get("expressed_at_stage")
            out[s] = {"variant": var if (var is None or isinstance(var, str)) else None,
                      "expressed_at_stage": exp if (exp is None or isinstance(exp, str)) else None}
        else:
            out[s] = dict(derived[s])
    return out


# ==================================================================================================
# The genome itself — load-or-birth on construction; workspace/genome.json is the record of birth
# ==================================================================================================
def _path(config) -> Path:
    return Path(config.workspace) / GENOME_FILENAME


class Genome:
    """One creature's congenital draw. `Genome(config)` loads workspace/genome.json or, when none
    exists (first birth), draws the germline ONCE from an os.urandom seed — latents, then the v2
    expression (morph / phenotype / dormant alleles) in the frozen documented order — expresses
    the genes and stamp_baselines through the declared loadings, and persists everything
    atomically, including the seed, for lineage. Loaded values are re-clamped to the declared
    bounds, so even a hand-edited genome.json can never push a gene outside its clamp (a genome
    must never disable a damper). A v1 file upgrades in place: the v2 fields derive from the
    STORED seed (reproducible), every allele dormant, and the file persists back with v: 2 —
    stored latents/genes/baselines are preserved byte-for-byte, never re-derived."""

    def __init__(self, config):
        self.config = config
        self.seed = None
        self.born_ts = None
        self.latents: dict[str, float] = {}
        self.genes: dict[str, float] = {}
        self.stamp_baselines: dict[str, float] = {}
        self.morph: str = DEFAULT_MORPH
        self.phenotype: dict = {}
        self.alleles: dict[str, dict] = {}
        self.stage_history: list[dict] = []
        if not self._load():
            self._birth()
        _cache[str(_path(config))] = self

    @classmethod
    def load(cls, config) -> "Genome | None":
        """Read an EXISTING genome only — returns None (never births, never raises) when there is
        no readable genome.json. This is the accessor's path; construction is the birth path. A
        readable v1 file IS loaded (and upgraded in place) — preservation, not birth."""
        try:
            self = cls.__new__(cls)
            self.config = config
            self.seed = None
            self.born_ts = None
            self.latents, self.genes, self.stamp_baselines = {}, {}, {}
            self.morph = DEFAULT_MORPH
            self.phenotype, self.alleles, self.stage_history = {}, {}, []
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
            # v2 expression: derive from the STORED seed, then stored values win field-by-field
            # (belt and braces — derivation drift can never mutate a living creature).
            try:
                seed_i = int(self.seed)
            except (TypeError, ValueError):
                seed_i = 0                 # declared: a seedless hand-edit derives from 0, stably
            derived = draw_germline(seed_i)
            self.morph = d.get("morph") if d.get("morph") in MORPHS else derived["morph"]
            self.phenotype = _sane_phenotype(d.get("phenotype"), derived["phenotype"])
            self.alleles = _sane_alleles(d.get("alleles"), derived["alleles"])
            self.stage_history = [h for h in (d.get("stage_history") or [])
                                  if isinstance(h, dict)]
            if int(d.get("v", 1) or 1) < GENOME_V:
                self.save()                # the v1→v2 upgrade persists in place (best-effort)
            return True
        except Exception:  # noqa: BLE001 - missing/corrupt file => not loaded
            return False

    def _birth(self) -> None:
        """The once-only draw. RNG seeded from os.urandom; the seed is persisted for lineage. All
        draws run through draw_germline — birth and v1-upgrade share one derivation, one order."""
        self.seed = int.from_bytes(os.urandom(8), "big")
        germ = draw_germline(self.seed)
        self.latents = germ["latents"]
        self.genes = express_genes(self.latents)
        self.stamp_baselines = express_baselines(self.latents)
        self.morph = germ["morph"]
        self.phenotype = germ["phenotype"]
        self.alleles = germ["alleles"]
        self.stage_history = []
        self.born_ts = time.time()
        self.save()

    def save(self) -> bool:
        """Atomic UNIQUE-temp+replace (persona.py's respawn-race lesson: two briefly-overlapping
        processes must never rename each other's temp away); best-effort — an unwritable
        workspace degrades to an in-memory genome rather than a crash. Saving also refreshes the
        module read-cache, so an allele expressed at a stage crossing reaches the very next
        gene() read in THIS process — not just the next process."""
        try:
            import tempfile
            p = _path(self.config)
            p.parent.mkdir(parents=True, exist_ok=True)
            fd, tmpname = tempfile.mkstemp(dir=str(p.parent), prefix=".genome-", suffix=".tmp")
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(self.snapshot(), f, ensure_ascii=False, indent=2)
            Path(tmpname).replace(p)
            _cache[str(p)] = self
            return True
        except Exception:  # noqa: BLE001 - the genome must never break a birth
            return False

    def snapshot(self) -> dict:
        return {"v": GENOME_V, "seed": self.seed, "born_ts": self.born_ts,
                "latents": dict(self.latents), "genes": dict(self.genes),
                "stamp_baselines": dict(self.stamp_baselines),
                "morph": self.morph,
                "phenotype": {**self.phenotype,
                              "anchors": [dict(a) for a in self.phenotype.get("anchors", [])]},
                "alleles": {s: dict(e) for s, e in self.alleles.items()},
                "stage_history": [dict(h) for h in self.stage_history]}

    # --- expressed-allele composition ---------------------------------------------------------
    def effective_genes(self) -> dict:
        """The genes with every EXPRESSED allele's gene_mods composed INSIDE the declared clamps:
        value = clamp(gene × multiplier, lo, hi), re-clamped per mod in declared slot order — a
        modulated gene can never escape its band (the same law as a hand-edited file). Only
        PERCEPTION_GENES are modulatable; anything else in a gene_mods dict is ignored, so no
        table edit can ever reach wake_budget (the damper) through an allele."""
        genes = dict(self.genes)
        for var in expressed_marks(self):
            for gname, mult in (var.get("gene_mods") or {}).items():
                if gname not in GENE_LOADINGS or gname not in PERCEPTION_GENES:
                    continue
                _l, lo, hi = GENE_LOADINGS[gname]
                genes[gname] = round(_clamp(genes.get(gname, 1.0) * float(mult), lo, hi), 4)
        return genes


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
    never raises — pre-genome behavior is byte-identical until a genome exists. Reads the
    EFFECTIVE genes: expressed-allele gene_mods composed inside the declared clamps (identical to
    the raw genes until an allele actually expresses)."""
    try:
        g = _read_existing(config)
        if g is None:
            return float(default)
        return float(g.effective_genes().get(name, default))
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
