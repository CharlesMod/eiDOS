"""IDE backend (ide.py StintManager) — spawn contract, turn gating, caps.

Mocks Popen + the reader Thread so no real pi is launched (the live end-to-end is
the manual smoke in workspace/ide). Pins the RPC invocation shape and the
one-turn-at-a-time backpressure the interactive protocol requires.
"""

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).parent.parent))

import ide
from config import Config


class TestStintManager(unittest.TestCase):

    def setUp(self):
        self.config = Config()
        self.config.workspace_dir = tempfile.mkdtemp()

    def _mgr_with_mock_pi(self):
        proc = MagicMock(pid=4321)
        proc.stdin = MagicMock()
        ctx = (patch.object(ide.subprocess, "Popen", return_value=proc),
               patch.object(ide.threading, "Thread"),
               patch.object(ide, "_resolve_pi", return_value="pi.cmd"))
        return ide.StintManager(self.config), proc, ctx

    def test_create_spawn_contract(self):
        mgr, proc, ctx = self._mgr_with_mock_pi()
        with ctx[0] as popen, ctx[1], ctx[2]:
            stint, err = mgr.create("scraper")
        self.assertIsNotNone(stint)
        self.assertIsNone(err)
        (argv,), kw = popen.call_args
        self.assertIs(kw["stdin"], ide.subprocess.PIPE)
        self.assertEqual(kw["env"]["PYTHONUTF8"], "1")
        for flag in ("--mode", "rpc", "--provider", "--session-dir", "-a"):
            self.assertIn(flag, argv)
        self.assertNotIn("-p", argv)               # interactive, not one-shot
        self.assertIn("ide", str(kw["cwd"]))       # sandbox under workspace/ide/stints
        self.assertEqual(stint.status, "running")

    def test_prompt_turn_gating(self):
        mgr, proc, ctx = self._mgr_with_mock_pi()
        with ctx[0], ctx[1], ctx[2]:
            stint, _ = mgr.create("t")
            ok, err = mgr.prompt(stint.sid, "build a CLI")
            self.assertTrue(ok)
            self.assertTrue(stint.turn_active)
            ok2, err2 = mgr.prompt(stint.sid, "and tests")   # turn in flight
            self.assertFalse(ok2)
            self.assertIn("turn", err2)
        written = "".join(c.args[0] for c in proc.stdin.write.call_args_list)
        self.assertIn('"type": "prompt"', written)
        self.assertIn("build a CLI", written)

    def test_unresolvable_pi(self):
        with patch.object(ide, "_resolve_pi", return_value=""):
            stint, err = ide.StintManager(self.config).create("t")
        self.assertIsNone(stint)
        self.assertIn("pi", err)

    def test_max_stints_cap(self):
        self.config.ide_max_stints = 2
        mgr, proc, ctx = self._mgr_with_mock_pi()
        with ctx[0], ctx[1], ctx[2]:
            self.assertIsNotNone(mgr.create("a")[0])
            self.assertIsNotNone(mgr.create("b")[0])
            stint, err = mgr.create("c")
        self.assertIsNone(stint)
        self.assertIn("too many", err)

    def test_prompt_unknown_stint(self):
        ok, err = ide.StintManager(self.config).prompt("nope", "hi")
        self.assertFalse(ok)
        self.assertIn("no such", err)


