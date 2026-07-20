"""growth.py — the Growth Panel aggregator (the missing meta-capability).

WHY THIS EXISTS:
    PILLARS_PLAN.md §10 defines ten behavioral dream-tests (D1–D10) — the ONLY place
    behaviors are named. The plan calls *measuring* them "the missing meta-capability":
    the harness that would compute the numbers was never built, so a functional review
    had to hand-assemble every figure by hand. This module is that harness.

    It is strictly READ-ONLY. It reads the stores the creature already writes — under
    config.workspace and config.state_dir — and distils each dream-test into a metric.
    It never writes, never mutates, never touches the live loop. The dashboard exposes
    it at GET /api/growth (unauthenticated by design, like the other read panels).

THE HONESTY CONTRACT (PILLARS §0, ARCH #4 — "the system never lies"):
    Every metric is a dict {value, status, basis}:
      status = "measured"     → the number is computed from a real store,
               "unmeasured"   → the store to compute it doesn't exist yet / isn't
                                trend-capable — we report the honest gap, NEVER a fake number,
               "human-judged" → the dream-test (D1/D4/D6/D7/D9) is a human observation;
                                we surface whatever supporting counts exist but pass no verdict.
      basis  = the file(s) the value was derived from (auditability).

    Fail-open everywhere: a missing or corrupt store yields status "unmeasured" (or a
    zero count with an honest basis), NEVER an exception. A half-measured panel is worth
    more to the operator than a crashed one.
"""
from __future__ import annotations

import json
from collections import Counter
from pathlib import Path


# ---------------------------------------------------------------------------
# Fail-open readers (mirror dashboard._read_json / _tail_jsonl conventions).
# ---------------------------------------------------------------------------

def _read_json(path: Path):
    """Load a JSON file, returning None on any failure (missing / corrupt / OSError)."""
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError, ValueError):
        return None


