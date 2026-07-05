"""Recall broadening (Concern 3: re-deriving stored facts instead of recalling).

The otter re-derived the ToolResult signature ~8 times though it was already stored, because
_build_relevant_recall keyed the BM25 query ONLY on the top plan line — "create a status skill"
never lexically matches a stored "ToolResult requires (...)" fact. The query now folds in the
active objective (goal-relevant priors) and the tools recently USED, so procedural knowledge about
a tool surfaces exactly when the creature is working with that tool.

No services / GPU — temp workspace only.
"""
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import context
import knowledge
import objectives
from config import Config
from memory import write_plan, append_observation


class TestRecallBroadening(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.cfg = Config()
        self.cfg.workspace_dir = os.path.join(self.tmp, "workspace")
        os.makedirs(self.cfg.workspace_dir, exist_ok=True)
        self.cfg.knowledge_enabled = True
        self.cfg.knowledge_recall_top_k = 2      # tight budget → ranking matters
        knowledge._index_cache = None
        knowledge._index_mtime = 0.0
        knowledge._invalidate_bm25_cache()
        # The procedural fact we want surfaced when working with create_skill — its words do NOT
        # appear in the plan line.
        knowledge.store_entry(
            self.cfg,
            "create_skill returns a ToolResult with fields success output full_output_path "
            "duration_s — get the signature right or it errors",
            tags=["create_skill"], category="procedures", confidence="verified")
        # Distractors the plan line matches BETTER, so with a tight top_k they crowd the
        # procedural fact out unless the query is broadened toward the tool/goal.
        for i, d in enumerate([
            "the status tracker room sensor reports temperature every minute",
            "a room status tracker should write to the status log file",
            "the room monitor tracker shows status of each device",
        ]):
            knowledge.store_entry(self.cfg, d, tags=["room"], category="facts")

    def _recall(self):
        return context._build_relevant_recall(self.cfg, exclude_ids=set())

    def test_plan_line_alone_ranks_out_the_procedural_fact(self):
        # The plan line matches the room distractors; the create_skill fact loses the top-k...
        write_plan(self.cfg, "# Plan\nStep 1: write a status tracker for the room")
        self.assertNotIn("ToolResult", self._recall())

    def test_objective_and_tool_context_surface_it(self):
        # ...but with the active goal + the tool it's been using folded into the query, it wins.
        write_plan(self.cfg, "# Plan\nStep 1: write a status tracker for the room")
        objectives.add(self.cfg, "Skill Library",
                       "build reusable skills with create_skill", tick=1)
        append_observation(self.cfg, {"tick": 2, "tool": "create_skill",
                                      "success": False, "output": "TypeError"})
        out = self._recall()
        self.assertIn("ToolResult", out)          # the procedural prior now surfaces

    def test_broadening_is_guarded_when_stores_absent(self):
        # No objective, no observations → the plan line still drives recall, nothing raises.
        write_plan(self.cfg, "# Plan\nStep 1: create_skill a ToolResult helper")
        out = self._recall()
        self.assertIn("ToolResult", out)


if __name__ == "__main__":
    unittest.main()
