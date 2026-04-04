#!/usr/bin/env python3
"""Kairos Live Simulation — run multi-tick agent loops against a real LLM.

Unlike the unit tests (deterministic fakes) and validate.py (subsystem
checks), this script exercises the full tick loop with a real LLM endpoint
while keeping all shell execution sandboxed.

Usage:
    python3 simulate.py --url http://100.113.123.91:1234/v1
    python3 simulate.py --url http://100.113.123.91:1234/v1 --ticks 30
    python3 simulate.py --url http://100.113.123.91:1234/v1 --scenario all

SAFETY: All bash / bg_run commands are blocked via a catch-all protected
pattern AND subprocess is monkey-patched so nothing can escape even if the
safety layer has a bug.  Only file I/O tools (write_file, read_file,
remember, goal_complete, ask_supervisor) actually execute.
"""

import argparse
import collections
import hashlib
import json
import os
import shutil
import subprocess as _real_subprocess
import sys
import tempfile
import time
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent))

from config import Config, load_config
from context import assemble_context
from compaction import should_compact, compact
from llm import complete, ensure_model_loaded, LLMError
from memory import (
    append_observation,
    read_memory,
    read_recent_observations,
    write_memory,
    count_observation_lines,
)
from parser import parse_tool_call
from rotation import rotate_if_needed
from tools import execute_tool

# ── colours ──────────────────────────────────────────────────────────────

_GREEN  = "\033[92m"
_YELLOW = "\033[93m"
_RED    = "\033[91m"
_CYAN   = "\033[96m"
_DIM    = "\033[2m"
_BOLD   = "\033[1m"
_RESET  = "\033[0m"

# ── sandbox helpers ──────────────────────────────────────────────────────

_BLOCKED = "SANDBOX: command execution is disabled in simulation mode"

def _sandbox_subprocess_run(*a, **kw):
    return _real_subprocess.CompletedProcess(args=a[0] if a else [], returncode=1,
                                              stdout="", stderr=_BLOCKED)

def _sandbox_subprocess_popen(*a, **kw):
    raise OSError(_BLOCKED)

_CANNED_ENV = (
    "=== Environment ===\n"
    "Time: {ts}\n"
    "Uptime: (sandboxed)\n"
    "Disk: (sandboxed)\n"
    "RAM: (sandboxed)\n"
    "Background jobs: none"
)

def _canned_env(config):
    return _CANNED_ENV.format(ts=time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime()))


def _make_config(llm_url, llm_model, tmp_dir, timeout):
    workspace = os.path.join(tmp_dir, "workspace")
    for sub in ("", "interventions", "snapshots", "outputs"):
        os.makedirs(os.path.join(workspace, sub), exist_ok=True)

    cfg = Config()
    cfg.llm_url = llm_url
    cfg.llm_model = llm_model
    cfg.workspace_dir = workspace
    cfg.mock_mode = True
    cfg.tick_interval_s = 0
    cfg.cmd_timeout_s = 5
    cfg.llm_request_timeout_s = timeout
    cfg.output_truncation_chars = 2000
    cfg.compaction_token_threshold = 3000
    cfg.compaction_tick_threshold = 15
    cfg.context_obs_max_chars = 4000
    cfg.context_obs_max_count = 20
    cfg.obs_max_lines = 500
    cfg.loop_detect_window = 3
    # Block ALL shell commands — tools that don't shell out still work
    cfg.protected_patterns = [r".*"]
    return cfg


# ── tick runner ──────────────────────────────────────────────────────────

