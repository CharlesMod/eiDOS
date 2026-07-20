"""WORLD_PLAN Phase W1 — creature-facing wiring tests (context block + `go` tool + registration).

These tests are the CONTRACT TEST for the sibling's `world.py`: `world` does NOT exist in this
worktree, so we inject a faithful STUB module (unittest.mock.patch.dict on sys.modules) that
implements the §2 public API exactly — Referent/WorldObject/Exit/Place/World dataclasses,
build_world / current_place / move_to / render_here / to_json / world_enabled. If the real
world.py honours §2, this wiring works against it unchanged; the orchestrator reconciles any
drift.

Pinned here: the "## Where you are" block renders under the flag and carries render_here's output;
it is absent flag-off and absent when the world import raises (fail-open, W7); the `go` tool is
registered only under the flag; its honest-refusal shapes (args/blocked fail_kinds + message
content, ARCH #4); its success path calls move_to and returns its line; and visible_tools
invisibility conventions still hold.
"""

import os
import sys
import tempfile
import types
import unittest
from dataclasses import dataclass, field
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent.parent))

from config import Config


# ---------------------------------------------------------------------------
# A faithful §2-contract stub of world.py (this IS the contract test).
# ---------------------------------------------------------------------------

def _make_world_stub(*, places=None, here="the_commons", render_text="",
                     move_result=None, build_raises=False):
    """Build a stub `world` module implementing the WORLD_PLAN §2 public API.

    `render_text` is what render_here returns; `move_result` is the (bool, str) move_to returns.
    `build_raises` makes build_world raise (to exercise the fail-open path)."""
    mod = types.ModuleType("world")

    @dataclass
    class Referent:
        kind: str
        key: str

    @dataclass
    class WorldObject:
        id: str
        name: str
        referent: "Referent"
        state: str
        detail: str = ""
        affordances: list = field(default_factory=list)

    @dataclass
    class Exit:
        to: str
        open: bool
        locked_reason: str = ""

    @dataclass
    class Place:
        id: str
        name: str
        kind: str
        referent: "Referent"
        objects: list
        exits: list
        notices: list = field(default_factory=list)

    @dataclass
    class World:
        places: dict
        here: str
        weather: str
        generated_tick: int

    _places = places if places is not None else {
        "the_commons": Place("the_commons", "The Commons", "hub",
                             Referent("system", "workspace"), [], []),
        "workshop": Place("workshop", "The Workshop", "district",
                          Referent("unit", "skillcraft"), [], []),
    }

    def build_world(config, *, persona=None, tick=0):
        if build_raises:
            raise RuntimeError("world build blew up")
        return World(places=dict(_places), here=here, weather="mild; reserve half-full",
                     generated_tick=tick)

    def current_place(config):
        return here

    def move_to(config, place_id):
        if move_result is not None:
            return move_result
        if place_id not in _places:
            return (False, f"There's no place called {place_id}.")
        return (True, f"You walk to {_places[place_id].name}.")

    def render_here(world, *, budget_chars=900):
        return render_text

    def to_json(world):
        return {"here": world.here, "weather": world.weather,
                "places": list(world.places)}

    def world_enabled(config):
        return bool(getattr(config, "world_enabled", False))

    mod.Referent = Referent
    mod.WorldObject = WorldObject
    mod.Exit = Exit
    mod.Place = Place
    mod.World = World
    mod.build_world = build_world
    mod.current_place = current_place
    mod.move_to = move_to
    mod.render_here = render_here
    mod.to_json = to_json
    mod.world_enabled = world_enabled
    return mod


def _cfg():
    c = Config()
    c.workspace_dir = tempfile.mkdtemp()
    Path(c.workspace_dir).mkdir(parents=True, exist_ok=True)
    return c


# ---------------------------------------------------------------------------
# Context block (§4) — "## Where you are"
# ---------------------------------------------------------------------------

