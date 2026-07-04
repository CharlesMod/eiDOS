"""Phenotype — genetics → prompt → creature-forge (CREATURE_GENETICS.md canon).

The stage description is a DETERMINISTIC DECLARED GRAMMAR, never LLM freeform — recognizability
is the point: the same (genome, stage, expressed) must produce the byte-identical prompt every
time, so the text→image pipeline (creature-forge: prompt → Z-Image-Turbo → 3D bake-off → 32px
digivice sprite) renders the SAME being at every regeneration. The register is creature-forge's
proven concept style ("cute baby-schema …, plain background" — what the 32px digitizer likes).

What the grammar composes, in fixed order (§0.4 — every piece declared):
  - the baby-schema frame and the morph's body-plan noun phrase (genome.MORPHS[...]["phrases"]);
  - the build / palette / pattern / eye words (word tables below; raw draws live in the genome);
  - stage-appropriate features mirroring creature_gen's appendage schedule EXACTLY
    (hatchling bare → juvenile ears+tail → adult limbs → guardian metamorphosis) — the ASCII
    dashboard creature and the rendered creature grow the same organs in the same order;
  - BOTH identity anchors, their fixed phrases VERBATIM at every stage (egg included: the shell
    already traces them) — the thread that makes a hatchling recognizably the same being as its
    guardian;
  - the marks of every EXPRESSED allele, declared slot order (genome.expressed_marks).

Artifact: workspace/phenotype.json — {v, seed, morph, stage, prompt, anchors, expressed, ts} —
rewritten at every stage transition (write_phenotype, atomic tmp+replace) and served outward;
eiDOS owes the description, creature-forge owns everything after.

body_words(config) is the lexicon accessor for stanza templating (TOOL_PROGRESSION body-image
section): FAIL-OPEN to the declared genome.DEFAULT_MORPH row when no genome exists — creature-
facing templates always have a coherent body vocabulary, they never see a KeyError. This module
does no adjudication and touches no ledger (same firewall as genome.py, test-pinned).
"""
from __future__ import annotations

import json
import time
from pathlib import Path

import creature_gen
import genome as genome_mod
from genome import ALL_BODY_NOUNS, DEFAULT_MORPH, MORPHS  # noqa: F401 - re-exported for the
#                                                           integration red gate's scan imports

PHENOTYPE_FILENAME = "phenotype.json"
PHENOTYPE_V = 1   # declared: schema version of workspace/phenotype.json (not the genome's v)

# --- The word tables (declared grammar vocabulary; keys red-gate-tested against creature_gen) ---
PATTERN_WORDS = {"speckle": "speckled", "zigzag": "zigzag-striped",
                 "band": "banded", "swirl": "swirl-marked"}
EYE_WORDS = {"dot": "small round eyes", "ring": "wide ringed eyes",
             "glow": "softly glowing eyes", "slit": "narrow slit eyes",
             "star": "star-bright eyes"}
BUILD_WORDS = {-1: "slight, narrow build", 0: "compact build", 1: "broad, sturdy build"}
FRAME_PREFIX = "cute baby-schema"   # declared: creature-forge's proven concept register opener
FRAME_SUFFIX = "plain background"   # declared: …and its closer (the digitizer's friend)
HATCHLING_FEATURE = "body still small and bare"   # declared: the pre-appendage stage in words —
#                                                   mirrors creature_gen._appendages returning []

# The appendage schedule, mirroring creature_gen._appendages stage-for-stage (renderer coherence):
# which phrase keys from MORPHS[morph]["phrases"] each stage composes, in fixed order.
_STAGE_FEATURES = {
    "hatchling": (),                                      # bare — creature_gen draws none
    "juvenile": ("ears", "tail"),                         # ears + tail arrive
    "adult": ("ears", "tail", "limbs"),                   # limbs arrive
    "guardian": ("ears", "tail", "limbs", "metamorphosis"),  # the visible metamorphosis payoff
}


def _expressed(g) -> list[dict]:
    """Expressed variant defs in declared slot order (delegates to the genome's single walker)."""
    return genome_mod.expressed_marks(g)


