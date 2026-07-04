"""Genome v1 (genome.py) — congenital personality as pressure, never script. Offline unit tests.

Pins:
  - the once-only birth draw: latents within bounds, genes within their declared clamps, persisted
    to workspace/genome.json with the lineage seed, and a second load is IDENTICAL (no re-draw);
  - two births differ, and the loading STRUCTURE holds (forced latents covary coherently: a highly
    sensitive creature stamps feelings deeper AND springs back slower);
  - the fail-open accessor: no workspace / no file / corrupt file → gene() == 1.0, nothing raises,
    nothing gets created or overwritten;
  - the ledger firewall: genome.py is never imported by the ledger files, and no gene name
    references xp / levels / bets / coins (drives and perception only — no wireheading-by-birth);
  - the consumer seams: temperament baselines come from stamp_baselines, restlessness scales (and
    re-clamps), the recall explore ratio scales, grip scales the park threshold, wake_budget stays
    a tight ±10% flavor on adenosine, emotional_stamp scales the feeling gain only;
  - the newborn guarantee: 1000 synthetic draws → disposition() is always "steady".

No services / tick loop / GPU — temp workspaces only (eiDOS is live on this machine).
"""
import json
import random
import sys
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest

from config import Config
from genome import (BASELINE_HI, BASELINE_LO, GENE_LOADINGS, GENOME_FILENAME, LATENT_BOUND,
                    LATENTS, Genome, express_baselines, express_genes, gene, stamp_baselines)

_ROOT = Path(__file__).parent.parent


# --- helpers -------------------------------------------------------------------------------------

def _cfg(tmp_path, *, mkdir: bool = True) -> Config:
    cfg = Config()
    cfg.workspace_dir = str(tmp_path / "workspace")
    cfg.mock_mode = True
    if mkdir:
        cfg.workspace.mkdir(parents=True, exist_ok=True)
    return cfg


def _write_genome(cfg, *, genes=None, baselines=None) -> None:
    """Lay a hand-built genome.json (bypassing birth) so a test can force exact gene values."""
    g = {name: 1.0 for name in GENE_LOADINGS}
    g.update(genes or {})
    b = {"initiative": 0.5, "persistence": 0.5, "caution": 0.5}
    b.update(baselines or {})
    doc = {"v": 1, "seed": 42, "born_ts": 1.0,
           "latents": {n: 0.0 for n in LATENTS}, "genes": g, "stamp_baselines": b}
    cfg.workspace.mkdir(parents=True, exist_ok=True)
    (cfg.workspace / GENOME_FILENAME).write_text(json.dumps(doc), encoding="utf-8")


# =================================================================================================
class TestBirth:
    def test_birth_draw_bounds_clamps_persistence_and_seed(self, tmp_path):
        cfg = _cfg(tmp_path)
        g = Genome(cfg)
        for name in LATENTS:
            assert -LATENT_BOUND <= g.latents[name] <= LATENT_BOUND
        for name, (_l, lo, hi) in GENE_LOADINGS.items():
            assert lo <= g.genes[name] <= hi, f"{name} outside its declared clamp"
        for v in g.stamp_baselines.values():
            assert BASELINE_LO <= v <= BASELINE_HI
        assert isinstance(g.seed, int) and g.born_ts
        doc = json.loads((cfg.workspace / GENOME_FILENAME).read_text(encoding="utf-8"))
        assert doc["seed"] == g.seed                      # the lineage seed is SAVED
        assert doc["latents"] == g.latents and doc["genes"] == g.genes

    def test_second_load_identical_no_redraw(self, tmp_path):
        cfg = _cfg(tmp_path)
        g1 = Genome(cfg)
        g2 = Genome(cfg)                                  # load-or-birth → must LOAD
        assert g2.seed == g1.seed
        assert g2.latents == g1.latents
        assert g2.genes == g1.genes
        assert g2.stamp_baselines == g1.stamp_baselines

    def test_two_births_differ(self, tmp_path):
        ga = Genome(_cfg(tmp_path / "a"))
        gb = Genome(_cfg(tmp_path / "b"))
        assert ga.seed != gb.seed
        assert ga.latents != gb.latents
        assert ga.genes != gb.genes

    def test_loading_structure_coherent_covariation(self):
        """Force latents directly: one latent moves several knobs in a believable DIRECTION."""
        sensitive = express_genes({"sensitivity": 1.5, "openness": 0.0, "tenacity": 0.0, "tempo": 0.0})
        assert sensitive["emotional_stamp"] > 1.0         # feelings burn deeper…
        assert sensitive["spring_return"] < 1.0           # …AND linger (weaker spring) — coherent
        assert sensitive["drift_rate"] > 1.0              # …and experience imprints harder
        open_flit = express_genes({"sensitivity": 0.0, "openness": 1.5, "tenacity": -1.0, "tempo": 0.0})
        assert open_flit["explore_recall"] > 1.0
        assert open_flit["explore_salience"] > 1.0
        assert open_flit["restlessness"] > 1.0            # dilettante
        assert open_flit["grip"] < 1.0                    # loose grip
        driller = express_genes({"sensitivity": 0.0, "openness": -1.0, "tenacity": 1.5, "tempo": 0.0})
        assert driller["restlessness"] < 1.0              # deep-driller stays put
        assert driller["grip"] > 1.0                      # and grinds longer

    def test_wake_budget_stays_tight(self):
        """Adenosine sovereignty: even a full-tail tempo flavors the wake budget by ≤10%."""
        for t in (-1.5, 1.5):
            g = express_genes({"sensitivity": 0.0, "openness": 0.0, "tenacity": 0.0, "tempo": t})
            assert 0.9 <= g["wake_budget"] <= 1.1


