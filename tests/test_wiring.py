"""Pillars 5.5 — the wiring-pass gate (PILLARS_TODO Phase 5.5 "Wiring pass (code, still dark)").

Two halves, matching the phase's promise:

  1. BYTE-IDENTITY FLAGS-OFF (the load-bearing assertion): with every pillars flag off (the
     default Config), the context render for a synthetic state is byte-for-byte identical to the
     PRE-WIRING code path (context.py extracted from the pre-wiring commit and run on the SAME
     inputs), the wiring hub is never constructed (`_pillars_any_enabled` is False → the tick
     body's new branches are all dead), and a flags-off runtime imports NO pillars module at all
     (asserted in a subprocess so this suite's own imports can't contaminate sys.modules).

  2. FLAGS-ON SMOKE (offline, mock only): with each flag on singly, the wired call sites execute
     without exception on a synthetic tick-shaped drive; the coupled all-flags-on hub survives a
     full tick + sleep window; and a deliberately-raising subsystem is swallowed-with-log, never
     propagated (I5). No GPU, no services, no live LLM — the hub's lazy mind provably stays None
     under mock mode, and the administrator smoke injects a recording mock through the test seam.
"""

import json
import subprocess
import sys
import textwrap
import time
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest

import context as context_mod
import eidos
import glue
from config import Config

_ROOT = Path(__file__).parent.parent

# The commit this wiring pass was built against — the last all-dark, un-wired tree. The identity
# test renders context through THAT context.py (same inputs, same repo siblings) and requires the
# wired render to match byte-for-byte with flags off.
PRE_WIRING_COMMIT = "c8d9af6"

# Modules that must NOT be imported by a flags-off runtime (the dark guarantee). nervous.salience
# is excluded: the nervous package re-exports it unconditionally (pre-existing, dark by flag).
PILLARS_MODULES = ("engram", "memory_manager", "bets", "expectations", "quests", "news",
                   "administrator", "level_gates", "learning_progress")


# ==================================================================================================
# Rig
# ==================================================================================================
def _mk_config(tmp_path, **flags) -> Config:
    cfg = Config()
    cfg.workspace_dir = str(tmp_path / "workspace")
    cfg.mock_mode = True          # the hub's lazy llm stays None — no test can reach a live model
    for k, v in flags.items():
        setattr(cfg, k, v)
    cfg.workspace.mkdir(parents=True, exist_ok=True)
    cfg.state_dir.mkdir(parents=True, exist_ok=True)
    return cfg


def _seed_workspace(cfg) -> None:
    """A small synthetic state so the render has real material (goal / plan / an observation)."""
    from memory import append_observation, write_plan
    (cfg.workspace / "goal.md").write_text(
        "# Goal\n**Immediate focus**: probe the router\n", encoding="utf-8")
    write_plan(cfg, "# Plan\n1. probe the router\n")
    append_observation(cfg, {"tick": 1, "tool": "bash", "args": {"cmd": "echo hi"},
                             "success": True, "output": "hi"})


def _drive_tick(hub, cfg, *, tick=1, success=True, tool="bash", persona=None,
                situation="obj1|probe the router"):
    """One synthetic tick through every wired call site, in loop order."""
    hub.pre_tick(tick)
    hub.recall_block(situation=situation, query="probe the router")
    hub.open_bets(tick)
    glue.record_outcome(cfg, success=success, fail_kind="" if success else "exec",
                        signature="" if success else "sig-x", tool=tool)
    hub.after_outcome(tick=tick, tool=tool, args={"cmd": "probe"}, success=success,
                      fail_kind="" if success else "exec", situation=situation,
                      summary="did a probe", event_text="probe output", persona=persona)


@pytest.fixture(autouse=True)
def _cleanup_predict_tool():
    """The predict tool registers into the GLOBAL tools.TOOLS; deregister after every test here so
    an expectations-flag smoke can't leak the tool into unrelated suites."""
    yield
    try:
        from tools import register_predict_tool
        register_predict_tool(Config())     # flags off → pops 'predict'
    except Exception:  # noqa: BLE001
        pass


