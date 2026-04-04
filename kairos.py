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
from llm import complete, LLMError
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
from rotation import rotate_if_needed, cleanup_old_archives, rotate_llm_log, cleanup_old_snapshots
from safety import check_ram, kill_child_processes
from session import human_present, take_workspace_snapshot, workspace_diff
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
              goal_start_time: float, consecutive_failures: int = 0):
    """Atomically write tick state to WAL for crash recovery."""
    wal = {
        "tick_number": tick_number,
        "ticks_since_compaction": ticks_since_compaction,
        "goal_start_time": goal_start_time,
        "consecutive_failures": consecutive_failures,
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

    # 2. Create memory.md if missing
    if not config.memory_path.exists():
        write_memory(config, "# Working Memory\nFresh start. No prior context.")
        print("[kairos] Created initial memory.md")

    # 3. Validate observations.jsonl
    truncated = validate_observations(config)
    if truncated:
        print(f"[kairos] Truncated {truncated} malformed line(s) from observations.jsonl")

    # 4. Scan background jobs, mark dead ones
    jobs = refresh_jobs(config)
    dead = [j for j in jobs if j["status"] != "running"]
    if dead:
        print(f"[kairos] Found {len(dead)} completed/dead background jobs")

    # 5. Log recovery
    append_observation(config, {
        "tick": 0,
        "tool": "system",
        "success": True,
        "output": "Kairos recovered from restart. All state validated.",
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
    recent_hashes: collections.deque = collections.deque(maxlen=config.loop_detect_window)
    standby = False
    standby_snapshot = None
    goal_complete = False
    last_tick_failed = False
    ticks_since_goal_complete = None  # None = never
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

        # --- Check for goal ---
        goal = read_goal(config)
        if not goal:
            if config.mock_mode:
                print("[kairos] No goal.md — exiting (mock mode)")
                break
            time.sleep(config.tick_interval_s)
            continue

        # --- Compaction check ---
        if should_compact(config, ticks_since_compaction):
            print(f"{pfx} Dreaming... consolidating memories.")
            try:
                compact(config, persona=persona)
                ticks_since_compaction = 0
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
        try:
            response = complete(messages, config)
            consecutive_failures = 0  # reset on success
        except LLMError as e:
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
                      goal_start_time, consecutive_failures)
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
            append_observation(config, {
                "tick": tick_number,
                "tool": "parse_error",
                "success": False,
                "output": f"No valid tool call in response. Raw: {response[:500]}",
            })
            print(f"{pfx} Tick {tick_number}: no valid tool call parsed")
            # Hash as empty for loop detection
            recent_hashes.append("__no_tool__")
            if persona and config.persona_enabled:
                record_tick(persona, None, False)
                last_tick_failed = True
        else:
            # --- Execute tool ---
            result = execute_tool(call, config)

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
            rotate_if_needed(config)
            rotate_llm_log(config)
            cleanup_old_snapshots(config)

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

        # --- Sleep ---
        tick_number += 1
        ticks_since_compaction += 1

        # --- Persist tick state to WAL ---
        write_wal(config, tick_number, ticks_since_compaction,
                  goal_start_time, consecutive_failures)

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
