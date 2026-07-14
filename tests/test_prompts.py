"""Prompt surfaces for the growing body (TOOL_PROGRESSION.md W2b) — the RED GATES.

These are the durable defense, not review hopes (§0 / CREATURE_GENETICS red-gate list):

  BODY-NOUN GATE   — no creature-facing string constant in prompts.py (nor the assembled flag-on
                     prompt) contains a literal body noun from ANY morph's lexicon
                     (genome.ALL_BODY_NOUNS); anatomy renders ONLY through {placeholder} templating
                     from the creature's own morph row (phenotype.body_words).
  TOOL-NAME GATE   — SYSTEM_PROMPT_CREATURE_BASE names no tool from any unit; each UNIT_STANZAS[u]
                     names ONLY unit u's tools; every tool of every unit appears in exactly one
                     stanza (a locked tool DOES NOT EXIST: never named, never teased).
  ASSEMBLY GATE    — flag-on: the assembled system prompt is BASE + exactly the granted units'
                     stanzas, in unlocks.UNITS canonical order, rendered with the fixture genome's
                     morph lexicon, and append-only between in-order grants (KV prefix). Flag-off:
                     the legacy SYSTEM_PROMPT_CREATURE renders BYTE-IDENTICALLY (test-pinned by
                     sha256 — an edit to the legacy constant is a conscious act). House mode is
                     untouched by the flag entirely.
  LEAK SWEEP       — the creature-facing platform strings in context.py (boss-voice nudge, park /
                     urgency wording, the RUMINATING nudge, the loop circuit-breaker) never name a
                     locked tool flag-on, and keep their legacy bytes flag-off.

No services / tick loop / GPU — temp workspaces only (eiDOS is live on this machine).
"""
import hashlib
import json
import os
import re
import shutil
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent.parent))

import context
import genome
import prompts
import unlocks
from config import Config
from prompts import (
    SYSTEM_PROMPT_BRIEFING,
    SYSTEM_PROMPT_CREATURE,
    SYSTEM_PROMPT_CREATURE_BASE,
    TICK_PROMPT,
    TICK_PROMPT_LOOP_DETECTED,
    TICK_PROMPT_LOOP_DETECTED_CREATURE,
    UNIT_STANZAS,
    render_creature_system_prompt,
)

# --- Pins ----------------------------------------------------------------------------------------
# The legacy constants are the FLAG-OFF surface: byte-identical rendering is doctrine, so their
# bytes are pinned. Changing either is a conscious act: update the hash in the same commit that
# proves flag-off behavior was meant to change.
_LEGACY_CREATURE_SHA256 = "94da241b3784e5254ac16e0e975b69d71410057201c8e85c7a1a5c41c4f413a1"
_LEGACY_LOOP_SHA256 = "39d237e1bf0ca5befef58a031fc39e1d0575c6455362e0bb4d532e227b96dd01"

# Every tool of every unit, mapped to its owning unit (unlocks is the single source of truth).
_TOOL_OWNER = {t: u.id for u in unlocks.UNITS for t in u.tools}

# The flag-on creature-facing string constants in prompts.py — the body-noun gate's scan set.
# (The legacy SYSTEM_PROMPT_CREATURE / TICK_PROMPT_LOOP_DETECTED are the grandfathered FLAG-OFF
# surface — replaced, not scanned; they are pinned byte-identical above instead.)
_CREATURE_FACING = {
    "SYSTEM_PROMPT_CREATURE_BASE": SYSTEM_PROMPT_CREATURE_BASE,
    "TICK_PROMPT": TICK_PROMPT,
    "TICK_PROMPT_LOOP_DETECTED_CREATURE": TICK_PROMPT_LOOP_DETECTED_CREATURE,
    **{f"UNIT_STANZAS[{u}]": s for u, s in UNIT_STANZAS.items()},
}

# A sentinel lexicon: replaces every placeholder with a token that is itself no body noun, so any
# anatomy word SURVIVING the render is hardcoded — the exact definition of the drift being gated.
_SENTINEL_LEXICON = {k: f"«{k}»" for k in genome.LEXICON_KEYS}