class TestWorldContextBlock(unittest.TestCase):

    def test_block_renders_under_flag_and_carries_render_here(self):
        import context
        cfg = _cfg()
        cfg.world_enabled = True
        marker = "## Where you are\nThe Workshop — skills hum here."
        stub = _make_world_stub(render_text=marker)
        with patch.dict(sys.modules, {"world": stub}):
            block = context._world_block(cfg, tick_number=7)
        self.assertEqual(block, marker)   # the block IS render_here's output, verbatim (a pure view)

    def test_block_absent_when_flag_off(self):
        import context
        cfg = _cfg()   # no world_enabled attr → getattr default False (W7 flag-dark)
        stub = _make_world_stub(render_text="## Where you are\nsomewhere")
        with patch.dict(sys.modules, {"world": stub}):
            self.assertEqual(context._world_block(cfg, tick_number=1), "")
        # even asserting the attribute False explicitly:
        cfg.world_enabled = False
        with patch.dict(sys.modules, {"world": stub}):
            self.assertEqual(context._world_block(cfg, tick_number=1), "")

    def test_block_absent_when_world_import_raises(self):
        import context
        cfg = _cfg()
        cfg.world_enabled = True
        # No 'world' in sys.modules and no world.py on the path → import fails → fail-open (W7).
        # Force the import to raise even if some stray module exists, via a raising build too.
        with patch.dict(sys.modules, {"world": None}):   # None → ImportError on `import world`
            self.assertEqual(context._world_block(cfg, tick_number=1), "")

    def test_block_absent_when_build_raises(self):
        import context
        cfg = _cfg()
        cfg.world_enabled = True
        stub = _make_world_stub(render_text="## Where you are\nx", build_raises=True)
        with patch.dict(sys.modules, {"world": stub}):
            self.assertEqual(context._world_block(cfg, tick_number=1), "")

    def test_empty_render_yields_no_block(self):
        import context
        cfg = _cfg()
        cfg.world_enabled = True
        stub = _make_world_stub(render_text="   \n  ")   # whitespace-only → treated as no block
        with patch.dict(sys.modules, {"world": stub}):
            self.assertEqual(context._world_block(cfg, tick_number=1), "")

    def test_block_lands_in_durable_semi_tier_not_stable_head(self):
        """The rendered block appears in the durable blob (messages[1]), the SEMI tier — after the
        stable head, so it never destabilises the cached KV head."""
        import context
        from memory import append_observation
        cfg = _cfg()
        cfg.creature_mode = True
        cfg.world_enabled = True
        marker = "## Where you are\nThe Commons — home."
        stub = _make_world_stub(render_text=marker)
        with patch.dict(sys.modules, {"world": stub}):
            messages = context.assemble_context(cfg, tick_number=3, goal_start_time=0.0)
        durable = messages[1]["content"]
        self.assertIn("## Where you are", durable)
        # It is NOT in the system prompt (messages[0], the stable prefix's anchor).
        self.assertNotIn("## Where you are", messages[0]["content"])

    def test_assembly_flag_off_has_no_world_block(self):
        import context
        cfg = _cfg()
        cfg.creature_mode = True   # world flag absent → dark
        stub = _make_world_stub(render_text="## Where you are\nshould not show")
        with patch.dict(sys.modules, {"world": stub}):
            messages = context.assemble_context(cfg, tick_number=1, goal_start_time=0.0)
        joined = "\n".join(m["content"] for m in messages)
        self.assertNotIn("## Where you are", joined)


# ---------------------------------------------------------------------------
# The `go` tool (§5) — registration + honest refusals + success
# ---------------------------------------------------------------------------

class TestGoToolRegistration(unittest.TestCase):

    def tearDown(self):
        # Keep the global registry clean between tests (register_world_tool mutates TOOLS).
        import tools
        tools.TOOLS.pop("go", None)
        tools._TOOL_ARG_MODELS.pop("go", None)

    def test_go_registered_only_under_flag(self):
        import tools
        cfg = _cfg()
        # flag off (default) → not registered
        tools.register_world_tool(cfg)
        self.assertNotIn("go", tools.TOOLS)
        # flag on → registered, with its args model
        cfg.world_enabled = True
        self.assertTrue(tools.register_world_tool(cfg))
        self.assertIn("go", tools.TOOLS)
        self.assertIs(tools._TOOL_ARG_MODELS.get("go"), tools.GoArgs)
        # idempotent, and flipping the flag off unregisters it again
        cfg.world_enabled = False
        tools.register_world_tool(cfg)
        self.assertNotIn("go", tools.TOOLS)

    def test_go_in_flag_registered_builtins(self):
        import tools
        # `go` is a flag-registered PLATFORM builtin (not a self-authored skill), so the
        # visible_tools machinery treats it as lockable, never as an always-visible making.
        self.assertIn("go", tools._FLAG_REGISTERED_BUILTINS)
        self.assertIn("go", tools._EVER_BUILTIN_NAMES)


