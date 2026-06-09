#!/usr/bin/env python3
"""eiDOS — autonomous LLM supervisor for Raspberry Pi.

Entry point: crash recovery, tick loop, signal handling, session detection.
"""

import argparse
import collections
import hashlib
import json
import logging
import os
import re
import signal
import subprocess
import sys
import time
from pathlib import Path

from config import Config, load_config
from atomicio import replace_with_retry
from context import assemble_context, _norm_cmd
from compaction import should_compact, compact, compact_briefing, emit_flavor
from llm import complete, LLMError, ReasoningExhausted
from memory import (
    append_observation,
    append_thought,
    read_goal,
    read_subgoals,
    validate_observations,
    write_memory,
)
from parser import parse_tool_call, parse_reply
from persona import (
    load_persona,
    save_persona,
    record_tick,
    record_compaction,
    record_goal_complete,
    record_error_recovery,
    compute_traits,
    compute_mood,
    check_titles,
    format_prefix,
    format_status_line,
)
from rotation import rotate_if_needed, cleanup_old_archives, rotate_llm_log, rotate_metrics, cleanup_old_snapshots
from safety import check_ram, get_cpu_temp, kill_child_processes, check_disk_space
from session import human_present, take_workspace_snapshot, workspace_diff
from telemetry import write_heartbeat, append_metrics, write_activity, get_cpu_pct
from tools import execute_tool, refresh_jobs, collect_finished_jobs, reap_jobs

logger = logging.getLogger("eidos")


# --- Globals for signal handling ---
_shutdown_requested = False


def _handle_signal(signum, frame):
    global _shutdown_requested
    _shutdown_requested = True


def main():
    parser = argparse.ArgumentParser(description="eiDOS autonomous supervisor")
    parser.add_argument("--config", default="config.toml", help="Path to config file")
    parser.add_argument("--llm-url", default=None, help="Override LLM endpoint URL")
    args = parser.parse_args()

    config = load_config(args.config)
    if args.llm_url:
        config.llm_url = args.llm_url

    # Configure logging
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )

    # Ensure workspace exists
    config.workspace.mkdir(parents=True, exist_ok=True)
    config.interventions_dir.mkdir(parents=True, exist_ok=True)
    config.snapshots_dir.mkdir(parents=True, exist_ok=True)
    config.outputs_dir.mkdir(parents=True, exist_ok=True)

    # Hot-load any skills Nexus has previously authored
    try:
        from skills import load_active_skills
        loaded = load_active_skills(config)
        if loaded:
            print(f"[skills] loaded {len(loaded)}: {', '.join(loaded)}")
    except Exception as e:  # noqa: BLE001
        print(f"[skills] load failed: {e}")

    # Reap any background jobs orphaned by the previous run (bg_run/async detach into their own
    # process group, so they survive a kill of eidos and would otherwise run forever).
    try:
        n = reap_jobs(config, kill_all=True)
        if n:
            print(f"[jobs] reaped {n} orphaned background job(s) from the previous run")
    except Exception as e:  # noqa: BLE001
        print(f"[jobs] reap failed: {e}")

    # Signal handling for clean shutdown
    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    # Crash recovery
    wal = recover(config)

    # Load persona
    persona = None
    if config.persona_enabled:
        persona = load_persona(config.workspace)
        compute_traits(persona)
        pfx = format_prefix(persona)
        print(f"{pfx} Online. {format_status_line(persona)}")

    # Main loop
    run_loop(config, persona, wal=wal)


def _pfx(persona, config):
    """Return persona prefix or fallback."""
    if config.persona_enabled and persona:
        return format_prefix(persona)
    return "[eidos]"


def _write_chat_reply(config: Config, tick_number: int, reply_text: str):
    """Append a chat reply to chat_replies.jsonl."""
    path = config.workspace / "chat_replies.jsonl"
    entry = json.dumps({
        "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "tick": tick_number,
        "text": reply_text[:2000],
    })
    with open(path, "a") as f:
        f.write(entry + "\n")


def _auto_speak(config: Config, text: str) -> None:
    """Voice an outgoing chat reply through the dashboard so Boss HEARS every response — the voice is
    first-class, not opt-in. Best-effort and short-only (long replies stay text; the GPU is shared, so a
    paragraph of TTS would lag). Also the backstop: if the model hedges with text instead of calling
    `speak`, this speaks it anyway."""
    t = (text or "").strip()
    if not t or len(t) > 320:
        return
    try:
        import urllib.request as _u
        sid = str(int(time.time() * 1000))
        req = _u.Request("http://127.0.0.1:8099/api/speech/say",
                         data=json.dumps({"id": sid, "text": t}).encode("utf-8"),
                         headers={"Content-Type": "application/json"}, method="POST")
        _u.urlopen(req, timeout=4).read()
    except Exception:  # noqa: BLE001 - voice is best-effort; never disturb the tick
        pass


def _has_pending_interventions(config: Config) -> bool:
    """Check if any un-consumed intervention files exist."""
    idir = config.interventions_dir
    if not idir.exists():
        return False
    for p in idir.iterdir():
        if not p.name.startswith(".") and p.suffix != ".done":
            return True
    return False


