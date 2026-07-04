"""P5b / Pillar 6 gates: the neuromodulatory state — arousal rises with pressure, affect tracks,
modulation is broadcast retained.

Also pins the infant nap curve (TOOL_PROGRESSION.md decision #3): a stage-scaled adenosine wake
ceiling so a hatchling naps in short bouts (~5/day) and consolidates toward the adult ~1/day. Dark
until `pillars_tool_unlocks_enabled` exists; flag off is byte-identical to the pre-curve ceiling.
"""
import json
import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config import Config  # noqa: E402
from nervous import NervousBus, Kind, Modality, Delivery, NeuromodulatoryState  # noqa: E402
from nervous.neuromod import Adenosine, NAP_STAGE_SCALE, _stage_scale  # noqa: E402


class TestNeuromod(unittest.TestCase):
    def test_at_rest_is_calm(self):
        bus = NervousBus()
        self.addCleanup(bus.close)
        nm = NeuromodulatoryState(bus, baseline_arousal=0.3)
        nm.observe_interoception({"bars": {"ram": "ok", "vram": "ok"}})
        self.assertEqual(nm.mood(), "calm")

    def test_arousal_rises_with_pressure(self):
        bus = NervousBus()
        self.addCleanup(bus.close)
        nm = NeuromodulatoryState(bus, baseline_arousal=0.3)
        a0 = nm.arousal
        for _ in range(20):
            nm.observe_interoception({"bars": {"gpu_temp": "critical"}})   # a real stressor (heat)
        self.assertGreater(nm.arousal, a0)          # the body's stress raises arousal
        self.assertLess(nm.valence, 0.0)            # and lowers valence
        self.assertIn(nm.mood(), ("vigilant", "distressed", "uneasy", "tense"))

    def test_high_vram_does_not_sweat(self):
        # high VRAM is the resident mind (high usage BY DESIGN) — it must not raise arousal or sour mood.
        bus = NervousBus()
        self.addCleanup(bus.close)
        nm = NeuromodulatoryState(bus, baseline_arousal=0.3)
        for _ in range(20):
            nm.observe_interoception({"bars": {"vram": "critical"}})
        self.assertEqual(nm.valence, 0.0)           # pressure = 0 -> valence unsoured
        self.assertEqual(nm.mood(), "calm")         # an at-ease body, a calm mind

    def test_bump_raises_arousal(self):
        bus = NervousBus()
        self.addCleanup(bus.close)
        nm = NeuromodulatoryState(bus)
        a0 = nm.arousal
        nm.bump(0.5)
        self.assertGreater(nm.arousal, a0)          # a startle spike

    def test_reward_arousal_is_phasic_not_per_tick(self):
        # Routine small-RPE ticks must NOT pump arousal every tick (the newborn-creature pin); only a
        # genuine surprise spikes it, and only by a small bounded amount.
        bus = NervousBus()
        self.addCleanup(bus.close)
        nm = NeuromodulatoryState(bus, baseline_arousal=0.3)
        base = nm.arousal
        for _ in range(20):
            nm.observe_reward(rpe=0.1, reward=0.05)      # 20 routine ticks
        self.assertLessEqual(nm.arousal, base + 1e-9)    # arousal untouched — it can relax to baseline
        nm.observe_reward(rpe=1.0, reward=0.5)           # a real surprise
        self.assertGreater(nm.arousal, base)             # spikes
        self.assertLessEqual(nm.arousal, base + 0.1 + 1e-9)   # but bounded

    def test_exhaustion_collapses_arousal_toward_sleep(self):
        # M0.3: above the exhaustion floor, energy doesn't sap arousal; near-empty, arousal collapses
        # toward sleep (torpor) so the creature RESTS before flatlining — hibernation, not death.
        bus = NervousBus()
        self.addCleanup(bus.close)
        nm = NeuromodulatoryState(bus, baseline_arousal=0.3, exhaustion_energy=0.15)
        nm.observe_energy(0.8)                              # well-fed
        for _ in range(20):
            nm.observe_interoception({"bars": {}})
        self.assertGreater(nm.arousal, 0.2)                # rests near baseline when fed
        nm.observe_energy(0.0)                              # reserve empty
        for _ in range(40):
            nm.observe_interoception({"bars": {}})
        self.assertLess(nm.arousal, 0.15)                  # collapsed into the sleep range (torpor)

    def test_modulation_published_retained(self):
        bus = NervousBus()
        self.addCleanup(bus.close)
        sub = bus.subscribe(topics={(Kind.modulation, Modality.system)}, deliveries={Delivery.retained})
        nm = NeuromodulatoryState(bus)
        nm.observe_interoception({"bars": {"ram": "high"}})
        nm.publish()
        e = bus.recv(sub, timeout=1.0)
        self.assertIsNotNone(e)
        d = json.loads(bus.payloads.get(e.payload_ref).decode("utf-8"))
        self.assertIn("arousal", d)
        self.assertIn("valence", d)
        self.assertIn("mood", d)


