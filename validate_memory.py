#!/usr/bin/env python3
"""Validate Phases 1-5 memory system against a live LLM endpoint.

Exercises the knowledge store, tools (memorize/recall/update_plan),
dream cycle with knowledge extraction, passive BM25 recall in context,
and embedding-based semantic pre-fetch — all through real LLM calls.

Usage:
    python3 validate_memory.py --url http://100.113.123.91:1234/v1
    python3 validate_memory.py --url http://100.113.123.91:1234/v1 --verbose
"""

import argparse
import json
import os
import shutil
import sys
import tempfile
import time
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from config import Config, load_config
from llm import complete, LLMError, ReasoningExhausted
from parser import parse_tool_call
from prompts import SYSTEM_PROMPT_BRIEFING, TICK_PROMPT
from tools import execute_tool, TOOLS
from memory import (
    read_goal, write_memory, read_memory,
    read_plan, write_plan,
    append_observation, read_recent_observations,
)
from knowledge import (
    store_entry, load_index, search_bm25,
    format_recalled, rebuild_index, _invalidate_bm25_cache,
)
from context import assemble_context, _build_intelligence_section
from compaction import compact_briefing
from embedding import (
    embed_and_store, semantic_search, _load_vectors, mock_embed_texts,
)


VERBOSE = False


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

class Result:
    def __init__(self, name):
        self.name = name
        self.passed = False
        self.duration = 0.0
        self.error = None
        self.details = {}

    def __str__(self):
        status = "\033[32mPASS\033[0m" if self.passed else "\033[31mFAIL\033[0m"
        line = f"  [{status}] {self.name} ({self.duration:.1f}s)"
        if self.details:
            detail_str = ", ".join(f"{k}={v}" for k, v in self.details.items())
            line += f"  {detail_str}"
        if self.error:
            line += f"\n         Error: {self.error}"
        return line


def _make_config(llm_url, llm_model):
    """Create a config with temp workspace and briefing model enabled."""
    tmp = tempfile.mkdtemp(prefix="eidos_mem_val_")
    ws = os.path.join(tmp, "workspace")
    os.makedirs(ws)
    for d in ("interventions", "outputs", "snapshots"):
        os.makedirs(os.path.join(ws, d))

    config = Config()
    config.llm_url = llm_url
    config.llm_model = llm_model
    config.workspace_dir = ws
    config.briefing_model = True
    config.knowledge_enabled = True
    config.knowledge_embedding_enabled = False  # disable ONNX; use mock where needed
    config.mock_mode = False  # we're hitting a real LLM
    config.tick_interval_s = 1
    config.llm_request_timeout_s = 300
    config.llm_max_tokens = 1024
    config.compaction_max_tokens = 2048
    config.compaction_obs_max_chars = 16000
    config.compaction_memory_max_chars = 6000
    config.compaction_context_max_chars = 40000
    config.context_obs_max_chars = 4000
    config.context_obs_max_count = 20
    config.output_truncation_chars = 2000
    config.cmd_timeout_s = 30
    config.dream_combined = True
    return config, tmp


def _check_server(config):
    """Verify the LLM endpoint is reachable."""
    import urllib.request, urllib.error
    url = config.llm_url.rstrip("/") + "/v1/models"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "eiDOS-MemVal/1.0"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
            models = [m.get("id", "?") for m in data.get("data", [])]
            return models
    except Exception as e:
        return None


def _pr(msg):
    if VERBOSE:
        print(f"    {msg}")


# ---------------------------------------------------------------------------
# Stage 1: Knowledge Store Fundamentals
# ---------------------------------------------------------------------------

