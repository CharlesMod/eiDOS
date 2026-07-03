"""Pillars 3.3 — skill composition (from library to language). All behind
`config.pillars_skill_composition_enabled` (default False); these tests flip it ON. Offline only:
skills are authored into a temp workspace and driven through the live TOOLS registry / manifest
exactly as the tick loop does. No services, no GPU, no network.

The gate (PILLARS_TODO.md 3.3 / plan S-3, S-4):
  (a) a composed skill runs WITHIN budget — it calls a sub-skill, completes, and the shared budget
      accounting is correct;
  (b) a CYCLIC composition (A→B→A) is REJECTED at authoring — it never reaches the runtime;
  (c) ONE promotion flows end-to-end: a trusted + reused composition → candidate queue →
      apply_promotion → it exists in the atom vocabulary;
  plus: a composition exceeding depth cap 2 is rejected; budget exhaustion aborts a runaway
  composition (killably, never a hang).
"""

import json
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import skills
import skill_atoms
from skill_atoms import (
    _Budget, COMPOSITION_MAX_DEPTH, COMPOSITION_BUDGET_UNITS, COMPOSITION_CALL_COST,
    CompositionBudgetError, CompositionDepthError, check_composition_cycle,
    static_calls_in_source, build_atoms,
)
from config import Config
from tools import TOOLS, execute_tool
from parser import ToolCall


def _cfg(composition=True):
    c = Config()
    c.workspace_dir = tempfile.mkdtemp()
    c.pillars_skill_composition_enabled = composition
    return c


# A trivial always-succeeds leaf skill (no composition), parameterised by name.
def _leaf(name: str) -> str:
    return (f"def tool_{name}(args, config):\n"
            f"    return ToolResult(output=\"leaf:{name}\", full_output_path=None, "
            f"success=True, duration_s=0.0)\n")


# A composed skill that calls one sub-skill and reports what it got back.
def _composed_calling(name: str, sub: str) -> str:
    return (f"def tool_{name}(args, config):\n"
            f"    got = call('{sub}')\n"
            f"    return ToolResult(output=\"parent-saw:\" + str(got), full_output_path=None, "
            f"success=True, duration_s=0.0)\n")


def _make_leaf(c, name):
    r = skills.create_skill(c, name, _leaf(name), description=f"leaf {name}")
    assert r["success"], (name, r)
    return r


def _mark_trusted(c, name, invocations=10, successes=10):
    """Force a skill to trusted with a reuse record (the manifest is the only source of truth)."""
    m = json.loads(skills._manifest_path(c).read_text())
    ent = m["skills"][name]
    ent["status"] = "trusted"
    ent["invocations"] = invocations
    ent["successes"] = successes
    skills._manifest_path(c).write_text(json.dumps(m))


# ---------------------------------------------------------------------------
# The shared-budget object (unit-level: accounting is correct + exhaustion raises)
# ---------------------------------------------------------------------------

class TestBudget(unittest.TestCase):
    def test_spend_deducts_and_counts(self):
        b = _Budget(units=3.0)
        b.spend(COMPOSITION_CALL_COST, "a")
        b.spend(COMPOSITION_CALL_COST, "b")
        self.assertEqual(b.spent_calls, 2)
        self.assertAlmostEqual(b.remaining, 3.0 - 2 * COMPOSITION_CALL_COST)

    def test_exhaustion_raises_budget_error(self):
        b = _Budget(units=COMPOSITION_CALL_COST)  # room for exactly one call
        b.spend(COMPOSITION_CALL_COST, "a")
        with self.assertRaises(CompositionBudgetError):
            b.spend(COMPOSITION_CALL_COST, "b")


# ---------------------------------------------------------------------------
# (a) A composed skill runs within budget, and the shared budget accounts correctly.
# ---------------------------------------------------------------------------