# =================================================================================================
class TestFailOpen:
    def test_no_workspace_gene_is_one_and_nothing_raises(self, tmp_path):
        cfg = _cfg(tmp_path, mkdir=False)
        assert gene(cfg, "emotional_stamp") == 1.0
        assert gene(cfg, "no_such_gene") == 1.0
        assert gene(None, "emotional_stamp") == 1.0
        assert stamp_baselines(cfg) is None
        assert not Path(cfg.workspace_dir).exists()       # the accessor never creates anything

    def test_corrupt_file_reads_as_one_and_is_not_overwritten(self, tmp_path):
        cfg = _cfg(tmp_path)
        p = cfg.workspace / GENOME_FILENAME
        p.write_text("{not json", encoding="utf-8")
        assert gene(cfg, "emotional_stamp") == 1.0
        assert stamp_baselines(cfg) is None
        assert p.read_text(encoding="utf-8") == "{not json"   # read-only: no birth over the corpse

    def test_loaded_genes_are_reclamped(self, tmp_path):
        """A hand-edited genome.json can never push a gene outside its declared clamp — a genome
        must never disable a damper."""
        cfg = _cfg(tmp_path)
        _write_genome(cfg, genes={"wake_budget": 5.0, "emotional_stamp": 0.0})
        assert gene(cfg, "wake_budget") == pytest.approx(1.1)
        assert gene(cfg, "emotional_stamp") == pytest.approx(0.6)


# =================================================================================================
class TestLedgerFirewall:
    def test_ledger_files_never_import_genome(self):
        for fname in ("persona.py", "level_gates.py", "quests.py", "expectations.py"):
            src = (_ROOT / fname).read_text(encoding="utf-8")
            assert "import genome" not in src and "from genome" not in src, \
                f"{fname} imports genome — the ledger firewall is breached (wireheading-by-birth)"

    def test_no_gene_name_references_the_ledger(self):
        banned = {"xp", "level", "levels", "coin", "coins", "bet", "bets", "quest", "quests"}
        for name in GENE_LOADINGS:
            hits = banned & set(name.lower().split("_"))
            assert not hits, \
                f"gene {name!r} references the ledger ({hits}) — species law is untouchable"


