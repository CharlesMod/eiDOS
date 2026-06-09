# eiDOS Operating Manual — how to actually USE your big-lift features

This is your tested HOW-TO reference for the powerful features you have access to. Each recipe here is
**verified working** — the exact endpoint, payload, and the gotchas that make naive attempts fail. When
you want to use one of these, call `manual("<topic>")` and follow it FIRST, instead of reverse-
engineering the feature and landing on a broken access method. Distill what you need into `memorize`
so it sticks across dreams.

Topics: `tts` (speak) · `vision` (see) · `ask_ai` (think) · `network` (discover) · `devices` · `cpu`.

---

## tts — SPEAK in your GLaDOS voice
**Use the `speak(text)` tool. That's the whole answer.** `speak {"text":"Hello Boss."}` returns INSTANTLY —
it just hands your words to the dashboard, which streams your GLaDOS voice (live, low-latency) to wherever
Boss has the dashboard open (he clicks "🔊 Voice: on" once). You do NOT wait for audio, you do NOT generate
wavs, you do NOT handle playback. Use it to be HEARD; use `<reply>` for silent text.
- **Keep each utterance to ~ONE sentence.** Generation shares the GPU with your mind, so short lines speak
  fastest; a paragraph can take many seconds.
- **NEVER build a 'speak' / 'speak_glados' / TTS skill.** `speak` IS your voice — building your own just
  re-creates the slow path you're avoiding. If `speak` reports the voice system was momentarily
  unreachable, that's fine — it'll play when reachable; do NOT reinvent it.
- If a clip doesn't play, it's almost always that Boss hasn't clicked "🔊 Voice: on" yet — not your problem
  to solve from this side.

The rest of this section is the raw pipeline, for reference only. Your voice is the Chatterbox TTS server
(:8004) behind a GLaDOS FX proxy (:8005); the dashboard now streams it through a live ffmpeg FX pipe.

- **Endpoint:** `POST http://127.0.0.1:8005/v1/audio/speech`
  (`:8005` = full GLaDOS effect — use this. `:8004` = the raw clone with no effects.)
- **Body (JSON):**
  ```json
  {"model":"chatterbox","input":"What to say.","voice":"glados.wav","response_format":"wav"}
  ```
- **Returns:** `audio/wav` bytes (~150 KB for a short line, HTTP 200).
- **The three gotchas that make it fail (this is why it's hard):**
  1. The path is **`/v1/audio/speech`**. POSTing to the root `/` returns **405 Method Not Allowed**.
  2. `voice` must be the filename **`"glados.wav"`** (a file in `reference_audio/`), NOT `"glados"`
     → that returns **404 "Voice file not found"**. (`glados_golden.wav` also works.)
  3. `response_format` must be **`"wav"`**. `"mp3"`/`"opus"` return **500 "Failed to encode audio"**.
- **Easiest — ONE tool call, no skill, no `requests`:** use the built-in `http_request` tool:
  ```
  http_request {"method":"POST","url":"http://127.0.0.1:8005/v1/audio/speech",
    "json":{"model":"chatterbox","input":"<what to say>","voice":"glados.wav","response_format":"wav"},
    "save":"say.wav"}
  ```
  → it POSTs the JSON, gets the `audio/wav` back, and saves it to `workspace/say.wav` (binary responses are
  auto-saved). Saving the .wav confirms it worked; playback to a speaker is a separate concern.
- To make it a named `speak(text)` skill, have the skill call `http_request` internally — **never** `import
  requests` in a skill (the skill runner can lack it; `http_request` is stdlib and always works).

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
  BOTH require credentials only **Boss** can provide, and he'll provide them **later**. Until you have
  them you cannot control a plug or pull a camera stream no matter how you probe — so **PARK** these
  (`objective_block(id, reason, wake="Boss provides keys")`), don't brute-force or re-ask.
- **3D printer (OctoPrint):** an HTTP API on the printer's IP. Probe `http_probe(ip, <port>, "/api/version")`
  to find it; reading status may be open, but control needs an API key (ask once, then park if needed).

---

## cpu — WRITE A SCRIPT, BACKGROUND IT, REVIEW THE OUTPUT
- For any slow / programmatic / multi-step work: write a small `.py` script, run it with `bash` async
  (or `bg_run`), then spend a LATER tick reviewing its output (`ask_ai` to digest a big result).
- One tick dispatches the worker; later ticks read what it found. Don't grind slow work inline tick-by-
  tick. **The GPU is your mind; the CPU is your hands — use both.** Your CPU is underused.

---

_When something here turns out to be wrong or incomplete, that's a real limitation in your own tooling —
`propose_self_edit` an update to this manual so the next version of you starts smarter._