class TestColdResume(unittest.TestCase):

    def setUp(self):
        self.config = Config()
        self.config.workspace_dir = tempfile.mkdtemp()

    def _make(self):
        proc = MagicMock(pid=1)
        proc.stdin = MagicMock()
        with patch.object(ide.subprocess, "Popen", return_value=proc), \
             patch.object(ide.threading, "Thread"), \
             patch.object(ide, "_resolve_pi", return_value="pi.cmd"):
            mgr = ide.StintManager(self.config)
            stint, _ = mgr.create("persist")
        return mgr, stint

    def test_close_makes_cold_then_load_and_resume(self):
        mgr, stint = self._make()
        sid = stint.sid
        mgr.close(sid)
        self.assertEqual(stint.status, "cold")
        # a brand-new manager (service restart) rediscovers it from disk as cold
        mgr2 = ide.StintManager(self.config)
        mgr2.load_cold()
        self.assertIn(sid, mgr2.stints)
        self.assertEqual(mgr2.stints[sid].status, "cold")
        # resume re-spawns pi --continue
        with patch.object(ide.subprocess, "Popen", return_value=MagicMock(pid=2)) as popen, \
             patch.object(ide.threading, "Thread"), \
             patch.object(ide, "_resolve_pi", return_value="pi.cmd"):
            ok, err = mgr2.resume(sid)
        self.assertTrue(ok, err)
        self.assertEqual(mgr2.stints[sid].status, "running")
        self.assertIn("--continue", popen.call_args[0][0])

    def test_prompt_on_cold_asks_to_resume(self):
        mgr, stint = self._make()
        mgr.close(stint.sid)
        ok, err = mgr.prompt(stint.sid, "hi")
        self.assertFalse(ok)
        self.assertIn("resume", err)

    def test_reap_orphans_clears_pidfile(self):
        mgr, stint = self._make()
        self.assertTrue(mgr._pidfile().exists())
        with patch.object(ide, "_kill_tree") as kill:
            mgr.reap_orphans()
        kill.assert_called()                      # the live pid was targeted
        self.assertEqual(mgr._pidfile().read_text(), "[]")


class TestCodeSurfaces(unittest.TestCase):

    def setUp(self):
        self.config = Config()
        self.config.workspace_dir = tempfile.mkdtemp()

    def _stint(self):
        proc = MagicMock(pid=1)
        proc.stdin = MagicMock()
        with patch.object(ide.subprocess, "Popen", return_value=proc), \
             patch.object(ide.threading, "Thread"), \
             patch.object(ide, "_resolve_pi", return_value="pi.cmd"):
            mgr = ide.StintManager(self.config)
            stint, _ = mgr.create("t")
        return mgr, stint

    def test_tree_lists_and_skips(self):
        mgr, stint = self._stint()
        (stint.work / "main.py").write_text("print('hi')")
        (stint.work / "pkg").mkdir()
        (stint.work / "pkg" / "a.txt").write_text("A")
        (stint.work / "node_modules").mkdir()
        (stint.work / "node_modules" / "junk.js").write_text("x")
        names = {i["name"] for i in mgr.tree(stint.sid, "")}
        self.assertIn("main.py", names)
        self.assertIn("pkg", names)
        self.assertNotIn("node_modules", names)
        self.assertEqual({i["name"] for i in mgr.tree(stint.sid, "pkg")}, {"a.txt"})

    def test_read_file(self):
        mgr, stint = self._stint()
        (stint.work / "main.py").write_text("print('hi')")
        res, err = mgr.read_file(stint.sid, "main.py")
        self.assertIsNone(err)
        self.assertIn("hi", res["content"])

    def test_sandbox_escape_blocked(self):
        mgr, stint = self._stint()
        self.assertIsNone(ide._safe_path(stint.work, "../../../etc/passwd"))
        res, err = mgr.read_file(stint.sid, "../../../config.toml")
        self.assertIsNone(res)
        self.assertIn("no such", err)

    def test_binary_file_rejected(self):
        mgr, stint = self._stint()
        (stint.work / "b.bin").write_bytes(b"\x00\x01\x02ELF")
        res, err = mgr.read_file(stint.sid, "b.bin")
        self.assertIsNone(res)
        self.assertIn("binary", err)

    def test_zip_excludes_skip_dirs(self):
        mgr, stint = self._stint()
        (stint.work / "main.py").write_text("x")
        (stint.work / ".git").mkdir()
        (stint.work / ".git" / "HEAD").write_text("ref")
        data, err = mgr.zip_work(stint.sid)
        self.assertIsNone(err)
        import io as _io
        import zipfile as _zf
        names = _zf.ZipFile(_io.BytesIO(data)).namelist()
        self.assertTrue(any("main.py" in n for n in names))
        self.assertFalse(any(".git" in n for n in names))


if __name__ == "__main__":
    unittest.main()
