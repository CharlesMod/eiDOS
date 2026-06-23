"""BIBLE §2.11 delta prompting — the KV-stable head render cache.

The stable prefix (identity/self-guide/skills/learned/mission) must be re-RENDERED only when one of its
source files changes; otherwise the prior render is reused verbatim (byte-identical → llama.cpp prefix-KV
reuse intact). These pin: a cache hit reuses bytes and skips the render, a source-file change forces a
re-render, the TTL forces a periodic rebuild, and the kill-switch disables caching.
"""
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import context
from config import Config


class TestStableHeadCache(unittest.TestCase):

    def setUp(self):
        self.config = Config()
        self.config.workspace_dir = tempfile.mkdtemp()
        Path(self.config.goal_path).write_text("Map the LAN.", encoding="utf-8")
        # reset the process-local cache so tests don't bleed into each other
        context._STABLE_HEAD_CACHE.update({"sig": None, "blocks": None, "tick": -(10 ** 9)})
        self._calls = 0
        self._real = context._stable_head_blocks

        def _counting(cfg, creature):
            self._calls += 1
            return self._real(cfg, creature)
        context._stable_head_blocks = _counting
        self.addCleanup(lambda: setattr(context, "_stable_head_blocks", self._real))

    def test_hit_reuses_bytes_and_skips_render(self):
        a = context._cached_stable_head(self.config, False, 1)
        b = context._cached_stable_head(self.config, False, 2)
        self.assertEqual(a, b)            # byte-identical (same list of blocks)
        self.assertEqual(self._calls, 1)  # rendered once, reused on the second tick

    def test_goal_change_forces_rerender(self):
        context._cached_stable_head(self.config, False, 1)
        time.sleep(0.01)
        Path(self.config.goal_path).write_text("Map the LAN AND speak.", encoding="utf-8")
        context._cached_stable_head(self.config, False, 2)
        self.assertEqual(self._calls, 2)  # the file changed → re-rendered

    def test_ttl_forces_rebuild(self):
        context._cached_stable_head(self.config, False, 1)
        context._cached_stable_head(self.config, False, 1 + context._STABLE_HEAD_TTL)
        self.assertEqual(self._calls, 2)  # TTL expired → rebuilt even with no file change

    def test_kill_switch_always_renders(self):
        self.config.context_stable_head_cache = False
        context._cached_stable_head(self.config, False, 1)
        context._cached_stable_head(self.config, False, 2)
        self.assertEqual(self._calls, 2)  # caching off → render every call

    def test_content_matches_uncached(self):
        cached = context._cached_stable_head(self.config, False, 1)
        uncached = self._real(self.config, False)
        self.assertEqual(cached, uncached)   # the cache never changes the bytes


if __name__ == "__main__":
    unittest.main()
