"""Genome v2 (genome.py) — one germline, many expressions. Offline unit tests.

Pins (CREATURE_GENETICS.md red gates, genome side):
  - the FROZEN draw order: latents (v1, draws 1-4) then morph / palette / pattern / eye / build /
    anchors / allele carries appended AFTER — mirrored draw-by-draw so an inserted draw is a
    failing test, not a mutated creature;
  - birth goes through draw_germline (one derivation for birth AND upgrade), persists v: 2 with
    every allele dormant and an empty stage_history;
  - v1→v2 upgrade: a v1 genome.json loads, derives morph/phenotype/alleles from its STORED seed
    (reproducible — two upgrades of the same seed agree, and both agree with a pure derivation),
    preserves stored latents/genes/baselines byte-for-byte, and persists back v: 2;
  - stored-values-win (belt and braces): a v2 file's morph/anchors survive loading verbatim even
    when they differ from what the seed would derive — derivation drift never mutates a creature;
  - renderer coherence: every morph's part families ⊆ creature_gen's name tuples, palette accents
    == creature_gen.ACCENTS, build stays in the renderer's {-1,0,+1};
  - alleles: threshold expression, canalization (expressed is permanent, never re-evaluated, and
    survives stat regression), dormant slots re-adjudicated at later transitions, wild-type never
    expresses, expression is PURE (no file I/O — the caller persists), stage_history records;
  - clamp composition: an expressed gene_mod multiplies INSIDE the declared band and can never
    escape it; only PERCEPTION_GENES are modulatable and there are at most 2 gene-mod slots;
  - the firewall: genome.py/phenotype.py never import the ledger files (and vice versa), no gene
    name references the ledger, and every activation criterion reads persona.* stats data only;
  - ALL_BODY_NOUNS covers every morph's lexicon (the no-hardcoded-body-nouns scan set).

No services / tick loop / GPU — temp workspaces only (eiDOS is live on this machine).
"""
import json
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest

import creature_gen
from config import Config
from genome import (ALL_BODY_NOUNS, ALLELE_SLOTS, ANCHOR_COUNT, BUILD_CHOICES, DEFAULT_MORPH,
                    GENE_LOADINGS, GENOME_FILENAME, GENOME_V, LATENT_BOUND, LATENT_SD, LATENTS,
                    LEXICON_KEYS, MAX_GENE_MOD_SLOTS, MORPH_NAMES, MORPHS, PALETTES,
                    PERCEPTION_GENES, Genome, draw_germline, express_alleles, express_genes,
                    expressed_marks, gene)

_ROOT = Path(__file__).parent.parent


# --- helpers -------------------------------------------------------------------------------------

def _cfg(tmp_path, *, mkdir: bool = True) -> Config:
    cfg = Config()
    cfg.workspace_dir = str(tmp_path / "workspace")
    cfg.mock_mode = True
    if mkdir:
        cfg.workspace.mkdir(parents=True, exist_ok=True)
    return cfg


def _write_v1(cfg, *, seed: int = 42) -> dict:
    """Lay a hand-built v1 genome.json (the pre-morph on-disk shape) to drive the upgrade path."""
    doc = {"v": 1, "seed": seed, "born_ts": 1.0,
           "latents": {n: 0.1 for n in LATENTS},
           "genes": {name: 1.0 for name in GENE_LOADINGS},
           "stamp_baselines": {"initiative": 0.52, "persistence": 0.48, "caution": 0.5}}
    cfg.workspace.mkdir(parents=True, exist_ok=True)
    (cfg.workspace / GENOME_FILENAME).write_text(json.dumps(doc), encoding="utf-8")
    return doc


_PHENO = {"palette": {"accent": "#ffb000", "word": "amber"}, "pattern": "band",
          "eye_family": "glow", "build": 0,
          "anchors": [{"id": "cream_bib", "phrase": "a cream-colored bib at its throat"},
                      {"id": "freckled_nose", "phrase": "a freckled nose"}]}