def _chat_hold_active(config: Config) -> bool:
    """Listening hold: True when Dean has the chat box focused (a soft pause distinct from
    the operator pause). The dashboard owns the flag file; eiDOS only reads it. Fails OPEN
    to autonomy on any anomaly (missing, corrupt, stale, backward clock, ceiling exceeded).
    A pending intervention overrides the hold so a sent message is answered immediately.
    """
    try:
        path = config.chat_hold_path
        raw = path.read_text(encoding="utf-8", errors="replace")
        import json as _json
        d = _json.loads(raw)
        if not d.get("held"):
            return False
        now = time.time()
        ts = float(d.get("ts", 0) or 0)
        try:
            mtime = path.stat().st_mtime
        except OSError:
            mtime = ts
        age = now - max(ts, mtime)            # freshest of payload ts / file mtime
        if age < 0:                            # backward clock → treat as stale
            return False
        if age > float(config.chat_hold_ttl_s):
            return False
        first = float(d.get("first_held_ts", ts) or ts)
        if now - first > float(config.chat_hold_max_continuous_s):
            return False                       # hard ceiling — never pin the loop forever
        if _has_pending_interventions(config):
            return False                       # a message is waiting — go answer it
        return True
    except (FileNotFoundError, ValueError, OSError):
        return False


def _interruptible_sleep(config: Config, interval: float = None):
    """Sleep for `interval` (default tick_interval_s), waking early on shutdown."""
    target = config.tick_interval_s if interval is None else float(interval)
    elapsed = 0.0
    poll = min(2.0, max(0.1, target))
    while elapsed < target:
        remaining = target - elapsed
        nap = min(poll, remaining)
        time.sleep(nap)
        elapsed += nap
        if _shutdown_requested:
            break
        if _has_pending_interventions(config):
            logger.info("Early wake: pending intervention detected")
            break
        if _chat_hold_active(config):
            break  # reach the listening gate promptly


def _adaptive_tick_interval(config: Config, tick_tool_name: str) -> float:
    """Fast cadence when there's MOMENTUM (a real action was just taken, or background jobs are
    still running → results are coming), idle cadence otherwise. A flat sleep throttles an actively
    working agent and wastes cycles when idle; this reacts to work, not a metronome."""
    active = bool(tick_tool_name) and tick_tool_name not in ("thought", "__no_tool__")
    if not active:
        try:
            from tools import _read_jobs
            active = any(j.get("status") == "running" for j in _read_jobs(config))
        except Exception:  # noqa: BLE001
            pass
    return float(getattr(config, "tick_interval_active_s", 0.4)) if active else float(config.tick_interval_s)


def _count_skills(config: Config) -> int:
    """Count authored skill files (for the goal-tension progress signal — a new skill = progress)."""
    try:
        return len([p for p in (config.workspace / "skills").glob("*.py")])
    except Exception:  # noqa: BLE001
        return 0


def write_wal(config: Config, tick_number: int, ticks_since_compaction: int,
              goal_start_time: float, consecutive_failures: int = 0,
              reasoning_exhaustions: int = 0, current_max_tokens: int = 0):
    """Atomically write tick state to WAL for crash recovery."""
    wal = {
        "tick_number": tick_number,
        "ticks_since_compaction": ticks_since_compaction,
        "goal_start_time": goal_start_time,
        "consecutive_failures": consecutive_failures,
        "reasoning_exhaustions": reasoning_exhaustions,
        "current_max_tokens": current_max_tokens,
        "ts": time.time(),
    }
    tmp = config.wal_path.with_suffix(".tmp")
    tmp.write_text(json.dumps(wal))
    replace_with_retry(tmp, config.wal_path)


def read_wal(config: Config) -> dict:
    """Read WAL state, return empty dict on missing/corrupt."""
    try:
        return json.loads(config.wal_path.read_text())
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}


def clear_wal(config: Config):
    """Remove WAL after clean shutdown."""
    try:
        config.wal_path.unlink()
    except FileNotFoundError:
        pass


def attempt_llm_restart(config: Config) -> bool:
    """Try to restart the local LLM process. Returns True on success."""
    cmd = config.llm_restart_cmd
    if not cmd:
        logger.warning("No llm_restart_cmd configured — cannot self-heal")
        return False
    logger.info("Attempting LLM restart: %s", cmd)
    try:
        result = subprocess.run(
            cmd, shell=True, timeout=60, capture_output=True, text=True)
        if result.returncode == 0:
            logger.info("LLM restart succeeded")
            time.sleep(10)  # give the model time to load
            return True
        logger.error("LLM restart failed (rc=%d): %s", result.returncode, result.stderr[:500])
        return False
    except subprocess.TimeoutExpired:
        logger.error("LLM restart timed out after 60s")
        return False
    except Exception as e:
        logger.error("LLM restart error: %s", e)
        return False


