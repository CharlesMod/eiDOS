"""Memory quality: arousal-seeded encoding strength + recency-tilted recall.

Gates (red-able):
  - encode salience: with the flag on and a live neuromod organ, a high-arousal moment births a
    STRONGER engram than a flat one; flag off (or organ absent, or caller-set strength) births at
    the historical default — byte-identical behavior;
  - recency: recency_factor is 1.0 at birth, halves per half-life, floors at RECENCY_FLOOR, and
    fails OPEN (unparseable timestamp → 1.0);
  - recall tilt: with the flag on, of two same-relevance same-strength engrams the RECENT one
    outranks the stale one; with the flag off, ranking is unchanged by age;
  - BM25 tilt: same contract over the knowledge store.

No services / tick loop / GPU / model — temp workspaces only.
"""

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from config import Config
import engram
from engram import STRENGTH_DEFAULT, RECENCY_FLOOR, RECENCY_HALFLIFE_S, recency_factor
from memory_manager import MemoryManager, ENCODE_AROUSAL_GAIN


def _cfg(tmp_path) -> Config:
    cfg = Config()
    cfg.workspace_dir = str(tmp_path / "workspace")
    cfg.mock_mode = False
    cfg.pillars_memory_engram_enabled = True
    cfg.pillars_memory_manager_enabled = True
    cfg.workspace.mkdir(parents=True, exist_ok=True)
    return cfg


class _Neuromod:
    def __init__(self, arousal):
        self.arousal = arousal
        self.valence = 0.0


def _iso(epoch: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(epoch))


# =================================================================================================
# recency_factor — the one shared clock for every recall ranker
# =================================================================================================
class TestRecencyFactor:
    def test_newborn_scores_full(self):
        now = time.time()
        assert recency_factor(_iso(now), now=now) > 0.999   # ISO stamps at second resolution

    def test_one_halflife_halves(self):
        now = time.time()
        f = recency_factor(_iso(now - RECENCY_HALFLIFE_S), now=now)
        assert abs(f - 0.5) < 0.01

    def test_age_floors_never_buries(self):
        now = time.time()
        f = recency_factor(_iso(now - 100 * RECENCY_HALFLIFE_S), now=now)
        assert f == RECENCY_FLOOR

    def test_unparseable_timestamp_fails_open(self):
        assert recency_factor("not a date") == 1.0
        assert recency_factor("") == 1.0
        assert recency_factor(None) == 1.0


# =================================================================================================
# Encode salience — arousal seeds birth strength (§M-1), dark by flag
# =================================================================================================
class TestEncodeSalience:
    def test_spiked_moment_births_stronger_than_flat(self, tmp_path):
        cfg = _cfg(tmp_path)
        cfg.pillars_encode_salience_enabled = True
        hot = MemoryManager(cfg, neuromod=_Neuromod(1.0)).encode("episode", "the fuse blew")
        cold = MemoryManager(cfg, neuromod=_Neuromod(0.0)).encode("episode", "a quiet tick passed")
        assert hot.strength > STRENGTH_DEFAULT
        assert cold.strength < STRENGTH_DEFAULT
        assert abs(hot.strength - (STRENGTH_DEFAULT + ENCODE_AROUSAL_GAIN * 0.5)) < 1e-9
        assert abs(cold.strength - (STRENGTH_DEFAULT - ENCODE_AROUSAL_GAIN * 0.5)) < 1e-9

    def test_flag_off_births_at_default(self, tmp_path):
        cfg = _cfg(tmp_path)
        cfg.pillars_encode_salience_enabled = False
        eg = MemoryManager(cfg, neuromod=_Neuromod(1.0)).encode("episode", "a hot moment, dark flag")
        assert eg.strength == STRENGTH_DEFAULT

    def test_no_organ_births_at_default(self, tmp_path):
        cfg = _cfg(tmp_path)
        cfg.pillars_encode_salience_enabled = True
        eg = MemoryManager(cfg, neuromod=None).encode("episode", "no affect channel")
        assert eg.strength == STRENGTH_DEFAULT

    def test_caller_set_strength_is_respected(self, tmp_path):
        cfg = _cfg(tmp_path)
        cfg.pillars_encode_salience_enabled = True
        eg = MemoryManager(cfg, neuromod=_Neuromod(1.0)).encode(
            "fact", "an inherited floor", strength=0.6)
        assert eg.strength == 0.6


