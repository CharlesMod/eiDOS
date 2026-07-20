"""Tests for wisdom_curve.py — the experience-curve instrument (WISDOM_PLAN §4).

ALL LLM calls are MOCKED (never a real model, never the live box). Covers:
  - battery loads + is well-formed (mechanical scorers, unique ids)
  - each scorer type verified
  - WIS6 refusal when eidos appears running (fixture pidfile / fresh heartbeat)
  - wise-arm fixture copies stores READ-ONLY (original bytes + mtimes untouched)
  - result row shape + bounded append
  - immutability guard (recorded hash mismatch refuses)
  - dry-run makes zero LLM calls
"""

import json
import os
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import wisdom_curve as wc
from config import Config


# ── battery well-formedness ────────────────────────────────────────────────

def test_battery_loads_and_is_well_formed():
    tasks = wc.load_battery("v1")
    assert len(tasks) >= 20, "battery should have ~20 meaty tasks"
    ids = [t["id"] for t in tasks]
    assert len(ids) == len(set(ids)), "ids must be unique"
    domains = {t["domain"] for t in tasks}
    assert domains == {"ops", "subsystems", "recall", "procedure"}, \
        "all four §4 domains must be represented"
    for t in tasks:
        assert t["scorer"]["type"] in wc.SCORER_TYPES, "every scorer must be a known mechanical type"
        assert t["prompt"].strip()


def test_all_four_domains_have_multiple_tasks():
    tasks = wc.load_battery("v1")
    counts = {}
    for t in tasks:
        counts[t["domain"]] = counts.get(t["domain"], 0) + 1
    for d in ("ops", "subsystems", "recall", "procedure"):
        assert counts.get(d, 0) >= 2, f"domain {d} is thin ({counts.get(d)})"


def _write_battery(tmp_path, lines, version="vX"):
    """Point wc.BATTERY_DIR at a temp dir with a custom battery file."""
    d = tmp_path / "battery"
    d.mkdir(exist_ok=True)
    (d / f"{version}.jsonl").write_text("\n".join(json.dumps(x) for x in lines) + "\n",
                                        encoding="utf-8")
    return d


def test_malformed_battery_rejected(tmp_path, monkeypatch):
    d = _write_battery(tmp_path, [
        {"id": "a", "domain": "ops", "prompt": "q", "scorer": {"type": "exact", "answer": "x"}},
        {"id": "a", "domain": "ops", "prompt": "q2", "scorer": {"type": "exact", "answer": "y"}},
    ])
    monkeypatch.setattr(wc, "BATTERY_DIR", d)
    with pytest.raises(wc.BatteryError, match="duplicate id"):
        wc.load_battery("vX")


def test_nonmechanical_scorer_rejected(tmp_path, monkeypatch):
    # A scorer type not in SCORER_TYPES (e.g. a would-be "llm_judge") must be refused.
    d = _write_battery(tmp_path, [
        {"id": "a", "domain": "ops", "prompt": "q", "scorer": {"type": "llm_judge"}},
    ])
    monkeypatch.setattr(wc, "BATTERY_DIR", d)
    with pytest.raises(wc.BatteryError, match="scorer.type"):
        wc.load_battery("vX")


def test_scorer_missing_required_fields_rejected(tmp_path, monkeypatch):
    d = _write_battery(tmp_path, [
        {"id": "a", "domain": "ops", "prompt": "q", "scorer": {"type": "regex"}},  # no pattern
    ])
    monkeypatch.setattr(wc, "BATTERY_DIR", d)
    with pytest.raises(wc.BatteryError, match="pattern"):
        wc.load_battery("vX")


# ── scorer types, each verified ─────────────────────────────────────────────

def test_scorer_exact():
    s = {"type": "exact", "answers": ["gemma4-12b", "gemma-4-12b"]}
    assert wc.score_answer(s, "gemma4-12b") == 1.0
    assert wc.score_answer(s, "The model is gemma-4-12b.") == 1.0   # substring accepted
    assert wc.score_answer(s, "qwen27b") == 0.0


def test_scorer_regex():
    s = {"type": "regex", "pattern": r"\b8099\b"}
    assert wc.score_answer(s, "port 8099 is the dashboard") == 1.0
    assert wc.score_answer(s, "port 8080") == 0.0


