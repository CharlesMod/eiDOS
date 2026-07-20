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
        assert PROVENANCE == frozenset({"experienced", "told", "inherited", "dreamed"})


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

    def test_contained_short_body_is_not_swallowed(self, tmp_path):
        # Subset-swallow regression: the overlap coefficient scores ANY token-subset 1.0, so a
        # short distinct engram whose tokens all appear inside a longer one merged — and _fold's
        # keeper-wins policy DISCARDED its body (silent information loss). The Jaccard scope
        # guard must keep them separate.
        cfg = _cfg(tmp_path)
        con = Consolidator(cfg)
        long = con.commit(_engram(
            body="quest skill library governance board needs the water heater moved from the "
                 "garage before the inspection deadline friday"))
        short = con.commit(_engram(body="the water heater moved from the garage"))
        store = LongTermStore(cfg)
        assert len(store) == 2                                # NOT swallowed by the container
        assert store.get(short.id) is not None                # the short body survived intact
        assert store.get(short.id).body == "the water heater moved from the garage"
        assert store.get(long.id) is not None

    def test_commit_many_matches_n_commits(self, tmp_path):
        # The bulk path (used by the boot importer) must produce the SAME store as N single commits —
        # identical pattern-separation dedup — but with ONE store rewrite instead of O(n) per record
        # (the O(n^2)-at-import fix). Same records, two paths, identical survivors.
        bodies = ["the water heater is in the garage",
                  "the water heater is in the garage now",       # near-dup of #1 → merges
                  "the router password rotates every friday",
                  "the printer is an octoprint node"]
        cfg_a = _cfg(tmp_path / "a")
        con_a = Consolidator(cfg_a)
        for b in bodies:
            con_a.commit(_engram(body=b))
        surv_a = sorted(e.body for e in LongTermStore(cfg_a).load())
        cfg_b = _cfg(tmp_path / "b")
        added = Consolidator(cfg_b).commit_many([_engram(body=b) for b in bodies])
        surv_b = sorted(e.body for e in LongTermStore(cfg_b).load())
        assert surv_a == surv_b                                # identical survivors + dedup
        assert added == len(surv_b) == 3                       # 4 records, one near-dup merged
        assert Consolidator(_cfg(tmp_path / "c")).commit_many([]) == 0   # empty batch is a no-op


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
class TestIncrementalEmbedding:
    """The vector sidecar syncs INCREMENTALLY: an ordinary single-engram commit embeds ONLY the new
    engram, not the whole store. This is the scaling fix — memory_manager.encode() commits on every
    acting tick, so a re-embed-all sidecar meant N embed HTTP calls/tick, growing without bound
    (CONTEXT_SPEC.md Finding 7). Bodies are immutable per id, so cached vectors are reused by id."""

    # Distinct rooms+fixtures per index — genuinely different token sets so pattern-separation keeps
    # them as separate engrams (overlap well under LONGTERM_MERGE_THRESHOLD) rather than merging.
    _ROOMS = ["garage", "kitchen", "attic", "cellar", "garden", "porch", "hallway", "basement"]
    _FIXTURES = ["heater", "faucet", "window", "ladder", "railing", "carpet", "freezer", "mailbox"]

    def _distinct_body(self, i: int) -> str:
        return f"the {self._ROOMS[i]} {self._FIXTURES[i]} needs attention soon"

    def _count_embeds(self, monkeypatch):
        """Wrap embedding.embed_query with a call counter; return the list of embedded texts."""
        import embedding as _emb
        calls: list[str] = []
        real = _emb.embed_query

        def counting(config, text):
            calls.append(text)
            return real(config, text)

        monkeypatch.setattr(_emb, "embed_query", counting)
        return calls

    def test_single_commit_embeds_only_the_new_engram(self, tmp_path, monkeypatch):
        cfg = _cfg(tmp_path)                         # mock_mode → deterministic hash embedder
        con = Consolidator(cfg)
        for i in range(6):                           # seed a non-trivial store (distinct → no merge)
            con.commit(_engram(kind="fact", body=self._distinct_body(i)))
        assert len(engram.LongTermStore(cfg)) == 6   # guard: seeding really made 6, not a merge blob
        # From here, one more commit must trigger EXACTLY ONE embed — not one-per-existing-engram.
        calls = self._count_embeds(monkeypatch)
        con.commit(_engram(kind="fact", body=self._distinct_body(6)))
        assert len(calls) == 1, f"a single commit must embed only the new engram, got {len(calls)}"

    def test_sidecar_stays_complete_after_incremental_commits(self, tmp_path):
        cfg = _cfg(tmp_path)
        con = Consolidator(cfg)
        ids = [con.commit(_engram(kind="fact", body=self._distinct_body(i))).id for i in range(4)]
        vecs, vec_ids = engram.LongTermStore(cfg)._load_vectors()
        assert vecs is not None
        assert vec_ids == ids                        # every engram embedded, in store order
        assert vecs.shape[0] == len(ids)

    def test_merge_and_update_embed_nothing(self, tmp_path, monkeypatch):
        cfg = _cfg(tmp_path)
        con = Consolidator(cfg)
        a = con.commit(_engram(body="the water heater is in the garage", strength=0.4))
        # After the store is seeded, neither a merge (body unchanged, same id) nor a strength update
        # touches a body — both must reuse the cached vector and embed NOTHING.
        calls = self._count_embeds(monkeypatch)
        con.commit(_engram(body="the water heater is in the garage", strength=0.9))   # merges into a
        con.update_strength(a.id, 0.7, recalled_tick=5)
        assert calls == [], f"merge/update must not re-embed (bodies immutable), got {calls}"

    def test_incremental_sidecar_matches_full_rebuild(self, tmp_path):
        cfg = _cfg(tmp_path)
        con = Consolidator(cfg)
        for i in range(5):
            con.commit(_engram(kind="fact", body=self._distinct_body(i)))
        store = engram.LongTermStore(cfg)
        assert len(store) == 5                        # guard: 5 distinct engrams, not a merge blob
        inc_vecs, inc_ids = store._load_vectors()
        store.rebuild_vectors()                      # repair path: re-embed everything from scratch
        reb_vecs, reb_ids = store._load_vectors()
        assert reb_ids == inc_ids                    # same ids, same order
        import numpy as np
        assert np.allclose(reb_vecs, inc_vecs)       # incremental reuse produced identical vectors

    def test_recall_still_semantic_after_incremental(self, tmp_path):
        cfg = _cfg(tmp_path)
        con = Consolidator(cfg)
        con.commit(_engram(body="the garage water heater pilot light went out"))
        con.commit(_engram(body="the front door lock battery is low"))
        con.commit(_engram(body="the router password rotates every friday morning"))
        hits = engram.LongTermStore(cfg).recall("water heater garage", top_k=2)
        assert hits and "water heater" in hits[0].body   # incremental vectors still rank correctly


# =================================================================================================
class TestConfigFlagWiring:
    def test_engram_flag_defaults_off(self):
        assert Config().pillars_memory_engram_enabled is False

    def test_flag_loads_from_toml(self):
        # The real config.toml documents the flag; loading it must not error and the flag must be
        # present as a real bool. (This test pinned `is False` during the dark era — the pillars
        # went LIVE 2026-07-05, so the shipped value is a deployment choice, not a test invariant.)
        root = Path(__file__).parent.parent
        toml = root / "config.toml"
        if toml.exists():
            from config import load_config
            cfg = load_config(str(toml))
            assert isinstance(cfg.pillars_memory_engram_enabled, bool)
