#!/usr/bin/env python3
"""eiDOS dashboard — operator shell: web UI + supervisor/watchdog.

Two co-located responsibilities (phase 8.3 split VOICE out into its own process — voice.py — so a
native TTS/ffmpeg crash can't take the watchdog down; the UI HTML lives in static/dashboard.html):
  UI         — HTML dashboard (static/dashboard.html) + /api/status,/api/ping,/api/activity models
  SUPERVISOR — watchdog (spawn/respawn/crash-loop auto-rollback), /api/control/* + the event-driven
               /api/control/wait channel, git safety, self-edit apply, self-guide apply (trust boundary)

The browser loads this page from here (port 8099) but opens the speech SSE + audio streams directly
to the voice service (config.voice_port); eidos POSTs speech and yields the GPU gate there too.

Writes: paused/should_run/pid sentinels, chat_hold.json, interventions/, self_guide.md, watchdog
crash notes, and the source tree via git restore / self-edit apply. Stdlib only — no dependencies.
"""

import argparse
import json
import logging
import re
import sys
import threading
import time
from http.server import HTTPServer, ThreadingHTTPServer, BaseHTTPRequestHandler
from pathlib import Path

# Add project root for imports
sys.path.insert(0, str(Path(__file__).parent))

logger = logging.getLogger("dashboard")

from config import load_config, Config
from ascii_art import get_creature
from persona import load_persona, compute_level
from telemetry import get_cpu_pct
from typed_boundary import DashboardPayloadError, validate_dashboard_post_payload

import os

import creature_gen
import glue
from atomicio import replace_with_retry


def _read_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text())
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}


def _read_text(path: Path) -> str:
    try:
        return path.read_text()
    except (FileNotFoundError, OSError):
        return ""


_LAST_TOOL_SKIP = {"system", "watchdog", "dream", "thought", "planning", "__no_tool__"}


def _last_tool_call(config: Config) -> dict:
    """Most recent *real* tool call from observations.jsonl, for the tool bubble.

    Skips meta entries (thoughts, planning, watchdog/system, dream). Returns a small
    dict {tool, ok, summary, tick} or None.
    """
    path = config.workspace / "observations.jsonl"
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return None
    for ln in reversed(lines[-80:]):
        try:
            o = json.loads(ln)
        except Exception:  # noqa: BLE001
            continue
        tool = o.get("tool")
        if not tool or tool in _LAST_TOOL_SKIP:
            continue
        args = o.get("args") or {}
        summ = ""
        if isinstance(args, dict):
            summ = (args.get("cmd") or args.get("command") or args.get("path")
                    or args.get("url") or args.get("skill_name") or "")
        return {
            "tool": tool,
            "ok": bool(o.get("success")),
            "summary": str(summ)[:64],
            "tick": o.get("tick"),
        }
    return None


def _tail_jsonl(path: Path, n: int = 20) -> list:
    try:
        lines = path.read_text().strip().splitlines()
        result = []
        for line in lines[-n:]:
            try:
                result.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return result
    except (FileNotFoundError, OSError):
        return []


def _compute_narration(heartbeat: dict, persona: dict, goal: str, flavor: dict) -> str:
    """Derive a status narration from current state."""
    failures = heartbeat.get("consecutive_failures", 0)
    tick = heartbeat.get("tick", 0)
    uptime = heartbeat.get("uptime_s", 0)
    mood = persona.get("mood", "curious")
    streak = persona.get("current_streak", 0)

    if failures >= 3:
        return "Struggling... something isn't working. Might need a different approach."
    if not goal.strip():
        return "No goal set. Waiting for instructions."
    if tick <= 1:
        return "Just woke up. Getting my bearings."
    if mood == "triumphant":
        return "Just finished a goal. Feeling accomplished."
    if mood == "frustrated":
        return "Running into walls. Need to think differently."
    if mood == "struggling":
        return "Things are rough but not giving up."
    if streak > 20:
        return f"Good flow \u2014 {streak} successful actions in a row."
    if uptime and uptime > 86400:
        days = uptime / 86400
        return f"Been at this for {days:.1f} days. Steady progress."
    if mood == "focused":
        return "Locked in. Making progress."
    if mood == "determined":
        return "Working through challenges. Pushing forward."
    return "Working on it. One step at a time."


def build_knowledge_list(config: Config) -> dict:
    """Read last 10 knowledge entries from index."""
    idx_path = config.workspace / "knowledge" / "index.json"
    try:
        entries = json.loads(idx_path.read_text())
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        entries = []
    entries.sort(key=lambda e: e.get("created", ""), reverse=True)
    return {"entries": entries[:25]}



# --- Control-change channel (event-driven; ARCHITECTURE_PRINCIPLES.md #1) -----------------
# The reverse of the GPU gate: the dashboard is the PRODUCER of control state (pause/resume,
# listening hold, chat arrival) and eidos is the consumer. v1 made eidos poll three sentinel
# files on timers (pause @5s, hold @2s, interventions @<=2s) — delay-based guessing. Now every
# control mutation bumps a sequence counter and notifies; eidos makes ONE long-poll to
# /api/control/wait that returns the instant anything changes (or at its bounded timeout).
# The sentinel files REMAIN the crash-survivable ground truth — eidos re-reads them on wake and
# falls back to nap-polling if this channel is down. It's the polled consumption that violated
# the principle, not the files.
_ctl_cond = threading.Condition()
_ctl_seq = 0          # bumped on every control-state change; guarded by _ctl_cond


def control_notify(reason: str = "") -> None:
    """Producer hook: call after ANY control-state mutation (pause/resume/hold/chat)."""
    global _ctl_seq
    with _ctl_cond:
        _ctl_seq += 1
        _ctl_cond.notify_all()


def control_wait(config, since: int, max_s: float = 25.0) -> dict:
    """Block until the control seq passes `since` (event) or `max_s` elapses (bounded long-poll).
    Returns the new seq + a state snapshot so the consumer never needs a second request."""
    start = time.monotonic()
    max_s = max(0.0, min(float(max_s), 60.0))
    with _ctl_cond:
        while _ctl_seq <= since:
            remaining = max_s - (time.monotonic() - start)
            if remaining <= 0:
                break
            _ctl_cond.wait(timeout=remaining)
        seq = _ctl_seq
    snap = {"seq": seq, "paused": False, "held": False, "interventions": 0}
    try:
        snap["paused"] = (config.workspace / "paused").exists()
        snap["held"] = config.chat_hold_path.exists()
        idir = config.interventions_dir
        if idir.exists():
            snap["interventions"] = sum(
                1 for p in idir.iterdir()
                if not p.name.startswith(".") and p.suffix != ".done")
    except OSError:
        pass
    return snap


def build_dream_list(config: Config) -> dict:
    """Read last 10 memory snapshots (dream records)."""
    snap_dir = config.workspace / "snapshots"
    if not snap_dir.exists():
        return {"dreams": []}
    # Prefer real dream records (the briefing dream cycle's distillation: flavor + learned + plan).
    # Fall back to legacy memory_snapshot_* files. The <80-char filter below drops empty stubs.
    snapshots = sorted(
        list(snap_dir.glob("dream_*.md")) + list(snap_dir.glob("memory_snapshot_*")),
        key=lambda p: p.stat().st_mtime,
        reverse=True,   # newest first -> renders newest-at-top
    )
    dreams = []
    for snap in snapshots:
        try:
            content = snap.read_text()
        except OSError:
            continue
        if len(content.strip()) < 80:
            continue  # skip empty startup/test stubs that clutter the journal
        dreams.append({
            "ts": snap.stem.replace("memory_snapshot_", "").replace("dream_", ""),
            "chars": len(content),
            "preview": content[:300],
        })
        if len(dreams) >= 10:
            break
    return {"dreams": dreams}


def _disk_total_gb() -> float:
    """Total size of the drive the dashboard runs from (for the disk gauge scale)."""
    try:
        import shutil
        return round(shutil.disk_usage(__file__).total / (1024 ** 3), 1)
    except OSError:
        return 0.0


# --- Procedural creature (workspace/creature.json; dashboard is sole writer) ---

_CREATURE_LOCK = threading.Lock()   # ThreadingHTTPServer → concurrent /api/status
_HATCH_XP = 25                      # persona awards +1 XP per successful tick


def _jobs_list(config: Config) -> list:
    """jobs.json is a JSON ARRAY (unlike the dict files _read_json serves)."""
    try:
        data = json.loads((config.workspace / "jobs.json").read_text())
        return data if isinstance(data, list) else []
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return []


def _delegate_running(config: Config) -> bool:
    return any(j.get("kind") == "delegate" and j.get("status") == "running"
               for j in _jobs_list(config))


def _listening_hold_fresh(config: Config) -> bool:
    """Mirror eidos._chat_hold_active's freshness RULES (eidos.py:255-261): held, within the
    TTL, AND under the continuous ceiling. eiDOS stops honoring a hold after
    chat_hold_max_continuous_s, so a forgotten focused tab must stop rendering as listening
    too — TTL alone let it show ~5 min of false listening."""
    hold = _read_json(config.workspace / "state" / "chat_hold.json")
    if not hold.get("held"):
        return False
    try:
        now = time.time()
        ts = float(hold.get("ts", 0))
        age = now - ts
        if age < 0 or age > float(getattr(config, "chat_hold_ttl_s", 60.0)):
            return False
        first = float(hold.get("first_held_ts", ts) or ts)
        if now - first > float(getattr(config, "chat_hold_max_continuous_s", 300.0)):
            return False
    except (TypeError, ValueError):
        return False
    return True


def _creature_path(config: Config) -> Path:
    return config.workspace / "creature.json"


# --- Terrarium garden builder (read-only; reflects eiDOS's real growth) ---
import calendar  # noqa: E402
import hashlib   # noqa: E402

_GARDEN_CACHE = {}   # path -> (mtime, parsed_index)


def _iso_epoch(s: str) -> float:
    try:
        return float(calendar.timegm(time.strptime(s, "%Y-%m-%dT%H:%M:%SZ")))
    except (ValueError, TypeError):
        return 0.0


def _slot(record_id: str, n: int) -> int:
    """Stable per-record slot — md5, NOT Python hash() (salted per process)."""
    return int(hashlib.md5(str(record_id).encode("utf-8")).hexdigest(), 16) % n


def _hatched_ts(doc: dict) -> float:
    for e in reversed(doc.get("events", [])):
        if e.get("kind") == "hatched":
            return float(e.get("ts", 0)) or float(doc.get("born_ts", 0) or 0)
    return float(doc.get("born_ts", 0) or 0)


def _read_index_cached(config: Config) -> list:
    p = config.workspace / "knowledge" / "index.json"
    try:
        mtime = p.stat().st_mtime
    except OSError:
        return []
    cached = _GARDEN_CACHE.get(p)
    if cached and cached[0] == mtime:
        return cached[1]
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        data = data if isinstance(data, list) else []
    except (OSError, json.JSONDecodeError, ValueError):
        data = []
    _GARDEN_CACHE[p] = (mtime, data)
    return data


def _build_garden(config: Config, doc: dict, persona: dict) -> dict:
    """Per-slot counts of THIS incarnation's lived experience. Two filters make
    the garden a biography: drop seed (bootstrap) records, and drop anything
    created before this creature hatched (the knowledge store outlives a wipe)."""
    hatched = _hatched_ts(doc)
    buckets = {"facts": [0] * creature_gen.FACT_SLOTS, "procedures": [0] * creature_gen.TREE_SLOTS,
               "reflections": [0] * creature_gen.MOSS_SLOTS, "errors": [0] * creature_gen.STONE_SLOTS}
    for rec in _read_index_cached(config):
        if rec.get("source_goal") == "seed":
            continue
        if _iso_epoch(rec.get("created", "")) < hatched:
            continue
        slots = buckets.get(rec.get("category"))
        if slots is None:
            continue
        slots[_slot(rec.get("id", ""), len(slots))] += 1
    # done objectives
    done = 0
    try:
        obj = _read_json(config.workspace / "objectives.json")
        done = sum(1 for o in obj.get("objectives", []) if o.get("state") == "done")
    except Exception:  # noqa: BLE001
        pass
    # unconsumed interventions (consumed ones are renamed *.md.done)
    mail = False
    try:
        idir = config.interventions_dir
        mail = idir.exists() and any(idir.glob("*.md"))
    except Exception:  # noqa: BLE001
        pass
    return {
        "facts": buckets["facts"], "trees": buckets["procedures"],
        "moss": buckets["reflections"], "stones": buckets["errors"],
        "titles": len(persona.get("titles") or []),
        "done": done, "mail": bool(mail),
    }


def _save_creature(config: Config, doc: dict) -> None:
    config.workspace.mkdir(parents=True, exist_ok=True)
    tmp = _creature_path(config).with_suffix(".tmp")
    tmp.write_text(json.dumps(doc, indent=2), encoding="utf-8")
    replace_with_retry(tmp, _creature_path(config))