def test_scorer_claim_partial_and_exclude():
    s = {"type": "claim", "must_include": ["systemctl", "respawn"], "must_exclude": ["silent"]}
    assert wc.score_answer(s, "use systemctl because it respawns") == 1.0
    assert wc.score_answer(s, "use systemctl") == 0.5                       # partial credit
    assert wc.score_answer(s, "systemctl respawn but silent no-op") == 0.0  # excluded phrase zeros it


def test_scorer_action_signature():
    s = {"type": "action_signature", "tool": "remember"}
    good = '<tool>remember</tool><args>{"note": "check the garden"}</args>'
    assert wc.score_answer(s, good) == 1.0
    assert wc.score_answer(s, '<tool>note_append</tool><args>{}</args>') == 0.0


def test_scorer_action_signature_args_pattern():
    s = {"type": "action_signature", "tool": "objective_done", "args_pattern": "obj_17"}
    assert wc.score_answer(s, '<tool>objective_done</tool><args>{"id": "obj_17"}</args>') == 1.0
    assert wc.score_answer(s, '<tool>objective_done</tool><args>{"id": "obj_99"}</args>') == 0.0


def test_every_v1_task_scorer_is_exercisable():
    """Each real task's scorer must produce a valid [0,1] score on both a matching and empty answer
    — proving the scorer is fully mechanical (decides alone, no model judge)."""
    for t in wc.load_battery("v1"):
        empty = wc.score_answer(t["scorer"], "")
        assert 0.0 <= empty <= 1.0


# ── WIS6 refusal ────────────────────────────────────────────────────────────

def _base_config(tmp_path):
    cfg = Config()
    cfg.workspace_dir = str(tmp_path / "workspace")
    (tmp_path / "workspace" / "state").mkdir(parents=True, exist_ok=True)
    cfg.llm_url = "http://127.0.0.1:8080"
    cfg.llm_model = "gemma4-12b"
    return cfg


def test_refuses_when_pidfile_alive(tmp_path):
    cfg = _base_config(tmp_path)
    (cfg.workspace / "eidos.pid").write_text(str(os.getpid()))  # our own pid IS alive
    running, reason = wc.eidos_running(cfg)
    assert running is True
    assert "alive" in reason


def test_refuses_when_heartbeat_fresh(tmp_path):
    cfg = _base_config(tmp_path)
    (cfg.workspace / "eidos.pid").write_text("999999999")       # dead pid
    (cfg.workspace / "heartbeat.json").write_text(json.dumps({"ts": time.time()}))
    running, reason = wc.eidos_running(cfg)
    assert running is True
    assert "heartbeat" in reason


def test_allows_when_stopped(tmp_path):
    cfg = _base_config(tmp_path)
    (cfg.workspace / "eidos.pid").write_text("999999999")       # dead pid
    (cfg.workspace / "heartbeat.json").write_text(json.dumps({"ts": time.time() - 10_000}))
    running, _ = wc.eidos_running(cfg)
    assert running is False


def test_run_curve_refuses_when_running(tmp_path, monkeypatch):
    cfg = _base_config(tmp_path)
    (cfg.workspace / "eidos.pid").write_text(str(os.getpid()))
    monkeypatch.setattr("llm.complete",
                        lambda *a, **k: pytest.fail("LLM called despite refusal"))
    with pytest.raises(SystemExit, match="REFUSED"):
        wc.run_curve(cfg, arms=["naive12"], version="v1", timeout=5,
                     dry_run=False, verbose=False)


# ── wise-arm reads stores READ-ONLY ─────────────────────────────────────────

def _seed_live_stores(ws: Path):
    ws.mkdir(parents=True, exist_ok=True)
    (ws / "engram_episodic.jsonl").write_text('{"id":"e1","body":"ran restart -> worked"}\n')
    (ws / "creature.json").write_text(json.dumps({"born_ts": time.time() - 5 * 86400}))
    kdir = ws / "knowledge"
    kdir.mkdir(exist_ok=True)
    (kdir / "index.json").write_text('{"n": 1}')