class TestComposedWithinBudget(unittest.TestCase):
    def setUp(self):
        self.c = _cfg(composition=True)

    def tearDown(self):
        for n in ("leaf_a", "parent_p"):
            TOOLS.pop(n, None)

    def test_composed_skill_calls_trusted_sub_and_completes(self):
        _make_leaf(self.c, "leaf_a")
        _mark_trusted(self.c, "leaf_a")  # a composition may only call a TRUSTED skill
        r = skills.create_skill(self.c, "parent_p", _composed_calling("parent_p", "leaf_a"),
                                description="calls leaf_a")
        self.assertTrue(r["success"], r.get("errors"))
        res = execute_tool(ToolCall(tool="parent_p", args={}, raw=""), self.c)
        self.assertTrue(res.success, res.output)
        self.assertIn("leaf:leaf_a", res.output)  # the sub-skill's output threaded back up

    def test_call_atom_spends_shared_budget_once_per_hop(self):
        # Drive `call` directly through a built atom set and confirm one hop == one spend.
        _make_leaf(self.c, "leaf_a")
        _mark_trusted(self.c, "leaf_a")
        budget = _Budget()
        call = skill_atoms._make_call(self.c, build_atoms(self.c), budget=budget, depth=0)
        before = budget.remaining
        out = call("leaf_a")
        self.assertIn("leaf:leaf_a", str(out))
        self.assertEqual(budget.spent_calls, 1)
        self.assertAlmostEqual(budget.remaining, before - COMPOSITION_CALL_COST)

    def test_call_to_untrusted_skill_fails_soft(self):
        # A composition may only call trusted skills; calling an active-but-untrusted one fails soft.
        _make_leaf(self.c, "leaf_a")  # left 'active', not trusted
        budget = _Budget()
        call = skill_atoms._make_call(self.c, build_atoms(self.c), budget=budget, depth=0)
        out = call("leaf_a")
        self.assertIsInstance(out, dict)
        self.assertFalse(out.get("ok"))
        self.assertIn("trusted", out.get("error", ""))


# ---------------------------------------------------------------------------
# (b) A cyclic composition is rejected at AUTHORING — never reaches runtime.
# ---------------------------------------------------------------------------

class TestCycleRejectedAtAuthoring(unittest.TestCase):
    def setUp(self):
        self.c = _cfg(composition=True)

    def tearDown(self):
        for n in ("cyc_a", "cyc_b", "selfcall"):
            TOOLS.pop(n, None)

    def test_self_call_cycle_rejected(self):
        # A skill that calls itself is a 1-cycle — refused at create.
        src = ("def tool_selfcall(args, config):\n"
               "    return ToolResult(output=str(call('selfcall')), full_output_path=None, "
               "success=True, duration_s=0.0)\n")
        r = skills.create_skill(self.c, "selfcall", src, description="calls itself")
        self.assertFalse(r["success"])
        self.assertIn("cycle", " ".join(r["errors"]).lower())
        self.assertNotIn("selfcall", TOOLS)  # never activated

    def test_ab_ba_cycle_rejected_at_authoring(self):
        # Author A (trusted) that calls B; then authoring B that calls A closes an A→B→A cycle.
        _make_leaf(self.c, "cyc_b")            # first a plain leaf so A can be authored calling it
        _mark_trusted(self.c, "cyc_b")
        ra = skills.create_skill(self.c, "cyc_a", _composed_calling("cyc_a", "cyc_b"),
                                 description="A calls B")
        self.assertTrue(ra["success"], ra.get("errors"))
        _mark_trusted(self.c, "cyc_a")
        # Now edit B so it calls A — this would create A→B→A. The static graph must catch it at authoring.
        b_calls_a = _composed_calling("cyc_b", "cyc_a")
        rb = skills.edit_skill(self.c, "cyc_b", b_calls_a)
        self.assertFalse(rb["success"])
        self.assertIn("cycle", " ".join(rb["errors"]).lower())

    def test_check_composition_cycle_is_static(self):
        # The check runs on source text alone (no execution) — directly exercisable.
        _make_leaf(self.c, "cyc_a")
        _mark_trusted(self.c, "cyc_a")
        errs = check_composition_cycle(self.c, "cyc_a", "def tool_cyc_a(args, config):\n    call('cyc_a')\n")
        self.assertTrue(errs)  # self-cycle
        clean = check_composition_cycle(self.c, "brand_new", _leaf("brand_new"))
        self.assertEqual(clean, [])  # no calls == acyclic


# ---------------------------------------------------------------------------
# Depth cap 2: a composition nesting deeper than COMPOSITION_MAX_DEPTH is rejected at runtime.
# ---------------------------------------------------------------------------