def _write_v2(cfg, *, seed: int = 7, morph: str = "otter", genes=None, alleles=None) -> dict:
    """Lay a hand-built v2 genome.json so a test can force exact morph/phenotype/allele state."""
    g = {name: 1.0 for name in GENE_LOADINGS}
    g.update(genes or {})
    slots = {sd["slot"]: {"variant": None, "expressed_at_stage": None} for sd in ALLELE_SLOTS}
    for k, v in (alleles or {}).items():
        slots[k] = dict(v)
    doc = {"v": 2, "seed": seed, "born_ts": 1.0,
           "latents": {n: 0.0 for n in LATENTS},
           "genes": g,
           "stamp_baselines": {"initiative": 0.5, "persistence": 0.5, "caution": 0.5},
           "morph": morph,
           "phenotype": json.loads(json.dumps(_PHENO)),
           "alleles": slots, "stage_history": []}
    cfg.workspace.mkdir(parents=True, exist_ok=True)
    (cfg.workspace / GENOME_FILENAME).write_text(json.dumps(doc), encoding="utf-8")
    return doc


def _genome_bytes(cfg) -> bytes:
    return (cfg.workspace / GENOME_FILENAME).read_bytes()


# =================================================================================================
class TestGermlineDraws:
    def test_draw_order_frozen(self):
        """Mirror the documented order draw-by-draw: latents (1-4), morph (5), palette (6),
        pattern (7), eye (8), build (9), anchors (10), allele carries (11+). Inserting or
        reordering ANY draw breaks this test before it can mutate a living creature."""
        seed = 0xBEEF
        rng = random.Random(seed)
        lat = {n: round(max(-LATENT_BOUND, min(LATENT_BOUND, rng.gauss(0.0, LATENT_SD))), 4)
               for n in LATENTS}
        morph = rng.choice(MORPH_NAMES)
        accent, word = rng.choice(PALETTES)
        pattern = rng.choice(creature_gen.EGG_PATTERN_NAMES)
        eye = rng.choice(creature_gen.EYE_FAMILY_NAMES)
        build = rng.choice(BUILD_CHOICES)
        anchors = rng.sample(MORPHS[morph]["anchors"], ANCHOR_COUNT)
        carries = {sd["slot"]: rng.choice((None,) + tuple(v["id"] for v in sd["variants"]))
                   for sd in ALLELE_SLOTS}
        # v3 APPEND (drawn LAST): type pick + the 3 new axes, then the whole-vector mean-shift.
        from genome import LATENTS_V3, ALL_LATENTS, PRIMARY_NAMES, PRESETS, BLEND_PRIMARY
        primary = rng.choice(PRIMARY_NAMES)
        secondary = rng.choice([n for n in PRIMARY_NAMES if n != primary])
        for n in LATENTS_V3:
            lat[n] = round(max(-LATENT_BOUND, min(LATENT_BOUND, rng.gauss(0.0, LATENT_SD))), 4)
        p, s = PRESETS[primary], PRESETS[secondary]
        for i, ax in enumerate(ALL_LATENTS):
            mean = BLEND_PRIMARY * p[i] + (1.0 - BLEND_PRIMARY) * s[i]
            lat[ax] = round(max(-LATENT_BOUND, min(LATENT_BOUND, lat[ax] + mean)), 4)
        germ = draw_germline(seed)
        # BODY FROZEN: morph/phenotype/alleles must be byte-identical (the v3 draws come AFTER them, so
        # no existing creature's body can drift) — this is the safety-critical guarantee.
        assert germ["morph"] == morph
        assert germ["phenotype"]["palette"] == {"accent": accent, "word": word}
        assert germ["phenotype"]["pattern"] == pattern
        assert germ["phenotype"]["eye_family"] == eye
        assert germ["phenotype"]["build"] == build
        assert germ["phenotype"]["anchors"] == [dict(a) for a in anchors]
        assert {s2: e["variant"] for s2, e in germ["alleles"].items()} == carries
        assert all(e["expressed_at_stage"] is None for e in germ["alleles"].values())
        # v3 latents + type match the mirrored order
        assert germ["latents"] == lat
        assert germ["type"] == {"primary": primary, "secondary": secondary}

    def test_birth_goes_through_the_germline(self, tmp_path):
        cfg = _cfg(tmp_path)
        g = Genome(cfg)
        germ = draw_germline(g.seed)
        assert g.latents == germ["latents"]                # v1 draws unchanged for the same seed
        assert g.genes == express_genes(g.latents)
        assert g.morph == germ["morph"]
        assert g.phenotype == germ["phenotype"]
        assert g.alleles == germ["alleles"]
        assert g.stage_history == []
        doc = json.loads(_genome_bytes(cfg))
        assert doc["v"] == GENOME_V == 3
        assert doc["morph"] == g.morph and doc["phenotype"] == g.phenotype
        assert all(e["expressed_at_stage"] is None for e in doc["alleles"].values())

    def test_palette_accents_are_the_renderer_accents(self):
        """Seed unity: the dashboard accent and the prompt color are the SAME draw domain."""
        assert {accent for accent, _w in PALETTES} == set(creature_gen.ACCENTS)
        assert all(isinstance(w, str) and w for _a, w in PALETTES)

    def test_build_stays_in_renderer_band(self):
        assert set(BUILD_CHOICES) <= {-1, 0, 1}            # creature_gen canvas math depends on it


