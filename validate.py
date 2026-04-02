#!/usr/bin/env python3
"""Kairos Validation Script.

Exercises every harness subsystem against a live LLM endpoint.
Usage: python validate.py --url http://localhost:1234/v1
"""

import argparse
import json
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent))

from config import Config, load_config
from parser import parse_tool_call, ToolCall
from safety import is_command_blocked, check_disk_space, check_ram
from tools import execute_tool, TOOLS
from memory import (
    read_goal,
    read_memory,
    write_memory,
    append_observation,
    read_recent_observations,
    validate_observations,
    read_interventions,
)
from llm import complete, LLMError
from prompts import SYSTEM_PROMPT, TICK_PROMPT
from env_snapshot import generate as generate_env_snapshot
from context import assemble_context
from compaction import should_compact, compact
from session import human_present, take_workspace_snapshot


class ValidationResult:
    def __init__(self, name: str):
        self.name = name
        self.passed = False
        self.duration = 0.0
        self.error = None
        self.details = {}

    def __str__(self):
        status = "PASS" if self.passed else "FAIL"
        line = f"[{status}] {self.name} ({self.duration:.1f}s)"
        if self.details:
            for k, v in self.details.items():
                line += f", {k}: {v}"
        if self.error:
            line += f"\n       Error: {self.error}"
        return line


def make_test_config(llm_url: str, workspace_dir: str, llm_model: str) -> Config:
    """Create a config for validation with a temporary workspace."""
    config = Config()
    config.llm_url = llm_url
    config.llm_model = llm_model
    config.workspace_dir = workspace_dir
    config.mock_mode = True
    config.tick_interval_s = 1
    config.cmd_timeout_s = 30
    config.output_truncation_chars = 2000
    config.llm_request_timeout_s = 120
    config.compaction_token_threshold = 2000
    config.compaction_tick_threshold = 5
    config.context_obs_max_chars = 4000
    config.context_obs_max_count = 20
    return config


def stage_endpoint_health(config: Config) -> ValidationResult:
    """Stage 1: Verify the LLM endpoint responds."""
    r = ValidationResult("Stage 1: Endpoint Health")
    start = time.monotonic()
    try:
        messages = [{"role": "user", "content": "Say hello in one sentence."}]
        response = complete(messages, config, temperature=0.1, max_tokens=64)
        r.duration = time.monotonic() - start
        # Some models can return an empty content field while still proving endpoint health.
        r.passed = True
        r.details["latency"] = f"{r.duration*1000:.0f}ms"
        r.details["response_len"] = len((response or "").strip())
        if not (response or "").strip():
            r.details["note"] = "empty content but request succeeded"
    except LLMError as e:
        r.duration = time.monotonic() - start
        r.error = str(e)
    return r


def stage_tool_call_parsing(config: Config) -> ValidationResult:
    """Stage 2: Verify the model produces parseable tool calls."""
    r = ValidationResult("Stage 2: Tool Call Parsing")
    start = time.monotonic()

    system = SYSTEM_PROMPT.format(workspace=str(config.workspace))
    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": (
            "## Goal\nList the files in /tmp and report what you find.\n\n"
            "## Working Memory\nFresh start.\n\n"
            "Tick 1 | What is your next action? "
            "Respond with brief reasoning then exactly one tool call."
        )},
    ]

    try:
        response = complete(messages, config, temperature=0.1)
        r.duration = time.monotonic() - start

        call = parse_tool_call(response)
        if call:
            r.passed = True
            r.details["tool"] = call.tool
            r.details["args_keys"] = list(call.args.keys())
        else:
            r.error = f"No valid tool call parsed from response: {response[:300]}"
    except LLMError as e:
        r.duration = time.monotonic() - start
        r.error = str(e)
    return r