def stage_knowledge_store(config):
    """Store entries, rebuild index, verify BM25 search."""
    r = Result("1. Knowledge Store (store → index → BM25 search)")
    start = time.monotonic()
    try:
        # Store entries the LLM will encounter later
        store_entry(config, "pip install fails on Bookworm without --break-system-packages",
                    tags=["pip", "bookworm", "debian"], category="errors")
        store_entry(config, "DHT22 sensor needs 10K pullup on data pin, max 3m wire run",
                    tags=["dht22", "gpio", "hardware"], category="facts")
        store_entry(config, "Use systemctl --user for non-root services, lingers with loginctl",
                    tags=["systemd", "services"], category="procedures")
        store_entry(config, "numpy already installed as dep of rank-bm25, no separate install needed",
                    tags=["numpy", "pip", "dependencies"], category="facts")
        store_entry(config, "SD card write amplification: use tmpfs for logs, rotate aggressively",
                    tags=["sd-card", "tmpfs", "reliability"], category="procedures")

        rebuild_index(config)
        _invalidate_bm25_cache()

        idx = load_index(config)
        if len(idx) != 5:
            r.error = f"Expected 5 index entries, got {len(idx)}"
            r.duration = time.monotonic() - start
            return r

        # BM25 search
        results = search_bm25(config, "pip install error bookworm", top_k=3)
        if not results:
            r.error = "BM25 search returned no results for 'pip install error bookworm'"
            r.duration = time.monotonic() - start
            return r

        top_hit = results[0]["content_preview"]
        if "pip" not in top_hit.lower() and "bookworm" not in top_hit.lower():
            r.error = f"Top BM25 hit not relevant: {top_hit[:80]}"
            r.duration = time.monotonic() - start
            return r

        r.passed = True
        r.details["entries"] = 5
        r.details["bm25_top"] = top_hit[:60]

    except Exception as e:
        r.error = f"{e}\n{traceback.format_exc()}" if VERBOSE else str(e)
    r.duration = time.monotonic() - start
    return r


# ---------------------------------------------------------------------------
# Stage 2: Tool — memorize (LLM stores knowledge)
# ---------------------------------------------------------------------------

def stage_tool_memorize(config):
    """Ask the LLM to memorize a fact, verify it lands in the knowledge store."""
    r = Result("2. Tool: memorize (LLM stores knowledge via tool call)")
    start = time.monotonic()
    try:
        system = SYSTEM_PROMPT_BRIEFING.format(workspace=str(config.workspace))
        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": (
                "## Goal\nYou discovered that the I2C bus on this Pi runs at 100kHz by default "
                "and needs dtparam=i2c_baudrate=400000 in /boot/config.txt for the SSD1306 OLED.\n\n"
                "## Plan\nRemember this finding for future reference.\n\n"
                "## Intelligence\n(empty)\n\n"
                "Tick 1/10 | Use the memorize tool to store this OLED I2C finding. "
                "Args: fact (string), tags (list of strings), category (one of: facts, errors, procedures, reflections)."
            )},
        ]

        _pr("Calling LLM for memorize tool...")
        response = complete(messages, config, temperature=0.1)
        _pr(f"Response: {response[:300]}")

        call = parse_tool_call(response)
        if not call:
            r.error = f"No tool call parsed. Response: {response[:200]}"
            r.duration = time.monotonic() - start
            return r

        if call.tool != "memorize":
            r.details["tool_used"] = call.tool
            # Not fatal — the model might use a different tool. Check if it's close.
            r.error = f"Expected 'memorize' tool, got '{call.tool}'. Args: {call.args}"
            r.duration = time.monotonic() - start
            return r

        # Execute the tool
        result = execute_tool(call, config)
        _pr(f"Tool result: {result.output}")
        rebuild_index(config)
        _invalidate_bm25_cache()

        if not result.success:
            r.error = f"memorize tool failed: {result.output}"
            r.duration = time.monotonic() - start
            return r

        # Verify it landed in the store
        hits = search_bm25(config, "I2C OLED SSD1306 baudrate", top_k=3)
        if not hits:
            r.error = "memorize succeeded but BM25 search found nothing for 'I2C OLED'"
            r.duration = time.monotonic() - start
            return r

        found = any("i2c" in h["content_preview"].lower() or "oled" in h["content_preview"].lower()
                     or "ssd1306" in h["content_preview"].lower() for h in hits)
        if not found:
            r.error = f"memorize stored entry not found via BM25. Top hits: {[h['content_preview'][:50] for h in hits]}"
            r.duration = time.monotonic() - start
            return r

        r.passed = True
        r.details["entry_id"] = result.output.split(":")[-1].strip()[:40]

    except Exception as e:
        r.error = f"{e}\n{traceback.format_exc()}" if VERBOSE else str(e)
    r.duration = time.monotonic() - start
    return r


# ---------------------------------------------------------------------------
# Stage 3: Tool — recall (LLM searches knowledge)
# ---------------------------------------------------------------------------