# ==================================================================================================
# 1. Byte-identity flags-off — the load-bearing assertions
# ==================================================================================================
def test_flags_off_hub_never_constructed():
    """Default Config → _pillars_any_enabled False → run_loop keeps pillars=None and every new
    tick-body branch is dead code."""
    assert eidos._pillars_any_enabled(Config()) is False
    cfg = Config()
    for flag in eidos._PILLARS_WIRED_FLAGS:
        assert getattr(cfg, flag) is False, f"{flag} defaults on — the wiring would go live"


@pytest.mark.parametrize("flag", eidos._PILLARS_WIRED_FLAGS)
def test_each_flag_alone_constructs_the_hub(flag):
    cfg = Config()
    setattr(cfg, flag, True)
    assert eidos._pillars_any_enabled(cfg) is True


def test_context_render_byte_identical_flags_off(tmp_path, monkeypatch):
    """THE identity assertion: the wired context.py and the pre-wiring context.py (extracted from
    git at the commit this pass built on) render byte-for-byte identical messages from the same
    synthetic inputs, with all flags off. Time is frozen and the host-telemetry alert probe is
    pinned (it reads live machine state, which is not context-assembly logic)."""
    try:
        old_src = subprocess.run(
            ["git", "show", f"{PRE_WIRING_COMMIT}:context.py"],
            cwd=_ROOT, capture_output=True, text=True, check=True).stdout
    except Exception:  # noqa: BLE001 - no git object (shallow clone / exported tree)
        pytest.skip("pre-wiring context.py not retrievable from git")

    import importlib.util
    old_path = tmp_path / "context_prewiring.py"
    old_path.write_text(old_src, encoding="utf-8")
    spec = importlib.util.spec_from_file_location("context_prewiring", old_path)
    old_mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(old_mod)

    cfg = _mk_config(tmp_path)
    _seed_workspace(cfg)

    fixed = 1_752_000_000.0
    st_local, st_gm = time.localtime(fixed), time.gmtime(fixed)
    monkeypatch.setattr(time, "time", lambda: fixed)
    monkeypatch.setattr(time, "localtime", lambda *a: st_local)
    monkeypatch.setattr(time, "gmtime", lambda *a: st_gm)
    for m in (context_mod, old_mod):
        monkeypatch.setattr(m, "generate_env_alerts", lambda c: "")

    kwargs = dict(tick_number=7, goal_start_time=fixed - 300.0, tension=2)
    new_msgs = context_mod.assemble_context(cfg, **kwargs)
    old_msgs = old_mod.assemble_context(cfg, **kwargs)
    assert new_msgs == old_msgs, "flags-off context render diverged from the pre-wiring code path"


def test_flags_off_runtime_imports_no_pillars_modules(tmp_path):
    """Import-graph guarantee, in a clean subprocess: importing eidos/context/glue, assembling a
    context, and running the glue outcome+settlement pass with all flags off pulls in ZERO pillars
    modules. (This is what 'the wiring adds no imports until a flag flips' means mechanically.)"""
    script = textwrap.dedent(f"""
        import sys
        sys.path.insert(0, {str(_ROOT)!r})
        from config import Config
        cfg = Config()
        cfg.workspace_dir = {str(tmp_path / "ws")!r}
        cfg.mock_mode = True
        cfg.workspace.mkdir(parents=True, exist_ok=True)
        cfg.state_dir.mkdir(parents=True, exist_ok=True)
        (cfg.workspace / "goal.md").write_text("probe the router", encoding="utf-8")
        import eidos, context, glue
        assert eidos._pillars_any_enabled(cfg) is False
        context.assemble_context(cfg, tick_number=1, goal_start_time=0.0)
        glue.record_outcome(cfg, success=True, tool="bash")
        glue.settle_bets(cfg, tick=1)
        glue.settle_predictions(cfg, tick=1)
        glue.surfaced_news(cfg)
        bad = [m for m in {PILLARS_MODULES!r} if m in sys.modules]
        print("PILLARS_IMPORTED=" + ",".join(bad))
    """)
    out = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True,
                         cwd=_ROOT, env={**__import__("os").environ, "PYTHONUTF8": "1"})
    assert out.returncode == 0, f"flags-off runtime crashed:\n{out.stderr}"
    assert "PILLARS_IMPORTED=\n" in out.stdout.replace("\r", "") or \
           out.stdout.strip().endswith("PILLARS_IMPORTED="), \
        f"flags-off runtime imported pillars modules: {out.stdout!r}"


