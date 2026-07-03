"""Pillars 4.2: learning-progress XP (learning_progress.py + the flag-gated persona.award_xp
rewire) — offline unit tests.

The gate (PILLARS_TODO 4.2, verbatim):
  - replaying a recorded grind (identical action × 1000) yields ≈0 XP;
  - a recorded novel-success run yields XP;
  - a synthetic noise domain (coin-flip outcomes) pays ≈0 to both XP and curiosity
    (the restlessness signal);
plus, per the phase brief:
  - the error-recovery bonus is preserved (flat +5, never weighted);
  - flag off → persona.award_xp is byte-identical to the legacy path (asserted on the persona dict).

No services / tick loop / GPU — temp workspaces only.
"""

import copy
import json
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest

import learning_progress
import persona as persona_mod
from config import Config
from learning_progress import (
    MIN_OBSERVATIONS, NOVICE_WEIGHT, R2_GATE, WEIGHT_MAX, WINDOW, MAX_DOMAINS,
    ProgressTracker, domain_key, weighted_xp, award_adjudicated_xp,
)


# --- helpers -------------------------------------------------------------------------------------

def _cfg(tmp_path, *, enabled: bool = True) -> Config:
    cfg = Config()
    cfg.workspace_dir = str(tmp_path / "workspace")
    cfg.pillars_learning_xp_enabled = enabled
    return cfg


def _replay(cfg, domain, errors, base_xp, *, p=None):
    """Replay a recorded run: each adjudicated outcome reports its prediction error to the tracker,
    then its success pays base_xp through the flag-gated award path. Returns total XP earned."""
    p = p if p is not None else persona_mod._default_persona()
    tracker = ProgressTracker(cfg)
    start = p.get("xp", 0)
    for err in errors:
        tracker.observe(domain, err)
        persona_mod.award_xp(p, base_xp, "adjudicated success", domain=domain, config=cfg)
    return p.get("xp", 0) - start


# =================================================================================================
# The gate, part 1: a recorded grind (identical action × 1000) yields ≈0 XP
# =================================================================================================

class TestGrindPaysNothing:
    def test_mastered_grind_x1000(self, tmp_path):
        """Identical action × 1000 in a mastered domain (error 0 every time): low-and-flat pays
        nothing. Only the priceless warm-up (< MIN_OBSERVATIONS points, paid at the neutral novice
        rate) earns — the volume clock would have paid 1000."""
        cfg = _cfg(tmp_path)
        total = _replay(cfg, "grind", [0.0] * 1000, base_xp=1)
        assert total <= MIN_OBSERVATIONS          # warm-up only
        assert total < 0.01 * 1000                # ≈0 vs the volume clock

    def test_high_and_flat_grind(self, tmp_path):
        """Identical action with a CONSTANT nonzero error (high-and-flat): still unlearnable,
        still pays ≈0 — a constant series has no trend to pay."""
        cfg = _cfg(tmp_path)
        total = _replay(cfg, "flatline", [0.8] * 500, base_xp=10)
        assert total <= MIN_OBSERVATIONS * 10     # warm-up only
        assert total < 0.02 * (500 * 10)

    def test_grind_after_mastery_pays_exactly_zero(self, tmp_path):
        """Once the window is flat, every further identical rep pays exactly 0 — not merely
        'little'. The weight itself is 0."""
        cfg = _cfg(tmp_path)
        tracker = ProgressTracker(cfg)
        for _ in range(WINDOW):
            tracker.observe("grind", 0.0)
        assert tracker.progress_weight("grind") == 0.0
        assert weighted_xp(cfg, 100, "grind", tracker=tracker) == 0


# =================================================================================================
# The gate, part 2: a recorded novel-success run yields XP
# =================================================================================================