def _iter_jsonl(path: Path):
    """Yield parsed records from a JSONL file, skipping unparseable lines. Empty on any
    file-level failure. Whole-file read then per-line parse (the stores here are small;
    the engram store is the largest and is still bounded)."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except (FileNotFoundError, OSError):
        return
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            yield json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue


def _skills_manifest_path(config) -> Path:
    """Skills manifest lives at workspace/skills/_index.json (skills._skills_dir convention)."""
    return config.workspace / "skills" / "_index.json"


def _metric(value, status: str, basis: str) -> dict:
    """The one metric shape. status ∈ {measured, unmeasured, human-judged}."""
    return {"value": value, "status": status, "basis": basis}


# ---------------------------------------------------------------------------
# Per-metric distillers. Each takes config, returns a metric dict. Each is
# independently fail-open.
# ---------------------------------------------------------------------------

def _engram_error_signatures(config) -> list:
    """The failure-signature bag for D2: bodies of kind=="error" engrams in the long-term
    store. A signature recurring across time is a repeated mistake."""
    path = config.knowledge_dir / "engram_longterm.jsonl"
    sigs = []
    for rec in _iter_jsonl(path):
        if not isinstance(rec, dict) or rec.get("kind") != "error":
            continue
        body = str(rec.get("body") or "").strip()
        if body:
            sigs.append((body, rec.get("created") or ""))
    return sigs


def d2_repeat_failure(config) -> dict:
    """D2 — 'It doesn't repeat June's mistake.' Repeat-failure rate: the fraction of error
    signatures that recur (a normalized body appearing 2+ times). A high rate over a long
    window is the creature re-making the same mistake; a declining rate is the target.

    Basis: knowledge/engram_longterm.jsonl (kind=="error"), fallen back to the observations
    archives for failed tool calls when no error engrams exist."""
    sigs = _engram_error_signatures(config)
    basis = "knowledge/engram_longterm.jsonl(kind=error)"
    if not sigs:
        # Fallback: recurring failed-tool signatures from the observations archives.
        archive_fails = Counter()
        n_arch = 0
        for arch in sorted(config.state_dir.glob("observations_archive_*.jsonl")):
            n_arch += 1
            for rec in _iter_jsonl(arch):
                if isinstance(rec, dict) and rec.get("success") is False:
                    key = str(rec.get("tool") or "") + "|" + str(rec.get("output") or "")[:80]
                    archive_fails[key] += 1
        if not archive_fails:
            return _metric(None, "unmeasured",
                           "no error engrams and no failed obs in state/observations_archive_*.jsonl")
        total = sum(archive_fails.values())
        repeated = {k: c for k, c in archive_fails.items() if c > 1}
        repeat_events = sum(repeated.values())
        rate = round(repeat_events / total, 4) if total else 0.0
        return _metric(
            {"repeat_failure_rate": rate, "distinct_signatures": len(archive_fails),
             "total_failures": total, "repeated_signatures": len(repeated)},
            "measured",
            f"state/observations_archive_*.jsonl ({n_arch} archive(s), tool+output signature)")
    # Normalize each error body to collapse near-duplicates (lowercase, whitespace-squeezed).
    norm = Counter()
    for body, _created in sigs:
        key = " ".join(body.lower().split())[:120]
        norm[key] += 1
    total = sum(norm.values())
    repeated = {k: c for k, c in norm.items() if c > 1}
    repeat_events = sum(repeated.values())
    rate = round(repeat_events / total, 4) if total else 0.0
    return _metric(
        {"repeat_failure_rate": rate, "distinct_signatures": len(norm),
         "total_failures": total, "repeated_signatures": len(repeated)},
        "measured", basis)


def d3_wakes_up_smarter(config) -> dict:
    """D3 — 'It wakes up smarter.' Needs post-sleep vs pre-sleep held-out recall/prediction
    quality. The dream snapshots (workspace/snapshots/dream_*.md) are prose summaries with no
    structured pre/post metric, and state/calibration.json holds only a *current* Brier snapshot
    with no per-cycle history — so a genuine pre/post *delta* is not computable from existing
    stores. We honestly report it as unmeasured, surfacing the current calibration and dream
    count as context (never inventing a delta)."""
    cal = _read_json(config.state_dir / "calibration.json") or {}
    gen = cal.get("general") if isinstance(cal, dict) else None
    brier = gen.get("brier") if isinstance(gen, dict) else None
    n_cal = gen.get("n") if isinstance(gen, dict) else None
    try:
        n_dreams = sum(1 for _ in config.snapshots_dir.glob("dream_*.md"))
    except OSError:
        n_dreams = 0
    return _metric(
        {"current_brier": brier, "calibration_n": n_cal, "dream_cycles": n_dreams,
         "note": "pre/post delta needs per-cycle calibration history; not stored"},
        "unmeasured",
        "state/calibration.json (single snapshot, no history) + snapshots/dream_*.md")


def d5_reuse_vs_authorship(config) -> dict:
    """D5 — 'It reuses its own hands.' Reuse rate vs authorship rate from the skills manifest
    (workspace/skills/_index.json). Authorship = number of live skills authored. Reuse =
    total invocations across them. reuse_ratio = total_invocations / skills_count (uses per
    authored hand); a value >1 means it leans on existing skills more than it mints new ones —
    the target within 4 weeks of S2.

    Basis: workspace/skills/_index.json."""
    manifest = _read_json(_skills_manifest_path(config))
    if not isinstance(manifest, dict):
        return _metric(None, "unmeasured", "workspace/skills/_index.json missing/corrupt")
    skills = manifest.get("skills")
    if not isinstance(skills, dict) or not skills:
        return _metric({"skills_authored": 0, "total_invocations": 0, "reuse_ratio": 0.0},
                       "measured", "workspace/skills/_index.json (no skills authored yet)")
    authored = len(skills)
    total_inv = 0
    total_succ = 0
    for ent in skills.values():
        if isinstance(ent, dict):
            total_inv += int(ent.get("invocations") or 0)
            total_succ += int(ent.get("successes") or 0)
    reuse_ratio = round(total_inv / authored, 4) if authored else 0.0
    return _metric(
        {"skills_authored": authored, "total_invocations": total_inv,
         "total_successes": total_succ, "reuse_ratio": reuse_ratio},
        "measured", "workspace/skills/_index.json")


def d8_nothing_freezes(config) -> dict:
    """D8 — 'Nothing freezes the mind.' Watchdog event count + stale-heartbeat restarts from
    state/watchdog_events.log (the dashboard watchdog appends one line per rollback/standdown/
    stale-heartbeat restart). Zero freezes attributable to skills/organs is the pass state.

    Basis: state/watchdog_events.log."""
    path = config.state_dir / "watchdog_events.log"
    try:
        lines = [ln for ln in path.read_text(encoding="utf-8", errors="replace").splitlines()
                 if ln.strip()]
    except (FileNotFoundError, OSError):
        return _metric({"watchdog_events": 0, "stale_heartbeat_restarts": 0, "rollback_events": 0},
                       "measured", "state/watchdog_events.log (absent → zero events)")
    stale = sum(1 for ln in lines if "stale" in ln.lower() or "heartbeat" in ln.lower())
    rollbacks = sum(1 for ln in lines if "rollback" in ln.lower() or "restore" in ln.lower())
    return _metric(
        {"watchdog_events": len(lines), "stale_heartbeat_restarts": stale,
         "rollback_events": rollbacks},
        "measured", "state/watchdog_events.log")


def d10_rises_to_voice(config) -> dict:
    """D10 — 'It rises to the voice.' Quest pass/fail/expired counts by tier from
    workspace/quests.jsonl, plus the current active quest and sleeps_since_close from
    state/quest_cadence.json. Completion rate holding in-band while the difficulty tier
    climbs is the pass signal.

    Basis: workspace/quests.jsonl + state/quest_cadence.json."""
    by_tier = {}          # tier -> Counter of states
    active_id = None
    total_states = Counter()
    seen_any = False
    for rec in _iter_jsonl(config.workspace / "quests.jsonl"):
        if not isinstance(rec, dict):
            continue
        seen_any = True
        tier = rec.get("tier")
        state = rec.get("state") or "unknown"
        total_states[state] += 1
        tk = str(tier)
        by_tier.setdefault(tk, Counter())[state] += 1
        if state == "active":
            active_id = rec.get("id")
    cadence = _read_json(config.state_dir / "quest_cadence.json") or {}
    sleeps_since_close = cadence.get("sleeps_since_close") if isinstance(cadence, dict) else None
    # Completion rate = passed / (passed+failed+expired) across terminal quests.
    terminal = (total_states.get("passed", 0) + total_states.get("failed", 0)
                + total_states.get("expired", 0))
    completion_rate = round(total_states.get("passed", 0) / terminal, 4) if terminal else None
    if not seen_any:
        return _metric(
            {"by_tier": {}, "totals": {}, "active": active_id,
             "sleeps_since_close": sleeps_since_close, "completion_rate": completion_rate},
            "unmeasured", "workspace/quests.jsonl missing/empty")
    return _metric(
        {"by_tier": {t: dict(c) for t, c in by_tier.items()},
         "totals": dict(total_states),
         "active": active_id,
         "sleeps_since_close": sleeps_since_close,
         "completion_rate": completion_rate},
        "measured", "workspace/quests.jsonl + state/quest_cadence.json")


def goal_horizon_trend(config) -> dict:
    """Goal-horizon KPI trend from state/goal_horizon_stats.json (mean / max / recent / causes).
    Not a dream-test but the review's headline planning-depth KPI."""
    stats = _read_json(config.state_dir / "goal_horizon_stats.json")
    if not isinstance(stats, dict):
        return _metric(None, "unmeasured", "state/goal_horizon_stats.json missing/corrupt")
    return _metric(
        {"mean": stats.get("mean"), "max": stats.get("max"),
         "samples": stats.get("samples"), "recent": stats.get("recent"),
         "by_cause": stats.get("by_cause"), "last": stats.get("last")},
        "measured", "state/goal_horizon_stats.json")