def test_context_ignores_recall_block_when_flag_off(tmp_path):
    """The dark gate on the new assemble_context parameter: with the manager flag OFF, a non-empty
    pillars_recall_block is NOT consumed and the legacy blocks render as before."""
    cfg = _mk_config(tmp_path)
    _seed_workspace(cfg)
    msgs = context_mod.assemble_context(
        cfg, tick_number=2, goal_start_time=0.0,
        pillars_recall_block="## Recalled from memory (ranked relevance × strength)\n- [fact] x")
    joined = "\n".join(m["content"] for m in msgs)
    assert "Recalled from memory" not in joined


def test_glue_accessor_dark_by_default(tmp_path):
    assert glue.surfaced_news(Config()) == []


# ==================================================================================================
# 2. Flags-on smoke — each wired call site executes on a synthetic tick-shaped drive
# ==================================================================================================
@pytest.mark.parametrize("flag", eidos._PILLARS_WIRED_FLAGS)
def test_single_flag_tick_and_sleep_smoke(tmp_path, flag):
    """Each flag ON singly: construct the hub, drive a full synthetic tick through every call
    site, run the sleep window and the presence handler — no exception may escape."""
    cfg = _mk_config(tmp_path, **{flag: True})
    bus = organ_registry = None
    if flag == "pillars_salience_gate_enabled":
        from nervous.bus import NervousBus
        from nervous.organs import OrganRegistry
        bus, organ_registry = NervousBus(), OrganRegistry()
    hub = eidos._Pillars(cfg, bus=bus, organ_registry=organ_registry)
    persona = {"level": 1, "xp": 0}
    _drive_tick(hub, cfg, persona=persona)
    hub.sleep_window(tick=2, persona=persona,
                     observations=[{"tool": "bash", "success": True, "output": "hi"}])
    hub.on_presence()


def test_all_flags_on_coupled_tick(tmp_path):
    """The coupled smoke: every wired flag on together (mock mind only), one full tick + sleep
    window + presence pass through the single hub — the call-site ordering the loop uses."""
    cfg = _mk_config(tmp_path, **{f: True for f in eidos._PILLARS_WIRED_FLAGS})
    from nervous.bus import NervousBus
    from nervous.neuromod import Adenosine
    from nervous.organs import OrganRegistry
    nm = types.SimpleNamespace(adenosine=Adenosine(), arousal=0.4, valence=0.1)
    hub = eidos._Pillars(cfg, bus=NervousBus(), neuromod=nm, organ_registry=OrganRegistry())
    assert hub._live_llm() is None       # mock mode: the lazy mind is provably never constructed
    persona = {"level": 1, "xp": 0}
    _drive_tick(hub, cfg, tick=1, persona=persona)
    _drive_tick(hub, cfg, tick=2, success=False, persona=persona)
    nm.adenosine.accumulate(3.0)
    report = hub.sleep_window(tick=3, persona=persona,
                              observations=[{"tool": "bash", "success": True, "output": "hi"}])
    assert report is not None and report.results
    assert nm.adenosine.level_hours == 0.0     # run_sleep cleared it — the creature wakes rested
    hub.on_presence()