# =================================================================================================
class TestMorphTable:
    def test_part_families_subset_of_creature_gen(self):
        """Renderer coherence (red gate 2): no morph may name a part family the ASCII renderer
        cannot draw — the dashboard creature and the 3D creature are the SAME creature."""
        domains = {"ears": creature_gen.EAR_KIND_NAMES, "limbs": creature_gen.LIMB_KIND_NAMES,
                   "tail": creature_gen.TAIL_KIND_NAMES, "body": creature_gen.BODY_FAMILIES}
        for name, row in MORPHS.items():
            assert set(row["parts"]) == set(domains), f"{name}: parts keys incomplete"
            for fam, allowed in row["parts"].items():
                assert allowed, f"{name}.{fam}: empty family list"
                assert set(allowed) <= set(domains[fam]), \
                    f"{name}.{fam}: {set(allowed) - set(domains[fam])} unknown to creature_gen"

    def test_every_row_complete(self):
        for name, row in MORPHS.items():
            assert set(row["lexicon"]) == set(LEXICON_KEYS), f"{name}: lexicon keys drifted"
            assert all(isinstance(v, str) and v for v in row["lexicon"].values())
            assert {"creature", "ears", "tail", "limbs", "metamorphosis"} <= set(row["phrases"])
            assert all(isinstance(v, str) and v for v in row["phrases"].values())
            assert len(row["anchors"]) >= ANCHOR_COUNT, f"{name}: anchor pool too small to draw"
            ids = [a["id"] for a in row["anchors"]]
            assert len(ids) == len(set(ids)), f"{name}: duplicate anchor ids"
            assert all(isinstance(a["phrase"], str) and a["phrase"] for a in row["anchors"])
        assert DEFAULT_MORPH in MORPHS

    def test_all_body_nouns_covers_every_lexicon(self):
        """The no-hardcoded-body-nouns red gate's scan set: distinctive words AND full phrases."""
        for word in ("paws", "claws", "whiskers", "den", "wings", "beak", "nest", "holt",
                     "cocoon", "feelers", "kit", "chick", "pup", "larva"):
            assert word in ALL_BODY_NOUNS
        for phrase in ("webbed paws", "clever hands", "bright eyes", "night eyes", "the current"):
            assert phrase in ALL_BODY_NOUNS
        assert all(n == n.lower() for n in ALL_BODY_NOUNS)
        assert "a" not in ALL_BODY_NOUNS and "the" not in ALL_BODY_NOUNS


