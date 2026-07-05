"""TOOL_PROGRESSION W2a — capability plumbing gate (visible_tools + the quest/milestone seams).

Pins (TOOL_PROGRESSION.md §0 + CREATURE_GENETICS.md, all behind pillars_tool_unlocks_enabled):

  FLAG OFF = BYTE-IDENTICAL. visible_tools returns the TOOLS OBJECT itself (grammar, dispatch,
  check_tools, manual all read it → every surface is provably the pre-ladder path); a full quest
  drive writes NO unlocks.json, NO announcement observations, NO genome/phenotype artifacts.

  LADDER ON (creature_mode AND the flag):
  · visible_tools = granted units ∪ aliases of granted ∪ hot-loaded self-authored skills — a pure
    filtered copy, never a registry mutation; the flag-registered `predict` is a lockable builtin,
    never mistaken for a skill.
  · the grammar built from the accessor cannot represent a locked name.
  · ISSUANCE-GRANT: a quest carrying grants_unit grants it BEFORE the system window that names it.
  · REWARD SINK: kind=="unlock" grants the unit (source quest_reward:<id>) and its riding XP leg
    pays through the standard path.
  · MILESTONES: adjudication runs in after_outcome and at the sleep boundary over the same typed
    stats dict; the I8 probe holds a service-gated grant PENDING until the organ answers.
  · ANNOUNCEMENTS: system register → ONE verbatim system_window turn; body register → the plain
    "dream" notice (the bracketed body-fact user turn) — one-shot, crash-safe.
  · STAGE TRANSITIONS: creature_gen.stage_for over persona level + creature.json hatched; a
    crossing expresses dormant alleles, persists the genome, writes phenotype.json — EXACTLY once.
  · §0 INDISTINGUISHABILITY: a locked builtin's refusal is byte-shaped like a name that never
    existed; check_tools / the manual name only what exists.

No services / GPU / live LLM — temp workspaces, mock mode, injected probes only.
"""
import json
import re
import sys
import time
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest

import eidos
import glue
import unlocks
from config import Config
from parser import ToolCall
from tools import TOOLS, execute_tool, tool_list_skills, tool_manual, visible_tools

_ROOT = Path(__file__).parent.parent

_NEWBORN = frozenset({"bash", "write_file", "read_file",
                      "note_append", "note_read", "note_list", "note_close", "check_tools"})


# --- rig (test_wiring.py's shape) -----------------------------------------------------------------

def _cfg(tmp_path, **flags) -> Config:
    cfg = Config()
    cfg.workspace_dir = str(tmp_path / "workspace")
    cfg.mock_mode = True
    for k, v in flags.items():
        setattr(cfg, k, v)
    cfg.workspace.mkdir(parents=True, exist_ok=True)
    cfg.state_dir.mkdir(parents=True, exist_ok=True)
    return cfg


def _ladder_cfg(tmp_path, **flags) -> Config:
    return _cfg(tmp_path, creature_mode=True, pillars_tool_unlocks_enabled=True, **flags)


def _drive_tick(hub, cfg, *, tick=1, success=True, tool="bash", persona=None):
    glue.record_outcome(cfg, success=success, fail_kind="" if success else "exec",
                        signature="" if success else "sig-x", tool=tool)
    hub.after_outcome(tick=tick, tool=tool, args={"cmd": "probe"}, success=success,
                      fail_kind="" if success else "exec", situation="obj1|probe",
                      summary="did a probe", event_text="probe output", persona=persona)


def _obs(cfg, tool=None):
    from memory import read_recent_observations
    rows = list(reversed(read_recent_observations(cfg, max_chars=200_000, max_count=200)))
    return [o for o in rows if tool is None or o.get("tool") == tool]


def _quest(qid, *, grants_unit="", reward=None, criterion=None, hidden=False):
    import quests
    return quests.Quest(
        id=qid, directive=f"Directive of {qid}.",
        success_criteria=criterion or quests.Criterion(path="persona.xp", op=">=", value=10**9),
        reward=reward or {"kind": quests.REWARD_XP, "amount": 0},
        grants_unit=grants_unit, hidden=hidden)


