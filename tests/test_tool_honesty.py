"""Tool-honesty audit (ARCHITECTURE_PRINCIPLES #4) — pinned closed.

"The system never lies to the creature. Every tool result must reflect what actually happened.
success=True means the action took effect; a gate/cap/refusal/no-op is a visible failure
(success=False, typed fail_kind, the real rule in the output)."

The worst case (tool_objective_add's phantom successes) is pinned in test_progression_deadlock.py.
This file pins the OTHER lies found in the same audit — paths that swallowed a real failure and
returned a soothing success wrapped around nothing.

Style mirrors test_progression_deadlock.py: a temp-workspace Config and direct handler calls.
"""

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent.parent))

import tools
from config import Config


class _Base(unittest.TestCase):
    def setUp(self):
        self.cfg = Config()
        self.cfg.workspace_dir = tempfile.mkdtemp()
        (Path(self.cfg.workspace_dir) / "workspace").mkdir(parents=True, exist_ok=True)


class TestSpeakUnreachableIsHonest(_Base):
    """tool_speak returned success=True + 'submitted, it will play when reachable' when the POST
    to the voice service failed outright. The voice queue is in-memory + connection-scoped: nothing
    was submitted and nothing is held for later. That claim was a lie — a network failure the
    creature must be able to see (ARCHITECTURE_PRINCIPLES #4)."""

    def test_voice_unreachable_is_a_visible_network_failure(self):
        def _boom(*a, **k):
            raise ConnectionRefusedError("voice service down")
        # The voice POST goes through urllib.request.urlopen; make it fail like the service being down.
        with patch("urllib.request.urlopen", side_effect=_boom):
            r = tools.tool_speak({"text": "hello, boss"}, self.cfg)
        self.assertFalse(r.success)                       # a swallowed failure is NOT a success
        self.assertEqual(r.fail_kind, "network")          # typed as the failure it is
        self.assertNotIn("submitted", r.output.lower())   # it was NOT submitted
        self.assertNotIn("will play when reachable", r.output.lower())  # nothing is queued for later
        self.assertIn("fail", r.output.lower())            # the wall is named

    def test_unreachable_speak_still_mirrors_text_to_chat(self):
        """The failure is only about the SPOKEN action; the text mirror to Boss's chat is a separate
        best-effort side effect that legitimately still happens — and the honest output says so."""
        def _boom(*a, **k):
            raise ConnectionRefusedError("voice service down")
        with patch("urllib.request.urlopen", side_effect=_boom):
            r = tools.tool_speak({"text": "cornerstone words"}, self.cfg)
        chat = self.cfg.workspace / "chat_replies.jsonl"
        self.assertTrue(chat.exists() and "cornerstone words" in chat.read_text())
        self.assertIn("chat", r.output.lower())            # output tells the creature the text landed there
        self.assertFalse(r.success)

    def test_reachable_speak_still_succeeds(self):
        """The fix must not convert a genuine success into a failure: when the voice service answers,
        speak reports success as before."""
        class _Resp:
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def read(self): return b'{"ok": true, "id": "1", "delivered": 2}'
        with patch("urllib.request.urlopen", return_value=_Resp()):
            r = tools.tool_speak({"text": "out loud"}, self.cfg)
        self.assertTrue(r.success, r.output)
        self.assertEqual(r.fail_kind, "")


if __name__ == "__main__":
    unittest.main()