# =================================================================================================
class TestV1Upgrade:
    def test_upgrade_persists_v2_derived_from_stored_seed(self, tmp_path):
        cfg = _cfg(tmp_path)
        v1 = _write_v1(cfg, seed=1234)
        g = Genome(cfg)                                    # load-or-birth → must LOAD + upgrade
        doc = json.loads(_genome_bytes(cfg))
        assert doc["v"] == 3 and doc["seed"] == 1234
        # stored v1 state preserved — an upgrade never re-derives a living creature's OLD axes; the 3
        # v3 axes are ADDED as neutral (0.0 latent -> 1.0 gene), fail-open, until a fresh birth.
        assert {k: doc["latents"][k] for k in v1["latents"]} == v1["latents"]
        assert all(doc["latents"][a] == 0.0 for a in ("affiliation", "boldness", "playfulness"))
        assert {k: doc["genes"][k] for k in v1["genes"]} == v1["genes"]
        assert doc["genes"]["operator_pull"] == doc["genes"]["levity"] == doc["genes"]["press_scale"] == 1.0
        assert doc["stamp_baselines"] == v1["stamp_baselines"]
        # the NEW fields derive from the STORED seed (reproducible), all alleles dormant
        germ = draw_germline(1234)
        assert g.morph == doc["morph"] == germ["morph"]
        assert g.phenotype == doc["phenotype"] == germ["phenotype"]
        assert g.alleles == germ["alleles"]
        assert all(e["expressed_at_stage"] is None for e in doc["alleles"].values())
        assert doc["stage_history"] == []

    def test_upgrade_reproducible_across_workspaces(self, tmp_path):
        ca, cb = _cfg(tmp_path / "a"), _cfg(tmp_path / "b")
        _write_v1(ca, seed=99), _write_v1(cb, seed=99)
        ga, gb = Genome(ca), Genome(cb)
        assert ga.morph == gb.morph
        assert ga.phenotype == gb.phenotype
        assert ga.alleles == gb.alleles

    def test_readonly_accessor_upgrades_in_place(self, tmp_path):
        """gene() on a v1 file still answers fail-open-identically AND leaves the file upgraded —
        preservation, not birth (the corrupt-file and no-file contracts are pinned in v1 tests)."""
        cfg = _cfg(tmp_path)
        _write_v1(cfg, seed=5)
        assert gene(cfg, "emotional_stamp") == 1.0         # stored v1 gene value, unchanged
        assert json.loads(_genome_bytes(cfg))["v"] == 3

    def test_v2_stored_values_win_over_derivation(self, tmp_path):
        """Belt and braces: a persisted morph + anchors survive loading VERBATIM even when the
        seed would derive something else. A v2 file upgrades to v3 once; a CURRENT-version file is
        then never rewritten again."""
        cfg = _cfg(tmp_path)
        _write_v2(cfg, seed=7, morph="moth")               # seed 7 need not derive "moth"
        g = Genome(cfg)
        assert g.morph == "moth"
        assert g.phenotype["anchors"] == _PHENO["anchors"]
        assert g.phenotype["palette"] == _PHENO["palette"]
        assert json.loads(_genome_bytes(cfg))["v"] == 3    # v2 upgraded to current
        upgraded = _genome_bytes(cfg)
        Genome(cfg)                                         # reload a now-current file
        assert _genome_bytes(cfg) == upgraded              # no gratuitous rewrite of a current file

    def test_v2_build_reclamped_on_load(self, tmp_path):
        """A hand-edited build outside {-1,0,+1} is re-clamped (creature_gen canvas math) — the
        same discipline as the gene clamps."""
        cfg = _cfg(tmp_path)
        doc = _write_v2(cfg, seed=7)
        doc["phenotype"]["build"] = 5
        (cfg.workspace / GENOME_FILENAME).write_text(json.dumps(doc), encoding="utf-8")
        assert Genome(cfg).phenotype["build"] == 1


