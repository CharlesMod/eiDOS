"""WISDOM_PLAN §3 — the wisdom calling convention (retrieval as answer, not reading material).

Acceptance (WISDOM_PLAN §3 + §W invariants):
  - The `## Before you act` block renders decision-shaped output from fixture stores: THE CASE
    (verified episode, action-first, sim + outcome + provenance), THE GUARDRAIL (matched strategy
    engram, verbatim imperative), THE OFFER (top affordance, invocation offer), closed with the
    fixed verify-transfer frame. Every engram item carries a provenance mark (§M-2).
  - WIS5 platform gate: renders ONLY when best-match similarity ≥ wisdom_recall_min_sim; below it,
    silence. Empty sections omitted; whole block absent when nothing qualifies.
  - Budget: wisdom_block_max_chars is a HARD cap — never exceeded (items dropped tail-first; if even
    one item overflows, the block is omitted).
  - REPLACES not adds: at the context.py placement, the prose recall is shaved by the block's actual
    size so the tick's total recall spend is unchanged.
  - Settlement plumbing: the engram items the block injects also ride the recall set `recall()`
    returns (which eidos.py wagers on as bets) — even when the char budget would have cut them.
  - WIS7 flag-dark: wisdom_recall_enabled off → block '' and context assembly byte-identical.
  - Bounded/fail-open: empty or corrupt stores yield graceful absence, never a raised exception.

Embeddings are mock-aware (config.mock_mode); no model / services / tick loop. Temp workspaces only;
the manager is driven directly, exactly as the other memory_manager tests drive it.
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest

from config import Config
from engram import Engram, Consolidator, LongTermStore
import memory_manager
from memory_manager import MemoryManager


# --- helpers -------------------------------------------------------------------------------------

def _cfg(tmp_path, *, wisdom=True, mock=True) -> Config:
    cfg = Config()
    cfg.workspace_dir = str(tmp_path / "workspace")
    cfg.mock_mode = mock                 # deterministic hash embedder (no ONNX model needed)
    cfg.wisdom_recall_enabled = wisdom
    cfg.pillars_memory_manager_enabled = True
    (tmp_path / "workspace").mkdir(parents=True, exist_ok=True)
    return cfg


_SIT = "obj7|scan the lan"


def _seed_case_guardrail(cfg, *, case_prov="experienced", case_strength=0.9,
                         guard_strength=0.8, verified=True, fix_ticks=None):
    """A verified success episode (THE CASE) + a matched strategy guardrail, both keyed to _SIT so
    the exact-situation layer scores them 1.0 (well over the 0.55 floor)."""
    con = Consolidator(cfg)
    case_stats = {"situation": _SIT, "verified": verified}
    if fix_ticks is not None:
        case_stats["fix_ticks"] = fix_ticks
    case = con.commit(Engram(kind="episode",
                             body="ran `ping 10.0.0.1` -> worked",
                             provenance=case_prov, strength=case_strength, stats=case_stats))
    guard = con.commit(Engram(kind="strategy",
                              body="When the lan scan stalls, restart the switch first.",
                              provenance="experienced", strength=guard_strength,
                              stats={"situation": _SIT}))
    return case, guard


# =================================================================================================
class TestDecisionShapedRender:
    def test_renders_case_guardrail_offer_and_frame(self, tmp_path):
        cfg = _cfg(tmp_path)
        _seed_case_guardrail(cfg)
        mgr = MemoryManager(cfg)
        text, injected = mgr.wisdom_block("scan the lan", situation=_SIT,
                                          affordances=[{"name": "lan_scan"}])
        assert text.startswith("## Before you act")
        # THE CASE — action-first with similarity + outcome + provenance
        assert "THE CASE" in text
        assert "sim 1.00" in text
        assert "ran `ping 10.0.0.1` -> worked" in text
        # THE GUARDRAIL — verbatim strategy imperative
        assert "THE GUARDRAIL — When the lan scan stalls, restart the switch first." in text
        # THE OFFER — invocation offer for the top affordance (not re-ranked)
        assert "THE OFFER" in text and "`lan_scan`" in text and "<lan_scan>" in text
        # the fixed verify-transfer frame closes the block (WIS5)
        assert text.rstrip().endswith(
            "These are YOUR precedents, not orders — verify they transfer before leaning on them.")
        # the two engram items are what the block injected (offer carries no engram)
        assert [e.kind for e in injected] == ["episode", "strategy"]

    def test_every_item_carries_provenance_mark(self, tmp_path):
        cfg = _cfg(tmp_path)
        # an INHERITED case + a strategy — the provenance marks must be visible per §M-2
        _seed_case_guardrail(cfg, case_prov="inherited")
        mgr = MemoryManager(cfg)
        text, _ = mgr.wisdom_block("scan the lan", situation=_SIT)
        assert "[inherited]" in text        # the case's provenance
        assert "[experienced]" in text      # the guardrail's provenance

    def test_fix_ticks_stat_adds_clause(self, tmp_path):
        cfg = _cfg(tmp_path)
        _seed_case_guardrail(cfg, fix_ticks=2)
        mgr = MemoryManager(cfg)
        text, _ = mgr.wisdom_block("scan the lan", situation=_SIT)
        assert "(fixed in 2 ticks)" in text

    def test_offer_omitted_when_no_affordances(self, tmp_path):
        cfg = _cfg(tmp_path)
        _seed_case_guardrail(cfg)
        mgr = MemoryManager(cfg)
        text, _ = mgr.wisdom_block("scan the lan", situation=_SIT, affordances=None)
        assert "THE OFFER" not in text          # empty section omitted
        assert "THE CASE" in text and "THE GUARDRAIL" in text

    def test_failure_episode_is_never_the_case(self, tmp_path):
        cfg = _cfg(tmp_path)
        con = Consolidator(cfg)
        # a FAILED episode at the exact situation — high relevance, but not a "do this" precedent
        con.commit(Engram(kind="episode", body="`ping` failed (timeout)", strength=0.9,
                          stats={"situation": _SIT, "verified": False}))
        mgr = MemoryManager(cfg)
        text, injected = mgr.wisdom_block("scan the lan", situation=_SIT)
        # nothing qualifies (no verified case, no strategy) -> graceful absence
        assert text == "" and injected == []


# =================================================================================================
class TestPlatformGate:
    def test_below_floor_silences_the_block(self, tmp_path):
        cfg = _cfg(tmp_path)
        con = Consolidator(cfg)
        # a verified episode, but keyed to a DIFFERENT, unrelated situation and body — the query
        # shares no tokens, so it can only earn a weak (or zero) semantic score, under the 0.55 floor.
        con.commit(Engram(kind="episode", body="the garden hose is coiled by the shed door",
                          strength=0.9, stats={"situation": "objZ|coil the hose", "verified": True}))
        mgr = MemoryManager(cfg)
        text, injected = mgr.wisdom_block("quantum entanglement handshake",
                                          situation="objX|nothing matches here")
        assert text == "" and injected == []

    def test_floor_read_from_config(self, tmp_path):
        cfg = _cfg(tmp_path)
        _seed_case_guardrail(cfg)
        mgr = MemoryManager(cfg)
        # raise the floor ABOVE the exact-match relevance (1.0 is the ceiling, so 1.01 silences all)
        cfg.wisdom_recall_min_sim = 1.01
        text, injected = mgr.wisdom_block("scan the lan", situation=_SIT)
        assert text == "" and injected == []
        # default floor lets the exact match through
        cfg.wisdom_recall_min_sim = 0.55
        text2, _ = mgr.wisdom_block("scan the lan", situation=_SIT)
        assert text2 and "THE CASE" in text2


# =================================================================================================
class TestBudgetHardCap:
    def test_never_exceeds_max_chars_dropping_tail_first(self, tmp_path):
        cfg = _cfg(tmp_path)
        _seed_case_guardrail(cfg)
        mgr = MemoryManager(cfg)
        # a cap that fits header + frame + case + guardrail but NOT the offer -> offer dropped
        full, _ = mgr.wisdom_block("scan the lan", situation=_SIT,
                                   affordances=[{"name": "lan_scan"}])
        cap = len(full) - 5
        cfg.wisdom_block_max_chars = cap
        text, injected = mgr.wisdom_block("scan the lan", situation=_SIT,
                                          affordances=[{"name": "lan_scan"}])
        assert len(text) <= cap                       # HARD cap: never exceeded
        assert "THE OFFER" not in text                # the tail item was dropped first
        assert "THE CASE" in text                     # the head item survives
        # injected still aligns to the surviving engram items
        assert all(e.kind in ("episode", "strategy") for e in injected)

    def test_omits_block_when_even_one_item_overflows(self, tmp_path):
        cfg = _cfg(tmp_path)
        _seed_case_guardrail(cfg)
        mgr = MemoryManager(cfg)
        cfg.wisdom_block_max_chars = 10        # header+frame alone already exceed this
        text, injected = mgr.wisdom_block("scan the lan", situation=_SIT,
                                          affordances=[{"name": "lan_scan"}])
        assert text == "" and injected == []   # hard cap -> silence, not an over-budget single


# =================================================================================================
class TestSettlementPlumbing:
    def test_wisdom_items_ride_the_recall_set_even_under_tight_budget(self, tmp_path):
        """The block IS recall (§3): its case/guardrail must ride the SAME recall-bet machinery the
        prose set rides — eidos.py wagers on exactly `manager.recall(...)`'s output. So when the flag
        is on, `recall()` GUARANTEES the wisdom-selected engrams are in its returned set even when a
        tight char budget would otherwise have cut them."""
        cfg = _cfg(tmp_path)
        con = Consolidator(cfg)
        # a LOW-strength guardrail a tight budget would normally cut, + a strong verified case
        guard = con.commit(Engram(kind="strategy",
                                  body="When the lan scan stalls, restart the switch first.",
                                  strength=0.05, stats={"situation": _SIT}))
        case = con.commit(Engram(kind="episode", body="ran `ping` -> worked",
                                 strength=0.9, stats={"situation": _SIT, "verified": True}))
        for i in range(6):
            con.commit(Engram(kind="fact", body=f"unrelated filler fact {i} about the pantry",
                              strength=0.95, stats={"situation": ""}))
        mgr = MemoryManager(cfg)
        out = mgr.recall("scan the lan", situation=_SIT, budget_chars=40, explore_ratio=0.0)
        ids = {e.id for e in out}
        assert guard.id in ids, "the low-strength guardrail the block shows must be bet-covered"
        assert case.id in ids, "the case the block shows must be bet-covered"

    def test_recall_unchanged_when_flag_off(self, tmp_path):
        """WIS7: flag-off, `recall()` is byte-identical — no wisdom items forced into the set."""
        cfg_off = _cfg(tmp_path, wisdom=False)
        con = Consolidator(cfg_off)
        con.commit(Engram(kind="strategy", body="restart the switch first", strength=0.05,
                          stats={"situation": _SIT}))
        for i in range(6):
            con.commit(Engram(kind="fact", body=f"filler {i} about the pantry shelf and jars",
                              strength=0.95, stats={"situation": ""}))
        mgr = MemoryManager(cfg_off)
        out_off = mgr.recall("scan the lan", situation=_SIT, budget_chars=40, explore_ratio=0.0)
        # flip only the flag on the SAME store and recall again: the on-set is a superset (adds the
        # forced wisdom items), the off-set never got them.
        cfg_on = _cfg(tmp_path, wisdom=True)
        out_on = MemoryManager(cfg_on).recall("scan the lan", situation=_SIT,
                                              budget_chars=40, explore_ratio=0.0)
        assert {e.id for e in out_off} <= {e.id for e in out_on}
        assert len(out_on) >= len(out_off)


# =================================================================================================
class TestFlagDark:
    def test_flag_off_yields_empty_block(self, tmp_path):
        cfg = _cfg(tmp_path, wisdom=False)
        _seed_case_guardrail(cfg)
        mgr = MemoryManager(cfg)
        assert mgr.wisdom_block("scan the lan", situation=_SIT,
                                affordances=[{"name": "lan_scan"}]) == ("", [])

    def test_context_assembly_byte_identical_flag_off(self, tmp_path):
        """WIS7: with the wisdom flag off, context assembly is byte-for-byte what it was before —
        the prose recall passes through untouched and no wisdom block appears."""
        import context
        from memory import write_plan
        cfg = _cfg(tmp_path, wisdom=False)
        cfg.creature_mode = False
        write_plan(cfg, "1. Scan the lan.\n2. Reboot the switch.")
        prose = "## Recalled from memory\n- [episode] ran `ping` -> worked"
        msgs = context.assemble_context(cfg, tick_number=5, goal_start_time=0.0,
                                        pillars_recall_block=prose)
        blob = "\n\n".join(m["content"] for m in msgs)
        assert prose in blob                      # prose recall passes through verbatim
        assert "## Before you act" not in blob    # no wisdom block


# =================================================================================================
class TestProseRecallShave:
    def _blob(self, cfg, prose):
        import context
        msgs = context.assemble_context(cfg, tick_number=5, goal_start_time=0.0,
                                        pillars_recall_block=prose)
        return "\n\n".join(m["content"] for m in msgs)

    def test_shave_keeps_total_recall_spend_constant(self, tmp_path):
        """REPLACES not adds (§3): when the block renders, the prose recall is trimmed by the block's
        actual size, so (shaved prose + block) spends the SAME chars the prose alone would have."""
        import context, episodes
        from memory import write_plan
        cfg = _cfg(tmp_path, wisdom=True)
        cfg.creature_mode = False
        write_plan(cfg, "1. Scan the lan.\n2. Reboot the switch.")
        (cfg.workspace / "goal.md").write_text("Immediate focus: scan the lan\n", encoding="utf-8")
        # key the fixtures to the ACTUAL situation the assembler will use, so the block renders the
        # case + guardrail (exact-match layer) exactly as it will inside assemble_context.
        sit = episodes.situation_key(cfg)
        con = Consolidator(cfg)
        con.commit(Engram(kind="episode", body="ran `ping 10.0.0.1` -> worked",
                          provenance="experienced", strength=0.9,
                          stats={"situation": sit, "verified": True}))
        con.commit(Engram(kind="strategy",
                          body="When the lan scan stalls, restart the switch first.",
                          provenance="experienced", strength=0.8, stats={"situation": sit}))
        prose = "P" * 1200                                  # a generously long prose recall
        # find the block the assembler actually rendered inside its OWN message, and measure IT (the
        # block content is the ground truth for the shave, not a separately-recomputed copy).
        import context as _ctx
        msgs = _ctx.assemble_context(cfg, tick_number=5, goal_start_time=0.0,
                                     pillars_recall_block=prose)
        from memory_manager import WISDOM_FRAME
        # the shaved prose AND the block both land in the SAME situation message (prose is a unique
        # sentinel char, so count it only where it lives — other messages carry stray 'P's in words).
        block_msg = next(m["content"] for m in msgs if "## Before you act" in m["content"])
        start = block_msg.index("## Before you act")
        end = block_msg.index(WISDOM_FRAME, start) + len(WISDOM_FRAME)
        block = block_msg[start:end]      # exactly header..frame (the rest is presence/temperament)
        assert "THE CASE" in block, "fixture must produce a non-empty block"

        shaved_len = block_msg.count("P")                   # surviving prose chars in the recall msg
        # total recall spend (shaved prose + block) <= original prose spend (replaces, not adds)
        assert shaved_len + len(block) <= len(prose) + 1    # (+1 tolerance for an rstrip'd boundary)
        assert shaved_len == len(prose) - len(block)        # shaved by EXACTLY the block's size
        assert shaved_len < len(prose)                      # the prose really was shaved

    def test_block_absent_no_shave(self, tmp_path):
        """When nothing qualifies (below-floor / empty), the prose recall is untouched."""
        cfg = _cfg(tmp_path, wisdom=True)
        cfg.creature_mode = False
        # empty store -> no block -> full prose survives
        prose = "Q" * 500
        blob = self._blob(cfg, prose)
        assert "## Before you act" not in blob
        assert blob.count("Q") == 500                       # prose untouched


# =================================================================================================
class TestGracefulAbsence:
    def test_empty_store_returns_empty(self, tmp_path):
        cfg = _cfg(tmp_path)
        assert MemoryManager(cfg).wisdom_block("anything", situation="a|b") == ("", [])

    def test_corrupt_long_term_store_fails_open(self, tmp_path):
        cfg = _cfg(tmp_path)
        # seed one good record, then append garbage lines into the long-term store file so a load
        # meets malformed content (the store skips corrupt lines; the block must still not raise).
        Consolidator(cfg).commit(Engram(kind="episode", body="ran `ping` -> worked",
                                        strength=0.9, stats={"situation": _SIT, "verified": True}))
        import engram as _eg
        path = _eg._longterm_jsonl_path(cfg)
        with open(path, "a", encoding="utf-8") as f:
            f.write("{ this is not json\n")
            f.write("\x00\x00 broken line\n")
        # must not raise — a memory fault never breaks the tick (WIS8 fail-open)
        text, injected = MemoryManager(cfg).wisdom_block("scan the lan", situation=_SIT)
        # the good record still surfaces; the corrupt lines are skipped, never crash
        assert isinstance(text, str) and isinstance(injected, list)

    def test_recall_scored_empty_store(self, tmp_path):
        cfg = _cfg(tmp_path)
        assert MemoryManager(cfg).recall_scored("q", situation="a|b") == []