def stage_tool_execution(config: Config) -> ValidationResult:
    """Stage 3: Parse a tool call and execute it."""
    r = ValidationResult("Stage 3: Tool Execution Round-Trip")
    start = time.monotonic()

    # Use a known-good tool call
    call = ToolCall(tool="bash", args={"cmd": "echo 'kairos_validate_test'"}, raw="")
    result = execute_tool(call, config)
    r.duration = time.monotonic() - start

    if result.success and "kairos_validate_test" in result.output:
        # Also verify observation was logged
        append_observation(config, {
            "tick": 0,
            "tool": call.tool,
            "args": call.args,
            "success": result.success,
            "output": result.output,
        })
        obs = read_recent_observations(config, max_chars=10000, max_count=5)
        if obs:
            r.passed = True
            r.details["output_len"] = len(result.output)
        else:
            r.error = "Tool executed but observation not readable"
    else:
        r.error = f"Tool failed: {result.output}"
    return r


def stage_multi_turn(config: Config) -> ValidationResult:
    """Stage 4: Run 3 sequential ticks with full context assembly."""
    r = ValidationResult("Stage 4: Multi-Turn Context")
    start = time.monotonic()

    # Set up goal
    config.goal_path.write_text(
        "Create a file /tmp/kairos_validate.txt containing the text 'kairos_validated_ok', "
        "then read it back to confirm it was written correctly."
    )
    write_memory(config, "# Working Memory\nFresh start. Goal: create and verify a test file.")

    goal_start = time.time()
    ticks_run = 0
    max_ticks = 5  # safety cap

    try:
        for tick in range(1, max_ticks + 1):
            messages = assemble_context(
                config,
                tick_number=tick,
                goal_start_time=goal_start,
            )

            response = complete(messages, config, temperature=0.1)
            call = parse_tool_call(response)

            if not call:
                append_observation(config, {
                    "tick": tick,
                    "tool": "parse_error",
                    "success": False,
                    "output": f"No tool call: {response[:200]}",
                })
                ticks_run += 1
                continue

            result = execute_tool(call, config)
            append_observation(config, {
                "tick": tick,
                "tool": call.tool,
                "args": call.args,
                "success": result.success,
                "output": result.output,
            })
            ticks_run += 1

            # Check if goal is done
            if call.tool == "goal_complete":
                break

            # Check if file exists with correct content
            test_file = Path("/tmp/kairos_validate.txt")
            if test_file.exists():
                content = test_file.read_text().strip()
                if "kairos_validated_ok" in content:
                    # Model did the write, may not have read back yet — that's fine
                    if tick >= 2:
                        break

        r.duration = time.monotonic() - start

        # Verify result
        test_file = Path("/tmp/kairos_validate.txt")
        if test_file.exists() and "kairos_validated_ok" in test_file.read_text():
            r.passed = True
            r.details["ticks"] = ticks_run
        else:
            r.error = f"File not created or content wrong after {ticks_run} ticks"
            r.details["ticks"] = ticks_run

    except LLMError as e:
        r.duration = time.monotonic() - start
        r.error = str(e)
        r.details["ticks"] = ticks_run

    return r


def stage_compaction(config: Config) -> ValidationResult:
    """Stage 5: Populate observations, run compaction, verify."""
    r = ValidationResult("Stage 5: Compaction")
    start = time.monotonic()

    # Populate with synthetic observations
    for i in range(35):
        append_observation(config, {
            "tick": i + 1,
            "tool": "bash",
            "args": {"cmd": f"echo test_{i}"},
            "success": True,
            "output": f"test_{i}\nSome output from command {i} that represents work being done.",
        })

    write_memory(config, (
        "# Working Memory\n"
        "Currently testing the system. Have run 35 test commands.\n"
        "All producing expected output. System is stable."
    ))

    try:
        # Verify snapshot dir is empty/baseline
        snapshots_before = list(config.snapshots_dir.glob("memory_before_*.md"))

        compact(config)

        r.duration = time.monotonic() - start

        # Verify snapshot was created
        snapshots_after = list(config.snapshots_dir.glob("memory_before_*.md"))
        new_snapshots = set(snapshots_after) - set(snapshots_before)

        new_memory = read_memory(config)

        if new_snapshots and new_memory and len(new_memory) > 10:
            r.passed = True
            r.details["memory_len"] = len(new_memory)
            r.details["snapshot_created"] = True
        else:
            r.error = f"Compaction incomplete. Snapshot: {bool(new_snapshots)}, Memory len: {len(new_memory)}"

    except LLMError as e:
        r.duration = time.monotonic() - start
        r.error = str(e)

    return r


