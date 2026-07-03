"""Pillars 4.4: the news queue (news.py) — offline unit tests.

Acceptance (PILLARS_TODO 4.4, the gate):
  - news NEVER surfaces during absence (presence=False → [], always, regardless of queue depth);
  - engagement feedback MEASURABLY reorders ranking (engaged source beats ignored source next
    surface — including overturning the prior's own ordering);
  - the bound is respected: item N+1 evicts the lowest-ranked; expired items vanish;
  - items are valid kind='news' engrams written via the Consolidator (visible in long-term);
  - flag off → ingest/surface are no-ops and nothing is persisted.

No services / tick loop / GPU — temp workspaces, mock embedder (config.mock_mode).
"""

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest

from config import Config
from engram import Engram, LongTermStore
import news
from news import (
    NewsQueue, NewsItem, SOURCES,
    NEWS_TTL_S, EXPECTATION_SURPRISE_FLOOR_BITS,
    PRIOR_SOURCE_WEIGHT, WEIGHT_FLOOR, WEIGHT_CEIL,
    _queue_path,
)


# --- helpers -------------------------------------------------------------------------------------

def _cfg(tmp_path, *, enabled: bool = True, max_items: int = 20) -> Config:
    cfg = Config()
    cfg.workspace_dir = str(tmp_path / "workspace")
    cfg.mock_mode = True                      # deterministic hash embedder (no ONNX model needed)
    cfg.pillars_news_enabled = enabled
    cfg.pillars_news_max_items = max_items
    return cfg


# Bodies with disjoint token sets so the Consolidator's merge (overlap ≥ 0.85) never collapses
# distinct test items into one engram.
_BODIES = [
    "backup restore drill verified overnight without errors",
    "printer queue jammed twice during morning window",
    "front door sensor battery dropped below threshold",
    "garden irrigation valve stuck open near sunrise",
    "network switch rebooted itself around midnight",
    "solar inverter output spiked past yesterday peak",
]


def _t0() -> float:
    return time.time()


# =================================================================================================
class TestPresenceGate:
    def test_absence_returns_nothing_regardless_of_queue_depth(self, tmp_path):
        cfg = _cfg(tmp_path)
        q = NewsQueue(cfg)
        now = _t0()
        for i, body in enumerate(_BODIES):
            assert q.ingest(body, "anomaly", surprise=5.0, now=now) is not None
        assert len(q.items(now=now)) == len(_BODIES)
        # The gate: absence surfaces NOTHING — deep queue, huge budget, doesn't matter.
        assert q.surface(False, now=now) == []
        assert q.surface(False, budget_chars=10**6, now=now) == []
        assert q.surface(presence=False, now=now) == []

    def test_presence_surfaces_ranked_items(self, tmp_path):
        cfg = _cfg(tmp_path)
        q = NewsQueue(cfg)
        now = _t0()
        q.ingest(_BODIES[0], "quest", now=now)
        q.ingest(_BODIES[1], "anomaly", surprise=4.0, now=now)
        out = q.surface(True, now=now)
        assert len(out) == 2
        assert all(isinstance(it, NewsItem) for it in out)
        # anomaly + surprise outranks a plain quest event under the prior
        assert out[0].source == "anomaly"

    def test_budget_limits_the_digest(self, tmp_path):
        cfg = _cfg(tmp_path)
        q = NewsQueue(cfg)
        now = _t0()
        q.ingest(_BODIES[0], "anomaly", surprise=5.0, now=now)
        q.ingest(_BODIES[1], "anomaly", surprise=1.0, now=now)
        top_len = len(_BODIES[0])
        out = q.surface(True, budget_chars=top_len, now=now)
        assert [it.body for it in out] == [_BODIES[0]]   # only the top item fits the budget


