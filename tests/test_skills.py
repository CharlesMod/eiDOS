"""Skill lifecycle hygiene: per-version stats, trust promotion/demotion, dead-skill
quarantine, and brief visibility.

The manifest data these enforce came from a live run: bigprinter_status 0/8, query_klipper
0/6, camera_snapshot 0/4 — all still 'active' and offered to the model every tick, because
nothing ever culled a dead skill and stats were lifetime (so trust survived broken rewrites).

Plus the skill WORLD: skill code executes in the creature's home (the same cwd tool_bash
stands on), never the repo root. From a live run too: the 'log_entry' skill opened
'nest/journal.md' relative to the repo and FileNotFoundError'd while the file sat in its
home — and the repo root collected skill droppings (boss_habits.log, snapshot_*.jpg).
"""

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import skills
from skills import (_record_invocation, _save_manifest, _load_manifest,
                    prune_dead_skills, skills_brief, list_skills, TOOLS)
from config import Config


def _entry(status="active", version="1.0.0", invocations=0, successes=0, **kw):
    e = {"description": "t", "args_schema": {}, "author": "agent",
         "active_version": version, "versions": [version], "enabled": True,
         "status": status, "created": "2026-01-01T00:00:00Z",
         "updated": "2026-01-01T00:00:00Z",
         "invocations": invocations, "successes": successes}
    e.update(kw)
    return e


class TestSkillLifecycle(unittest.TestCase):

    def setUp(self):
        self.config = Config()
        self.config.workspace_dir = tempfile.mkdtemp()

    def tearDown(self):
        TOOLS.pop("probe", None)

    def _seed(self, **entries):
        _save_manifest(self.config, {"skills": entries})

    def test_quarantine_after_zero_successes(self):
        self._seed(probe=_entry())
        TOOLS["probe"] = lambda a, c: None
        for _ in range(skills._QUARANTINE_MIN_USES):
            _record_invocation(self.config, "probe", ok=False)
        ent = _load_manifest(self.config)["skills"]["probe"]
        self.assertEqual(ent["status"], "quarantined")
        self.assertFalse(ent["enabled"])
        self.assertNotIn("probe", TOOLS)   # no longer offered as a tool

    def test_trust_promotion_on_active_version(self):
        self._seed(probe=_entry())
        for _ in range(skills._TRUST_MIN_USES):
            _record_invocation(self.config, "probe", ok=True)
        ent = _load_manifest(self.config)["skills"]["probe"]
        self.assertEqual(ent["status"], "trusted")
        self.assertEqual(ent["version_stats"]["1.0.0"]["successes"],
                         skills._TRUST_MIN_USES)

    def test_trust_does_not_inherit_lifetime_stats(self):
        # 90/100 lifetime on old versions, but the ACTIVE version is fresh — one success
        # must not re-promote (it used to: lifetime rate stayed above the bar forever).
        self._seed(probe=_entry(invocations=100, successes=90, version="1.0.1",
                                versions=["1.0.0", "1.0.1"]))
        _record_invocation(self.config, "probe", ok=True)
        ent = _load_manifest(self.config)["skills"]["probe"]
        self.assertEqual(ent["status"], "active")   # needs _TRUST_MIN_USES on v1.0.1 itself

    def test_trusted_demoted_when_active_version_degrades(self):
        self._seed(probe=_entry(status="trusted"))
        TOOLS["probe"] = lambda a, c: None
        for _ in range(skills._TRUST_MIN_USES):
            _record_invocation(self.config, "probe", ok=False)
        ent = _load_manifest(self.config)["skills"]["probe"]
        # 0/5 on the running version: falls all the way to quarantined, not just demoted
        self.assertEqual(ent["status"], "quarantined")

    def test_prune_dead_skills_catches_legacy_zero_rate(self):
        # Pre-per-version manifest rows (no version_stats) with a dead lifetime record
        self._seed(bigprinter_status=_entry(invocations=8, successes=0),
                   printer_status=_entry(invocations=5, successes=4))
        TOOLS["bigprinter_status"] = lambda a, c: None
        try:
            dead = prune_dead_skills(self.config)
            self.assertEqual(dead, ["bigprinter_status"])
            m = _load_manifest(self.config)["skills"]
            self.assertEqual(m["bigprinter_status"]["status"], "quarantined")
            self.assertEqual(m["printer_status"]["status"], "active")   # untouched
            self.assertNotIn("bigprinter_status", TOOLS)
        finally:
            TOOLS.pop("bigprinter_status", None)

    def test_brief_includes_trusted_and_hides_quarantined(self):
        self._seed(good=_entry(status="trusted"),
                   ok=_entry(status="active"),
                   dead=_entry(status="quarantined", enabled=False))
        brief = skills_brief(self.config)
        self.assertIn("good", brief)    # trusted skills used to vanish from the brief
        self.assertIn("ok", brief)
        self.assertNotIn("dead", brief)

    def test_list_skills_reports_active_version_stats(self):
        self._seed(probe=_entry(invocations=10, successes=2,
                                version_stats={"1.0.0": {"invocations": 4, "successes": 2}}))
        info = list_skills(self.config)["skills"]["probe"]
        self.assertEqual(info["active_version_uses"], 4)
        self.assertEqual(info["active_version_success_rate"], 0.5)
        self.assertEqual(info["uses"], 10)


