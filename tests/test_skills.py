"""Skill lifecycle hygiene: per-version stats, trust promotion/demotion, dead-skill
quarantine, and brief visibility.

The manifest data these enforce came from a live run: bigprinter_status 0/8, query_klipper
0/6, camera_snapshot 0/4 — all still 'active' and offered to the model every tick, because
nothing ever culled a dead skill and stats were lifetime (so trust survived broken rewrites).
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


if __name__ == "__main__":
    unittest.main()