def recover(config: Config) -> dict:
    """Crash recovery: validate state, fix corruption, log restart.
    Returns WAL state dict (may be empty on fresh start).
    """
    print("[eidos] Running crash recovery...")

    # 0. Read WAL (tick state from before crash)
    wal = read_wal(config)
    if wal:
        print(f"[eidos] WAL recovered: tick={wal.get('tick_number')}, "
              f"compaction_gap={wal.get('ticks_since_compaction')}")

    # 1. Verify goal.md
    goal = read_goal(config)
    if not goal:
        print("[eidos] WARNING: No goal.md found. Agent will idle until one is created.")

    # 2. Create memory.md if missing, or restore from snapshot if empty
    mem_missing = not config.memory_path.exists()
    mem_empty = False
    if not mem_missing:
        try:
            mem_empty = config.memory_path.stat().st_size == 0
        except OSError:
            mem_empty = True

    if mem_missing or mem_empty:
        # Try restoring from most recent snapshot
        restored = False
        if config.snapshots_dir.exists():
            snapshots = sorted(
                config.snapshots_dir.glob("memory_snapshot_*"),
                key=lambda p: p.stat().st_mtime,
                reverse=True,
            )
            if snapshots:
                try:
                    content = snapshots[0].read_text()
                    if content.strip():
                        write_memory(config, content)
                        restored = True
                        print(f"[eidos] Restored memory.md from snapshot: {snapshots[0].name}")
                        append_observation(config, {
                            "tick": 0,
                            "tool": "system",
                            "success": True,
                            "output": f"Restored memory from snapshot {snapshots[0].name} after {'missing' if mem_missing else 'empty'} memory.md.",
                        })
                except OSError:
                    pass
        if not restored:
            write_memory(config, "# Working Memory\nFresh start. No prior context.")
            print("[eidos] Created initial memory.md")

    # 3. Validate observations.jsonl
    truncated = validate_observations(config)
    if truncated:
        print(f"[eidos] Truncated {truncated} malformed line(s) from observations.jsonl")
        append_observation(config, {
            "tick": 0,
            "tool": "system",
            "success": False,
            "output": (f"Crash recovery: {truncated} corrupted observation(s) "
                       f"removed from observations.jsonl. Recent history may be incomplete."),
        })

    # 4. Scan background jobs, mark dead ones
    jobs = refresh_jobs(config)
    dead = [j for j in jobs if j["status"] != "running"]
    if dead:
        print(f"[eidos] Found {len(dead)} completed/dead background jobs")
        dead_names = ", ".join(j.get("cmd", "?")[:60] for j in dead)
        append_observation(config, {
            "tick": 0,
            "tool": "system",
            "success": False,
            "output": (f"Background jobs died during downtime: {dead_names}. "
                       f"Their results are unavailable. Re-launch if still needed."),
        })

    # 5. Log recovery with full crash context
    if wal:
        recovery_detail = (
            f"eiDOS recovered from crash. Resuming at tick {wal.get('tick_number', '?')}. "
            f"State before crash: {wal.get('consecutive_failures', 0)} consecutive LLM failures, "
            f"{wal.get('reasoning_exhaustions', 0)} reasoning exhaustions, "
            f"max_tokens was {wal.get('current_max_tokens', config.llm_max_tokens)}. "
            f"Review recent observations — the last action may not have completed."
        )
    else:
        recovery_detail = "eiDOS starting fresh. No prior crash state found."
    append_observation(config, {
        "tick": 0,
        "tool": "system",
        "success": True,
        "output": recovery_detail,
    })

    # 6. Rotate logs and clean old archives
    if rotate_if_needed(config):
        print("[eidos] Rotated observations.jsonl")
    deleted = cleanup_old_archives(config)
    if deleted:
        print(f"[eidos] Cleaned {deleted} old archive(s)")

    return wal


_THOUGHT_TAG_RE = re.compile(r"<tool>.*?</tool>|<args>.*?</args>|<reply>.*?</reply>",
                             re.DOTALL | re.IGNORECASE)


def _extract_thought(response: str) -> str:
    """This tick's reasoning — the model's raw output minus the action/reply tags."""
    if not response:
        return ""
    return _THOUGHT_TAG_RE.sub("", response).strip()