def test_memory_manager_recall_encode_and_bets(tmp_path):
    """2.2 + 2.3 wired together: encode → recall (slate recorded) → open_bets → glue settles the
    tick's bets mechanically from its own outcome record."""
    cfg = _mk_config(tmp_path, pillars_memory_engram_enabled=True,
                     pillars_memory_manager_enabled=True, pillars_bet_ledger_enabled=True)
    hub = eidos._Pillars(cfg)
    assert hub.manager is not None and hub.bets is not None
    hub.manager.encode("fact", "the router lives in rack four behind the probe panel")
    block = hub.recall_block(situation="", query="probe the router rack panel")
    assert block.startswith("## Recalled from memory") and hub.injected
    hub.open_bets(5)
    assert [b for b in hub.bets.all_bets() if b["status"] == "open" and b["tick"] == 5]
    glue.record_outcome(cfg, success=True, tool="bash")
    settled = glue.settle_bets(cfg, tick=5, action_tool="bash",
                               action_args={"cmd": "probe rack 4"}, ledger=hub.bets)
    assert settled
    assert not [b for b in hub.bets.all_bets() if b["status"] == "open" and b["tick"] == 5]


def test_encode_body_cleans_step_and_summary_shards(tmp_path):
    """2.2 encode hygiene: the engram body is what recall injects verbatim — a plan-list marker
    riding in on the situation step must be stripped, a marker-only step must drop the "While ,"
    shard entirely, and the summary shard must land one-line and word-bounded."""
    cfg = _mk_config(tmp_path, pillars_memory_engram_enabled=True,
                     pillars_memory_manager_enabled=True)
    hub = eidos._Pillars(cfg)
    hub.after_outcome(tick=1, tool="bash", args={}, success=True, fail_kind="",
                      situation="obj1|#. create a journal in the nest",
                      summary="wrote the\nfirst entry", event_text="", persona=None)
    hub.after_outcome(tick=2, tool="thought", args={}, success=False, fail_kind="timeout",
                      situation="obj1|#.", summary="", event_text="", persona=None)
    from engram import LongTermStore
    bodies = sorted(e.body for e in LongTermStore(cfg).load() if e.kind == "episode")
    assert bodies == ["While create a journal in the nest, `bash` succeeded. wrote the first entry",
                      "`thought` failed (timeout)."]


def test_context_manager_takeover_flag_on(tmp_path):
    """With the manager flag ON: the handed-in recall block renders and the legacy knowledge slice
    ('Possibly relevant from memory') stays retired from the durable blob."""
    cfg = _mk_config(tmp_path, pillars_memory_manager_enabled=True)
    _seed_workspace(cfg)
    block = "## Recalled from memory (ranked relevance × strength)\n- [fact] rack four"
    msgs = context_mod.assemble_context(cfg, tick_number=3, goal_start_time=0.0,
                                        pillars_recall_block=block)
    joined = "\n".join(m["content"] for m in msgs)
    assert "Recalled from memory" in joined and "rack four" in joined
    assert "Possibly relevant from memory" not in joined


def test_expectations_tool_awaiting_block_and_closure(tmp_path):
    """4.1 wired: the predict tool registers behind the flag, the awaiting strip renders in
    context, and the tick's closure pass settles a due prediction through glue only."""
    cfg = _mk_config(tmp_path, pillars_expectations_enabled=True)
    hub = eidos._Pillars(cfg)
    from tools import TOOLS
    assert "predict" in TOOLS
    import expectations
    led = expectations.ExpectationLedger(cfg)
    led.predict(statement="the probe finishes tonight", target="probe done",
                deadline=time.time() + 3600, confidence=0.8)
    _seed_workspace(cfg)
    msgs = context_mod.assemble_context(cfg, tick_number=4, goal_start_time=0.0)
    assert "AWAITING" in "\n".join(m["content"] for m in msgs)
    # a second, already-due bet closes on the tick's pass (deadline ground, never narration)
    led.predict(statement="the kettle boils by noon", target="kettle boiled",
                deadline=time.time() - 60, confidence=0.9)
    glue.record_outcome(cfg, success=True, tool="bash")
    hub.after_outcome(tick=4, tool="bash", args={}, success=True, fail_kind="",
                      situation="", summary="", event_text="", persona=None)
    opens = expectations.ExpectationLedger(cfg).open_predictions()
    assert all(p.deadline > time.time() for p in opens)   # the due bet is closed, the future one open