@pytest.fixture(autouse=True)
def _clean_registry():
    """predict registers into the GLOBAL tools.TOOLS; deregister after every test, and drop any
    hot-loaded fake skill this suite added."""
    yield
    try:
        from tools import register_predict_tool
        register_predict_tool(Config())
    except Exception:  # noqa: BLE001
        pass
    TOOLS.pop("wt_fake_skill", None)


# =================================================================================================
class TestFlagPlumbing:
    def test_flag_defaults_off_and_is_wired(self):
        assert Config().pillars_tool_unlocks_enabled is False
        assert "pillars_tool_unlocks_enabled" in eidos._PILLARS_WIRED_FLAGS  # hub constructs on it

    def test_config_toml_sets_the_flag_explicitly(self):
        # Shipped dark until the operator's flip (2026-07-05, the maiden walk); either way the
        # flag must be EXPLICIT in config.toml — never left to a silent default.
        text = (_ROOT / "config.toml").read_text(encoding="utf-8")
        assert re.search(r"^tool_unlocks_enabled = (true|false)\b", text, re.M)

    def test_toml_loader_parses_the_flag(self, tmp_path, monkeypatch):
        import config as config_mod
        cfg_file = tmp_path / "c.toml"
        cfg_file.write_text("[pillars]\ntool_unlocks_enabled = true\n", encoding="utf-8")
        cfg = config_mod.load_config(str(cfg_file))
        assert cfg.pillars_tool_unlocks_enabled is True


# =================================================================================================
class TestVisibleTools:
    def test_flag_off_is_the_registry_object(self, tmp_path):
        """The load-bearing byte-identity: every consumer reads THE registry, not a copy."""
        assert visible_tools(None) is TOOLS
        assert visible_tools(_cfg(tmp_path)) is TOOLS                                # house
        assert visible_tools(_cfg(tmp_path, creature_mode=True)) is TOOLS            # ladder off
        assert visible_tools(_cfg(tmp_path, pillars_tool_unlocks_enabled=True)) is TOOLS  # house mode

    def test_newborn_floor_and_no_mutation(self, tmp_path):
        cfg = _ladder_cfg(tmp_path)
        before = dict(TOOLS)
        vis = visible_tools(cfg)
        assert set(vis) == _NEWBORN
        assert dict(TOOLS) == before          # pure filter — no global mutation
        assert "memorize" in TOOLS            # the registry itself keeps every organ

    def test_grants_surface_tools_and_aliases(self, tmp_path):
        cfg = _ladder_cfg(tmp_path)
        unlocks.grant(cfg, "memory", "test")
        vis = set(visible_tools(cfg))
        assert {"memorize", "recall"} <= vis
        unlocks.grant(cfg, "senses", "test")
        vis = set(visible_tools(cfg))
        assert {"speak", "vision", "see"} <= vis      # the alias travels with its organ

    def test_house_only_tools_never_exist(self, tmp_path):
        """Even a fully-grown creature never sees the excluded house tools (TOOL_PROGRESSION)."""
        cfg = _ladder_cfg(tmp_path)
        unlocks.seed_from_evidence(cfg, {k: 1 for k in unlocks.EVIDENCE_KEYS})
        vis = set(visible_tools(cfg))
        for name in ("http_request", "fetch", "http", "bg_run", "bg_check", "ask_ai",
                     "net_scan", "tcp_probe", "http_probe", "udp_listen", "update_plan",
                     "update_self_guide", "propose_self_edit", "check_messages", "check_system"):
            assert name not in vis, f"house tool {name!r} leaked into the creature universe"

    def test_self_authored_skill_never_locked(self, tmp_path):
        cfg = _ladder_cfg(tmp_path)
        TOOLS["wt_fake_skill"] = lambda a, c: None    # skills.py's hot-load shape
        assert "wt_fake_skill" in visible_tools(cfg)  # its own makings are its own body

    def test_predict_is_a_lockable_builtin_not_a_skill(self, tmp_path):
        from tools import register_predict_tool
        cfg = _ladder_cfg(tmp_path, pillars_expectations_enabled=True)
        register_predict_tool(cfg)
        assert "predict" in TOOLS
        assert "predict" not in visible_tools(cfg)    # foresight not granted → does not exist
        unlocks.grant(cfg, "foresight", "test")
        assert "predict" in visible_tools(cfg)