# =================================================================================================
class TestConsumers:
    def test_temperament_baselines_come_from_stamp_baselines(self, tmp_path):
        cfg = _cfg(tmp_path)
        _write_genome(cfg, baselines={"initiative": 0.61, "persistence": 0.39, "caution": 0.57})
        from nervous.temperament import Temperament
        t = Temperament(config=cfg)
        assert t.baselines == {"initiative": 0.61, "persistence": 0.39, "caution": 0.57}
        for ax in Temperament.AXES:                       # a newborn starts AT its own nature
            assert getattr(t, ax) == t.baselines[ax]
        assert t.disposition() == "steady"                # never pre-labeled

    def test_temperament_uniform_fallback_without_genome(self, tmp_path):
        cfg = _cfg(tmp_path)                              # no genome.json
        from nervous.temperament import BIRTH_SPREAD, Temperament
        t = Temperament(config=cfg)
        for ax in Temperament.AXES:
            assert abs(t.baselines[ax] - 0.5) <= BIRTH_SPREAD + 1e-6

    def test_restlessness_scales_and_clamps(self, tmp_path):
        from learning_progress import ProgressTracker
        cfg = _cfg(tmp_path)
        _write_genome(cfg, genes={"restlessness": 0.6})
        assert ProgressTracker(cfg).restlessness_signal("unexplored") == pytest.approx(0.6)
        cfg2 = _cfg(tmp_path / "hi")
        _write_genome(cfg2, genes={"restlessness": 1.6})
        assert ProgressTracker(cfg2).restlessness_signal("unexplored") == 1.0   # re-clamped

    def test_recall_explore_ratio_scales(self, tmp_path):
        from memory_manager import MemoryManager
        cfg = _cfg(tmp_path)
        _write_genome(cfg, genes={"explore_recall": 1.8})
        mgr = MemoryManager(cfg)
        expected = float(cfg.pillars_recall_explore_ratio) * 1.8
        assert mgr.effective_explore_ratio() == pytest.approx(expected)
        bare = MemoryManager(_cfg(tmp_path / "bare"))     # no genome → the bare config constant
        assert bare.effective_explore_ratio() == pytest.approx(
            float(bare.config.pillars_recall_explore_ratio))

    def test_grip_scales_park_threshold(self, tmp_path):
        from nervous.temperament import Temperament
        cfg = _cfg(tmp_path)
        _write_genome(cfg, genes={"grip": 1.4})
        t = Temperament(config=cfg)                       # neutral persistence → base factor 1.0
        assert t.park_threshold(10) == 14
        loose = _cfg(tmp_path / "loose")
        _write_genome(loose, genes={"grip": 0.7})
        assert Temperament(config=loose).park_threshold(10) == 7

    def test_wake_budget_flavors_adenosine_within_ten_percent(self, tmp_path):
        from nervous.neuromod import Adenosine
        cfg = _cfg(tmp_path)
        _write_genome(cfg, genes={"wake_budget": 1.1})
        assert Adenosine(max_wake_hours=18.0, config=cfg).max_wake_hours == pytest.approx(19.8)
        assert Adenosine(max_wake_hours=18.0, config=None).max_wake_hours == pytest.approx(18.0)

    def test_emotional_stamp_scales_the_feeling_gain_only(self, tmp_path):
        from bets import emotional_multiplier
        cfg = _cfg(tmp_path)
        _write_genome(cfg, genes={"emotional_stamp": 1.6})
        hot = types.SimpleNamespace(arousal=1.0, valence=1.0)
        neutral = types.SimpleNamespace(arousal=0.0, valence=0.0)
        base = emotional_multiplier(hot)                  # no config → gene 1.0
        boosted = emotional_multiplier(hot, cfg)
        assert boosted - 1.0 == pytest.approx((base - 1.0) * 1.6)   # the GAIN scales
        assert emotional_multiplier(neutral, cfg) == 1.0  # a neutral stamp still multiplies by 1


# =================================================================================================
class TestNewbornAlwaysSteady:
    def test_thousand_draws_disposition_is_steady(self):
        """1000 synthetic latent draws → every expressed baseline is inside the clamp band and a
        newborn parked AT those baselines is always disposition() == 'steady'."""
        from nervous.temperament import Temperament
        rng = random.Random(0xE1D05)
        probe = Temperament(config=None)                  # a bare probe; no persistence, no draw
        for _ in range(1000):
            latents = {n: max(-LATENT_BOUND, min(LATENT_BOUND, rng.gauss(0.0, 0.6)))
                       for n in LATENTS}
            bl = express_baselines(latents)
            for v in bl.values():
                assert BASELINE_LO <= v <= BASELINE_HI
            probe.initiative = bl["initiative"]
            probe.persistence = bl["persistence"]
            probe.caution = bl["caution"]
            assert probe.disposition() == "steady"