def _load_or_create_creature(config: Config) -> dict:
    """Read creature.json, or lay a brand-new egg (fresh incarnation = new genome).

    Seed unity (CREATURE_GENETICS.md red gate #5): the germline authority is
    workspace/genome.json — drawn once at the creature's first breath. When laying a NEW egg,
    adopt that seed so the dashboard creature, the behavioral genome, and the phenotype
    descriptions are ONE individual. Only when no genome exists yet (dashboard polled before
    eidos ever booted) does the egg fall back to its own draw — the pre-unification behavior."""
    with _CREATURE_LOCK:
        doc = _read_json(_creature_path(config))
        genome = doc.get("genome") or {}
        if doc.get("seed") and genome.get("v") == creature_gen.GENOME_VERSION:
            return doc
        seed = None
        try:
            germ = _read_json(config.workspace / "genome.json")
            if germ.get("seed"):
                seed = int(germ["seed"])
        except Exception:  # noqa: BLE001 - a missing/corrupt germline never blocks the egg
            seed = None
        if seed is None:
            seed = int.from_bytes(os.urandom(8), "big")
        doc = {
            "v": 1,
            "seed": seed,
            "genome": creature_gen.genome_from_seed(seed),
            "born_ts": time.time(),
            "hatched": False,
            "hatch_xp": _HATCH_XP,
            "events": [{"ts": time.time(), "kind": "laid"}],
        }
        try:
            _save_creature(config, doc)
        except OSError:
            logger.exception("creature.json save failed (continuing in-memory)")
        return doc


def _update_hatch(config: Config, doc: dict, persona: dict) -> dict:
    """Hatch progress from persona XP. Persists ONLY on threshold crossings
    (cracks at 1/3 and 2/3, hatch at 1.0) — never churns disk on a plain poll."""
    if doc.get("hatched"):
        return {"hatched": True, "progress": 1.0}
    xp = persona.get("xp", 0)
    progress = min(1.0, xp / max(1, doc.get("hatch_xp", _HATCH_XP)))
    events = doc.setdefault("events", [])
    have_cracks = sum(1 for e in events if e.get("kind") == "crack")
    want_cracks = (1 if progress >= 0.34 else 0) + (1 if progress >= 0.67 else 0)
    changed = False
    for n in range(have_cracks + 1, want_cracks + 1):
        events.append({"ts": time.time(), "kind": "crack", "n": n})
        changed = True
    if progress >= 1.0:
        doc["hatched"] = True
        events.append({"ts": time.time(), "kind": "hatched"})
        changed = True
    if changed:
        with _CREATURE_LOCK:
            try:
                _save_creature(config, doc)
            except OSError:
                logger.exception("creature.json hatch update failed")
    return {"hatched": bool(doc.get("hatched")), "progress": round(progress, 3)}


_METAMORPHOSIS_S = 60.0   # how long the cocoon interlude lasts


def _update_stage_events(config: Config, doc: dict, stage: str) -> None:
    """Phase B: record stage transitions. A non-egg UPGRADE (juvenile→adult etc.)
    triggers a metamorphosis event + cocoon interlude; hatching, downgrades, and
    the first-ever record (pre-Phase-B creatures) pass silently."""
    last = doc.get("last_stage")
    if stage == last:
        return
    order = creature_gen.STAGES
    is_upgrade = (last in order and stage in order
                  and last != "egg" and stage != "egg"
                  and order.index(stage) > order.index(last))
    doc["last_stage"] = stage
    if is_upgrade:
        doc["interlude_until"] = time.time() + _METAMORPHOSIS_S
        doc.setdefault("events", []).append(
            {"ts": time.time(), "kind": "metamorphosis", "stage": stage})
    with _CREATURE_LOCK:
        try:
            _save_creature(config, doc)
        except OSError:
            logger.exception("creature.json stage update failed")


def build_creature_spec(config: Config, persona: dict, heartbeat: dict,
                        goal: str) -> dict:
    """The living-creature payload: genome morphology + v2 truth expression."""
    doc = _load_or_create_creature(config)
    hatch = _update_hatch(config, doc, persona)
    stage = creature_gen.stage_for(persona.get("level", 1), hatch["hatched"])
    _update_stage_events(config, doc, stage)
    try:
        condition = glue.compute_condition(glue.recent_outcomes(config))
    except Exception:  # noqa: BLE001 — truth display must not break the page
        condition = "STABLE"
    expr = {
        "condition": condition,
        "delegating": _delegate_running(config),
        "listening": _listening_hold_fresh(config),
        "dead": heartbeat.get("consecutive_failures", 0) >= 5,
        "paused": (config.workspace / "paused").exists(),
        "has_goal": bool(goal.strip()),
    }
    spec = creature_gen.build_spec(doc["genome"], stage, hatch, expr)
    until = float(doc.get("interlude_until") or 0)
    if stage != "egg" and until > time.time():
        # Mid-metamorphosis: the body is wrapped — swap in the chrysalis grids.
        spec.update(creature_gen.compose_cocoon(doc["genome"], stage))
        spec["interlude"] = {"kind": "cocoon", "until_ts": until}
    spec["events"] = doc.get("events", [])[-5:]
    try:
        spec["terrarium"] = creature_gen.compose_terrarium(
            doc["genome"], _build_garden(config, doc, persona))
    except Exception:  # noqa: BLE001 — the garden must never break the page
        logger.exception("terrarium build failed")
    spec["delegates"] = _delegates_payload(config)
    spec["pending"] = _build_pending(config)
    spec["beats"] = _update_beats(config, doc)
    bond = _accrue_bond(config, doc)
    spec["bond_expr"] = {"tier": int(bond.get("tier", 0))}
    spec["bond_hover"] = dict(bond.get("counts", {}))
    spec["identity"] = _identity_payload(config)
    spec["ladder"] = _ladder_payload(config)
    spec["quest"] = _active_quest_payload(config)
    return spec


def _identity_payload(config: Config) -> dict:
    """The creature's genetic identity (genetics v2): morph + germline seed for the Buddy pane's
    nameplate. Operator-facing — the fourth wall doesn't apply to the forge's window."""
    try:
        g = json.loads((config.workspace / "genome.json").read_text(encoding="utf-8"))
        return {"morph": str(g.get("morph") or ""), "seed": str(g.get("seed") or "")}
    except Exception:  # noqa: BLE001 — pre-genome workspace: the pane just omits the line
        return {}


def _ladder_payload(config: Config) -> dict:
    """The tool-unlock ladder at a glance: every unit in ladder order, with its books state
    (granted source / pending hold reason). Flag off → {} and the pane renders nothing."""
    try:
        if not getattr(config, "pillars_tool_unlocks_enabled", False):
            return {}
        import unlocks
        st = unlocks.UnlockState(config)
        return {"units": [u.id for u in unlocks.UNITS],
                "granted": {k: str(v.get("source") or "") for k, v in st.granted.items()},
                "pending": dict(st.pending)}
    except Exception:  # noqa: BLE001 — the ladder must never break the page
        return {}


def _active_quest_payload(config: Config) -> dict:
    """What the System has ISSUED to the creature right now (not the Administrator's proposal
    queue — that has its own panel). Empty dict when nothing is active."""
    try:
        import quests
        q = quests.QuestStore(config).active()
        if q is None:
            return {}
        return {"id": q.id, "directive": q.directive, "tier": int(getattr(q, "tier", 1) or 1)}
    except Exception:  # noqa: BLE001 — the System must never break the page
        return {}


def _delegates_payload(config: Config) -> list:
    """All delegate jobs (running + recently-finished; jobs.json keeps the last 15)
    so the client mini-me can catch the return transition on the 2.5s poll. A 1:1
    render of reality — the mini-me IS the delegate job's live state."""
    out = []
    for j in _jobs_list(config):
        if j.get("kind") != "delegate":
            continue
        out.append({"name": j.get("name"), "mode": j.get("mode", "research"),
                    "status": j.get("status", "running"),
                    "started_ts": j.get("started_ts", 0)})
    return out


def _build_pending(config: Config) -> dict:
    """What eiDOS is asking Dean to approve RIGHT NOW — the actionable pull. A
    self-guide proposal staged, and/or self-edit proposals awaiting review. The
    creature holds up a tablet while this is non-empty (client renders it)."""
    try:
        sg = config.self_guide_proposed_path.exists()
    except Exception:  # noqa: BLE001
        sg = False
    se = 0
    try:
        import selfedit
        se = sum(1 for m in selfedit.list_proposals(config, kind="self_edit")
                 if m.get("status") == "pending")
    except Exception:  # noqa: BLE001
        se = 0
    return {"self_guide": bool(sg), "selfedits": int(se)}


def _count_consumed(config: Config) -> int:
    try:
        idir = config.interventions_dir
        return sum(1 for _ in idir.glob("*.md.done")) if idir.exists() else 0
    except Exception:  # noqa: BLE001
        return 0


def _update_beats(config: Config, doc: dict) -> list:
    """Edge-triggered 'it responded to me' beats with stable ids (client plays each
    id once, ever — reload mid-beat replays nothing). Consume = eiDOS read a message
    (a *.md → *.md.done rename). Multiple consumes between polls collapse to one beat.
    First sight establishes a baseline silently (no beat for historical consumes)."""
    seen = doc.setdefault("bond_seen", {})
    beats = doc.setdefault("beats", [])
    consumed = _count_consumed(config)
    prev = seen.get("consumed")
    changed = False
    if prev is None:
        seen["consumed"] = consumed          # baseline only — no beat for the backlog
        changed = True
    elif consumed > prev:
        seen["consumed"] = consumed
        doc["beat_seq"] = int(doc.get("beat_seq", 0)) + 1
        beats.append({"id": "b%d" % doc["beat_seq"], "type": "consume",
                      "ts": time.time()})
        del beats[:-8]
        changed = True
    if changed:
        with _CREATURE_LOCK:
            try:
                _save_creature(config, doc)
            except OSError:
                logger.exception("creature.json beat update failed")
    return beats[-5:]


# Provisional bond tiers (recalibrated from real telemetry after ~2 weeks — the
# ledger carries by_kind so the thresholds can be tuned without code changes).
BOND_TIERS = [(400, 5), (220, 4), (120, 3), (60, 2), (25, 1)]


def _bond_tier(score: float) -> int:
    for thresh, n in BOND_TIERS:
        if score >= thresh:
            return n
    return 0


def _accrue_bond(config: Config, doc: dict) -> dict:
    """Monotonic ledger of shared work. Accrues from the poll-detectable signals
    (consume / listening minutes / self-edit-that-survived), each rarity-weighted
    and daily-capped so it can't be farmed. Resets with the incarnation; never
    decays. Persists only when a point is actually credited."""
    now = time.time()
    b = doc.setdefault("bond", {})
    b.setdefault("score", 0.0)
    b.setdefault("by_kind", {})
    counts = b.setdefault("counts", {"exchanges": 0, "hold_min": 0, "approvals": 0})
    day = b.setdefault("day", {})
    today = time.strftime("%Y-%m-%d", time.gmtime(now))
    if day.get("date") != today:
        day = b["day"] = {"date": today}
    changed = False

    def credit(kind, pts, day_key=None, cap=None):
        nonlocal changed
        if day_key is not None and cap is not None:
            pts = min(pts, max(0, cap - day.get(day_key, 0)))
        if pts <= 0:
            return 0
        if day_key is not None:
            day[day_key] = day.get(day_key, 0) + pts
        b["score"] = round(b.get("score", 0) + pts, 2)
        b["by_kind"][kind] = b["by_kind"].get(kind, 0) + pts
        changed = True
        return pts

    # S1 consume +2 each, cap 10/day — baseline skips the historical backlog
    consumed = _count_consumed(config)
    if not b.get("baseline_set"):
        b["credited_consumed"] = consumed
        b["baseline_set"] = True
        changed = True
    elif consumed > b.get("credited_consumed", 0):
        delta = consumed - b["credited_consumed"]
        credit("chat", delta * 2, "chat", 10)
        counts["exchanges"] += delta
        b["credited_consumed"] = consumed
        changed = True

    # S3 listening +1/min, cap 4/day (presence with intent)
    if _listening_hold_fresh(config) and now - b.get("last_listen_credit_ts", 0) >= 60:
        if credit("hold_min", 1, "hold", 4):
            counts["hold_min"] += 1
        b["last_listen_credit_ts"] = now
        changed = True

    # S5 self-edit applied AND survived 30 min without rollback, +12 (real coaching)
    try:
        import selfedit
        seen = set(b.get("credited_selfedits", []))
        before = len(seen)
        for m in selfedit.list_proposals(config, kind="self_edit"):
            mid = m.get("id")
            if (m.get("status") == "applied" and mid not in seen
                    and m.get("applied_ts") and now - float(m["applied_ts"]) >= 1800):
                credit("selfedits", 12)
                counts["approvals"] += 1
                seen.add(mid)
        if len(seen) != before:
            b["credited_selfedits"] = sorted(seen)
            changed = True
    except Exception:  # noqa: BLE001
        pass

    b["tier"] = _bond_tier(b.get("score", 0))
    b["tiers_provisional"] = True
    if changed:
        with _CREATURE_LOCK:
            try:
                _save_creature(config, doc)
            except OSError:
                logger.exception("creature.json bond update failed")
    return b