# =================================================================================================
class TestDispatchBackstop:
    def test_locked_call_indistinguishable_from_unknown(self, tmp_path):
        cfg = _ladder_cfg(tmp_path)
        locked = execute_tool(ToolCall(tool="memorize", args={"fact": "x"}, raw=""), cfg)
        unknown = execute_tool(ToolCall(tool="zz_never_a_tool", args={}, raw=""), cfg)
        assert locked.success is False and unknown.success is False
        assert locked.fail_kind == "no_such_tool" == unknown.fail_kind
        # Same message byte-shape modulo the name itself (§0 indistinguishability).
        assert locked.output.replace("memorize", "@") == unknown.output.replace("zz_never_a_tool", "@")
        assert "memorize" not in unknown.output       # the listing names only what exists
        for line in (locked.output, unknown.output):
            assert "Available: " in line and "bash" in line

    def test_flag_off_locked_builtin_still_dispatches(self, tmp_path):
        cfg = _cfg(tmp_path, creature_mode=True)      # ladder off: recall is a normal organ
        r = execute_tool(ToolCall(tool="recall", args={"query": "x"}, raw=""), cfg)
        assert r.fail_kind != "no_such_tool"

    def test_granted_tool_dispatches_after_grant(self, tmp_path):
        cfg = _ladder_cfg(tmp_path)
        unlocks.grant(cfg, "memory", "test")
        r = execute_tool(ToolCall(tool="recall", args={"query": "x"}, raw=""), cfg)
        assert r.fail_kind != "no_such_tool"


# =================================================================================================
class TestGrammar:
    def test_locked_name_unrepresentable(self, tmp_path):
        from grammar import build_tick_grammar
        cfg = _ladder_cfg(tmp_path)
        gram = build_tick_grammar(visible_tools(cfg).keys())
        assert '"bash"' in gram and '"note_append"' in gram
        for name in ("memorize", "recall", "create_skill", "predict", "speak", "delegate",
                     "objective_add", "http_request", "ask_ai"):
            assert f'"{name}"' not in gram, f"locked/house name {name!r} representable at the sampler"
        unlocks.grant(cfg, "memory", "test")
        gram2 = build_tick_grammar(visible_tools(cfg).keys())
        assert '"memorize"' in gram2                  # next tick the grammar accepts the word

    def test_tick_call_site_builds_from_the_accessor(self):
        """The loop's grammar call site reads visible_tools(config) — pinned at source level so a
        refactor back to raw TOOLS is a failing test, not a review hope."""
        src = (_ROOT / "eidos.py").read_text(encoding="utf-8")
        assert "_live_tools = visible_tools(config)" in src
        assert "tick_grammar_cached(_live_tools.keys()" in src


# =================================================================================================
class TestIssuanceGrant:
    def test_grant_lands_before_the_window(self, tmp_path, monkeypatch):
        cfg = _ladder_cfg(tmp_path, pillars_quests_enabled=True)
        hub = eidos._Pillars(cfg)
        hub.quests.propose(_quest("genesis-01", grants_unit="skillcraft"))
        seen = []          # (output, was create_skill already real when this window was written?)
        real = eidos.append_observation

        def _spy(config, obs):
            seen.append((obs.get("tool"), str(obs.get("output", "")),
                         "create_skill" in unlocks.granted_tools(cfg)))
            return real(config, obs)

        monkeypatch.setattr(eidos, "append_observation", _spy)
        hub._issue_next({"level": 1, "xp": 0}, tick=3)
        assert "create_skill" in unlocks.granted_tools(cfg)
        doc = json.loads((cfg.state_dir / unlocks.STATE_NAME).read_text(encoding="utf-8"))
        assert doc["granted"]["skillcraft"]["source"] == "quest_issue:genesis-01"
        windows = [s for s in seen if s[0] == "system_window"]
        issued = [s for s in windows if "QUEST ISSUED" in s[1]]
        granted = [s for s in windows if s[1].startswith("[SYSTEM] GRANTED:")]
        assert len(issued) == 1 and issued[0][2], "the window named a tool that did not yet exist"
        assert len(granted) == 1 and "create_skill" in granted[0][1]
        assert seen.index(granted[0]) < seen.index(issued[0])   # payment first, then the training

    def test_hidden_quest_still_grants_silently(self, tmp_path):
        cfg = _ladder_cfg(tmp_path, pillars_quests_enabled=True)
        hub = eidos._Pillars(cfg)
        hub.quests.propose(_quest("hidden-grant", grants_unit="foresight", hidden=True))
        hub._issue_next({"level": 1, "xp": 0}, tick=2)
        assert "predict" in unlocks.granted_tools(cfg)
        assert not [o for o in _obs(cfg, "system_window") if "QUEST ISSUED" in o["output"]]
        # …but the grant's own felt moment DID land (the System paid on-screen).
        assert [o for o in _obs(cfg, "system_window") if "[SYSTEM] GRANTED: predict" in o["output"]]

    def test_no_grants_unit_grants_nothing(self, tmp_path):
        cfg = _ladder_cfg(tmp_path, pillars_quests_enabled=True)
        hub = eidos._Pillars(cfg)
        hub.quests.propose(_quest("plain"))
        hub._issue_next({"level": 1, "xp": 0}, tick=2)
        assert unlocks.granted_tools(cfg) == frozenset(_NEWBORN)


