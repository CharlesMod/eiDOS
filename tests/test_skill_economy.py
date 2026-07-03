"""Pillars 3.1/3.2 — the skill economy (reuse as the resting state). All behind config flags that
default False; these tests flip them ON. Offline only: skills are authored into a temp workspace and
driven through the live TOOLS registry / manifest exactly as the tick loop does. Embeddings use the
deterministic mock (config.mock_mode = True) — no ONNX model, no network, no GPU, no services.

The gate (PILLARS_TODO.md 3.1/3.2):
  (a) affordances returns the K most situation-similar skills; the exploration ε slot occasionally
      surfaces a cold (untrusted / never-used) skill so it can earn (anti-Matthew);
  (b) authoring a near-duplicate costs MORE energy than a novel skill (the price is the dedup pressure);
  (c) a successful reuse grants MORE XP than a create;
  (d) an unused-past-threshold skill is auto-retired and vanishes from affordances + brief;
  (e) with ALL flags OFF, skills.py / context.py behaviour is unchanged (hard dup-veto, no XP, no
      energy charge, no affordances, no retire).
"""

import json
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import skills
import persona
from config import Config
from tools import TOOLS


# A trivial always-succeeds skill body, parameterised by name.
def _good(name: str) -> str:
    return (f"def tool_{name}(args, config):\n"
            f"    return ToolResult(output=\"ok\", full_output_path=None, success=True, duration_s=0.0)\n")


def _cfg(*, affordances=False, economy=False, k=3, retire_days=30.0):
    c = Config()
    c.workspace_dir = tempfile.mkdtemp()
    c.mock_mode = True                      # deterministic hash embedder, no ONNX
    c.pillars_skill_affordances_enabled = affordances
    c.pillars_skill_economy_enabled = economy
    c.pillars_skill_affordance_k = k
    c.pillars_skill_retire_unused_days = retire_days
    return c


def _make(c, name, description):
    r = skills.create_skill(c, name, _good(name), description=description)
    assert r["success"], (name, r)
    return r


def _mark_trusted(c, name, invocations=10, successes=10):
    m = json.loads(skills._manifest_path(c).read_text())
    ent = m["skills"][name]
    ent["status"] = "trusted"
    ent["invocations"] = invocations
    ent["successes"] = successes
    skills._manifest_path(c).write_text(json.dumps(m))


class _StubRng:
    """Deterministic RNG stand-in: random() returns a fixed value; choice() returns a fixed index."""
    def __init__(self, r, pick=0):
        self._r = r
        self._pick = pick

    def random(self):
        return self._r

    def choice(self, seq):
        return seq[self._pick]


# ---------------------------------------------------------------------------
# (a) Affordances: K most situation-similar skills + exploration ε
# ---------------------------------------------------------------------------