def test_quest_issue_render_adjudicate_and_reward(tmp_path):
    """5.1 wired end-to-end offline: propose → event-driven issue → window renders in context →
    the tick's adjudication passes it against typed stats → reward pays through award_xp WITH
    config threaded → the digestion counter resets."""
    cfg = _mk_config(tmp_path, pillars_quests_enabled=True)
    hub = eidos._Pillars(cfg)
    import quests
    hub.quests.propose(quests.Quest(
        id="wq1", directive="Log one adjudicated success.",
        success_criteria=quests.Criterion(path="persona.xp", op=">=", value=0),
        reward={"kind": quests.REWARD_XP, "amount": 7}))
    hub._issue_next({"level": 1, "xp": 0})
    assert hub.quests.store.active() is not None
    _seed_workspace(cfg)
    joined = "\n".join(m["content"] for m in context_mod.assemble_context(
        cfg, tick_number=5, goal_start_time=0.0))
    assert "SYSTEM" in joined and "QUEST" in joined       # the distinct terse register rendered
    persona = {"level": 1, "xp": 0}
    _drive_tick(hub, cfg, tick=5, persona=persona)
    assert hub.quests.store.active() is None              # criteria met → glue closed it
    assert persona["xp"] == 7                             # payout through the standard XP path
    assert hub.sleeps_since_close() == 0                  # closure reset the digestion counter


def _system_window_obs(cfg):
    from memory import read_recent_observations
    return [o for o in reversed(read_recent_observations(cfg, max_chars=100000, max_count=100))
            if o.get("tool") == "system_window"]     # oldest → newest


def test_quest_issuance_and_settlement_are_lived_turns(tmp_path):
    """5.1 experienced, not silent bookkeeping: an ACTUAL issuance writes exactly ONE
    system_window observation carrying the full directive; the close writes exactly ONE carrying
    the real paid amount; the history thread renders each as a VERBATIM user turn (no '[system]'
    wrapper — the System is neither the operator nor the platform). A no-op issue attempt and a
    hidden quest's issuance stay silent."""
    cfg = _mk_config(tmp_path, pillars_quests_enabled=True)
    hub = eidos._Pillars(cfg)
    import quests
    directive = "[SYSTEM] Log one adjudicated success."
    hub.quests.propose(quests.Quest(
        id="wq-lived", directive=directive,
        success_criteria=quests.Criterion(path="persona.xp", op=">=", value=1),
        reward={"kind": quests.REWARD_XP, "amount": 25}))
    hub._issue_next({"level": 1, "xp": 0}, tick=3)
    wins = _system_window_obs(cfg)
    assert len(wins) == 1                                 # exactly one issuance notice
    assert directive in wins[0]["output"]                 # the FULL directive rides in it
    hub._issue_next({"level": 1, "xp": 0}, tick=4)        # already active → silence, no repeat
    assert len(_system_window_obs(cfg)) == 1
    persona = {"level": 1, "xp": 1}
    _drive_tick(hub, cfg, tick=5, persona=persona)        # criteria met → glue closes + pays
    settles = [o for o in _system_window_obs(cfg) if "PASSED" in o.get("output", "")]
    assert len(settles) == 1                              # exactly one settlement notice
    assert "PAID 25 XP" in settles[0]["output"]           # the REAL amount award_xp paid
    assert persona["xp"] == 1 + 25
    thread = context_mod._build_history_thread(cfg)
    issue = [m for m in thread if "QUEST ISSUED" in m["content"]]
    settle = [m for m in thread if "QUEST PASSED" in m["content"]]
    assert len(issue) == 1 and len(settle) == 1
    assert issue[0]["role"] == "user" and settle[0]["role"] == "user"
    assert issue[0]["content"] == wins[0]["output"]       # verbatim — no wrapper prefix
    assert settle[0]["content"] == "[SYSTEM] QUEST PASSED — PAID 25 XP."
    # A HIDDEN quest issues in silence — achievements announce only on completion (§7).
    hub.quests.propose(quests.Quest(
        id="wq-hidden", directive="Unseen.", hidden=True,
        success_criteria=quests.Criterion(path="persona.xp", op=">=", value=10**9),
        reward={"kind": quests.REWARD_XP, "amount": 5}))
    hub._set_sleeps_since_close(1)
    hub._issue_next(persona, tick=6)
    assert hub.quests.store.active() is not None          # it DID issue…
    assert len(_system_window_obs(cfg)) == 2              # …but wrote no issuance notice


