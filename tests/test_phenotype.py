"""Phenotype (phenotype.py) — the deterministic description grammar. Offline unit tests.

Pins (CREATURE_GENETICS.md red gates, description side):
  - describe() is BYTE-DETERMINISTIC: same (genome, stage, expressed) → the identical string,
    across calls and across separately-loaded copies of the same germline;
  - BOTH identity anchors appear VERBATIM at EVERY stage (egg included — the shell traces them);
  - the register is creature-forge's proven concept style ("cute baby-schema …, plain background");
  - the stage features mirror creature_gen's appendage schedule EXACTLY: hatchling bare →
    juvenile ears+tail → adult +limbs → guardian +metamorphosis;
  - expressed-allele marks fold in deterministically (pattern override, palette shift, build
    delta re-clamped to the renderer's band, mark phrases appended);
  - the word tables cover creature_gen's families exactly (a new renderer family without a word
    is a failing test, not a blank prompt);
  - write_phenotype: the atomic {v, seed, morph, stage, prompt, anchors, expressed, ts} artifact,
    best-effort (an unwritable workspace returns False, never raises);
  - body_words: the stanza-templating lexicon accessor, FAIL-OPEN to the declared DEFAULT_MORPH
    row when no genome exists — never raises, never births, never creates a file.

No services / tick loop / GPU — temp workspaces only (eiDOS is live on this machine).
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest

import creature_gen
from config import Config
from genome import ALLELE_SLOTS, GENE_LOADINGS, GENOME_FILENAME, LATENTS, MORPHS, Genome
from phenotype import (BUILD_WORDS, DEFAULT_MORPH, EYE_WORDS, PATTERN_WORDS, PHENOTYPE_FILENAME,
                       body_words, describe, write_phenotype)

_ROOT = Path(__file__).parent.parent


# --- helpers -------------------------------------------------------------------------------------

def _cfg(tmp_path, *, mkdir: bool = True) -> Config:
    cfg = Config()
    cfg.workspace_dir = str(tmp_path / "workspace")
    cfg.mock_mode = True
    if mkdir:
        cfg.workspace.mkdir(parents=True, exist_ok=True)
    return cfg


_ANCHOR_1 = "a cream-colored bib at its throat"
_ANCHOR_2 = "a freckled nose"
_PHENO = {"palette": {"accent": "#ffb000", "word": "amber"}, "pattern": "band",
          "eye_family": "glow", "build": 0,
          "anchors": [{"id": "cream_bib", "phrase": _ANCHOR_1},
                      {"id": "freckled_nose", "phrase": _ANCHOR_2}]}


def _write_v2(cfg, *, seed: int = 7, morph: str = "otter", alleles=None) -> None:
    slots = {sd["slot"]: {"variant": None, "expressed_at_stage": None} for sd in ALLELE_SLOTS}
    for k, v in (alleles or {}).items():
        slots[k] = dict(v)
    doc = {"v": 2, "seed": seed, "born_ts": 1.0,
           "latents": {n: 0.0 for n in LATENTS},
           "genes": {name: 1.0 for name in GENE_LOADINGS},
           "stamp_baselines": {"initiative": 0.5, "persistence": 0.5, "caution": 0.5},
           "morph": morph, "phenotype": json.loads(json.dumps(_PHENO)),
           "alleles": slots, "stage_history": []}
    cfg.workspace.mkdir(parents=True, exist_ok=True)
    (cfg.workspace / GENOME_FILENAME).write_text(json.dumps(doc), encoding="utf-8")


# =================================================================================================
class TestDescribe:
    def test_byte_deterministic_across_calls_and_copies(self, tmp_path):
        ca, cb = _cfg(tmp_path / "a"), _cfg(tmp_path / "b")
        _write_v2(ca), _write_v2(cb)
        ga, gb = Genome(ca), Genome(cb)
        for stage in creature_gen.STAGES:
            assert describe(ga, stage) == describe(ga, stage) == describe(gb, stage)

    def test_anchors_verbatim_at_every_stage(self, tmp_path):
        """The identity thread: the hatchling is recognizably the same being as its guardian."""
        cfg = _cfg(tmp_path)
        _write_v2(cfg)
        g = Genome(cfg)
        for stage in creature_gen.STAGES:
            prompt = describe(g, stage)
            assert _ANCHOR_1 in prompt and _ANCHOR_2 in prompt, f"anchors missing at {stage}"

    def test_concept_register(self, tmp_path):
        cfg = _cfg(tmp_path)
        _write_v2(cfg)
        g = Genome(cfg)
        for stage in creature_gen.STAGES:
            prompt = describe(g, stage)
            assert prompt.startswith("cute baby-schema ")
            assert prompt.endswith(", plain background")

    def test_stage_features_mirror_creature_gen_schedule(self, tmp_path):
        """hatchling bare → juvenile ears+tail → adult +limbs → guardian +metamorphosis — the
        exact appendage schedule creature_gen._appendages draws."""
        cfg = _cfg(tmp_path)
        _write_v2(cfg, morph="otter")
        g = Genome(cfg)
        ph = MORPHS["otter"]["phrases"]
        hatchling = describe(g, "hatchling")
        juvenile = describe(g, "juvenile")
        adult = describe(g, "adult")
        guardian = describe(g, "guardian")
        for feat in (ph["ears"], ph["tail"], ph["limbs"], ph["metamorphosis"]):
            assert feat not in hatchling                   # bare
        assert ph["ears"] in juvenile and ph["tail"] in juvenile
        assert ph["limbs"] not in juvenile and ph["metamorphosis"] not in juvenile
        assert ph["ears"] in adult and ph["tail"] in adult and ph["limbs"] in adult
        assert ph["metamorphosis"] not in adult
        for feat in (ph["ears"], ph["tail"], ph["limbs"], ph["metamorphosis"]):
            assert feat in guardian                        # the metamorphosis payoff
        # the egg is morph-neutral by design: the surprise is kept until hatch
        assert ph["creature"] not in describe(g, "egg")

    def test_palette_and_pattern_words_present(self, tmp_path):
        cfg = _cfg(tmp_path)
        _write_v2(cfg)
        g = Genome(cfg)
        for stage in creature_gen.STAGES:
            prompt = describe(g, stage)
            assert "amber" in prompt
            assert "banded" in prompt                      # PATTERN_WORDS["band"]

    def test_unknown_stage_raises(self, tmp_path):
        cfg = _cfg(tmp_path)
        _write_v2(cfg)
        g = Genome(cfg)
        with pytest.raises(ValueError):
            describe(g, "elder")

    def test_expressed_marks_fold_in_deterministically(self, tmp_path):
        """Pattern override replaces the pattern word, palette shift brightens the color word,
        build delta re-clamps into the renderer band, mark phrases append — all in declared
        slot order, all byte-stable."""
        cfg = _cfg(tmp_path)
        _write_v2(cfg, alleles={
            "weathering": {"variant": "storm_marked", "expressed_at_stage": "juvenile"},
            "luster": {"variant": "operator_bright", "expressed_at_stage": "adult"},
            "frame": {"variant": "grind_dense", "expressed_at_stage": "adult"},
        })
        g = Genome(cfg)
        prompt = describe(g, "adult")
        assert "storm-marked" in prompt and "banded" not in prompt   # pattern override
        assert "bright amber" in prompt                              # palette shift
        assert "slight, narrow build" in prompt                      # build 0 − 1 → −1
        assert "weathered storm-marks across its back" in prompt     # mark phrases
        assert "a dense, low-set frame" in prompt
        assert _ANCHOR_1 in prompt and _ANCHOR_2 in prompt           # anchors survive the marks
        assert prompt == describe(g, "adult")

    def test_word_tables_cover_renderer_families(self):
        assert set(PATTERN_WORDS) == set(creature_gen.EGG_PATTERN_NAMES)
        assert set(EYE_WORDS) == set(creature_gen.EYE_FAMILY_NAMES)
        assert set(BUILD_WORDS) == {-1, 0, 1}


# =================================================================================================
class TestWritePhenotype:
    def test_writes_the_stage_artifact(self, tmp_path):
        cfg = _cfg(tmp_path)
        _write_v2(cfg, alleles={"weathering": {"variant": "storm_marked",
                                               "expressed_at_stage": "juvenile"}})
        g = Genome(cfg)
        assert write_phenotype(cfg, g, "juvenile") is True
        doc = json.loads((cfg.workspace / PHENOTYPE_FILENAME).read_text(encoding="utf-8"))
        assert set(doc) == {"v", "seed", "morph", "stage", "prompt", "anchors", "expressed", "ts"}
        assert doc["seed"] == g.seed and doc["morph"] == "otter" and doc["stage"] == "juvenile"
        assert doc["prompt"] == describe(g, "juvenile")    # the artifact IS the grammar's output
        assert doc["anchors"] == [_ANCHOR_1, _ANCHOR_2]
        assert doc["expressed"] == ["storm_marked"]
        assert doc["ts"] > 0
        assert not (cfg.workspace / (PHENOTYPE_FILENAME + ".tmp")).exists()
        assert not (cfg.workspace / "phenotype.json.tmp").exists()   # atomic: no droppings

    def test_rewritten_at_each_transition(self, tmp_path):
        cfg = _cfg(tmp_path)
        _write_v2(cfg)
        g = Genome(cfg)
        assert write_phenotype(cfg, g, "hatchling")
        assert write_phenotype(cfg, g, "adult")
        doc = json.loads((cfg.workspace / PHENOTYPE_FILENAME).read_text(encoding="utf-8"))
        assert doc["stage"] == "adult"                     # one artifact, latest stage wins

    def test_best_effort_never_raises(self, tmp_path):
        """An unwritable workspace (here: the path is a FILE) degrades to False, not a crash —
        a broken disk must never brick a metamorphosis."""
        cfg = _cfg(tmp_path, mkdir=False)
        Path(cfg.workspace_dir).parent.mkdir(parents=True, exist_ok=True)
        Path(cfg.workspace_dir).write_text("not a directory", encoding="utf-8")
        g = Genome.__new__(Genome)                         # a bare in-memory genome is enough
        g.config, g.seed, g.morph = cfg, 1, "otter"
        g.phenotype = json.loads(json.dumps(_PHENO))
        g.alleles, g.stage_history = {}, []
        assert write_phenotype(cfg, g, "adult") is False


# =================================================================================================
class TestBodyWords:
    def test_fail_open_to_default_morph_row(self, tmp_path):
        cfg = _cfg(tmp_path, mkdir=False)                  # no workspace, no genome
        words = body_words(cfg)
        assert words == MORPHS[DEFAULT_MORPH]["lexicon"]
        assert not Path(cfg.workspace_dir).exists()        # the accessor never creates anything
        assert body_words(None) == MORPHS[DEFAULT_MORPH]["lexicon"]   # and never raises

    def test_reads_the_creature_morph(self, tmp_path):
        cfg = _cfg(tmp_path)
        _write_v2(cfg, morph="moth")
        assert body_words(cfg) == MORPHS["moth"]["lexicon"]

    def test_returns_a_copy_not_the_table(self, tmp_path):
        cfg = _cfg(tmp_path)
        words = body_words(cfg)
        words["mover"] = "tentacles"
        assert MORPHS[DEFAULT_MORPH]["lexicon"]["mover"] != "tentacles"
