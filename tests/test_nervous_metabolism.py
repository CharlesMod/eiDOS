"""M0 gates — the metabolism (energy economy) and hunger as a felt bar.

The organism's stakes: thinking drains energy (the dearest act), rest recovers it, and a depleting
reserve is FELT as hunger that genuinely worsens the body-feeling (NON-baseline) — so the existing
wellbeing→reward loop punishes staying hungry and rumination (cognition cost, no nourishment) stops
being free. Loops, not guardrails."""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from nervous import Metabolism, hunger_to_bar, NervousBus, Kind, Modality  # noqa: E402
from nervous.metabolism import solar_charge_in  # noqa: E402
from nervous.felt import to_felt, felt_state, stress_bars, BASELINE_SYSTEMS  # noqa: E402
from nervous.interoception import Interoception  # noqa: E402


class TestMetabolismCore(unittest.TestCase):
    def test_thinking_drains_resting_recovers(self):
        m = Metabolism(start_energy=0.8)
        m.metabolize(thought=True)
        self.assertLess(m.energy, 0.8)              # a thought costs energy
        low = m.energy
        for _ in range(5):
            m.metabolize(resting=True)
        self.assertGreater(m.energy, low)           # rest restores it

    def test_cognition_is_the_dearest_act(self):
        think = Metabolism(start_energy=1.0); think.metabolize(thought=True)
        idle = Metabolism(start_energy=1.0); idle.metabolize(thought=False)   # basal only
        self.assertLess(think.energy, idle.energy)  # thinking drains more than just living

    def test_acting_adds_cost(self):
        a = Metabolism(start_energy=1.0); a.metabolize(thought=True, acted=True)
        b = Metabolism(start_energy=1.0); b.metabolize(thought=True, acted=False)
        self.assertLess(a.energy, b.energy)

    def test_energy_is_bounded(self):
        m = Metabolism(start_energy=0.02)
        for _ in range(50):
            m.metabolize(thought=True)
        self.assertGreaterEqual(m.energy, 0.0)      # never negative (hibernation floor, not debt)
        for _ in range(200):
            m.metabolize(resting=True)
        self.assertLessEqual(m.energy, 1.0)         # never over-full

    def test_feed_restores_clamped(self):
        m = Metabolism(start_energy=0.5)
        m.feed(0.3)
        self.assertAlmostEqual(m.energy, 0.8, places=3)
        m.feed(1.0)
        self.assertEqual(m.energy, 1.0)             # nourishment clamps at full

    def test_hunger_is_inverse_of_energy(self):
        m = Metabolism(start_energy=0.7)
        self.assertAlmostEqual(m.hunger(), 0.3, places=3)

    def test_hunger_bar_thresholds(self):
        self.assertEqual(hunger_to_bar(0.0), "ok")
        self.assertEqual(hunger_to_bar(0.35), "elevated")
        self.assertEqual(hunger_to_bar(0.60), "high")
        self.assertEqual(hunger_to_bar(0.85), "critical")

    def test_energy_persists_across_instances(self):
        d = tempfile.mkdtemp()
        p = os.path.join(d, "metabolism.json")
        m = Metabolism(start_energy=0.9, state_path=p, save_every=1)
        m.metabolize(thought=True)
        spent = 0.9 - m.energy
        m2 = Metabolism(start_energy=0.5, state_path=p)   # a fresh instance...
        self.assertAlmostEqual(m2.energy, 0.9 - spent, places=4)  # ...wakes with the SAME energy

    def test_publishes_retained_metabolism(self):
        bus = NervousBus()
        self.addCleanup(bus.close)
        m = Metabolism(bus=bus, start_energy=0.6)
        m.metabolize(thought=True)
        ev = bus.retained_snapshot(Kind.metabolism, Modality.intero)
        self.assertIsNotNone(ev)
        body = json.loads(bus.payloads.get(ev.payload_ref).decode("utf-8"))
        self.assertIn("energy", body)
        self.assertIn("hunger", body)


