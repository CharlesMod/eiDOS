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
        # a GENUINE stressor (thermal) escalates to distress...
        self.assertEqual(to_felt({"ram": "ok", "gpu_temp": "critical"})["overall"], "in distress")
        # ...but high VRAM is the resident mind by design — felt as calm posture, never distress.
        self.assertEqual(to_felt({"ram": "ok", "vram": "critical"})["overall"], "at ease")
        f = to_felt({"vram": "high", "cpu": "elevated"})
        self.assertEqual(f["overall"], "a little tense")      # worst STRESS bar = cpu (vram doesn't count)
        self.assertIn("working hard", f["felt"])              # the cpu qualia
        self.assertIn("mind resident on the GPU", f["felt"])  # vram felt as calm posture
        self.assertNotIn("GPU tight", f["felt"])              # never the old aversive framing

    def test_felt_state_carries_bars_and_qualia(self):
        s = felt_state({"vram": "critical", "disk": None})
        self.assertEqual(s["bars"], {"vram": "critical"})     # None bars dropped
        self.assertEqual(s["overall"], "at ease")             # resident mind (baseline) is not distress
        self.assertEqual(felt_state({"gpu_temp": "critical"})["overall"], "in distress")  # real stress escalates


class TestRenderView(unittest.TestCase):
    def test_view_reads_the_single_source_of_truth(self):
        bus = NervousBus()
        self.addCleanup(bus.close)
        view = FeltStateView(bus)
        self.addCleanup(view.close)
        Interoception(bus, reader=reader(vram_used_pct=99, gpu_temp_c=95)).emit()
        cur = view.current()
        self.assertIsNotNone(cur)
        self.assertEqual(cur["overall"], "in distress")        # from thermal stress (read from the projection)
        self.assertEqual(cur["bars"]["vram"], "critical")      # resident mind still reported as a bar

    def test_truth_rendering_view_and_context_agree(self):
        # the render and the deliberative core read the SAME retained projection -> they cannot
        # disagree about how the body feels (I6; the 'renders falsehoods' bug class cannot recur).
        bus = NervousBus()
        self.addCleanup(bus.close)
        view = FeltStateView(bus)
        self.addCleanup(view.close)
        aff = AfferentContext(bus, max_chars=2000)
        self.addCleanup(aff.close)
        Interoception(bus, reader=reader(cpu_pct=90)).emit()    # cpu high -> strained (a real stressor)
        block, _ = aff.drain_block()
        cur = view.current()
        self.assertEqual(cur["overall"], "strained")
        self.assertIn(cur["overall"], block)                   # context shows the SAME feeling

    def test_late_view_gets_current_felt(self):
        bus = NervousBus()
        self.addCleanup(bus.close)
        Interoception(bus, reader=reader(gpu_temp_c=95)).emit()      # published BEFORE the view exists
        view = FeltStateView(bus)                                    # late subscriber (retained)
        self.addCleanup(view.close)
        self.assertEqual(view.current()["overall"], "in distress")


if __name__ == "__main__":
    unittest.main()