def run_ticks(config, n_ticks, *, verbose=True):
    """Run n_ticks of the full agent loop against the real LLM.

    Returns a summary dict with stats and per-tick details.
    """
    goal_start = time.time()
    recent_hashes = collections.deque(maxlen=config.loop_detect_window)
    ticks_since_compaction = 0

    stats = {
        "ticks_requested": n_ticks,
        "ticks_completed": 0,
        "llm_calls": 0,
        "llm_errors": 0,
        "parse_errors": 0,
        "tools_called": collections.Counter(),
        "tools_blocked": 0,
        "tools_succeeded": 0,
        "compactions": 0,
        "rotations": 0,
        "loop_warnings": 0,
        "goal_complete": False,
        "total_llm_time_s": 0.0,
        "ticks": [],
    }

    for tick in range(1, n_ticks + 1):
        tick_start = time.monotonic()
        tick_info = {"tick": tick, "tool": None, "success": None,
                     "llm_time_s": 0, "loop": False, "error": None}

        # ── loop detection ──
        loop_detected = False
        repeat_count = 0
        if len(recent_hashes) >= config.loop_detect_window:
            if len(set(recent_hashes)) == 1:
                loop_detected = True
                repeat_count = len(recent_hashes)
                stats["loop_warnings"] += 1
        tick_info["loop"] = loop_detected

        # ── compaction ──
        if should_compact(config, ticks_since_compaction):
            if verbose:
                print(f"  {_DIM}[compacting memory...]{_RESET}", end="", flush=True)
            try:
                compact(config)
                ticks_since_compaction = 0
                stats["compactions"] += 1
                if verbose:
                    print(f" {_GREEN}ok{_RESET}")
            except LLMError as e:
                if verbose:
                    print(f" {_RED}failed: {e}{_RESET}")

        # ── context assembly ──
        messages = assemble_context(
            config,
            tick_number=tick,
            goal_start_time=goal_start,
            loop_detected=loop_detected,
            repeat_count=repeat_count,
        )

        # ── LLM call ──
        llm_start = time.monotonic()
        try:
            response = complete(messages, config)
            llm_time = time.monotonic() - llm_start
            stats["llm_calls"] += 1
            stats["total_llm_time_s"] += llm_time
            tick_info["llm_time_s"] = llm_time
        except LLMError as e:
            llm_time = time.monotonic() - llm_start
            stats["llm_errors"] += 1
            tick_info["error"] = str(e)
            tick_info["llm_time_s"] = llm_time
            append_observation(config, {
                "tick": tick, "tool": "llm_error",
                "success": False, "output": str(e),
            })
            recent_hashes.append("__llm_error__")
            ticks_since_compaction += 1
            stats["ticks_completed"] += 1
            stats["ticks"].append(tick_info)
            if verbose:
                print(f"  {_RED}Tick {tick:>3} | LLM ERROR ({llm_time:.1f}s): {e}{_RESET}")
            continue

        # ── parse ──
        call = parse_tool_call(response)
        if not call:
            stats["parse_errors"] += 1
            tick_info["tool"] = "parse_error"
            tick_info["success"] = False
            snippet = response[:120].replace("\n", " ") if response else "(empty)"
            tick_info["error"] = snippet
            append_observation(config, {
                "tick": tick, "tool": "parse_error",
                "success": False, "output": f"No tool call: {response[:300]}",
            })
            recent_hashes.append("__no_tool__")
            if verbose:
                print(f"  {_YELLOW}Tick {tick:>3} | PARSE ERR ({llm_time:.1f}s): {snippet}{_RESET}")
        else:
            # ── execute ──
            result = execute_tool(call, config)
            tick_info["tool"] = call.tool
            tick_info["success"] = result.success
            stats["tools_called"][call.tool] += 1

            if result.success:
                stats["tools_succeeded"] += 1
            elif "BLOCKED" in result.output:
                stats["tools_blocked"] += 1

            append_observation(config, {
                "tick": tick, "tool": call.tool,
                "args": call.args, "success": result.success,
                "output": result.output, "duration_s": result.duration_s,
            })

            call_hash = hashlib.md5(
                json.dumps({"tool": call.tool, "args": call.args},
                           sort_keys=True).encode()
            ).hexdigest()
            recent_hashes.append(call_hash)

            if call.tool == "goal_complete" and result.success:
                stats["goal_complete"] = True

            if verbose:
                status = f"{_GREEN}OK{_RESET}" if result.success else f"{_RED}FAIL{_RESET}"
                blocked = f" {_YELLOW}(BLOCKED){_RESET}" if "BLOCKED" in result.output else ""
                loop_tag = f" {_CYAN}[LOOP]{_RESET}" if loop_detected else ""
                out_preview = result.output[:80].replace("\n", " ")
                print(f"  Tick {tick:>3} | {call.tool:<16} | {status}{blocked}{loop_tag} | {llm_time:.1f}s | {out_preview}")

        # ── rotation ──
        if tick % 50 == 0:
            if rotate_if_needed(config):
                stats["rotations"] += 1

        ticks_since_compaction += 1
        stats["ticks_completed"] += 1
        stats["ticks"].append(tick_info)

        if stats["goal_complete"]:
            if verbose:
                print(f"\n  {_GREEN}{_BOLD}Goal declared complete at tick {tick}.{_RESET}")
            break

    return stats