# =================================================================================================
# The infant nap curve — a stage-scaled adenosine wake ceiling (TOOL_PROGRESSION.md decision #3).
# Dark until pillars_tool_unlocks_enabled; flag off is byte-identical to the pre-curve ceiling.
# =================================================================================================
import tempfile  # noqa: E402


def _nap_cfg(tmp, *, level, hatched=True, flag=True, genes=None):
    """A Config whose workspace holds persona.json (level) and creature.json (hatched), with the
    integration flag set dynamically (it does NOT exist in config.py — this code is dark until it
    does). Optional genes writes a genome.json so the wake_budget gene can be forced."""
    cfg = Config()
    cfg.workspace_dir = str(tmp)
    cfg.mock_mode = True
    cfg.workspace.mkdir(parents=True, exist_ok=True)
    setattr(cfg, "pillars_tool_unlocks_enabled", flag)
    (cfg.workspace / "persona.json").write_text(json.dumps({"level": level}), encoding="utf-8")
    (cfg.workspace / "creature.json").write_text(json.dumps({"hatched": hatched}), encoding="utf-8")
    if genes is not None:
        from genome import GENE_LOADINGS, LATENTS
        import genome as _genome_mod
        g = {name: 1.0 for name in GENE_LOADINGS}
        g.update(genes)
        doc = {"v": 1, "seed": 7, "born_ts": 1.0,
               "latents": {n: 0.0 for n in LATENTS}, "genes": g, "stamp_baselines": {}}
        (cfg.workspace / "genome.json").write_text(json.dumps(doc), encoding="utf-8")
        _genome_mod._cache.clear()   # the accessor caches by path; a fresh tmp genome must be re-read
    return cfg


# level → the canonical stage_for() bucket (dashboard's derivation): 1-2 hatchling, 3-4 juvenile,
# 5-7 adult, 8+ guardian. One representative level per stage.
_STAGE_LEVEL = {"hatchling": 1, "juvenile": 3, "adult": 5, "guardian": 8}


