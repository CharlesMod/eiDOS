"""P6 gates: exteroception — cheap pre-filters gate salience; only the salient escalates (acquires
the GPU and tokenizes); the raw modality never enters the core."""
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from nervous import (NervousBus, Kind, Modality, GpuArbiter,  # noqa: E402
                     PreFilter, FrameDiffFilter, VadFilter, Exteroceptor)


class TestPreFilters(unittest.TestCase):
    def test_vad(self):
        v = VadFilter(floor=0.2)
        self.assertEqual(v.score(0.1), 0.0)            # quiet -> nothing
        self.assertGreater(v.score(0.9), 0.5)          # loud -> salient

    def test_frame_diff(self):
        f = FrameDiffFilter()
        self.assertEqual(f.score(b"aaaa"), 1.0)        # first frame is novel
        self.assertEqual(f.score(b"aaaa"), 0.0)        # identical -> no salience
        self.assertGreater(f.score(b"bbbb"), 0.5)      # changed -> salient


class TestExteroceptor(unittest.TestCase):
    def test_below_threshold_never_enters_core(self):
        bus = NervousBus()
        self.addCleanup(bus.close)
        sub = bus.subscribe(topics={(Kind.percept, Modality.audio)})

        class Quiet(PreFilter):
            def score(self, raw):
                return 0.05

        ext = Exteroceptor(bus, name="mic", modality=Modality.audio, prefilter=Quiet(), threshold=0.3)
        self.assertIsNone(ext.observe(b"x"))
        self.assertEqual(ext.dropped, 1)
        self.assertIsNone(bus.recv(sub, timeout=0.1))  # the raw modality NEVER reached the core

    def test_salient_escalates_with_gpu_lease(self):
        bus = NervousBus()
        self.addCleanup(bus.close)
        sub = bus.subscribe(topics={(Kind.percept, Modality.vision)})
        arb = GpuArbiter()
        held = {}

        def tokenize(raw):
            held["holder"] = arb.current()             # the GPU is held during tokenization
            return {"seen": "motion"}

        ext = Exteroceptor(bus, name="cam", modality=Modality.vision, prefilter=FrameDiffFilter(),
                           threshold=0.3, arbiter=arb, tokenizer=tokenize)
        ext.observe(b"aaaa")                           # first frame is salient -> escalates
        self.assertEqual(ext.escalations, 1)
        self.assertEqual(held["holder"], "cam-escalation")   # acquired the GPU to tokenize
        self.assertIsNone(arb.current())               # and released it after
        e = bus.recv(sub, timeout=1.0)
        self.assertIsNotNone(e)
        self.assertEqual(e.kind, Kind.percept)
        payload = json.loads(bus.payloads.get(e.payload_ref).decode("utf-8"))
        self.assertEqual(payload["seen"], "motion")    # only the bounded percept crosses, not the frame


if __name__ == "__main__":
    unittest.main()