# =================================================================================================
class TestRewardUnlock:
    def test_unlock_reward_pays_the_limb_and_the_xp_leg(self, tmp_path):
        import quests
        cfg = _ladder_cfg(tmp_path, pillars_quests_enabled=True)
        hub = eidos._Pillars(cfg)
        hub.quests.propose(_quest(
            "genesis-03", reward={"kind": quests.REWARD_UNLOCK, "what": "workshop", "xp": 5},
            criterion=quests.Criterion(path="persona.xp", op=">=", value=0)))
        persona = {"level": 1, "xp": 0}
        hub._issue_next(persona, tick=4)
        _drive_tick(hub, cfg, tick=5, persona=persona)          # criteria met → glue closes + pays
        assert "delegate" in unlocks.granted_tools(cfg)
        doc = json.loads((cfg.state_dir / unlocks.STATE_NAME).read_text(encoding="utf-8"))
        assert doc["granted"]["workshop"]["source"] == "quest_reward:genesis-03"
        assert persona["xp"] == 5                                # the riding XP leg still pays
        paid = [o for o in _obs(cfg, "system_window") if "[SYSTEM] PAID: delegate" in o["output"]]
        assert len(paid) == 1                                    # the felt moment, same tick

    def test_flag_off_unlock_reward_stays_dark(self, tmp_path):
        """Byte-identity: with the ladder off an unlock reward is recorded on the quest and
        NOTHING else moves — no XP leg, no grant, no books."""
        import quests
        cfg = _cfg(tmp_path, creature_mode=True, pillars_quests_enabled=True)
        hub = eidos._Pillars(cfg)
        hub.quests.propose(_quest(
            "g3", reward={"kind": quests.REWARD_UNLOCK, "what": "workshop", "xp": 5},
            criterion=quests.Criterion(path="persona.xp", op=">=", value=0)))
        persona = {"level": 1, "xp": 0}
        hub._issue_next(persona, tick=4)
        _drive_tick(hub, cfg, tick=5, persona=persona)
        assert persona["xp"] == 0
        assert not (cfg.state_dir / unlocks.STATE_NAME).exists()