def test_quest_expiry_settles_on_screen_nothing_paid(tmp_path):
    """The not-coddled close is ALSO lived: an expired quest writes ONE system_window settlement
    stating nothing was paid — and no XP moves."""
    cfg = _mk_config(tmp_path, pillars_quests_enabled=True)
    hub = eidos._Pillars(cfg)
    import quests
    hub.quests.propose(quests.Quest(
        id="wq-exp", directive="Unreachable.",
        success_criteria=quests.Criterion(path="persona.xp", op=">=", value=10**9),
        reward={"kind": quests.REWARD_XP, "amount": 9},
        expiry_ts=time.time() - 1))
    hub._issue_next({"level": 1, "xp": 0}, tick=2)
    persona = {"level": 1, "xp": 0}
    _drive_tick(hub, cfg, tick=3, persona=persona)        # past expiry, unmet → glue expires it
    settles = [o for o in _system_window_obs(cfg) if "EXPIRED" in o.get("output", "")]
    assert len(settles) == 1
    assert "NOTHING PAID" in settles[0]["output"]
    assert persona["xp"] == 0


def test_sleep_window_advances_gates_and_cadence(tmp_path):
    """2.4 + 4.3 + 5.1 at the boundary: a completed sleep clears adenosine, advances the mastery
    gate's sleep counter, and advances the quest digestion counter."""
    cfg = _mk_config(tmp_path, pillars_sleep_engine_enabled=True,
                     pillars_memory_engram_enabled=True, pillars_mastery_gates_enabled=True,
                     pillars_quests_enabled=True)
    from nervous.neuromod import Adenosine
    nm = types.SimpleNamespace(adenosine=Adenosine())
    hub = eidos._Pillars(cfg, neuromod=nm)
    nm.adenosine.accumulate(5.0)
    report = hub.sleep_window(tick=6, persona={"level": 1, "xp": 0}, observations=[])
    assert report is not None and report.results
    assert nm.adenosine.level_hours == 0.0
    import level_gates
    assert level_gates.GateState(cfg).sleeps_since_level == 1
    assert hub.sleeps_since_close() == 2     # newborn floor 1 + this completed sleep


def test_news_three_sources_presence_gate_and_accessor(tmp_path):
    """4.4 wired: the anomaly source ingests on a repeated dead end; nothing surfaces before
    presence; the presence handler surfaces + snapshots; glue.surfaced_news serves the snapshot."""
    cfg = _mk_config(tmp_path, pillars_news_enabled=True)
    hub = eidos._Pillars(cfg)
    for t in (1, 2, 3):     # the same failure signature three ticks in a row = an anomaly
        glue.record_outcome(cfg, success=False, fail_kind="exec",
                            signature="sig-dead-end", tool="bash")
    hub.after_outcome(tick=3, tool="bash", args={}, success=False, fail_kind="exec",
                      situation="", summary="", event_text="", persona=None)
    assert hub.news.items(), "the anomaly never reached the queue"
    assert glue.surfaced_news(cfg) == []                  # nothing surfaced before presence
    assert hub.on_presence()                              # the hold opens the gate
    got = glue.surfaced_news(cfg)
    assert got and "anomaly" in got[0]["body"]