def build_status(config: Config) -> dict:
    """Assemble full status from workspace files."""
    heartbeat = _read_json(config.workspace / "heartbeat.json")
    persona = _read_json(config.workspace / "persona.json")
    wal = _read_json(config.workspace / "wal.json")
    activity = _read_json(config.workspace / "activity.json")
    goal = _read_text(config.workspace / "goal.md")
    plan = _read_text(config.workspace / "plan.md")[:2000]
    observations = _tail_jsonl(config.workspace / "observations.jsonl", 20)
    paused = (config.workspace / "paused").exists()
    flavor = _read_json(config.workspace / "flavor.json")
    narration = _compute_narration(heartbeat, persona, goal, flavor)

    level = persona.get("level", 1)
    mood = persona.get("mood", "curious")
    traits = persona.get("traits", [])
    xp = persona.get("xp", 0)
    titles = persona.get("titles", [])

    # Determine special state
    special = None
    cf = heartbeat.get("consecutive_failures", 0)
    if cf >= 5:
        special = "dead"
    elif not goal.strip():
        special = "sleeping"

    creature = get_creature(level, mood, traits, special=special)

    creature_spec = None
    try:
        creature_spec = build_creature_spec(config, persona, heartbeat, goal)
    except Exception:  # noqa: BLE001 — the spec must never kill /api/status
        logger.exception("creature spec build failed (client falls back to legacy)")

    return {
        "heartbeat": heartbeat,
        "creature_spec": creature_spec,
        "persona": {
            "name": persona.get("name", "eiDOS"),
            "level": level,
            "xp": xp,
            "xp_next": ((level) ** 2) * 50,  # XP needed for next level
            "mood": mood,
            "traits": traits,
            "titles": titles,
            "goals_completed": persona.get("goals_completed", 0),
            "total_ticks": persona.get("total_ticks", 0),
            "longest_streak": persona.get("longest_streak", 0),
        },
        "creature": creature,
        "goal": goal[:500],
        "plan": plan,
        "observations": observations,
        "narration": narration,
        "flavor": flavor,
        "paused": paused,
        "disk_total_gb": _disk_total_gb(),
        "activity": activity,
        "wal": {
            "tick": wal.get("tick_number", 0),
            "consecutive_failures": wal.get("consecutive_failures", 0),
        },
        "commission": _commission_status(config),
        "ts": time.time(),
    }


def _commission_status(config: Config) -> dict:
    """The commission at a glance for /api/status (read-only — the eidos engine owns the store).
    Empty dict when the organ is dark or there is no commission."""
    if not getattr(config, "pillars_commission_enabled", False):
        return {}
    try:
        import commission as _cm
        c = _cm.Commission(config)
        tasks = c.load()
        live = [t for t in tasks if t.state in ("open", "done_claimed")]
        return {
            "brief": bool(_cm.load_brief(config)),
            "confirmed_total": sum(1 for t in tasks if t.state == "confirmed"),
            "awaiting": [{"id": t.id, "title": t.title, "evidence": t.evidence}
                         for t in live if t.state == "done_claimed"],
            "open": sum(1 for t in live if t.state == "open"),
        }
    except Exception:  # noqa: BLE001 — the strip must never kill /api/status
        return {}


def build_ping(config: Config) -> dict:
    """Tiny health-check response (<500 bytes)."""
    hb = _read_json(config.workspace / "heartbeat.json")
    return {
        "ts": hb.get("ts", 0),
        "tick": hb.get("tick", 0),
        "level": hb.get("level", 1),
        "mood": hb.get("mood", "unknown"),
        "ok": hb.get("consecutive_failures", 0) < 5,
        "failures": hb.get("consecutive_failures", 0),
        "disk_free_gb": hb.get("disk_free_gb"),
        "ram_pct": hb.get("ram_pct"),
        "uptime_s": hb.get("uptime_s", 0),
    }


def build_chat(config: Config) -> dict:
    """Build chat history from interventions, replies, and pending questions."""
    messages = []

    # Operator → LLM: intervention files (pending + consumed)
    idir = config.interventions_dir
    if idir.exists():
        for path in sorted(idir.iterdir()):
            if path.name.startswith("."):
                continue
            try:
                content = path.read_text().strip()
                if not content:
                    continue
                done = path.suffix == ".done"
                mtime = path.stat().st_mtime
                messages.append({
                    "direction": "outgoing",
                    "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(mtime)),
                    "text": content[:2000],
                    "status": "delivered" if done else "pending",
                })
            except OSError:
                continue

    # LLM → Operator: chat replies
    replies = _tail_jsonl(config.workspace / "chat_replies.jsonl", 50)
    for r in replies:
        messages.append({
            "direction": "incoming",
            "ts": r.get("ts", ""),
            "text": r.get("text", ""),
            "status": "delivered",
            "spoken": bool(r.get("spoken", False)),  # spoken aloud (speak tool) vs silent <reply>
        })

    messages.sort(key=lambda m: m.get("ts", ""))
    return {"messages": messages}


def _tool_preview(name: str, args) -> str:
    """Build a human-readable preview of a tool call."""
    if not isinstance(args, dict):
        return name
    if name == "bash":
        return "$ " + (args.get("cmd", "") or "")[:100]
    if name == "write_file":
        return "writing " + (args.get("path", "") or "")
    if name == "read_file":
        return "reading " + (args.get("path", "") or "")
    if name == "memorize":
        return (args.get("fact", "") or "")[:100] or "memorizing"
    if name == "remember":
        return (args.get("note", "") or "")[:100] or "noting something"
    if name == "recall":
        return "recalling: " + (args.get("query", "") or "")[:80]
    if name == "http_request":
        return "fetching " + (args.get("url", "") or "")[:80]
    if name == "bg_run":
        return "starting: " + (args.get("cmd", "") or "")[:80]
    if name == "bg_check":
        return "checking on " + (args.get("name", "") or "")
    if name == "update_plan":
        return (args.get("note", "") or "")[:100] or "updating plan"
    return name


def build_thoughts(config: Config, limit: int = 30) -> dict:
    """The agent's train of thought (thoughts.jsonl) for the Buddy Thoughts panel.

    Falls back to parsing llm_log.jsonl when no thought stream exists yet.
    """
    thought_entries = _tail_jsonl(config.workspace / "thoughts.jsonl", limit)
    if thought_entries:
        out = []
        for e in reversed(thought_entries):  # newest first
            text = (e.get("text") or "").strip()
            if not text:
                continue
            out.append({
                "tick": e.get("tick", 0),
                "ts": e.get("ts", ""),
                "elapsed_s": 0,
                "preview": text,
                "raw_tail": text[-60:].replace("\n", " ").strip(),
                "segments": [{"type": "thinking", "text": text}],
            })
        return {"thoughts": out}

    import re

    entries = _tail_jsonl(config.workspace / "llm_log.jsonl", limit)
    thoughts = []
    for entry in reversed(entries):  # newest first
        raw = entry.get("response_preview", "")
        if not raw:
            continue

        tick = entry.get("tick", 0)
        ts = entry.get("ts", "")
        elapsed = entry.get("elapsed_s", 0)

        # Split response into segments: thinking text vs tool calls
        segments = []
        pos = 0
        for m in re.finditer(
            r'<tool>(\w+)</tool>\s*\n?<args>(.*?)</args>',
            raw, re.DOTALL
        ):
            # Thinking text before this tool call
            thinking = raw[pos:m.start()].strip()
            if thinking:
                segments.append({"type": "thinking", "text": thinking})
            # The tool call itself
            tool_name = m.group(1)
            try:
                tool_args = json.loads(m.group(2))
            except (json.JSONDecodeError, ValueError):
                tool_args = m.group(2)
            segments.append({"type": "tool", "name": tool_name, "args": tool_args})
            pos = m.end()

        # Trailing thinking text after last tool call
        trailing = raw[pos:].strip()
        if trailing:
            segments.append({"type": "thinking", "text": trailing})

        # If no tool tags found, treat entire response as thinking
        if not segments and raw.strip():
            segments.append({"type": "thinking", "text": raw.strip()})

        # Build a short preview — prefer thinking text, else describe the tool action
        preview = ""
        for seg in segments:
            if seg["type"] == "thinking":
                preview = seg["text"][:120]
                break
        if not preview:
            for seg in segments:
                if seg["type"] == "tool":
                    preview = _tool_preview(seg["name"], seg.get("args", {}))
                    break

        # Raw tail for thought bubble display
        raw_tail = raw[-60:].replace('\n', ' ').strip() if raw else ''

        thoughts.append({
            "tick": tick,
            "ts": ts,
            "elapsed_s": elapsed,
            "preview": preview,
            "raw_tail": raw_tail,
            "segments": segments,
        })

    return {"thoughts": thoughts}


def build_metrics(config: Config, limit: int = 60) -> dict:
    """Return last N metrics points for charting."""
    entries = _tail_jsonl(config.workspace / "metrics.jsonl", limit)
    pts = []
    for e in entries:
        pts.append({
            "ts": e.get("ts", 0),
            "tick": e.get("tick", 0),
            "cpu_pct": e.get("cpu_pct", 0),
            "ram_pct": e.get("ram_pct", 0),
            "llm_elapsed_s": e.get("llm_elapsed_s", 0),
        })
    return {"metrics": pts}


# --- HTML Template (served from static/dashboard.html; phase 8.3a) ---

_STATIC_DIR = Path(__file__).resolve().parent / "static"


def _render_html(config: Config) -> str:
    """Load the dashboard page from static/dashboard.html and fill placeholders. Read per
    request so UI edits go live without a dashboard restart (the page is fetched once per
    browser load, so the disk read is negligible)."""
    html = (_STATIC_DIR / "dashboard.html").read_text(encoding="utf-8")
    html = html.replace("{{NAME}}", "eiDOS")
    html = html.replace("{{INTERVAL_MS}}", str(config.tick_interval_s * 1000))
    html = html.replace("{{VOICE_PORT}}", str(getattr(config, "voice_port", 8098)))
    html = html.replace("{{IDE_PORT}}", str(getattr(config, "ide_port", 8100)))
    return html


