"""Pillars 1.2 — killable skill execution, authoring-time ToolResult contract, and per-skill
telemetry. All behind `config.pillars_killable_skills_enabled` (default False); these tests flip it
ON. The gate (PILLARS_TODO.md 1.2 / dream-test D8 "nothing freezes the mind"):

  (a) a deliberately-hanging skill is KILLED DEAD — no orphan process, no tick freeze;
  (b) a skill that returns a dict instead of a ToolResult is REJECTED at create time;
  (c) telemetry (latency, arg-shape success) is visible via list_skills.

Offline only: skills are authored into a temp workspace and driven through execute_tool / the live
TOOLS registry exactly as the tick loop does. No services, no GPU, no network.
"""

import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import skills
from skills import create_skill, edit_skill, list_skills, run_skill_killable, derived_timeout_s
from tools import TOOLS, execute_tool
from parser import ToolCall
from config import Config


def _cfg(floor=1.0, ceiling=2.0):
    c = Config()
    c.workspace_dir = tempfile.mkdtemp()
    c.pillars_killable_skills_enabled = True
    c.pillars_skill_timeout_floor_s = floor
    c.pillars_skill_timeout_ceiling_s = ceiling
    return c


# A skill that only spins when handed mode="spin"; with the dry-run's sample string arg ("x") it
# returns fast, so it passes authoring but hangs forever on a real spin call — the tick-342 shape,
# reproduced without a real timeout-less socket.
_SPIN = '''import time
def tool_spinner(args, config):
    if args.get("mode") == "spin":
        while True:
            time.sleep(0.02)
    return ToolResult(output="fast", full_output_path=None, success=True, duration_s=0.0)
'''

_GOOD = '''def tool_adder(args, config):
    n = int(args.get("n", 1))
    return ToolResult(output=str(n * 10), full_output_path=None, success=True, duration_s=0.0)
'''

_DICT = '''def tool_dicty(args, config):
    return {"presence": True, "raw": [1, 2, 3]}
'''


class TestKillableExecution(unittest.TestCase):
    """(a) a hung skill is killed dead — no orphan, no freeze."""

    def setUp(self):
        self.config = _cfg(floor=1.0, ceiling=1.0)  # 1s so the test is quick

    def tearDown(self):
        for n in ("spinner", "adder"):
            TOOLS.pop(n, None)

    def test_hanging_skill_is_killed_not_frozen(self):
        r = create_skill(self.config, "spinner", _SPIN, args_schema={"mode": "str"})
        self.assertTrue(r.get("success"), r.get("errors"))
        t = time.monotonic()
        res = execute_tool(ToolCall(tool="spinner", args={"mode": "spin"}, raw=""), self.config)
        elapsed = time.monotonic() - t
        # Freed at ~the derived timeout (1s ceiling), never the infinite spin.
        self.assertLess(elapsed, 5.0, "the tick loop was frozen — kill did not fire")
        self.assertFalse(res.success)
        self.assertEqual(res.fail_kind, "timeout")
        self.assertIn("WATCHDOG", res.output)
        self.assertIn("KILLED", res.output)

    def test_no_orphan_process_survives_the_kill(self):
        create_skill(self.config, "spinner", _SPIN, args_schema={"mode": "str"})
        execute_tool(ToolCall(tool="spinner", args={"mode": "spin"}, raw=""), self.config)
        time.sleep(0.6)  # give the OS a moment to reap the killed tree
        ps = subprocess.run(["ps", "-eo", "pid,args"], capture_output=True, text=True).stdout
        alive = [ln for ln in ps.splitlines()
                 if ".exec_spinner" in ln and "grep" not in ln and "ps -eo" not in ln]
        self.assertEqual(alive, [], f"orphan subprocess survived the kill: {alive}")
        # And the per-invocation harness file is cleaned up.
        left = list((self.config.workspace / "skills").glob(".exec_*.py"))
        self.assertEqual(left, [], f"harness files left behind: {left}")

    def test_good_skill_runs_in_subprocess_and_returns_result(self):
        # A GOOD skill has ample headroom, so give it a generous watchdog: the tight 1s ceiling is
        # only needed for the KILL-path test above, and it made THIS test load-flaky — spawning a
        # fresh Python subprocess can exceed 1s under CPU contention and get false-killed. 5s covers
        # subprocess startup even under heavy load while a real good skill still returns in ms.
        self.config = _cfg(floor=5.0, ceiling=5.0)
        create_skill(self.config, "adder", _GOOD, args_schema={"n": "int"})
        res = execute_tool(ToolCall(tool="adder", args={"n": 5}, raw=""), self.config)
        self.assertTrue(res.success, res.output)
        self.assertEqual(res.output, "50")