class TestAffordances(unittest.TestCase):
    def setUp(self):
        # economy ON so domain-noun-sharing skills can coexist (the old hard veto is a warning now).
        self.c = _cfg(affordances=True, economy=True, k=3)
        for name, desc in [
            ("check_mqtt_broker", "connect to the mqtt broker and check status"),
            ("read_mqtt_topic", "subscribe and read an mqtt topic message"),
            ("mqtt_publish", "publish a message to an mqtt topic"),
            ("obscure_widget", "totally unrelated widget frobnicator gadget"),
        ]:
            _make(self.c, name, desc)
        for n in ("check_mqtt_broker", "read_mqtt_topic", "mqtt_publish"):
            _mark_trusted(self.c, n)

    def tearDown(self):
        for n in ("check_mqtt_broker", "read_mqtt_topic", "mqtt_publish", "obscure_widget"):
            TOOLS.pop(n, None)

    def test_returns_k_most_similar(self):
        # random() >= eps → no exploration; pure exploit ranking by similarity × trust.
        aff = skills.skill_affordances(self.c, "I need to work with the mqtt broker",
                                       _rng=_StubRng(0.99))
        names = [a["name"] for a in aff]
        self.assertEqual(len(aff), 3)
        # All three mqtt skills rank above the unrelated widget for an mqtt situation.
        self.assertNotIn("obscure_widget", names)
        self.assertTrue(all(n.count("mqtt") or "mqtt" in n for n in names), names)
        # Score-descending.
        self.assertEqual([a["score"] for a in aff], sorted((a["score"] for a in aff), reverse=True))

    def test_exploration_surfaces_cold_skill(self):
        # random() < eps → the LAST slot goes to a cold (untrusted / never-used) skill.
        aff = skills.skill_affordances(self.c, "I need to work with the mqtt broker",
                                       _rng=_StubRng(0.0, pick=0))
        self.assertEqual(len(aff), 3)
        last = aff[-1]
        self.assertTrue(last["explore"])
        self.assertEqual(last["name"], "obscure_widget")   # the only cold skill
        # The exploit slots (all but last) are NOT flagged as exploration.
        self.assertFalse(any(a["explore"] for a in aff[:-1]))

    def test_render_distinct_from_brief(self):
        aff = skills.skill_affordances(self.c, "mqtt broker", _rng=_StubRng(0.99))
        block = skills.render_affordances(aff)
        self.assertIn("Tools at hand", block)
        for a in aff:
            self.assertIn(a["name"], block)


# ---------------------------------------------------------------------------
# (b) Similarity-priced authoring: near-duplicate costs more than a novel skill
# ---------------------------------------------------------------------------

class TestAuthoringEconomics(unittest.TestCase):
    def setUp(self):
        self.c = _cfg(economy=True)

    def tearDown(self):
        for n in ("check_mqtt_broker", "ping_octoprint", "read_mqtt_status"):
            TOOLS.pop(n, None)

    def test_duplicate_costs_more_than_novel(self):
        first = _make(self.c, "check_mqtt_broker",
                      "connect to the mqtt broker and check its status")
        novel = _make(self.c, "ping_octoprint",
                      "query the octoprint 3d printer for job progress")
        dup = _make(self.c, "read_mqtt_status",
                    "connect to the mqtt broker and check its status")
        # The first author has nothing to be similar to → base cost.
        self.assertAlmostEqual(first["author_energy_cost"],
                               self.c.pillars_skill_author_energy_cost, places=6)
        # A near-duplicate of the mqtt skill is far more expensive than an unrelated novel skill.
        self.assertGreater(dup["max_similarity"], novel["max_similarity"])
        self.assertGreater(dup["author_energy_cost"], novel["author_energy_cost"])
        # And the near-duplicate carries the (now non-fatal) domain-overlap warning.
        self.assertTrue(dup.get("warnings"))

    def test_energy_actually_drained(self):
        from nervous.metabolism import Metabolism
        before = Metabolism(config=self.c).energy
        _make(self.c, "ping_octoprint", "query octoprint printer job progress")
        after = Metabolism(config=self.c).energy    # reloads persisted reserve
        self.assertLess(after, before)


# ---------------------------------------------------------------------------
# (c) Reuse pays more XP than creation
# ---------------------------------------------------------------------------

class TestReuseXP(unittest.TestCase):
    def setUp(self):
        self.c = _cfg(economy=True)

    def tearDown(self):
        TOOLS.pop("adder", None)

    def test_reuse_grants_more_xp_than_create(self):
        _make(self.c, "adder", "adds numbers")
        xp_after_create = persona.load_persona(self.c.workspace)["xp"]
        self.assertGreater(xp_after_create, 0)          # creation grants some XP
        TOOLS["adder"]({}, self.c)                       # one successful reuse
        xp_after_reuse = persona.load_persona(self.c.workspace)["xp"]
        create_gain = xp_after_create
        reuse_gain = xp_after_reuse - xp_after_create
        self.assertGreater(reuse_gain, create_gain)      # THE gate: reuse > create


# ---------------------------------------------------------------------------
# (d) Auto-retire: unused-past-threshold skill vanishes from affordances + brief
# ---------------------------------------------------------------------------

