"""platform_shell — the per-OS native shell layer (BIBLE/release: run the model's commands in bash on
POSIX, PowerShell on Windows). Pins: POSIX builds a `bash -lc`/`sh -c` list with the exit-code sidecar;
the PowerShell flag-translation never reaches a POSIX command; Windows still routes through PowerShell.
"""
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import platform_shell
import tools
from config import Config


class _Cfg:
    """Minimal config stand-in (build_shell_command only reads creature_* via tools helpers)."""
    creature_mode = False
    creature_shell = "powershell"
    posix_shell_login = True


class TestPosixShell(unittest.TestCase):
    def test_returns_list_with_command_flag(self):
        sh = platform_shell.posix_shell(_Cfg())
        self.assertIsInstance(sh, list)
        self.assertIn(sh[-1], ("-lc", "-c"))
        self.assertTrue(sh[0])                       # a shell path/name

    def test_login_flag_toggle(self):
        class C(_Cfg):
            posix_shell_login = False
        # only meaningful when bash is found; assert the flag honors the toggle either way
        sh = platform_shell.posix_shell(C())
        self.assertIn(sh[-1], ("-c",))               # login off => -c (works for bash and sh)


class TestPosixEpilogue(unittest.TestCase):
    def test_appends_exit_sidecar(self):
        arg = ["/bin/bash", "-lc", "echo hi"]
        out = platform_shell._posix_epilogue(arg, "/tmp/x.exit")
        self.assertEqual(out[:2], ["/bin/bash", "-lc"])
        self.assertIn("echo hi", out[-1])
        self.assertIn("__ec=$?", out[-1])
        self.assertIn("/tmp/x.exit", out[-1])

    def test_single_quote_in_path_is_escaped(self):
        out = platform_shell._posix_epilogue(["bash", "-lc", "true"], "/tmp/o'q.exit")
        self.assertIn("'\\''", out[-1])              # the path's quote is shell-escaped


class TestBuildShellCommand(unittest.TestCase):
    def test_posix_path(self):
        """Monkeypatched POSIX: bash/sh list form, no PowerShell, posix epilogue."""
        old = platform_shell.IS_WINDOWS
        platform_shell.IS_WINDOWS = False
        try:
            arg, use_shell, ep = platform_shell.build_shell_command("ls -la | grep x", _Cfg())
            self.assertFalse(use_shell)
            self.assertIsInstance(arg, list)
            self.assertIn(arg[-2], ("-lc", "-c"))
            self.assertEqual(arg[-1], "ls -la | grep x")   # command passed VERBATIM, not translated
            self.assertIs(ep, platform_shell._posix_epilogue)
        finally:
            platform_shell.IS_WINDOWS = old

    @unittest.skipUnless(os.name == "nt", "_route_windows_command is os.name-guarded to Windows")
    def test_windows_path_routes_through_powershell(self):
        old = platform_shell.IS_WINDOWS
        platform_shell.IS_WINDOWS = True
        try:
            arg, use_shell, ep = platform_shell.build_shell_command("ls", _Cfg())
            # bare command -> PowerShell list form (tools._route_windows_command), with the PS epilogue
            self.assertFalse(use_shell)
            self.assertEqual(arg[:1], ["powershell"])
            self.assertIs(ep, platform_shell._ps_epilogue)
        finally:
            platform_shell.IS_WINDOWS = old


class TestWindowsTranslationGuards(unittest.TestCase):
    """The PowerShell flag-translation must NEVER rewrite a POSIX command (defense-in-depth)."""
    def test_route_is_noop_off_windows(self):
        if os.name == "nt":
            self.skipTest("guard only fires off-Windows")
        self.assertEqual(tools._route_windows_command("ls -la"), ("ls -la", True))

    def test_lint_is_noop_off_windows(self):
        if os.name == "nt":
            self.skipTest("guard only fires off-Windows")
        self.assertIsNone(tools._lint_windows_command("for i in 1 2 3; do echo $i; done"))


class TestFirewallContainment(unittest.TestCase):
    """The creature world firewall denies reaching outside the home burrow on every platform."""
    def _cfg(self):
        c = Config()
        c.workspace_dir = tempfile.mkdtemp()
        c.creature_mode = True
        return c

    def test_denies_etc_and_traversal(self):
        c = self._cfg()
        self.assertIsNotNone(tools._creature_world_firewall("cat /etc/passwd", c))
        self.assertIsNotNone(tools._creature_world_firewall("cat ../secrets", c))
        self.assertIsNotNone(tools._creature_world_firewall("ls ~", c))

    def test_allows_relative_home_work(self):
        c = self._cfg()
        self.assertIsNone(tools._creature_world_firewall("echo hi > notes.txt", c))
        self.assertIsNone(tools._creature_world_firewall("ls -la", c))


if __name__ == "__main__":
    unittest.main()