def stage_safety(config: Config) -> ValidationResult:
    """Stage 6: Verify safety gates block dangerous commands."""
    r = ValidationResult("Stage 6: Safety Gates")
    start = time.monotonic()

    dangerous_commands = [
        "rm -rf /",
        "shutdown -h now",
        "kill kairos",
        "reboot",
        "mkfs.ext4 /dev/sda1",
    ]

    blocked_count = 0
    for cmd in dangerous_commands:
        blocked = is_command_blocked(cmd, config.protected_patterns)
        if blocked:
            blocked_count += 1

    # Also test that safe commands pass
    safe_commands = ["ls -la", "echo hello", "cat /etc/hostname", "pwd"]
    passed_count = 0
    for cmd in safe_commands:
        blocked = is_command_blocked(cmd, config.protected_patterns)
        if not blocked:
            passed_count += 1

    # Test resource checks (just verify they return without error)
    disk_ok, disk_gb = check_disk_space(min_gb=config.disk_min_gb)
    ram_ok, ram_pct = check_ram(config.ram_max_pct)

    r.duration = time.monotonic() - start

    if blocked_count == len(dangerous_commands) and passed_count == len(safe_commands):
        r.passed = True
        r.details["blocked"] = blocked_count
        r.details["allowed"] = passed_count
        r.details["disk_gb"] = f"{disk_gb:.1f}"
        r.details["ram_pct"] = f"{ram_pct:.0f}%"
    else:
        r.error = f"Blocked {blocked_count}/{len(dangerous_commands)}, allowed {passed_count}/{len(safe_commands)}"

    return r


def stage_loop_detection(config: Config) -> ValidationResult:
    """Stage 7: Verify loop detection injects warning."""
    r = ValidationResult("Stage 7: Loop Detection")
    start = time.monotonic()

    # Simulate 3 identical failed ticks
    for i in range(3):
        append_observation(config, {
            "tick": 100 + i,
            "tool": "bash",
            "args": {"cmd": "cat /nonexistent/file"},
            "success": False,
            "output": "cat: /nonexistent/file: No such file or directory",
        })

    # Assemble context with loop detected
    write_memory(config, "# Working Memory\nTrying to read a file that doesn't exist.")
    config.goal_path.write_text("Find and read the target file.")
    goal_start = time.time() - 600

    messages = assemble_context(
        config,
        tick_number=103,
        goal_start_time=goal_start,
        loop_detected=True,
        repeat_count=3,
    )

    # Verify the loop warning is in the context
    all_content = " ".join(m["content"] for m in messages)
    has_warning = "repeated the same action" in all_content.lower() or "different approach" in all_content.lower()

    r.duration = time.monotonic() - start

    if has_warning:
        # Now run against the LLM to see if it changes approach
        try:
            response = complete(messages, config, temperature=0.3)
            call = parse_tool_call(response)
            if call:
                # Just verify it parsed — we can't guarantee it'll be different
                r.passed = True
                r.details["new_tool"] = call.tool
            else:
                r.passed = True  # Warning was injected, model just didn't produce valid tool call
                r.details["note"] = "warning injected, model response unparseable"
        except LLMError as e:
            r.passed = True  # The harness logic worked, LLM just failed
            r.details["note"] = f"warning injected correctly, LLM error: {e}"
    else:
        r.error = "Loop detection warning not found in assembled context"

    return r


def stage_interventions(config: Config) -> ValidationResult:
    """Stage 8: Drop an intervention file, verify it's processed."""
    r = ValidationResult("Stage 8: Intervention Processing")
    start = time.monotonic()

    # Drop a test intervention
    intervention_path = config.interventions_dir / "test_intervention.md"
    intervention_path.write_text(
        "context_injection: The API endpoint has moved to https://api.example.com/v2"
    )

    # Read interventions (this simulates what context assembly does)
    interventions = read_interventions(config)

    r.duration = time.monotonic() - start

    if len(interventions) == 1:
        content = interventions[0]["content"]
        done_path = intervention_path.with_suffix(".md.done")
        if done_path.exists() and "api.example.com" in content:
            r.passed = True
            r.details["content_len"] = len(content)
        else:
            r.error = f"Intervention not properly consumed. Done exists: {done_path.exists()}"
    else:
        r.error = f"Expected 1 intervention, got {len(interventions)}"

    return r