def _make_handler(config: Config):
    """Create a request handler class bound to the given config."""

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format, *args):
            pass  # suppress default stderr logging

        def _respond(self, code, content_type, body):
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            if isinstance(body, str):
                body = body.encode("utf-8")
            self.wfile.write(body)

        def do_GET(self):
            if self.path == "/":
                self._respond(200, "text/html; charset=utf-8", _render_html(config))

            elif self.path in ("/static/creature.js", "/static/ide.js"):
                # Explicit whitelist (no generic static serving = no traversal surface).
                try:
                    name = self.path.rsplit("/", 1)[-1]
                    body = (_STATIC_DIR / name).read_text(encoding="utf-8")
                    self._respond(200, "application/javascript; charset=utf-8", body)
                except OSError:
                    self.send_error(404)

            elif self.path == "/api/status":
                status = build_status(config)
                self._respond(200, "application/json", json.dumps(status))

            elif self.path == "/api/ping":
                ping = build_ping(config)
                self._respond(200, "application/json", json.dumps(ping))

            elif self.path == "/api/activity":
                activity = _read_json(config.workspace / "activity.json")
                activity["gpu"] = get_gpu_stats()
                activity["llm"] = get_llm_stats(config)
                activity["last_tool"] = _last_tool_call(config)
                self._respond(200, "application/json", json.dumps(activity))

            elif self.path == "/api/chat":
                chat = build_chat(config)
                self._respond(200, "application/json", json.dumps(chat))

            elif self.path == "/api/knowledge":
                data = build_knowledge_list(config)
                self._respond(200, "application/json", json.dumps(data))

            elif self.path == "/api/dreams":
                data = build_dream_list(config)
                self._respond(200, "application/json", json.dumps(data))

            elif self.path == "/api/config":
                # Curated settings + their CURRENT values (re-read from disk so the overlay shows
                # immediately), plus platform/shell info for the menu header.
                import settings_schema
                from config import load_config as _load, LOCAL_CONFIG_NAME
                import platform_shell as _ps
                _, _, _kdir = _ctrl_paths(config)
                _fresh = _load(str(_kdir / "config.toml"))
                _shell = "PowerShell" if os.name == "nt" else " ".join(_ps.posix_shell(_fresh))
                self._respond(200, "application/json", json.dumps({
                    "groups": settings_schema.current_settings(_fresh),
                    "profiles": settings_schema.model_profiles(_fresh),
                    "active_model": _fresh.llm_model,
                    "has_overlay": (_kdir / LOCAL_CONFIG_NAME).exists(),
                    "platform": sys.platform, "shell": _shell, "llm_url": _fresh.llm_url,
                }))

            elif self.path.startswith("/api/llm/models"):
                from urllib.parse import urlparse, parse_qs
                q = parse_qs(urlparse(self.path).query)
                url = (q.get("url", [""])[0] or "").strip() or config.llm_url
                self._respond(200, "application/json", json.dumps(_fetch_models(url)))

            elif self.path.startswith("/api/control/wait"):
                # eidos's event-driven control channel: blocks until pause/hold/chat state
                # changes past ?since= (or ?max_s= elapses), then returns seq + snapshot.
                from urllib.parse import urlparse, parse_qs
                q = parse_qs(urlparse(self.path).query)
                try:
                    since = int((q.get("since") or ["-1"])[0])
                except (TypeError, ValueError):
                    since = -1
                try:
                    max_s = float((q.get("max_s") or ["25"])[0])
                except (TypeError, ValueError):
                    max_s = 25.0
                self._respond(200, "application/json",
                              json.dumps(control_wait(config, since, max_s)))

            elif self.path == "/api/thoughts":
                data = build_thoughts(config)
                self._respond(200, "application/json", json.dumps(data))

            elif self.path == "/api/metrics":
                data = build_metrics(config)
                self._respond(200, "application/json", json.dumps(data))

            elif self.path == "/api/nervous":
                self._respond(200, "application/json", json.dumps(build_nervous(config)))

            elif self.path.startswith("/api/why"):
                # Causal ledger (Pillars 0.3): the pressure field that produced a given tick —
                # ?tick=N returns that tick's record; no tick returns the recent field for a panel.
                from urllib.parse import urlparse, parse_qs
                q = parse_qs(urlparse(self.path).query)
                _tick_q = (q.get("tick") or [""])[0]
                self._respond(200, "application/json",
                              json.dumps(build_why(config, _tick_q)))

            elif self.path == "/api/control/status":
                self._respond(200, "application/json", json.dumps(_ctrl_status(config)))

            elif self.path == "/api/self_guide":
                self._respond(200, "application/json", json.dumps(build_self_guide(config)))

            elif self.path == "/api/phenotype":
                # The creature's genetics-derived visual description (CREATURE_GENETICS.md):
                # workspace/phenotype.json, rewritten by the loop at each stage transition.
                # creature-forge (the text→image→3D digivice pipeline) consumes the prompt from
                # here — eiDOS owes the description; the forge owns everything after.
                pheno = _read_json(config.workspace / "phenotype.json")
                if pheno:
                    self._respond(200, "application/json", json.dumps(pheno))
                else:
                    self._respond(404, "application/json",
                                  json.dumps({"error": "no phenotype yet — the creature has "
                                              "not crossed a stage on genetics v2"}))

            elif self.path == "/api/git/log":
                import git_safety
                self._respond(200, "application/json", json.dumps(git_safety.git_log_summary(config)))

            elif self.path == "/api/selfedit/list":
                import selfedit
                self._respond(200, "application/json",
                              json.dumps({"proposals": selfedit.list_proposals(config, kind="self_edit"),
                                          "enabled": bool(getattr(config, "self_edit_enabled", False))}))

            elif self.path.startswith("/api/selfedit/diff"):
                import selfedit
                from urllib.parse import urlparse, parse_qs
                pid = parse_qs(urlparse(self.path).query).get("id", [""])[0]
                self._respond(200, "application/json", json.dumps(selfedit.get_diff(config, pid)))

            elif self.path == "/api/admin/list":
                # The System — proposals (Pillars 5.2): pending Administrator quest proposals
                # + the per-tier graduated-autonomy books.
                self._respond(200, "application/json", json.dumps(build_admin(config)))

            else:
                self._respond(404, "text/plain", "not found")

        def do_POST(self):
            # Uniform auth (phase 8.1): when a token is configured, EVERY state-changing POST
            # requires it — including /api/control/* (the kill-switch), /api/chat (the agent's
            # input channel), and /api/speech/* — which were previously ungated even with a
            # token set. Default empty token = open (accident-safety, trusted-LAN/Tailscale).
            if not _token_ok(self.headers, self.path, config):
                self._respond(401, "application/json", '{"error":"unauthorized"}')
                return
            if self.path == "/api/chat":
                length = int(self.headers.get("Content-Length", 0))
                if length > 10_000:
                    self._respond(413, "application/json", '{"error":"too large"}')
                    return
                try:
                    payload = validate_dashboard_post_payload(self.path, self.rfile.read(length))
                except DashboardPayloadError as exc:
                    self._respond(exc.status, "application/json", json.dumps({"error": str(exc)}))
                    return
                message = payload.message
                routed = _commission_chat_command(config, message)
                if routed is not None:
                    # An operator VERDICT, not a message to the creature: it went to the
                    # commission verdicts channel; the engine settles it next tick.
                    self._respond(200 if routed.get("ok") else 400,
                                  "application/json", json.dumps(routed))
                    if routed.get("ok"):
                        control_notify("chat")   # wake eidos — a settlement is waiting
                    return
                idir = config.interventions_dir
                idir.mkdir(parents=True, exist_ok=True)
                ts = time.strftime("%Y%m%d_%H%M%S", time.gmtime())
                fname = f"dash_{ts}.md"
                fpath = idir / fname
                n = 0
                while fpath.exists():
                    n += 1
                    fname = f"dash_{ts}_{n}.md"
                    fpath = idir / fname
                fpath.write_text(message)
                control_notify("chat")   # wake eidos instantly — a Boss message is the top event
                self._respond(200, "application/json", json.dumps({"ok": True, "filename": fname}))
            elif self.path == "/api/control/start":
                self._respond(200, "application/json", json.dumps(_ctrl_start(config)))
            elif self.path == "/api/control/stop":
                self._respond(200, "application/json", json.dumps(_ctrl_stop(config)))
            elif self.path == "/api/control/resume":
                self._respond(200, "application/json", json.dumps(_ctrl_resume(config)))
            elif self.path == "/api/control/pause":
                self._respond(200, "application/json", json.dumps(_ctrl_pause(config)))
            elif self.path == "/api/control/reset":
                # Destructive wipe (rebirth / full). Operator-only like the other controls.
                if not _token_ok(self.headers, self.path, config):
                    self._respond(401, "application/json", '{"ok":false,"error":"auth"}'); return
                length = int(self.headers.get("Content-Length", 0) or 0)
                if length > 1000:
                    self._respond(413, "application/json", '{"ok":false,"error":"too large"}'); return
                try:
                    payload = validate_dashboard_post_payload(self.path, self.rfile.read(length))
                except DashboardPayloadError as exc:
                    self._respond(exc.status, "application/json", json.dumps({"ok": False, "error": str(exc)})); return
                mode = payload.mode
                self._respond(200, "application/json", json.dumps(_ctrl_reset(config, mode)))
            elif self.path == "/api/config":
                # Save settings to the machine-local overlay (config.local.toml), then optionally
                # restart eidos so it picks them up. Never rewrites the committed config.toml.
                length = int(self.headers.get("Content-Length", 0) or 0)
                if length <= 0 or length > 20000:
                    self._respond(400, "application/json", '{"ok":false,"error":"bad body"}'); return
                try:
                    body = validate_dashboard_post_payload(self.path, self.rfile.read(length))
                except DashboardPayloadError as exc:
                    self._respond(exc.status, "application/json", json.dumps({"ok": False, "error": str(exc)})); return
                import settings_schema
                from config import save_overrides
                overrides, errors = settings_schema.build_overrides(body.settings)
                if errors:
                    self._respond(400, "application/json",
                                  json.dumps({"ok": False, "errors": errors})); return
                if not overrides:
                    self._respond(400, "application/json", '{"ok":false,"error":"no settings"}'); return
                _, _, _kdir = _ctrl_paths(config)
                try:
                    save_overrides(overrides, path=str(_kdir / "config.toml"))
                except Exception as exc:  # noqa: BLE001
                    self._respond(500, "application/json",
                                  json.dumps({"ok": False, "error": str(exc)})); return
                # Apply by restarting eidos on the new config (boots PAUSED → click GO), unless the
                # caller just wants to save. The dashboard re-reads config per request, so the menu
                # reflects the change immediately regardless.
                applied = bool(body.apply)
                result = _ctrl_start(config) if applied else {"ok": True}
                self._respond(200, "application/json", json.dumps({
                    "ok": True, "saved": overrides, "applied": applied,
                    "message": ("saved — restarting to apply (click GO)" if applied
                                else "saved — restart eidos to apply"),
                    "control": result}))
            elif self.path == "/api/llm/test":
                length = int(self.headers.get("Content-Length", 0) or 0)
                if length > 2000:
                    self._respond(413, "application/json", '{"ok":false,"error":"too large"}'); return
                url = config.llm_url
                try:
                    payload = validate_dashboard_post_payload(self.path, self.rfile.read(length))
                except DashboardPayloadError as exc:
                    self._respond(exc.status, "application/json", json.dumps({"ok": False, "error": str(exc)})); return
                url = payload.url or url
                self._respond(200, "application/json", json.dumps(_probe_llm(url)))
            elif self.path == "/api/chat_hold":
                # Listening hold — focusing the chat box quiets the loop. Best-effort,
                # never 500s; token-gated if a token is configured.
                if not _token_ok(self.headers, self.path, config):
                    self._respond(401, "application/json", '{"ok":false,"error":"auth"}'); return
                length = int(self.headers.get("Content-Length", 0) or 0)
                if length > 1000:
                    self._respond(413, "application/json", '{"ok":false,"error":"too large"}'); return
                try:
                    payload = validate_dashboard_post_payload(self.path, self.rfile.read(length))
                except DashboardPayloadError as exc:
                    self._respond(exc.status, "application/json", json.dumps({"ok": False, "error": str(exc)})); return
                held = payload.held
                self._respond(200, "application/json", json.dumps(_write_chat_hold(config, held)))
            elif self.path == "/api/self_guide":
                # Operator saves the LIVE self-guide (clears any pending eiDOS proposal).
                if not _token_ok(self.headers, self.path, config):
                    self._respond(401, "application/json", '{"ok":false,"error":"auth"}'); return
                length = int(self.headers.get("Content-Length", 0) or 0)
                if length > 20000:
                    self._respond(413, "application/json", '{"ok":false,"error":"too large"}'); return
                try:
                    payload = validate_dashboard_post_payload(self.path, self.rfile.read(length))
                except DashboardPayloadError as exc:
                    self._respond(exc.status, "application/json", json.dumps({"ok": False, "error": str(exc)})); return
                from memory import write_self_guide
                try:
                    write_self_guide(config, payload.content)
                    try:
                        config.self_guide_proposed_path.unlink()
                    except FileNotFoundError:
                        pass
                    self._respond(200, "application/json", json.dumps({"ok": True}))
                except OSError as e:
                    self._respond(500, "application/json", json.dumps({"ok": False, "error": str(e)}))
            elif self.path == "/api/self_guide/reject":
                if not _token_ok(self.headers, self.path, config):
                    self._respond(401, "application/json", '{"ok":false,"error":"auth"}'); return
                try:
                    config.self_guide_proposed_path.unlink()
                except FileNotFoundError:
                    pass
                self._respond(200, "application/json", json.dumps({"ok": True}))
            elif self.path == "/api/git/checkpoint":
                if not _token_ok(self.headers, self.path, config):
                    self._respond(401, "application/json", '{"ok":false,"error":"auth"}'); return
                length = int(self.headers.get("Content-Length", 0) or 0)
                if length > 2000:
                    self._respond(413, "application/json", '{"ok":false,"error":"too large"}'); return
                try:
                    payload = validate_dashboard_post_payload(self.path, self.rfile.read(length))
                except DashboardPayloadError as exc:
                    self._respond(exc.status, "application/json", json.dumps({"ok": False, "error": str(exc)})); return
                label = payload.label
                self._respond(200, "application/json", json.dumps(_git_checkpoint_endpoint(config, label)))
            elif self.path == "/api/git/restore":
                if not _token_ok(self.headers, self.path, config):
                    self._respond(401, "application/json", '{"ok":false,"error":"auth"}'); return
                length = int(self.headers.get("Content-Length", 0) or 0)
                if length > 2000:
                    self._respond(413, "application/json", '{"ok":false,"error":"too large"}'); return
                try:
                    payload = validate_dashboard_post_payload(self.path, self.rfile.read(length))
                except DashboardPayloadError as exc:
                    self._respond(exc.status, "application/json", json.dumps({"ok": False, "error": str(exc)})); return
                tag = payload.tag
                self._respond(200, "application/json", json.dumps(_git_restore_endpoint(config, tag)))
            elif self.path == "/api/selfedit/apply":
                if not _token_ok(self.headers, self.path, config):
                    self._respond(401, "application/json", '{"ok":false,"error":"auth"}'); return
                length = int(self.headers.get("Content-Length", 0) or 0)
                if length > 2000:
                    self._respond(413, "application/json", '{"ok":false,"error":"too large"}'); return
                try:
                    payload = validate_dashboard_post_payload(self.path, self.rfile.read(length))
                except DashboardPayloadError as exc:
                    self._respond(exc.status, "application/json", json.dumps({"ok": False, "error": str(exc)})); return
                pid = payload.id
                self._respond(200, "application/json", json.dumps(_selfedit_apply_endpoint(config, pid)))
            elif self.path == "/api/selfedit/reject":
                if not _token_ok(self.headers, self.path, config):
                    self._respond(401, "application/json", '{"ok":false,"error":"auth"}'); return
                import selfedit
                length = int(self.headers.get("Content-Length", 0) or 0)
                if length > 2000:
                    self._respond(413, "application/json", '{"ok":false,"error":"too large"}'); return
                try:
                    payload = validate_dashboard_post_payload(self.path, self.rfile.read(length))
                except DashboardPayloadError as exc:
                    self._respond(exc.status, "application/json", json.dumps({"ok": False, "error": str(exc)})); return
                pid, reason = payload.id, payload.reason
                self._respond(200, "application/json", json.dumps(selfedit.reject(config, pid, reason)))
            elif self.path == "/api/admin/approve":
                # Administrator proposal approval (Pillars 5.2): routes the pending quest through
                # System.propose — the ONLY channel into the creature's world. Optional `edit`
                # overrides the editable quest-window fields before it crosses.
                if not _token_ok(self.headers, self.path, config):
                    self._respond(401, "application/json", '{"ok":false,"error":"auth"}'); return
                import administrator
                length = int(self.headers.get("Content-Length", 0) or 0)
                if length > 8000:
                    self._respond(413, "application/json", '{"ok":false,"error":"too large"}'); return
                try:
                    payload = validate_dashboard_post_payload(self.path, self.rfile.read(length))
                except DashboardPayloadError as exc:
                    self._respond(exc.status, "application/json", json.dumps({"ok": False, "error": str(exc)})); return
                quest = administrator.approve_proposal(config, payload.id, payload.edit)
                if quest is None:
                    self._respond(200, "application/json",
                                  json.dumps({"ok": False, "error": "not pending / invalid edit / administrator off"}))
                else:
                    self._respond(200, "application/json", json.dumps({"ok": True, "quest_id": quest.id}))
            elif self.path == "/api/admin/reject":
                if not _token_ok(self.headers, self.path, config):
                    self._respond(401, "application/json", '{"ok":false,"error":"auth"}'); return
                import administrator
                length = int(self.headers.get("Content-Length", 0) or 0)
                if length > 2000:
                    self._respond(413, "application/json", '{"ok":false,"error":"too large"}'); return
                try:
                    payload = validate_dashboard_post_payload(self.path, self.rfile.read(length))
                except DashboardPayloadError as exc:
                    self._respond(exc.status, "application/json", json.dumps({"ok": False, "error": str(exc)})); return
                ok = administrator.reject_proposal(config, payload.id, payload.reason)
                self._respond(200, "application/json",
                              json.dumps({"ok": bool(ok)} if ok else
                                         {"ok": False, "error": "not pending / administrator off"}))
            elif self.path == "/api/admin/revoke":
                # The ban-hammer seam: revoke a tier's earned auto-issue; trust re-earned from zero.
                if not _token_ok(self.headers, self.path, config):
                    self._respond(401, "application/json", '{"ok":false,"error":"auth"}'); return
                import administrator
                length = int(self.headers.get("Content-Length", 0) or 0)
                if length > 2000:
                    self._respond(413, "application/json", '{"ok":false,"error":"too large"}'); return
                try:
                    payload = validate_dashboard_post_payload(self.path, self.rfile.read(length))
                except DashboardPayloadError as exc:
                    self._respond(exc.status, "application/json", json.dumps({"ok": False, "error": str(exc)})); return
                enabled = bool(getattr(config, "pillars_administrator_enabled", False))
                administrator.revoke_autonomy(config, payload.tier)
                self._respond(200, "application/json",
                              json.dumps({"ok": enabled, "tier": payload.tier} if enabled else
                                         {"ok": False, "error": "administrator off"}))
            else:
                self._respond(404, "text/plain", "not found")

    return Handler