def test_learning_progress_fed_from_adjudicated_outcomes(tmp_path):
    cfg = _mk_config(tmp_path, pillars_learning_xp_enabled=True)
    hub = eidos._Pillars(cfg)
    glue.record_outcome(cfg, success=True, tool="bash")
    hub.after_outcome(tick=1, tool="bash", args={}, success=True, fail_kind="",
                      situation="obj9|probe", summary="", event_text="", persona=None)
    import learning_progress as lp
    assert hub.tracker.series(lp.domain_key("obj9", "bash")) == [0.0]


def test_mastery_gate_holds_the_level(tmp_path):
    """4.3 wired: a config-less award path recomputing level-from-XP is reverted each tick; the
    XP-floor crossing runs can_level, which (rightly) refuses without trusted-skill evidence."""
    cfg = _mk_config(tmp_path, pillars_mastery_gates_enabled=True)
    hub = eidos._Pillars(cfg)
    persona = {"level": 1, "xp": 10}
    hub.after_outcome(tick=1, tool="bash", args={}, success=True, fail_kind="",
                      situation="", summary="", event_text="", persona=persona)  # snapshot = 1
    persona["xp"] = 100_000
    persona["level"] = 45          # what compute_level would have done without the gate
    hub.after_outcome(tick=2, tool="bash", args={}, success=True, fail_kind="",
                      situation="", summary="", event_text="", persona=persona)
    assert persona["level"] == 1   # only apply_level_up moves the level, and the gate said no


def test_salience_gate_registered_and_hears_relevance(tmp_path):
    """1.3 wired: the gate registers with the 1.1 registry; pre_tick publishes the core's focus
    terms as a relevance_set and runs the registry's pre_tick phase, and the gate hears it."""
    cfg = _mk_config(tmp_path, pillars_salience_gate_enabled=True)
    from nervous.bus import NervousBus
    from nervous.organs import OrganRegistry
    hub = eidos._Pillars(cfg, bus=NervousBus(), organ_registry=OrganRegistry())
    assert hub.salience is not None
    from memory import write_plan
    write_plan(cfg, "# Plan\n1. probe the router\n")
    hub.pre_tick(1)
    assert hub.salience._relevance, "the published relevance_set never reached the gate"


def test_administrator_event_driven_with_mock_seam(tmp_path):
    """5.2 wired: a wake event drives one check-in through the INJECTED mock mind (the test seam);
    a non-wake event drives none; and without injection the lazy mind stays None under mock mode
    (no test can ever reach a live model)."""
    cfg = _mk_config(tmp_path, pillars_administrator_enabled=True)
    hub = eidos._Pillars(cfg)
    assert hub._live_llm() is None       # the mock seam's ground state
    calls = []

    def _mock(messages, grammar=None):
        calls.append((len(messages), bool(grammar)))
        return "not the grammar's json"   # malformed → dropped-with-log, never raises

    hub.llm = _mock
    hub._event("sleep_complete", {"level": 1, "xp": 0})
    assert len(calls) == 1
    hub._event("not_a_wake_event", {"level": 1, "xp": 0})
    assert len(calls) == 1               # non-wake events never check in (ARCH #1: no timers)


def test_raising_subsystem_is_swallowed_never_propagates(tmp_path, monkeypatch):
    """I5: a subsystem that raises at any wired call site is logged and swallowed — the drive
    completes and the other subsystems still run."""
    cfg = _mk_config(tmp_path, pillars_memory_engram_enabled=True,
                     pillars_memory_manager_enabled=True, pillars_quests_enabled=True,
                     pillars_learning_xp_enabled=True)
    hub = eidos._Pillars(cfg)

    def _boom(*a, **k):
        raise RuntimeError("boom")

    monkeypatch.setattr(hub.manager, "recall", _boom)
    assert hub.recall_block(situation="s", query="q") == ""     # swallowed, empty slate
    monkeypatch.setattr(hub.quests.store, "active", _boom)
    persona = {"level": 1, "xp": 0}
    _drive_tick(hub, cfg, persona=persona)                      # must not raise
    import learning_progress as lp
    assert hub.tracker.series(lp.domain_key("obj1", "bash"))    # the tracker still ran after it
