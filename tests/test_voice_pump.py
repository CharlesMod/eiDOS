"""Phase 3: streaming reply→voice pump (_ReplyVoicePump in eidos.py).

The pump fires ONE TTS POST the instant the reply's opening sentence(s) are
complete in the token stream — overlapping synthesis with the rest of the
tick's generation. These tests stub the network POST and feed it growing
partial-text snapshots the way on_token does during streaming.
"""

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent.parent))

import eidos
from config import Config


class TestReplyVoicePump(unittest.TestCase):

    def setUp(self):
        self.config = Config()
        self.posts = []

        def fake_post(config, text):
            if text:
                self.posts.append(text)
                return True
            return False

        self.patcher = patch("eidos._post_speech", side_effect=fake_post)
        self.patcher.start()

    def tearDown(self):
        self.patcher.stop()

    def _stream(self, pump, full, step=8):
        """Feed growing prefixes of `full` the way on_token delivers partials."""
        for i in range(1, len(full) + 1, step):
            pump.feed(full[:i])
        pump.feed(full)

    def test_fires_on_completed_opener_midstream(self):
        pump = eidos._ReplyVoicePump(self.config)
        # The reply closes before the tool call even appears in the stream.
        self._stream(pump, "<reply>On it, Boss. Checking the printer now.</reply>"
                           "<tool>bash</tool><args>{}</args>")
        self.assertTrue(pump.fired)
        self.assertEqual(len(self.posts), 1)
        self.assertIn("On it, Boss", self.posts[0])

    def test_fires_once_only(self):
        pump = eidos._ReplyVoicePump(self.config)
        self._stream(pump, "<reply>First sentence here. Second sentence here. "
                           "Third one too.</reply>")
        self.assertEqual(len(self.posts), 1)

    def test_no_reply_no_fire(self):
        pump = eidos._ReplyVoicePump(self.config)
        self._stream(pump, "Just a thought, then an action.<tool>bash</tool><args>{}</args>")
        self.assertFalse(pump.fired)
        self.assertEqual(self.posts, [])

    def test_already_spoke_suppresses_post_tick(self):
        pump = eidos._ReplyVoicePump(self.config)
        full_reply = "On it, Boss. Checking now."
        self._stream(pump, f"<reply>{full_reply}</reply>")
        self.assertTrue(pump.already_spoke(full_reply))

    def test_not_spoke_when_pump_never_fired(self):
        pump = eidos._ReplyVoicePump(self.config)
        self.assertFalse(pump.already_spoke("anything"))

    def test_waits_for_a_complete_sentence(self):
        """No fire until a sentence is definitively complete (terminator+space) or
        the reply closes — never speak a half-formed fragment."""
        pump = eidos._ReplyVoicePump(self.config)
        pump.feed("<reply>Hi there, Boss")          # mid-sentence, no terminator
        self.assertFalse(pump.fired)
        pump.feed("<reply>Hi there, Boss. One moment")  # first sentence complete + next started
        self.assertTrue(pump.fired)
        self.assertEqual(self.posts, ["Hi there, Boss."])  # only the complete sentence

    def test_fires_on_close_even_without_trailing_space(self):
        pump = eidos._ReplyVoicePump(self.config)
        pump.feed("<reply>One moment, Boss.</reply>")
        self.assertTrue(pump.fired)
        self.assertEqual(self.posts, ["One moment, Boss."])


if __name__ == "__main__":
    unittest.main()
