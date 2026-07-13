"""Notebook de-echo: a near-verbatim line is NOT written, so the active notebook (re-injected into
context every tick) can't become a litany the creature imitates ("the wall is my horizon" ×N)."""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import notes
from config import Config


class TestNoteDedup(unittest.TestCase):
    def setUp(self):
        self.cfg = Config()
        self.cfg.workspace_dir = tempfile.mkdtemp()

    def test_near_verbatim_repeat_is_dropped_new_line_is_kept(self):
        name, dropped = notes.append_note(self.cfg, "thought", "the wall is my horizon.")
        self.assertFalse(dropped)
        _, dropped = notes.append_note(self.cfg, "thought", "the wall is my horizon")  # near-verbatim
        self.assertTrue(dropped)
        _, dropped = notes.append_note(self.cfg, "thought", "i found a new blue file today")
        self.assertFalse(dropped)                                   # genuinely new content is kept
        # The dropped line never reached disk: only two lines in the notebook.
        body = notes.read_note(self.cfg, "thought")
        self.assertEqual(len([ln for ln in body.splitlines() if ln.strip()]), 2)

    def test_dedup_is_scoped_per_notebook(self):
        notes.append_note(self.cfg, "a", "the same line")
        _, dropped = notes.append_note(self.cfg, "b", "the same line")   # different notebook
        self.assertFalse(dropped)                                        # not a repeat in 'b'


if __name__ == "__main__":
    unittest.main()