# ---------------------------------------------------------------------------
# Raw vitals the review needed by hand.
# ---------------------------------------------------------------------------

def _vitals(config) -> dict:
    """The bare numbers a functional review had to hand-assemble."""
    v = {}

    gates = _read_json(config.state_dir / "level_gates.json")
    if isinstance(gates, dict):
        v["sleeps_total"] = _metric(gates.get("sleeps_total"), "measured", "state/level_gates.json")
    else:
        v["sleeps_total"] = _metric(None, "unmeasured", "state/level_gates.json missing/corrupt")

    persona = _read_json(config.workspace / "persona.json")
    if isinstance(persona, dict):
        v["goals_completed"] = _metric(persona.get("goals_completed"), "measured", "persona.json")
        v["level"] = _metric(persona.get("level"), "measured", "persona.json")
        v["total_ticks"] = _metric(persona.get("total_ticks"), "measured", "persona.json")
    else:
        for k in ("goals_completed", "level", "total_ticks"):
            v[k] = _metric(None, "unmeasured", "persona.json missing/corrupt")

    # Life stage (egg/hatchling/…) lives in phenotype.json (rewritten at each stage transition),
    # with persona.json as a fallback for older layouts.
    pheno = _read_json(config.workspace / "phenotype.json")
    if isinstance(pheno, dict) and pheno.get("stage") is not None:
        v["stage"] = _metric(pheno.get("stage"), "measured", "phenotype.json")
    elif isinstance(persona, dict) and persona.get("stage") is not None:
        v["stage"] = _metric(persona.get("stage"), "measured", "persona.json")
    else:
        v["stage"] = _metric(None, "unmeasured", "phenotype.json/persona.json (no stage field)")

    # Objectives open/done/dead — objectives.json is {objectives:[{state:...}]}.
    obj_store = _read_json(config.workspace / "objectives.json")
    if isinstance(obj_store, dict) and isinstance(obj_store.get("objectives"), list):
        oc = Counter(o.get("state") for o in obj_store["objectives"] if isinstance(o, dict))
        v["objectives"] = _metric(
            {"active": oc.get("active", 0), "blocked": oc.get("blocked", 0),
             "done": oc.get("done", 0), "dead": oc.get("dead", 0),
             "open": oc.get("active", 0) + oc.get("blocked", 0)},
            "measured", "objectives.json")
    else:
        v["objectives"] = _metric(
            {"active": 0, "blocked": 0, "done": 0, "dead": 0, "open": 0},
            "measured", "objectives.json (absent → none open)")

    # Self-edit proposals: count *.json manifests in the proposals dir (exclude staged sidecars).
    try:
        n_prop = sum(1 for p in config.proposals_dir.glob("*.json")
                     if not p.name.endswith(".staged.py"))
    except OSError:
        n_prop = 0
    v["self_edit_proposals"] = _metric(n_prop, "measured", "workspace/proposals/*.json")

    # Strategy-engram count: kind == "strategy" in the long-term engram store.
    strat = 0
    saw_engram = (config.knowledge_dir / "engram_longterm.jsonl").exists()
    for rec in _iter_jsonl(config.knowledge_dir / "engram_longterm.jsonl"):
        if isinstance(rec, dict) and rec.get("kind") == "strategy":
            strat += 1
    if saw_engram:
        v["strategy_engrams"] = _metric(strat, "measured",
                                        "knowledge/engram_longterm.jsonl(kind=strategy)")
    else:
        v["strategy_engrams"] = _metric(0, "measured",
                                        "knowledge/engram_longterm.jsonl (absent → none)")
    return v