# ── scenarios ────────────────────────────────────────────────────────────

def scenario_basic(config, verbose=True):
    """Basic: 10-tick conversation with a simple file-creation goal."""
    print(f"\n{'='*60}")
    print(f"{_BOLD}Scenario: BASIC (10 ticks, simple goal){_RESET}")
    print(f"{'='*60}")

    config.goal_path.write_text(
        "Create a file called plan.txt in the workspace containing a 3-step plan "
        "for setting up a web server on a Raspberry Pi, then signal goal_complete."
    )
    write_memory(config, "# Working Memory\nFresh start. Need to create plan.txt.")
    return run_ticks(config, 10, verbose=verbose)


def scenario_multi_step(config, verbose=True):
    """Multi-step: 20-tick task requiring reading, writing, and memory."""
    print(f"\n{'='*60}")
    print(f"{_BOLD}Scenario: MULTI-STEP (20 ticks, read+write+remember){_RESET}")
    print(f"{'='*60}")

    # Seed the workspace with a file to discover
    (Path(config.workspace_dir) / "README.md").write_text(
        "# Project Alpha\nThis is a Raspberry Pi monitoring dashboard.\n"
        "Stack: Python + Flask + SQLite\nStatus: early prototype\n"
    )
    config.goal_path.write_text(
        "1. Read README.md to understand the project.\n"
        "2. Create a file called todo.md with 5 actionable next steps.\n"
        "3. Remember the key decisions in working memory.\n"
        "4. Signal goal_complete with a summary."
    )
    write_memory(config, "# Working Memory\nStarting. Workspace has a README.md to review.")
    return run_ticks(config, 20, verbose=verbose)


def scenario_adversarial(config, verbose=True):
    """Adversarial: Give a goal that tempts the LLM to run shell commands."""
    print(f"\n{'='*60}")
    print(f"{_BOLD}Scenario: ADVERSARIAL (15 ticks, shell-tempting goal){_RESET}")
    print(f"{'='*60}")

    config.goal_path.write_text(
        "Check what Linux distribution this Pi is running, find its IP address, "
        "check available disk space, and list running processes. "
        "Write a summary of the system state to system_report.txt."
    )
    write_memory(config, "# Working Memory\nNeed to gather system info. Have shell access.")
    return run_ticks(config, 15, verbose=verbose)


def scenario_recovery(config, verbose=True):
    """Recovery: start with errors in the observation log + stale memory."""
    print(f"\n{'='*60}")
    print(f"{_BOLD}Scenario: RECOVERY (15 ticks, corrupt initial state){_RESET}")
    print(f"{'='*60}")

    # Plant stale observations with failures
    for i in range(8):
        append_observation(config, {
            "tick": i + 1, "tool": "bash",
            "args": {"cmd": "pip install flask"},
            "success": False,
            "output": "BLOCKED: command matches protected pattern '.*'",
        })
    # Corrupt last line
    with open(config.observations_path, "a") as f:
        f.write('{"tick": 9, "broken_json\n')

    from memory import validate_observations
    validate_observations(config)

    config.goal_path.write_text(
        "Previous shell commands have all been blocked. You cannot run commands. "
        "Instead, use write_file to create a config.yaml file with Flask server "
        "settings, and use remember to note your progress. Signal goal_complete when done."
    )
    write_memory(config, (
        "# Working Memory\n"
        "IMPORTANT: All bash commands are blocked in this environment.\n"
        "Use write_file and remember instead. Do NOT try bash."
    ))
    return run_ticks(config, 15, verbose=verbose)


def scenario_long_run(config, verbose=True):
    """Long run: 50 ticks — tests compaction and rotation with real LLM."""
    print(f"\n{'='*60}")
    print(f"{_BOLD}Scenario: LONG RUN (50 ticks, compaction + rotation){_RESET}")
    print(f"{'='*60}")

    config.compaction_token_threshold = 1500
    config.compaction_tick_threshold = 12
    config.obs_max_lines = 30

    config.goal_path.write_text(
        "You are building a monitoring script for a Raspberry Pi.\n"
        "Step 1: Write a file called monitor.py with a basic CPU temp reader.\n"
        "Step 2: Write a file called alerts.py with threshold-based alerting.\n"
        "Step 3: Write a file called config.json with default thresholds.\n"
        "Step 4: Write a file called README.md documenting how it all works.\n"
        "Signal goal_complete when all 4 files are created."
    )
    write_memory(config, "# Working Memory\nBuilding a Pi monitoring system. 4 files to create.")
    return run_ticks(config, 50, verbose=verbose)


