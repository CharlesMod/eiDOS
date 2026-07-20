"""git_safety.py against a REAL temp git repo — the recovery core, previously only ever mocked.

Every recovery path (watchdog auto-rollback, operator restore, self-edit apply floor) bottoms
out in this module, so its behaviors are exercised here for real: checkpoint commit+tag,
workspace/ exclusion, last_good floor semantics (including set_last_good=False for the
pre-restore rescue checkpoints), PROTECT_PATHS skipping on restore, tag-collision uniqueness,
and prune keeping the active floor. _repo_root() is patched to the temp repo — these tests
must never touch the live repo's tags.
"""

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent.parent))

import git_safety
from config import Config


def _git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args],
                          capture_output=True, text=True, check=False)


class GitSafetyBase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.repo = self.tmp / "repo"
        self.repo.mkdir()
        _git(self.repo, "init", "-q")
        _git(self.repo, "config", "user.email", "test@test")
        _git(self.repo, "config", "user.name", "test")
        _git(self.repo, "config", "commit.gpgsign", "false")
        # A source file, a protected file, and runtime workspace state.
        (self.repo / "organ.py").write_text("v1\n")
        (self.repo / "safety.py").write_text("protected v1\n")  # basename in PROTECT_PATHS
        (self.repo / "workspace").mkdir()
        (self.repo / "workspace" / "state.json").write_text("{}\n")
        _git(self.repo, "add", "organ.py", "safety.py")
        _git(self.repo, "commit", "-q", "-m", "initial")

        self.config = Config()
        self.config.workspace_dir = str(self.tmp / "ws")
        self.config.workspace.mkdir(parents=True, exist_ok=True)
        self.config.state_dir.mkdir(parents=True, exist_ok=True)

        self._root_patch = patch.object(git_safety, "_repo_root", return_value=self.repo)
        self._root_patch.start()
        self.addCleanup(self._root_patch.stop)

    def _tags(self):
        return set(_git(self.repo, "tag", "--list").stdout.split())


class TestCheckpoint(GitSafetyBase):
    def test_checkpoint_commits_tags_and_sets_last_good(self):
        (self.repo / "organ.py").write_text("v2\n")
        res = git_safety.make_checkpoint(self.config, "unit test")
        self.assertTrue(res["ok"], res)
        self.assertIn(res["tag"], self._tags())
        self.assertEqual(git_safety.read_last_good(self.config), res["tag"])
        # The change really landed in the tagged commit.
        show = _git(self.repo, "show", f"{res['tag']}:organ.py").stdout
        self.assertEqual(show, "v2\n")

    def test_checkpoint_excludes_workspace(self):
        (self.repo / "workspace" / "state.json").write_text('{"runtime": true}\n')
        res = git_safety.make_checkpoint(self.config, "ws excluded")
        self.assertTrue(res["ok"], res)
        # workspace/ state must never be committed by a checkpoint.
        self.assertFalse(_git(self.repo, "cat-file", "-e",
                              f"{res['tag']}:workspace/state.json").returncode == 0)

    def test_set_last_good_false_does_not_move_floor(self):
        good = git_safety.make_checkpoint(self.config, "good floor")
        (self.repo / "organ.py").write_text("possibly bad\n")
        rescue = git_safety.make_checkpoint(self.config, "pre-restore rescue",
                                            set_last_good=False)
        self.assertTrue(rescue["ok"], rescue)
        self.assertIn(rescue["tag"], self._tags())          # rescue IS tagged (reversible)
        self.assertEqual(git_safety.read_last_good(self.config), good["tag"])  # floor unmoved

    def test_nothing_to_commit_still_tags_head(self):
        res = git_safety.make_checkpoint(self.config, "no changes")
        self.assertTrue(res["ok"], res)
        self.assertIn(res["tag"], self._tags())

    def test_same_second_tags_stay_unique(self):
        a = git_safety.make_checkpoint(self.config, "first")
        b = git_safety.make_checkpoint(self.config, "second")  # same wall-second is likely
        self.assertTrue(a["ok"] and b["ok"])
        self.assertNotEqual(a["tag"], b["tag"])


class TestRestore(GitSafetyBase):
    def test_restore_reverts_source_file(self):
        cp = git_safety.make_checkpoint(self.config, "before break")
        (self.repo / "organ.py").write_text("broken\n")
        _git(self.repo, "add", "organ.py")
        _git(self.repo, "commit", "-q", "-m", "bad change")
        res = git_safety.restore_to(self.config, cp["tag"])
        self.assertTrue(res["ok"], res)
        self.assertEqual((self.repo / "organ.py").read_text(), "v1\n")

    def test_restore_skips_protected_paths(self):
        cp = git_safety.make_checkpoint(self.config, "floor")
        (self.repo / "safety.py").write_text("protected v2 — newer safety machinery\n")
        _git(self.repo, "add", "safety.py")
        _git(self.repo, "commit", "-q", "-m", "safety upgrade")
        res = git_safety.restore_to(self.config, cp["tag"])
        self.assertTrue(res["ok"], res)
        # A stale checkpoint must never downgrade the safety machinery.
        self.assertIn("v2", (self.repo / "safety.py").read_text())

    def test_restore_keeps_files_added_after_tag(self):
        cp = git_safety.make_checkpoint(self.config, "floor")
        (self.repo / "new_module.py").write_text("added later\n")
        _git(self.repo, "add", "new_module.py")
        _git(self.repo, "commit", "-q", "-m", "add module")
        res = git_safety.restore_to(self.config, cp["tag"])
        self.assertTrue(res["ok"], res)
        # Documented conservative gap: restore never deletes — added files survive.
        self.assertTrue((self.repo / "new_module.py").exists())

    def test_restore_unknown_tag_errors(self):
        res = git_safety.restore_to(self.config, "eidos-good-nonexistent")
        self.assertFalse(res["ok"])

    def test_restore_defaults_to_last_good(self):
        cp = git_safety.make_checkpoint(self.config, "floor")
        (self.repo / "organ.py").write_text("drifted\n")
        _git(self.repo, "add", "organ.py")
        _git(self.repo, "commit", "-q", "-m", "drift")
        res = git_safety.restore_to(self.config)  # no tag → last_good
        self.assertTrue(res["ok"], res)
        self.assertEqual(res["tag"], cp["tag"])
        self.assertEqual((self.repo / "organ.py").read_text(), "v1\n")

    def test_restore_file_to_refuses_protected(self):
        cp = git_safety.make_checkpoint(self.config, "floor")
        res = git_safety.restore_file_to(self.config, "safety.py", cp["tag"])
        self.assertFalse(res["ok"])
        self.assertIn("protected", res["error"])


class TestPrune(GitSafetyBase):
    def test_prune_never_deletes_active_floor(self):
        first = git_safety.make_checkpoint(self.config, "oldest")
        for i in range(3):
            (self.repo / "organ.py").write_text(f"v{i + 10}\n")
            git_safety.make_checkpoint(self.config, f"cp{i}")
        # Point the floor at the OLDEST tag, then prune down hard.
        git_safety._write_last_good(self.config, first["tag"])
        git_safety.prune_checkpoints(self.config, keep=1)
        self.assertIn(first["tag"], self._tags())  # active floor survived the prune


if __name__ == "__main__":
    unittest.main()