# ---------------------------------------------------------------------------
# The D-table: ALWAYS ten rows, in order. Human-judged tests report supporting
# counts but pass no verdict.
# ---------------------------------------------------------------------------

def _d_table(config, measured: dict) -> list:
    """Build all ten D-rows. `measured` carries the pre-computed metric dicts for the
    machine-measurable tests (D2/D3/D5/D8/D10); the rest are human-judged with support."""
    # Support counts for human-judged tests, pulled fail-open from real stores.
    news = _read_json(config.state_dir / "news_queue.json")
    news_len = None
    if isinstance(news, dict):
        q = news.get("queue") or news.get("items") or news.get("news")
        news_len = len(q) if isinstance(q, list) else news.get("count")
    elif isinstance(news, list):
        news_len = len(news)

    obj_store = _read_json(config.workspace / "objectives.json")
    open_objs = 0
    if isinstance(obj_store, dict) and isinstance(obj_store.get("objectives"), list):
        open_objs = sum(1 for o in obj_store["objectives"]
                        if isinstance(o, dict) and o.get("state") in ("active", "blocked"))

    rows = []
    rows.append({"d": "D1", "name": "It has news when you come home",
                 **_metric({"news_queue_len": news_len}, "human-judged",
                           "state/news_queue.json (engagement trend is operator-observed)")})
    rows.append({"d": "D2", "name": "It doesn't repeat June's mistake", **measured["d2"]})
    rows.append({"d": "D3", "name": "It wakes up smarter", **measured["d3"]})
    rows.append({"d": "D4", "name": "It hesitates at the frontier",
                 **_metric({"note": "validation-strictness vs episodic coverage; observed"},
                           "human-judged", "human-judged (no strictness-vs-coverage store)")})
    rows.append({"d": "D5", "name": "It reuses its own hands", **measured["d5"]})
    rows.append({"d": "D6", "name": "It orbits locked doors",
                 **_metric({"open_objectives": open_objs},
                           "human-judged", "objectives.json (preparatory work is operator-observed)")})
    rows.append({"d": "D7", "name": "Its face never lies",
                 **_metric({"note": "render==felt-state projection (I6 gate)"},
                           "human-judged", "human-judged (render-parity gate I6)")})
    rows.append({"d": "D8", "name": "Nothing freezes the mind", **measured["d8"]})
    rows.append({"d": "D9", "name": "It commands well",
                 **_metric({"note": "delegated-mission success; shadow roster"},
                           "human-judged", "human-judged (no delegation store yet)")})
    rows.append({"d": "D10", "name": "It rises to the voice", **measured["d10"]})
    return rows


