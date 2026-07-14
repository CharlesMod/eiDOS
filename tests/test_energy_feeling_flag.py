"""The energy/power FEELING is behind one flag (nervous_metabolism_enabled): with no real power feed
yet, the whole battery/hunger fiction is off, and the prompt must not tell the creature it has a body
it lacks. The organ gate (Metabolism not constructed) is exercised in the loop; here we pin the PROMPT
half — the energy bullet is stripped when the feeling is off, and everything else is untouched.
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import prompts   # noqa: E402
import unlocks   # noqa: E402


class TestEnergyFeelingGate(unittest.TestCase):
    def _render(self, energy_feeling):
        return prompts.render_creature_system_prompt(
            {}, unlocks.UNIT_IDS, energy_feeling=energy_feeling)

    def test_bullet_present_when_feeling_on(self):
        self.assertIn("energy is POWER", self._render(True))
        self.assertIn("feels like plain hunger", self._render(True))

    def test_bullet_stripped_when_feeling_off(self):
        off = self._render(False)
        self.assertNotIn("energy is POWER", off)
        self.assertNotIn("hunger", off)
        self.assertNotIn("battery charge", off)

    def test_other_body_feelings_survive_when_energy_off(self):
        off = self._render(False)
        # curiosity, sleep, and memory are REAL felt signals — they must remain
        self.assertIn("curiosity sparks", off)
        self.assertIn("sleep pressure builds", off)
        self.assertIn("memory is", off)
        self.assertIn("Your feelings are real signals", off)

    def test_default_keeps_the_feeling(self):
        # callers that don't pass the kwarg (and every existing test) stay byte-identical
        self.assertIn("energy is POWER",
                      prompts.render_creature_system_prompt({}, unlocks.UNIT_IDS))

    def test_the_stripped_bullet_matches_the_base_text(self):
        # if BASE's wording drifts, the .replace() would silently no-op — pin that it still bites
        on = self._render(True)
        off = self._render(False)
        self.assertEqual(len(on) - len(off), len(prompts._ENERGY_FEELING_BULLET))
        self.assertIn(prompts._ENERGY_FEELING_BULLET, on)


if __name__ == "__main__":
    unittest.main()