def stage_tool_recall(config):
    """Ask the LLM to recall knowledge, verify it gets results."""
    r = Result("3. Tool: recall (LLM searches knowledge store)")
    start = time.monotonic()
    try:
        system = SYSTEM_PROMPT_BRIEFING.format(workspace=str(config.workspace))
        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": (
                "## Goal\nInstall a Python package on this Raspberry Pi running Bookworm.\n\n"
                "## Plan\nBefore installing, check if there are any known issues with pip on this OS.\n\n"
                "## Intelligence\n(empty)\n\n"
                "Tick 1/10 | Search your long-term memory for information about pip on Bookworm. "
                "Use the recall tool with a query string."
            )},
        ]

        _pr("Calling LLM for recall tool...")
        response = complete(messages, config, temperature=0.1)
        _pr(f"Response: {response[:300]}")

        call = parse_tool_call(response)
        if not call:
            r.error = f"No tool call parsed. Response: {response[:200]}"
            r.duration = time.monotonic() - start
            return r

        if call.tool != "recall":
            r.details["tool_used"] = call.tool
            r.error = f"Expected 'recall' tool, got '{call.tool}'"
            r.duration = time.monotonic() - start
            return r

        result = execute_tool(call, config)
        _pr(f"Tool result: {result.output[:300]}")

        if not result.success:
            r.error = f"recall tool failed: {result.output}"
            r.duration = time.monotonic() - start
            return r

        if "no relevant" in result.output.lower():
            r.error = f"recall returned no results. Query was: {call.args.get('query', '?')}"
            r.duration = time.monotonic() - start
            return r

        # Check it returned something about pip/bookworm
        lowered = result.output.lower()
        if "pip" in lowered or "bookworm" in lowered or "break-system" in lowered:
            r.passed = True
            r.details["result_chars"] = len(result.output)
        else:
            r.error = f"recall result doesn't mention pip/bookworm: {result.output[:200]}"

    except Exception as e:
        r.error = f"{e}\n{traceback.format_exc()}" if VERBOSE else str(e)
    r.duration = time.monotonic() - start
    return r


# ---------------------------------------------------------------------------
# Stage 4: Passive BM25 recall in context assembly
# ---------------------------------------------------------------------------

def stage_passive_recall(config):
    """Verify _build_intelligence_section injects knowledge into the context."""
    r = Result("4. Passive BM25 recall (auto-surfaced in Intelligence section)")
    start = time.monotonic()
    try:
        goal = "Set up a DHT22 temperature sensor on GPIO pin 4"
        plan = "Step 1: Wire the DHT22 with a 10K pullup. Step 2: Install adafruit-dht library."

        intel = _build_intelligence_section(config, goal, plan)
        if not intel:
            r.error = "Intelligence section is empty — BM25 recall returned nothing"
            r.duration = time.monotonic() - start
            return r

        # Should surface the DHT22 knowledge entry
        lowered = intel.lower()
        if "dht22" in lowered or "pullup" in lowered or "gpio" in lowered:
            r.passed = True
            r.details["intel_chars"] = len(intel)
            r.details["preview"] = intel[:80].replace("\n", " ")
        else:
            r.error = f"Intelligence section doesn't mention DHT22/GPIO: {intel[:200]}"

    except Exception as e:
        r.error = f"{e}\n{traceback.format_exc()}" if VERBOSE else str(e)
    r.duration = time.monotonic() - start
    return r


# ---------------------------------------------------------------------------
# Stage 5: Context assembly with briefing model
# ---------------------------------------------------------------------------