def _word_hits(text: str, words) -> list:
    """Whole-word (and whole-phrase) matches, case-insensitive; underscores/letters/digits bound a
    word so `note_append` never matches inside `note_appendix` and `see` never matches `seeing`."""
    low = text.lower()
    return sorted(w for w in words
                  if re.search(r"(?<![a-z0-9_])" + re.escape(w) + r"(?![a-z0-9_])", low))


def _strip_placeholders(text: str) -> str:
    return re.sub(r"\{[a-z_]+\}", " ", text)


def _cfg(tmp: str, *, creature: bool = True) -> Config:
    cfg = Config()
    cfg.workspace_dir = os.path.join(tmp, "workspace")
    cfg.knowledge_enabled = False
    cfg.creature_mode = creature
    os.makedirs(cfg.workspace_dir, exist_ok=True)
    os.makedirs(str(cfg.interventions_dir), exist_ok=True)
    return cfg


def _write_genome(cfg: Config, morph: str) -> None:
    (cfg.workspace / "genome.json").write_text(
        json.dumps({"v": 2, "seed": 7, "morph": morph}), encoding="utf-8")


def _assemble(cfg: Config, **kw):
    with patch("context.generate_env_alerts", return_value=""):
        return context.assemble_context(cfg, tick_number=kw.pop("tick_number", 1),
                                        goal_start_time=time.time(), **kw)


# =================================================================================================
class TestLegacyPinned(unittest.TestCase):
    """Flag off ⇒ byte-identical everywhere; the legacy constants themselves are hash-pinned."""

    def test_legacy_creature_prompt_bytes_pinned(self):
        self.assertEqual(hashlib.sha256(SYSTEM_PROMPT_CREATURE.encode("utf-8")).hexdigest(),
                         _LEGACY_CREATURE_SHA256,
                         "SYSTEM_PROMPT_CREATURE changed — the flag-off surface must stay "
                         "byte-identical (update the pin only with a deliberate flag-off change)")

    def test_legacy_loop_prompt_bytes_pinned(self):
        self.assertEqual(hashlib.sha256(TICK_PROMPT_LOOP_DETECTED.encode("utf-8")).hexdigest(),
                         _LEGACY_LOOP_SHA256)
        self.assertIn("create the skill", TICK_PROMPT_LOOP_DETECTED)   # the legacy wording, kept

    def test_creature_variant_is_generic(self):
        self.assertNotIn("create the skill", TICK_PROMPT_LOOP_DETECTED_CREATURE)


# =================================================================================================
class TestBodyNounGate(unittest.TestCase):
    """No literal body noun outside the lexicon renderer (CREATURE_GENETICS red gate #1)."""

    def test_no_hardcoded_anatomy_in_creature_facing_constants(self):
        for name, text in _CREATURE_FACING.items():
            hits = _word_hits(_strip_placeholders(text), genome.ALL_BODY_NOUNS)
            self.assertEqual(hits, [], f"{name} hardcodes body noun(s) {hits} — anatomy must "
                                       "render through the morph lexicon placeholders")

    def test_no_hardcoded_anatomy_in_full_assembled_prompt(self):
        """Sentinel render of BASE + EVERY stanza: any body noun that survives is hardcoded."""
        rendered = render_creature_system_prompt(_SENTINEL_LEXICON, unlocks.UNIT_IDS)
        hits = _word_hits(rendered, genome.ALL_BODY_NOUNS)
        self.assertEqual(hits, [], f"assembled prompt hardcodes body noun(s) {hits}")

    def test_own_morph_words_do_render(self):
        """The inverse control: with a real morph row, its own anatomy IS present (warmth stays)."""
        lex = genome.MORPHS["burrower"]["lexicon"]
        rendered = render_creature_system_prompt(lex, unlocks.UNIT_IDS)
        self.assertIn("paws", rendered)
        self.assertIn("den", rendered)