class TestGoToolBehaviour(unittest.TestCase):

    def test_go_dark_refuses_without_flag(self):
        import tools
        cfg = _cfg()   # world_enabled absent → dark
        r = tools.tool_go({"place": "workshop"}, cfg)
        self.assertFalse(r.success)
        self.assertEqual(r.fail_kind, "blocked")

    def test_go_success_calls_move_to_and_returns_its_line(self):
        import tools
        cfg = _cfg()
        cfg.world_enabled = True
        line = "You walk into The Workshop; the forge glows."
        stub = _make_world_stub(move_result=(True, line))
        with patch.dict(sys.modules, {"world": stub}):
            r = tools.tool_go({"place": "workshop"}, cfg)
        self.assertTrue(r.success)
        self.assertEqual(r.output, line)   # the arrival line is move_to's message, verbatim
        self.assertEqual(r.fail_kind, "")

    def test_go_unknown_place_is_args_failure_listing_real_places(self):
        import tools
        cfg = _cfg()
        cfg.world_enabled = True
        stub = _make_world_stub()   # default places: the_commons, workshop
        with patch.dict(sys.modules, {"world": stub}):
            r = tools.tool_go({"place": "atlantis"}, cfg)
        self.assertFalse(r.success)
        self.assertEqual(r.fail_kind, "args")          # ARCH #4: bad arg, learnable
        self.assertIn("atlantis", r.output)
        self.assertIn("the_commons", r.output)          # the REAL place ids are listed
        self.assertIn("workshop", r.output)

    def test_go_locked_place_is_blocked_naming_the_condition(self):
        import tools
        cfg = _cfg()
        cfg.world_enabled = True
        # workshop is a KNOWN place, but move_to refuses it and names the unlock condition (W6).
        reason = "The workshop door is locked — it opens when you reach the skillcraft unit."
        stub = _make_world_stub(move_result=(False, reason))
        with patch.dict(sys.modules, {"world": stub}):
            r = tools.tool_go({"place": "workshop"}, cfg)
        self.assertFalse(r.success)
        self.assertEqual(r.fail_kind, "blocked")        # ARCH #4/W6: the wall names its condition
        self.assertEqual(r.output, reason)

    def test_go_empty_place_is_args_failure(self):
        import tools
        cfg = _cfg()
        cfg.world_enabled = True
        stub = _make_world_stub()
        with patch.dict(sys.modules, {"world": stub}):
            r = tools.tool_go({"place": "   "}, cfg)
        self.assertFalse(r.success)
        self.assertEqual(r.fail_kind, "args")

    def test_go_build_failure_fails_closed_blocked(self):
        import tools
        cfg = _cfg()
        cfg.world_enabled = True
        stub = _make_world_stub(build_raises=True)
        with patch.dict(sys.modules, {"world": stub}):
            r = tools.tool_go({"place": "workshop"}, cfg)
        self.assertFalse(r.success)
        self.assertEqual(r.fail_kind, "blocked")        # never a crash

    def test_go_args_model_forbids_extra_and_requires_place(self):
        import tools
        # extra=forbid (the strict boundary), and `place` required + non-empty.
        with self.assertRaises(Exception):
            tools.GoArgs(place="workshop", junk=1)
        with self.assertRaises(Exception):
            tools.GoArgs(place="  ")
        m = tools.GoArgs(place="workshop")
        self.assertEqual(m.place, "workshop")
        # alias: `to` / `destination` populate `place`
        self.assertEqual(tools.GoArgs(to="library").place, "library")


class TestVisibleToolsConventions(unittest.TestCase):
    """When the ladder is OFF (house/task mode), visible_tools returns TOOLS itself — so once `go`
    is registered under the world flag it is visible there. When the ladder is ACTIVE, an ungranted
    builtin is invisible; `go` being in _EVER_BUILTIN_NAMES means it obeys that convention (it is
    not a self-authored making that leaks in). We assert the machinery, not a specific grant."""

    def tearDown(self):
        import tools
        tools.TOOLS.pop("go", None)
        tools._TOOL_ARG_MODELS.pop("go", None)

    def test_go_visible_when_ladder_off(self):
        import tools
        cfg = _cfg()
        cfg.world_enabled = True
        tools.register_world_tool(cfg)
        # ladder off (creature_mode/pillars_tool_unlocks_enabled default False) → TOOLS itself
        self.assertIs(tools.visible_tools(cfg), tools.TOOLS)
        self.assertIn("go", tools.visible_tools(cfg))

    def test_go_treated_as_lockable_builtin_under_ladder(self):
        import tools
        cfg = _cfg()
        cfg.world_enabled = True
        cfg.creature_mode = True
        cfg.pillars_tool_unlocks_enabled = True
        tools.register_world_tool(cfg)
        # Under the ladder, visible_tools filters to granted units ∪ non-builtin skills. `go` is a
        # builtin (in _EVER_BUILTIN_NAMES), so it is visible ONLY if granted — never leaks in as a
        # self-authored making. We assert it obeys the builtin gate (present ⟺ in granted set).
        vis = tools.visible_tools(cfg)
        try:
            from unlocks import granted_tools
            granted = set(granted_tools(cfg))
        except Exception:  # noqa: BLE001
            granted = set()
        self.assertEqual("go" in vis, "go" in granted)


if __name__ == "__main__":
    unittest.main()