def describe(g, stage: str) -> str:
    """The deterministic declared grammar: (genome, stage) → the text→image prompt, byte-identical
    on every call (§ description-determinism red gate). Both anchor phrases appear VERBATIM at
    every stage; expressed-allele marks fold in (palette shift, pattern override, build delta,
    mark phrases) in declared slot order. Raises ValueError on a stage outside creature_gen.STAGES
    — the caller owns stage names; this grammar never guesses."""
    if stage not in creature_gen.STAGES:
        raise ValueError(f"unknown stage: {stage!r} (known: {', '.join(creature_gen.STAGES)})")
    morph_row = MORPHS.get(g.morph) or MORPHS[DEFAULT_MORPH]
    ph = g.phenotype or {}
    palette = ph.get("palette") or {}
    color = str(palette.get("word", ""))
    pattern = str(ph.get("pattern", ""))
    build = int(ph.get("build", 0) or 0)
    anchors = [a.get("phrase", "") for a in (ph.get("anchors") or [])][:2]
    while len(anchors) < 2:
        anchors.append("")                 # a malformed genome still describes; it never crashes

    # Fold the expressed-allele marks (declared slot order — genome.expressed_marks):
    # pattern override replaces the pattern word, palette shift prefixes the color word, build
    # deltas re-clamp into creature_gen's {-1,0,+1} band, phrases append after the anchors.
    mark_phrases: list[str] = []
    for var in _expressed(g):
        marks = var.get("marks") or {}
        if isinstance(marks.get("pattern"), str):
            pattern = marks["pattern"]
        if marks.get("palette") == "bright":
            color = f"bright {color}"
        try:
            build = max(-1, min(1, build + int(marks.get("build", 0) or 0)))
        except (TypeError, ValueError):
            pass
        if isinstance(marks.get("phrase"), str) and marks["phrase"]:
            mark_phrases.append(marks["phrase"])

    pattern_word = PATTERN_WORDS.get(pattern, pattern)

    if stage == "egg":
        # Morph-neutral by design: the egg keeps the surprise (the dashboard egg renders from
        # pattern alone too) — but the identity thread is already traced on the shell.
        parts = [f"{FRAME_PREFIX} creature egg",
                 f"{color} {pattern_word} shell",
                 f"its markings already tracing {anchors[0]} and {anchors[1]}"]
    else:
        eye_word = EYE_WORDS.get(ph.get("eye_family"), f"{ph.get('eye_family', 'plain')} eyes")
        features = ([HATCHLING_FEATURE] if stage == "hatchling"
                    else [morph_row["phrases"][k] for k in _STAGE_FEATURES[stage]])
        parts = [f"{FRAME_PREFIX} {morph_row['phrases']['creature']}",
                 f"{stage} stage",
                 BUILD_WORDS.get(build, BUILD_WORDS[0]),
                 f"{color} {pattern_word} {morph_row['lexicon']['coat']}",
                 eye_word,
                 *features,
                 anchors[0],
                 anchors[1]]
    parts.extend(mark_phrases)
    parts.append(FRAME_SUFFIX)
    return ", ".join(parts)


def write_phenotype(config, g, stage: str) -> bool:
    """Persist the stage's expression artifact: workspace/phenotype.json = {v, seed, morph, stage,
    prompt, anchors, expressed, ts}, atomic tmp+replace (house convention). Called at every stage
    transition by the same seam that applies the stage change; the dashboard serves it outward and
    creature-forge takes over from the prompt. Best-effort bool — an unwritable workspace never
    bricks a metamorphosis."""
    try:
        doc = {
            "v": PHENOTYPE_V,
            "seed": g.seed,
            "morph": g.morph,
            "stage": str(stage),
            "prompt": describe(g, stage),
            "anchors": [a.get("phrase", "") for a in (g.phenotype or {}).get("anchors", [])],
            "expressed": [v["id"] for v in _expressed(g)],
            "ts": time.time(),
        }
        p = Path(config.workspace) / PHENOTYPE_FILENAME
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(p)
        return True
    except Exception:  # noqa: BLE001 - the artifact is derived state; it can always be rewritten
        return False


def body_words(config) -> dict:
    """The lexicon accessor for stanza templating (TOOL_PROGRESSION: every creature-facing string
    that names anatomy is a template filled from THIS row). FAIL-OPEN: no config / no genome /
    unreadable file → the declared DEFAULT_MORPH row — templates always render, and the wording
    stays coherent with what a genome-less creature was already being told. Never raises, never
    births, never creates a file (reads through genome's cached read-only accessor)."""
    try:
        g = genome_mod._read_existing(config)   # the read-only seam — shared cache, never births
        row = MORPHS.get(getattr(g, "morph", None)) if g is not None else None
        row = row if row is not None else MORPHS[DEFAULT_MORPH]
        return dict(row["lexicon"])
    except Exception:  # noqa: BLE001 - fail-open by contract
        return dict(MORPHS[DEFAULT_MORPH]["lexicon"])