# =================================================================================================
class TestToolNameGate(unittest.TestCase):
    """A locked tool does not exist: the base teaches none, each stanza teaches exactly its own."""

    def test_base_names_no_tool_from_any_unit(self):
        hits = _word_hits(SYSTEM_PROMPT_CREATURE_BASE, _TOOL_OWNER)
        self.assertEqual(hits, [], f"BASE names tool(s) {hits} — the being-text must be timeless")

    def test_stanza_keys_are_the_canonical_ladder(self):
        self.assertEqual(tuple(UNIT_STANZAS), unlocks.UNIT_IDS,
                         "UNIT_STANZAS must cover every unit, in canonical grant order")

    def test_each_stanza_names_exactly_its_own_units_tools(self):
        for uid, stanza in UNIT_STANZAS.items():
            named = set(_word_hits(stanza, _TOOL_OWNER))
            own = set(unlocks.unit(uid).tools)
            self.assertEqual(named - own, set(),
                             f"stanza {uid!r} names foreign tool(s) {sorted(named - own)}")
            self.assertEqual(own - named, set(),
                             f"stanza {uid!r} never teaches its own tool(s) {sorted(own - named)}")

    def test_every_tool_in_exactly_one_stanza(self):
        seen: dict = {}
        for uid, stanza in UNIT_STANZAS.items():
            for t in _word_hits(stanza, _TOOL_OWNER):
                self.assertNotIn(t, seen, f"{t!r} appears in both {seen.get(t)!r} and {uid!r}")
                seen[t] = uid
        self.assertEqual(set(seen), set(_TOOL_OWNER))

    def test_flag_on_tick_prompts_name_no_tools(self):
        for name in ("TICK_PROMPT", "TICK_PROMPT_LOOP_DETECTED_CREATURE"):
            hits = _word_hits(_strip_placeholders(getattr(prompts, name)), _TOOL_OWNER)
            self.assertEqual(hits, [], f"{name} names tool(s) {hits}")


# =================================================================================================
class TestRenderer(unittest.TestCase):
    """render_creature_system_prompt: fail-open, canonical order, append-only, fully resolved."""

    def test_every_morph_resolves_every_placeholder(self):
        for morph, row in genome.MORPHS.items():
            rendered = render_creature_system_prompt(row["lexicon"], unlocks.UNIT_IDS)
            leftover = re.findall(r"\{[a-z_]+\}", rendered)
            self.assertEqual(leftover, [], f"{morph}: unresolved placeholder(s) {leftover}")

    def test_missing_lexicon_key_renders_literally_never_raises(self):
        rendered = render_creature_system_prompt({}, ("body",))
        self.assertIn("{mover}", rendered)          # degraded but alive (fail-open contract)

    def test_newborn_floor_always_included(self):
        rendered = render_creature_system_prompt(_SENTINEL_LEXICON, ())
        self.assertIn("bash", rendered)             # the body stanza rides even with no grants
        self.assertNotIn("memorize", rendered)

    def test_canonical_order_regardless_of_input_order(self):
        rendered = render_creature_system_prompt(
            _SENTINEL_LEXICON, ("workshop", "memory", "skillcraft"))
        self.assertLess(rendered.index("check_tools"), rendered.index("memorize"))
        self.assertLess(rendered.index("memorize"), rendered.index("create_skill"))
        self.assertLess(rendered.index("create_skill"), rendered.index("delegate"))

    def test_in_order_grants_grow_append_only(self):
        """The KV property: each in-order grant extends the previous prompt as a strict prefix."""
        lex = genome.MORPHS["moth"]["lexicon"]
        grown = []
        for i in range(len(unlocks.UNIT_IDS)):
            grown.append(render_creature_system_prompt(lex, unlocks.UNIT_IDS[:i + 1]))
        for smaller, bigger in zip(grown, grown[1:]):
            self.assertTrue(bigger.startswith(smaller),
                            "a canonical-order grant must only APPEND to the prompt")

    def test_unknown_unit_ids_ignored(self):
        a = render_creature_system_prompt(_SENTINEL_LEXICON, ("body", "wings"))
        b = render_creature_system_prompt(_SENTINEL_LEXICON, ("body",))
        self.assertEqual(a, b)