class TestArchetypeAndPower(unittest.TestCase):
    """Post-pivot (2026-06-20): food = literal battery power. A PLANT recharges only from environmental
    power (charge_in / solar); an ANIMAL also recharges by resting/docking."""

    def test_animal_recharges_by_resting(self):
        a = Metabolism(start_energy=0.5, archetype="animal")
        a.metabolize(resting=True)
        self.assertGreater(a.energy, 0.5)            # an animal naps/docks to recover

    def test_plant_does_not_recharge_by_resting(self):
        # A plant resting with no power coming in just sits dormant (pays basal) — it does NOT refill.
        p = Metabolism(start_energy=0.5, archetype="plant")
        p.metabolize(resting=True, charge_in=0.0)
        self.assertLess(p.energy, 0.5)               # no sun, no food — even at rest it ebbs
        self.assertAlmostEqual(p.energy, 0.5 - p.basal, places=4)

    def test_plant_recharges_from_charge_in(self):
        p = Metabolism(start_energy=0.5, archetype="plant")
        p.metabolize(thought=True, charge_in=0.05)   # bright sun out-paces a thinking tick
        self.assertGreater(p.energy, 0.5)

    def test_resting_body_pays_no_cognition(self):
        # Dormant = no thought/action cost, only basal — both archetypes.
        busy = Metabolism(start_energy=1.0, archetype="plant"); busy.metabolize(thought=True, acted=True)
        dorm = Metabolism(start_energy=1.0, archetype="plant"); dorm.metabolize(thought=True, resting=True)
        self.assertLess(busy.energy, dorm.energy)

    def test_solar_curve_dark_at_night_peaks_midday(self):
        self.assertEqual(solar_charge_in(3.0), 0.0)          # pre-dawn: nothing
        self.assertEqual(solar_charge_in(23.0), 0.0)         # after dusk: nothing
        self.assertEqual(solar_charge_in(6.0), 0.0)          # sunrise edge
        noon = solar_charge_in(13.0, peak=0.03)              # ~solar noon (mid of 6..20)
        self.assertGreater(noon, 0.02)
        self.assertLessEqual(noon, 0.03 + 1e-9)              # never exceeds peak
        self.assertGreater(solar_charge_in(13.0), solar_charge_in(8.0))   # midday > morning

    def test_solar_curve_handles_degenerate_window(self):
        self.assertEqual(solar_charge_in(12.0, sunrise=20.0, sunset=6.0), 0.0)  # inverted window → 0


class TestHungerIsFelt(unittest.TestCase):
    def test_hunger_is_not_baseline(self):
        # VRAM is baseline (the resident mind, never stress); hunger must NOT be — it has to bite.
        self.assertNotIn("energy", BASELINE_SYSTEMS)
        self.assertIn("energy", stress_bars({"energy": "high", "vram": "critical"}))

    def test_hunger_worsens_the_body_feeling(self):
        self.assertEqual(to_felt({"energy": "ok"})["overall"], "at ease")
        self.assertEqual(to_felt({"energy": "elevated"})["overall"], "a little tense")
        self.assertEqual(to_felt({"energy": "critical"})["overall"], "in distress")
        self.assertIn("starving", to_felt({"energy": "critical"})["felt"])

    def test_high_vram_plus_hunger_feels_only_the_hunger(self):
        # The resident mind (VRAM critical) is calm posture; the hunger is what the creature feels.
        f = to_felt({"vram": "critical", "energy": "high"})
        self.assertEqual(f["overall"], "strained")          # driven by hunger, not VRAM
        self.assertIn("hungry", f["felt"])

    def test_interoception_folds_in_the_hunger_bar(self):
        class FakeMetabolism:
            def hunger_bar(self):
                return "critical"
        bus = NervousBus()
        self.addCleanup(bus.close)
        # reader returns no host telemetry -> the ONLY bar is the folded-in hunger
        io = Interoception(bus, reader=lambda: {}, metabolism=FakeMetabolism())
        io.emit()
        ev = bus.retained_snapshot(Kind.interoceptive, Modality.intero)
        self.assertIsNotNone(ev)
        proj = json.loads(bus.payloads.get(ev.payload_ref).decode("utf-8"))
        self.assertEqual(proj["bars"].get("energy"), "critical")
        self.assertEqual(proj["overall"], "in distress")     # a starving body is in distress


if __name__ == "__main__":
    unittest.main()
