"""Pillars cross-cutting: the simulated-days harness (tools/simdays.py) — the coupled-economy
damper gate required GREEN before any Phase 5.5 flag flip (PILLARS_TODO "Simulated-days harness").

One 50-day coupled run (module-scoped — the POINT is the economies running together, so every
damper asserts against the same run, not five isolated scenarios):
  - exploration survives decay: after 50 sleep cycles of strength decay + pruning, the recall
    exploration slot still surfaces low-strength engrams;
  - adenosine overrides pinned pressure: with goal-tension's drive floor held at its cap every
    tick of every day, the creature still sleeps before pillars_max_wake_hours;
  - springs recover a bad streak: written against the temperament-springs seam; SKIPPED with
    reason until 4.3 lands the mechanism (nervous/temperament.py has no baseline spring yet);
  - cadence never deadlocks: quests keep issuing and closing, sleeps keep completing, no
    starvation window;
  - no runaway account: every bounded store within its bound, no economy value railed
    (the named envelope in tools/simdays.py, each bound justified).

No GPU, no services, no wall clock — mock LLM only, all state under pytest temp dirs.
The 100-day standalone run (`tools/simdays.py --days 100 --seed 7`) is the manual/CI artifact;
tests use 50 days (the TODO's verbatim 50-sleep horizon) to keep the suite fast.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest

import learning_progress
from bets import BETS_PERSIST_MAX
from memory_manager import EXPLORE_STRENGTH_CEILING
from tools.simdays import (
    CADENCE_STARVATION_DAYS_MAX, EXPLORATION_GATE_SLEEPS, SIM_LONGTERM_BUDGET, SIM_RING_MAX,
    SPRINGS_RECOVERY_DAYS_MAX, SimCreature, main, springs_available, springs_scenario,
)

_DAYS = EXPLORATION_GATE_SLEEPS   # 50: the TODO's verbatim decay/prune horizon (one sleep per day)
_SEED = 7


@pytest.fixture(scope="module")
def sim(tmp_path_factory):
    """THE coupled run: 50 simulated days, every driven pillars flag on, one shared instance."""
    return SimCreature(tmp_path_factory.mktemp("simdays"), seed=_SEED).run(_DAYS)


# ==================================================================================================
# The rig itself
# ==================================================================================================
def test_rig_drives_real_libraries_with_flags_on(sim):
    """The sim runs the REAL code paths, not dark no-ops: every driven flag is on and every
    library actually persisted state (a dark gate would have written nothing)."""
    cfg = sim.config
    for flag in ("pillars_memory_engram_enabled", "pillars_memory_manager_enabled",
                 "pillars_sleep_engine_enabled", "pillars_bet_ledger_enabled",
                 "pillars_learning_xp_enabled", "pillars_quests_enabled",
                 "pillars_news_enabled"):
        assert getattr(cfg, flag) is True
    assert (cfg.state_dir / "bets.jsonl").exists()          # the ledger settled for real
    assert (cfg.workspace / "quests.jsonl").exists()        # the System issued for real
    assert (cfg.state_dir / "news_queue.json").exists()     # the queue ingested for real
    assert (cfg.state_dir / "learning_progress.json").exists()
    assert len(sim.consolidator.store.load()) > 0           # long-term memory exists
    assert sim.mind.calls >= _DAYS                          # the (mock) mind dreamed every night
    # The sim never touches the real workspace/ — everything lives under the pytest tmp dir.
    assert str(sim.workdir) in str(cfg.workspace)


def test_sleep_engine_ran_real_jobs_every_night(sim):
    """Every day ended in a REAL SleepEngine pass whose jobs all ran clean (I5 guards unused)."""
    assert sim.sleeps == _DAYS
    assert all(d.sleep_ok for d in sim.days), \
        [f"day {d.day}: {d.sleep_failed_jobs}" for d in sim.days if not d.sleep_ok]


# ==================================================================================================
# Damper 1 — exploration survives decay (TODO: "after 50 sleep cycles of strength decay +
# pruning, the recall exploration slot still surfaces low-strength engrams")
# ==================================================================================================
def test_exploration_survives_decay(sim):
    assert sim.sleeps >= EXPLORATION_GATE_SLEEPS
    # Decay + prune genuinely happened: the store was pruned to its budget and the un-credited
    # mass decayed (mean strength well below the 0.5 seed).
    assert any(d.lt_size >= SIM_LONGTERM_BUDGET for d in sim.days), "prune-to-budget never fired"
    assert sim.days[-1].mean_strength < 0.45
    # The slot mechanism still works: pure ranking vs the exploration-slotted recall differ, and
    # the promoted engram is genuinely low-strength (the anti-Matthew slot is not impotent).
    probe = sim.exploration_probe()
    assert probe["ok"], probe
    assert probe["promoted_strength"] <= EXPLORE_STRENGTH_CEILING
    # And low-strength engrams still actually reach recall sets in the final days.
    tail = sim.days[-3:]
    assert sum(d.low_strength_recalls for d in tail) > 0
    v = next(x for x in sim.verdicts() if x.name == "exploration_survives_decay")
    assert v.passed, v.note


# ==================================================================================================
# Damper 2 — adenosine overrides pinned pressure (TODO: "a creature held at max goal-tension
# still enters sleep before pillars_max_wake_hours")
# ==================================================================================================
def test_adenosine_overrides_pinned_pressure(sim):
    ceiling = float(sim.config.pillars_max_wake_hours)
    for d in sim.days:
        assert d.wake_hours < ceiling, \
            f"day {d.day} stayed awake {d.wake_hours}h >= ceiling {ceiling}h"
    # The pressure was genuinely pinned: the drive floor sat at its cap when each day ended.
    assert sim.nm.drive_floor == pytest.approx(sim.nm.drive_floor_cap)
    v = next(x for x in sim.verdicts() if x.name == "adenosine_overrides_pinned_pressure")
    assert v.passed, v.note


# ==================================================================================================
# Damper 3 — springs recover a bad streak (seam test; skip-with-reason until 4.3 lands)
# ==================================================================================================
def test_springs_recover_bad_streak(sim):
    if not springs_available():
        pytest.skip(
            "4.3 temperament setpoint springs not landed on this base: nervous/temperament.py "
            "has no baseline/spring seam (a neutral tick leaves the axes frozen by design). "
            "springs_scenario() is written against the seam and activates the day 4.3 lands.")
    r = springs_scenario(sim.config)
    assert r["spiked"] > r["baseline"], "the losing streak failed to spike caution"
    assert r["recovered"], (
        f"caution never relaxed to within 0.1 of baseline {r['baseline']:.2f} inside "
        f"{SPRINGS_RECOVERY_DAYS_MAX} days (final {r['final']:.2f})")


# ==================================================================================================
# Damper 4 — cadence never deadlocks (TODO: "quest cadence (close + sleep + healthy) and sleep
# pressure never reach a state where neither can proceed")
# ==================================================================================================
def test_cadence_never_deadlocks(sim):
    assert sim.sleeps == len(sim.days), "sleeps stopped completing"
    assert sim.quests_closed > 0, "no quest ever closed"
    assert sim.quests_closed + sim.quests_expired >= len(sim.days) // 6, "quest issuance starved"
    assert sim.max_questless_streak <= CADENCE_STARVATION_DAYS_MAX, \
        f"questless streak {sim.max_questless_streak} days"
    assert not sim.cadence_violations, sim.cadence_violations
    # The healthy-gate was actually exercised (RECOVERY days occurred) and cadence resumed after.
    assert any(d.condition == "RECOVERY" for d in sim.days)
    v = next(x for x in sim.verdicts() if x.name == "cadence_never_deadlocks")
    assert v.passed, v.note


# ==================================================================================================
# Damper 5 — no runaway account (the named envelope: every bound held, nothing railed)
# ==================================================================================================
def test_no_runaway_account(sim):
    assert not sim.envelope_violations, sim.envelope_violations[:8]
    last = sim.days[-1]
    # The bounds were EXERCISED, not just respected: each bounded store actually reached the
    # regime its bound governs (a bound never approached proves nothing).
    assert last.ring_len == SIM_RING_MAX          # FIFO eviction ran
    assert last.bets_rows == BETS_PERSIST_MAX     # the bet log trimmed
    assert last.lt_size <= SIM_LONGTERM_BUDGET + 32 and \
        max(d.lt_size for d in sim.days) >= SIM_LONGTERM_BUDGET   # prune ran
    assert last.domains <= learning_progress.MAX_DOMAINS
    assert last.news_items <= sim.config.pillars_news_max_items
    v = next(x for x in sim.verdicts() if x.name == "no_runaway_account")
    assert v.passed, v.note


def test_learning_progress_pays_slope_not_volume(sim):
    """The coupled XP economy behaves as 4.2 promises inside the full rig: early days (frontier
    falling) pay more tick-XP than late days (mastered/noise pay ~0; late XP is mostly quest
    payout). Volume stayed constant — ticks/day never changed — so any fall is the weighting."""
    early = sum(d.xp for d in sim.days[:5])
    late = sum(d.xp for d in sim.days[-5:])
    assert late < early, f"XP did not fall with mastery: early={early} late={late}"


# ==================================================================================================
# Determinism + the report artifact
# ==================================================================================================
def test_determinism_same_seed(tmp_path):
    a = SimCreature(tmp_path / "a", seed=11).run(2)
    b = SimCreature(tmp_path / "b", seed=11).run(2)

    def sig(s):
        return [(d.day, d.wake_ticks, d.successes, d.xp, d.lt_size, d.ring_len,
                 d.bets_rows, d.news_items, d.quests_closed_cum) for d in s.days] + \
               [s.total_xp, s.total_successes]
    assert sig(a) == sig(b)


def test_report_is_the_phase55_artifact(tmp_path, capsys):
    """`tools/simdays.py --days N --seed S` standalone prints the per-day telemetry table plus a
    final PASS/FAIL per damper and exits 0 when no damper FAILs (SKIP is not a failure)."""
    rc = main(["--days", "2", "--seed", "3", "--workdir", str(tmp_path / "cli")])
    out = capsys.readouterr().out
    assert rc == 0
    assert "day wake_h" in out                       # the telemetry table header
    assert out.count("\n  1") >= 1 and "  2 " in out  # one row per day
    for damper in ("exploration_survives_decay", "adenosine_overrides_pinned_pressure",
                   "springs_recover_bad_streak", "cadence_never_deadlocks",
                   "no_runaway_account"):
        assert damper in out
    assert "RESULT:" in out
