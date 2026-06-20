"""P2 gates: the GPU lease arbiter — mutual exclusion, priority preemption, liveness reclaim, the
speech-gate as its first client, and GPU contention published so the body feels it."""
import json
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from nervous import NervousBus, Kind, Modality, Delivery  # noqa: E402
from nervous.arbiter import GpuArbiter, PRI_MIND, PRI_SPEECH, PRI_REFLEX  # noqa: E402


class TestArbiter(unittest.TestCase):
    def test_mutual_exclusion(self):
        arb = GpuArbiter()
        a = arb.acquire("a", PRI_MIND)
        self.assertEqual(arb.current(), "a")
        got = {}

        def grab():
            got["b"] = arb.acquire("b", PRI_MIND, timeout=2.0)

        t = threading.Thread(target=grab)
        t.start()
        time.sleep(0.2)
        self.assertEqual(arb.current(), "a")          # b is blocked while a holds
        self.assertNotIn("b", got)
        arb.release(a)
        t.join(timeout=2.0)
        self.assertIsNotNone(got.get("b"))            # b gets it on release
        self.assertEqual(arb.current(), "b")

    def test_priority_preemption(self):
        arb = GpuArbiter()
        mind = arb.acquire("mind", PRI_MIND)
        speech = arb.acquire("speech", PRI_SPEECH)    # higher priority preempts
        self.assertTrue(mind.preempted.is_set())       # the lower holder is told to yield
        self.assertEqual(arb.current(), "speech")
        self.assertIsNotNone(speech)

    def test_liveness_reclaim(self):
        tmp = tempfile.mkdtemp(prefix="arb-")
        log = os.path.join(tmp, "leases.jsonl")
        try:
            arb = GpuArbiter(log_path=log)
            stuck = arb.acquire("stuck", PRI_MIND, max_s=0.1)   # acquires, then never progresses
            got = {}

            def grab():
                got["b"] = arb.acquire("b", PRI_MIND, timeout=3.0)

            t = threading.Thread(target=grab)
            t.start()
            t.join(timeout=3.0)
            self.assertIsNotNone(got.get("b"))          # the wedged holder is reclaimed
            self.assertTrue(stuck.preempted.is_set())
            self.assertEqual(arb.current(), "b")
            entries = [json.loads(line) for line in open(log, encoding="utf-8") if line.strip()]
            self.assertTrue(any(e["action"] == "reclaim" for e in entries))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_speech_gate_as_first_client(self):
        # the existing speech-gate, expressed as two arbiter clients: TTS outranks the mind tick, so
        # the tick yields to live speech and resumes when speech ends.
        arb = GpuArbiter()
        mind = arb.acquire("mind", PRI_MIND)            # tick holds the GPU
        self.assertEqual(arb.current(), "mind")
        speech = arb.acquire("speech", PRI_SPEECH)      # TTS arrives -> tick yields
        self.assertTrue(mind.preempted.is_set())
        self.assertEqual(arb.current(), "speech")
        arb.release(speech)                             # speech done
        mind2 = arb.acquire("mind", PRI_MIND, timeout=1.0)   # tick resumes
        self.assertIsNotNone(mind2)
        self.assertEqual(arb.current(), "mind")

    def test_contention_published_and_felt(self):
        bus = NervousBus()
        self.addCleanup(bus.close)
        arb = GpuArbiter(bus=bus)
        sub = bus.subscribe(topics={(Kind.capability, Modality.system)},
                            deliveries={Delivery.retained})
        lease = arb.acquire("vision-escalation", PRI_REFLEX)
        e = bus.recv(sub, timeout=1.0)
        self.assertIsNotNone(e)
        payload = json.loads(bus.payloads.get(e.payload_ref).decode("utf-8"))
        self.assertEqual(payload["gpu_holder"], "vision-escalation")
        self.assertGreater(e.salience, 0.0)             # a non-mind holder is FELT (contention)
        arb.release(lease)


if __name__ == "__main__":
    unittest.main()