# =================================================================================================
class TestMilestonesAndProbe:
    def _earn_senses_evidence(self, cfg, hub):
        """sleeps.total=2 (the gate's monotonic counter) + one adjudicated PASS in the store."""
        import level_gates
        import quests
        level_gates.record_sleep_cycle(cfg)
        level_gates.record_sleep_cycle(cfg)
        passed = _quest("done1", criterion=quests.Criterion(path="persona.xp", op=">=", value=0))
        passed.state = quests.PASSED
        hub.quests.store.save(hub.quests.store.load() + [passed])

    def test_pending_until_probe_answers(self, tmp_path):
        cfg = _ladder_cfg(tmp_path, pillars_quests_enabled=True,
                          pillars_mastery_gates_enabled=True)
        hub = eidos._Pillars(cfg)
        hub.unlock_probe = lambda s: False            # the organ is down (Sprinter today)
        self._earn_senses_evidence(cfg, hub)
        persona = {"level": 1, "xp": 0}
        _drive_tick(hub, cfg, tick=6, persona=persona)
        granted = unlocks.granted_tools(cfg)
        assert {"memorize", "recall"} <= granted      # U1 landed (sleeps ≥ 1)
        assert "speak" not in granted                 # I8 hold
        doc = json.loads((cfg.state_dir / unlocks.STATE_NAME).read_text(encoding="utf-8"))
        assert "senses" in doc["pending"]
        hub.unlock_probe = lambda s: True             # the organ comes up → the grant lands
        _drive_tick(hub, cfg, tick=7, persona=persona)
        assert {"speak", "vision", "see"} <= unlocks.granted_tools(cfg)

    def test_sleep_window_adjudicates_at_the_boundary(self, tmp_path):
        cfg = _ladder_cfg(tmp_path, pillars_sleep_engine_enabled=True,
                          pillars_memory_engram_enabled=True, pillars_mastery_gates_enabled=True)
        from nervous.neuromod import Adenosine
        nm = types.SimpleNamespace(adenosine=Adenosine())
        # The dream-vs-nap split: only a NAP advances sleeps.total, and U1 rides the first
        # nap's wake — so this boundary must arrive with real accumulated wake pressure.
        nm.adenosine.accumulate(nm.adenosine.max_wake_hours)
        hub = eidos._Pillars(cfg, neuromod=nm)
        hub.unlock_probe = lambda s: False
        report = hub.sleep_window(tick=2, persona={"level": 1, "xp": 0}, observations=[])
        assert report is not None and report.results
        assert {"memorize", "recall"} <= unlocks.granted_tools(cfg)   # U1 on the first wake
        notes = [o for o in _obs(cfg, "dream") if "memorize" in o.get("output", "")]
        assert len(notes) == 1                        # the felt moment arrives WITH the waking

    def test_default_probe_is_bounded_and_memoized(self, tmp_path, monkeypatch):
        cfg = _cfg(tmp_path)
        cfg.voice_port = 1                            # nothing answers port 1
        eidos._unlock_probe_cache.clear()
        assert eidos._probe_service(cfg, "voice") is False
        # Memoized: a second call inside the TTL never touches the network again.
        import urllib.request

        def _boom(*a, **k):
            raise AssertionError("probe re-hit the port inside the TTL")

        monkeypatch.setattr(urllib.request, "urlopen", _boom)
        assert eidos._probe_service(cfg, "voice") is False
        eidos._unlock_probe_cache.clear()
        assert eidos._probe_service(cfg, "unknown_organ") is False    # never guessed

    def test_default_probe_counts_any_http_answer(self, tmp_path, monkeypatch):
        import urllib.error
        import urllib.request
        cfg = _cfg(tmp_path)
        eidos._unlock_probe_cache.clear()
        monkeypatch.setattr(urllib.request, "urlopen",
                            lambda *a, **k: (_ for _ in ()).throw(
                                urllib.error.HTTPError("u", 404, "nf", {}, None)))
        assert eidos._probe_service(cfg, "voice") is True   # a 404 is a live organ
        eidos._unlock_probe_cache.clear()


# =================================================================================================
class TestAnnouncementRegisters:
    def test_registers_render_through_the_right_streams(self, tmp_path):
        import context as context_mod
        cfg = _ladder_cfg(tmp_path)
        hub = eidos._Pillars(cfg)
        unlocks.grant(cfg, "memory", "milestone")     # body register
        unlocks.grant(cfg, "workshop", "quest_reward:g3")   # system register
        hub._announce_unlocks(9)
        body = _obs(cfg, "dream")
        system = _obs(cfg, "system_window")
        assert len(body) == 1 and len(system) == 1
        assert body[0]["output"] == "overnight, new words settled in you: memorize, recall"
        assert system[0]["output"] == "[SYSTEM] PAID: delegate. Capacity 1."
        thread = context_mod._build_history_thread(cfg)
        rendered = [m for m in thread if m["role"] == "user"]
        assert any(m["content"] == "[SYSTEM] PAID: delegate. Capacity 1." for m in rendered)
        assert any(m["content"] == "[you rested and consolidated memory — overnight, new words "
                                   "settled in you: memorize, recall]" for m in rendered)

    def test_one_shot_never_replays(self, tmp_path):
        cfg = _ladder_cfg(tmp_path)
        hub = eidos._Pillars(cfg)
        unlocks.grant(cfg, "memory", "milestone")
        hub._announce_unlocks(9)
        n = len(_obs(cfg))
        hub._announce_unlocks(10)
        eidos._Pillars(cfg)._announce_unlocks(11)     # a fresh hub (restart) replays nothing
        assert len(_obs(cfg)) == n