# =================================================================================================
class TestAssemblyGate(unittest.TestCase):
    """The assembled flag-on prompt: exactly the granted stanzas, canonical order, own-morph words;
    flag-off: the legacy constant byte-for-byte. Fixture workspaces, no services."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.config = _cfg(self.tmp)

    def test_flag_off_is_the_legacy_constant_byte_for_byte(self):
        _write_genome(self.config, "otter")
        # even with grants on the books, flag off renders the legacy constant untouched
        unlocks.grant(self.config, "memory", "t")
        for flag in (None, False):
            if flag is not None:
                self.config.pillars_tool_unlocks_enabled = flag
            messages = _assemble(self.config)
            self.assertEqual(
                messages[0]["content"],
                SYSTEM_PROMPT_CREATURE.format(workspace=str(self.config.workspace)))

    def test_house_mode_untouched_by_the_flag(self):
        cfg = _cfg(self.tmp, creature=False)
        cfg.pillars_tool_unlocks_enabled = True
        messages = _assemble(cfg)
        self.assertEqual(messages[0]["content"],
                         SYSTEM_PROMPT_BRIEFING.format(workspace=str(cfg.workspace)))

    def test_flag_on_newborn_units_body_only(self):
        _write_genome(self.config, "otter")
        self.config.pillars_tool_unlocks_enabled = True
        messages = _assemble(self.config)
        sysp = messages[0]["content"]
        # exactly BASE + the body stanza, rendered with the otter lexicon (mirror the config's
        # energy-feeling flag so this stays a unit-ordering test, not an energy-gate test)
        self.assertEqual(sysp, render_creature_system_prompt(
            genome.MORPHS["otter"]["lexicon"], ("body",),
            workspace=str(self.config.workspace),
            energy_feeling=getattr(self.config, "nervous_metabolism_enabled", True)))
        self.assertIn("webbed paws", sysp)                  # its own mover
        self.assertIn("holt", sysp)                         # its own home
        # locked units simply do not exist — no name, no tease
        for t in ("memorize", "recall", "create_skill", "manual", "predict",
                  "speak", "vision", "objective_add", "delegate"):
            self.assertEqual(_word_hits(sysp, {t}), [], t)
        # …and no foreign morph's anatomy either
        for noun in ("den", "wall-scratches", "beak", "cocoon", "feelers"):
            self.assertEqual(_word_hits(sysp, {noun}), [], noun)

    def test_flag_on_granted_units_render_in_canonical_order(self):
        _write_genome(self.config, "otter")
        self.config.pillars_tool_unlocks_enabled = True
        unlocks.grant(self.config, "skillcraft", "t")       # granted out of order on purpose
        unlocks.grant(self.config, "memory", "t")
        messages = _assemble(self.config)
        sysp = messages[0]["content"]
        self.assertEqual(sysp, render_creature_system_prompt(
            genome.MORPHS["otter"]["lexicon"], ("body", "memory", "skillcraft"),
            workspace=str(self.config.workspace),
            energy_feeling=getattr(self.config, "nervous_metabolism_enabled", True)))
        self.assertLess(sysp.index("check_tools"), sysp.index("memorize"))
        self.assertLess(sysp.index("memorize"), sysp.index("create_skill"))
        self.assertIn("pebble-pile", sysp)                  # the otter's notebook word
        for t in ("predict", "speak", "vision", "objective_add", "delegate"):
            self.assertEqual(_word_hits(sysp, {t}), [], t)

    def test_flag_on_grant_appends_only(self):
        _write_genome(self.config, "burrower")
        self.config.pillars_tool_unlocks_enabled = True
        before = _assemble(self.config)[0]["content"]
        unlocks.grant(self.config, "memory", "t")
        after = _assemble(self.config, tick_number=2)[0]["content"]
        self.assertTrue(after.startswith(before))
        self.assertIn("memorize", after)

    def test_stable_head_signature_re_renders_on_grant_flag_on_only(self):
        _write_genome(self.config, "otter")
        # flag off: a grant must not perturb the signature (byte-identical world)
        sig_off_a = context._stable_head_signature(self.config, True)
        unlocks.grant(self.config, "memory", "t")
        sig_off_b = context._stable_head_signature(self.config, True)
        self.assertEqual(sig_off_a, sig_off_b)
        # flag on: the granted-unit set is a prompt input → one re-render per grant
        self.config.pillars_tool_unlocks_enabled = True
        sig_on_a = context._stable_head_signature(self.config, True)
        unlocks.grant(self.config, "skillcraft", "t")
        sig_on_b = context._stable_head_signature(self.config, True)
        self.assertNotEqual(sig_on_a, sig_on_b)
        sig_on_c = context._stable_head_signature(self.config, True)
        self.assertEqual(sig_on_b, sig_on_c)                 # …and stable between grants


# =================================================================================================
class TestLeakSweep(unittest.TestCase):
    """context.py's creature-facing platform strings: locked tools never named flag-on; legacy
    bytes flag-off."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.config = _cfg(self.tmp)

    def _tick(self, **kw):
        return context._build_tick_prompt(
            self.config, kw.pop("tick_number", 1), time.time(),
            kw.pop("loop_detected", False), kw.pop("repeat_count", 0),
            kw.pop("max_ticks", 0), **kw)

    # --- the loop circuit-breaker -----------------------------------------------------------
    def test_loop_prompt_generic_flag_on_legacy_flag_off(self):
        msg_off = self._tick(loop_detected=True, repeat_count=3)
        self.assertIn("create the skill", msg_off)           # legacy bytes, flag off
        self.config.pillars_tool_unlocks_enabled = True
        msg_on = self._tick(loop_detected=True, repeat_count=3)
        self.assertIn("without real progress", msg_on)
        self.assertEqual(_word_hits(msg_on, _TOOL_OWNER), [])   # no tool named at all

    # --- the boss-voice nudge ---------------------------------------------------------------
    def test_voice_request_never_names_a_locked_speak(self):
        self.config.pillars_tool_unlocks_enabled = True
        msg = self._tick(boss_waiting=True, boss_text="speak to me out loud")
        self.assertEqual(_word_hits(msg, {"speak"}), [],
                         "a locked voice was named — the refusal must be indistinguishable "
                         "from a word that never existed")
        self.assertIn("run, build, make", msg)

    def test_voice_request_keeps_teeth_once_senses_granted(self):
        self.config.pillars_tool_unlocks_enabled = True
        unlocks.grant(self.config, "senses", "t")
        msg = self._tick(boss_waiting=True, boss_text="speak to me out loud")
        self.assertIn("🔊 BOSS WANTS TO HEAR YOU", msg)

    def test_voice_request_legacy_flag_off(self):
        msg = self._tick(boss_waiting=True, boss_text="speak to me out loud")
        self.assertIn("🔊 BOSS WANTS TO HEAR YOU", msg)

    # --- urgency / park wording -------------------------------------------------------------
    def test_urgency_note_locked_vs_granted(self):
        self.config.pillars_tool_unlocks_enabled = True
        msg = self._tick(tick_number=10, max_ticks=10)
        self.assertEqual(_word_hits(msg, {"objective_done"}), [])
        self.assertIn("FINAL TICK: state your result now", msg)
        unlocks.grant(self.config, "resolve", "t")
        msg = self._tick(tick_number=10, max_ticks=10)
        self.assertIn("mark the objective done (objective_done)", msg)

    def test_urgency_note_legacy_flag_off(self):
        msg = self._tick(tick_number=10, max_ticks=10)
        self.assertIn("mark the objective done (objective_done)", msg)
        msg = self._tick(tick_number=9, max_ticks=10)
        self.assertIn("wrap up and call objective_done if ready", msg)

    def test_park_pressure_locked_vs_granted(self):
        self.config.pillars_tool_unlocks_enabled = True
        msg = self._tick(tension=7)
        self.assertEqual(_word_hits(msg, {"objective_block"}), [])
        self.assertIn("set it down", msg)
        unlocks.grant(self.config, "resolve", "t")
        msg = self._tick(tension=7)
        self.assertIn("park it (objective_block)", msg)

    def test_park_pressure_legacy_flag_off(self):
        msg = self._tick(tension=7)
        self.assertIn("park it (objective_block)", msg)

    # --- the RUMINATING nudge ---------------------------------------------------------------
    def _presence(self):
        with patch("context.generate_env_alerts", return_value=""), \
             patch("glue.recent_outcomes", return_value=[]), \
             patch("glue.compute_condition", return_value="RUMINATING"):
            return context._build_presence(self.config, 1, time.time())

    def test_ruminating_nudge_locked_vs_granted_vs_flag_off(self):
        self.assertIn("memorize a fact", self._presence())          # legacy bytes, flag off
        self.config.pillars_tool_unlocks_enabled = True
        out = self._presence()
        self.assertEqual(_word_hits(out, {"memorize"}), [])
        self.assertIn("write down a fact", out)
        unlocks.grant(self.config, "memory", "t")
        self.assertIn("memorize a fact", self._presence())


if __name__ == "__main__":
    unittest.main()