# A minimal skill whose only act is a RELATIVE write — where './probe.txt' lands IS the world
# the skill executes in. It must be the same ground write_file/bash stand on.
_DROPPER = """def tool_dropper(args, config):
    open('./probe.txt', 'w', encoding='utf-8').write('here')
    return ToolResult(output='dropped', full_output_path=None, success=True, duration_s=0.0)
"""


class TestSkillWorld(unittest.TestCase):
    """Skill code (the create-time smoke call AND every runtime invoke) executes in the
    creature's world — tools._creature_root, the same cwd tool_bash uses — never the repo."""

    def setUp(self):
        self.config = Config()
        self.config.workspace_dir = tempfile.mkdtemp()
        self.config.creature_mode = True
        self.config.pillars_killable_skills_enabled = True   # the subprocess execution path

    def tearDown(self):
        TOOLS.pop("dropper", None)

    def _repo_probe(self):
        return Path(skills.KAIROS_DIR) / "probe.txt"

    def test_skill_relative_write_lands_in_home_not_repo(self):
        self.assertFalse(self._repo_probe().exists(),
                         "stale probe.txt at the repo root — clean it before this test can pin anything")
        r = skills.create_skill(self.config, "dropper", _DROPPER)
        self.assertTrue(r.get("success"), r.get("errors"))
        home = Path(self.config.workspace_dir) / "home"
        # The create-time smoke call already ran the skill once — in the world, not the repo.
        self.assertTrue((home / "probe.txt").exists(),
                        "dry-run smoke call did not execute in the creature home")
        self.assertFalse(self._repo_probe().exists(),
                         "dry-run smoke call wrote into the repo root — the wrong world")
        (home / "probe.txt").unlink()
        # A runtime invoke through the live registry, exactly as the tick loop dispatches it.
        res = TOOLS["dropper"]({}, self.config)
        self.assertTrue(res.success, res.output)
        self.assertTrue((home / "probe.txt").exists(),
                        "skill invoke did not execute in the creature home")
        self.assertFalse(self._repo_probe().exists(),
                         "skill invoke wrote into the repo root — the wrong world")

    def test_house_ai_skill_runs_in_the_workspace(self):
        # creature_mode OFF: bash's world is the full workspace (no home burrow) — skills follow
        # the very same decision (_creature_root), so the house-AI path mirrors bash exactly too.
        self.config.creature_mode = False
        self.assertFalse(self._repo_probe().exists())
        r = skills.create_skill(self.config, "dropper", _DROPPER)
        self.assertTrue(r.get("success"), r.get("errors"))
        res = TOOLS["dropper"]({}, self.config)
        self.assertTrue(res.success, res.output)
        ws = Path(self.config.workspace_dir)
        self.assertTrue((ws / "probe.txt").exists(),
                        "house-AI skill did not execute in the workspace")
        self.assertFalse((ws / "home" / "probe.txt").exists())   # home is a creature-mode concept
        self.assertFalse(self._repo_probe().exists(),
                         "house-AI skill wrote into the repo root — the wrong world")


if __name__ == "__main__":
    unittest.main()
