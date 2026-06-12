"""creature_gen.py — genome determinism, variety, and grid invariants.

These pin the procedural engine's contract: same seed → byte-identical
creature forever; wide morphological variety across seeds; every emitted grid
is rectangular, in-bounds, single-width-glyph clean, with eye/mouth/appendage
cells left blank for the client's layer compositor.
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import creature_gen as cg


def _expr():
    return {"condition": "STABLE", "delegating": False, "listening": False,
            "dead": False, "paused": False, "has_goal": True}


def _spec(seed, stage, progress=1.0):
    g = cg.genome_from_seed(seed)
    hatch = {"hatched": stage != "egg", "progress": progress}
    return cg.build_spec(g, stage, hatch, _expr())


class TestDeterminism(unittest.TestCase):

    def test_same_seed_same_genome(self):
        self.assertEqual(cg.genome_from_seed(1234), cg.genome_from_seed(1234))

    def test_same_seed_same_spec_grids(self):
        a = _spec(987654321, "adult")
        b = _spec(987654321, "adult")
        self.assertEqual(a["base"], b["base"])
        self.assertEqual(a["appendages"], b["appendages"])
        self.assertEqual(a["id"], b["id"])

    def test_different_seeds_differ(self):
        diffs = sum(1 for s in range(50)
                    if cg.genome_from_seed(s) != cg.genome_from_seed(s + 1000))
        self.assertGreater(diffs, 45)


class TestSchema(unittest.TestCase):

    def test_genes_in_enums_and_ranges(self):
        for seed in range(1000):
            g = cg.genome_from_seed(seed)
            self.assertEqual(g["v"], cg.GENOME_VERSION)
            self.assertIn(g["body"], cg.BODY_FAMILIES)
            self.assertIn(g["eyes"], cg.EYE_FAMILY_NAMES)
            self.assertIn(g["mouth"], cg.MOUTH_SET_NAMES)
            self.assertIn(g["ears"], cg.EAR_KIND_NAMES)
            self.assertIn(g["limbs"], cg.LIMB_KIND_NAMES)
            self.assertIn(g["tail"], cg.TAIL_KIND_NAMES)
            self.assertIn(g["size"], (-1, 0, 1))
            self.assertIn(g["accent"], cg.ACCENTS)
            self.assertIn(g["particle"], cg.PARTICLE_AFFINITIES)
            self.assertIn(g["egg_pattern"], cg.EGG_PATTERN_NAMES)
            self.assertTrue(2200 <= g["blink_ms"] <= 5200)
            self.assertTrue(3500 <= g["saccade_ms"] <= 8000)
            self.assertIn(g["sway_amp"], (0, 1, 2))
            self.assertTrue(2600 <= g["breath_ms"] <= 4200)
            self.assertTrue(8000 <= g["micro_ms"] <= 16000)


class TestVariety(unittest.TestCase):

    def test_all_families_and_accents_occur(self):
        seen_body, seen_eyes, seen_accent = set(), set(), set()
        for seed in range(400):
            g = cg.genome_from_seed(seed)
            seen_body.add(g["body"])
            seen_eyes.add(g["eyes"])
            seen_accent.add(g["accent"])
        self.assertEqual(seen_body, set(cg.BODY_FAMILIES))
        self.assertEqual(seen_eyes, set(cg.EYE_FAMILY_NAMES))
        self.assertEqual(seen_accent, set(cg.ACCENTS))

    def test_morphology_variety(self):
        # Identity = everything that shapes the rendered creature. Base grids
        # alone collapse to family×size (features are client layers), so
        # variety is measured on the full morphological tuple.
        morphs = set()
        for seed in range(400):
            s = _spec(seed, "adult")
            morphs.add((
                tuple(s["base"][0]), s["eyes"]["family"],
                s["mouth"]["glyphs"]["idle"],
                tuple(a["name"] + str(a["frames"]) for a in s["appendages"]),
            ))
        self.assertGreaterEqual(len(morphs), 150)


class TestGridInvariants(unittest.TestCase):

    def test_rectangular_in_bounds_blank_feature_cells(self):
        for seed in range(50):
            for stage in cg.STAGES:
                s = _spec(seed, stage, progress=0.5)
                w, h = s["w"], s["h"]
                self.assertEqual(len(s["base"]), 2, f"{seed}/{stage}")
                for frame in s["base"]:
                    self.assertEqual(len(frame), h)
                    for line in frame:
                        self.assertEqual(len(line), w, f"{seed}/{stage}: {line!r}")
                        for ch in line:
                            self.assertIn(ch, cg.APPROVED_GLYPHS,
                                          f"{seed}/{stage}: {ch!r}")
                if stage == "egg":
                    self.assertIsNone(s["eyes"])
                    self.assertIsNone(s["mouth"])
                    continue
                er, ec = s["eyes"]["l"]
                rr, rc = s["eyes"]["r"]
                feature_cells = {(er, ec), (er, ec + 1), (rr, rc), (rr, rc + 1)}
                m = s["mouth"]
                feature_cells |= {(m["row"], m["col"] + i) for i in range(m["len"])}
                ap_cells = set()
                for ap in s["appendages"]:
                    for (r, c) in ap["cells"]:
                        ap_cells.add((r, c))
                    for fr in ap["frames"]:
                        self.assertEqual(len(fr), len(ap["cells"]))
                        for ch in fr:
                            self.assertIn(ch, cg.APPROVED_GLYPHS)
                self.assertFalse(feature_cells & ap_cells,
                                 f"{seed}/{stage}: appendage overlaps feature")
                for (r, c) in feature_cells | ap_cells:
                    self.assertTrue(0 <= r < h and 0 <= c < w)
                    for frame in s["base"]:
                        self.assertEqual(frame[r][c], " ",
                                         f"{seed}/{stage}: cell {(r, c)} not blank")


class TestEgg(unittest.TestCase):

    def test_cracks_monotonic(self):
        g = cg.genome_from_seed(42)
        counts = []
        for p in (0.0, 0.4, 0.8, 1.0):
            frame = cg.compose_egg(g, p)["base"][0]
            counts.append(sum(line.count(ch) for line in frame for ch in "\\/X"))
        self.assertEqual(counts, sorted(counts))
        self.assertEqual(counts[0], min(counts))

    def test_egg_deterministic(self):
        g = cg.genome_from_seed(7)
        self.assertEqual(cg.compose_egg(g, 0.5), cg.compose_egg(g, 0.5))


class TestCocoon(unittest.TestCase):

    def test_invariants(self):
        for seed in range(30):
            g = cg.genome_from_seed(seed)
            for stage in ("juvenile", "adult", "guardian"):
                c = cg.compose_cocoon(g, stage)
                self.assertEqual(len(c["base"]), 2)
                self.assertIsNone(c["eyes"])
                self.assertIsNone(c["mouth"])
                self.assertEqual(c["appendages"], [])
                for frame in c["base"]:
                    self.assertEqual(len(frame), c["h"])
                    for line in frame:
                        self.assertEqual(len(line), c["w"])
                        for ch in line:
                            self.assertIn(ch, cg.APPROVED_GLYPHS)

    def test_deterministic_and_sized_to_stage(self):
        g = cg.genome_from_seed(5)
        self.assertEqual(cg.compose_cocoon(g, "adult"), cg.compose_cocoon(g, "adult"))
        spec_w = cg.compose(g, "adult")["w"]
        self.assertEqual(cg.compose_cocoon(g, "adult")["w"], spec_w)


class TestGuardianRestructure(unittest.TestCase):
    """Metamorphosis payoff: guardian appendages restructure, not just persist."""

    def _with(self, **genes):
        # find a seed with the wanted genes, then assert restructuring
        for seed in range(3000):
            g = cg.genome_from_seed(seed)
            if all(g[k] == v for k, v in genes.items()):
                return g
        self.fail(f"no seed with {genes} in 3000 — variety regression?")

    def test_guardian_ears_grow_taller(self):
        g = self._with(ears="cat")
        adult = {a["name"]: a for a in cg.compose(g, "adult")["appendages"]}
        guard = {a["name"]: a for a in cg.compose(g, "guardian")["appendages"]}
        self.assertEqual(len(adult["ear_l"]["cells"]), 1)
        self.assertEqual(len(guard["ear_l"]["cells"]), 2)
        for fr in guard["ear_l"]["frames"]:
            self.assertEqual(len(fr), 2)

    def test_guardian_tail_two_segments(self):
        g = self._with(tail="curl")
        adult = {a["name"]: a for a in cg.compose(g, "adult")["appendages"]}
        guard = {a["name"]: a for a in cg.compose(g, "guardian")["appendages"]}
        self.assertEqual(len(adult["tail"]["cells"]), 1)
        self.assertEqual(len(guard["tail"]["cells"]), 2)


class TestStageFor(unittest.TestCase):

    def test_table(self):
        self.assertEqual(cg.stage_for(1, False), "egg")
        self.assertEqual(cg.stage_for(2, False), "egg")
        self.assertEqual(cg.stage_for(1, True), "hatchling")
        self.assertEqual(cg.stage_for(3, True), "juvenile")
        self.assertEqual(cg.stage_for(4, True), "juvenile")
        self.assertEqual(cg.stage_for(5, True), "adult")
        self.assertEqual(cg.stage_for(7, True), "adult")
        self.assertEqual(cg.stage_for(8, True), "guardian")
        self.assertEqual(cg.stage_for(12, True), "guardian")


if __name__ == "__main__":
    unittest.main()
