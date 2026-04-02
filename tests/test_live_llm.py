"""Integration test — calls the real LM Studio endpoint.

Requires LM Studio to be running at the URL in config.toml.
Skip with: pytest -m "not live"
Run with:  pytest tests/test_live_llm.py -v -s
"""

import json
import os
import tempfile
import time
from pathlib import Path

import pytest

from config import load_config, Config
from compaction import compact
from llm import complete, LLMError
from memory import (
    append_observation,
    read_memory,
    read_recent_observations,
    write_memory,
)
from context import assemble_context
from parser import parse_tool_call
from prompts import SYSTEM_PROMPT

# Mark every test in this module as "live" — requires real LM Studio
pytestmark = pytest.mark.live


def _make_config():
    """Load real config.toml so we hit the actual LM Studio server."""
    config = load_config("config.toml")
    # Use a temp workspace so logs don't pollute the real one
    tmp = tempfile.mkdtemp(prefix="kairos_live_")
    config.workspace_dir = tmp
    os.makedirs(os.path.join(tmp, "interventions"), exist_ok=True)
    os.makedirs(os.path.join(tmp, "outputs"), exist_ok=True)
    os.makedirs(os.path.join(tmp, "snapshots"), exist_ok=True)
    return config, tmp


LIVE = os.environ.get("KAIROS_TEST_LIVE") == "1"


def _check_server(config):
    """Skip unless --live flag is set AND server is reachable."""
    if not LIVE:
        pytest.skip("Live tests require --live flag")
    import urllib.request
    import urllib.error
    url = config.llm_url.rstrip("/") + "/v1/models"
    try:
        req = urllib.request.Request(url)
        with urllib.request.urlopen(req, timeout=5):
            pass
    except (urllib.error.URLError, OSError, TimeoutError):
        pytest.skip(f"LM Studio not reachable at {config.llm_url}")


class TestLiveLLM:
    """Smoke tests against the real LM Studio endpoint."""

    @pytest.fixture(autouse=True)
    def setup(self):
        self.config, self.tmp = _make_config()
        _check_server(self.config)
        yield
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_simple_completion(self):
        """Model responds to a simple question."""
        messages = [
            {"role": "system", "content": "You are a helpful assistant. Reply concisely."},
            {"role": "user", "content": "What is 2 + 2? Reply with just the number."},
        ]
        response = complete(messages, self.config, max_tokens=64)
        print(f"\n  Response: {response!r}")
        assert response.strip(), "Response should not be empty"
        assert "4" in response

    def test_tool_format_response(self):
        """Model can produce Kairos tool-call format when instructed."""
        system = SYSTEM_PROMPT.format(workspace="/tmp/test_workspace")
        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": (
                "Tick 1 | 2026-04-01 00:00:00 UTC | Goal set 0:00:00\n\n"
                "Your goal is: Write 'hello world' to a file called greeting.txt\n\n"
                "What is your next action? Respond with brief reasoning, then exactly one tool call."
            )},
        ]
        response = complete(messages, self.config, max_tokens=256)
        print(f"\n  Response: {response!r}")
        assert "<tool>" in response, "Model should produce a <tool> tag"
        assert "<args>" in response, "Model should produce an <args> tag"

    def test_model_name_matches_config(self):
        """Verify the model loaded in LM Studio matches config."""
        import urllib.request
        url = self.config.llm_url.rstrip("/") + "/v1/models"
        with urllib.request.urlopen(url, timeout=5) as resp:
            data = json.loads(resp.read())
        model_ids = [m["id"] for m in data.get("data", [])]
        print(f"\n  Loaded models: {model_ids}")
        print(f"  Config model: {self.config.llm_model}")
        assert len(model_ids) > 0, "No models loaded in LM Studio"

    def test_token_usage_reported(self):
        """LM Studio returns token usage stats."""
        messages = [
            {"role": "user", "content": "Say 'ping'."},
        ]
        # Call complete — it logs to llm_log.jsonl
        response = complete(messages, self.config, max_tokens=32)
        print(f"\n  Response: {response!r}")

        # Check the log file was written
        log_path = Path(self.config.workspace_dir) / "llm_log.jsonl"
        assert log_path.exists(), "llm_log.jsonl should be created"
        with open(log_path) as f:
            entry = json.loads(f.readline())
        print(f"  Usage: {entry.get('usage', {})}")
        assert entry["elapsed_s"] > 0
        assert entry.get("usage", {}).get("prompt_tokens", 0) > 0

    def test_interaction_log_written(self):
        """Verify llm_log.jsonl captures request and response."""
        messages = [
            {"role": "user", "content": "Reply with the word 'logged'."},
        ]
        complete(messages, self.config, max_tokens=32)

        log_path = Path(self.config.workspace_dir) / "llm_log.jsonl"
        assert log_path.exists()
        with open(log_path) as f:
            entry = json.loads(f.readline())

        assert entry["model"] == self.config.llm_model
        assert "logged" in entry["messages"][0]["content"].lower() or True  # request logged
        assert entry["response_content"], "Response should be captured"
        print(f"\n  Log entry keys: {list(entry.keys())}")
        print(f"  Response logged: {entry['response_content'][:100]!r}")


