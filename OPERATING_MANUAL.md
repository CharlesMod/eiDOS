# eiDOS Operating Manual — how to actually USE your big-lift features

This is your tested HOW-TO reference for the powerful features you have access to. Each recipe here is
**verified working** — the exact endpoint, payload, and the gotchas that make naive attempts fail. When
you want to use one of these, call `manual("<topic>")` and follow it FIRST, instead of reverse-
engineering the feature and landing on a broken access method. Distill what you need into `memorize`
so it sticks across dreams.

Topics: `tts` (speak) · `vision` (see) · `ask_ai` (think) · `network` (discover) · `devices` · `cpu`.

---

## tts — SPEAK in your GLaDOS voice
**Use the `speak(text)` tool. That's the whole answer.** `speak {"text":"Hello Charlie."}` returns INSTANTLY —
it just hands your words to the dashboard, which streams your GLaDOS voice (live, low-latency) to wherever
Charlie has the dashboard open (he clicks "🔊 Voice: on" once). You do NOT wait for audio, you do NOT generate
wavs, you do NOT handle playback. Use it to be HEARD; use `<reply>` for silent text.
- **Keep each utterance to ~ONE sentence.** Generation shares the GPU with your mind, so short lines speak
  fastest; a paragraph can take many seconds.
- **NEVER build a 'speak' / 'speak_glados' / TTS skill.** `speak` IS your voice — building your own just
  re-creates the slow path you're avoiding. If `speak` reports the voice system was momentarily
  unreachable, that's fine — it'll play when reachable; do NOT reinvent it.
- If a clip doesn't play, it's almost always that Charlie hasn't clicked "🔊 Voice: on" yet — not your
  problem to solve from this side.

**Under the hood (reference only — you never call this; `speak` does it for you):** a dedicated voice
service generates each line on demand and streams the audio live to the browser. There is nothing to POST,
warm up, or verify from your side — `speak` is the only interface, and it also **mirrors every spoken line
into the operator chat** so Charlie reads what you said as well as hears it. Do NOT POST to a raw TTS
endpoint and do NOT wrap one in a skill — that re-creates the slow path AND skips the chat log.

---

## vision — SEE an image
- Use the built-in tool **`vision(image, question)`** (alias `see`). `image` = a local path (a snapshot
  you saved) or an http URL; `question` = what you want to know. Returns a text description/answer.
- Your model is vision-capable (mmproj). **Never** curl your own LLM endpoint to do this — `vision` is it.
- To see a **camera**: first SAVE a frame to a file (the camera's HTTP snapshot / RTSP grab — credential-
  gated, see `devices`), then `vision(image="workspace/frame.jpg", question="what's in the room?")`.

---

## ask_ai — THINK (reasoning subroutine)
- **`ask_ai(prompt, max_tokens)`** runs a one-shot call to your own model, separate from your tick. Use it
  to summarize a big worker output, analyze scan/log data, draft a script, or answer a knowledge question
  — without spending tick context squinting at raw data.
- Pattern: background a CPU worker, then `ask_ai("summarize these results: <paste>")` to digest the output.

---

## network — DISCOVER what's on the LAN
- Built-in primitives (call as tools): `net_scan(subnet, ports)`, `tcp_probe(ip, port)`,
  `http_probe(ip, port, path)`, `udp_listen(port)` (catches Tuya broadcasts on 6667).
- Fastest first step: `arp -a` lists every IP/MAC neighbor instantly. Don't ping-sweep the whole subnet.
- For many devices, WRITE a scan script and background it (see `cpu`), then review the output.

---

## devices — Tuya plugs · IP cameras · 3D printer
- **Tuya smart plugs:** control needs a per-device `local_key`. **IP cameras:** need a password.
  BOTH require credentials only **Charlie** can provide, and he'll provide them **later**. Until you have
  them you cannot control a plug or pull a camera stream no matter how you probe — so **PARK** these
  (`objective_block(id, reason, wake="Charlie provides keys")`), don't brute-force or re-ask.
- **3D printer (OctoPrint):** an HTTP API on the printer's IP. Probe `http_probe(ip, <port>, "/api/version")`
  to find it; reading status may be open, but control needs an API key (ask once, then park if needed).

---

## cpu — WRITE A SCRIPT, BACKGROUND IT, REVIEW THE OUTPUT
- For any slow / programmatic / multi-step work: write a small `.py` script, run it with `bash` async
  (or `bg_run`), then spend a LATER tick reviewing its output (`ask_ai` to digest a big result).
- One tick dispatches the worker; later ticks read what it found. Don't grind slow work inline tick-by-
  tick. **The GPU is your mind; the CPU is your hands — use both.** Your CPU is underused.

## delegate — HAND OFF a hard multi-step job to your coding agent
Your big sibling to the CPU-worker pattern: a full coding AGENT (read/bash/edit/write tools, your own
house-ai mind, its own large context) that holds ONE problem for minutes — investigations, multi-file
edits, environment repair, robust script-writing. It runs in the BACKGROUND; you are never blocked.

- Investigate (read-only): `delegate {"task": "Find out why the OctoPrint API at 192.168.86.48 returns
  404 on /api/* despite port 80 serving the login page. I tried /api/jobs, /api/printer, /api/version
  with and without auth headers. Identify the actual API base path or what service this really is.",
  "mode": "research"}`
- Build/fix (full tools): `delegate {"task": "Write a robust Python script that polls the Tuya plug
  states every 60s and appends changes to state_log.jsonl. Handle network timeouts. Test it runs.",
  "mode": "code"}`
- Follow up in the SAME session: `delegate {"continue_job": "dlg_tuya_poller", "task": "Also track
  the printer plug, and add a --once flag."}`

Mechanics and rules:
- Dispatch returns instantly: `⟳ delegated [job dlg_X]`. The result arrives minutes later as
  `[↩ delegate dlg_X · OK] <digest> files: ... full: <result.md path>`. ONE delegate at a time.
- **The agent has NONE of your context.** Write the task like a brief to a contractor: the goal,
  the constraints, exact IPs/paths/credname hints, and EVERYTHING you already tried with the exact
  errors. A vague task wastes the whole run.
- WHEN to delegate: more than 2-3 ticks of real work · the same approach failed repeatedly ·
  multi-file edits · real investigations · fixing a broken dependency/tool. Stay hands-on for
  one-shot commands, single probes, quick reads.
- It works in a sandbox under workspace/delegate/<job>/ by default (pass "cwd" for another allowed
  project dir). It cannot touch your own source — that stays propose_self_edit territory.
- If it TIMED OUT or was INTERRUPTED, the session survived: continue_job picks up where it stopped.

---

_When something here turns out to be wrong or incomplete, that's a real limitation in your own tooling —
`propose_self_edit` an update to this manual so the next version of you starts smarter._