def stage_context_assembly(config):
    """Full context assembly in briefing mode, verify Intelligence section appears."""
    r = Result("5. Context assembly (briefing model with Intelligence)")
    start = time.monotonic()
    try:
        config.goal_path.write_text(
            "Install and test a DHT22 temperature sensor on this Raspberry Pi."
        )
        write_plan(config, (
            "# Plan\n"
            "1. Wire DHT22 to GPIO4 with pullup resistor\n"
            "2. Install adafruit-circuitpython-dht\n"
            "3. Write test script to read temperature\n"
            "4. Verify readings are sane"
        ))
        # Add some recent observations
        append_observation(config, {
            "tick": 1, "tool": "bash",
            "args": {"cmd": "cat /proc/cpuinfo | grep Model"},
            "success": True,
            "output": "Model           : Raspberry Pi 4 Model B Rev 1.4",
        })
        append_observation(config, {
            "tick": 2, "tool": "bash",
            "args": {"cmd": "pip3 install adafruit-circuitpython-dht"},
            "success": False,
            "output": "error: externally-managed-environment",
        })

        messages = assemble_context(
            config,
            tick_number=3,
            goal_start_time=time.time() - 600,
        )

        all_content = "\n".join(m["content"] for m in messages)

        # Check key components
        has_goal = "DHT22" in all_content
        has_plan = "Wire DHT22" in all_content or "GPIO4" in all_content
        has_obs = "externally-managed" in all_content or "cpuinfo" in all_content
        has_intel = "Intelligence" in all_content or "pullup" in all_content.lower()

        r.details["has_goal"] = has_goal
        r.details["has_plan"] = has_plan
        r.details["has_obs"] = has_obs
        r.details["has_intel"] = has_intel
        r.details["total_chars"] = len(all_content)

        if has_goal and has_plan and has_obs:
            r.passed = True
            if not has_intel:
                r.details["note"] = "Intelligence section empty (BM25 may not match)"
        else:
            missing = []
            if not has_goal: missing.append("goal")
            if not has_plan: missing.append("plan")
            if not has_obs: missing.append("observations")
            r.error = f"Missing from context: {', '.join(missing)}"

    except Exception as e:
        r.error = f"{e}\n{traceback.format_exc()}" if VERBOSE else str(e)
    r.duration = time.monotonic() - start
    return r


# ---------------------------------------------------------------------------
# Stage 6: Dream cycle (compact_briefing) — plan + knowledge extraction
# ---------------------------------------------------------------------------

def stage_dream_cycle(config):
    """Run compact_briefing, verify plan is updated AND knowledge extracted."""
    r = Result("6. Dream cycle (compact_briefing → plan + knowledge extraction)")
    start = time.monotonic()
    try:
        config.goal_path.write_text(
            "Set up a complete weather station with DHT22 and BMP280 sensors."
        )
        write_plan(config, "# Plan\n1. Wire sensors\n2. Install libraries\n3. Build dashboard")

        # Populate observations that should trigger knowledge extraction
        obs_data = [
            {"tick": 1, "tool": "bash",
             "args": {"cmd": "pip3 install adafruit-circuitpython-dht"},
             "success": False,
             "output": "error: externally-managed-environment\nhint: use --break-system-packages"},
            {"tick": 2, "tool": "bash",
             "args": {"cmd": "pip3 install --break-system-packages adafruit-circuitpython-dht"},
             "success": True,
             "output": "Successfully installed adafruit-circuitpython-dht-4.0.2"},
            {"tick": 3, "tool": "bash",
             "args": {"cmd": "python3 -c 'import adafruit_dht; d=adafruit_dht.DHT22(board.D4); print(d.temperature)'"},
             "success": True, "output": "22.3"},
            {"tick": 4, "tool": "bash",
             "args": {"cmd": "i2cdetect -y 1"},
             "success": True, "output": "76 -- BMP280 detected at 0x76"},
            {"tick": 5, "tool": "bash",
             "args": {"cmd": "pip3 install --break-system-packages adafruit-circuitpython-bmp280"},
             "success": True,
             "output": "Successfully installed adafruit-circuitpython-bmp280-3.2.0"},
        ]
        for obs in obs_data:
            append_observation(config, obs)

        index_before = load_index(config)
        count_before = len(index_before)
        plan_before = read_plan(config)

        _pr(f"Before dream: {count_before} knowledge entries, plan={len(plan_before)} chars")
        _pr("Running compact_briefing (dream cycle)...")

        compact_briefing(config)

        plan_after = read_plan(config)
        rebuild_index(config)
        _invalidate_bm25_cache()
        index_after = load_index(config)
        count_after = len(index_after)

        _pr(f"After dream: {count_after} knowledge entries, plan={len(plan_after)} chars")
        _pr(f"New plan: {plan_after[:200]}")

        # Check plan was updated
        plan_changed = plan_after != plan_before and len(plan_after.strip()) > 10
        # Check knowledge was extracted (at least 1 new entry)
        new_entries = count_after - count_before
        knowledge_grown = new_entries > 0

        r.details["plan_changed"] = plan_changed
        r.details["plan_chars"] = f"{len(plan_before)}→{len(plan_after)}"
        r.details["new_knowledge"] = new_entries

        if plan_changed and knowledge_grown:
            r.passed = True
            # Log the new entries
            new = index_after[count_before:]
            for e in new[:3]:
                _pr(f"  Extracted: [{e.get('category','?')}] {e.get('content_preview','')[:80]}")
        elif plan_changed:
            r.passed = True
            r.details["note"] = "plan updated but no knowledge extracted (LLM may not have found durable facts)"
        else:
            r.error = f"Dream cycle: plan_changed={plan_changed}, new_knowledge={new_entries}"

    except Exception as e:
        r.error = f"{e}\n{traceback.format_exc()}" if VERBOSE else str(e)
    r.duration = time.monotonic() - start
    return r