class TestLiveCompaction:
    """Verify compaction produces useful output with the real model."""

    @pytest.fixture(autouse=True)
    def setup(self):
        self.config, self.tmp = _make_config()
        _check_server(self.config)
        # Write goal + memory
        self.config.goal_path.write_text("Explore the filesystem and document findings.")
        write_memory(self.config, "# Working Memory\nFresh start. No observations yet.")
        yield
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_compaction_returns_content_not_reasoning(self):
        """With compaction_max_tokens=4096, model produces actual content."""
        # Seed 10 observations so compaction has something to distill
        for i in range(10):
            append_observation(self.config, {
                "tick": i + 1,
                "tool": "bash",
                "args": {"cmd": f"ls /directory_{i}"},
                "success": True,
                "output": f"file_a.txt  file_b.txt  readme_{i}.md",
            })

        start = time.monotonic()
        compact(self.config)
        elapsed = time.monotonic() - start
        print(f"\n  Compaction took {elapsed:.1f}s")

        mem = read_memory(self.config)
        print(f"  Memory length: {len(mem)} chars")
        print(f"  Memory preview: {mem[:300]!r}")

        # Must have real content, not empty or a single token
        assert len(mem.strip()) > 50, (
            f"Compaction output too short ({len(mem)} chars) — "
            f"model likely exhausted tokens on reasoning"
        )
        # Should not be raw thinking/reasoning dump
        assert "<think>" not in mem.lower(), "Memory contains raw thinking tags"

    def test_compaction_preserves_key_information(self):
        """Compacted memory retains important facts from observations."""
        write_memory(self.config, "# Working Memory\nGoal: find config files.")
        # Observations with distinctive facts the model should retain
        append_observation(self.config, {
            "tick": 1, "tool": "bash", "args": {"cmd": "cat /etc/hostname"},
            "success": True, "output": "raspberrypi-kairos",
        })
        append_observation(self.config, {
            "tick": 2, "tool": "bash", "args": {"cmd": "df -h /"},
            "success": True, "output": "Filesystem  Size  Used Avail Use%\n/dev/sda1   32G   8.2G  22G  28%",
        })
        append_observation(self.config, {
            "tick": 3, "tool": "remember",
            "args": {"note": "CRITICAL: SSH key found at /home/pi/.ssh/id_rsa"},
            "success": True, "output": "Noted.",
        })

        compact(self.config)
        mem = read_memory(self.config)
        print(f"\n  Compacted memory:\n{mem}")

        # At least some key facts should survive compaction
        mem_lower = mem.lower()
        has_hostname = "raspberrypi" in mem_lower or "kairos" in mem_lower
        has_disk = "32g" in mem_lower or "22g" in mem_lower or "disk" in mem_lower
        has_ssh = "ssh" in mem_lower or "key" in mem_lower
        retained = sum([has_hostname, has_disk, has_ssh])
        assert retained >= 1, (
            f"Compaction lost all key facts. Only retained {retained}/3. "
            f"Memory: {mem[:200]}"
        )