# =================================================================================================
class TestStageTransitions:
    def _genome_with_dormant_allele(self, cfg):
        import genome
        g = genome.Genome(cfg)                        # birth (test-only; the loop never births)
        g.alleles["weathering"] = {"variant": "storm_marked", "expressed_at_stage": None}
        g.save()
        genome._cache.clear()                         # tests must re-read from disk
        return g

    def test_crossing_expresses_persists_and_writes_phenotype_once(self, tmp_path, monkeypatch):
        import genome
        import phenotype
        cfg = _ladder_cfg(tmp_path)
        hub = eidos._Pillars(cfg)
        self._genome_with_dormant_allele(cfg)
        writes = []
        real_write = phenotype.write_phenotype
        monkeypatch.setattr(phenotype, "write_phenotype",
                            lambda c, g, s: writes.append(s) or real_write(c, g, s))
        persona = {"level": 3, "xp": 0, "total_errors_recovered": 20}   # juvenile; allele earned
        _drive_tick(hub, cfg, tick=3, persona=persona)
        assert writes == ["juvenile"]
        g = genome.Genome.load(cfg)
        assert [h["stage"] for h in g.stage_history] == ["juvenile"]
        assert g.alleles["weathering"]["expressed_at_stage"] == "juvenile"
        doc = json.loads((cfg.workspace / "phenotype.json").read_text(encoding="utf-8"))
        assert doc["stage"] == "juvenile" and "storm_marked" in doc["expressed"]
        # Same stage again — exactly once per crossing: no new writes, no new history.
        _drive_tick(hub, cfg, tick=4, persona=persona)
        _drive_tick(hub, cfg, tick=5, persona=persona)
        assert writes == ["juvenile"]
        # A fresh hub at the SAME stage (restart) also writes nothing new.
        _drive_tick(eidos._Pillars(cfg), cfg, tick=6, persona=persona)
        assert writes == ["juvenile"]
        # The next crossing writes exactly once more.
        persona["level"] = 5                          # adult
        _drive_tick(hub, cfg, tick=7, persona=persona)
        assert writes == ["juvenile", "adult"]
        g = genome.Genome.load(cfg)
        assert [h["stage"] for h in g.stage_history] == ["juvenile", "adult"]

    def test_hatched_flag_read_like_neuromod(self, tmp_path):
        """Stage derivation matches the nap curve's exactly: level 1 + creature.json hatched=true
        → hatchling, not egg."""
        import genome
        cfg = _ladder_cfg(tmp_path)
        hub = eidos._Pillars(cfg)
        self._genome_with_dormant_allele(cfg)
        (cfg.workspace / "creature.json").write_text(json.dumps({"hatched": True}),
                                                     encoding="utf-8")
        _drive_tick(hub, cfg, tick=2, persona={"level": 1, "xp": 0})
        g = genome.Genome.load(cfg)
        assert [h["stage"] for h in g.stage_history] == ["hatchling"]

    def test_no_genome_never_births_one(self, tmp_path):
        cfg = _ladder_cfg(tmp_path)
        hub = eidos._Pillars(cfg)
        _drive_tick(hub, cfg, tick=2, persona={"level": 3, "xp": 0})
        assert not (cfg.workspace / "genome.json").exists()
        assert not (cfg.workspace / "phenotype.json").exists()