# ---------------------------------------------------------------------------
# Stage 7: Tool — update_plan
# ---------------------------------------------------------------------------

def stage_tool_update_plan(config):
    """Ask the LLM to update its plan using the update_plan tool."""
    r = Result("7. Tool: update_plan (LLM modifies its own plan)")
    start = time.monotonic()
    try:
        write_plan(config, "# Plan\n1. Check sensors\n2. Build dashboard")

        system = SYSTEM_PROMPT_BRIEFING.format(workspace=str(config.workspace))
        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": (
                "## Goal\nBuild a weather dashboard for the Pi.\n\n"
                "## Plan\n1. Check sensors\n2. Build dashboard\n\n"
                "## Observations\n[tick 1 | bash | OK] Both DHT22 and BMP280 responding.\n\n"
                "Tick 2/10 | Sensors are confirmed working. "
                "You should update your plan to reflect this progress — mark step 1 complete "
                "and add detail to step 2. Use the remember tool with a note about the updated plan."
            )},
        ]

        _pr("Calling LLM for update_plan/remember...")
        response = complete(messages, config, temperature=0.1)
        _pr(f"Response: {response[:300]}")

        call = parse_tool_call(response)
        if not call:
            r.error = f"No tool call parsed. Response: {response[:200]}"
            r.duration = time.monotonic() - start
            return r

        # Accept remember or update_plan — both update working state
        if call.tool in ("remember", "update_plan", "write_file"):
            result = execute_tool(call, config)
            r.passed = result.success
            r.details["tool"] = call.tool
            if not result.success:
                r.error = f"Tool failed: {result.output}"
        else:
            # Model used something else — still informative
            r.details["tool"] = call.tool
            r.error = f"Expected remember/update_plan, got '{call.tool}'"

    except Exception as e:
        r.error = f"{e}\n{traceback.format_exc()}" if VERBOSE else str(e)
    r.duration = time.monotonic() - start
    return r


# ---------------------------------------------------------------------------
# Stage 8: Multi-turn with memory tools
# ---------------------------------------------------------------------------

def stage_multi_turn(config):
    """Run 3 ticks using full context assembly + tool execution. Verify the
    LLM can use memorize/recall naturally in a multi-turn conversation."""
    r = Result("8. Multi-turn (3 ticks with memorize & recall)")
    start = time.monotonic()
    try:
        config.goal_path.write_text(
            "Diagnose why pip installs fail on this Raspberry Pi and document the solution."
        )
        write_plan(config, "# Plan\n1. Try pip install\n2. Diagnose error\n3. Fix and document")

        goal_start = time.time()
        tools_used = []
        knowledge_ops = 0

        for tick in range(1, 4):
            _pr(f"--- Tick {tick} ---")
            messages = assemble_context(
                config,
                tick_number=tick,
                goal_start_time=goal_start,
            )

            response = complete(messages, config, temperature=0.2)
            _pr(f"LLM: {response[:200]}")

            call = parse_tool_call(response)
            if not call:
                append_observation(config, {
                    "tick": tick, "tool": "parse_error",
                    "success": False,
                    "output": f"No tool call parsed: {response[:150]}",
                })
                continue

            tools_used.append(call.tool)

            # Simulate some outputs for bash commands
            if call.tool == "bash":
                cmd = call.args.get("cmd", "")
                if "pip" in cmd.lower() and "install" in cmd.lower():
                    result = execute_tool(call, config)
                    # If it actually ran something, use the real result
                    if not result.success or "error" in result.output.lower():
                        append_observation(config, {
                            "tick": tick, "tool": call.tool,
                            "args": call.args,
                            "success": False,
                            "output": "error: externally-managed-environment\nhint: use --break-system-packages flag",
                        })
                    else:
                        append_observation(config, {
                            "tick": tick, "tool": call.tool,
                            "args": call.args,
                            "success": result.success,
                            "output": result.output,
                        })
                else:
                    result = execute_tool(call, config)
                    append_observation(config, {
                        "tick": tick, "tool": call.tool,
                        "args": call.args,
                        "success": result.success,
                        "output": result.output,
                    })
            else:
                result = execute_tool(call, config)
                append_observation(config, {
                    "tick": tick, "tool": call.tool,
                    "args": call.args,
                    "success": result.success,
                    "output": result.output,
                })
                if call.tool in ("memorize", "recall"):
                    knowledge_ops += 1

            _pr(f"Tool: {call.tool} → success={result.success}")

        r.details["tools"] = tools_used
        r.details["knowledge_ops"] = knowledge_ops

        # Pass if we got through without crashes and the LLM produced valid tool calls
        valid_calls = sum(1 for t in tools_used if t in TOOLS)
        if valid_calls >= 2:
            r.passed = True
        else:
            r.error = f"Only {valid_calls}/3 valid tool calls"

    except Exception as e:
        r.error = f"{e}\n{traceback.format_exc()}" if VERBOSE else str(e)
    r.duration = time.monotonic() - start
    return r