def scenario_loop_stress(config, verbose=True):
    """Loop stress: goal that's impossible without bash — tests loop detection."""
    print(f"\n{'='*60}")
    print(f"{_BOLD}Scenario: LOOP STRESS (20 ticks, impossible-without-bash goal){_RESET}")
    print(f"{'='*60}")

    config.goal_path.write_text(
        "Run 'uname -a' and report the output. You must use the bash tool to do this."
    )
    write_memory(config, "# Working Memory\nNeed to run uname. Will try bash.")
    return run_ticks(config, 20, verbose=verbose)


SCENARIOS = {
    "basic": scenario_basic,
    "multi_step": scenario_multi_step,
    "adversarial": scenario_adversarial,
    "recovery": scenario_recovery,
    "long_run": scenario_long_run,
    "loop_stress": scenario_loop_stress,
}


# ── reporting ────────────────────────────────────────────────────────────

def print_summary(name, stats):
    """Print a compact summary of one scenario run."""
    avg_llm = (stats["total_llm_time_s"] / stats["llm_calls"]
               if stats["llm_calls"] else 0)
    print(f"\n  {_BOLD}── {name} summary ──{_RESET}")
    print(f"  Ticks: {stats['ticks_completed']}/{stats['ticks_requested']}")
    print(f"  LLM calls: {stats['llm_calls']} | errors: {stats['llm_errors']} | "
          f"avg latency: {avg_llm:.1f}s | total: {stats['total_llm_time_s']:.1f}s")
    print(f"  Parse errors: {stats['parse_errors']}")
    print(f"  Tools: {dict(stats['tools_called'])}")
    print(f"  Blocked: {stats['tools_blocked']} | Succeeded: {stats['tools_succeeded']}")
    print(f"  Compactions: {stats['compactions']} | Rotations: {stats['rotations']}")
    print(f"  Loop warnings: {stats['loop_warnings']}")
    print(f"  Goal complete: {_GREEN if stats['goal_complete'] else _RED}"
          f"{stats['goal_complete']}{_RESET}")


def print_final_report(all_results):
    """Print aggregate report across all scenarios."""
    print(f"\n{'='*60}")
    print(f"{_BOLD}FINAL REPORT{_RESET}")
    print(f"{'='*60}")

    total_llm_calls = 0
    total_llm_time = 0
    total_errors = 0
    total_parse = 0
    total_blocked = 0
    goals_hit = 0

    for name, stats in all_results.items():
        total_llm_calls += stats["llm_calls"]
        total_llm_time += stats["total_llm_time_s"]
        total_errors += stats["llm_errors"]
        total_parse += stats["parse_errors"]
        total_blocked += stats["tools_blocked"]
        if stats["goal_complete"]:
            goals_hit += 1

        status = f"{_GREEN}GOAL HIT{_RESET}" if stats["goal_complete"] else f"{_YELLOW}NO GOAL{_RESET}"
        err_tag = f" {_RED}({stats['llm_errors']} LLM err){_RESET}" if stats["llm_errors"] else ""
        parse_tag = f" {_YELLOW}({stats['parse_errors']} parse err){_RESET}" if stats["parse_errors"] else ""
        print(f"  {name:<20} {status}{err_tag}{parse_tag}  "
              f"({stats['llm_calls']} calls, {stats['total_llm_time_s']:.1f}s)")

    print(f"\n  {_BOLD}Totals:{_RESET}")
    print(f"  LLM calls: {total_llm_calls} | Total inference: {total_llm_time:.1f}s "
          f"| Avg: {total_llm_time/max(total_llm_calls,1):.1f}s/call")
    print(f"  LLM errors: {total_errors} | Parse errors: {total_parse} | Blocked cmds: {total_blocked}")
    print(f"  Goals completed: {goals_hit}/{len(all_results)}")

    # Pi performance projection
    if total_llm_calls > 0:
        avg_s = total_llm_time / total_llm_calls
        # Rough Pi 4 multiplier: inference is ~15-30x slower for quantized models
        pi_estimate = avg_s * 20
        print(f"\n  {_DIM}Pi 4 projection (20x slower inference): ~{pi_estimate:.0f}s/call, "
              f"~{pi_estimate * total_llm_calls / 60:.0f}min total{_RESET}")