# =================================================================================================
class TestEngagementReordering:
    def test_feedback_measurably_reorders_and_overturns_the_prior(self, tmp_path):
        """The gate: record engaged on source-B (quest) items and ignored on source-A (anomaly)
        items → B ranks above A on the NEXT surface — even though the PRIOR ranks anomaly above
        quest. Dean's actual responses own the ordering."""
        cfg = _cfg(tmp_path)
        q = NewsQueue(cfg)
        now = _t0()

        # Round 1: equal surprise, equal recency — the prior puts anomaly (A) first.
        a1 = q.ingest(_BODIES[0], "anomaly", now=now)
        b1 = q.ingest(_BODIES[1], "quest", now=now)
        first = q.surface(True, now=now)
        assert [it.id for it in first].index(a1.id) < [it.id for it in first].index(b1.id)

        # Dean's actual responses: engaged with the quest item, ignored the anomaly.
        assert q.record_outcome(b1.id, engaged=True, now=now)
        assert q.record_outcome(a1.id, engaged=False, now=now)

        # Round 2: fresh items, same shape — the learned weights must now rank B above A.
        a2 = q.ingest(_BODIES[2], "anomaly", now=now)
        b2 = q.ingest(_BODIES[3], "quest", now=now)
        second = q.surface(True, now=now)
        ids = [it.id for it in second]
        assert ids.index(b2.id) < ids.index(a2.id), \
            "engagement feedback failed to reorder ranking (quest should now outrank anomaly)"

    def test_settled_items_leave_the_queue(self, tmp_path):
        cfg = _cfg(tmp_path)
        q = NewsQueue(cfg)
        now = _t0()
        it = q.ingest(_BODIES[0], "anomaly", now=now)
        assert q.record_outcome(it.id, engaged=True, now=now) is True
        assert q.items(now=now) == []                     # settled → no longer news
        assert q.record_outcome(it.id, engaged=True, now=now) is False   # already gone

    def test_weight_updates_are_bounded(self, tmp_path):
        """§0.6: no volume of one-sided feedback pushes a weight past the clamps."""
        cfg = _cfg(tmp_path)
        q = NewsQueue(cfg)
        now = _t0()
        for i in range(40):
            it = q.ingest(f"anomaly item number {i} token{i} distinct{i}", "anomaly",
                          surprise=6.0, now=now)
            if it is not None:
                q.record_outcome(it.id, engaged=False, now=now)
        w = q.weights()
        for v in [w["surprise"], w["recency"], *w["source"].values()]:
            assert WEIGHT_FLOOR <= v <= WEIGHT_CEIL

    def test_surfaced_then_expired_settles_as_not_engaged(self, tmp_path):
        """§0.3: an item Dean SAW and let die is the silent 'ignored' outcome."""
        cfg = _cfg(tmp_path)
        q = NewsQueue(cfg)
        now = _t0()
        q.ingest(_BODIES[0], "anomaly", now=now)
        q.surface(True, now=now)                          # shown → stamped surfaced_ts
        w_before = q.weights()["source"]["anomaly"]
        # Expiry passes; the next write-path pass settles it as not-engaged.
        q.surface(True, now=now + NEWS_TTL_S + 1)
        assert q.weights()["source"]["anomaly"] < w_before


# =================================================================================================
class TestBoundsAndExpiry:
    def test_item_over_bound_evicts_the_lowest_ranked(self, tmp_path):
        cfg = _cfg(tmp_path, max_items=3)
        q = NewsQueue(cfg)
        now = _t0()
        low = q.ingest(_BODIES[0], "anomaly", surprise=0.0, now=now)     # clearly lowest-ranked
        q.ingest(_BODIES[1], "anomaly", surprise=5.0, now=now)
        q.ingest(_BODIES[2], "anomaly", surprise=5.0, now=now)
        q.ingest(_BODIES[3], "anomaly", surprise=5.0, now=now)           # item N+1
        items = q.items(now=now)
        assert len(items) == 3                                           # bound respected
        assert low.id not in {it.id for it in items}                     # lowest-ranked evicted

    def test_expired_items_vanish(self, tmp_path):
        cfg = _cfg(tmp_path)
        q = NewsQueue(cfg)
        now = _t0()
        q.ingest(_BODIES[0], "anomaly", now=now)
        later = now + NEWS_TTL_S + 1
        assert q.items(now=later) == []
        assert q.surface(True, now=later) == []

    def test_expectation_surprise_floor_is_report_by_exception(self, tmp_path):
        """Pitfall #7: a routine closure (below the floor) is refused; a surprising one queues.
        The floor applies ONLY to the expectation source."""
        cfg = _cfg(tmp_path)
        q = NewsQueue(cfg)
        now = _t0()
        assert q.ingest(_BODIES[0], "expectation",
                        surprise=EXPECTATION_SURPRISE_FLOOR_BITS - 0.5, now=now) is None
        assert q.ingest(_BODIES[1], "expectation",
                        surprise=EXPECTATION_SURPRISE_FLOOR_BITS + 1.0, now=now) is not None
        assert q.ingest(_BODIES[2], "quest", surprise=0.0, now=now) is not None

    def test_unknown_source_is_rejected(self, tmp_path):
        q = NewsQueue(_cfg(tmp_path))
        with pytest.raises(ValueError):
            q.ingest("some body text here", "gossip")