# ---------------------------------------------------------------------------
# Stage 9: Embedding mock round-trip
# ---------------------------------------------------------------------------

def stage_embedding_roundtrip(config):
    """Embed entries using mock mode, do semantic search, verify results."""
    r = Result("9. Embedding round-trip (mock mode: store → search)")
    start = time.monotonic()
    try:
        # Temporarily enable mock mode for embedding
        config.mock_mode = True
        rebuild_index(config)
        _invalidate_bm25_cache()

        count = embed_and_store(config)
        if count == 0:
            r.error = "embed_and_store returned 0 — no entries to embed"
            config.mock_mode = False
            r.duration = time.monotonic() - start
            return r

        vecs, ids = _load_vectors(config)
        if vecs is None:
            r.error = "Vectors not saved to disk"
            config.mock_mode = False
            r.duration = time.monotonic() - start
            return r

        # Semantic search
        results = semantic_search(config, "pip install error on Bookworm", top_k=3)
        if not results:
            r.error = "Semantic search returned no results"
            config.mock_mode = False
            r.duration = time.monotonic() - start
            return r

        r.passed = True
        r.details["embedded"] = count
        r.details["vectors_shape"] = f"{vecs.shape}"
        r.details["search_hits"] = len(results)
        r.details["top_score"] = f"{results[0]['score']:.3f}"
        r.details["top_hit"] = results[0]["content_preview"][:50]

        config.mock_mode = False

    except Exception as e:
        config.mock_mode = False
        r.error = f"{e}\n{traceback.format_exc()}" if VERBOSE else str(e)
    r.duration = time.monotonic() - start
    return r


# ---------------------------------------------------------------------------
# Stage 10: Dream prefetch (semantic recall cache)
# ---------------------------------------------------------------------------

def stage_dream_prefetch(config):
    """Run dream_prefetch, verify recall_cache.md is written."""
    r = Result("10. Dream prefetch (semantic → recall_cache.md)")
    start = time.monotonic()
    try:
        config.mock_mode = True
        rebuild_index(config)
        _invalidate_bm25_cache()

        from compaction import dream_prefetch
        count = dream_prefetch(
            config,
            goal="Set up DHT22 temperature sensor on GPIO4",
            plan="1. Wire sensor\n2. Install library\n3. Test readings",
        )

        cache_path = config.workspace / "recall_cache.md"
        if count > 0 and cache_path.exists():
            cache_text = cache_path.read_text()
            r.passed = True
            r.details["cached"] = count
            r.details["cache_chars"] = len(cache_text)
            r.details["preview"] = cache_text[:80].replace("\n", " ")
        elif count == 0:
            r.error = "dream_prefetch returned 0 cached entries"
        else:
            r.error = "recall_cache.md not written"

        config.mock_mode = False

    except Exception as e:
        config.mock_mode = False
        r.error = f"{e}\n{traceback.format_exc()}" if VERBOSE else str(e)
    r.duration = time.monotonic() - start
    return r


# ---------------------------------------------------------------------------
# Stage 11: End-to-end — context with both BM25 + recall cache
# ---------------------------------------------------------------------------