class TestDepthCap(unittest.TestCase):
    def setUp(self):
        self.c = _cfg(composition=True)

    def test_depth_cap_constant_is_two(self):
        self.assertEqual(COMPOSITION_MAX_DEPTH, 2)

    def test_call_beyond_depth_cap_raises(self):
        # A call made from a namespace already AT the cap would create a child one deeper — refuse.
        _make_leaf(self.c, "leaf_a")
        _mark_trusted(self.c, "leaf_a")
        deep = skill_atoms._make_call(self.c, build_atoms(self.c), budget=_Budget(),
                                      depth=COMPOSITION_MAX_DEPTH)
        with self.assertRaises(CompositionDepthError):
            deep("leaf_a")

    def test_chain_deeper_than_cap_aborts(self):
        # The top skill runs at depth 0, so cap 2 permits exactly two nested hops (a→b→c). A FOURTH
        # level (a→b→c→d) is the 3rd hop — refused. The abort propagates up and surfaces in lvl_a's
        # output; the whole thing is bounded (never a hang).
        _make_leaf(self.c, "lvl_d")
        _mark_trusted(self.c, "lvl_d")
        skills.create_skill(self.c, "lvl_c", _composed_calling("lvl_c", "lvl_d"), description="c->d")
        _mark_trusted(self.c, "lvl_c")
        skills.create_skill(self.c, "lvl_b", _composed_calling("lvl_b", "lvl_c"), description="b->c")
        _mark_trusted(self.c, "lvl_b")
        skills.create_skill(self.c, "lvl_a", _composed_calling("lvl_a", "lvl_b"), description="a->b")
        _mark_trusted(self.c, "lvl_a")
        try:
            t = time.monotonic()
            res = execute_tool(ToolCall(tool="lvl_a", args={}, raw=""), self.c)
            self.assertLess(time.monotonic() - t, 10.0)  # bounded, never a hang
            # lvl_c's call('lvl_d') is the 3rd hop (depth 2 → 3 > cap) — CompositionDepthError propagates
            # out of lvl_c, so lvl_b's call('lvl_c') returns the depth-abort error dict, which lvl_a sees.
            self.assertIn("depth", res.output.lower())
        finally:
            for n in ("lvl_a", "lvl_b", "lvl_c", "lvl_d"):
                TOOLS.pop(n, None)

    def test_two_hop_chain_within_cap_completes(self):
        # a→b→c is exactly two hops (the cap) — it runs to completion, proving the cap is inclusive.
        _make_leaf(self.c, "ok_c")
        _mark_trusted(self.c, "ok_c")
        skills.create_skill(self.c, "ok_b", _composed_calling("ok_b", "ok_c"), description="b->c")
        _mark_trusted(self.c, "ok_b")
        skills.create_skill(self.c, "ok_a", _composed_calling("ok_a", "ok_b"), description="a->b")
        _mark_trusted(self.c, "ok_a")
        try:
            res = execute_tool(ToolCall(tool="ok_a", args={}, raw=""), self.c)
            self.assertTrue(res.success, res.output)
            self.assertIn("leaf:ok_c", res.output)  # the leaf's output threaded all the way up
        finally:
            for n in ("ok_a", "ok_b", "ok_c"):
                TOOLS.pop(n, None)


# ---------------------------------------------------------------------------
# Budget exhaustion aborts a runaway composition (killably — bounded, no hang).
# ---------------------------------------------------------------------------

class TestBudgetExhaustionAborts(unittest.TestCase):
    def setUp(self):
        self.c = _cfg(composition=True)

    def test_runaway_calls_abort_when_budget_spent(self):
        # A composition that fans out MANY calls to a sub-skill in a loop must abort once the shared
        # budget is spent — bounded, not unbounded.
        _make_leaf(self.c, "leaf_a")
        _mark_trusted(self.c, "leaf_a")
        # Loop calling leaf_a far more than COMPOSITION_BUDGET_UNITS / COST times.
        n_calls = int(COMPOSITION_BUDGET_UNITS / COMPOSITION_CALL_COST) + 50
        src = ("def tool_runaway(args, config):\n"
               f"    for _ in range({n_calls}):\n"
               "        call('leaf_a')\n"
               "    return ToolResult(output='never', full_output_path=None, success=True, duration_s=0.0)\n")
        r = skills.create_skill(self.c, "runaway", src, description="fans out")
        self.assertTrue(r["success"], r.get("errors"))
        try:
            t = time.monotonic()
            res = execute_tool(ToolCall(tool="runaway", args={}, raw=""), self.c)
            self.assertLess(time.monotonic() - t, 20.0)  # aborted, never runs unbounded
            self.assertFalse(res.success)
            self.assertIn("budget", res.output.lower())
        finally:
            TOOLS.pop("runaway", None)

    def test_budget_direct_exhaustion_via_call(self):
        # Same, exercised directly on `call` with a tiny shared budget.
        _make_leaf(self.c, "leaf_a")
        _mark_trusted(self.c, "leaf_a")
        budget = _Budget(units=2 * COMPOSITION_CALL_COST)  # room for exactly two calls
        call = skill_atoms._make_call(self.c, build_atoms(self.c), budget=budget, depth=0)
        call("leaf_a")
        call("leaf_a")
        with self.assertRaises(CompositionBudgetError):
            call("leaf_a")