def _minimal_d_table(measured: dict) -> list:
    """Last-resort ten-row table if support-count gathering itself failed. Guarantees the
    invariant 'always ten rows' even under total store corruption."""
    names = [
        ("D1", "It has news when you come home", "human-judged"),
        ("D2", "It doesn't repeat June's mistake", "measured"),
        ("D3", "It wakes up smarter", "measured"),
        ("D4", "It hesitates at the frontier", "human-judged"),
        ("D5", "It reuses its own hands", "measured"),
        ("D6", "It orbits locked doors", "human-judged"),
        ("D7", "Its face never lies", "human-judged"),
        ("D8", "Nothing freezes the mind", "measured"),
        ("D9", "It commands well", "human-judged"),
        ("D10", "It rises to the voice", "measured"),
    ]
    key_for = {"D2": "d2", "D3": "d3", "D5": "d5", "D8": "d8", "D10": "d10"}
    rows = []
    for d, name, kind in names:
        if d in key_for and key_for[d] in measured:
            rows.append({"d": d, "name": name, **measured[key_for[d]]})
        else:
            rows.append({"d": d, "name": name, **_metric(None, kind, "fallback")})
    return rows


# ---------------------------------------------------------------------------
# Public entry point.
# ---------------------------------------------------------------------------

def build_growth(config) -> dict:
    """Aggregate the D1–D10 scoreboard + KPI trends + raw vitals. Pure read; fail-open
    per metric so a missing store degrades one row to 'unmeasured', never the whole panel.

    Returns:
        {
          "d_tests":  [ {d, name, value, status, basis} × 10 ],   # always ten rows
          "kpis":     { "goal_horizon": <metric> },
          "vitals":   { <name>: <metric>, ... },
        }
    """
    measured = {}
    # Each distiller is independently fail-open, but wrap defensively so one unexpected
    # store shape can never sink the whole panel.
    for key, fn in (("d2", d2_repeat_failure), ("d3", d3_wakes_up_smarter),
                    ("d5", d5_reuse_vs_authorship), ("d8", d8_nothing_freezes),
                    ("d10", d10_rises_to_voice)):
        try:
            measured[key] = fn(config)
        except Exception as e:  # noqa: BLE001 — never let one metric crash the panel
            measured[key] = _metric(None, "unmeasured", f"error computing {key}: {type(e).__name__}")

    try:
        kpi_horizon = goal_horizon_trend(config)
    except Exception as e:  # noqa: BLE001
        kpi_horizon = _metric(None, "unmeasured", f"error: {type(e).__name__}")

    try:
        vitals = _vitals(config)
    except Exception as e:  # noqa: BLE001
        vitals = {"_error": _metric(None, "unmeasured", f"error: {type(e).__name__}")}

    try:
        d_tests = _d_table(config, measured)
    except Exception:  # noqa: BLE001 — the table MUST always have ten rows; rebuild minimally
        d_tests = _minimal_d_table(measured)

    return {
        "d_tests": d_tests,
        "kpis": {"goal_horizon": kpi_horizon},
        "vitals": vitals,
    }