# --- Self-improvement: token gate, self-guide, listening hold ---

import threading as _threading
_LIFECYCLE_LOCK = _threading.RLock()  # serialize privileged ops (checkpoint/restore/apply/restart)


def _token_ok(headers, path, config) -> bool:
    """Pragmatic auth: if a dashboard token is configured, require it (header or ?token=)
    on state-changing POSTs. Default empty token = off (trusted-LAN/Tailscale buddy)."""
    tok = (getattr(config, "dashboard_token", "") or "").strip()
    if not tok:
        return True
    given = headers.get("X-EiDOS-Token", "") or ""
    if not given:
        try:
            from urllib.parse import urlparse, parse_qs
            given = parse_qs(urlparse(path).query).get("token", [""])[0]
        except Exception:  # noqa: BLE001
            given = ""
    import hmac
    return hmac.compare_digest(given, tok)   # constant-time; never short-circuits on prefix


def build_self_guide(config) -> dict:
    """Self-guide panel payload: live content + any pending eiDOS proposal."""
    from memory import read_self_guide, read_self_guide_proposed
    live = read_self_guide(config)
    proposed = read_self_guide_proposed(config)
    mtime = None
    try:
        mtime = config.self_guide_path.stat().st_mtime
    except OSError:
        pass
    return {
        "content": live,
        "proposed": proposed,
        "has_proposal": bool(proposed) and proposed.strip() != live.strip(),
        "mtime": mtime,
        "max_bytes": config.self_guide_max_bytes,
    }


def build_admin(config) -> dict:
    """The System — proposals panel payload (Pillars 5.2): pending Administrator quest proposals
    (oldest first) + the per-tier graduated-autonomy books. Flag off → enabled False, empty lists.
    Read-only: the AdminState books are the Administrator's; this only renders them."""
    out = {"enabled": bool(getattr(config, "pillars_administrator_enabled", False)),
           "proposals": [], "autonomy": []}
    if not out["enabled"]:
        return out
    import administrator
    out["proposals"] = administrator.pending_proposals(config)
    state = administrator.AdminState(config)
    tiers = {int(t) for t in state.autonomy} | {int(p.get("tier", 1)) for p in out["proposals"]}
    for tier in sorted(tiers):
        a = state.autonomy.get(str(tier)) or {}
        dec = list(a.get("decisions") or [])
        out["autonomy"].append({
            "tier": tier,
            "decisions": len(dec),
            "approvals": sum(1 for x in dec if x),
            "approval_rate": round(sum(1 for x in dec if x) / len(dec), 3) if dec else None,
            "revoked": bool(a.get("revoked")),
            "auto_issue": state.tier_has_autonomy(tier),
        })
    return out


def _write_chat_hold(config, held: bool) -> dict:
    """Dashboard owns the chat_hold flag file (single writer). Carries first_held_ts forward."""
    import json as _json
    from atomicio import replace_with_retry
    path = config.chat_hold_path
    try:
        if not held:
            try:
                path.unlink()
            except FileNotFoundError:
                pass
            control_notify("hold_release")   # the loop resumes the instant Dean unfocuses/sends
            return {"ok": True, "held": False}
        config.state_dir.mkdir(parents=True, exist_ok=True)
        now = time.time()
        first = now
        try:
            prev = _json.loads(path.read_text(encoding="utf-8"))
            if prev.get("held") and (now - float(prev.get("ts", 0) or 0)) <= float(config.chat_hold_ttl_s):
                first = float(prev.get("first_held_ts", now) or now)
        except (FileNotFoundError, ValueError, OSError):
            pass
        tmp = path.with_suffix(".tmp")
        tmp.write_text(_json.dumps({"held": True, "ts": now, "first_held_ts": first,
                                    "source": "chat_focus"}), encoding="utf-8")
        replace_with_retry(str(tmp), str(path))
        control_notify("hold")
        return {"ok": True, "held": True}
    except OSError as e:
        return {"ok": False, "error": str(e)}


# --- eiDOS process control (start paused / go / pause / stop) ---

def _ctrl_paths(config):
    from pathlib import Path
    ws = config.workspace
    return ws / "eidos.pid", ws / "paused", Path(__file__).resolve().parent


def _llm_base(url: str) -> str:
    """Normalize an LLM base URL to its root (strip a trailing /v1 or /v1/chat/completions) so we can
    try both `/models` and `/v1/models` regardless of what the user pasted."""
    u = (url or "").rstrip("/")
    for suf in ("/v1/chat/completions", "/chat/completions", "/v1"):
        if u.endswith(suf):
            u = u[: -len(suf)]
            break
    return u.rstrip("/")


def _fetch_models(url: str) -> dict:
    """List models from an OpenAI-compatible server (best-effort) to power the Settings model picker.
    Tries {base}/v1/models then {base}/models. Returns {ok, models:[ids], error}."""
    import json as _json
    import urllib.request
    base = _llm_base(url)
    last = ""
    for ep in (base + "/v1/models", base + "/models"):
        try:
            with urllib.request.urlopen(ep, timeout=4) as r:
                doc = _json.loads(r.read().decode("utf-8", "replace"))
            rows = doc.get("data", doc) if isinstance(doc, dict) else doc
            ids = [m.get("id") or m.get("name") for m in rows if isinstance(m, dict)]
            ids = [i for i in ids if i]
            if ids:
                return {"ok": True, "models": sorted(set(ids)), "endpoint": ep}
        except Exception as e:  # noqa: BLE001
            last = f"{type(e).__name__}: {e}"
    return {"ok": False, "models": [], "error": last or "no models endpoint responded"}


def _probe_llm(url: str) -> dict:
    """Quick reachability + latency probe of an LLM endpoint for the Settings 'Test connection' button."""
    import time as _t
    import urllib.request
    base = _llm_base(url)
    t0 = _t.monotonic()
    for ep in (base + "/v1/models", base + "/models"):
        try:
            with urllib.request.urlopen(ep, timeout=4) as r:
                r.read(1)
            return {"ok": True, "latency_ms": int((_t.monotonic() - t0) * 1000), "endpoint": ep}
        except Exception as e:  # noqa: BLE001
            last = f"{type(e).__name__}: {e}"
    return {"ok": False, "error": last, "hint": "is the LLM server running at that URL?"}


_pid_cache = {}  # pid -> (checked_at, alive); tasklist is slow, cache briefly


def _ctrl_pid_alive(pid):
    import subprocess, time
    if not pid or pid <= 0:
        return False
    now = time.time()
    hit = _pid_cache.get(pid)
    if hit and now - hit[0] < 2.5:
        return hit[1]
    if os.name == "nt":
        try:
            out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"],
                                 capture_output=True, text=True, timeout=10)
            alive = str(pid) in (out.stdout or "")
        except Exception:
            alive = False
    else:
        # POSIX: signal 0 probes existence without touching the process. Using tasklist here (as the
        # original did) always raised FileNotFoundError on Linux → alive=False for EVERY pid, so the
        # watchdog saw a live, paused eidos as "died" and respawn-looped it forever (Start appeared to
        # do nothing; processes piled up).
        try:
            os.kill(pid, 0)
            alive = True
        except ProcessLookupError:
            alive = False
        except PermissionError:
            alive = True   # exists, owned by another user — still "alive"
        except OSError:
            alive = False
    _pid_cache[pid] = (now, alive)
    return alive


def _ctrl_status(config):
    pidfile, pausefile, _ = _ctrl_paths(config)
    pid = 0
    try:
        pid = int(pidfile.read_text().strip())
    except Exception:
        pid = 0
    running = _ctrl_pid_alive(pid)
    return {"running": running, "paused": pausefile.exists(), "pid": (pid if running else 0)}


def _eidos_should_run_path(config):
    """Desired-state flag: present = the watchdog should keep eiDOS alive."""
    return config.workspace / "eidos.should_run"


def _eidos_restart_breadcrumb(config):
    """Set by an operator START: the dashboard exit that follows is an INTENTIONAL full-service restart
    (reload ALL current code — supervisor + the eidos child + the nervous system), not a crash. The
    next boot honors it by force-respawning a FRESH, paused eidos even if the old one survived the
    restart; the outgoing watchdog stays quiet about eidos's death during the stop."""
    return config.workspace / "eidos.intentional_restart"


# The live HTTP server, so a control action can gracefully stop it to restart the service (set in main()).
_HTTP_SERVER = None


def _under_supervisor():
    """True when an external supervisor (nssm on Windows, systemd on Linux) will relaunch this process
    on exit — the only case where START should restart-by-exiting. The service definitions export
    EIDOS_SUPERVISED=1. Default FALSE so a bare `python dashboard.py` (a friend's desktop) restarts the
    eidos child IN PLACE instead of exiting the dashboard with nothing to bring it back."""
    return os.environ.get("EIDOS_SUPERVISED") == "1"


def _kill_tree_posix(pid):
    """POSIX counterpart of taskkill /T: SIGTERM the whole process group, give it a moment, then SIGKILL.
    eidos and its tool subprocesses are spawned with start_new_session=True, so they share a group."""
    import os as _os
    import signal as _signal
    import time as _time
    if not pid:
        return
    try:
        pgid = _os.getpgid(int(pid))
    except (ProcessLookupError, PermissionError, ValueError):
        pgid = None
    for sig in (_signal.SIGTERM, _signal.SIGKILL):
        try:
            if pgid is not None:
                _os.killpg(pgid, sig)
            else:
                _os.kill(int(pid), sig)
        except (ProcessLookupError, PermissionError):
            return
        if sig is _signal.SIGTERM:
            _time.sleep(0.5)


