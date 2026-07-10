"""Sleep-engine maintenance jobs (nervous/sleep.py): SkillRetireJob + CalibrationJob.

Gates (red-able):
  - a skill unused past the retirement window is archived BY THE SLEEP PASS (§N-3 job "S": the
    time-based half of skill maintenance runs offline, like every other forgetting);
  - chronic mis-calibration (closed claim-bearing bets, exposure-weighted Brier above the coin-flip
    neutral) moves temperament caution by ONE bounded step, never more (pitfall #3: clamp the step);
  - calibrated / evidence-poor / flag-off states move NOTHING;
  - both jobs sit in the default engine between telemetry and the organ hooks.

No services / tick loop / GPU / model — temp workspaces only.
"""

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from config import Config
from expectations import ExpectationLedger, close_prediction
from nervous.temperament import Temperament
from nervous.sleep import (
    SleepContext, default_sleep_engine,
    SkillRetireJob, CalibrationJob,
    CALIBRATION_CAUTION_STEP_MAX, CALIBRATION_MIN_CLOSED,
)


def _cfg(tmp_path) -> Config:
    cfg = Config()
    cfg.workspace_dir = str(tmp_path / "workspace")
    cfg.mock_mode = False
    cfg.pillars_sleep_engine_enabled = True
    cfg.workspace.mkdir(parents=True, exist_ok=True)
    return cfg


# =================================================================================================
# SkillRetireJob — the sleep pass archives what the economy declared stale
# =================================================================================================
class TestSkillRetireJob:
    def _seed_manifest(self, cfg, *, last_used: str) -> None:
        import skills
        m = {"skills": {"stale_skill": {
            "description": "an old helper nobody calls",
            "active_version": "1.0.0", "status": "active", "enabled": True,
            "created": "2000-01-01T00:00:00Z", "updated": "2000-01-01T00:00:00Z",
            "last_used": last_used, "invocations": 1, "successes": 1,
        }}}
        p = skills._manifest_path(cfg)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(m), encoding="utf-8")

    def test_stale_skill_retired_by_the_sleep_pass(self, tmp_path):
        import skills
        cfg = _cfg(tmp_path)
        cfg.pillars_skill_economy_enabled = True
        cfg.pillars_skill_retire_unused_days = 1.0
        self._seed_manifest(cfg, last_used="2000-01-01T00:00:00Z")
        summary = SkillRetireJob().run(SleepContext(config=cfg))
        assert summary == {"retired": 1, "names": ["stale_skill"]}
        m = json.loads(skills._manifest_path(cfg).read_text(encoding="utf-8"))
        assert m["skills"]["stale_skill"]["status"] == "retired"

    def test_economy_dark_means_no_op(self, tmp_path):
        import skills
        cfg = _cfg(tmp_path)
        cfg.pillars_skill_economy_enabled = False
        self._seed_manifest(cfg, last_used="2000-01-01T00:00:00Z")
        summary = SkillRetireJob().run(SleepContext(config=cfg))
        assert summary["retired"] == 0
        m = json.loads(skills._manifest_path(cfg).read_text(encoding="utf-8"))
        assert m["skills"]["stale_skill"]["status"] == "active"


# =================================================================================================
# CalibrationJob — Brier → one bounded caution step (expectations.py's sleep-job seam)
# =================================================================================================
def _close_bets(cfg, n: int, *, confidence: float, outcome: bool) -> None:
    """Place and glue-close `n` claim-bearing bets at the given confidence/outcome."""
    led = ExpectationLedger(cfg)
    deadline = time.time() + 3600
    for i in range(n):
        p = led.predict(statement=f"bet {i} on a checkable claim",
                        target=f"skills.total >= {i + 1}",
                        deadline=deadline, confidence=confidence)
        close_prediction(cfg, led, p, outcome=outcome, reason="deadline", tick=i)


class TestCalibrationJob:
    def _cfg(self, tmp_path) -> Config:
        cfg = _cfg(tmp_path)
        cfg.pillars_expectations_enabled = True
        cfg.pillars_mastery_gates_enabled = False   # no springs in-test: isolate the job's step
        return cfg

    def test_confidently_wrong_bets_raise_caution_one_bounded_step(self, tmp_path):
        cfg = self._cfg(tmp_path)
        _close_bets(cfg, CALIBRATION_MIN_CLOSED, confidence=0.95, outcome=False)
        t = Temperament(config=cfg)
        before = t.caution
        summary = CalibrationJob().run(SleepContext(config=cfg, temperament=t))
        assert summary["brier"] > 0.8                       # (0.95 − 0)² per bet
        moved = t.caution - before
        assert 0.0 < moved <= CALIBRATION_CAUTION_STEP_MAX + 1e-9
        # The calibration book is persisted for the dashboard regardless of the step.
        assert (cfg.state_dir / "calibration.json").exists()

    def test_well_calibrated_bets_move_nothing(self, tmp_path):
        cfg = self._cfg(tmp_path)
        _close_bets(cfg, CALIBRATION_MIN_CLOSED, confidence=0.9, outcome=True)
        t = Temperament(config=cfg)
        before = t.caution
        summary = CalibrationJob().run(SleepContext(config=cfg, temperament=t))
        assert summary["caution_step"] == 0.0
        assert t.caution == before

    def test_one_bet_is_noise_not_evidence(self, tmp_path):
        cfg = self._cfg(tmp_path)
        _close_bets(cfg, 1, confidence=0.95, outcome=False)
        t = Temperament(config=cfg)
        before = t.caution
        CalibrationJob().run(SleepContext(config=cfg, temperament=t))
        assert t.caution == before

    def test_expectations_dark_means_skip(self, tmp_path):
        cfg = _cfg(tmp_path)
        cfg.pillars_expectations_enabled = False
        summary = CalibrationJob().run(SleepContext(config=cfg))
        assert summary == {"skipped": "expectations dark"}

    def test_no_temperament_reports_without_moving(self, tmp_path):
        cfg = self._cfg(tmp_path)
        _close_bets(cfg, CALIBRATION_MIN_CLOSED, confidence=0.95, outcome=False)
        summary = CalibrationJob().run(SleepContext(config=cfg, temperament=None))
        assert summary["caution_step"] == 0.0
        assert summary["closed"] == CALIBRATION_MIN_CLOSED


# =================================================================================================
# The default engine carries both jobs, ordered between telemetry and the organ hooks
# =================================================================================================
def test_default_engine_runs_maintenance_before_organ_hooks():
    names = [j.name for j in default_sleep_engine().jobs]
    assert names.index("telemetry_rederive") < names.index("skill_retire")
    assert names.index("skill_retire") < names.index("calibration")
    assert names.index("calibration") < names.index("organ_on_sleep")