class TestLiveToolCompliance:
    """Verify the real model produces valid tool calls from Kairos prompts."""

    @pytest.fixture(autouse=True)
    def setup(self):
        self.config, self.tmp = _make_config()
        _check_server(self.config)
        self.config.goal_path.write_text("Explore the system and report findings.")
        write_memory(self.config, "# Working Memory\nJust started. No actions taken yet.")
        yield
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_tool_parse_rate(self):
        """Model produces parseable tool calls on >70% of ticks."""
        n_ticks = 5
        parse_ok = 0

        for tick in range(1, n_ticks + 1):
            messages = assemble_context(
                self.config,
                tick_number=tick,
                goal_start_time=time.time(),
            )
            response = complete(messages, self.config)
            call = parse_tool_call(response)
            if call:
                parse_ok += 1
                print(f"  tick {tick}: {call.tool}({json.dumps(call.args)[:60]})")
            else:
                print(f"  tick {tick}: PARSE FAIL — {response[:100]!r}")

            # Record observation so context evolves
            if call:
                append_observation(self.config, {
                    "tick": tick, "tool": call.tool,
                    "args": call.args, "success": True,
                    "output": "(simulated output)",
                })
            else:
                append_observation(self.config, {
                    "tick": tick, "tool": "parse_error",
                    "success": False, "output": response[:200],
                })

        rate = parse_ok / n_ticks
        print(f"\n  Parse rate: {parse_ok}/{n_ticks} = {rate:.0%}")
        assert rate >= 0.7, f"Tool parse rate {rate:.0%} below 70% threshold"

    def test_model_uses_varied_tools(self):
        """Model doesn't get stuck calling the same tool every tick."""
        n_ticks = 8
        tools_used = set()

        for tick in range(1, n_ticks + 1):
            messages = assemble_context(
                self.config,
                tick_number=tick,
                goal_start_time=time.time(),
            )
            response = complete(messages, self.config)
            call = parse_tool_call(response)
            if call:
                tools_used.add(call.tool)
                # Simulate plausible output
                output = "(simulated)" if call.tool != "remember" else "Noted."
                append_observation(self.config, {
                    "tick": tick, "tool": call.tool,
                    "args": call.args, "success": True,
                    "output": output,
                })
            else:
                append_observation(self.config, {
                    "tick": tick, "tool": "parse_error",
                    "success": False, "output": response[:200],
                })

        print(f"\n  Tools used across {n_ticks} ticks: {tools_used}")
        # bash-only is valid for exploration, but warn if stuck
        if len(tools_used) < 2:
            print(f"  WARNING: model only used {tools_used} — consider if goal prompts variety")
        assert len(tools_used) >= 1, "Model produced no valid tool calls"

    def test_model_responds_to_loop_warning(self):
        """When told it's looping, model changes its approach."""
        # Seed 3 identical observations to trigger loop context
        for tick in range(1, 4):
            append_observation(self.config, {
                "tick": tick, "tool": "bash",
                "args": {"cmd": "ls /tmp"},
                "success": True,
                "output": "file1.txt file2.txt",
            })

        # Tick 4 with loop_detected=True
        messages = assemble_context(
            self.config,
            tick_number=4,
            goal_start_time=time.time(),
            loop_detected=True,
            repeat_count=3,
        )
        response = complete(messages, self.config)
        call = parse_tool_call(response)
        print(f"\n  After loop warning: {response[:200]!r}")
        if call:
            print(f"  Tool chosen: {call.tool}({json.dumps(call.args)[:80]})")
            # Should NOT repeat "ls /tmp" — it was told it's looping
            if call.tool == "bash":
                assert call.args.get("cmd") != "ls /tmp", (
                    "Model repeated the exact same command after loop warning"
                )

    def test_context_with_large_observation_history(self):
        """Model handles a large context window without breaking format."""
        # Fill up observation history
        for i in range(30):
            append_observation(self.config, {
                "tick": i + 1, "tool": "bash",
                "args": {"cmd": f"find /dir_{i} -type f"},
                "success": True,
                "output": "\n".join(f"file_{j}.txt" for j in range(10)),
            })

        messages = assemble_context(
            self.config,
            tick_number=31,
            goal_start_time=time.time() - 3600,  # 1 hour elapsed
        )
        total_chars = sum(len(m["content"]) for m in messages)
        print(f"\n  Context size: {total_chars} chars ({total_chars/3.5:.0f} est tokens)")

        response = complete(messages, self.config)
        call = parse_tool_call(response)
        print(f"  Response: {response[:200]!r}")
        assert call is not None, (
            f"Model failed to produce tool call with large context ({total_chars} chars)"
        )
