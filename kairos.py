#!/usr/bin/env python3
"""Kairos — autonomous LLM supervisor for Raspberry Pi.

Entry point: crash recovery, tick loop, signal handling, session detection.
"""

import argparse
import collections
import hashlib
import json
import logging
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

from config import Config, load_config
from context import assemble_context
from compaction import should_compact, compact
from llm import complete, LLMError, ReasoningExhausted
from memory import (
    append_observation,
    read_goal,
    validate_observations,
    write_memory,
)
from parser import parse_tool_call
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
from telemetry import write_heartbeat, append_metrics
from tools import execute_tool, refresh_jobs

logger = logging.getLogger("kairos")


# --- Globals for signal handling ---
_shutdown_requested = False


def _handle_signal(signum, frame):
    global _shutdown_requested
    _shutdown_requested = True


def main():
    parser = argparse.ArgumentParser(description="Kairos autonomous supervisor")
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
    return "[kairos]"


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
    tmp.rename(config.wal_path)


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
    print("[kairos] Running crash recovery...")

    # 0. Read WAL (tick state from before crash)
    wal = read_wal(config)
    if wal:
        print(f"[kairos] WAL recovered: tick={wal.get('tick_number')}, "
              f"compaction_gap={wal.get('ticks_since_compaction')}")

    # 1. Verify goal.md
    goal = read_goal(config)
    if not goal:
        print("[kairos] WARNING: No goal.md found. Agent will idle until one is created.")

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
                        print(f"[kairos] Restored memory.md from snapshot: {snapshots[0].name}")
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
            print("[kairos] Created initial memory.md")

    # 3. Validate observations.jsonl
    truncated = validate_observations(config)
    if truncated:
        print(f"[kairos] Truncated {truncated} malformed line(s) from observations.jsonl")
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
        print(f"[kairos] Found {len(dead)} completed/dead background jobs")
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
            f"Kairos recovered from crash. Resuming at tick {wal.get('tick_number', '?')}. "
            f"State before crash: {wal.get('consecutive_failures', 0)} consecutive LLM failures, "
            f"{wal.get('reasoning_exhaustions', 0)} reasoning exhaustions, "
            f"max_tokens was {wal.get('current_max_tokens', config.llm_max_tokens)}. "
            f"Review recent observations — the last action may not have completed."
        )
    else:
        recovery_detail = "Kairos starting fresh. No prior crash state found."
    append_observation(config, {
        "tick": 0,
        "tool": "system",
        "success": True,
        "output": recovery_detail,
    })

    # 6. Rotate logs and clean old archives
    if rotate_if_needed(config):
        print("[kairos] Rotated observations.jsonl")
    deleted = cleanup_old_archives(config)
    if deleted:
        print(f"[kairos] Cleaned {deleted} old archive(s)")

    return wal


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
    loop_start = time.monotonic()

    pfx = _pfx(persona, config)
    print(f"{pfx} Starting tick loop (interval={config.tick_interval_s}s, mock={config.mock_mode})")

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

        # --- Thermal check (Linux only) ---
        cpu_temp = get_cpu_temp()
        if cpu_temp is not None and cpu_temp > config.thermal_pause_c:
            print(f"{pfx} Thermal throttle ({cpu_temp:.0f}°C > {config.thermal_pause_c}°C), skipping tick")
            append_observation(config, {
                "tick": tick_number,
                "tool": "system",
                "success": False,
                "output": f"Thermal throttle: {cpu_temp:.0f}°C, pausing tick.",
            })
            time.sleep(config.tick_interval_s)
            continue

        # --- Check for goal ---
        goal = read_goal(config)
        if not goal:
            if idle_since is None:
                idle_since = time.time()
            if config.mock_mode:
                print("[kairos] No goal.md — exiting (mock mode)")
                break
            time.sleep(config.tick_interval_s)
            continue
        else:
            idle_since = None

        # --- Compaction check ---
        tick_compacted = False
        if should_compact(config, ticks_since_compaction):
            print(f"{pfx} Dreaming... consolidating memories.")
            try:
                compact(config, persona=persona)
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

        # --- Loop detection ---
        loop_detected = False
        repeat_count = 0
        if len(recent_hashes) >= config.loop_detect_window:
            if len(set(recent_hashes)) == 1:
                loop_detected = True
                repeat_count = len(recent_hashes)

        # --- Assemble context ---
        messages = assemble_context(
            config,
            tick_number=tick_number,
            goal_start_time=goal_start_time,
            loop_detected=loop_detected,
            repeat_count=repeat_count,
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
        llm_start = time.monotonic()
        tick_tool_name = ""
        tick_tool_success = False
        tick_tool_duration = 0.0
        tick_compacted = False
        try:
            response = complete(messages, config, max_tokens=current_max_tokens)
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

        # --- Parse tool call ---
        call = parse_tool_call(response)
        if not call:
            # Give the model actionable feedback so it can self-correct.
            # Include the raw output snippet so it can see what went wrong,
            # plus the expected format so it doesn't have to guess.
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

            # --- Loop detection hash ---
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
                    print(f"[kairos] Goal declared complete: {summary}")
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
            cpu_temp_c=_cpu_temp, llm_elapsed_s=llm_elapsed,
            tool_name=tick_tool_name, tool_success=tick_tool_success,
            uptime_s=_uptime,
        )
        write_heartbeat(config, goal_snippet=_goal_snip,
                        idle_since=idle_since, **_telem_kw)
        append_metrics(config, ctx_chars=ctx_chars, memory_chars=_mem_chars,
                       obs_count=_obs_count, tool_duration_s=tick_tool_duration,
                       compacted=tick_compacted, **_telem_kw)

        # --- Sleep ---
        tick_number += 1
        ticks_since_compaction += 1

        # --- Persist tick state to WAL ---
        write_wal(config, tick_number, ticks_since_compaction,
                  goal_start_time, consecutive_failures,
                  reasoning_exhaustions, current_max_tokens)

        if not goal_complete and not _shutdown_requested:
            time.sleep(config.tick_interval_s)

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
        print("[kairos] Shutting down...")
    append_observation(config, {
        "tick": tick_number,
        "tool": "system",
        "success": True,
        "output": "Kairos shutting down cleanly.",
    })


if __name__ == "__main__":
    # Add project root to path so imports work
    sys.path.insert(0, str(Path(__file__).parent))
    main()