# ---------------------------------------------------------------------------
# (c) One promotion flows end-to-end: trusted+reused composition → queue → apply → atom vocabulary.
# ---------------------------------------------------------------------------

class TestPromotionEndToEnd(unittest.TestCase):
    def setUp(self):
        self.c = _cfg(composition=True)

    def tearDown(self):
        for n in ("leaf_a", "combo"):
            TOOLS.pop(n, None)

    def test_promotion_pipeline(self):
        # Build a composition, make it trusted + reused, propose, apply, assert it's in the vocabulary.
        _make_leaf(self.c, "leaf_a")
        _mark_trusted(self.c, "leaf_a")
        r = skills.create_skill(self.c, "combo", _composed_calling("combo", "leaf_a"),
                                description="combo composition")
        self.assertTrue(r["success"], r.get("errors"))
        _mark_trusted(self.c, "combo", invocations=8, successes=6)  # trusted + reused (≥ threshold)

        # Propose → a pending candidate in the queue.
        pr = skills.propose_promotion(self.c, "combo", rationale="proven, reused")
        self.assertTrue(pr["success"], pr.get("errors"))
        pending = skills.list_promotion_candidates(self.c, status="pending")
        self.assertEqual([c["skill"] for c in pending], ["combo"])

        # Apply (the dashboard seam) → compiled into the atom vocabulary.
        ap = skills.apply_promotion(self.c, "combo")
        self.assertTrue(ap["success"], ap.get("errors"))
        self.assertTrue(ap["in_vocabulary"])

        # It exists in the atom vocabulary: named in the promoted set AND injected by build_atoms.
        self.assertIn("combo", skills.promoted_atom_names(self.c))
        atoms = build_atoms(self.c)
        self.assertIn("combo", atoms)
        self.assertTrue(callable(atoms["combo"]))
        # And the promoted atom actually runs (executes the composition it congealed).
        out = atoms["combo"]()
        self.assertIn("leaf:leaf_a", str(out))

        # The candidate is now 'applied', no longer pending.
        self.assertEqual(skills.list_promotion_candidates(self.c, status="pending"), [])

    def test_promotion_rejects_non_composition_and_untrusted(self):
        _make_leaf(self.c, "leaf_a")  # active, not a composition
        # Not trusted yet:
        r1 = skills.propose_promotion(self.c, "leaf_a")
        self.assertFalse(r1["success"])
        # Trusted but not a composition (calls nothing):
        _mark_trusted(self.c, "leaf_a")
        r2 = skills.propose_promotion(self.c, "leaf_a")
        self.assertFalse(r2["success"])
        self.assertIn("not a composition", " ".join(r2["errors"]))


# ---------------------------------------------------------------------------
# Flag OFF: composition is dark — `call` absent, no promotion, existing skills unchanged.
# ---------------------------------------------------------------------------

class TestFlagOffUnchanged(unittest.TestCase):
    def setUp(self):
        self.c = _cfg(composition=False)

    def tearDown(self):
        for n in ("plain", "wants_call"):
            TOOLS.pop(n, None)

    def test_call_absent_from_atoms_when_flag_off(self):
        self.assertNotIn("call", build_atoms(self.c))

    def test_authoring_a_call_is_refused_when_flag_off(self):
        src = ("def tool_wants_call(args, config):\n"
               "    return ToolResult(output=str(call('x')), full_output_path=None, "
               "success=True, duration_s=0.0)\n")
        r = skills.create_skill(self.c, "wants_call", src)
        self.assertFalse(r["success"])
        self.assertIn("composition is disabled", " ".join(r["errors"]))

    def test_plain_skill_still_works_when_flag_off(self):
        r = skills.create_skill(self.c, "plain", _leaf("plain"))
        self.assertTrue(r["success"], r.get("errors"))
        res = execute_tool(ToolCall(tool="plain", args={}, raw=""), self.c)
        self.assertTrue(res.success)
        self.assertIn("leaf:plain", res.output)

    def test_propose_promotion_noop_when_flag_off(self):
        r = skills.propose_promotion(self.c, "anything")
        self.assertFalse(r["success"])
        self.assertIn("disabled", " ".join(r["errors"]))


if __name__ == "__main__":
    unittest.main()
