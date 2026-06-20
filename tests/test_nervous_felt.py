"""P1b gates: the felt-qualia transfer function + the creature-render view (truth-rendering)."""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from nervous import NervousBus, AfferentContext, FeltStateView  # noqa: E402
from nervous.felt import to_felt, felt_state  # noqa: E402
from nervous.interoception import Interoception  # noqa: E402


def reader(**vals):
    base = {"ram_pct": None, "disk_free_gb": None, "cpu_pct": None,
            "vram_used_pct": None, "gpu_temp_c": None}
    base.update(vals)
    return lambda: base


class TestTransfer(unittest.TestCase):
    def test_to_felt_maps_bars_to_qualia(self):
        self.assertEqual(to_felt({"ram": "ok", "vram": "ok"})["overall"], "at ease")
        self.assertEqual(to_felt({"ram": "ok", "vram": "critical"})["overall"], "in distress")
        f = to_felt({"vram": "high", "cpu": "elevated"})
        self.assertEqual(f["overall"], "strained")            # worst bar = high -> strained
        self.assertIn("GPU tight", f["felt"])                 # the qualia, not "vram: high"
        self.assertIn("working hard", f["felt"])

    def test_felt_state_carries_bars_and_qualia(self):
        s = felt_state({"vram": "critical", "disk": None})
        self.assertEqual(s["bars"], {"vram": "critical"})     # None bars dropped
        self.assertEqual(s["overall"], "in distress")


class TestRenderView(unittest.TestCase):
    def test_view_reads_the_single_source_of_truth(self):
        bus = NervousBus()
        self.addCleanup(bus.close)
        view = FeltStateView(bus)
        self.addCleanup(view.close)
        Interoception(bus, reader=reader(vram_used_pct=99)).emit()
        cur = view.current()
        self.assertIsNotNone(cur)
        self.assertEqual(cur["overall"], "in distress")        # read from the projection, not recomputed
        self.assertEqual(cur["bars"]["vram"], "critical")

    def test_truth_rendering_view_and_context_agree(self):
        # the render and the deliberative core read the SAME retained projection -> they cannot
        # disagree about how the body feels (I6; the 'renders falsehoods' bug class cannot recur).
        bus = NervousBus()
        self.addCleanup(bus.close)
        view = FeltStateView(bus)
        self.addCleanup(view.close)
        aff = AfferentContext(bus, max_chars=2000)
        self.addCleanup(aff.close)
        Interoception(bus, reader=reader(vram_used_pct=95)).emit()   # vram high -> strained
        block, _ = aff.drain_block()
        cur = view.current()
        self.assertEqual(cur["overall"], "strained")
        self.assertIn(cur["overall"], block)                   # context shows the SAME feeling

    def test_late_view_gets_current_felt(self):
        bus = NervousBus()
        self.addCleanup(bus.close)
        Interoception(bus, reader=reader(vram_used_pct=99)).emit()   # published BEFORE the view exists
        view = FeltStateView(bus)                                    # late subscriber (retained)
        self.addCleanup(view.close)
        self.assertEqual(view.current()["overall"], "in distress")


if __name__ == "__main__":
    unittest.main()