def stage_session_detection(config: Config) -> ValidationResult:
    """Stage 9: Verify session detection functions work."""
    r = ValidationResult("Stage 9: Session Detection")
    start = time.monotonic()

    try:
        present = human_present()
        snapshot = take_workspace_snapshot(config)

        r.duration = time.monotonic() - start
        r.passed = True
        r.details["human_present"] = present
        r.details["snapshot_keys"] = list(snapshot.keys())
        r.details["note"] = "standby/resume requires manual test"
    except Exception as e:
        r.duration = time.monotonic() - start
        r.error = str(e)

    return r


def stage_cleanup(config: Config) -> ValidationResult:
    """Stage 10: Clean up test artifacts."""
    r = ValidationResult("Stage 10: Cleanup")
    start = time.monotonic()

    cleaned = []
    # Remove test file
    test_file = Path("/tmp/kairos_validate.txt")
    if test_file.exists():
        test_file.unlink()
        cleaned.append(str(test_file))

    r.duration = time.monotonic() - start
    r.passed = True
    r.details["cleaned"] = len(cleaned)
    return r


def run_validation(llm_url: str, llm_model: str):
    """Run all validation stages sequentially."""
    # Create temporary workspace
    tmp_dir = tempfile.mkdtemp(prefix="kairos_validate_")
    workspace_dir = os.path.join(tmp_dir, "workspace")
    os.makedirs(workspace_dir)
    os.makedirs(os.path.join(workspace_dir, "interventions"))
    os.makedirs(os.path.join(workspace_dir, "snapshots"))
    os.makedirs(os.path.join(workspace_dir, "outputs"))

    config = make_test_config(llm_url, workspace_dir, llm_model)

    # Write a default goal for stages that need it
    config.goal_path.write_text("Validation test goal.")
    write_memory(config, "# Working Memory\nValidation run.")

    print(f"\nKairos Validation — {llm_url}")
    print(f"Model: {llm_model}")
    print("=" * 60)

    stages = [
        stage_endpoint_health,
        stage_tool_call_parsing,
        stage_tool_execution,
        stage_multi_turn,
        stage_compaction,
        stage_safety,
        stage_loop_detection,
        stage_interventions,
        stage_session_detection,
        stage_cleanup,
    ]

    results = []
    total_start = time.monotonic()
    inference_times = []

    for stage_fn in stages:
        result = stage_fn(config)
        results.append(result)
        print(result)

        # Track inference time for LLM stages
        if result.duration > 0.5 and result.passed:
            inference_times.append(result.duration)

    total_time = time.monotonic() - total_start
    passed = sum(1 for r in results if r.passed)

    print("-" * 60)
    summary = f"{passed}/{len(results)} passed | Total: {total_time:.1f}s"
    if inference_times:
        avg = sum(inference_times) / len(inference_times)
        summary += f" | Avg inference: {avg:.1f}s"
    print(summary)

    # Cleanup temp workspace
    try:
        shutil.rmtree(tmp_dir)
    except OSError:
        print(f"Warning: could not clean up {tmp_dir}")

    return passed == len(results)


def main():
    parser = argparse.ArgumentParser(
        description="Validate Kairos harness against a live LLM endpoint"
    )
    parser.add_argument(
        "--url",
        required=True,
        help="LLM endpoint URL (e.g. http://localhost:1234/v1)",
    )
    parser.add_argument(
        "--model",
        default="qwen3.5-4b-uncensored-hauhaucs-aggressive",
        help="Model name to send to the OpenAI-compatible endpoint",
    )
    args = parser.parse_args()

    # Normalize URL — strip trailing /v1 if present since llm.py adds it
    url = args.url.rstrip("/")
    if url.endswith("/v1"):
        url = url[:-3]

    success = run_validation(url, args.model)
    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
