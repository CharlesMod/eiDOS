"""A mind must be able to CORRECT its own memory (the store_entry swallow gotcha).

Found while hand-correcting the otter's false "create_skill is locked" belief: store_entry's
near-duplicate dedup returned the stale WRONG entry unchanged, silently swallowing the correction —
a 'verified: create_skill was never locked' could not replace the 'tentative: create_skill is
locked' it meant to fix. Now a HIGHER-confidence near-duplicate SUPERSEDES the entry in place;
a same-or-lower-confidence rewording is still dropped (anti-bloat).

No services / GPU — temp workspace only.
"""
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import knowledge
from config import Config


class _Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.cfg = Config()
        self.cfg.workspace_dir = self.tmp
        self.cfg.knowledge_enabled = True
        knowledge._index_cache = None
        knowledge._index_mtime = 0.0
        knowledge._invalidate_bm25_cache()

    def _entry(self, eid):
        return next((e for e in knowledge.load_index(self.cfg) if e["id"] == eid), None)


class TestCorrection(_Base):
    def test_higher_confidence_supersedes_in_place(self):
        # The real swallow case: a correction that reuses the error's phrasing (same subject) is a
        # near-duplicate, so it landed as a silent no-op. Now the higher confidence overwrites.
        wrong = knowledge.store_entry(
            self.cfg, "create_skill and edit_skill hit a ModuleNotFoundError for "
            "some_internal_module and seem blocked",
            tags=["create_skill"], category="errors", confidence="tentative")
        fix = knowledge.store_entry(
            self.cfg, "create_skill and edit_skill hit a ModuleNotFoundError for "
            "some_internal_module; RESOLVED, they are NOT blocked, my own import was the bug",
            tags=["create_skill", "resolved"], category="errors", confidence="verified")
        self.assertEqual(fix, wrong)                      # same entry (near-dup), corrected in place
        e = self._entry(wrong)
        self.assertEqual(e["confidence"], "verified")
        self.assertIn("RESOLVED", e["content_preview"])
        self.assertIn("NOT blocked", e["content_preview"])
        self.assertEqual(len(knowledge.load_index(self.cfg)), 1)   # no bloat — one entry

    def test_same_confidence_reword_is_dropped(self):
        a = knowledge.store_entry(self.cfg, "the broker lives at 10.0.0.5 on port 1883",
                                  tags=["broker"], confidence="tentative")
        b = knowledge.store_entry(self.cfg, "broker is at 10.0.0.5 port 1883 mqtt",
                                  tags=["broker"], confidence="tentative")
        self.assertEqual(a, b)                            # dedup returns the original
        self.assertEqual(len(knowledge.load_index(self.cfg)), 1)
        # the original content is untouched (a reword never overwrites)
        self.assertIn("lives at", self._entry(a)["content_preview"])

    def test_lower_confidence_never_overwrites(self):
        good = knowledge.store_entry(self.cfg, "the printer API base path is /api/v1 confirmed",
                                     tags=["printer"], confidence="verified")
        knowledge.store_entry(self.cfg, "maybe the printer API base path is /api/v1 possibly",
                              tags=["printer"], confidence="tentative")
        self.assertEqual(self._entry(good)["confidence"], "verified")   # unchanged

    def test_tags_merge_on_supersede(self):
        eid = knowledge.store_entry(self.cfg, "the delegate tool seems broken with module errors "
                                    "and internal failures",
                                    tags=["delegate"], category="errors", confidence="tentative")
        knowledge.store_entry(self.cfg, "the delegate tool seems fine now; the module errors and "
                              "internal failures were my own import bug",
                              tags=["delegate", "resolved", "self-diagnosis"],
                              category="errors", confidence="verified")
        self.assertEqual(set(self._entry(eid)["tags"]),
                         {"delegate", "resolved", "self-diagnosis"})

    def test_conf_rank_ordering(self):
        self.assertGreater(knowledge._conf_rank("verified"), knowledge._conf_rank("tentative"))
        self.assertGreater(knowledge._conf_rank("confident"), knowledge._conf_rank("likely"))
        # An unknown label sits MID, not at the floor — a "likely" dup must not downgrade it.
        self.assertEqual(knowledge._conf_rank("some-weird-label"), knowledge._CONF_UNKNOWN)
        self.assertEqual(knowledge._conf_rank("some-weird-label"), knowledge._conf_rank("likely"))

    def test_unknown_label_not_downgraded_by_likely(self):
        # A de-facto-strong belief stored with a non-vocabulary label must not be superseded by a
        # merely-"likely" restatement (the review's downgrade edge).
        eid = knowledge.store_entry(self.cfg, "the octoprint api base path is slash api slash v1 "
                                    "confirmed by probing", tags=["printer"], confidence="validated")
        knowledge.store_entry(self.cfg, "the octoprint api base path is slash api slash v1 "
                              "probably", tags=["printer"], confidence="likely")
        # 'validated' is known-high (3); even if it were unknown it's mid (1) ≥ likely (1) → no swap
        self.assertIn("confirmed", self._entry(eid)["content_preview"])


if __name__ == "__main__":
    unittest.main()
