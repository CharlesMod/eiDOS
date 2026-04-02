"""Tests for safety module."""

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

import unittest
from config import Config
from safety import is_command_blocked, check_disk_space, check_ram


class TestCommandBlocking(unittest.TestCase):

    def setUp(self):
        self.patterns = Config().protected_patterns

    def test_rm_rf_root(self):
        self.assertIsNotNone(is_command_blocked("rm -rf /", self.patterns))

    def test_rm_rf_with_path(self):
        # rm -rf /path is also blocked (pattern matches any rm -rf /)
        self.assertIsNotNone(is_command_blocked("rm -rf /tmp/test", self.patterns))
        # rm without -rf is fine
        self.assertIsNone(is_command_blocked("rm /tmp/test.txt", self.patterns))

    def test_shutdown(self):
        self.assertIsNotNone(is_command_blocked("shutdown -h now", self.patterns))

    def test_reboot(self):
        self.assertIsNotNone(is_command_blocked("reboot", self.patterns))

    def test_kill_kairos(self):
        self.assertIsNotNone(is_command_blocked("kill kairos_process", self.patterns))

    def test_safe_commands(self):
        safe = ["ls -la", "echo hello", "cat /etc/hostname", "pip install requests", "pwd"]
        for cmd in safe:
            self.assertIsNone(is_command_blocked(cmd, self.patterns), f"Falsely blocked: {cmd}")

    def test_mkfs(self):
        self.assertIsNotNone(is_command_blocked("mkfs.ext4 /dev/sda1", self.patterns))

    def test_dd_to_device(self):
        self.assertIsNotNone(is_command_blocked("dd if=/dev/zero of=/dev/sda", self.patterns))

    def test_systemctl_stop_kairos(self):
        self.assertIsNotNone(is_command_blocked("systemctl stop kairos", self.patterns))

    def test_systemctl_start_ok(self):
        # start should NOT be blocked
        self.assertIsNone(is_command_blocked("systemctl start some_service", self.patterns))


class TestResourceChecks(unittest.TestCase):

    def test_disk_space_returns_tuple(self):
        ok, gb = check_disk_space("/", min_gb=0.001)
        self.assertIsInstance(ok, bool)
        self.assertIsInstance(gb, float)
        self.assertTrue(ok)  # Should have at least 1MB free

    def test_ram_returns_tuple(self):
        ok, pct = check_ram(max_pct=99.9)
        self.assertIsInstance(ok, bool)
        self.assertIsInstance(pct, float)


if __name__ == "__main__":
    unittest.main()