def _schedule_dashboard_exit(delay_s=0.8):
    """Exit this process shortly so nssm re-launches the dashboard from disk (fresh supervisor code).
    Runs in a daemon timer so the triggering HTTP response flushes first. nssm restarts the app on exit
    (a bare kill already respawns it — see CLAUDE.md), and the new boot brings eidos back paused."""
    import os, threading, time

    def _bye():
        time.sleep(delay_s)
        try:
            srv = _HTTP_SERVER
            if srv is not None:
                threading.Thread(target=srv.shutdown, daemon=True).start()  # stop accepting; don't block
                time.sleep(0.3)
        except Exception:  # noqa: BLE001
            pass
        os._exit(0)   # hard exit so nothing can wedge the restart; nssm relaunches from disk

    threading.Thread(target=_bye, name="dashboard-restart", daemon=True).start()


# Death event (phase 4b, ARCH #1): the spawn HOLDS the Popen handle and a daemon thread
# wait()s on it, so a child exit is an interrupt to the watchdog — not something a 5s
# tasklist poll discovers late. The pid file + tasklist liveness remain ground truth for
# children this dashboard run didn't spawn (e.g. eidos surviving a dashboard restart).
_child_died = threading.Event()


def _watch_child(proc):
    try:
        proc.wait()
    finally:
        _child_died.set()


def _spawn_eidos(config):
    """Spawn the eidos process detached, record its pid, return the pid."""
    import subprocess, sys, os
    _, _, kdir = _ctrl_paths(config)
    logf = open(config.workspace / "eidos_console.log", "ab")
    try:
        env = {**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8", "PYTHONUNBUFFERED": "1"}
        spawn_kwargs = {}
        if os.name == "nt":
            spawn_kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            # POSIX: own session/process group so _kill_tree_posix can reap eidos AND its tool
            # subprocesses (bash jobs, probes) as a group, mirroring Windows taskkill /T.
            spawn_kwargs["start_new_session"] = True
        proc = subprocess.Popen(
            [sys.executable, str(kdir / "eidos.py"), "--config", str(kdir / "config.toml")],
            cwd=str(kdir), stdout=logf, stderr=subprocess.STDOUT, env=env, **spawn_kwargs,
        )
    finally:
        # The child inherits its own handle at CreateProcess; closing the parent's copy
        # avoids leaking a handle (and a Windows file lock) on every respawn.
        try:
            logf.close()
        except OSError:
            pass
    (config.workspace / "eidos.pid").write_text(str(proc.pid))
    # Floor for the stale-heartbeat watchdog: a fresh process hasn't written a heartbeat yet, so the
    # OLD heartbeat ts is stale — record spawn time so we don't flag a still-booting eidos as wedged.
    try:
        (config.workspace / "eidos_spawn.ts").write_text(str(time.time()))
    except OSError:
        pass
    _child_died.clear()
    threading.Thread(target=_watch_child, args=(proc,), daemon=True,
                         name="eidos-death-watch").start()
    return proc.pid


def _ctrl_reset(config, mode):
    """Operator-triggered wipe via reset_eidos.py (it stops eidos, archives, clears).
    mode 'rebirth' = keep knowledge + skills, back to egg (same genome), Lv.0.
    mode 'full'    = Level 0, fresh bootstrap nuggets, new-genome egg.
    Leaves eidos STOPPED; operator presses Start → GO to begin the new life."""
    import subprocess, os, sys
    repo = Path(__file__).resolve().parent
    flags = ["--yes"]
    if mode == "rebirth":
        flags.append("--keep-knowledge")
    elif mode != "full":
        return {"ok": False, "error": f"unknown reset mode {mode!r}"}
    try:
        r = subprocess.run([sys.executable, str(repo / "reset_eidos.py"), *flags],
                           cwd=str(repo), env={**os.environ, "PYTHONUTF8": "1"},
                           capture_output=True, text=True, timeout=150)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)}
    tail = [l for l in (r.stdout or "").strip().splitlines() if l.strip()]
    ok = r.returncode == 0
    return {"ok": ok, "mode": mode,
            "message": ("reborn — back to egg, kept knowledge" if mode == "rebirth"
                        else "full wipe — Level 0") + " · press Start → GO" if ok else "reset failed",
            "output": "\n".join(tail[-8:]),
            "error": "" if ok else (r.stderr or "reset failed")[:300]}


def _ctrl_start(config):
    """START = (re)start the service so the CURRENT code goes live, with no shell. On Windows-under-nssm
    we restart the whole service (exit -> nssm relaunches the supervisor from disk), and its boot
    force-respawns a fresh, PAUSED eidos — so one click reloads the supervisor, the eidos child, AND the
    nervous system. Off-Windows / manual run (no supervisor): (re)start just the eidos child in place.
    Either way eidos comes up PAUSED awaiting GO."""
    import subprocess, os
    pidfile, pausefile, kdir = _ctrl_paths(config)
    # Ensure GPU services are up (STOP frees them). They're independent services, so they keep loading
    # even across the dashboard restart below.
    llama_note = ""
    if os.name == "nt":
        svc_list = ",".join(f"'{s}'" for s in _GPU_SERVICES_START)
        ps = (
            f"foreach ($s in @({svc_list})) "
            "{ Start-Service $s -ErrorAction SilentlyContinue }"
        )
        try:
            r = subprocess.run(
                ["powershell", "-NonInteractive", "-Command", ps],
                capture_output=True, text=True, timeout=60)
            if r.returncode == 0:
                llama_note = " (GPU services starting — give the mind ~20s before GO)"
        except Exception:  # noqa: BLE001
            pass
    pausefile.write_text("paused on start - click GO to begin")  # boot PAUSED
    _eidos_should_run_path(config).write_text("1")   # arm the watchdog
    try:
        (config.state_dir / "rollback_attempted").unlink()  # fresh operator start re-arms auto-recovery
    except OSError:
        pass
    if _under_supervisor():
        # Under a supervisor (nssm/systemd): restart the whole service — drop a breadcrumb (boot
        # force-respawns fresh eidos + quiets the outgoing watchdog), then exit so the supervisor
        # relaunches the dashboard on current code. (Works on Windows AND Linux under systemd.)
        _eidos_restart_breadcrumb(config).write_text(str(time.time()))
        _schedule_dashboard_exit()
        return {"ok": True, "restarting": True,
                "message": f"restarting service to load current code — reconnecting, then click GO{llama_note}",
                **_ctrl_status(config)}
    # Standalone (`python dashboard.py`, any OS): (re)start the eidos child in place on fresh code.
    st = _ctrl_status(config)
    if st["running"]:
        try:
            if os.name == "nt":
                subprocess.run(["taskkill", "/PID", str(st["pid"]), "/T", "/F"], capture_output=True, timeout=15)
            else:
                _kill_tree_posix(st["pid"])
        except Exception:  # noqa: BLE001
            pass
    pid = _spawn_eidos(config)
    return {"ok": True, "message": f"started PAUSED (pid {pid}) - click GO to wake it{llama_note}",
            **_ctrl_status(config)}


_GPU_SERVICES_STOP = (
    # Stopped in order; -Force cascades HouseAI-OpenWebUI via its dependency on HouseAI-Llama
    "EidosVoice",
    "HouseAI-Chatterbox",
    "HouseAI-Llama",
)
_GPU_SERVICES_START = (
    # Started in reverse order so the model is resident before voice/webui come up
    "HouseAI-Llama",
    "HouseAI-Chatterbox",
    "EidosVoice",
    "HouseAI-OpenWebUI",
)


def _free_llama_vram():
    """Stop all GPU-resident services to fully free VRAM. Returns a status string."""
    import subprocess, os
    if os.name != "nt":
        return ""
    svc_list = ",".join(f"'{s}'" for s in _GPU_SERVICES_STOP)
    ps = (
        f"foreach ($s in @({svc_list})) "
        "{ Stop-Service $s -Force -ErrorAction SilentlyContinue }"
    )
    try:
        r = subprocess.run(
            ["powershell", "-NonInteractive", "-Command", ps],
            capture_output=True, text=True, timeout=30)
        if r.returncode == 0:
            return "; VRAM freed (GPU services stopped)"
        return f"; GPU services stop failed (rc={r.returncode}): {r.stderr.strip()}"
    except Exception as e:  # noqa: BLE001
        return f"; GPU services stop err: {e}"


def _ctrl_stop(config):
    import subprocess, os
    pidfile, pausefile, _ = _ctrl_paths(config)
    try:
        _eidos_should_run_path(config).unlink()   # disarm watchdog: this is an intentional stop
    except OSError:
        pass
    st = _ctrl_status(config)
    if not st["running"]:
        try:
            pidfile.unlink()
        except OSError:
            pass
        vram_msg = _free_llama_vram()
        return {"ok": True, "message": f"not running{vram_msg}", **_ctrl_status(config)}
    pid = st["pid"]
    if os.name == "nt":
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                       capture_output=True, text=True, timeout=15)
    else:
        _kill_tree_posix(pid)
    try:
        pidfile.unlink()
    except OSError:
        pass
    # Reap eidos's detached background jobs — they survive its kill and would otherwise orphan.
    reaped = 0
    try:
        import tools
        reaped = tools.reap_jobs(config, kill_all=True)
    except Exception:  # noqa: BLE001
        pass
    vram_msg = _free_llama_vram()
    msg = f"force-killed pid {pid} (and children)" + (f"; reaped {reaped} bg job(s)" if reaped else "") + vram_msg
    return {"ok": True, "message": msg, **_ctrl_status(config)}


def _ctrl_resume(config):
    _, pausefile, _ = _ctrl_paths(config)
    try:
        pausefile.unlink()
    except OSError:
        pass
    control_notify("resume")   # wake eidos's control channel the instant the operator resumes
    return {"ok": True, "message": "resumed - consciousness running", **_ctrl_status(config)}


def _ctrl_pause(config):
    _, pausefile, _ = _ctrl_paths(config)
    pausefile.write_text("paused by operator")
    control_notify("pause")
    return {"ok": True, "message": "paused", **_ctrl_status(config)}


def _restart_eidos_keep_armed(config, reason="restart"):
    """Kill eidos but LEAVE the watchdog armed so it respawns with fresh code, booted PAUSED.
    Used after a git restore / self-edit apply. (Distinct from _ctrl_stop, which disarms.)"""
    import subprocess, os
    pidfile, pausefile, _ = _ctrl_paths(config)
    try:
        pausefile.write_text(f"paused: {reason}")   # boot paused for operator review
    except OSError:
        pass
    st = _ctrl_status(config)
    pid = st.get("pid")
    if st.get("running") and pid:
        if os.name == "nt":
            subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True, timeout=15)
        else:
            _kill_tree_posix(pid)
    try:
        pidfile.unlink()
    except OSError:
        pass
    _pid_cache.clear()
    return pid


def _git_checkpoint_endpoint(config, label=""):
    import git_safety
    with _LIFECYCLE_LOCK:
        return git_safety.make_checkpoint(config, label or "manual checkpoint")


def _git_restore_endpoint(config, tag=""):
    import git_safety
    with _LIFECYCLE_LOCK:
        res = git_safety.restore_to(config, tag)
        if res.get("ok"):
            pid = _restart_eidos_keep_armed(config, reason=f"git restore {res.get('tag','')}")
            res["restarted_pid"] = pid
            res["message"] = (f"Restored {res.get('restored',0)} files to {res.get('tag')}. "
                              f"eiDOS restarting (paused) on the restored code.")
        return res


def _current_heartbeat_ts(config) -> float:
    try:
        return float(_read_json(config.workspace / "heartbeat.json").get("ts", 0) or 0)
    except (ValueError, TypeError):
        return 0.0


def _eidos_spawn_ts(config) -> float:
    """When eidos was last spawned — the floor for the stale-heartbeat check, so a freshly-booting
    process (heartbeat not yet written) isn't mistaken for a wedged one."""
    try:
        return float((config.workspace / "eidos_spawn.ts").read_text().strip() or 0)
    except (OSError, ValueError):
        return 0.0


def _eidos_is_stuck(config, now: float = None) -> tuple:
    """(stuck, stale_for_seconds): True when eidos is ALIVE but not TICKING — its heartbeat (which
    only advances on a SUCCESSFUL tick) has been frozen longer than eidos_stuck_threshold_s. Floored
    by the spawn time so a still-booting eidos isn't flagged, and skipped while paused (a paused eidos
    legitimately doesn't tick). Pure read; the watchdog decides what to do with it."""
    import time as _t
    thr = getattr(config, "eidos_stuck_threshold_s", 600)
    if thr <= 0 or (config.workspace / "paused").exists():
        return False, 0.0
    last_progress = max(_current_heartbeat_ts(config), _eidos_spawn_ts(config))
    stale_for = (now or _t.time()) - last_progress
    return (stale_for > thr), stale_for