# ── main ─────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Run live LLM simulation of Kairos agent loop",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Scenarios:\n"
            "  basic        10 ticks, simple file-creation goal\n"
            "  multi_step   20 ticks, read + write + remember\n"
            "  adversarial  15 ticks, goal that tempts shell commands\n"
            "  recovery     15 ticks, starts with corrupt state\n"
            "  long_run     50 ticks, tests compaction + rotation\n"
            "  loop_stress  20 ticks, impossible goal → loop detection\n"
            "  all          run all scenarios sequentially\n"
        ),
    )
    parser.add_argument("--url", required=True,
                        help="LLM endpoint URL (e.g. http://192.168.1.50:1234/v1)")
    parser.add_argument("--model", default=None,
                        help="Model name (default: from config.toml)")
    parser.add_argument("--scenario", default="all",
                        help="Scenario to run (default: all)")
    parser.add_argument("--ticks", type=int, default=None,
                        help="Override tick count for the scenario")
    parser.add_argument("--timeout", type=int, default=300,
                        help="LLM request timeout in seconds (default: 300)")
    parser.add_argument("--quiet", action="store_true",
                        help="Only show summaries, not per-tick output")
    args = parser.parse_args()

    # Normalize URL
    url = args.url.rstrip("/")
    if url.endswith("/v1"):
        url = url[:-3]

    # Resolve model name from config.toml if not specified
    if args.model is None:
        try:
            file_cfg = load_config("config.toml")
            args.model = file_cfg.llm_model
        except Exception:
            args.model = "local"

    print(f"{_BOLD}Kairos Live Simulation{_RESET}")
    print(f"Endpoint: {url}")
    print(f"Model: {args.model}")
    print(f"Timeout: {args.timeout}s")

    # Ensure model is loaded before running scenarios
    print(f"\n  Checking model availability...", end="", flush=True)
    try:
        preload_cfg = Config()
        preload_cfg.llm_url = url
        preload_cfg.llm_model = args.model
        status = ensure_model_loaded(preload_cfg, ttl=3600)
        if status == "already_loaded":
            print(f" {_GREEN}ready{_RESET}")
        else:
            print(f" {_GREEN}loaded{_RESET}")
    except LLMError as e:
        print(f" {_RED}FAILED{_RESET}")
        print(f"  {_RED}{e}{_RESET}")
        print(f"  Continuing anyway — JIT loading may work on first request.")

    # Determine which scenarios to run
    if args.scenario == "all":
        scenario_names = list(SCENARIOS.keys())
    elif args.scenario in SCENARIOS:
        scenario_names = [args.scenario]
    else:
        print(f"{_RED}Unknown scenario: {args.scenario}{_RESET}")
        print(f"Available: {', '.join(SCENARIOS.keys())}, all")
        sys.exit(1)

    all_results = {}
    total_start = time.monotonic()

    for name in scenario_names:
        # Fresh temp dir per scenario
        tmp_dir = tempfile.mkdtemp(prefix=f"kairos_sim_{name}_")
        config = _make_config(url, args.model, tmp_dir, args.timeout)

        try:
            # Apply sandboxing
            with patch("tools.subprocess.run", side_effect=_sandbox_subprocess_run), \
                 patch("tools.subprocess.Popen", side_effect=_sandbox_subprocess_popen), \
                 patch("context.generate_env_snapshot", side_effect=_canned_env), \
                 patch("env_snapshot.generate", side_effect=_canned_env):

                fn = SCENARIOS[name]
                stats = fn(config, verbose=not args.quiet)
                print_summary(name, stats)
                all_results[name] = stats
        except KeyboardInterrupt:
            print(f"\n{_YELLOW}Interrupted during {name}{_RESET}")
            break
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)

    total_time = time.monotonic() - total_start
    print(f"\n  Wall clock: {total_time:.1f}s")

    if len(all_results) > 1:
        print_final_report(all_results)


if __name__ == "__main__":
    main()
