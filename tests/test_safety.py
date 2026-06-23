"""Tests for safety module."""

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

import unittest
from unittest.mock import patch, MagicMock
from config import Config
from safety import is_command_blocked, check_disk_space, check_ram


class TestCommandBlocking(unittest.TestCase):

    def setUp(self):
        self.patterns = Config().protected_patterns

    def test_rm_rf_root(self):
        self.assertIsNotNone(is_command_blocked("rm -rf /", self.patterns))

    def test_rm_rf_with_path(self):
        self.assertIsNotNone(is_command_blocked("rm -rf /tmp/test", self.patterns))
        self.assertIsNone(is_command_blocked("rm /tmp/test.txt", self.patterns))

    def test_shutdown(self):
        self.assertIsNotNone(is_command_blocked("shutdown -h now", self.patterns))

    def test_reboot(self):
        self.assertIsNotNone(is_command_blocked("reboot", self.patterns))

    def test_kill_eidos(self):
        self.assertIsNotNone(is_command_blocked("kill eidos_process", self.patterns))

    def test_safe_commands(self):
        safe = ["ls -la", "echo hello", "cat /etc/hostname", "pip install requests", "pwd"]
        for cmd in safe:
            self.assertIsNone(is_command_blocked(cmd, self.patterns), f"Falsely blocked: {cmd}")

    def test_mkfs(self):
        self.assertIsNotNone(is_command_blocked("mkfs.ext4 /dev/sda1", self.patterns))

    def test_dd_to_device(self):
        self.assertIsNotNone(is_command_blocked("dd if=/dev/zero of=/dev/sda", self.patterns))

    def test_systemctl_stop_eidos(self):
        self.assertIsNotNone(is_command_blocked("systemctl stop eidos", self.patterns))

    def test_systemctl_start_ok(self):
        self.assertIsNone(is_command_blocked("systemctl start some_service", self.patterns))

    def test_empty_patterns(self):
        self.assertIsNone(is_command_blocked("rm -rf /", []))

    def test_case_insensitive(self):
        self.assertIsNotNone(is_command_blocked("SHUTDOWN -h now", self.patterns))


class TestResourceChecks(unittest.TestCase):

    def test_disk_space_returns_tuple(self):
        ok, gb = check_disk_space("/", min_gb=0.001)
        self.assertIsInstance(ok, bool)
        self.assertIsInstance(gb, float)
        self.assertTrue(ok)

    def test_disk_space_fail_high_threshold(self):
        ok, gb = check_disk_space("/", min_gb=999999)
        self.assertFalse(ok)

    def test_ram_returns_tuple(self):
        ok, pct = check_ram(max_pct=99.9)
        self.assertIsInstance(ok, bool)
        self.assertIsInstance(pct, float)

    @unittest.skipUnless(sys.platform == "win32", "check_ram is Windows-native; fails open on POSIX")
    def test_ram_tight_threshold(self):
        ok, pct = check_ram(max_pct=0.001)
        self.assertFalse(ok)




if __name__ == "__main__":
    unittest.main()