class TestInfantNapCurve(unittest.TestCase):
    ADULT_CEILING = 18.0

    def _tmp(self):
        d = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(d, ignore_errors=True))
        return Path(d)

    def test_table_values_scale_the_ceiling(self):
        # Each declared stage scale, applied to the 18 h base ceiling (no genome → gene 1.0).
        expected = {"hatchling": 0.2, "juvenile": 0.55, "adult": 1.0, "guardian": 1.0}
        for stage, scale in expected.items():
            self.assertEqual(NAP_STAGE_SCALE[stage], scale)
            cfg = _nap_cfg(self._tmp(), level=_STAGE_LEVEL[stage])
            a = Adenosine(max_wake_hours=self.ADULT_CEILING, config=cfg)
            self.assertAlmostEqual(a.max_wake_hours, self.ADULT_CEILING * scale, places=6,
                                   msg=f"{stage} ceiling should be {scale}x the adult ceiling")

    def test_egg_and_hatchling_share_the_infant_scale(self):
        # Pre-hatch (egg) and post-hatch (hatchling) both nap on the 0.2 infant rhythm.
        egg = Adenosine(max_wake_hours=self.ADULT_CEILING,
                        config=_nap_cfg(self._tmp(), level=1, hatched=False))
        hatchling = Adenosine(max_wake_hours=self.ADULT_CEILING,
                              config=_nap_cfg(self._tmp(), level=1, hatched=True))
        self.assertAlmostEqual(egg.max_wake_hours, self.ADULT_CEILING * 0.2, places=6)
        self.assertAlmostEqual(hatchling.max_wake_hours, self.ADULT_CEILING * 0.2, places=6)

    def test_adult_is_unchanged_by_the_curve(self):
        # At the adult stage the scale is 1.0, so the ceiling — genome gene included — is identical
        # whether the flag is on or off: the curve consolidates TOWARD the adult, it never moves it.
        on = Adenosine(max_wake_hours=self.ADULT_CEILING,
                       config=_nap_cfg(self._tmp(), level=5, flag=True, genes={"wake_budget": 1.1}))
        off = Adenosine(max_wake_hours=self.ADULT_CEILING,
                        config=_nap_cfg(self._tmp(), level=5, flag=False, genes={"wake_budget": 1.1}))
        self.assertAlmostEqual(on.max_wake_hours, self.ADULT_CEILING * 1.1, places=6)
        self.assertAlmostEqual(on.max_wake_hours, off.max_wake_hours, places=9)

    def test_flag_off_is_byte_identical_even_for_a_hatchling(self):
        # The whole feature is dark without the flag: a hatchling config with the flag OFF produces
        # exactly the pre-curve ceiling (base × gene), NOT the 0.2 infant ceiling.
        cfg = _nap_cfg(self._tmp(), level=1, flag=False, genes={"wake_budget": 0.9})
        a = Adenosine(max_wake_hours=self.ADULT_CEILING, config=cfg)
        self.assertAlmostEqual(a.max_wake_hours, self.ADULT_CEILING * 0.9, places=9)
        # and _stage_scale itself short-circuits to 1.0 (no persona/creature read at all)
        self.assertEqual(_stage_scale(cfg), 1.0)
        self.assertEqual(_stage_scale(None), 1.0)

    def test_genome_gene_stays_sovereign_over_the_stage_scale(self):
        # The stage scale applies FIRST, then the gene multiplies — and the gene is still clamped to
        # its TIGHT ±10% band: even a hand-edited wake_budget=5.0 can only reach 1.1×, and the total
        # (hatchling 0.2 × 1.1) can never exceed the adult (1.0 × 1.1) ceiling. Adenosine sovereign.
        hatch = Adenosine(max_wake_hours=self.ADULT_CEILING,
                          config=_nap_cfg(self._tmp(), level=1, genes={"wake_budget": 5.0}))
        adult = Adenosine(max_wake_hours=self.ADULT_CEILING,
                          config=_nap_cfg(self._tmp(), level=5, genes={"wake_budget": 5.0}))
        self.assertAlmostEqual(hatch.max_wake_hours, self.ADULT_CEILING * 0.2 * 1.1, places=6)
        self.assertAlmostEqual(adult.max_wake_hours, self.ADULT_CEILING * 1.0 * 1.1, places=6)
        self.assertLess(hatch.max_wake_hours, adult.max_wake_hours)
        self.assertLessEqual(adult.max_wake_hours, self.ADULT_CEILING * 1.1 + 1e-9)

    def test_stage_scale_clamped_to_one_even_if_table_mis_edited(self):
        # A hand-edited NAP_STAGE_SCALE entry > 1.0 must NOT be able to lengthen the wake budget past
        # the adult's — the stage dimension can only shorten it. Patch a bad value and confirm clamp.
        cfg = _nap_cfg(self._tmp(), level=1)
        original = NAP_STAGE_SCALE["hatchling"]
        try:
            NAP_STAGE_SCALE["hatchling"] = 5.0
            self.assertEqual(_stage_scale(cfg), 1.0)   # clamped down, never inflates
        finally:
            NAP_STAGE_SCALE["hatchling"] = original

    def test_missing_stage_data_fails_open_to_adult(self):
        # A creature whose stage can't be read gets the FULL adult budget (never an accidental
        # over-nap): flag on but no persona.json → fail-open scale 1.0.
        tmp = self._tmp()
        cfg = Config()
        cfg.workspace_dir = str(tmp)
        cfg.mock_mode = True
        cfg.workspace.mkdir(parents=True, exist_ok=True)
        setattr(cfg, "pillars_tool_unlocks_enabled", True)   # flag on, but no persona.json written
        self.assertEqual(_stage_scale(cfg), 1.0)
        a = Adenosine(max_wake_hours=self.ADULT_CEILING, config=cfg)
        self.assertAlmostEqual(a.max_wake_hours, self.ADULT_CEILING, places=9)

    def test_hatchling_crosses_the_sleep_threshold_about_five_times_faster(self):
        # THE behavioural gate: same wake time accumulated, the hatchling's adenosine pressure is ~5×
        # the adult's, so it hits the override (and every threshold below it) ~5× sooner — ~5 naps to
        # the adult's 1. The genome gene cancels in the ratio (both creatures carry the same gene).
        hatch = Adenosine(max_wake_hours=self.ADULT_CEILING,
                          config=_nap_cfg(self._tmp(), level=1, genes={"wake_budget": 1.0}))
        adult = Adenosine(max_wake_hours=self.ADULT_CEILING,
                          config=_nap_cfg(self._tmp(), level=5, genes={"wake_budget": 1.0}))
        # accumulate exactly the hatchling's whole budget (3.6 h): it saturates, the adult barely stirs
        for a in (hatch, adult):
            a.accumulate(self.ADULT_CEILING * 0.2)
        self.assertTrue(hatch.overrides())               # the infant is forced to nap...
        self.assertFalse(adult.overrides())              # ...while the adult is only 1/5 of the way
        self.assertAlmostEqual(hatch.pressure() / adult.pressure(), 5.0, places=4)


if __name__ == "__main__":
    unittest.main()
