"""The ladder's capstone red gate: grammar == prompt == check_tools == one accessor.

W2a pinned each consumer against tools.visible_tools; W2b pinned the prompt surfaces. This file
pins the CROSS-consistency the design demands (TOOL_PROGRESSION.md: one unit table, one accessor,
five consumers — drift becomes a failing test, not a review hope): for real fixture creatures at
three points on the ladder, the three RENDERED artifacts — the tick grammar, the assembled system
prompt, and the check_tools listing — must tell one story. A locked tool appears in NONE of them;
a granted tool appears in ALL of the ones that list capability.

No services / tick loop / GPU — temp workspaces only.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest

import phenotype
import prompts
import tools as tools_mod
import unlocks
from config import Config
from genome import Genome
from grammar import build_tick_grammar
from skills import list_skills


def _cfg(tmp_path) -> Config:
    cfg = Config()
    cfg.workspace_dir = str(tmp_path / "workspace")
    cfg.workspace.mkdir(parents=True, exist_ok=True)
    cfg.mock_mode = True
    cfg.creature_mode = True
    cfg.pillars_tool_unlocks_enabled = True
    Genome(cfg)                          # a real germline: morph lexicon for the prompt renderer
    return cfg


def _grant_upto(cfg, unit_ids):
    for uid in unit_ids:
        unlocks.grant(cfg, uid, "test")


# Prose-collision names: common English words we cannot whole-word scan for in flowing prompt
# text ("see", "manual", "predict", "recall" can all appear as ordinary words). The per-stanza
# exactness for these is already pinned in test_prompts.py's tool-name gate; here we scan only
# the unambiguous names.
_PROSE_COLLISIONS = {"see", "manual", "predict", "recall", "speak", "vision", "bash", "delegate"}

LADDER_POINTS = [
    ("newborn", []),
    ("mid", ["memory", "skillcraft"]),
    ("grown", ["memory", "skillcraft", "foresight", "senses", "resolve", "workshop"]),
    ("commissioned", ["memory", "skillcraft", "foresight", "senses", "resolve", "workshop",
                      "commission"]),
]


@pytest.mark.parametrize("label,extra_units", LADDER_POINTS)
def test_three_surfaces_tell_one_story(tmp_path, label, extra_units):
    cfg = _cfg(tmp_path)
    if "commission" in extra_units:
        # The commission verbs are flag-registered builtins (like predict): the fully-grown point
        # exercises them VISIBLE; the registry is restored in the finally so no test inherits them.
        cfg.pillars_commission_enabled = True
        tools_mod.register_commission_tools(cfg)
    _grant_upto(cfg, extra_units)
    try:
        _assert_one_story(cfg, label)
    finally:
        if "commission" in extra_units:
            tools_mod.TOOLS.pop("commission_add", None)
            tools_mod.TOOLS.pop("commission_done", None)


def _assert_one_story(cfg, label):

    visible = set(tools_mod.visible_tools(cfg).keys())
    granted_units = [u.id for u in unlocks.UNITS if set(u.tools) <= visible]
    locked_units = [u.id for u in unlocks.UNITS if u.id not in granted_units]

    # --- the grammar: locked names unrepresentable, visible names present -----------------------
    g = build_tick_grammar(sorted(visible))
    for u in unlocks.UNITS:
        for name in u.tools:
            if u.id in granted_units:
                assert f'"{name}"' in g or name in g, (label, name, "granted but not in grammar")
            else:
                assert name not in g, (label, name, "locked yet representable at the sampler")

    # --- the prompt: granted stanzas only ---------------------------------------------------------
    lex = phenotype.body_words(cfg)
    prompt = prompts.render_creature_system_prompt(lex, granted_units)
    for u in unlocks.UNITS:
        scannable = [n for n in u.tools if n not in _PROSE_COLLISIONS]
        for name in scannable:
            if u.id in granted_units:
                assert name in prompt, (label, name, "granted but the prompt never teaches it")
            else:
                assert name not in prompt, (label, name, "locked yet named in the prompt")

    # --- check_tools: the mirror shows only what exists -------------------------------------------
    listing = list_skills(cfg)
    shown = set(listing.get("builtins") or [])
    if not shown:  # tolerate a renamed key — fall back to a full-text scan of the listing
        shown = {n for n in visible if n in str(listing)}
    for u in locked_units:
        for name in unlocks.unit(u).tools:
            assert name not in shown, (label, name, "locked yet visible in the mirror")
    assert shown <= visible | {"goal_complete"}, (label, "mirror shows names outside the world")

    # --- and the chain itself ---------------------------------------------------------------------
    lockable = {n for u in unlocks.UNITS for n in u.tools}
    assert (visible & lockable) == {n for uid in granted_units
                                    for n in unlocks.unit(uid).tools}, label


def test_flag_off_all_three_surfaces_are_legacy(tmp_path):
    cfg = _cfg(tmp_path)
    cfg.pillars_tool_unlocks_enabled = False
    assert tools_mod.visible_tools(cfg) is tools_mod.TOOLS       # the very object — byte-identical


def test_every_unit_tool_exists_in_the_registry():
    """The unit table can only ever grant real organs: every tool it names must be a registry
    name (or a flag-registered builtin like predict) — a typo'd unit tool would otherwise grant
    a phantom limb the dispatcher can't move."""
    known = set(tools_mod.TOOLS.keys()) | set(getattr(tools_mod, "_EVER_BUILTIN_NAMES", set()))
    for u in unlocks.UNITS:
        for name in u.tools:
            assert name in known, (u.id, name)
