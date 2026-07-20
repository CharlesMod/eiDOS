"""Tests for dashboard.build_world — the shared-map endpoint builder (WORLD_PLAN W3).

The world module (world.py) is built in parallel and is NOT imported at module load — the
dashboard lazy-imports it inside build_world, exception-guarded. These tests exercise the
builder directly (the /api/world route is a one-line `json.dumps(build_world(config))`
passthrough, like /api/growth and /api/why), stubbing a `world` module in sys.modules via
unittest.mock.patch.dict so the tests are hermetic and never depend on world.py existing.

Contract (WORLD_PLAN §2 to_json shape + the endpoint contract from the task):
  - flag ON  + module present + build succeeds → to_json(build_world(config)) merged with
    {"enabled": True}: the full place graph (places dict, here, weather, generated_tick).
  - flag OFF                                   → {"enabled": False} (byte-simple dark panel).
  - flag ON  but module MISSING (ImportError)  → {"enabled": False} (honest, 200-render-friendly).
  - flag ON  but build RAISES                  → {"enabled": False}.

Convention borrowed from tests/test_growth.py / test_dashboard_data.py: a temp Config, no
real world state, assert the builder's fail-open contract (never crashes; dark on absence).
"""

import sys
import types
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent.parent))

from config import Config
import dashboard


def _cfg(world_enabled):
    c = Config()
    c.world_enabled = world_enabled
    return c


# A fixture conforming to WORLD_PLAN §2 to_json(world): places dict of Place dicts
# (id/name/kind/referent/objects/exits/notices), here, weather, generated_tick.
FIXTURE_JSON = {
    "places": {
        "the_commons": {
            "id": "the_commons",
            "name": "The Commons",
            "kind": "hub",
            "referent": {"kind": "system", "key": "workspace"},
            "objects": [
                {"id": "standing", "name": "the standing stone",
                 "referent": {"kind": "system", "key": "level"},
                 "state": "level 2", "detail": "portfolio 3/10", "affordances": []},
            ],
            "exits": [
                {"to": "workshop", "open": True, "locked_reason": ""},
                {"to": "watchtower", "open": False,
                 "locked_reason": "foresight unlocks at level 4"},
            ],
            "notices": ["a courier arrived"],
        },
        "workshop": {
            "id": "workshop",
            "name": "The Workshop",
            "kind": "district",
            "referent": {"kind": "unit", "key": "skillcraft"},
            "objects": [
                {"id": "bench", "name": "the bench",
                 "referent": {"kind": "skill", "key": "grep_logs"},
                 "state": "trusted 12/12", "detail": "12 uses", "affordances": ["run_skill"]},
            ],
            "exits": [{"to": "the_commons", "open": True, "locked_reason": ""}],
            "notices": [],
        },
    },
    "here": "the_commons",
    "weather": "the mill runs warm; reserve half-full",
    "generated_tick": 4242,
}


def _stub_world_module(*, to_json_result=None, build_raises=None):
    """A fake `world` module for sys.modules with the §2 public API surface used here."""
    mod = types.ModuleType("world")

    def build_world(config, *, persona=None, tick=0):
        if build_raises is not None:
            raise build_raises
        return object()  # opaque World; to_json is what the builder actually serializes

    def to_json(world):
        return to_json_result if to_json_result is not None else FIXTURE_JSON

    mod.build_world = build_world
    mod.to_json = to_json
    return mod


class TestFlagOff(unittest.TestCase):
    """Flag off is byte-simple and never touches the world module (W7 flag-dark spirit)."""

    def test_flag_off_returns_disabled(self):
        # Even with a healthy stub present, the flag gate short-circuits first.
        with mock.patch.dict(sys.modules, {"world": _stub_world_module()}):
            out = dashboard.build_world(_cfg(False))
        self.assertEqual(out, {"enabled": False})

    def test_flag_missing_attr_defaults_off(self):
        c = Config()
        if hasattr(c, "world_enabled"):
            delattr(c, "world_enabled")
        out = dashboard.build_world(c)
        self.assertEqual(out, {"enabled": False})


class TestFlagOnHealthy(unittest.TestCase):
    """Flag on + module present + build succeeds → the full graph, enabled True."""

    def test_returns_graph_with_enabled_true(self):
        with mock.patch.dict(sys.modules, {"world": _stub_world_module()}):
            out = dashboard.build_world(_cfg(True))
        self.assertTrue(out["enabled"])
        # §2 shape passed through intact.
        self.assertEqual(out["here"], "the_commons")
        self.assertEqual(out["weather"], "the mill runs warm; reserve half-full")
        self.assertEqual(out["generated_tick"], 4242)
        self.assertIn("the_commons", out["places"])
        self.assertIn("workshop", out["places"])

    def test_place_contents_pass_through(self):
        with mock.patch.dict(sys.modules, {"world": _stub_world_module()}):
            out = dashboard.build_world(_cfg(True))
        commons = out["places"]["the_commons"]
        self.assertEqual(commons["kind"], "hub")
        # A real object with real state, and a locked exit carrying its real reason (W6).
        self.assertEqual(commons["objects"][0]["state"], "level 2")
        locked = [e for e in commons["exits"] if not e["open"]]
        self.assertEqual(len(locked), 1)
        self.assertEqual(locked[0]["locked_reason"], "foresight unlocks at level 4")

    def test_enabled_key_does_not_clobber_graph(self):
        # The builder merges enabled=True without dropping any §2 top-level key.
        with mock.patch.dict(sys.modules, {"world": _stub_world_module()}):
            out = dashboard.build_world(_cfg(True))
        for k in ("places", "here", "weather", "generated_tick", "enabled"):
            self.assertIn(k, out)


class TestFlagOnModuleMissing(unittest.TestCase):
    """Flag on but the parallel-built module is absent → honest dark, still 200-render-friendly."""

    def test_import_error_returns_disabled(self):
        # Force `import world` to raise ImportError even if a real world.py ever lands.
        real_import = __import__

        def fake_import(name, *a, **k):
            if name == "world":
                raise ImportError("no module named world (parallel build)")
            return real_import(name, *a, **k)

        # Ensure no cached stub satisfies the import.
        with mock.patch.dict(sys.modules, {}, clear=False):
            sys.modules.pop("world", None)
            with mock.patch("builtins.__import__", side_effect=fake_import):
                out = dashboard.build_world(_cfg(True))
        self.assertEqual(out, {"enabled": False})


class TestFlagOnBuildRaises(unittest.TestCase):
    """Flag on, module present, but build_world/to_json blow up → dark, never a 500."""

    def test_build_world_raises_returns_disabled(self):
        stub = _stub_world_module(build_raises=RuntimeError("derivation exploded"))
        with mock.patch.dict(sys.modules, {"world": stub}):
            out = dashboard.build_world(_cfg(True))
        self.assertEqual(out, {"enabled": False})

    def test_to_json_returns_non_dict_returns_disabled(self):
        # A malformed renderer (not a dict) must not corrupt the response.
        stub = _stub_world_module(to_json_result=["not", "a", "dict"])
        with mock.patch.dict(sys.modules, {"world": stub}):
            out = dashboard.build_world(_cfg(True))
        self.assertEqual(out, {"enabled": False})


if __name__ == "__main__":
    unittest.main()