def _selfedit_apply_endpoint(config, pid):
    """Operator-approved self-edit apply: checkpoint+write+commit, then restart eidos paused.
    Arms the HEALTH PROBE (a pending_apply marker) before restarting, so the watchdog can
    auto-rollback a self-edit that boots-but-misbehaves — not just one that crash-loops."""
    import selfedit
    with _LIFECYCLE_LOCK:
        res = selfedit.apply(config, pid)
        if res.get("ok"):
            probe_s = float(getattr(config, "self_edit_health_probe_s", 90) or 90)
            selfedit.write_pending_apply(
                config, pid, res.get("prev_sha", ""),
                baseline_heartbeat_ts=_current_heartbeat_ts(config),
                deadline_epoch=time.time() + probe_s)
            newpid = _restart_eidos_keep_armed(config, reason=f"self-edit {pid}")
            res["restarted_pid"] = newpid
            res["message"] = (res.get("message", "") +
                              f" eiDOS restarting (paused) on the new code as pid {newpid}. "
                              f"Health probe armed ({probe_s:.0f}s).")
        return res


# --- Watchdog: auto-restart eiDOS on unexpected death + record the crash so it learns ---

def _read_console_tail(config, n=30):
    try:
        lines = (config.workspace / "eidos_console.log").read_text(
            encoding="utf-8", errors="replace").splitlines()
        return "\n".join(lines[-n:])[-1200:]
    except Exception:  # noqa: BLE001
        return "(no console output captured)"


def _watchdog_note(config, msg):
    """Record a crash/recovery note where eiDOS will see it: observation + durable knowledge."""
    import json, time
    try:
        obs = {"tick": 0, "tool": "watchdog", "fail_kind": "crash", "success": False,
               "output": msg[:1500],
               "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
        with open(config.workspace / "observations.jsonl", "a", encoding="utf-8") as f:
            f.write(json.dumps(obs) + "\n")
    except Exception:  # noqa: BLE001
        pass
    try:
        import knowledge
        knowledge.store_entry(config, msg[:600], ["crash", "watchdog", "recovery"], "errors")
    except Exception:  # noqa: BLE001
        pass
    print(f"[watchdog] {msg[:140]}")


def _watchdog_event(config, msg):
    """Append a one-line watchdog event (rollback/standdown) for the operator / babysit check."""
    import time
    try:
        config.state_dir.mkdir(parents=True, exist_ok=True)
        with open(config.state_dir / "watchdog_events.log", "a", encoding="utf-8") as f:
            f.write(time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()) + "  " + str(msg)[:300] + "\n")
    except OSError:
        pass


def _selfedit_probe(config):
    """Resolve or roll back an in-flight self-edit. Called from the watchdog's alive branch.

    Healthy when the booted code dropped its applied_ok breadcrumb (matching id) AND it is
    either paused (awaiting operator GO — the normal post-apply state) or has ticked past the
    pre-apply heartbeat baseline. If the deadline passes without that signal — the new code hung
    mid-boot (no breadcrumb) or wedged-alive (heartbeat never advanced) — revert to prev_sha and
    restart on the reverted code, paused. This is the gap the crash-loop path can't see: a bad
    self-edit that does NOT crash."""
    import selfedit
    pend = selfedit.read_pending_apply(config)
    if not pend:
        return
    crumb = selfedit.read_applied_ok(config)
    booted = bool(crumb and crumb.get("id") == pend.get("id"))
    paused = (config.workspace / "paused").exists()
    hb_ts = _current_heartbeat_ts(config)
    progressed = hb_ts > float(pend.get("baseline_heartbeat_ts", 0) or 0)
    if booted and (paused or progressed):
        selfedit.clear_pending_apply(config)
        _watchdog_event(config, f"self-edit {pend.get('id')} passed health probe "
                                f"({'paused, awaiting GO' if paused else 'ticking on new code'})")
        return
    if time.time() < float(pend.get("deadline_epoch", 0) or 0):
        return  # still within the probe window — keep watching
    prev = pend.get("prev_sha", "")
    res = selfedit.autorollback(config, prev, pend.get("id"))
    with _LIFECYCLE_LOCK:
        newpid = _restart_eidos_keep_armed(config, reason=f"self-edit {pend.get('id')} rolled back")
    why = "never reached run_loop (no breadcrumb)" if not booted else "heartbeat never advanced (wedged)"
    _watchdog_note(config,
        f"Self-edit {pend.get('id')} FAILED its health probe — {why}. Reverted source to "
        f"{prev[:9]} ({res.get('restored', 0)} files) and restarted you (paused) on the reverted "
        f"code as pid {newpid}. The change is rolled back; review the proposal before retrying.")
    _watchdog_event(config, f"HEALTH-PROBE ROLLBACK of self-edit {pend.get('id')} -> {prev[:9]} ({why})")


_COMMISSION_CMD = re.compile(
    r"^/(?:commission|mission)\s+(done|confirm|reject|drop)\s+#?(\d+)\s*(.*)$",
    re.IGNORECASE | re.DOTALL)


def _commission_chat_command(config, message: str):
    """Route an operator VERDICT typed into the normal chat box (`/commission done 3 nice work`,
    `reject`/`drop` likewise; `/mission` is an alias, `done` means confirm). Returns None when the
    message is ordinary chat (it flows to the creature untouched), else a response dict — the
    verdict went to the commission channel (commission.write_verdict), which the eidos-side engine
    settles next tick. The dashboard never touches commission.json itself (single writer)."""
    text = (message or "").strip()
    if not text.lower().startswith(("/commission", "/mission")):
        return None
    if not getattr(config, "pillars_commission_enabled", False):
        return {"ok": False, "error": "the commission organ is not enabled"}
    m = _COMMISSION_CMD.match(text)
    if not m:
        return {"ok": False,
                "error": "usage: /commission done|reject|drop <task-id> [note]"}
    verb, task_id, note = m.group(1).lower(), int(m.group(2)), m.group(3).strip()
    try:
        import commission as _cm
        _cm.write_verdict(config, task_id=task_id,
                          verdict={"done": "confirm"}.get(verb, verb), note=note)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": str(e)}
    return {"ok": True, "routed": "commission",
            "echo": f"task #{task_id}: {verb} recorded — settles next tick"}


def _hatch_beat(config):
    """Loop-side hatch (deferred seam, closed): hatch progress used to advance ONLY inside
    build_creature_spec — i.e. on a browser poll — so with no dashboard tab open the creature
    could cross its hatch XP and never hatch until someone looked. The watchdog beat is the
    dashboard's own loop, so cracks/hatch land on time with zero viewers. Same single writer
    (this process, _CREATURE_LOCK inside _update_hatch), idempotent, and it only touches disk
    on a threshold crossing."""
    try:
        doc = _load_or_create_creature(config)
        if not doc.get("hatched"):
            _update_hatch(config, doc, _read_json(config.workspace / "persona.json"))
    except Exception:  # noqa: BLE001 - the hatch beat must never wound the watchdog
        pass


def _watchdog_loop(config):
    """Supervise eiDOS: when it should be running but has died, record why and respawn it.

    Distinguishes an intentional Stop (eidos.should_run removed) from a crash, and backs
    off if it crash-loops so it never thrashes.
    """
    import time, os, subprocess
    restarts = []
    stuck_restarts = []   # timestamps of stale-heartbeat restarts (bounded so an external cause can't thrash)
    while True:
        try:
            _hatch_beat(config)
            # Event-driven (phase 4b): a spawned child's death fires _child_died instantly;
            # the 5s timeout retains every periodic check (should_run, stability re-arm,
            # children from a previous dashboard run that we have no handle for).
            died = _child_died.wait(timeout=5)
            if died:
                _child_died.clear()
                _pid_cache.clear()   # bypass the liveness cache — react to the death NOW
            if not _eidos_should_run_path(config).exists():
                continue  # operator stopped it — do not resurrect
            # Operator START triggers a full service restart; during that brief window ignore eidos's
            # death (no false "you died" note to the creature, no premature respawn) — the post-restart
            # boot brings it back fresh and paused.
            try:
                _crumb = _eidos_restart_breadcrumb(config)
                if _crumb.exists() and (time.time() - _crumb.stat().st_mtime) < 30:
                    continue
            except OSError:
                pass
            try:
                pid = int((config.workspace / "eidos.pid").read_text().strip())
            except Exception:  # noqa: BLE001
                pid = 0
            if pid and _ctrl_pid_alive(pid):
                # Self-edit HEALTH PROBE: a process being alive isn't proof a just-applied self-edit
                # is healthy — it could be hung mid-boot (never reaching run_loop) or wedged-alive
                # after resume. Resolve when the new code dropped its applied_ok breadcrumb AND is
                # either awaiting operator GO (paused) or ticking (heartbeat past the pre-apply
                # baseline). Roll back to prev_sha if the probe deadline passes without that.
                try:
                    _selfedit_probe(config)
                except Exception as _pe:  # noqa: BLE001 - probe must never crash the watchdog
                    _watchdog_event(config, f"health-probe error: {_pe}")
                # STALE-HEARTBEAT watchdog: alive != ticking. If eidos should be running and isn't
                # paused, but its heartbeat hasn't advanced in eidos_stuck_threshold_s, it's WEDGED —
                # a hung dream, or the LLM persistently timing out (the heartbeat only advances on a
                # SUCCESSFUL tick). Restart it: a fresh eidos recovers from the WAL and, if the LLM is
                # the cause, cleanly waits for /health instead of staying stuck. Bounded (3/30min) so
                # a persistent external cause (LLM down/slow) can't make it thrash.
                try:
                    _now = time.time()
                    _stuck, _stale = _eidos_is_stuck(config, _now)
                    if _stuck:
                        stuck_restarts = [t for t in stuck_restarts if _now - t < 1800]
                        if len(stuck_restarts) < 3:
                            stuck_restarts.append(_now)
                            _watchdog_note(config,
                                f"eiDOS is alive but hasn't completed a tick in {int(_stale)}s — wedged "
                                f"(a hung dream, or the LLM persistently timing out). Auto-restarting you; "
                                f"recover from the WAL and note what stalled so it doesn't recur.")
                            _watchdog_event(config, f"STALE-RESTART (heartbeat {int(_stale)}s old, pid {pid})")
                            if os.name == "nt":
                                subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True, timeout=15)
                            else:
                                _kill_tree_posix(pid)
                            new_pid = _spawn_eidos(config)
                            _child_died.clear(); _pid_cache.clear()
                            _watchdog_event(config, f"stale-restart respawned pid {new_pid}")
                            print(f"[watchdog] STALE-RESTART: killed wedged {pid}, respawned {new_pid} (stale {int(_stale)}s)")
                            continue
                        elif len(stuck_restarts) == 3:
                            stuck_restarts.append(_now)  # log the stand-down once, then go quiet for the window
                            _watchdog_event(config, "STALE-RESTART bound hit (3/30min) — persistent wedge, "
                                            "likely external (LLM down/slow). Holding; eidos waits for /health.")
                except Exception as _se:  # noqa: BLE001 - the stale check must never crash the watchdog
                    _watchdog_event(config, f"stale-check error: {_se}")
                # Healthy. If we auto-rolled-back earlier and eidos has since been stable
                # for >10 min, clear the guard so a *future* unrelated break can recover too.
                try:
                    rb = config.state_dir / "rollback_attempted"
                    if rb.exists() and (time.time() - rb.stat().st_mtime) > 600:
                        rb.unlink()
                        _watchdog_note(config, "eiDOS stable for 10 min after rollback — re-arming auto-recovery.")
                except OSError:
                    pass
                continue  # healthy
            now = time.time()
            restarts = [t for t in restarts if now - t < 180]
            if len(restarts) >= 5:
                # Crash-loop. Likeliest cause is a bad code change (ours or a self-edit).
                # Before standing down, auto-restore last_good and retry on known-good code —
                # the core of unattended overnight resilience. Bounded to 2 attempts so a
                # persistent failure (or a failed respawn) can never loop forever OR die after
                # a single try: the attempt is counted UP FRONT so even a thrown restore counts.
                rb_marker = config.state_dir / "rollback_attempted"
                attempts = 0
                try:
                    attempts = int((rb_marker.read_text() or "0").split(",")[0])
                except (OSError, ValueError):
                    attempts = 0
                if attempts < 2:
                    try:
                        config.state_dir.mkdir(parents=True, exist_ok=True)
                        rb_marker.write_text(f"{attempts + 1},{now}")  # count the attempt up front
                    except OSError:
                        pass
                    try:
                        import git_safety
                        lg = git_safety.read_last_good(config)
                        if lg:
                            with _LIFECYCLE_LOCK:
                                res = git_safety.restore_to(config, lg)
                            _watchdog_note(config,
                                f"eiDOS crash-looped (5x/3min). Auto-restored last good checkpoint {lg} "
                                f"({res.get('restored', 0)} source files) — attempt {attempts + 1}/2 on "
                                f"known-good code. If this recurs the watchdog stands down for the operator.")
                            _watchdog_event(config, f"AUTO-ROLLBACK ({attempts + 1}/2) to {lg}")
                            try:
                                import selfedit
                                selfedit.clear_pending_apply(config)  # last_good IS the pre-apply floor
                            except Exception:  # noqa: BLE001
                                pass
                            restarts = []  # fresh chance on good code
                            new_pid = _spawn_eidos(config)
                            time.sleep(3)
                            alive = _ctrl_pid_alive(new_pid)
                            _watchdog_event(config, f"respawned pid {new_pid} alive={alive}")
                            print(f"[watchdog] rolled back to {lg}, respawned pid {new_pid} alive={alive}")
                        else:
                            _watchdog_event(config, "auto-rollback: no last_good checkpoint available")
                    except Exception as e:  # noqa: BLE001
                        print(f"[watchdog] auto-rollback error: {e}")
                        _watchdog_event(config, f"auto-rollback error: {e}")
                    continue  # retries are bounded by the attempt counter
                # Attempts exhausted (or no checkpoint) and still crash-looping → stand down.
                try:
                    _eidos_should_run_path(config).unlink()
                except OSError:
                    pass
                _watchdog_note(config, "eiDOS crash-looped even after rollback. Watchdog standing "
                                       "down — needs operator attention.")
                _watchdog_event(config, "STAND DOWN — crash-loop persisted after 2 rollbacks")
                continue
            tail = _read_console_tail(config, 30)
            _watchdog_note(config,
                           "eiDOS process died unexpectedly. Last console output before death:\n"
                           + tail + "\n\nThe watchdog is auto-restarting you. Note what happened "
                           "above and adapt so it does not recur.")
            restarts.append(now)
            new_pid = _spawn_eidos(config)
            print(f"[watchdog] respawned eiDOS as pid {new_pid}")
        except Exception:  # noqa: BLE001 — the watchdog must never die
            pass