# =================================================================================================
class TestAlleles:
    def test_threshold_expression_and_permanence(self, tmp_path):
        cfg = _cfg(tmp_path)
        _write_v2(cfg, alleles={"weathering": {"variant": "storm_marked",
                                               "expressed_at_stage": None}})
        g = Genome(cfg)
        assert express_alleles(g, {"persona": {"total_errors_recovered": 11}}, "juvenile") == []
        assert g.alleles["weathering"]["expressed_at_stage"] is None
        newly = express_alleles(g, {"persona": {"total_errors_recovered": 12,
                                                "total_ticks": 400}}, "juvenile")
        assert newly == [{"slot": "weathering", "variant": "storm_marked", "stage": "juvenile"}]
        assert g.alleles["weathering"]["expressed_at_stage"] == "juvenile"
        # permanent: never re-returned, whatever the later stats say (canalization)
        assert express_alleles(g, {"persona": {"total_errors_recovered": 50}}, "adult") == []
        assert g.alleles["weathering"]["expressed_at_stage"] == "juvenile"

    def test_canalization_survives_stat_regression(self, tmp_path):
        cfg = _cfg(tmp_path)
        _write_v2(cfg, alleles={"sheen": {"variant": "dream_sheened",
                                          "expressed_at_stage": "juvenile"}})
        g = Genome(cfg)
        assert express_alleles(g, {"persona": {"total_compactions": 0}}, "adult") == []
        assert g.alleles["sheen"]["expressed_at_stage"] == "juvenile"   # childhood is forever
        assert [v["id"] for v in expressed_marks(g)] == ["dream_sheened"]

    def test_dormant_slot_readjudicated_at_later_transition(self, tmp_path):
        cfg = _cfg(tmp_path)
        _write_v2(cfg, alleles={"brow": {"variant": "keeper_marked",
                                         "expressed_at_stage": None}})
        g = Genome(cfg)
        assert express_alleles(g, {"persona": {"goals_completed": 0}}, "juvenile") == []
        newly = express_alleles(g, {"persona": {"goals_completed": 3}}, "adult")
        assert [n["variant"] for n in newly] == ["keeper_marked"]
        assert g.alleles["brow"]["expressed_at_stage"] == "adult"

    def test_wild_type_never_expresses(self, tmp_path):
        cfg = _cfg(tmp_path)
        _write_v2(cfg)                                     # every slot carries variant: None
        g = Genome(cfg)
        rich = {"persona": {"total_errors_recovered": 999, "goals_completed": 999,
                            "total_compactions": 999, "longest_streak": 999,
                            "tools_used": {"chat_reply": 999}}}
        assert express_alleles(g, rich, "adult") == []
        assert expressed_marks(g) == []

    def test_expression_is_pure_adjudication_no_io(self, tmp_path):
        """express_alleles reads the stats dict and mutates the object ONLY — the caller persists
        (the single-writer seam that applies the stage change). The file must not move."""
        cfg = _cfg(tmp_path)
        _write_v2(cfg, alleles={"weathering": {"variant": "storm_marked",
                                               "expressed_at_stage": None}})
        g = Genome(cfg)
        before = _genome_bytes(cfg)
        newly = express_alleles(g, {"persona": {"total_errors_recovered": 20}}, "adult")
        assert newly and _genome_bytes(cfg) == before      # pure: caller persists
        assert g.save()
        after = json.loads(_genome_bytes(cfg))
        assert after["alleles"]["weathering"]["expressed_at_stage"] == "adult"
        assert after["stage_history"][-1]["expressed"] == ["storm_marked"]

    def test_stage_history_records_the_transition(self, tmp_path):
        cfg = _cfg(tmp_path)
        _write_v2(cfg, alleles={"weathering": {"variant": "storm_marked",
                                               "expressed_at_stage": None}})
        g = Genome(cfg)
        express_alleles(g, {"persona": {"total_errors_recovered": 12, "total_ticks": 77}},
                        "juvenile")
        assert g.stage_history[-1] == {"stage": "juvenile", "tick": 77,
                                       "expressed": ["storm_marked"]}
        express_alleles(g, {"persona": {"total_ticks": 200}}, "adult")   # nothing new expresses
        assert g.stage_history[-1] == {"stage": "adult", "tick": 200, "expressed": []}

    def test_gene_mod_composes_inside_the_clamp(self, tmp_path):
        """clamp(gene × mult, lo, hi): a modulated gene can NEVER escape its declared band."""
        hi_cfg = _cfg(tmp_path / "hi")
        _write_v2(hi_cfg, genes={"explore_salience": 1.7},
                  alleles={"salience_mod": {"variant": "wide_eyed",
                                            "expressed_at_stage": "adult"}})
        _l, lo, hi = GENE_LOADINGS["explore_salience"]
        assert gene(hi_cfg, "explore_salience") == pytest.approx(hi)    # 1.7×1.15 → clamped 1.8
        mid_cfg = _cfg(tmp_path / "mid")
        _write_v2(mid_cfg, alleles={"salience_mod": {"variant": "wide_eyed",
                                                     "expressed_at_stage": "adult"}})
        assert gene(mid_cfg, "explore_salience") == pytest.approx(1.15)  # inside the band: scales
        dormant_cfg = _cfg(tmp_path / "dorm")
        _write_v2(dormant_cfg, alleles={"salience_mod": {"variant": "wide_eyed",
                                                         "expressed_at_stage": None}})
        assert gene(dormant_cfg, "explore_salience") == 1.0              # dormant: raw gene

    def test_gene_mods_touch_perception_only(self):
        """The allele firewall's teeth: ≤ MAX_GENE_MOD_SLOTS gene slots, every gene_mod key a
        declared PERCEPTION gene, and the damper (wake_budget) unreachable."""
        gene_slots = [sd for sd in ALLELE_SLOTS
                      if any(v.get("gene_mods") for v in sd["variants"])]
        assert len(gene_slots) <= MAX_GENE_MOD_SLOTS == 2
        for sd in ALLELE_SLOTS:
            for v in sd["variants"]:
                for gname in (v.get("gene_mods") or {}):
                    assert gname in PERCEPTION_GENES and gname in GENE_LOADINGS
        assert "wake_budget" not in PERCEPTION_GENES       # adenosine sovereignty holds
        assert "emotional_stamp" not in PERCEPTION_GENES   # feelings-gain is not perception