class TestNovelSuccessPays:
    def test_falling_error_run_pays(self, tmp_path):
        """A recorded novel-success run — prediction error falling from 1.0 toward 0 as the domain
        is learned (high-and-falling, the frontier) — earns substantial XP."""
        cfg = _cfg(tmp_path)
        run = [1.0 - i / 39.0 for i in range(40)]      # clean learning curve, 1.0 → 0.0
        total = _replay(cfg, "frontier", run, base_xp=10)
        assert total > 0
        assert total >= 0.5 * (40 * 10)     # the frontier pays at least half the volume rate...
        assert total <= WEIGHT_MAX * (40 * 10)   # ...and never beyond the declared ceiling

    def test_frontier_outearns_grind_and_noise(self, tmp_path):
        """The ordering the redesign exists for: same base pay, same length — the learning domain
        out-earns both the mastered grind and the noise domain by a wide margin."""
        cfg = _cfg(tmp_path)
        run = [max(0.0, 1.0 - i / 30.0) for i in range(60)]
        rng = random.Random(7)
        frontier = _replay(cfg, "frontier", run, base_xp=10)
        grind = _replay(cfg, "grind", [0.0] * 60, base_xp=10)
        noise = _replay(cfg, "noise", [float(rng.randint(0, 1)) for _ in range(60)], base_xp=10)
        assert frontier > 5 * grind
        assert frontier > 5 * noise

    def test_novice_domain_pays_neutral_base(self, tmp_path):
        """First contact with an unpriceable domain (< MIN_OBSERVATIONS) pays exactly the legacy
        base rate — neither punished nor jackpotted before a trend exists."""
        cfg = _cfg(tmp_path)
        tracker = ProgressTracker(cfg)
        tracker.observe("newland", 0.9)
        assert tracker.progress_weight("newland") == NOVICE_WEIGHT
        assert weighted_xp(cfg, 10, "newland", tracker=tracker) == 10


# =================================================================================================
# The gate, part 3: a synthetic noise domain pays ≈0 to BOTH XP and the restlessness signal
# =================================================================================================

class TestNoisePaysNothing:
    def test_coin_flip_domain_xp(self, tmp_path):
        """Coin-flip outcomes (error randomly 0 or 1 — irreducible randomness, high-and-flat in
        expectation): the noisy-TV trap. The R² gate keeps the spurious per-window slopes from
        paying: post-warm-up take is ≈0 against a 3000-XP volume clock."""
        cfg = _cfg(tmp_path)
        rng = random.Random(42)
        flips = [float(rng.randint(0, 1)) for _ in range(300)]
        total = _replay(cfg, "noise", flips, base_xp=10)
        warmup = (MIN_OBSERVATIONS - 1) * 10
        assert total - warmup < 0.02 * (300 * 10)     # ≈0 farmed after warm-up
        assert total < 0.05 * (300 * 10)              # ≈0 overall vs the volume clock

    def test_coin_flip_domain_restlessness(self, tmp_path):
        """The curiosity seam sees the same truth: once the noise domain has a full window, the
        restlessness signal stays pinned ≈0 — an unlearnable domain is not interesting."""
        cfg = _cfg(tmp_path)
        tracker = ProgressTracker(cfg)
        rng = random.Random(42)
        signals = []
        for i in range(300):
            tracker.observe("noise", float(rng.randint(0, 1)))
            if i >= WINDOW:                      # judged only on full windows
                signals.append(tracker.restlessness_signal("noise"))
        assert sum(signals) / len(signals) < 0.05    # ≈0 on average
        assert max(signals) < 1.0                    # never reads as a full frontier

    def test_learning_domain_restlessness_is_high(self, tmp_path):
        """Contrast: a domain that is actively teaching reads near 1 on the same signal, and an
        unexplored domain reads exactly 1 (optimism under no data — the explore side)."""
        cfg = _cfg(tmp_path)
        tracker = ProgressTracker(cfg)
        for i in range(WINDOW):
            tracker.observe("frontier", 1.0 - i / (WINDOW - 1))
        assert tracker.restlessness_signal("frontier") > 0.8
        assert tracker.restlessness_signal("never_seen") == 1.0


# =================================================================================================
# The persona seam: error-recovery bonus preserved; flag off → byte-identical
# =================================================================================================

