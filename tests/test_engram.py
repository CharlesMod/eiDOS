"""Pillars 2.1: the engram (engram.py) — offline unit tests.

Acceptance (PILLARS_TODO 2.1):
  - schema round-trips exactly (to_dict → from_dict identity) and validation rejects malformed;
  - the episodic ring is BOUNDED and evicts FIFO at capacity;
  - the Consolidator is the SINGLE WRITER of long-term (no public write on the store — asserted on
    the API shape);
  - a strength update mutates stats and re-persists;
  - provenance/confidence defaults are sane.

Embeddings are mock-aware / fail-open (config.mock_mode → deterministic hash embedder, matching
knowledge.py), so no MiniLM/ONNX model is needed. No services / tick loop / GPU — temp workspaces.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest

from config import Config
import engram
from engram import (
    Engram, EncodedAt, HotTrace, EpisodicRing, LongTermStore, Consolidator,
    KINDS, PROVENANCE, EPISODIC_RING_MAX,
    STRENGTH_DEFAULT, CONFIDENCE_DEFAULT,
)


# --- helpers -------------------------------------------------------------------------------------

def _cfg(tmp_path, *, mock: bool = True) -> Config:
    cfg = Config()
    cfg.workspace_dir = str(tmp_path / "workspace")
    cfg.mock_mode = mock            # deterministic hash embedder (no ONNX model needed)
    return cfg


def _engram(body="the mqtt broker lives at ten dot zero", kind="fact", **kw) -> Engram:
    return Engram(kind=kind, body=body, **kw)


# =================================================================================================
class TestSchemaRoundTrip:
    def test_to_dict_from_dict_identity(self):
        e = Engram(
            kind="episode", body="scanned the lan under objective seven",
            provenance="experienced", confidence=0.42, strength=0.71,
            encoded_at=EncodedAt(tick=9, felt="curious", arousal=0.8, valence=0.3),
            links=["abc", "def"],
            stats={"recall_count": 3, "last_recalled_tick": 9, "credit_sum": 1.25},
        )
        d = e.to_dict()
        e2 = Engram.from_dict(d)
        assert e2.to_dict() == d            # exact round-trip
        assert e2.id == e.id
        assert e2.encoded_at.to_dict() == e.encoded_at.to_dict()
        assert e2.links == e.links
        assert e2.stats == e.stats

    def test_defaults_are_sane(self):
        e = _engram()
        assert e.provenance == "experienced"
        assert e.confidence == CONFIDENCE_DEFAULT
        assert e.strength == STRENGTH_DEFAULT
        assert 0.0 <= e.confidence <= 1.0 and 0.0 <= e.strength <= 1.0
        assert e.stats == {"recall_count": 0, "last_recalled_tick": 0, "credit_sum": 0.0}
        assert e.id and e.created                # stable id + timestamp assigned
        assert e.is_valid()

    def test_from_dict_backfills_missing_stats(self):
        e = Engram.from_dict({"kind": "fact", "body": "x-ray of the pantry"})
        assert e.stats == {"recall_count": 0, "last_recalled_tick": 0, "credit_sum": 0.0}
        assert e.provenance == "experienced"     # sane defaults on a minimal record


# =================================================================================================
class TestValidation:
    def test_rejects_bad_kind(self):
        with pytest.raises(ValueError):
            _engram(kind="not-a-kind").validate()

    def test_rejects_bad_provenance(self):
        with pytest.raises(ValueError):
            _engram(provenance="hearsay").validate()

    def test_rejects_empty_body(self):
        with pytest.raises(ValueError):
            _engram(body="   ").validate()

    def test_rejects_out_of_range_strength(self):
        with pytest.raises(ValueError):
            _engram(strength=1.5).validate()
        with pytest.raises(ValueError):
            _engram(confidence=-0.1).validate()

    def test_rejects_non_dict_record(self):
        with pytest.raises(ValueError):
            Engram.from_dict(["not", "a", "dict"])

    def test_rejects_missing_required_field(self):
        with pytest.raises(ValueError):
            Engram.from_dict({"kind": "fact"})       # no body
        with pytest.raises(ValueError):
            Engram.from_dict({"body": "orphan"})     # no kind

    def test_valid_kinds_and_provenance_frozen(self):
        assert "episode" in KINDS and "identity" in KINDS
        assert PROVENANCE == frozenset({"experienced", "told", "inherited"})


# =================================================================================================
class TestHotTrace:
    def test_tick_scoped_and_cleared(self, tmp_path):
        hot = HotTrace()
        hot.add(_engram(body="alpha item"))
        hot.add(_engram(body="bravo item"))
        assert len(hot) == 2
        hot.clear()
        assert len(hot) == 0 and hot.all() == []

    def test_add_validates(self):
        hot = HotTrace()
        with pytest.raises(ValueError):
            hot.add(_engram(kind="bogus"))


# =================================================================================================
class TestEpisodicRing:
    def test_persists_and_reloads(self, tmp_path):
        cfg = _cfg(tmp_path)
        ring = EpisodicRing(cfg)
        ring.encode(_engram(body="first episode"))
        ring.encode(_engram(body="second episode"))
        reloaded = EpisodicRing(cfg).load()
        assert [e.body for e in reloaded] == ["first episode", "second episode"]

    def test_bounds_and_evicts_fifo_at_capacity(self, tmp_path):
        cfg = _cfg(tmp_path)
        cap = 5
        ring = EpisodicRing(cfg, max_items=cap)
        for i in range(cap + 3):                  # overfill by 3
            ring.encode(_engram(kind="episode", body=f"episode number {i}"))
        kept = ring.load()
        assert len(kept) == cap                   # bounded
        # FIFO: the OLDEST (0, 1, 2) evicted; the newest cap survive in order
        assert [e.body for e in kept] == [f"episode number {i}" for i in range(3, cap + 3)]
        assert len(ring) == cap

    def test_default_capacity_is_the_declared_constant(self, tmp_path):
        ring = EpisodicRing(_cfg(tmp_path))
        assert ring.max_items == EPISODIC_RING_MAX


# =================================================================================================
class TestSingleWriterConsolidator:
    """§I6: the Consolidator is the ONLY writer of long-term. The store's API shape enforces it —
    there is no public write method, and its mutator is name-mangled/private."""

    def test_store_exposes_no_public_write(self, tmp_path):
        store = LongTermStore(_cfg(tmp_path))
        public = {n for n in dir(store) if not n.startswith("_")}
        # Read surface exists...
        assert {"load", "get", "recall"} <= public
        # ...but no public mutator of any recognizable write name.
        for forbidden in ("add", "store", "write", "append", "commit", "save", "put", "insert"):
            assert forbidden not in public, f"long-term store must not expose public {forbidden!r}"

    def test_store_append_is_name_mangled(self, tmp_path):
        store = LongTermStore(_cfg(tmp_path))
        # The unmangled name is unreachable (that IS the guard); only the mangled bridge works.
        assert not hasattr(store, "append")
        assert hasattr(store, "_LongTermStore__append")

    def test_commit_is_the_write_path(self, tmp_path):
        cfg = _cfg(tmp_path)
        con = Consolidator(cfg)
        e = con.commit(_engram(body="the water heater is in the garage"))
        assert LongTermStore(cfg).get(e.id) is not None       # it landed in long-term
        assert len(LongTermStore(cfg)) == 1

    def test_commit_merges_near_restatement(self, tmp_path):
        cfg = _cfg(tmp_path)
        con = Consolidator(cfg)
        a = con.commit(_engram(body="the water heater is in the garage", strength=0.4))
        # A near-identical restatement of the SAME fact should merge, not duplicate.
        b = con.commit(_engram(body="the water heater is in the garage", strength=0.9))
        store = LongTermStore(cfg)
        assert len(store) == 1                                # merged, not two entries
        survivor = store.get(a.id)
        assert survivor is not None and survivor.strength == 0.9   # stronger witness wins
        assert b.id == a.id                                   # merge returned the keeper

    def test_distinct_facts_stay_separate(self, tmp_path):
        cfg = _cfg(tmp_path)
        con = Consolidator(cfg)
        con.commit(_engram(body="the water heater is in the garage"))
        con.commit(_engram(body="the router password rotates every friday morning"))
        assert len(LongTermStore(cfg)) == 2                   # pattern separation — not merged


# =================================================================================================
class TestStrengthUpdate:
    def test_update_strength_mutates_stats_and_repersists(self, tmp_path):
        cfg = _cfg(tmp_path)
        con = Consolidator(cfg)
        e = con.commit(_engram(body="dean tends to be home around six", strength=0.5))
        updated = con.update_strength(e.id, 0.82, recalled_tick=41, credit_delta=0.3)
        assert updated is not None
        assert updated.strength == pytest.approx(0.82)
        assert updated.stats["recall_count"] == 1
        assert updated.stats["last_recalled_tick"] == 41
        assert updated.stats["credit_sum"] == pytest.approx(0.3)
        # Re-persisted: a fresh read of the store sees the mutation.
        reread = LongTermStore(cfg).get(e.id)
        assert reread.strength == pytest.approx(0.82)
        assert reread.stats["recall_count"] == 1

    def test_update_strength_clamps_to_unit_interval(self, tmp_path):
        cfg = _cfg(tmp_path)
        con = Consolidator(cfg)
        e = con.commit(_engram(body="the fuse box trips on the space heater"))
        assert con.update_strength(e.id, 5.0).strength == 1.0
        assert con.update_strength(e.id, -3.0).strength == 0.0

    def test_update_strength_unknown_id_is_none(self, tmp_path):
        con = Consolidator(_cfg(tmp_path))
        assert con.update_strength("no-such-id", 0.5) is None


# =================================================================================================
class TestRecall:
    def test_recall_finds_relevant_engram(self, tmp_path):
        cfg = _cfg(tmp_path)                       # mock_mode → deterministic hash embedder
        con = Consolidator(cfg)
        con.commit(_engram(body="the garage water heater pilot light went out"))
        con.commit(_engram(body="the front door lock battery is low"))
        hits = LongTermStore(cfg).recall("water heater garage", top_k=2)
        assert hits, "recall returned nothing"
        assert "water heater" in hits[0].body      # most-relevant first

    def test_recall_empty_store_is_empty(self, tmp_path):
        assert LongTermStore(_cfg(tmp_path)).recall("anything") == []


# =================================================================================================
class TestConfigFlagWiring:
    def test_engram_flag_defaults_off(self):
        assert Config().pillars_memory_engram_enabled is False

    def test_flag_loads_from_toml(self):
        # The real config.toml documents the flag (dark); loading it must not error and the flag
        # must be present + false (nothing in the running system changes with it off).
        root = Path(__file__).parent.parent
        toml = root / "config.toml"
        if toml.exists():
            from config import load_config
            cfg = load_config(str(toml))
            assert cfg.pillars_memory_engram_enabled is False