def test_wise_arm_copies_stores_readonly(tmp_path):
    live = tmp_path / "workspace"
    _seed_live_stores(live)
    # Fingerprint the originals.
    before = {}
    for p in live.rglob("*"):
        if p.is_file():
            st = p.stat()
            before[str(p)] = (st.st_size, st.st_mtime_ns, p.read_bytes())

    dest = tmp_path / "wise_fixture"
    copied = wc.copy_wisdom_stores(live, dest)
    assert "engram_episodic.jsonl" in copied
    assert "knowledge" in copied
    assert (dest / "engram_episodic.jsonl").exists()
    assert (dest / "knowledge" / "index.json").exists()

    # Originals UNTOUCHED — size, mtime, bytes all identical.
    for p in live.rglob("*"):
        if p.is_file():
            st = p.stat()
            size0, mtime0, bytes0 = before[str(p)]
            assert st.st_size == size0
            assert st.st_mtime_ns == mtime0
            assert p.read_bytes() == bytes0


def test_wise_arm_copy_is_independent(tmp_path):
    """Mutating the wise-arm COPY must not reach back to the original."""
    live = tmp_path / "workspace"
    _seed_live_stores(live)
    dest = tmp_path / "wise_fixture"
    wc.copy_wisdom_stores(live, dest)
    (dest / "engram_episodic.jsonl").write_text("MUTATED\n")
    assert (live / "engram_episodic.jsonl").read_text() != "MUTATED\n"


# ── result shape + bounded append ───────────────────────────────────────────

def _mock_complete_factory(answer_map=None, default=""):
    """Return a fake llm.complete that answers from a {id-substring: answer} map by matching the
    prompt, else `default`. Records call count on the returned function's `.calls`."""
    answer_map = answer_map or {}

    def fake(messages, config, *a, **k):
        fake.calls += 1
        prompt = messages[-1]["content"]
        for key, val in answer_map.items():
            if key in prompt:
                return val
        return default
    fake.calls = 0
    return fake


def test_run_curve_result_shape_and_scores(tmp_path, monkeypatch):
    cfg = _base_config(tmp_path)
    _seed_live_stores(cfg.workspace)
    (cfg.workspace / "eidos.pid").write_text("999999999")   # not running

    # Answer a couple of prompts correctly so scores aren't all zero.
    fake = _mock_complete_factory({
        "dashboard": "8099",
        "resident house-mind": "gemma4-12b",
    }, default="unknown")
    monkeypatch.setattr("llm.complete", fake)

    row = wc.run_curve(cfg, arms=["naive12"], version="v1", timeout=5,
                       dry_run=False, verbose=False)

    assert row["battery_version"] == "v1"
    assert row["battery_sha256"] == wc.battery_sha256("v1")
    assert row["creature_age_days"] == pytest.approx(5.0, abs=0.1)
    assert "naive12" in row["arms"]
    arm = row["arms"]["naive12"]
    assert arm["model"] == "gemma4-12b"
    assert arm["score"] >= 1.0                    # at least the two we answered
    assert set(arm["per_domain"]) <= {"ops", "subsystems", "recall", "procedure"}
    # One call per task (serial) + one restore "touch" of the resident model at exit (WIS6).
    assert fake.calls == arm["max"] + 1

    # Row persisted.
    results = cfg.workspace / "state" / "wisdom_curve.jsonl"
    assert results.exists()
    lines = [l for l in results.read_text().splitlines() if l.strip()]
    assert len(lines) == 1
    assert json.loads(lines[0])["battery_version"] == "v1"


def test_bounded_append(tmp_path):
    state = tmp_path / "state"
    for i in range(wc.RESULTS_MAX_ROWS + 25):
        wc.append_result(state, {"i": i, "battery_version": "v1"}, max_rows=wc.RESULTS_MAX_ROWS)
    lines = [l for l in (state / "wisdom_curve.jsonl").read_text().splitlines() if l.strip()]
    assert len(lines) == wc.RESULTS_MAX_ROWS
    # Kept the most RECENT rows.
    assert json.loads(lines[-1])["i"] == wc.RESULTS_MAX_ROWS + 24


# ── immutability guard ──────────────────────────────────────────────────────