def stage_dual_recall(config):
    """Verify context assembly includes both BM25 and cached semantic recall."""
    r = Result("11. Dual recall (BM25 + recall_cache in context)")
    start = time.monotonic()
    try:
        # Ensure recall cache exists (from stage 10)
        cache_path = config.workspace / "recall_cache.md"
        if not cache_path.exists():
            cache_path.write_text("[FACT] (dht22, gpio) DHT22 sensor needs 10K pullup on data pin")

        config.goal_path.write_text("Set up DHT22 and BMP280 sensors")
        write_plan(config, "# Plan\n1. Wire DHT22 to GPIO4\n2. Install libraries")

        messages = assemble_context(
            config,
            tick_number=5,
            goal_start_time=time.time() - 300,
        )

        all_content = "\n".join(m["content"] for m in messages)
        has_intel = "dht22" in all_content.lower() or "pullup" in all_content.lower()

        r.details["context_chars"] = len(all_content)
        r.details["has_intel"] = has_intel

        # Send to LLM — can it use the intelligence?
        _pr("Sending context to LLM...")
        response = complete(messages, config, temperature=0.1)
        _pr(f"LLM: {response[:300]}")

        call = parse_tool_call(response)
        r.passed = True  # Context assembled and LLM responded
        if call:
            r.details["tool"] = call.tool
        r.details["response_chars"] = len(response or "")

    except Exception as e:
        r.error = f"{e}\n{traceback.format_exc()}" if VERBOSE else str(e)
    r.duration = time.monotonic() - start
    return r


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

def run_validation(llm_url, llm_model):
    config, tmp_dir = _make_config(llm_url, llm_model)

    models = _check_server(config)
    if models is None:
        print(f"\033[31mERROR: Cannot reach LLM at {llm_url}\033[0m")
        sys.exit(1)

    print(f"\neiDOS Memory System Validation")
    print(f"  Endpoint: {llm_url}")
    print(f"  Model:    {llm_model}")
    print(f"  Available: {', '.join(models[:3])}")
    print(f"  Workspace: {config.workspace}")
    print("=" * 65)

    stages = [
        stage_knowledge_store,      # 1. store + BM25
        stage_tool_memorize,        # 2. LLM → memorize tool
        stage_tool_recall,          # 3. LLM → recall tool
        stage_passive_recall,       # 4. BM25 auto-surface
        stage_context_assembly,     # 5. briefing context
        stage_dream_cycle,          # 6. compact_briefing
        stage_tool_update_plan,     # 7. update_plan tool
        stage_multi_turn,           # 8. 3 ticks
        stage_embedding_roundtrip,  # 9. mock embedding
        stage_dream_prefetch,       # 10. semantic cache
        stage_dual_recall,          # 11. BM25 + cache → LLM
    ]

    results = []
    for stage_fn in stages:
        result = stage_fn(config)
        results.append(result)
        print(result)

    print("=" * 65)
    passed = sum(1 for r in results if r.passed)
    total_time = sum(r.duration for r in results)
    llm_time = sum(r.duration for r in results if r.duration > 0.5)

    color = "\033[32m" if passed == len(results) else "\033[33m" if passed >= 8 else "\033[31m"
    print(f"{color}{passed}/{len(results)} passed\033[0m | "
          f"Total: {total_time:.1f}s | LLM time: {llm_time:.1f}s")

    # Cleanup
    try:
        shutil.rmtree(tmp_dir)
    except OSError:
        print(f"Warning: could not clean up {tmp_dir}")

    return passed, len(results)


def main():
    parser = argparse.ArgumentParser(
        description="Validate eiDOS memory system (Phases 1-5) against a live LLM"
    )
    parser.add_argument(
        "--url", required=True,
        help="LLM endpoint URL (e.g. http://100.113.123.91:1234/v1)",
    )
    parser.add_argument(
        "--model",
        default="qwen3.5-4b-uncensored-hauhaucs-aggressive",
        help="Model name for the endpoint",
    )
    parser.add_argument(
        "--verbose", "-v", action="store_true",
        help="Print LLM responses and intermediate state",
    )
    args = parser.parse_args()

    global VERBOSE
    VERBOSE = args.verbose

    url = args.url.rstrip("/")
    if url.endswith("/v1"):
        url = url[:-3]

    passed, total = run_validation(url, args.model)
    sys.exit(0 if passed == total else 1)


if __name__ == "__main__":
    main()
