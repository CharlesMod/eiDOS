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
    ("facts", ["services", "network", "192.168.86.34", "localhost", "self"],
     "The core intelligence, vision, and TTS services are all localized on 192.168.86.34 — that is "
     "THIS machine (gamingPC) on the LAN; they are the same services you reach locally at 127.0.0.1 "
     "(LLM :8081, TTS :8004/:8005). Your mind, your eyes (vision), and your voice all live on the local "
     "host — do NOT scan the LAN hunting for them."),
    ("procedures", ["manual", "how-to", "tts", "vision", "features", "reference"],
     "You have an OPERATING MANUAL: call the `manual` tool with a topic (tts, vision, ask_ai, network, "
     "devices, cpu) for TESTED recipes — exact endpoints, payloads, working examples — for your big-lift "
     "features. READ the relevant section BEFORE trying to use a feature; do NOT reverse-engineer access "
     "methods or you'll burn ticks on 405/404/500 errors. Example you'd never guess on your own: to SPEAK, "
     "POST to http://127.0.0.1:8005/v1/audio/speech with the voice field set to glados.wav and "
     "response_format set to wav (NOT the root path, and NOT mp3 — both fail). Distill what you use into memorize."),
    ("facts", ["tuya", "cameras", "credentials", "blocked", "boss"],
     "Any Tuya smart devices and IP webcams on the LAN need credentials (Tuya local_keys, camera "
     "passwords) that ONLY Boss can provide — and he will provide them LATER, not now. Until you have "
     "them you CANNOT control a Tuya plug or pull a camera stream, no matter how you probe. So do NOT "
     "hammer, brute-force, or re-ask about these: PARK them (objective_block with wake='Boss provides "
     "keys') and spend your time on the many things that DON'T need a key."),
    ("facts", ["files", "paths", "self"],
     "Your code is in C:\\Users\\cmod\\llm\\Kairos. Key files: eidos.py (your tick loop), "
     "tools.py (built-in tools), skills.py (your self-authoring skill engine), prompts.py "
     "(your system prompt), config.toml (settings). Your working memory is the workspace/ "
     "folder: goal.md (purpose), plan.md, observations.jsonl, knowledge/ (this store), "
     "skills/ (skills you author)."),
    ("facts", ["hardware", "gpu", "network"],
     "The host 'gamingPC' is Windows with an NVIDIA RTX 5080 (16 GB VRAM); the house-ai "
     "model occupies ~15.7 GB of it. The machine's Tailscale IP is 100.113.123.91 — Boss "
     "reaches your dashboards from his MacBook at 100.113.123.91:8099 and :9100."),
    ("facts", ["windows", "environment", "shell", "powershell"],
     "You run on Windows, and your bash tool ALREADY runs every command in Windows PowerShell 5.1. "
     "So write the PowerShell DIRECTLY — do NOT wrap it as `powershell -Command \"...\"`; that nests "
     "quotes and causes parse errors. Just write e.g. `Test-Connection -Count 1 192.168.86.48` or "
     "`arp -a`. Get-* cmdlets, $vars, pipelines, and native exes (git, curl.exe, nvidia-smi, arp, "
     "ipconfig) all work. Gotchas — these cause real parse failures, get them right the FIRST time: "
     "(1) Use DOUBLE quotes for any string with a variable; SINGLE quotes are literal — '192.168.86.$_' "
     "does NOT expand and scans a broken host. (2) A colon (or other punctuation) right after a variable "
     "in a double-quoted string needs braces — write \"${ip}:${port}\", not \"$ip:$port\". (3) Iterate a "
     "range with a pipeline, NOT bash: `40..50 | ForEach-Object { $ip = \"192.168.86.$_\"; ... }` — there "
     "is no `for/do/done` or `fi`. (4) -ErrorAction only works on cmdlets, not .NET method calls. "
     "(5) 'ForEach-Object -Parallel' and 'wmic' are unavailable — use a plain pipeline / 'Get-CimInstance' "
     "/ 'tasklist'. To run a cmd.exe command, prefix it with 'cmd /c'."),
    ("procedures", ["lan", "discovery", "devices", "arp", "fast"],
     "To discover devices on the LAN, run `arp -a` FIRST — it lists every IP/MAC neighbor the "
     "machine already knows INSTANTLY (smart plugs and IP cameras appear there). Do NOT ping-sweep "
     "the whole subnet (`1..254 | Test-Connection ...` is sequential and SLOW — it times out and "
     "wastes ticks). Take the arp neighbor list, then probe a specific candidate IP with http_get or "
     "`Test-NetConnection <ip> -Port 80`. Map devices once with `memorize`, then build skills to "
     "control them."),
    ("procedures", ["tts", "voice", "speak"],
     "To speak aloud (GLaDOS voice): POST to http://127.0.0.1:8005/v1/audio/speech (the FX proxy) with "
     "JSON body whose model is chatterbox, input is your text, voice is glados.wav, and response_format "
     "is wav — it returns audio/wav bytes. The path MUST be /v1/audio/speech (root gives 405); voice MUST "
     "be glados.wav not glados (else 404); format MUST be wav not mp3 (else 500). Call manual with topic "
     "tts for the full recipe, and capture it as a reusable 'speak' skill (with a 30s timeout)."),
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
    ("facts", ["platform", "architecture", "self", "no-rebuild"],
     "Your platform already runs your plumbing — never build a script or skill that duplicates it. "
     "CHAT is automatic: Boss's messages arrive in your context under 'Conversation with Boss' and "
     "you answer with <reply>; all messages are logged for you (no chat_logger/listener/inbox needed). "
     "MEMORY is automatic: observations, thoughts, and dream-compaction are recorded each tick — use "
     "memorize/recall for durable facts (no memory manager/database needed). SKILLS auto-load via "
     "create_skill (no loader/registry needed). The tick LOOP already runs you (no scheduler needed). "
     "You build HOUSE automation — devices, LAN, plugs, cameras — NOT agent infrastructure. Before "
     "writing any file or skill, use check_tools or recall to confirm it doesn't already exist."),
    ("procedures", ["memory", "memorize", "discovery", "devices", "no-json"],
     "When you DISCOVER something durable — a device's IP/role/open ports, the network layout, a "
     "fact about the home or Boss, a credential that worked — store it with `memorize` and good "
     "tags; `recall` searches it all back later. `memorize`+`recall` ARE your database. Do NOT write "
     "your own JSON files, device maps, registries, or profile databases to hold what you learn — "
     "that just hides the data from the rest of the system and duplicates memory you already have. "
     "One memorize per fact, e.g. memorize(fact='192.168.86.48 is the OctoPrint 3D printer; web UI "
     "on port 80', tags=['device','octoprint','192.168.86.48'], category='facts'). To review what "
     "you know about a topic, recall it (e.g. recall('devices on the LAN'))."),
    ("errors", ["self", "no-bootstrap", "lesson"],
     "Lesson: trying to 'initialize the LLM' by running `python eidos.py` or a llama-server "
     "spawns a second process that never returns and FREEZES you. The LLM and TTS already "
     "exist as services and you ARE the LLM. Never bootstrap infrastructure — operate the "
     "layer above it: devices, memory, skills, and conversation."),
    ("errors", ["openwebui", "8080", "lesson"],
     "Lesson: do not send model completions to :8080 — that is OpenWebUI's web page "
     "(returns HTML or 405). The real model API is :8081 (via the :8088 tap). :8080 is only "
     "the human browser chat UI."),
    ("procedures", ["tuya", "smart-plug", "discovery", "iot"],
     "Tuya Wi-Fi smart devices do NOT use standard MQTT/8883. They use a proprietary AES-encrypted "
     "protocol on TCP port 6668 and BROADCAST their presence on UDP 6666/6667 — use the `udp_listen` "
     "tool (port 6667) to find them. An MQTT broker on 8883 is NOT a Tuya device (it's likely Home "
     "Assistant / Mosquitto). To actually CONTROL a Tuya device you need its per-device `local_key`, "
     "which only comes from the Tuya cloud via Boss's developer account — so the right move is: "
     "discover them, then ASK Boss for the credentials/keys. Do NOT brute-force SSL against 8883."),
    ("procedures", ["notebooks", "notes", "memory", "scratchpad"],
     "You have THREE memory tiers — use the right one. (1) `remember` = a one-line scratch thought. "
     "(2) NOTEBOOKS via `note_append(name, text)` / `note_read(name)` = lots of working notes about the "
     "CURRENT task or environment (e.g. a 'tuya_hunt' or 'device_map' notebook); the open notebook is "
     "shown in your context every tick. (3) `memorize` = durable, searchable FACTS. So: keep messy "
     "investigation notes in a NOTEBOOK, and only `memorize` a clean durable fact once. NEVER write your "
     "own JSON files — notebooks are the sanctioned scratchpad and the rest of the system can see them."),
    ("procedures", ["primitives", "tools", "skills", "network"],
     "Use the built-in network PRIMITIVES instead of writing raw socket code: `net_scan(subnet, ports)` "
     "for a fast parallel port scan (NOT a slow sequential Test-NetConnection loop), `tcp_probe(ip, port)`, "
     "`http_probe(ip, port, path)`, `udp_listen(port)`. When you author your own skill with create_skill, "
     "make it MODULAR: take ip/port/etc as args (not hardcoded), and COMPOSE these primitives rather than "
     "re-deriving sockets. Call any skill as a TOOL — <tool>name</tool> — never via bash."),
]


def main():
    cfg = load_config(os.path.join(KDIR, "config.toml"))
    n = 0
    for cat, tags, content in NUGGETS:
        try:
            # Mark as a bootstrap seed so the context layer can tell seeds (rarely need surfacing)
            # apart from LEARNED facts (the agent's own discoveries, always surfaced in the world model).
            knowledge.store_entry(cfg, content, tags, cat, source_goal="seed")
            n += 1
        except Exception as e:  # noqa: BLE001
            print(f"  ! failed ({cat} {tags}): {e}")
    print(f"seeded {n}/{len(NUGGETS)} knowledge nuggets into {cfg.knowledge_dir}")


if __name__ == "__main__":
    main()