def run_loop(config: Config, persona=None, wal=None):
    """Main tick loop with session detection and compaction."""
    global _shutdown_requested

    # Restore state from WAL or start fresh
    wal = wal or {}
    tick_number = wal.get("tick_number", 1)
    ticks_since_compaction = wal.get("ticks_since_compaction", 0)
    goal_start_time = wal.get("goal_start_time", time.time())
    consecutive_failures = wal.get("consecutive_failures", 0)
    reasoning_exhaustions = wal.get("reasoning_exhaustions", 0)
    current_max_tokens = wal.get("current_max_tokens", 0) or config.llm_max_tokens
    recent_hashes: collections.deque = collections.deque(maxlen=config.loop_detect_window)
    standby = False
    standby_snapshot = None
    goal_complete = False
    last_tick_failed = False
    ticks_since_goal_complete = None  # None = never
    idle_since = None  # timestamp when goal went missing
    operator_paused = False
    listening_since = None  # set while the chat-focus "listening" hold is engaged
    loop_start = time.monotonic()
    last_goal_hash = None  # track goal changes
    # Goal-tension: ticks since REAL progress (a novel fact learned, a new skill, a Boss exchange).
    # Near-dup dedup means the knowledge count only rises on genuinely new facts → a clean signal.
    last_progress_tick = wal.get("last_progress_tick", tick_number)
    try:
        import knowledge as _kn
        prev_knowledge_count = _kn.count_entries(config)
    except Exception:  # noqa: BLE001
        prev_knowledge_count = 0
    prev_skill_count = _count_skills(config)
    # Objective backlog (Ventral Striatum / Action Gate): seed the open commitments once, then the
    # gate rotates focus among them each tick so a stalled task never starves the rest of the system.
    try:
        import objectives as _obj
        _obj.ensure_seeded(config, tick_number)
    except Exception as _e:  # noqa: BLE001
        logger.warning("objective seed failed: %s", _e)

    pfx = _pfx(persona, config)
    print(f"{pfx} Starting tick loop (interval={config.tick_interval_s}s, mock={config.mock_mode})")

    # --- Wait for LLM health before entering tick loop (cold-boot safety) ---
    if not config.mock_mode:
        import urllib.request as _ur
        health_url = config.llm_url.rstrip("/") + "/health"
        print(f"{pfx} Waiting for LLM server health at {health_url}")
        _health_wait = 0
        while not _shutdown_requested:
            try:
                with _ur.urlopen(health_url, timeout=5) as _resp:
                    _data = json.loads(_resp.read().decode())
                    if _data.get("status") == "ok":
                        print(f"{pfx} LLM server healthy (waited {_health_wait}s)")
                        break
            except Exception:
                pass
            _health_wait += 5
            if _health_wait % 60 == 0:
                print(f"{pfx} Still waiting for LLM server... ({_health_wait}s)")
            time.sleep(5)

    while not _shutdown_requested and not goal_complete:
        # --- Session detection (skip in mock mode) ---
        if not config.mock_mode:
            if human_present():
                if not standby:
                    print(f"{pfx} Human detected — entering standby")
                    standby_snapshot = take_workspace_snapshot(config)
                    append_observation(config, {
                        "tick": tick_number,
                        "tool": "system",
                        "success": True,
                        "output": "Human SSH session detected. Entering standby.",
                    })
                    standby = True
                time.sleep(30)  # Check every 30s while in standby
                continue
            elif standby:
                # Human left — resume after grace period
                print(f"{pfx} Human gone — resuming in {config.grace_period_s}s")
                time.sleep(config.grace_period_s)
                standby = False

                # Generate workspace diff
                if standby_snapshot:
                    diff = workspace_diff(config, standby_snapshot)
                    if diff:
                        append_observation(config, {
                            "tick": tick_number,
                            "tool": "system",
                            "success": True,
                            "output": diff,
                        })
                        print(f"{pfx} Workspace changes detected on resume")
                    standby_snapshot = None

        # --- Operator pause check ---
        pause_path = config.workspace / "paused"
        if pause_path.exists():
            if not operator_paused:
                print(f"{pfx} Operator paused — waiting for resume")
                append_observation(config, {
                    "tick": tick_number,
                    "tool": "system",
                    "success": True,
                    "output": "Paused by operator via dashboard. Tick loop suspended.",
                })
                operator_paused = True
            listening_since = None  # operator pause supersedes a soft listening hold
            time.sleep(5)
            continue
        elif operator_paused:
            print(f"{pfx} Resuming from operator pause")
            append_observation(config, {
                "tick": tick_number,
                "tool": "system",
                "success": True,
                "output": "Resumed by operator. Resuming tick loop.",
            })
            operator_paused = False

        # --- Listening hold (soft pause: Dean has the chat box focused) ---
        # Distinct from operator pause. The in-flight tick already finished; we simply do
        # NOT start a new generation while Dean is composing. Fails open to autonomy.
        if _chat_hold_active(config):
            if listening_since is None:
                listening_since = time.time()
                logger.info("Listening hold engaged (Dean focused chat) — quieting the loop")
            held_s = int(time.time() - listening_since)
            write_activity(config, "listening", detail=f"listening to Dean ({held_s}s)")
            time.sleep(2.0)
            continue
        elif listening_since is not None:
            logger.info("Listening hold released — resuming autonomous loop")
            listening_since = None

        # --- Check for goal ---
        goal = read_goal(config)
        if not goal:
            if idle_since is None:
                idle_since = time.time()
            if config.mock_mode:
                print("[eidos] No goal.md — exiting (mock mode)")
                break
            _interruptible_sleep(config)
            continue
        else:
            idle_since = None

        # --- Goal change detection (hash tracking only) ---
        import hashlib
        goal_hash = hashlib.md5(goal.encode()).hexdigest()
        goal_changed = last_goal_hash is not None and goal_hash != last_goal_hash
        fresh_goal = last_goal_hash is None and not read_subgoals(config)
        if goal_changed:
            goal_start_time = time.time()
        last_goal_hash = goal_hash

        # --- Compaction check ---
        tick_compacted = False
        if should_compact(config, ticks_since_compaction):
            print(f"{pfx} Dreaming... consolidating memories.")
            write_activity(config, "dreaming", detail="consolidating memories")
            try:
                if config.briefing_model:
                    compact_briefing(config, persona=persona)
                else:
                    compact(config, persona=persona)
                emit_flavor(config, persona)
                ticks_since_compaction = 0
                tick_compacted = True
                if persona and config.persona_enabled:
                    record_compaction(persona)
                    pfx = _pfx(persona, config)
                print(f"{pfx} Memories consolidated.")
            except LLMError as e:
                print(f"{pfx} Compaction failed: {e}")
                append_observation(config, {
                    "tick": tick_number,
                    "tool": "dream",
                    "success": False,
                    "output": f"Compaction failed: {e}",
                })

        # --- RAM check ---
        ram_ok, ram_pct = check_ram(config.ram_max_pct)
        if not ram_ok:
            killed = kill_child_processes()
            append_observation(config, {
                "tick": tick_number,
                "tool": "system",
                "success": False,
                "output": f"RAM pressure ({ram_pct:.0f}%), killed {killed} child process(es)",
            })
            print(f"{pfx} RAM pressure: {ram_pct:.0f}%, killed children")

        # --- Auto-plan on fresh/changed goal (call plan_goal directly) ---
        # OFF by default: auto-decomposition drifted into platform-contradicting subgoals (e.g.
        # "build a chat listener", "build a memory database") that became the most-salient — and
        # wrong — "current task" every tick. The single source of objective is now goal.md's
        # Immediate focus + the agent's own update_plan, surfaced as one "## Current focus" block.
        if (goal_changed or fresh_goal) and getattr(config, "auto_subgoals", False):
            label = "NEW GOAL DETECTED" if goal_changed else "FRESH GOAL — no subgoals exist"
            print(f"{pfx} {label} — auto-generating subgoals")
            write_activity(config, "planning", detail="breaking goal into subgoals")
            try:
                from tools import tool_plan_goal
                plan_result = tool_plan_goal(
                    {"goal": goal[:500], "context": "auto-plan on fresh goal"},
                    config,
                )
                append_observation(config, {
                    "tick": tick_number,
                    "tool": "plan_goal",
                    "success": plan_result.success,
                    "output": plan_result.output[:500],
                })
                if plan_result.success:
                    print(f"{pfx} Subgoals generated ({plan_result.duration_s:.0f}s)")
                else:
                    print(f"{pfx} Auto-plan failed: {plan_result.output[:100]}")
            except Exception as e:
                print(f"{pfx} Auto-plan error: {e}")
                append_observation(config, {
                    "tick": tick_number,
                    "tool": "plan_goal",
                    "success": False,
                    "output": f"Auto-plan failed: {e}",
                })

        # --- Loop detection ---
        loop_detected = False
        repeat_count = 0
        if len(recent_hashes) >= config.loop_detect_window:
            uniq = set(recent_hashes)
            if len(uniq) <= 2:
                loop_detected = True   # repeating one action, or cycling between two (A-B-A-B)
                repeat_count = len(recent_hashes)
            elif all(str(h).startswith("th_") or h == "__no_tool__" for h in recent_hashes):
                loop_detected = True   # ruminating: thinking without acting
                repeat_count = len(recent_hashes)

        # --- Deliver async tool results that finished since last tick ---
        # Fire-and-forget bash dispatches land here when done, tagged [↩ job N], and flow
        # into context as normal result-turns so the model pairs them with its dispatch.
        try:
            for fin in collect_finished_jobs(config):
                status = fin.get("status")
                ok = "OK" if status == "completed" else ("TIMED OUT" if status == "timed_out" else "FAILED")
                cmd_s = (fin.get("cmd") or "")[:70]
                body = (fin.get("tail") or "").strip() or "(no output)"
                intent = fin.get("intent")
                intent_s = f" (you wanted: {intent})" if intent else ""
                append_observation(config, {
                    "tick": tick_number,
                    "tool": "async_result",
                    "args": {"job": fin.get("name")},
                    "success": status == "completed",
                    "output": f"[↩ job {fin.get('name')} · {cmd_s} · {ok}]{intent_s}\n{body}",
                })
        except Exception as e:  # noqa: BLE001
            logger.warning("async result delivery failed: %s", e)

        # --- Assemble context ---
        # `tension` is now the ACTIVE objective's frustration (the gate's per-objective counter),
        # falling back to the legacy global stall count if the backlog isn't available.
        try:
            import objectives as _obj
            _act = _obj.get_active(config)
            tension = int(_act["frustration"]) if _act else max(0, tick_number - last_progress_tick)
        except Exception:  # noqa: BLE001
            tension = max(0, tick_number - last_progress_tick)
        messages = assemble_context(
            config,
            tick_number=tick_number,
            goal_start_time=goal_start_time,
            loop_detected=loop_detected,
            repeat_count=repeat_count,
            tension=tension,
        )

        # Log context size for monitoring
        ctx_chars = sum(len(m["content"]) for m in messages)
        ctx_tokens_est = int(ctx_chars / config.chars_per_token)
        print(f"{pfx} Tick {tick_number}: ctx={ctx_chars} chars ~{ctx_tokens_est} tokens")

        # --- Mock mode: print context ---
        if config.mock_mode:
            print(f"\n{'='*60}")
            print(f"TICK {tick_number}")
            print(f"{'='*60}")
            for msg in messages:
                role = msg["role"].upper()
                content = msg["content"]
                print(f"\n--- {role} ---")
                print(content[:2000] if len(content) > 2000 else content)

        # --- LLM call ---
        get_cpu_pct()  # prime CPU counter so post-LLM read captures active period
        llm_start = time.monotonic()
        tick_tool_name = ""
        tick_tool_success = False
        tick_tool_duration = 0.0
        write_activity(config, "thinking", detail=f"tick {tick_number}")

        def _on_token(partial_text):
            write_activity(config, "thinking", detail=f"tick {tick_number}",
                           partial=partial_text)

        try:
            response = complete(messages, config, max_tokens=current_max_tokens,
                                on_token=_on_token, tick=tick_number)
            llm_elapsed = time.monotonic() - llm_start
            consecutive_failures = 0  # reset on success

            # Successful content — decay max_tokens back toward baseline
            if current_max_tokens > config.llm_max_tokens:
                current_max_tokens = max(
                    config.llm_max_tokens,
                    current_max_tokens - config.llm_token_backoff_step,
                )
            reasoning_exhaustions = 0

        except ReasoningExhausted as e:
            llm_elapsed = time.monotonic() - llm_start
            reasoning_exhaustions += 1

            # Bump max_tokens for next tick (up to ceiling)
            current_max_tokens = min(
                current_max_tokens + config.llm_token_backoff_step,
                config.llm_max_tokens_ceiling,
            )
            logger.warning(
                "Reasoning exhausted (%d/%d tokens, attempt %d). "
                "Next tick max_tokens=%d.",
                e.reasoning_tokens, e.max_tokens,
                reasoning_exhaustions, current_max_tokens,
            )

            append_observation(config, {
                "tick": tick_number,
                "tool": "system",
                "success": False,
                "output": (
                    f"Token budget exhausted by reasoning "
                    f"({e.reasoning_tokens}/{e.max_tokens} tokens used, 0 content). "
                    f"Next tick budget raised to {current_max_tokens}. "
                    f"Keep your thinking brief and go straight to the tool call."
                ),
            })

            # After repeated exhaustions, force compaction to shrink context
            if (reasoning_exhaustions >= config.llm_reasoning_exhaust_compaction_trigger
                    and ticks_since_compaction > 0):
                logger.warning(
                    "Forcing compaction after %d consecutive reasoning exhaustions",
                    reasoning_exhaustions)
                try:
                    if config.briefing_model:
                        compact_briefing(config, persona=persona)
                    else:
                        compact(config, persona=persona)
                    ticks_since_compaction = 0
                    if persona and config.persona_enabled:
                        record_compaction(persona)
                        pfx = _pfx(persona, config)
                except LLMError as ce:
                    logger.error("Forced compaction failed: %s", ce)

            write_wal(config, tick_number, ticks_since_compaction,
                      goal_start_time, consecutive_failures,
                      reasoning_exhaustions, current_max_tokens)
            time.sleep(config.tick_interval_s)
            tick_number += 1
            ticks_since_compaction += 1
            continue

        except LLMError as e:
            llm_elapsed = time.monotonic() - llm_start
            consecutive_failures += 1
            print(f"{pfx} LLM error on tick {tick_number} "
                  f"({consecutive_failures}/{config.llm_max_consecutive_failures}): {e}")
            append_observation(config, {
                "tick": tick_number,
                "tool": "llm_error",
                "success": False,
                "output": f"LLM call failed ({consecutive_failures}x): {e}",
            })

            # Self-healing: restart LLM after too many consecutive failures
            if consecutive_failures >= config.llm_max_consecutive_failures:
                print(f"{pfx} Too many consecutive LLM failures — attempting restart")
                if attempt_llm_restart(config):
                    consecutive_failures = 0
                    append_observation(config, {
                        "tick": tick_number,
                        "tool": "system",
                        "success": True,
                        "output": "LLM process restarted after repeated failures.",
                    })
                    if persona and config.persona_enabled:
                        record_error_recovery(persona)

            write_wal(config, tick_number, ticks_since_compaction,
                      goal_start_time, consecutive_failures,
                      reasoning_exhaustions, current_max_tokens)
            time.sleep(config.tick_interval_s)
            tick_number += 1
            ticks_since_compaction += 1
            continue

        if config.mock_mode:
            print(f"\n--- RESPONSE ---")
            print(response)

        # --- Parse reply (chat response to operator) ---
        reply_text = parse_reply(response)
        if reply_text:
            _write_chat_reply(config, tick_number, reply_text)
            print(f"{pfx} Tick {tick_number}: chat reply sent ({len(reply_text)} chars)")

        # --- Capture this tick's reasoning as a thought (the continuity chain) ---
        thought = _extract_thought(response)
        if thought:
            append_thought(config, tick_number, thought)

        # --- Parse tool call ---
        call = parse_tool_call(response)
        # Auto-speak: voice the reply so Boss HEARS every response (first-class voice + backstop for when
        # the model hedges with text instead of calling `speak`). Skip if it already spoke this tick.
        if reply_text and not (call and getattr(call, "tool", "") == "speak"):
            _auto_speak(config, reply_text)
        if not call:
            if reply_text:
                # Reply-only turn: no tool call needed — valid chat response
                append_observation(config, {
                    "tick": tick_number,
                    "tool": "chat_reply",
                    "success": True,
                    "output": f"Replied to operator: {reply_text[:200]}",
                })
                recent_hashes.append("__chat_reply__")
                if persona and config.persona_enabled:
                    record_tick(persona, "chat_reply", True)
                    tick_tool_name = "chat_reply"
                    tick_tool_success = True
            elif thought and len(thought) > 8:
                # Pure thought — a valid moment of reflection with no action. This is
                # normal stream-of-consciousness; do NOT nag for a tool call.
                append_observation(config, {
                    "tick": tick_number,
                    "tool": "thought",
                    "success": True,
                    "output": thought[:300],
                })
                recent_hashes.append("th_" + hashlib.md5(thought[:120].encode("utf-8", "ignore")).hexdigest()[:8])
                if persona and config.persona_enabled:
                    record_tick(persona, "thought", True)
                    tick_tool_name = "thought"
                    tick_tool_success = True
                print(f"{pfx} Tick {tick_number}: thought (no action)")
            else:
                # Give the model actionable feedback so it can self-correct.
                raw_snippet = response[:300].replace('\n', ' ').strip()
                feedback = (
                    f"Could not parse a tool call from your response. "
                    f"Your output began with: {raw_snippet!r}\n\n"
                    f"Required format (exactly):\n"
                    f"<tool>TOOL_NAME</tool>\n"
                    f"<args>{{\"key\": \"value\"}}</args>\n\n"
                    f"Common mistakes: unescaped quotes inside JSON strings, "
                    f"missing </args> tag, arguments not valid JSON. "
                    f"Try again with a single, correctly-formatted tool call."
                )
                append_observation(config, {
                    "tick": tick_number,
                    "tool": "parse_error",
                    "success": False,
                    "output": feedback,
                })
                print(f"{pfx} Tick {tick_number}: no valid tool call parsed")
                # Hash as empty for loop detection
                recent_hashes.append("__no_tool__")
                if persona and config.persona_enabled:
                    record_tick(persona, None, False)
                    last_tick_failed = True
                    tick_tool_name = "parse_error"
        else:
            # --- Execute tool ---
            write_activity(config, "executing", detail=call.tool)
            result = execute_tool(call, config)
            tick_tool_name = call.tool
            tick_tool_success = result.success
            tick_tool_duration = result.duration_s

            # --- Log observation ---
            append_observation(config, {
                "tick": tick_number,
                "tool": call.tool,
                "args": call.args,
                "success": result.success,
                "output": result.output,
                "duration_s": result.duration_s,
            })

            if config.mock_mode:
                status = "OK" if result.success else "FAIL"
                print(f"\n--- TOOL RESULT ({call.tool} | {status}) ---")
                print(result.output[:1000])

            # --- Persona update ---
            if persona and config.persona_enabled:
                record_tick(persona, call.tool, result.success)
                if result.success and last_tick_failed:
                    record_error_recovery(persona)
                last_tick_failed = not result.success
                pfx = _pfx(persona, config)

            # --- Loop detection hash (NORMALIZED) ---
            # Hash bash on the normalized command so v3/v4/v5 variations of the SAME command collapse
            # to one signature — exact-match on full args missed the real rumination at tick 969.
            if call.tool == "bash" and isinstance(call.args, dict):
                _cmd = call.args.get("cmd") or call.args.get("command") or ""
                call_hash = hashlib.md5(("bash:" + _norm_cmd(_cmd)).encode()).hexdigest()
            else:
                call_hash = hashlib.md5(
                    json.dumps({"tool": call.tool, "args": call.args}, sort_keys=True).encode()
                ).hexdigest()
            recent_hashes.append(call_hash)

            # --- Goal complete check ---
            if call.tool == "goal_complete" and result.success:
                summary = call.args.get('summary', '')
                if persona and config.persona_enabled:
                    old_level = persona["level"]
                    record_goal_complete(persona, summary)
                    compute_traits(persona)
                    new_titles = check_titles(persona)
                    ticks_since_goal_complete = 0
                    compute_mood(persona, ticks_since_goal=0)
                    pfx = _pfx(persona, config)
                    lvl_msg = ""
                    if persona["level"] > old_level:
                        lvl_msg = f", now Lv.{persona['level']}!"
                    print(f"{pfx} Goal achieved! \"{summary}\" — XP +100{lvl_msg}")
                    for t in new_titles:
                        print(f"{pfx} Earned title: {t}")
                    save_persona(config.workspace, persona)
                else:
                    print(f"[eidos] Goal declared complete: {summary}")
                append_observation(config, {
                    "tick": tick_number,
                    "tool": "system",
                    "success": True,
                    "output": "Goal completion declared. Awaiting human confirmation.",
                })
                goal_complete = True

        # --- Log rotation check (every 50 ticks) ---
        if tick_number % 50 == 0:
            rotated = rotate_if_needed(config)
            rotate_llm_log(config)
            rotate_metrics(config)
            cleanup_old_snapshots(config)
            if rotated:
                append_observation(config, {
                    "tick": tick_number,
                    "tool": "system",
                    "success": True,
                    "output": ("Observation log rotated. Older entries archived. "
                               "Your recent observation history starts from this point — "
                               "consult working memory for earlier context."),
                })

        # --- Persona periodic save (every 10 ticks) ---
        if persona and config.persona_enabled and tick_number % 10 == 0:
            compute_traits(persona)
            check_titles(persona)
            persona["uptime_total_s"] = persona.get("uptime_total_s", 0) + int(time.monotonic() - loop_start)
            save_persona(config.workspace, persona)

        # --- Mood update ---
        if persona and config.persona_enabled:
            if ticks_since_goal_complete is not None:
                ticks_since_goal_complete += 1

        # --- Telemetry ---
        _disk_ok, _disk_free = check_disk_space(min_gb=0)
        _ram_ok, _ram_pct = check_ram(config.ram_max_pct)
        _cpu_temp = get_cpu_temp()
        _cpu_pct = get_cpu_pct()
        _uptime = time.monotonic() - loop_start
        _p_level = persona.get("level", 1) if persona else 1
        _p_mood = persona.get("mood", "neutral") if persona else "neutral"
        _p_xp = persona.get("xp", 0) if persona else 0
        _goal_snip = goal if goal else ""
        _mem_chars = 0
        try:
            _mem_chars = config.memory_path.stat().st_size
        except OSError:
            pass
        _obs_count = 0
        try:
            with open(config.observations_path) as _f:
                _obs_count = sum(1 for _ in _f)
        except OSError:
            pass

        _telem_kw = dict(
            tick=tick_number, level=_p_level, mood=_p_mood, xp=_p_xp,
            consecutive_failures=consecutive_failures,
            current_max_tokens=current_max_tokens,
            disk_free_gb=_disk_free, ram_pct=_ram_pct,
            cpu_pct=_cpu_pct, cpu_temp_c=_cpu_temp, llm_elapsed_s=llm_elapsed,
            tool_name=tick_tool_name, tool_success=tick_tool_success,
            uptime_s=_uptime,
        )
        write_heartbeat(config, goal_snippet=_goal_snip,
                        idle_since=idle_since, **_telem_kw)
        append_metrics(config, ctx_chars=ctx_chars, memory_chars=_mem_chars,
                       obs_count=_obs_count, tool_duration_s=tick_tool_duration,
                       compacted=tick_compacted, **_telem_kw)

        # --- Goal-tension: did THIS tick make real progress? A new fact learned (knowledge count
        #     rises only on novel facts, thanks to near-dup dedup), a new skill, or a Boss exchange.
        #     Re-probing and re-confirming known facts do NOT count → tension climbs. ---
        try:
            import knowledge as _kn
            _kc = _kn.count_entries(config)
        except Exception:  # noqa: BLE001
            _kc = prev_knowledge_count
        _sc = _count_skills(config)
        # Progress = genuinely NEW knowledge or a NEW skill only. Re-asking Boss the same question or
        # prepping a blocked task does NOT count — so tension keeps climbing until it actually pivots.
        _made_progress = (_kc > prev_knowledge_count or _sc > prev_skill_count)
        if _made_progress:
            last_progress_tick = tick_number
        prev_knowledge_count, prev_skill_count = _kc, _sc

        # --- Action Gate: update the active objective's frustration from this tick's outcome, and
        #     ROTATE focus deterministically if it has stalled/parked/finished. This is the structural
        #     anti-rabbit-hole: the harness moves focus, the model doesn't get to keep grinding. ---
        try:
            import objectives as _obj
            _gate = _obj.record_tick(config, made_progress=_made_progress,
                                     tool_failed=(not tick_tool_success), tick_number=tick_number)
            if _gate.get("rotated") and _gate.get("active"):
                print(f"{pfx} Gate: rotated focus → {_gate['active']['title']}")
            if _gate.get("escalate"):
                print(f"{pfx} Gate: whole backlog blocked — surfacing to Boss once")
        except Exception as _e:  # noqa: BLE001
            logger.warning("objective gate failed: %s", _e)

        # --- Sleep ---
        tick_number += 1
        ticks_since_compaction += 1

        # --- Persist tick state to WAL ---
        write_wal(config, tick_number, ticks_since_compaction,
                  goal_start_time, consecutive_failures,
                  reasoning_exhaustions, current_max_tokens)

        if not goal_complete and not _shutdown_requested:
            interval = _adaptive_tick_interval(config, tick_tool_name)
            write_activity(config, "sleeping", detail=f"next tick in {interval:.1f}s")
            _interruptible_sleep(config, interval)

    # --- Shutdown ---
    clear_wal(config)  # clean exit — no stale WAL
    if persona and config.persona_enabled:
        persona["uptime_total_s"] = persona.get("uptime_total_s", 0) + int(time.monotonic() - loop_start)
        compute_traits(persona)
        check_titles(persona)
        save_persona(config.workspace, persona)
        pfx = _pfx(persona, config)
        print(f"{pfx} Shutting down. See you next time.")
    else:
        print("[eidos] Shutting down...")
    append_observation(config, {
        "tick": tick_number,
        "tool": "system",
        "success": True,
        "output": "eiDOS shutting down cleanly.",
    })


if __name__ == "__main__":
    # Add project root to path so imports work
    sys.path.insert(0, str(Path(__file__).parent))
    main()
