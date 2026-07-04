"""Pillars 4.3: mastery gates (level_gates.py + the temperament setpoint springs) — offline unit tests.

Red-able gates (PILLARS_TODO 4.3):
  - a save with HIGH XP but ZERO trusted skills cannot level (evidence beats volume);
  - a suspension + remedial completion restores the tier;
  - caution recovers toward baseline after an induced bad streak (spring, bounded steps);
  - delegated-marked outcomes never feed a gate counter (pitfall #8);
  - the sleep-cycle floor is enforced; flag off → persona/temperament behavior byte-identical.

No services / tick loop / GPU — temp workspaces only.
"""

import copy
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import level_gates
from level_gates import (
    GateState, apply_level_up, can_level, record_remedial_completion, record_sleep_cycle,
    record_tier_outcome, tier_of_level, xp_for_level, SUSPEND_AFTER_FAILURES, TRUSTED_PER_TIER,
)
import persona as persona_mod
import quests
import skills as skills_mod
from nervous.temperament import Temperament, GENOME_BASELINE


class _Config:
    """Minimal Config stand-in: the paths + flags level_gates and its evidence sources read."""
    def __init__(self, root: Path, *, gates_on: bool = True):
        self.workspace = root / "workspace"
        self.workspace.mkdir(parents=True, exist_ok=True)
        self.pillars_mastery_gates_enabled = gates_on
        self.pillars_min_sleeps_per_level = 3

    @property
    def state_dir(self) -> Path:
        return self.workspace / "state"


def _cfg(tmp_path, **kw) -> _Config:
    return _Config(tmp_path, **kw)


def _manifest_with(config, entries: dict) -> None:
    skills_mod._save_manifest(config, {"skills": entries})


def _trusted_skill(inv: int = 3) -> dict:
    return {"status": "trusted", "tier": 1, "invocations": inv, "successes": inv}


def _sleep(config, n: int = 1) -> None:
    for _ in range(n):
        record_sleep_cycle(config)


# =================================================================================================
class TestTierMath:
    def test_tier_curve_and_xp_inverse(self):
        assert tier_of_level(1) == 1 and tier_of_level(2) == 1
        assert tier_of_level(3) == 2 and tier_of_level(4) == 2
        # xp_for_level inverts persona.compute_level exactly at the boundary.
        for lvl in (2, 3, 5, 8):
            assert persona_mod.compute_level(xp_for_level(lvl)) == lvl
            assert persona_mod.compute_level(xp_for_level(lvl) - 1) == lvl - 1


# =================================================================================================
class TestEvidenceBeatsVolume:
    def test_high_xp_zero_trusted_cannot_level(self, tmp_path):
        cfg = _cfg(tmp_path)
        _sleep(cfg, 3)
        persona = {"xp": 100_000, "level": 1}          # a volume-clock fortune...
        ok, report = can_level(persona, cfg)
        assert not ok                                   # ...buys nothing without evidence
        assert report["checks"]["xp_floor"]["ok"]       # XP was never the blocker
        assert not report["checks"]["trusted_in_tier"]["ok"]
        assert apply_level_up(persona, cfg)["applied"] is False
        assert persona["level"] == 1

    def test_full_evidence_levels_exactly_one(self, tmp_path):
        cfg = _cfg(tmp_path)
        _manifest_with(cfg, {"a": _trusted_skill(), "b": _trusted_skill()})
        _sleep(cfg, 3)
        persona = {"xp": xp_for_level(2), "level": 1}
        ok, report = can_level(persona, cfg)
        assert ok, report
        assert apply_level_up(persona, cfg)["applied"] is True
        assert persona["level"] == 2
        # The sleep counter reset: the next level needs fresh digestion (spacing effect).
        assert not can_level(persona, cfg)[0]
        assert GateState(cfg).sleeps_since_level == 0

    def test_sleep_floor_blocks(self, tmp_path):
        cfg = _cfg(tmp_path)
        _manifest_with(cfg, {"a": _trusted_skill(), "b": _trusted_skill()})
        _sleep(cfg, 2)                                  # one short of the declared floor (3)
        persona = {"xp": xp_for_level(2), "level": 1}
        ok, report = can_level(persona, cfg)
        assert not ok and not report["checks"]["sleep_cycles"]["ok"]

    def test_open_quest_line_blocks(self, tmp_path):
        cfg = _cfg(tmp_path)
        _manifest_with(cfg, {"a": _trusted_skill(), "b": _trusted_skill()})
        _sleep(cfg, 3)
        q = quests.Quest(id="q1", directive="test",
                         success_criteria=quests.Criterion(path="persona.level", op=">=", value=99))
        q.state = quests.ACTIVE
        quests.QuestStore(cfg).save([q])
        persona = {"xp": xp_for_level(2), "level": 1}
        ok, report = can_level(persona, cfg)
        assert not ok and not report["checks"]["quest_line_closed"]["ok"]


