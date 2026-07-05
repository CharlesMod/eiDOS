"""The dream-vs-nap split (the nap curve, completed): the BODY decides which sleep this was —
never a clock (ARCH #1).

An infant's context fills every few minutes, so the compaction window fires at dream frequency.
Before the split, every dream drove the whole sleep boundary: adenosine reset (so real naps never
arrived), sleeps_total inflated 13× in 90 minutes (so the mastery gates' spacing floor meant
nothing), and the Administrator burned a full dossier call per doze.

Pins:
  · DREAM (pressure < NAP_PRESSURE_MIN at the boundary): the memory jobs run and the System
    stays responsive (quest cadence + issuance), but the body keeps its tiredness (no adenosine
    clear), the gates' sleep counters do NOT advance, and sleep_complete does NOT fire.
  · NAP (pressure ≥ NAP_PRESSURE_MIN of the stage ceiling): everything the boundary always did —
    adenosine cleared, counters advance, sleep_complete fires.
  · No adenosine organ → NAP (fail-open to the pre-split semantics, byte-identical).
  · run_sleep(clear_adenosine=False) is the dream leg: jobs run, the accumulator stays.

No services / GPU — temp workspaces, mock mode only.
"""
import sys
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest

import eidos
import level_gates
from config import Config
from nervous.neuromod import Adenosine, NAP_PRESSURE_MIN


def _mk_config(tmp_path, **flags) -> Config:
    cfg = Config()
    cfg.workspace_dir = str(tmp_path / "workspace")
    cfg.mock_mode = True
    for k, v in flags.items():
        setattr(cfg, k, v)
    cfg.workspace.mkdir(parents=True, exist_ok=True)
    cfg.state_dir.mkdir(parents=True, exist_ok=True)
    return cfg


def _hub(tmp_path, *, adenosine=True, **extra):
    cfg = _mk_config(tmp_path, pillars_sleep_engine_enabled=True,
                     pillars_memory_engram_enabled=True, pillars_mastery_gates_enabled=True,
                     pillars_quests_enabled=True, **extra)
    nm = types.SimpleNamespace(adenosine=Adenosine()) if adenosine else None
    hub = eidos._Pillars(cfg, neuromod=nm)
    events = []
    hub._event = lambda kind, persona: events.append(kind)   # record, don't wake anything
    return cfg, hub, nm, events


def _boundary(hub):
    return hub.sleep_window(tick=5, persona={"level": 1, "xp": 0}, observations=[])


# =================================================================================================
class TestDream:
    def test_dream_keeps_the_body_tired_and_the_gates_still(self, tmp_path):
        cfg, hub, nm, events = _hub(tmp_path)
        nm.adenosine.accumulate(nm.adenosine.max_wake_hours * (NAP_PRESSURE_MIN / 2))
        before = nm.adenosine.level_hours
        cadence_before = hub.sleeps_since_close()
        report = _boundary(hub)
        assert report is not None and report.results          # the memory jobs DID run
        assert nm.adenosine.level_hours == pytest.approx(before)   # tiredness stays
        assert level_gates.GateState(cfg).sleeps_total == 0        # gates untouched
        assert level_gates.GateState(cfg).sleeps_since_level == 0
        assert hub.sleeps_since_close() == cadence_before + 1      # the System stays responsive
        assert "sleep_complete" not in events                      # the Administrator sleeps on


# =================================================================================================
class TestNap:
    def test_nap_rests_advances_and_wakes_the_administrator(self, tmp_path):
        cfg, hub, nm, events = _hub(tmp_path)
        nm.adenosine.accumulate(nm.adenosine.max_wake_hours * NAP_PRESSURE_MIN)  # exactly at it
        report = _boundary(hub)
        assert report is not None and report.results
        assert nm.adenosine.level_hours == 0.0                     # rested
        assert level_gates.GateState(cfg).sleeps_total == 1        # the spacing floor advances
        assert events.count("sleep_complete") == 1

    def test_no_adenosine_organ_fails_open_to_nap(self, tmp_path):
        cfg, hub, nm, events = _hub(tmp_path, adenosine=False)
        report = _boundary(hub)
        assert report is not None and report.results
        assert level_gates.GateState(cfg).sleeps_total == 1        # pre-split semantics
        assert events.count("sleep_complete") == 1


# =================================================================================================
class TestRunSleepLeg:
    def test_dream_leg_leaves_the_accumulator(self, tmp_path):
        from nervous.sleep import SleepContext, run_sleep
        cfg = _mk_config(tmp_path, pillars_sleep_engine_enabled=True)
        nm = types.SimpleNamespace(adenosine=Adenosine())
        nm.adenosine.accumulate(4.0)
        run_sleep(SleepContext(config=cfg, consolidator=None, neuromod=nm),
                  clear_adenosine=False)
        assert nm.adenosine.level_hours == pytest.approx(4.0)      # a dream does not rest
        run_sleep(SleepContext(config=cfg, consolidator=None, neuromod=nm))
        assert nm.adenosine.level_hours == 0.0                     # the default is still a nap
