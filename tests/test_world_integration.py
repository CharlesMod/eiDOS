"""World phases W0+W1+W3 REAL integration — no stubs.

The three world phases were built in parallel against WORLD_PLAN §2 as a contract, each
tested against stubbed siblings. This file is the reconciliation: the real world.py under
the real context block, the real go tool, and the real dashboard builder, in one temp
workspace. If a contract drift ever lands, it breaks here — not in a live creature.
"""

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import world
import context as context_mod
import tools
import dashboard
from config import Config


def _cfg(enabled=True):
    c = Config()
    c.workspace_dir = tempfile.mkdtemp()
    c.workspace.mkdir(parents=True, exist_ok=True)
    c.state_dir.mkdir(parents=True, exist_ok=True)
    (c.workspace / "home").mkdir(parents=True, exist_ok=True)
    c.world_enabled = enabled
    return c


class TestRealWorldEndToEnd(unittest.TestCase):
    def test_context_block_renders_real_world(self):
        cfg = _cfg()
        block = context_mod._world_block(cfg, tick_number=3)
        self.assertTrue(block.startswith("## Where you are"))
        self.assertEqual(block.count("## Where you are"), 1)   # no double heading
        self.assertLessEqual(len(block), 900 + 50)             # W8 budget (+strip slack)

    def test_flag_off_no_block(self):
        cfg = _cfg(enabled=False)
        self.assertEqual(context_mod._world_block(cfg, tick_number=3), "")

    def test_go_tool_against_real_world(self):
        cfg = _cfg()
        tools.register_world_tool(cfg)
        try:
            self.assertIn("go", tools.TOOLS)
            r = tools.TOOLS["go"]({"place": "nowhere_real"}, cfg)
            self.assertFalse(r.success)
            self.assertEqual(r.fail_kind, "args")
            self.assertIn("the_commons", r.output)              # lists REAL place ids
            w = world.build_world(cfg)
            open_ids = [e.to for p in w.places.values() for e in p.exits if e.open]
            if open_ids:
                r2 = tools.TOOLS["go"]({"place": open_ids[0]}, cfg)
                self.assertTrue(r2.success, r2.output)
                self.assertEqual(world.current_place(cfg), open_ids[0])
        finally:
            cfg.world_enabled = False
            tools.register_world_tool(cfg)                      # unregister; leave no globals

    def test_dashboard_builder_returns_real_graph(self):
        cfg = _cfg()
        payload = dashboard.build_world(cfg)
        self.assertTrue(payload.get("enabled"))
        self.assertIn("places", payload)
        self.assertIn(payload.get("here"), payload["places"])
        for pid, place in payload["places"].items():
            for obj in place.get("objects", []):
                self.assertTrue(obj.get("referent"), f"{pid}/{obj.get('id')} lacks a referent")

    def test_dashboard_builder_dark_when_flag_off(self):
        cfg = _cfg(enabled=False)
        self.assertEqual(dashboard.build_world(cfg), {"enabled": False})


if __name__ == "__main__":
    unittest.main()