# =================================================================================================
class TestSuspension:
    def test_suspension_then_remedial_restores(self, tmp_path):
        cfg = _cfg(tmp_path)
        _manifest_with(cfg, {"a": _trusted_skill(), "b": _trusted_skill()})
        _sleep(cfg, 3)
        persona = {"xp": xp_for_level(2), "level": 1}

        remedial_id = None
        for _ in range(SUSPEND_AFTER_FAILURES):
            remedial_id = record_tier_outcome(cfg, 1, False)
        assert remedial_id                              # the suspension fired on the Nth failure
        assert "1" in GateState(cfg).suspended
        # The remedial quest was proposed through the System's seam (state=offered).
        assert any(q.id == remedial_id for q in quests.QuestStore(cfg).queue())
        # A suspended tier blocks leveling...
        ok, report = can_level(persona, cfg)
        assert not ok and not report["checks"]["no_suspensions"]["ok"]
        # ...and the remedial's completion restores standing.
        assert record_remedial_completion(cfg, 1) is True
        assert can_level(persona, cfg)[0] is True

    def test_success_resets_the_streak(self, tmp_path):
        cfg = _cfg(tmp_path)
        for _ in range(SUSPEND_AFTER_FAILURES - 1):
            record_tier_outcome(cfg, 1, False)
        record_tier_outcome(cfg, 1, True)               # sustained means CONSECUTIVE
        assert record_tier_outcome(cfg, 1, False) is None
        assert "1" not in GateState(cfg).suspended

    def test_delegated_outcomes_never_count(self, tmp_path):
        cfg = _cfg(tmp_path)
        for _ in range(SUSPEND_AFTER_FAILURES * 2):     # an army's worth of delegated failures
            assert record_tier_outcome(cfg, 1, False, delegated=True) is None
        assert GateState(cfg).suspended == {}           # pitfall #8: levels are personal
        assert GateState(cfg).failures.get("1", 0) == 0


# =================================================================================================
class TestPersonaHook:
    def test_flag_off_award_xp_byte_identical(self, tmp_path):
        cfg = _cfg(tmp_path, gates_on=False)
        legacy = {"xp": 0, "level": 1}
        gated = copy.deepcopy(legacy)
        persona_mod.award_xp(legacy, 5000)              # legacy: no config at all
        persona_mod.award_xp(gated, 5000, config=cfg)   # flag off: config present, gate silent
        assert gated == legacy and gated["level"] > 1   # volume clock still ticks when dark

    def test_flag_on_xp_accrues_but_level_holds(self, tmp_path):
        cfg = _cfg(tmp_path)
        persona = {"xp": 0, "level": 1}
        persona_mod.award_xp(persona, 100_000, config=cfg)
        assert persona["xp"] == 100_000
        assert persona["level"] == 1                    # only apply_level_up moves it now


# =================================================================================================
class TestSetpointSprings:
    def _streaked(self, cfg, n_fail=60):
        t = Temperament(cfg)
        for _ in range(n_fail):
            t.observe(success=False, failed=True, overridden=False)
        return t

    def test_caution_recovers_toward_baseline(self, tmp_path):
        cfg = _cfg(tmp_path)
        t = self._streaked(cfg)
        spiked = t.caution
        assert spiked > t.baselines["caution"] + 0.1    # the streak genuinely moved it
        for _ in range(400):                            # neutral ticks: nothing happens but time
            t.observe(success=False, failed=False, overridden=False)
        assert t.caution < spiked                       # the spring relaxed it...
        # ...most of the way back to THIS creature's congenital baseline (drawn at birth,
        # GENOME_BASELINE ± BIRTH_SPREAD — the divergence mechanism).
        assert abs(t.caution - t.baselines["caution"]) < 0.1
        assert abs(t.baselines["caution"] - GENOME_BASELINE) <= 0.081

    def test_flag_off_neutral_ticks_leave_axes_untouched(self, tmp_path):
        cfg = _cfg(tmp_path, gates_on=False)
        t = self._streaked(cfg)
        spiked = (t.initiative, t.persistence, t.caution)
        for _ in range(400):
            t.observe(success=False, failed=False, overridden=False)
        assert (t.initiative, t.persistence, t.caution) == spiked   # legacy: no spring, no drift

    def test_experience_outpulls_the_spring(self, tmp_path):
        cfg = _cfg(tmp_path)
        t = Temperament(cfg)
        for _ in range(60):                             # a real streak still teaches under the spring
            t.observe(success=False, failed=True, overridden=False)
        assert t.caution > GENOME_BASELINE + 0.1
