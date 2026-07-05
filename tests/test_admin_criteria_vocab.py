"""The criteria-vocabulary fix: the Administrator writes success criteria against the engine's thin
adjudicatable rulebook, not the rich dossier it reasons from.

Found live on the maiden walk: 20/20 LLM-authored Administrator proposals were un-passable bricks
because every one referenced dossier paths (skill_economy.authored, pitfall_health.longterm_fill,
level.sleeps_since_level, skill_economy.trusted_by_tier.1) that eidos._quest_stats never exposes.
A quest whose criteria can't resolve sits ACTIVE forever and freezes the mastery gate's
quest_line_closed check — a level brick.

The fix is one vocabulary (quests.ADJUDICATABLE_PATHS) enforced three ways:
  · DRIFT GUARD — every vocabulary path resolves in a real _quest_stats dict (rulebook ≡ referee);
  · GRAMMAR — the criteria `path` is an enum of the vocabulary, so a bad path is unrepresentable;
  · VALIDATOR — _valid_criteria rejects any out-of-vocabulary path, guarding both generation
    (parse drops the whole output) and approval (a bad edit is refused);
  · PROMPT — the vocabulary is named in the system message from the same registry.
And the hand-authored genesis quests must already speak this vocabulary.

No services / GPU — temp workspaces, mock llm, and a real _quest_stats build.
"""
import json
import sys
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest

import administrator
import eidos
import quests
from config import Config


def _cfg(tmp_path, **flags) -> Config:
    cfg = Config()
    cfg.workspace_dir = str(tmp_path / "workspace")
    cfg.mock_mode = True
    cfg.pillars_administrator_enabled = True
    cfg.pillars_mastery_gates_enabled = True
    for k, v in flags.items():
        setattr(cfg, k, v)
    cfg.workspace.mkdir(parents=True, exist_ok=True)
    cfg.state_dir.mkdir(parents=True, exist_ok=True)
    return cfg


# =================================================================================================
class TestDriftGuard:
    """The rulebook and the referee can never diverge again."""

    def test_every_vocab_path_resolves_in_quest_stats(self, tmp_path):
        cfg = _cfg(tmp_path)
        hub = eidos._Pillars(cfg)
        # a persona rich enough that every persona.* leaf is present
        persona = {"xp": 10, "level": 1, "goals_completed": 0, "total_ticks": 5}
        stats = hub._quest_stats(persona)
        for path in quests.ADJUDICATABLE_PATHS:
            val = quests._dig(stats, path)
            assert val is not None, f"{path} does not resolve in _quest_stats (rulebook drift)"

    def test_genesis_quests_speak_the_vocabulary(self):
        # The hand-authored genesis quests must already use only adjudicatable paths — they are
        # the proof the vocabulary is the real one (they pass live).
        import seed_genesis_quests as sgq
        assert sgq.GENESIS, "no genesis quests to check"
        for q in sgq.GENESIS:
            for p in quests.criteria_paths(q.success_criteria.to_dict()):
                assert p in quests.ADJUDICATABLE_PATHS, f"genesis quest uses non-vocab path {p}"

    def test_criteria_paths_walks_compounds(self):
        crit = {"all_of": [{"path": "skills.live_count", "op": ">=", "value": 1},
                           {"any_of": [{"path": "persona.xp", "op": ">=", "value": 5},
                                       {"path": "sleeps.total", "op": ">=", "value": 1}]}]}
        assert set(quests.criteria_paths(crit)) == {
            "skills.live_count", "persona.xp", "sleeps.total"}
        assert quests.criteria_paths(None) == []
        assert quests.criteria_paths({"op": ">="}) == []   # no path key


# =================================================================================================
class TestValidator:
    def test_rejects_dossier_paths(self):
        # The exact paths the 20 dead proposals used — all now invalid.
        for bad in ("skill_economy.authored", "pitfall_health.longterm_fill",
                    "level.sleeps_since_level", "skill_economy.trusted_by_tier",
                    "calibration_by_domain", "memorize"):
            assert not administrator._valid_criteria(
                {"path": bad, "op": ">=", "value": 1}), bad

    def test_accepts_vocabulary_paths(self):
        for good in quests.ADJUDICATABLE_PATHS:
            assert administrator._valid_criteria({"path": good, "op": ">=", "value": 1}), good

    def test_compound_with_one_bad_leaf_is_invalid(self):
        crit = {"all_of": [{"path": "skills.live_count", "op": ">=", "value": 1},
                           {"path": "skill_economy.authored", "op": ">=", "value": 1}]}
        assert not administrator._valid_criteria(crit)


# =================================================================================================
class TestGrammar:
    def test_path_is_an_enum_not_a_free_string(self):
        g = administrator.build_admin_grammar()
        assert "cpath ::=" in g
        # the leaf must use cpath for its path, not the free bstring
        assert "path" in g and "cpath" in g
        for p in quests.ADJUDICATABLE_PATHS:
            assert f'"{p}"' in g, f"grammar missing vocab path {p}"
        # a dossier path must NOT be emittable
        assert '"skill_economy.authored"' not in g


# =================================================================================================
class TestPrompt:
    def test_vocab_block_lists_every_path(self):
        block = administrator._criteria_vocab_block()
        for p in quests.ADJUDICATABLE_PATHS:
            assert p in block


# =================================================================================================
class TestEndToEnd:
    """Generation: a dossier-path criterion is dropped-with-log; a vocab-path one commits."""

    def _out(self, path):
        return json.dumps({
            "quests": [{"id": "q1", "directive": "Do the thing.", "tier": 1, "reward_xp": 10,
                        "expiry_hours": 0,
                        "criteria": {"path": path, "op": ">=", "value": 1}}],
            "weakness_report": "w", "narrator": "n", "tuning_flags": [],
        })

    def _llm(self, out):
        def llm(messages, grammar):
            llm.last = {"messages": messages, "grammar": grammar}
            return out
        return llm

    def test_dossier_path_proposal_is_dropped(self, tmp_path):
        cfg = _cfg(tmp_path)
        rep = administrator.check_in(cfg, self._llm(self._out("skill_economy.authored")),
                                     administrator.EVT_SLEEP_COMPLETE, persona={"level": 1})
        assert rep.dropped                                    # malformed → nothing committed
        assert administrator.pending_proposals(cfg) == []

    def test_vocab_path_proposal_commits(self, tmp_path):
        cfg = _cfg(tmp_path)
        rep = administrator.check_in(cfg, self._llm(self._out("skills.live_count")),
                                     administrator.EVT_SLEEP_COMPLETE, persona={"level": 1})
        assert not rep.dropped
        assert [p["id"] for p in administrator.pending_proposals(cfg)] == ["q1"]

    def test_the_prompt_carries_the_vocabulary(self, tmp_path):
        cfg = _cfg(tmp_path)
        llm = self._llm(self._out("skills.live_count"))
        administrator.check_in(cfg, llm, administrator.EVT_SLEEP_COMPLETE, persona={"level": 1})
        sysmsg = llm.last["messages"][0]["content"]
        assert "ADJUDICATABLE CRITERIA PATHS" in sysmsg
        assert "skills.trusted_count" in sysmsg