# =================================================================================================
class TestAlleleFirewall:
    def test_criteria_read_adjudicated_persona_stats_only(self):
        """Activation may ONLY reference already-adjudicated facts — every criterion path roots in
        the persona counters the loop's glue writes. An environmental factor without an existing
        record was dropped, never tracked-for."""
        def paths(req: dict) -> list:
            if "all_of" in req or "any_of" in req:
                return [p for c in (req.get("all_of") or req.get("any_of") or [])
                        for p in paths(c)]
            return [req.get("path", "")]
        for sd in ALLELE_SLOTS:
            for v in sd["variants"]:
                for p in paths(v["requires"]):
                    assert p.split(".")[0] == "persona", \
                        f"{v['id']}: criterion path {p!r} outside the adjudicated stats surface"

    def test_no_ledger_imports_either_direction(self):
        """genome.py/phenotype.py never import the ledger; the ledger never imports them. The one
        sanctioned crossing is quests.Criterion — data in, bool out, no state touched."""
        for fname in ("genome.py", "phenotype.py"):
            src = (_ROOT / fname).read_text(encoding="utf-8")
            for banned in ("import persona", "from persona", "import bets", "from bets",
                           "import level_gates", "from level_gates",
                           "import expectations", "from expectations",
                           "import eidos", "from eidos"):
                assert banned not in src, f"{fname} touches the ledger ({banned!r})"
        assert "from quests import Criterion" in (_ROOT / "genome.py").read_text(encoding="utf-8")
        for fname in ("persona.py", "level_gates.py", "quests.py", "expectations.py", "bets.py"):
            src = (_ROOT / fname).read_text(encoding="utf-8")
            assert "import phenotype" not in src and "from phenotype" not in src, \
                f"{fname} imports phenotype — the firewall is breached"

    def test_no_variant_or_slot_names_the_ledger(self):
        banned = {"xp", "level", "levels", "coin", "coins", "bet", "bets", "quest", "quests"}
        for sd in ALLELE_SLOTS:
            names = {sd["slot"]} | {v["id"] for v in sd["variants"]}
            for n in names:
                hits = banned & set(n.lower().split("_"))
                assert not hits, f"allele name {n!r} references the ledger ({hits})"
