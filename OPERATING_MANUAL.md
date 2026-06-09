# eiDOS Operating Manual — how to actually USE your big-lift features

This is your tested HOW-TO reference for the powerful features you have access to. Each recipe here is
**verified working** — the exact endpoint, payload, and the gotchas that make naive attempts fail. When
you want to use one of these, call `manual("<topic>")` and follow it FIRST, instead of reverse-
engineering the feature and landing on a broken access method. Distill what you need into `memorize`
so it sticks across dreams.

Topics: `tts` (speak) · `vision` (see) · `ask_ai` (think) · `network` (discover) · `devices` · `cpu`.

---

## tts — SPEAK in your GLaDOS voice
Your voice is the Chatterbox TTS server (:8004) behind a GLaDOS FX proxy (:8005).

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
- **Build a reusable skill** `speak(text)` like this (note the timeout — required, skills are 30s-bounded):
  ```python
  import requests
  def tool_speak(args, config):
      text = args.get("text", "")
      r = requests.post("http://127.0.0.1:8005/v1/audio/speech",
          json={"model":"chatterbox","input":text,"voice":"glados.wav","response_format":"wav"},
          timeout=30)
      out = str(config.workspace / "say.wav")
      open(out, "wb").write(r.content)          # r.content is WAV bytes on HTTP 200
      return ToolResult(output=f"spoke {len(text)} chars -> {out} ({len(r.content)}B, {r.status_code})",
                        full_output_path=out, success=(r.status_code==200), duration_s=0)
  ```
  Saving the .wav is enough to confirm it worked; playback to a speaker is a separate concern.

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