# =================================================================================================
# Recall tilt — recent outranks stale at equal relevance × strength, only under the flag
# =================================================================================================
class TestRecallRecencyTilt:
    def _seed_pair(self, cfg):
        """Two engrams with identical bodies-relevance and strength; one stale, one fresh."""
        mm = MemoryManager(cfg, neuromod=None)
        now = time.time()
        stale = engram.Engram(kind="fact", body="the printer lives at port nine one００ old",
                              provenance="experienced")
        fresh = engram.Engram(kind="fact", body="the printer lives at port nine one００ new",
                              provenance="experienced")
        stale.created = _iso(now - 30 * 86400)
        fresh.created = _iso(now)
        engram._commit_to_store(mm.store, [stale, fresh])
        return mm, stale, fresh

    def test_flag_on_fresh_outranks_stale(self, tmp_path):
        cfg = _cfg(tmp_path)
        cfg.pillars_recall_recency_enabled = True
        cfg.pillars_recall_explore_ratio = 0.0
        mm, stale, fresh = self._seed_pair(cfg)
        got = mm.recall("printer port nine")
        assert [e.id for e in got[:2]] == [fresh.id, stale.id]

    def test_flag_off_age_changes_nothing(self, tmp_path):
        cfg = _cfg(tmp_path)
        cfg.pillars_recall_recency_enabled = False
        cfg.pillars_recall_explore_ratio = 0.0
        mm, stale, fresh = self._seed_pair(cfg)
        got = mm.recall("printer port nine")
        # Equal score → stable sort keeps store order (stale first): age must not re-order.
        assert [e.id for e in got[:2]] == [stale.id, fresh.id]


# =================================================================================================
# BM25 tilt — the knowledge store obeys the same clock
# =================================================================================================
class TestBM25RecencyTilt:
    def _seed(self, cfg):
        """Two token-identical index entries (only kitchen/garage differ, both df=1) so pure
        BM25 ties EXACTLY on the shared-token query; one entry backdated a month. Written to the
        index directly — store_entry's near-duplicate dedup would fold docs this similar."""
        import knowledge
        cfg.knowledge_enabled = True
        now = time.time()
        idx = [
            {"id": "stale_plug", "category": "facts", "tags": [],
             "content_preview": "smart plug answers on the kitchen lan segment",
             "created": _iso(now - 30 * 86400)},
            {"id": "fresh_plug", "category": "facts", "tags": [],
             "content_preview": "smart plug answers on the garage lan segment",
             "created": _iso(now)},
            # Filler so the query terms' document frequency < N/2 (positive IDF — the shape of
            # any real store; a 2-doc corpus scores every shared term negative and recency
            # deliberately leaves non-positive scores alone).
            {"id": "filler_1", "category": "facts", "tags": [],
             "content_preview": "the otter dreamed about rivers", "created": _iso(now)},
            {"id": "filler_2", "category": "facts", "tags": [],
             "content_preview": "quests settle at adjudication", "created": _iso(now)},
            {"id": "filler_3", "category": "facts", "tags": [],
             "content_preview": "backups rotate weekly in snapshots", "created": _iso(now)},
        ]
        knowledge._write_index(cfg, idx)
        knowledge._bm25_instance = None          # force a rebuild over the seeded index
        return "stale_plug", "fresh_plug"

    def test_flag_on_fresh_entry_outranks_stale(self, tmp_path):
        import knowledge
        cfg = _cfg(tmp_path)
        cfg.pillars_recall_recency_enabled = True
        a, b = self._seed(cfg)
        got = knowledge.search_bm25(cfg, "smart plug lan segment", top_k=2)
        assert [r["id"] for r in got] == [b, a]

    def test_flag_off_age_changes_nothing(self, tmp_path):
        import knowledge
        cfg = _cfg(tmp_path)
        cfg.pillars_recall_recency_enabled = False
        a, b = self._seed(cfg)
        got = knowledge.search_bm25(cfg, "smart plug lan segment", top_k=2)
        assert {r["id"] for r in got} == {a, b}
        scores = [r["score"] for r in got]
        assert abs(scores[0] - scores[1]) < 1e-9   # timeless BM25: identical-shape docs tie
