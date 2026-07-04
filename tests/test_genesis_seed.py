"""The genesis reseed (seed_genesis_quests.py) — the approved TOOL_PROGRESSION ladder as DATA.

Pins (red gates for the ladder's data layer; the grant machinery is unlocks.py's suite):
  - three quests in ladder order, the System's register kept, no expiry, idempotent seeding;
  - issuance-grant bindings: skillcraft → foresight → resolve (grants_unit, persisted);
  - criteria are ADJUDICATED facts — a skill LIVE in the manifest, a bet IN the ledger, a
    glue-marked completed goal — never `tools_used` attempt counters (which increment on FAILED
    calls; genesis-01 once passed on a failed create_skill call);
  - genesis-03 pays BOTH legs: the workshop unlock (grant seam) and 50 XP (standard sink path);
  - a directive may name the tool its OWN issuance grants (the window IS the grant) but never a
    later rung's tool — a locked tool does not exist in the creature's world.

No services / tick loop / GPU — temp workspaces only.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import quests
from quests import Criterion, System, reward_xp_amount
from seed_genesis_quests import GENESIS


class _Config:
    """Minimal Config stand-in: only the two path properties quests.py reads."""
    def __init__(self, root: Path):
        self.workspace = root / "workspace"
        self.workspace.mkdir(parents=True, exist_ok=True)

    @property
    def state_dir(self) -> Path:
        return self.workspace / "state"


def _by_id() -> dict:
    return {q.id: q for q in GENESIS}


# =================================================================================================
class TestLadderShape:
    def test_three_quests_in_ladder_order(self):
        assert [q.id for q in GENESIS] == [
            "genesis-01-first-light", "genesis-02-a-wager", "genesis-03-make-it-yours"]

    def test_system_register_and_no_expiry(self):
        for q in GENESIS:
            assert q.directive.startswith("[SYSTEM]")   # the System's terse register
            assert q.expiry_ts is None                  # no newborn fails its introduction by clock
            assert q.tier == 1 and q.kind == "quest" and not q.hidden

    def test_issuance_grant_bindings(self):
        # The window that names the tool IS the grant — one unit per rung, ladder order (U2/U3/U5).
        assert [q.grants_unit for q in GENESIS] == ["skillcraft", "foresight", "resolve"]


# =================================================================================================
class TestCriteriaAreAdjudicatedFacts:
    def test_no_attempt_counters_anywhere(self):
        # The closed hole: tools_used increments on FAILED calls too — no genesis criterion may
        # read it. Walk every leaf of every criterion tree.
        def paths(c: Criterion):
            for sub in (c.all_of or []) + (c.any_of or []):
                yield from paths(sub)
            if c.all_of is None and c.any_of is None:
                yield c.path
        for q in GENESIS:
            for path in paths(q.success_criteria):
                assert "tools_used" not in path, f"{q.id} adjudicates an attempt counter: {path}"

    def test_genesis_01_needs_a_live_skill_and_lived_ticks(self):
        c = _by_id()["genesis-01-first-light"].success_criteria
        # Five FAILED create_skill attempts buy nothing; a skill LIVE in the manifest passes.
        attempts = {"persona": {"total_ticks": 50, "tools_used": {"create_skill": 5}},
                    "skills": {"live_count": 0, "trusted_count": 0}}
        assert not c.check(attempts)
        assert c.check({"persona": {"total_ticks": 10}, "skills": {"live_count": 1}})
        assert not c.check({"persona": {"total_ticks": 9}, "skills": {"live_count": 1}})
        assert not c.check({"persona": {"total_ticks": 10}})    # absent stat never passes

    def test_genesis_02_needs_a_bet_in_the_ledger(self):
        c = _by_id()["genesis-02-a-wager"].success_criteria
        assert not c.check({"persona": {"tools_used": {"predict": 3}}})   # attempts buy nothing
        assert not c.check({"expectations": {"total": 0}})
        assert c.check({"expectations": {"total": 1}})

    def test_genesis_03_needs_a_completed_goal(self):
        c = _by_id()["genesis-03-make-it-yours"].success_criteria
        assert not c.check({"persona": {"goals_completed": 0}})
        assert c.check({"persona": {"goals_completed": 1}})


# =================================================================================================
class TestRewards:
    def test_early_rungs_pay_25_xp(self):
        for qid in ("genesis-01-first-light", "genesis-02-a-wager"):
            assert _by_id()[qid].reward == {"kind": "xp", "amount": 25}

    def test_genesis_03_pays_both_legs(self):
        r = _by_id()["genesis-03-make-it-yours"].reward
        assert r["kind"] == quests.REWARD_UNLOCK and r["what"] == "workshop"
        assert reward_xp_amount(r) == 50                       # the XP leg the sink pays
        assert quests._reward_str(r) == "unlock: workshop +50 XP"   # the window states both


# =================================================================================================
class TestDirectiveDiscipline:
    """A directive may name its own issuance's tool, never a later rung's (invisible doors)."""

    LATER_RUNG_WORDS = {
        # genesis-01 grants skillcraft; foresight/resolve/workshop tools are later rungs for it.
        "genesis-01-first-light": ("predict", "objective", "delegate", "speak", "vision"),
        # genesis-02 grants foresight (may say `predict`); resolve/workshop remain later.
        "genesis-02-a-wager": ("objective", "delegate", "speak", "vision"),
        # genesis-03 grants resolve (may say `objective_add`); only workshop's tool is later.
        "genesis-03-make-it-yours": ("delegate", "speak", "vision"),
    }

    def test_own_grant_may_be_named(self):
        byid = _by_id()
        assert "create_skill" in byid["genesis-01-first-light"].directive
        assert "predict" in byid["genesis-02-a-wager"].directive
        assert "objective_add" in byid["genesis-03-make-it-yours"].directive

    def test_no_later_rung_tool_is_named(self):
        for qid, banned in self.LATER_RUNG_WORDS.items():
            directive = _by_id()[qid].directive.lower()
            for word in banned:
                assert word not in directive, f"{qid} names a later rung's tool: {word}"


# =================================================================================================
class TestSeeding:
    def test_seed_is_idempotent_and_queued(self, tmp_path):
        cfg = _Config(tmp_path)
        system = System(cfg)
        for _ in range(2):                       # a re-run must not duplicate the line
            for q in GENESIS:
                system.propose(q)
        stored = system.store.load()
        assert [q.id for q in stored] == [q.id for q in GENESIS]
        assert all(q.state == quests.OFFERED for q in stored)
        # The bindings and both reward legs survive the store round-trip.
        assert [q.grants_unit for q in stored] == ["skillcraft", "foresight", "resolve"]
        assert reward_xp_amount(stored[2].reward) == 50
        assert stored[2].reward["what"] == "workshop"

    def test_full_arc_pays_100_xp_through_the_standard_sink(self, tmp_path):
        # Walk the whole ladder offline: issue → adjudicate against honest fixture stats → pay.
        import persona as persona_mod
        cfg = _Config(tmp_path)
        p = persona_mod._default_persona()
        system = System(cfg, reward_sink=lambda c, q: quests.default_reward_sink(c, q, p))
        for q in GENESIS:
            system.propose(q)
        lived = {"persona": {"total_ticks": 11, "goals_completed": 1},
                 "skills": {"live_count": 1, "trusted_count": 0},
                 "expectations": {"total": 1}}
        start_xp = p["xp"]
        for _ in GENESIS:
            active = system.issue_next(sleeps_since_close=1, condition="STABLE")
            assert active is not None
            assert system.check(active, lived)["passed"]
        assert p["xp"] == start_xp + 25 + 25 + 50   # both 25s and genesis-03's XP leg
        assert system.store.passed_count() == 3