class TestPersonaSeam:
    def test_error_recovery_bonus_preserved_flag_on(self, tmp_path):
        """record_error_recovery keeps its flat +5 with the flag ON — it is a domain-less award and
        never routes through the weighting, even when its would-be domain is fully mastered."""
        cfg = _cfg(tmp_path, enabled=True)
        tracker = ProgressTracker(cfg)
        for _ in range(WINDOW):
            tracker.observe("general", 0.0)      # everything mastered — weight would be 0
        p = persona_mod._default_persona()
        persona_mod.record_error_recovery(p)
        assert p["xp"] == 5
        assert p["total_errors_recovered"] == 1

    def test_flag_off_byte_identical(self, tmp_path):
        """Flag OFF: award_xp with domain+config produces a byte-identical persona dict to the
        legacy call — the whole 4.2 path is dark."""
        cfg = _cfg(tmp_path, enabled=False)
        tracker = ProgressTracker(cfg)
        for _ in range(WINDOW):
            tracker.observe("d", 0.0)            # would zero the award if the flag leaked
        legacy, gated = persona_mod._default_persona(), None
        gated = copy.deepcopy(legacy)
        lvl_a = persona_mod.award_xp(legacy, 7, "reason")
        lvl_b = persona_mod.award_xp(gated, 7, "reason", domain="d", config=cfg)
        assert gated == legacy
        assert lvl_a == lvl_b
        assert json.dumps(gated, sort_keys=True) == json.dumps(legacy, sort_keys=True)

    def test_flag_on_without_domain_unchanged(self, tmp_path):
        """Flag ON but no domain named (every pre-4.2 call site): unchanged legacy behavior."""
        cfg = _cfg(tmp_path, enabled=True)
        p = persona_mod._default_persona()
        persona_mod.award_xp(p, 100, "goal", config=cfg)
        assert p["xp"] == 100

    def test_award_fail_open_on_broken_tracker(self, tmp_path, monkeypatch):
        """A blown-up weighting pays the unweighted base rather than crashing the award path."""
        cfg = _cfg(tmp_path, enabled=True)
        monkeypatch.setattr(learning_progress, "weighted_xp",
                            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
        p = persona_mod._default_persona()
        persona_mod.award_xp(p, 9, "r", domain="d", config=cfg)
        assert p["xp"] == 9

    def test_award_adjudicated_xp_routes_and_gates(self, tmp_path):
        """The glue-cutover convenience routes through persona.award_xp: weighted when the flag is
        on (mastered domain → 0), legacy volume pay when off."""
        cfg_on = _cfg(tmp_path / "on", enabled=True)
        tracker = ProgressTracker(cfg_on)
        for _ in range(WINDOW):
            tracker.observe("grind", 0.0)
        p = persona_mod._default_persona()
        award_adjudicated_xp(cfg_on, p, 50, "grind", "settled")
        assert p["xp"] == 0

        cfg_off = _cfg(tmp_path / "off", enabled=False)
        p2 = persona_mod._default_persona()
        award_adjudicated_xp(cfg_off, p2, 50, "grind", "settled")
        assert p2["xp"] == 50


# =================================================================================================
# Mechanics: bounds, persistence fail-open, domain keys
# =================================================================================================

class TestTrackerMechanics:
    def test_series_bounded_to_window(self, tmp_path):
        cfg = _cfg(tmp_path)
        tracker = ProgressTracker(cfg)
        for i in range(WINDOW * 3):
            tracker.observe("d", float(i))
        s = tracker.series("d")
        assert len(s) == WINDOW
        assert s[-1] == float(WINDOW * 3 - 1)     # newest kept, oldest trimmed

    def test_domain_count_bounded_with_lru_eviction(self, tmp_path):
        cfg = _cfg(tmp_path)
        tracker = ProgressTracker(cfg)
        for i in range(MAX_DOMAINS + 5):
            tracker.observe(f"dom{i}", 0.5, now=float(i))
        assert len(tracker._domains) == MAX_DOMAINS
        assert tracker.series("dom0") == []                # oldest evicted
        assert tracker.series(f"dom{MAX_DOMAINS + 4}")     # newest kept

    def test_persists_across_instances(self, tmp_path):
        cfg = _cfg(tmp_path)
        t1 = ProgressTracker(cfg)
        for e in (0.9, 0.7, 0.5):
            t1.observe("d", e)
        t2 = ProgressTracker(cfg)
        assert t2.series("d") == [0.9, 0.7, 0.5]

    def test_corrupt_state_file_fails_open(self, tmp_path):
        cfg = _cfg(tmp_path)
        cfg.state_dir.mkdir(parents=True, exist_ok=True)
        (cfg.state_dir / learning_progress.STATE_FILENAME).write_text("{not json", encoding="utf-8")
        tracker = ProgressTracker(cfg)                # no raise
        assert tracker.series("d") == []
        tracker.observe("d", 0.5)                     # and it heals on the next observe
        assert ProgressTracker(cfg).series("d") == [0.5]

    def test_domain_key_normalization(self):
        assert domain_key("Backup Verify", "tier 2") == "backup_verify/tier_2"
        assert domain_key(None, "", "  ") == "general"    # empty buckets to the shared default
        assert len(domain_key("x" * 500)) <= 120

    def test_rising_error_pays_nothing(self, tmp_path):
        """Error going UP (a regressing domain) is not progress — weight 0, not negative XP."""
        cfg = _cfg(tmp_path)
        tracker = ProgressTracker(cfg)
        for i in range(WINDOW):
            tracker.observe("worse", i / (WINDOW - 1))
        assert tracker.progress_weight("worse") == 0.0
        assert tracker.restlessness_signal("worse") == 0.0