class TestAutoRetire(unittest.TestCase):
    def setUp(self):
        self.c = _cfg(affordances=True, economy=True, retire_days=1.0)

    def tearDown(self):
        TOOLS.pop("stale_skill", None)

    def test_unused_skill_is_retired_and_disappears(self):
        _make(self.c, "stale_skill", "some stale mqtt broker helper")
        _mark_trusted(self.c, "stale_skill")            # trusted, so it WOULD be an affordance
        # Backdate last_used well past the 1-day threshold.
        m = json.loads(skills._manifest_path(self.c).read_text())
        m["skills"]["stale_skill"]["last_used"] = "2000-01-01T00:00:00Z"
        skills._manifest_path(self.c).write_text(json.dumps(m))

        retired = skills.retire_unused_skills(self.c)
        self.assertEqual(retired, ["stale_skill"])
        # Gone from the brief, the registry, and the affordance list.
        self.assertNotIn("stale_skill", skills.skills_brief(self.c))
        self.assertNotIn("stale_skill", TOOLS)
        aff = skills.skill_affordances(self.c, "stale mqtt broker helper", _rng=_StubRng(0.99))
        self.assertNotIn("stale_skill", [a["name"] for a in aff])
        # Recoverable: the manifest entry + versioned file survive (rollback path).
        m2 = json.loads(skills._manifest_path(self.c).read_text())
        self.assertEqual(m2["skills"]["stale_skill"]["status"], "retired")
        self.assertTrue(skills._skill_file(self.c, "stale_skill", "1.0.0").exists())

    def test_recently_used_skill_is_kept(self):
        _make(self.c, "stale_skill", "fresh helper")    # last_used defaults to now via create/no-use
        m = json.loads(skills._manifest_path(self.c).read_text())
        m["skills"]["stale_skill"]["last_used"] = skills._now()
        skills._manifest_path(self.c).write_text(json.dumps(m))
        self.assertEqual(skills.retire_unused_skills(self.c), [])


# ---------------------------------------------------------------------------
# (e) Flag-OFF: byte-for-byte historical behaviour
# ---------------------------------------------------------------------------

class TestFlagOffUnchanged(unittest.TestCase):
    def setUp(self):
        self.c = _cfg()                                  # all pillars-3 flags default False

    def tearDown(self):
        for n in ("check_mqtt", "read_mqtt", "solo"):
            TOOLS.pop(n, None)

    def test_domain_duplicate_is_hard_vetoed(self):
        r1 = skills.create_skill(self.c, "check_mqtt", _good("check_mqtt"), description="mqtt")
        self.assertTrue(r1["success"])
        r2 = skills.create_skill(self.c, "read_mqtt", _good("read_mqtt"), description="mqtt")
        self.assertFalse(r2["success"])                  # veto, not warning
        self.assertIn("ALREADY", r2["errors"][0])

    def test_no_economy_side_effects(self):
        import os
        r = skills.create_skill(self.c, "solo", _good("solo"), description="a lone skill")
        self.assertTrue(r["success"])
        # No economy keys leak into the return.
        self.assertNotIn("author_energy_cost", r)
        self.assertNotIn("max_similarity", r)
        # No energy reserve was created/charged, no XP awarded.
        self.assertFalse(os.path.exists(str(self.c.state_dir / "metabolism.json")))
        self.assertEqual(persona.load_persona(self.c.workspace)["xp"], 0)
        # A successful reuse also grants no XP with the flag off.
        TOOLS["solo"]({}, self.c)
        self.assertEqual(persona.load_persona(self.c.workspace)["xp"], 0)

    def test_affordances_and_retire_are_noops(self):
        _make_ok = skills.create_skill(self.c, "solo", _good("solo"), description="lone")
        self.assertTrue(_make_ok["success"])
        self.assertEqual(skills.skill_affordances(self.c, "anything"), [])
        self.assertEqual(skills.retire_unused_skills(self.c), [])


if __name__ == "__main__":
    unittest.main()