# --- GPU + LLM telemetry for the dashboard (nvidia-smi + metrics.jsonl tail) ---

def get_gpu_stats(_cache={"t": 0.0, "v": {}}):
    import subprocess, time
    now = time.time()
    if _cache["v"] and now - _cache["t"] < 1.0:
        return _cache["v"]
    v = {}
    try:
        out = subprocess.run(
            ["nvidia-smi",
             "--query-gpu=utilization.gpu,memory.used,memory.total,temperature.gpu,power.draw,name",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=5,
        )
        line = (out.stdout or "").strip().splitlines()[0]
        p = [x.strip() for x in line.split(",")]
        v = {"util": float(p[0]), "mem_used": float(p[1]), "mem_total": float(p[2]),
             "temp": float(p[3]), "power": float(p[4]),
             "name": p[5] if len(p) > 5 else "GPU"}
    except Exception:
        v = {}
    _cache["t"] = now
    _cache["v"] = v
    return v


def _raw_substrate(config):
    """The bottom layer of the Pantheon reveal: the actual hardware numbers the creature never sees.
    Read on the dashboard side (it already polls nvidia-smi), so the felt snapshot stays substrate-free."""
    sub = {"gpu_name": None, "vram_used_gb": None, "vram_total_gb": None, "vram_pct": None,
           "gpu_temp_c": None, "gpu_util": None, "gpu_power_w": None,
           "cpu_pct": None, "ram_pct": None, "disk_free_gb": None}
    try:
        g = get_gpu_stats() or {}
        mu, mt = g.get("mem_used"), g.get("mem_total")
        sub["gpu_name"] = g.get("name")
        sub["vram_used_gb"] = round(mu / 1024.0, 2) if mu is not None else None
        sub["vram_total_gb"] = round(mt / 1024.0, 2) if mt is not None else None
        sub["vram_pct"] = round(mu / mt * 100.0, 1) if (mu and mt) else None
        sub["gpu_temp_c"] = g.get("temp")
        sub["gpu_util"] = g.get("util")
        sub["gpu_power_w"] = g.get("power")
    except Exception:  # noqa: BLE001
        pass
    try:
        sub["cpu_pct"] = round(float(get_cpu_pct()), 1)
    except Exception:  # noqa: BLE001
        pass
    try:
        import safety
        sub["ram_pct"] = round(float(safety.check_ram(100.0)[1]), 1)
    except Exception:  # noqa: BLE001
        pass
    try:
        import safety
        sub["disk_free_gb"] = round(float(safety.check_disk_space(str(config.workspace), 0.0)[1]), 1)
    except Exception:  # noqa: BLE001
        pass
    return sub


def build_why(config, tick_q: str) -> dict:
    """Causal ledger read (Pillars 0.3): the pressure field that produced a tick.

    `/api/why?tick=N` → {"tick": N, "field": {...}} (or field=None if none logged / off).
    `/api/why` with no tick → {"recent": [...]} newest-first, so the panel can list what
    exists. Off by default (feature ships dark): reports enabled=False when the flag is off.
    """
    import pressures
    enabled = bool(getattr(config, "pillars_causal_ledger_enabled", False))
    if tick_q:
        try:
            tick = int(tick_q)
        except (TypeError, ValueError):
            return {"error": "tick must be an integer", "enabled": enabled}
        field = pressures.read_field_by_tick(config, tick)
        return {"tick": tick, "field": field, "enabled": enabled}
    return {"recent": pressures.read_recent_fields(config, n=30), "enabled": enabled}


def build_nervous(config):
    """The 'behind the curtain' view: eiDOS's nervous-system snapshot (felt/mood/organs/bus/feed, written
    by the in-process NervousMonitor) merged with the live RAW substrate the dashboard reads directly.
    age_s tells the UI how fresh the snapshot is (None/large = eidos not ticking)."""
    snap = _read_json(config.nervous_snapshot_path)
    if not isinstance(snap, dict) or not snap:
        snap = None
    age = None
    if snap and snap.get("ts"):
        try:
            age = round(time.time() - float(snap["ts"]), 1)
        except (TypeError, ValueError):
            age = None
    st = _ctrl_status(config)
    # Power comes from the dashboard's OWN cache (it owns the radio), so the panel is live regardless of
    # whether eidos is running — that's the whole point of moving the poll out of the eidos child.
    power, power_age, power_fresh = None, None, None
    try:
        from nervous.power import read_power_cache
        pc = read_power_cache(str(config.power_cache_path),
                              max_age_s=getattr(config, "power_stale_after_s", 600.0))
        if pc:
            power, power_age, power_fresh = pc["reading"], pc["age_s"], pc["fresh"]
    except Exception:  # noqa: BLE001
        pass
    return {
        "snapshot": snap,
        "substrate": _raw_substrate(config),
        "age_s": age,
        "running": bool(st.get("running")),
        "paused": bool(st.get("paused")),
        "enabled": bool(getattr(config, "nervous_enabled", False)),
        "power": power,            # live battery/solar from the dashboard's poll (eidos-independent)
        "power_age_s": power_age,
        "power_fresh": power_fresh,
    }


def get_llm_stats(config, _cache={"t": 0.0, "v": {}}):
    import time
    now = time.time()
    if _cache["v"] and now - _cache["t"] < 1.0:
        return _cache["v"]
    v = {}
    try:
        p = config.workspace / "metrics.jsonl"
        with open(p, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - 4096))
            tail = f.read().decode("utf-8", "replace")
        lines = [ln for ln in tail.splitlines() if ln.strip()]
        if lines:
            m = json.loads(lines[-1])
            ct = m.get("completion_tokens", 0) or 0
            el = m.get("llm_elapsed_s", 0) or 0
            v = {"tok_s": round(ct / el, 1) if el > 0 else 0,
                 "completion_tokens": ct,
                 "prompt_tokens": m.get("prompt_tokens", 0),
                 "llm_elapsed_s": round(el, 2),
                 "tick": m.get("tick", 0)}
    except Exception:
        v = {}
    _cache["t"] = now
    _cache["v"] = v
    return v


def main():
    parser = argparse.ArgumentParser(description="eiDOS dashboard server")
    parser.add_argument("--config", default="config.toml", help="Path to config file")
    parser.add_argument("--port", type=int, default=None, help="Override dashboard port")
    args = parser.parse_args()

    config = load_config(args.config)
    port = args.port or config.dashboard_port

    # Boot-reconcile a dangling self-edit health probe BEFORE arming the watchdog: if the
    # dashboard itself restarted mid-probe and the deadline has since passed without a healthy
    # signal, roll the edit back now rather than leaving it unresolved (the marker outlives any
    # one dashboard process — it's in state_dir).
    try:
        import selfedit
        _pend = selfedit.read_pending_apply(config)
        if _pend and time.time() >= float(_pend.get("deadline_epoch", 0) or 0):
            _crumb = selfedit.read_applied_ok(config)
            if not (_crumb and _crumb.get("id") == _pend.get("id")):
                res = selfedit.autorollback(config, _pend.get("prev_sha", ""), _pend.get("id"))
                _watchdog_event(config, f"BOOT-RECONCILE rolled back stranded self-edit "
                                        f"{_pend.get('id')} -> {_pend.get('prev_sha','')[:9]} "
                                        f"({res.get('restored',0)} files)")
            else:
                selfedit.clear_pending_apply(config)  # it had booted OK; just clear the stale marker
    except Exception as _bre:  # noqa: BLE001
        print(f"[dashboard] boot-reconcile error: {_bre}")

    # Boot-respawn eiDOS so an operator START (which restarts this whole service) — or any other
    # dashboard restart while eidos should be running — brings the consciousness loop back WITHOUT a
    # second click. Done BEFORE the watchdog arms, so there's no respawn race and no false "you died"
    # note reaches the creature. An operator-restart breadcrumb forces FRESH code: any eidos that
    # survived the restart is killed first so it can't keep running stale code; a plain restart instead
    # preserves a surviving eidos (continuity) and only respawns if it actually died.
    try:
        import subprocess
        _crumb = _eidos_restart_breadcrumb(config)
        _intentional = _crumb.exists()
        if _intentional:
            try:
                _crumb.unlink()
            except OSError:
                pass
        if _eidos_should_run_path(config).exists():
            try:
                _live = int((config.workspace / "eidos.pid").read_text().strip())
            except Exception:  # noqa: BLE001
                _live = 0
            _alive = bool(_live) and _ctrl_pid_alive(_live)
            if _intentional and _alive:
                if os.name == "nt":
                    subprocess.run(["taskkill", "/PID", str(_live), "/T", "/F"], capture_output=True, timeout=15)
                else:
                    _kill_tree_posix(_live)
                _alive = False
            if not _alive:
                _bp = _spawn_eidos(config)
                _paused = (config.workspace / "paused").exists()
                print(f"[dashboard] boot-respawn eiDOS pid {_bp} "
                      f"({'PAUSED' if _paused else 'running'}{' · operator restart' if _intentional else ''})")
    except Exception as _bre2:  # noqa: BLE001 — boot must proceed even if respawn fails
        print(f"[dashboard] boot-respawn error: {_bre2}")

    handler = _make_handler(config)
    server = ThreadingHTTPServer(("0.0.0.0", port), handler)
    server.daemon_threads = True
    global _HTTP_SERVER
    _HTTP_SERVER = server   # so a control action can gracefully stop the server to restart the service
    import threading
    threading.Thread(target=_watchdog_loop, args=(config,), daemon=True).start()
    print("[watchdog] armed — eiDOS auto-restart-on-crash enabled")

    # Power: the always-on dashboard owns the SINGLE Renogy BLE poll and writes a shared cache, so
    # battery/solar stays live on the behind-the-curtain panel even when eidos is paused/stopped — and
    # eidos consumes that same cache (one radio owner, no contention). Guarded: a BLE/bleak fault, or a
    # busy device (Dean on the Renogy app), must never wound the dashboard — it just leaves the cache stale.
    if getattr(config, "power_enabled", False) and getattr(config, "power_mppt_address", ""):
        try:
            from nervous.power import PowerMonitor, default_reader
            from nervous.battery_profiler import BatteryProfiler
            config.state_dir.mkdir(parents=True, exist_ok=True)
            # The profiler learns this pack's true 0→100 over time (voltage+coulomb fusion) and persists
            # OUTSIDE the workspace, so it keeps calibrating 24/7 (even when eidos sleeps) and survives
            # wipes. It wraps the BLE reader: read → learn → enrich the reading's SOC → cache.
            _profiler = BatteryProfiler(
                path=str(config.battery_profile_path),
                cells=int(getattr(config, "power_battery_cells", 8) or 8),
                r_internal=float(getattr(config, "power_battery_r_internal", 0.015) or 0.015),
                capacity_nameplate_ah=float(getattr(config, "power_battery_capacity_ah", 100.0) or 100.0))
            _base_reader = default_reader(config)
            PowerMonitor(None, config=config, metabolism=None,
                         reader=lambda: _profiler.ingest(_base_reader()),
                         cache_path=str(config.power_cache_path),
                         interval_s=getattr(config, "power_poll_interval_s", 60.0),
                         stale_after_s=getattr(config, "power_stale_after_s", 600.0),
                         backoff_max_s=getattr(config, "power_backoff_max_s", 600.0)).start()
            print(f"[dashboard] power poller started — owns the Renogy BLE radio + learns the battery "
                  f"profile; live even when eidos is stopped")
        except Exception as _pe:  # noqa: BLE001
            print(f"[dashboard] power poller start failed (continuing): {_pe}")
    print(f"[dashboard] Serving on http://0.0.0.0:{port}")
    print(f"[dashboard] Reading from {config.workspace}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[dashboard] Shutting down")
        server.shutdown()


if __name__ == "__main__":
    main()