def test_immutability_guard_refuses_changed_battery(tmp_path):
    state = tmp_path / "state"
    # A prior result recorded a DIFFERENT hash for v1 than the file now has.
    wc.append_result(state, {
        "battery_version": "v1", "battery_sha256": "deadbeef" * 8,
    })
    with pytest.raises(wc.BatteryError, match="IMMUTABLE|CHANGED"):
        wc.assert_battery_immutable("v1", state)


def test_immutability_guard_allows_matching_hash(tmp_path):
    state = tmp_path / "state"
    wc.append_result(state, {
        "battery_version": "v1", "battery_sha256": wc.battery_sha256("v1"),
    })
    wc.assert_battery_immutable("v1", state)  # must not raise


def test_immutability_guard_allows_first_run(tmp_path):
    state = tmp_path / "state"
    wc.assert_battery_immutable("v1", state)  # no prior results → allowed


def test_run_curve_refuses_changed_battery(tmp_path, monkeypatch):
    cfg = _base_config(tmp_path)
    (cfg.workspace / "eidos.pid").write_text("999999999")  # not running
    state = cfg.workspace / "state"
    wc.append_result(state, {"battery_version": "v1", "battery_sha256": "cafe" * 16})
    monkeypatch.setattr("llm.complete",
                        lambda *a, **k: pytest.fail("LLM called despite immutability refusal"))
    with pytest.raises(wc.BatteryError, match="IMMUTABLE|CHANGED"):
        wc.run_curve(cfg, arms=["naive12"], version="v1", timeout=5,
                     dry_run=False, verbose=False)


# ── dry-run makes no LLM calls ──────────────────────────────────────────────

def test_dry_run_makes_no_llm_calls(tmp_path, monkeypatch):
    cfg = _base_config(tmp_path)
    # Even if the loop looks running, dry-run must not refuse and must not call the model.
    (cfg.workspace / "eidos.pid").write_text(str(os.getpid()))
    monkeypatch.setattr("llm.complete",
                        lambda *a, **k: pytest.fail("dry-run must not call the LLM"))
    out = wc.run_curve(cfg, arms=["naive12", "wise12", "naive27"], version="v1",
                       timeout=5, dry_run=True, verbose=False)
    assert out["dry_run"] is True
    assert out["tasks"] >= 20
    # No results file written by a dry run.
    assert not (cfg.workspace / "state" / "wisdom_curve.jsonl").exists()


def test_dry_run_reports_arm_models(tmp_path):
    cfg = _base_config(tmp_path)
    out = wc.run_curve(cfg, arms=["naive27"], version="v1", timeout=5,
                       dry_run=True, verbose=False)
    assert out["arms"] == ["naive27"]
    # naive27 resolves to qwen27b by name.
    assert wc._resolve_model("naive27", cfg) == "qwen27b"
    assert wc._resolve_model("naive12", cfg) == "gemma4-12b"
    assert wc._resolve_model("wise12", cfg) == "gemma4-12b"


# ── restore discipline ──────────────────────────────────────────────────────

def test_touch_resident_model_uses_resident_name(tmp_path, monkeypatch):
    cfg = _base_config(tmp_path)
    seen = {}

    def fake_complete(messages, config, *a, **k):
        seen["model"] = config.llm_model
        return "ok"
    monkeypatch.setattr("llm.complete", fake_complete)
    assert wc.touch_resident_model(cfg) is True
    assert seen["model"] == "gemma4-12b"   # the RESIDENT mind, not qwen


def test_run_curve_restores_after_run(tmp_path, monkeypatch):
    cfg = _base_config(tmp_path)
    _seed_live_stores(cfg.workspace)
    (cfg.workspace / "eidos.pid").write_text("999999999")
    calls = {"models": []}

    def fake_complete(messages, config, *a, **k):
        calls["models"].append(config.llm_model)
        return "unknown"
    monkeypatch.setattr("llm.complete", fake_complete)

    row = wc.run_curve(cfg, arms=["naive27"], version="v1", timeout=5,
                       dry_run=False, verbose=False)
    assert row["_restored"] is True
    # The LAST model touched must be the resident gemma (restore), even though the arm ran qwen.
    assert calls["models"][-1] == "gemma4-12b"
    assert "qwen27b" in calls["models"]