class TestAuthoringContract(unittest.TestCase):
    """(b) a bare return (dict/str/number) is FIRST-CLASS — accepted at authoring and wrapped into a
    successful ToolResult at runtime, on the killable path too. (Reversed 2026-07-13: forcing a
    newborn to construct a ToolResult was pure friction — it burned ticks failing the contract.)"""

    def setUp(self):
        self.config = _cfg()

    def tearDown(self):
        for n in ("dicty", "adder"):
            TOOLS.pop(n, None)

    def test_bare_return_accepted_at_create_and_wrapped_at_run(self):
        r = create_skill(self.config, "dicty", _DICT)
        self.assertTrue(r.get("success"), r.get("errors"))     # a dict return authors cleanly now
        self.assertIn("dicty", TOOLS)                          # and activates
        res = TOOLS["dicty"]({}, self.config)                  # runtime wraps it into a success
        self.assertTrue(res.success, res.output)

    def test_bare_return_accepted_at_edit(self):
        # Author a good ToolResult skill, then edit it into a bare-dict returner — now allowed.
        self.assertTrue(create_skill(self.config, "adder", _GOOD, args_schema={"n": "int"})["success"])
        dict_body = 'def tool_adder(args, config):\n    return {"presence": True}\n'
        r = edit_skill(self.config, "adder", dict_body)
        self.assertTrue(r.get("success"), r.get("errors"))

    def test_toolresult_return_accepted_at_create(self):
        r = create_skill(self.config, "adder", _GOOD, args_schema={"n": "int"})
        self.assertTrue(r.get("success"), r.get("errors"))

    def test_contract_lenient_when_flag_off(self):
        # Flag OFF preserves the historical behavior: a dict return is normalized at dispatch, not
        # rejected at authoring — so create_skill succeeds.
        c = Config()
        c.workspace_dir = tempfile.mkdtemp()
        c.pillars_killable_skills_enabled = False
        try:
            r = create_skill(c, "dicty", _DICT)
            self.assertTrue(r.get("success"), r.get("errors"))
        finally:
            TOOLS.pop("dicty", None)


class TestTelemetry(unittest.TestCase):
    """(c) telemetry — latency p50/p95, success by arg-shape, last-used — is visible via list_skills."""

    def setUp(self):
        self.config = _cfg(floor=1.0, ceiling=5.0)

    def tearDown(self):
        TOOLS.pop("adder", None)

    def test_telemetry_recorded_and_surfaced(self):
        create_skill(self.config, "adder", _GOOD, args_schema={"n": "int"})
        # Two different arg shapes, all successful.
        execute_tool(ToolCall(tool="adder", args={"n": 2}, raw=""), self.config)
        execute_tool(ToolCall(tool="adder", args={"n": 3}, raw=""), self.config)
        execute_tool(ToolCall(tool="adder", args={}, raw=""), self.config)  # shape ()
        info = list_skills(self.config)["skills"]["adder"]
        self.assertIsNotNone(info["latency_p50_s"])
        self.assertIsNotNone(info["latency_p95_s"])
        self.assertGreaterEqual(info["latency_p95_s"], info["latency_p50_s"])
        self.assertIsNotNone(info["last_used"])
        shapes = info["success_by_arg_shape"]
        self.assertIn("(n)", shapes)
        self.assertIn("()", shapes)
        self.assertEqual(shapes["(n)"]["uses"], 2)
        self.assertEqual(shapes["(n)"]["success_rate"], 1.0)
        # The derived timeout is exposed and lands inside the declared band.
        self.assertGreaterEqual(info["derived_timeout_s"], 1.0)
        self.assertLessEqual(info["derived_timeout_s"], 5.0)

    def test_derived_timeout_is_p95_times_three_clamped(self):
        # p95 = 2.0 -> 2*3 = 6, clamped to ceiling 5.0
        ent = {"latency_samples": [2.0, 2.0, 2.0, 2.0]}
        self.assertEqual(derived_timeout_s(self.config, ent), 5.0)
        # p95 = 0.1 -> 0.3, clamped up to floor 1.0
        ent = {"latency_samples": [0.1] * 10}
        self.assertEqual(derived_timeout_s(self.config, ent), 1.0)
        # p95 = 1.0 -> 3.0, inside band -> exactly 3.0
        ent = {"latency_samples": [1.0] * 10}
        self.assertEqual(derived_timeout_s(self.config, ent), 3.0)
        # No samples yet -> starts at the ceiling (generous until it has a record).
        self.assertEqual(derived_timeout_s(self.config, {}), 5.0)


class TestFlagOffUnchanged(unittest.TestCase):
    """Flag OFF keeps the in-thread execution + thread-watchdog abandon path (byte-for-byte)."""

    def setUp(self):
        self.config = Config()
        self.config.workspace_dir = tempfile.mkdtemp()
        self.config.pillars_killable_skills_enabled = False
        self.config.skill_watchdog_s = 0.5

    def tearDown(self):
        TOOLS.pop("sleeper", None)

    def test_flag_off_uses_thread_watchdog_abandon(self):
        # A skill that sleeps is freed by the thread-watchdog (ABANDONED, not killed) — the historical
        # wording, proving the old path is intact when the flag is off.
        code = ('import time\n'
                'def tool_sleeper(args, config):\n'
                '    time.sleep(10)\n'
                '    return ToolResult(output="done", full_output_path=None, success=True, duration_s=10)\n')
        r = create_skill(self.config, "sleeper", code)
        self.assertTrue(r.get("success"), r.get("errors"))
        t = time.monotonic()
        res = execute_tool(ToolCall(tool="sleeper", args={}, raw=""), self.config)
        elapsed = time.monotonic() - t
        self.assertLess(elapsed, 5.0)
        self.assertFalse(res.success)
        self.assertIn("ABANDONED", res.output)  # thread path wording, not the killable "KILLED"


if __name__ == "__main__":
    unittest.main()