# =================================================================================================
class TestEngramsViaConsolidator:
    def test_items_are_valid_news_engrams_in_long_term(self, tmp_path):
        cfg = _cfg(tmp_path)
        q = NewsQueue(cfg)
        now = _t0()
        item = q.ingest(_BODIES[0], "anomaly", surprise=2.0, now=now)
        store = LongTermStore(cfg)
        eg = store.get(item.id)
        assert eg is not None, "news engram not found in the long-term store"
        assert eg.kind == "news"
        assert eg.body == _BODIES[0]
        assert eg.is_valid()

    def test_store_exposes_no_public_write_path(self, tmp_path):
        """§I6 (the shape the Consolidator gate relies on): the long-term store the queue writes
        through has no public append/add/write — every news write went through commit()."""
        store = LongTermStore(_cfg(tmp_path))
        for name in ("append", "add", "write", "store", "save"):
            assert not hasattr(store, name)

    def test_closure_shaped_event_uses_residue_body_and_surprise(self, tmp_path):
        """An expectations.Closure-shaped object (duck-typed: .residue engram + .surprise)
        ingests from its residue — the 'what I bet vs what happened' is the news body."""
        cfg = _cfg(tmp_path)
        q = NewsQueue(cfg)

        class FakeClosure:
            residue = Engram(kind="episode",
                             body="I predicted the backup would finish by dawn. Outcome: WRONG.")
            surprise = 4.2

        item = q.ingest(FakeClosure(), "expectation", now=_t0())
        assert item is not None                       # 4.2 bits clears the floor
        assert item.surprise == 4.2
        assert "Outcome: WRONG" in item.body
        assert LongTermStore(cfg).get(item.id).kind == "news"

    def test_quest_shaped_event_ingests_from_directive(self, tmp_path):
        cfg = _cfg(tmp_path)
        q = NewsQueue(cfg)

        class FakeQuest:
            directive = "Verify a backup. Restore it and confirm it is whole."
            state = "passed"

        item = q.ingest(FakeQuest(), "quest", now=_t0())
        assert item is not None
        assert item.body.startswith("quest passed:")


# =================================================================================================
class TestDarkFlag:
    def test_flag_defaults_off(self):
        assert Config().pillars_news_enabled is False

    def test_flag_off_ingest_and_surface_are_noops_nothing_persisted(self, tmp_path):
        cfg = _cfg(tmp_path, enabled=False)
        q = NewsQueue(cfg)
        now = _t0()
        assert q.ingest(_BODIES[0], "anomaly", surprise=5.0, now=now) is None
        assert q.surface(True, now=now) == []
        assert q.record_outcome("whatever", engaged=True, now=now) is False
        # NOTHING persisted: no queue file, no long-term store, no workspace side effects.
        assert not _queue_path(cfg).exists()
        assert LongTermStore(cfg).load() == []
        assert not (cfg.knowledge_dir / "engram_longterm.jsonl").exists()

    def test_queue_state_round_trips_on_disk(self, tmp_path):
        """A fresh NewsQueue over the same workspace sees the same queue + learned weights."""
        cfg = _cfg(tmp_path)
        now = _t0()
        q1 = NewsQueue(cfg)
        it = q1.ingest(_BODIES[0], "anomaly", surprise=3.0, now=now)
        q1.record_outcome(it.id, engaged=True, now=now)
        q1.ingest(_BODIES[1], "quest", now=now)
        q2 = NewsQueue(cfg)                              # fresh instance, same disk
        assert [i.body for i in q2.items(now=now)] == [_BODIES[1]]
        assert q2.weights() == q1.weights()
        raw = json.loads(_queue_path(cfg).read_text(encoding="utf-8"))
        assert raw["outcomes"] == 1
