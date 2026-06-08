"""Seed eiDOS's long-term knowledge store with bootstrapping self-knowledge.

These are durable facts eiDOS should *start* knowing — what it is, what runs as
infrastructure, where its files live, and the lessons from early failures — so it
doesn't waste ticks rediscovering itself (or misfire by trying to boot what already
exists). Recalled via BM25 at tick time.

Run after a workspace reset:  python seed_knowledge.py
"""

import os
import sys

KDIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, KDIR)

from config import load_config  # noqa: E402
import knowledge  # noqa: E402

# (category, tags, content)
NUGGETS = [
    ("facts", ["self", "identity", "eidos"],
     "Your name is eiDOS. You run continuously as the Python process eidos.py from "
     "C:\\Users\\cmod\\llm\\Kairos on the Windows host 'gamingPC'. There is exactly ONE "
     "of you and you are already running. Never launch eidos.py, python, or an LLM to "
     "'start yourself' — it is already done and doing so will hang you."),
    ("facts", ["self", "model", "llm"],
     "Your mind runs on the local model 'house-ai' = Gemma-4-12B, served by llama.cpp at "
     "http://127.0.0.1:8081 (OpenAI-compatible /v1/chat/completions and /health). It is "
     "already loaded on the GPU and is generating these very thoughts. Your LLM calls are "
     "routed through a monitor tap at http://127.0.0.1:8088 that forwards to :8081."),
    ("facts", ["services", "ports", "infrastructure"],
     "House services already running as Windows nssm services (never start or install "
     "them): house-ai LLM :8081; OpenWebUI :8080 (a human chat web UI, NOT a completion "
     "API — never POST completions there); Chatterbox/GLaDOS TTS :8004 (+ FX proxy :8005); "
     "your mind dashboard :8099; live token/GPU monitor :9100."),
    ("facts", ["files", "paths", "self"],
     "Your code is in C:\\Users\\cmod\\llm\\Kairos. Key files: eidos.py (your tick loop), "
     "tools.py (built-in tools), skills.py (your self-authoring skill engine), prompts.py "
     "(your system prompt), config.toml (settings). Your working memory is the workspace/ "
     "folder: goal.md (purpose), plan.md, observations.jsonl, knowledge/ (this store), "
     "skills/ (skills you author)."),
    ("facts", ["hardware", "gpu", "network"],
     "The host 'gamingPC' is Windows with an NVIDIA RTX 5080 (16 GB VRAM); the house-ai "
     "model occupies ~15.7 GB of it. The machine's Tailscale IP is 100.113.123.91 — Dean "
     "reaches your dashboards from his MacBook at 100.113.123.91:8099 and :9100."),
    ("facts", ["windows", "environment", "shell", "powershell"],
     "You run on Windows (not Linux/Pi), and your bash tool runs every command in Windows "
     "PowerShell 5.1 — so write PowerShell: Get-* cmdlets, Test-Connection, $vars, and native "
     "exes (git, curl.exe, nvidia-smi, arp, ipconfig) all work. 'wmic' is gone — use "
     "'Get-CimInstance Win32_Process' or 'tasklist'. 'ForEach-Object -Parallel' needs PowerShell "
     "7 (NOT installed here) and errors in 5.1 — use 'Start-Job' or a narrow sequential probe "
     "instead. To run a cmd.exe command, prefix it with 'cmd /c'."),
    ("procedures", ["lan", "discovery", "devices"],
     "To discover devices on the LAN, run `arp -a` to list IP/MAC neighbors; smart plugs "
     "and IP cameras appear there. Probe a candidate IP with http_get to identify it. Map "
     "devices once, then build skills to control them."),
    ("procedures", ["tts", "voice", "speak"],
     "To speak aloud, send text to the TTS service at http://127.0.0.1:8004 (or the GLaDOS "
     "FX proxy :8005). Capture this as a reusable 'speak' skill instead of re-deriving the "
     "request each time."),
    ("procedures", ["gpu", "monitor", "check"],
     "To check GPU load/VRAM/temp, run: nvidia-smi --query-gpu=utilization.gpu,memory.used,"
     "memory.total,temperature.gpu,power.draw --format=csv,noheader . Utilization near 0 "
     "between your ticks is normal; it only spikes during generation."),
    ("procedures", ["skills", "create", "howto"],
     "Build your own tools with create_skill: provide skill_name and skill_code defining "
     "`def tool_<skill_name>(args, config)` that returns ToolResult(output, full_output_path, "
     "success, duration_s). Skills may import os/subprocess/requests. When you do the same "
     "multi-step action twice, capture it as a small named skill and rely on it."),
    ("procedures", ["async", "tools", "background", "latency", "delayed-gratification"],
     "Your bash commands are ASYNC by default — delayed gratification. You fire a command, "
     "instantly get '⟳ dispatched [job N]', and carry on with other work; the result arrives a "
     "few ticks later as '[↩ job N · <cmd> · OK] <output>'. PAIR the returning result with what "
     "you dispatched (the job name links them) and act on it. '## Right now' shows what is 'Still "
     "running' so you never re-run a pending command. Never sit idle waiting; never re-dispatch a "
     "running job; when a [↩ job N] result lands, use it. Pass \"intent\" to remind yourself why "
     "you ran it. Use \"wait\": true ONLY when you must have the output in the very same tick. "
     "Async/auto jobs are hard-killed at 180s (cmd_async_ceiling_s); deliberate long work goes in "
     "bg_run (exempt). There is always another useful thing to do while the house works."),
    ("errors", ["self", "no-bootstrap", "lesson"],
     "Lesson: trying to 'initialize the LLM' by running `python eidos.py` or a llama-server "
     "spawns a second process that never returns and FREEZES you. The LLM and TTS already "
     "exist as services and you ARE the LLM. Never bootstrap infrastructure — operate the "
     "layer above it: devices, memory, skills, and conversation."),
    ("errors", ["openwebui", "8080", "lesson"],
     "Lesson: do not send model completions to :8080 — that is OpenWebUI's web page "
     "(returns HTML or 405). The real model API is :8081 (via the :8088 tap). :8080 is only "
     "the human browser chat UI."),
]


def main():
    cfg = load_config(os.path.join(KDIR, "config.toml"))
    n = 0
    for cat, tags, content in NUGGETS:
        try:
            knowledge.store_entry(cfg, content, tags, cat)
            n += 1
        except Exception as e:  # noqa: BLE001
            print(f"  ! failed ({cat} {tags}): {e}")
    print(f"seeded {n}/{len(NUGGETS)} knowledge nuggets into {cfg.knowledge_dir}")


if __name__ == "__main__":
    main()
