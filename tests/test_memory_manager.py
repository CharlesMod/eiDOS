"""Pillars 2.2: the memory manager (memory_manager.py) — offline unit tests.

Acceptance (PILLARS_TODO 2.2):
  - Importer maps each legacy store to the right engram kind; nuggets get provenance='inherited' +
    a strength floor; the legacy files are left UNTOUCHED; a re-import is idempotent (no duplicates).
  - Recall runs the 4-layer cascade: exact matches rank first, ranking is relevance × strength, and
    the set respects the char budget.
  - The exploration slot is present in a recall set and surfaces a low-strength engram that pure
    ranking would bury.
  - The emotional stamp reads live arousal/valence from the neuromod organ (mocked) and lands on the
    encoded engram.

Embeddings are mock-aware / fail-open (config.mock_mode), so no MiniLM/ONNX model is needed. No
services / tick loop / GPU — temp workspaces only. The manager is driven DIRECTLY (it is a library;
nothing wires it into the running system in this phase).
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest

from config import Config
from engram import Engram, EncodedAt, LongTermStore, Consolidator, INHERITED_STRENGTH_FLOOR
import memory_manager
from memory_manager import MemoryManager


# --- helpers -------------------------------------------------------------------------------------

def _cfg(tmp_path, *, mock: bool = True) -> Config:
    cfg = Config()
    cfg.workspace_dir = str(tmp_path / "workspace")
    cfg.mock_mode = mock                 # deterministic hash embedder (no ONNX model needed)
    (tmp_path / "workspace").mkdir(parents=True, exist_ok=True)
    return cfg


class _FakeNeuromod:
    """Stand-in for nervous/neuromod.py — the manager reads .arousal / .valence attributes."""
    def __init__(self, arousal=0.0, valence=0.0):
        self.arousal = arousal
        self.valence = valence


def _write_episode(cfg, *, tick, key, tool, sig="", fail_kind="", success=True, summary=""):
    rec = {"tick": tick, "key": key, "tool": tool, "sig": sig or tool,
           "fail_kind": fail_kind, "success": success, "summary": summary, "ts": 1.0}
    with open(cfg.workspace / "episodes.jsonl", "a", encoding="utf-8") as f:
        f.write(json.dumps(rec) + "\n")


# =================================================================================================
class TestEmotionalStamp:
    def test_stamp_reads_live_affect_from_neuromod(self, tmp_path):
        cfg = _cfg(tmp_path)
        mgr = MemoryManager(cfg, neuromod=_FakeNeuromod(arousal=0.82, valence=-0.4))
        eg = mgr.encode("fact", "the space heater trips the fuse box", tick=7, felt="tense")
        assert eg.encoded_at.arousal == pytest.approx(0.82)
        assert eg.encoded_at.valence == pytest.approx(-0.4)
        assert eg.encoded_at.tick == 7
        assert eg.encoded_at.felt == "tense"
        # And it persisted through the consolidator onto the stored engram.
        stored = LongTermStore(cfg).get(eg.id)
        assert stored is not None
        assert stored.encoded_at.arousal == pytest.approx(0.82)

    def test_stamp_fails_open_to_neutral_without_neuromod(self, tmp_path):
        cfg = _cfg(tmp_path)
        mgr = MemoryManager(cfg, neuromod=None)     # no organ available
        eg = mgr.encode("fact", "the router lives behind the tv")
        assert eg.encoded_at.arousal == 0.0
        assert eg.encoded_at.valence == 0.0

    def test_stamp_fails_open_when_read_raises(self, tmp_path):
        class _Broken:
            @property
            def arousal(self):
                raise RuntimeError("bus down")
            valence = 0.5
        cfg = _cfg(tmp_path)
        mgr = MemoryManager(cfg, neuromod=_Broken())
        eg = mgr.encode("fact", "the water main shutoff is under the sink")
        assert eg.encoded_at.arousal == 0.0 and eg.encoded_at.valence == 0.0


# =================================================================================================
class TestImporter:
    def test_episodes_map_to_episode_engrams(self, tmp_path):
        cfg = _cfg(tmp_path)
        _write_episode(cfg, tick=1, key="obj7|scan the lan", tool="nmap",
                       success=False, fail_kind="timeout", summary="scan timed out")
        _write_episode(cfg, tick=2, key="obj7|scan the lan", tool="nmap",
                       success=True, summary="scan completed")
        mgr = MemoryManager(cfg)
        n = mgr.import_episodes()
        assert n == 2
        stored = LongTermStore(cfg).load()
        assert all(e.kind == "episode" for e in stored)
        assert all(e.provenance == "experienced" for e in stored)
        # The situation key rides along in stats so recall can key on it.
        assert all(e.stats.get("situation") == "obj7|scan the lan" for e in stored)

    def test_knowledge_categories_map_to_kinds(self, tmp_path):
        cfg = _cfg(tmp_path)
        import knowledge
        knowledge.store_entry(cfg, "the boiler is serviced every autumn", tags=["boiler"],
                              category="facts")
        knowledge.store_entry(cfg, "to reset the breaker flip it fully off then on", tags=["breaker"],
                              category="procedures")
        knowledge.store_entry(cfg, "ssh to the nas hangs when the vpn is up", tags=["nas"],
                              category="errors")
        knowledge.store_entry(cfg, "i tend to over-scan networks when anxious", tags=["self"],
                              category="reflections")
        mgr = MemoryManager(cfg)
        n = mgr.import_knowledge()
        assert n == 4
        kinds = sorted(e.kind for e in LongTermStore(cfg).load())
        # facts+reflections -> fact (x2), procedures -> procedure, errors -> error
        assert kinds == ["error", "fact", "fact", "procedure"]

    def test_nuggets_are_inherited_with_strength_floor(self, tmp_path, monkeypatch):
        cfg = _cfg(tmp_path)
        import seed_knowledge
        monkeypatch.setattr(seed_knowledge, "load_nuggets",
                            lambda *a, **k: [("facts", ["self"], "you are eidos, the house mind")])
        mgr = MemoryManager(cfg)
        n = mgr.import_nuggets()
        assert n == 1
        eg = LongTermStore(cfg).load()[0]
        assert eg.provenance == "inherited"
        assert eg.strength == pytest.approx(INHERITED_STRENGTH_FLOOR)
        assert eg.strength >= 0.6                       # the floor keeps bootstrap knowledge alive

    def test_legacy_files_left_untouched(self, tmp_path):
        cfg = _cfg(tmp_path)
        _write_episode(cfg, tick=1, key="obj1|do a thing", tool="ls", success=True)
        ep_path = cfg.workspace / "episodes.jsonl"
        before = ep_path.read_bytes()
        MemoryManager(cfg).import_episodes()
        assert ep_path.read_bytes() == before          # import is read-only on the original

    def test_reimport_is_idempotent(self, tmp_path, monkeypatch):
        cfg = _cfg(tmp_path)
        _write_episode(cfg, tick=1, key="obj1|do a thing", tool="ls", success=True)
        import knowledge, seed_knowledge
        knowledge.store_entry(cfg, "the fusebox is in the hall closet", tags=["fusebox"],
                              category="facts")
        monkeypatch.setattr(seed_knowledge, "load_nuggets",
                            lambda *a, **k: [("facts", ["boot"], "a durable bootstrap fact")])
        mgr = MemoryManager(cfg)
        first = mgr.import_all()
        count_after_first = len(LongTermStore(cfg).load())
        second = mgr.import_all()
        count_after_second = len(LongTermStore(cfg).load())
        assert count_after_second == count_after_first  # no duplication
        assert sum(second.values()) == 0                # a re-run imports nothing new

    def test_import_all_reports_per_store_counts(self, tmp_path, monkeypatch):
        cfg = _cfg(tmp_path)
        _write_episode(cfg, tick=1, key="obj1|a", tool="ls", success=True)
        _write_episode(cfg, tick=2, key="obj1|b", tool="cat", success=True)
        import knowledge, seed_knowledge
        knowledge.store_entry(cfg, "a wholly unique fact about the attic", tags=["attic"],
                              category="facts")
        monkeypatch.setattr(seed_knowledge, "load_nuggets",
                            lambda *a, **k: [("facts", ["x"], "an inherited nugget for counts")])
        counts = MemoryManager(cfg).import_all()
        assert counts["episodes"] == 2
        assert counts["knowledge"] == 1
        assert counts["nuggets"] == 1


# =================================================================================================
class TestEpisodeBodyHygiene:
    """The body is what a future recall injects verbatim — legacy records carry plan-list markers
    and mid-word hard slices, and the body seam must not let them through as malformed shards."""

    def _import_one(self, tmp_path, **rec):
        cfg = _cfg(tmp_path)
        _write_episode(cfg, **rec)
        MemoryManager(cfg).import_episodes()
        return LongTermStore(cfg).load()[0].body

    def test_plan_list_marker_stripped_from_step(self, tmp_path):
        body = self._import_one(tmp_path, tick=1, key="obj7|#. create a journal in the nest",
                                tool="thought", success=True)
        assert body == "While create a journal in the nest, `thought` succeeded."

    def test_marker_only_step_drops_the_while_shard(self, tmp_path):
        body = self._import_one(tmp_path, tick=1, key="obj7|#.", tool="bash", success=True)
        assert body == "`bash` succeeded."          # no "While ," fragment

    def test_legacy_midword_step_slice_healed_at_word_boundary(self, tmp_path):
        # Pre-fix keys were cut step[:80] mid-word ("…my progress, tho") — the body render must
        # back the shard off to a whole word and mark the cut honestly.
        step = ("track my progress with the journal " * 3)[:80]
        assert not step.endswith(" ") and step.endswith("p")     # a severed shard, like the live one
        body = self._import_one(tmp_path, tick=1, key=f"obj7|{step}", tool="thought", success=True)
        healed = body[len("While "):body.index(", `thought`")]
        assert healed.endswith("…")
        assert all(w in ("track", "my", "progress", "with", "the", "journal")
                   for w in healed[:-1].split()), healed         # only whole words survive

    def test_legacy_midword_summary_slice_healed(self, tmp_path):
        summary = ("catalogued the services on the segment " * 5)[:160]
        assert summary.endswith("cata")              # the severed stump a legacy [:160] leaves
        body = self._import_one(tmp_path, tick=1, key="obj7|probe the lan", tool="bash",
                                success=True, summary=summary)
        assert body.endswith("segment…")             # backed off to a word boundary, not the stump


# =================================================================================================
class TestRecallCascade:
    def _seed_situations(self, cfg, *, strength=0.9):
        """Episodes across two objectives sharing a step, plus an unrelated one. All at equal
        strength by default, so the *cascade layer order* (relevance) is what determines ranking."""
        con = Consolidator(cfg)
        exact = Engram(kind="episode", body="exact: scanning the lan under obj7 timed out",
                       strength=strength, stats={"situation": "obj7|scan the lan"})
        cross = Engram(kind="episode", body="cross: scanning the lan under obj9 succeeded",
                       strength=strength, stats={"situation": "obj9|scan the lan"})
        same_obj = Engram(kind="episode", body="same: rebooting the switch under obj7",
                          strength=strength, stats={"situation": "obj7|reboot the switch"})
        unrelated = Engram(kind="fact", body="wholly unrelated trivia about the garden hose",
                           strength=strength, stats={"situation": ""})
        for e in (exact, cross, same_obj, unrelated):
            con.commit(e)
        return exact, cross, same_obj, unrelated

    def test_exact_match_ranks_first(self, tmp_path):
        cfg = _cfg(tmp_path)
        # Equal strength across the field, so ranking (relevance × strength) is driven by the
        # cascade's relevance: exact (1.0) > cross-objective (0.85) > same-objective (0.7).
        exact, cross, same_obj, _ = self._seed_situations(cfg, strength=0.9)
        mgr = MemoryManager(cfg)
        hits = mgr.recall("scan the lan", situation="obj7|scan the lan",
                          explore_ratio=0.0)   # isolate ranking from the exploration slot
        ids = [e.id for e in hits]
        assert ids[0] == exact.id                              # exact match ranks FIRST
        assert ids.index(exact.id) < ids.index(cross.id)       # exact before cross-objective
        assert ids.index(cross.id) < ids.index(same_obj.id)    # cross-objective before same-objective

    def test_ranks_by_relevance_times_strength(self, tmp_path):
        cfg = _cfg(tmp_path)
        con = Consolidator(cfg)
        # Two engrams, identical situation relevance (both exact), different strength.
        weak = con.commit(Engram(kind="episode", body="weak exact hit on the boiler",
                                 strength=0.2, stats={"situation": "objA|check the boiler"}))
        strong = con.commit(Engram(kind="episode", body="strong exact hit on the boiler",
                                   strength=0.9, stats={"situation": "objA|check the boiler"}))
        mgr = MemoryManager(cfg)
        hits = mgr.recall("check the boiler", situation="objA|check the boiler", explore_ratio=0.0)
        ids = [e.id for e in hits]
        assert ids.index(strong.id) < ids.index(weak.id)   # higher strength ranks first at equal relevance

    def test_respects_char_budget(self, tmp_path):
        cfg = _cfg(tmp_path)
        con = Consolidator(cfg)
        for i in range(6):
            con.commit(Engram(kind="fact", body=f"budgeted fact number {i} about the pantry shelf",
                              strength=0.9, stats={"situation": ""}))
        mgr = MemoryManager(cfg)
        hits = mgr.recall("pantry shelf fact", budget_chars=90, explore_ratio=0.0)
        total = sum(len(e.body) for e in hits)
        assert hits                                   # not empty
        # Budget respected: the set fits, or is a single over-budget top engram.
        assert total <= 90 or len(hits) == 1

    def test_semantic_layer_finds_resemblance(self, tmp_path):
        cfg = _cfg(tmp_path)
        con = Consolidator(cfg)
        con.commit(Engram(kind="fact", body="the mqtt broker lives at ten dot zero dot one",
                          strength=0.7, stats={"situation": ""}))
        mgr = MemoryManager(cfg)
        # No situation — recall must fall to the semantic layer (token overlap / vectors).
        hits = mgr.recall("mqtt broker address", explore_ratio=0.0)
        assert hits and "mqtt broker" in hits[0].body


# =================================================================================================
class TestExplorationSlot:
    def test_low_strength_engram_surfaces_via_exploration(self, tmp_path):
        cfg = _cfg(tmp_path)
        con = Consolidator(cfg)
        # A field of strong, equally-relevant engrams — each about a DISTINCT pantry item. Every body
        # shares the query terms ("pantry shelf jar") so they compete on strength, but each carries
        # enough unique descriptor tokens to stay below LONGTERM_MERGE_THRESHOLD (0.85 overlap) —
        # genuinely separate memories the pattern-separation dedup must keep, not near-restatements.
        bodies = [
            "the pantry shelf keeps a jar of flour milled fine for sourdough baking",
            "the pantry shelf keeps a jar of sugar raw cane crystals sweetening dessert",
            "the pantry shelf keeps a jar of rice short grain steamed alongside curry",
            "the pantry shelf keeps a jar of beans black turtle simmered into chili",
            "the pantry shelf keeps a jar of pasta dried rigatoni boiled under ragu",
            "the pantry shelf keeps a jar of oats rolled thick soaked overnight cold",
            "the pantry shelf keeps a jar of lentils split orange stewed with cumin",
            "the pantry shelf keeps a jar of quinoa rinsed white fluffed beside salmon",
        ]
        strong = []
        for body in bodies:
            strong.append(con.commit(Engram(
                kind="fact", body=body, strength=0.95, stats={"situation": ""})))
        # ...plus ONE low-strength but equally relevant engram pure ranking buries at the bottom.
        buried = con.commit(Engram(
            kind="fact", body="the pantry shelf keeps a dusty forgotten jar of saffron",
            strength=0.05, stats={"situation": ""}))
        assert len(con.store.load()) == len(bodies) + 1       # nothing merged — all distinct

        mgr = MemoryManager(cfg)
        # Pure ranking (explore off) with a tight budget: the 8 strong fill it and the low-strength
        # buried engram, ranked last, is cut.
        pure = mgr.recall("pantry shelf jar", budget_chars=8 * 55, explore_ratio=0.0)
        assert buried.id not in {e.id for e in pure}, "buried engram should NOT survive pure ranking"

        # With exploration on, the buried low-strength engram must surface (§6 anti-Matthew).
        explored = mgr.recall("pantry shelf jar", budget_chars=10_000, explore_ratio=0.15)
        assert buried.id in {e.id for e in explored}, "exploration slot must surface the buried engram"

    def test_exploration_slot_survives_tight_budget(self, tmp_path):
        """The sim-days finding: the slot used to be spliced into the CANDIDATE list before
        budgeting, parking it at index ~n·(1−ratio) — a production-shaped char budget cut it every
        time (promotions → 0 from day 2 of the coupled run). The seat must live INSIDE the budget,
        paid for by exploit's last seat."""
        cfg = _cfg(tmp_path)
        con = Consolidator(cfg)
        bodies = [
            "the pantry shelf keeps a jar of flour milled fine for sourdough baking",
            "the pantry shelf keeps a jar of sugar raw cane crystals sweetening dessert",
            "the pantry shelf keeps a jar of rice short grain steamed alongside curry",
            "the pantry shelf keeps a jar of beans black turtle simmered into chili",
            "the pantry shelf keeps a jar of pasta dried rigatoni boiled under ragu",
            "the pantry shelf keeps a jar of oats rolled thick soaked overnight cold",
            "the pantry shelf keeps a jar of lentils split orange stewed with cumin",
            "the pantry shelf keeps a jar of quinoa rinsed white fluffed beside salmon",
        ]
        for body in bodies:
            con.commit(Engram(kind="fact", body=body, strength=0.95, stats={"situation": ""}))
        buried = con.commit(Engram(
            kind="fact", body="the pantry shelf keeps a dusty forgotten jar of saffron",
            strength=0.05, stats={"situation": ""}))
        mgr = MemoryManager(cfg)

        budget = 3 * 75   # fits only ~3 strong bodies — far short of where the old splice sat
        out = mgr.recall("pantry shelf jar", budget_chars=budget, explore_ratio=0.15)
        ids = {e.id for e in out}
        assert buried.id in ids, "the exploration seat must survive a tight budget"
        assert len(out) >= 2, "exploration accompanies recall, never replaces it"
        assert sum(len(e.body) for e in out) <= budget, "the seat is paid for WITHIN the budget"

    def test_exploration_ratio_defaults_from_config(self, tmp_path):
        cfg = _cfg(tmp_path)
        assert cfg.pillars_recall_explore_ratio == pytest.approx(0.15)   # the declared default
        con = Consolidator(cfg)
        for i in range(8):
            con.commit(Engram(kind="fact", body=f"strong config fact {i} kept by ranking",
                              strength=0.95, stats={"situation": ""}))
        buried = con.commit(Engram(kind="fact", body="buried config fact excluded by ranking",
                                   strength=0.05, stats={"situation": ""}))
        mgr = MemoryManager(cfg)
        # No explore_ratio arg -> the manager reads config.pillars_recall_explore_ratio (0.15).
        explored = mgr.recall("config fact", budget_chars=10_000)
        assert buried.id in {e.id for e in explored}


# =================================================================================================
class TestLibraryDiscipline:
    def test_flag_defaults_off_and_manager_reports_it(self, tmp_path):
        cfg = _cfg(tmp_path)
        assert cfg.pillars_memory_manager_enabled is False
        assert MemoryManager(cfg).enabled is False

    def test_writes_go_through_the_consolidator(self, tmp_path):
        # The manager holds a Consolidator and never touches the store's name-mangled writer.
        cfg = _cfg(tmp_path)
        mgr = MemoryManager(cfg)
        assert isinstance(mgr.consolidator, Consolidator)
        assert mgr.store is mgr.consolidator.store