# =================================================================================================
class TestFlagOffByteIdentity:
    def test_full_quest_drive_writes_no_ladder_state(self, tmp_path):
        """Ladder off, quests on: a grants_unit quest issues and closes exactly as before — no
        unlocks.json, no announcement observations, no genome/phenotype writes."""
        import quests
        cfg = _cfg(tmp_path, creature_mode=True, pillars_quests_enabled=True)
        hub = eidos._Pillars(cfg)
        hub.quests.propose(_quest("g1", grants_unit="skillcraft",
                                  criterion=quests.Criterion(path="persona.xp", op=">=", value=0),
                                  reward={"kind": quests.REWARD_XP, "amount": 7}))
        persona = {"level": 1, "xp": 0}
        hub._issue_next(persona, tick=2)
        _drive_tick(hub, cfg, tick=3, persona=persona)
        assert persona["xp"] == 7                                   # the pre-ladder payout path
        assert not (cfg.state_dir / unlocks.STATE_NAME).exists()    # no books
        assert _obs(cfg, "dream") == []                             # no body notices
        assert not [o for o in _obs(cfg, "system_window") if "GRANTED" in o["output"]]
        assert not (cfg.workspace / "genome.json").exists()
        assert not (cfg.workspace / "phenotype.json").exists()

    def test_check_tools_flag_off_names_everything(self, tmp_path):
        cfg = _cfg(tmp_path, creature_mode=True)
        out = tool_list_skills({}, cfg).output
        assert "memorize" in out
        assert "(none yet — author one with create_skill)" in out


# =================================================================================================
class TestCheckToolsSurface:
    def test_locked_names_absent_and_empty_state_generic(self, tmp_path):
        cfg = _ladder_cfg(tmp_path)
        out = tool_list_skills({}, cfg).output
        for name in ("memorize", "recall", "create_skill", "bg_run", "update_plan"):
            assert name not in out, f"check_tools names the locked/house tool {name!r}"
        assert "bash" in out
        assert "(none yet)" in out                    # generic — the forge is not teased

    def test_grant_grows_the_listing(self, tmp_path):
        cfg = _ladder_cfg(tmp_path)
        unlocks.grant(cfg, "memory", "milestone")
        unlocks.grant(cfg, "skillcraft", "quest_issue:g1")
        out = tool_list_skills({}, cfg).output
        assert "memorize" in out and "recall" in out
        assert "author one with create_skill" in out  # the hint returns with the forge


# =================================================================================================
class TestManualSurface:
    def test_flag_off_byte_identical(self, tmp_path):
        cfg = _cfg(tmp_path, creature_mode=True)
        whole = tool_manual({}, cfg).output
        assert whole == (_ROOT / "OPERATING_MANUAL.md").read_text(encoding="utf-8")
        tts = tool_manual({"topic": "tts"}, cfg).output
        assert tts.startswith("## tts")
        miss = tool_manual({"topic": "zzz"}, cfg).output
        assert "Topics: tts, vision, ask_ai, network, devices, cpu, delegate." in miss

    def test_locked_topic_indistinguishable_from_unknown(self, tmp_path):
        cfg = _ladder_cfg(tmp_path)
        unlocks.grant(cfg, "skillcraft", "test")      # the manual itself exists, its pages don't
        locked = tool_manual({"topic": "tts"}, cfg).output
        unknown = tool_manual({"topic": "zzz"}, cfg).output
        assert locked.replace("tts", "@") == unknown.replace("zzz", "@")
        assert "delegate" not in locked               # the topic list names only what exists
        assert tool_manual({}, cfg).output == "The manual has no pages yet."

    def test_pages_appear_with_their_organs(self, tmp_path):
        cfg = _ladder_cfg(tmp_path)
        unlocks.grant(cfg, "skillcraft", "test")
        unlocks.grant(cfg, "senses", "test")
        unlocks.grant(cfg, "workshop", "test")
        tts = tool_manual({"topic": "tts"}, cfg).output
        assert tts.startswith("## tts")
        whole = tool_manual({}, cfg).output
        assert "## tts" in whole and "## vision" in whole and "## delegate" in whole
        for gone in ("## network", "## devices", "## cpu", "## ask_ai", "_When something here"):
            assert gone not in whole, f"{gone!r} rendered without its organ"
        miss = tool_manual({"topic": "zzz"}, cfg).output
        assert "Topics: tts, vision, delegate." in miss
