# eiDOS voice latency — work log & handoff

Purpose: hand this to a fresh chat so it can continue the eiDOS realtime-voice work without re-deriving
everything. Written 2026-06-09 on host **gamingPC** (Windows, RTX 5080 16GB). Operator = **Dean** ("Boss").

---

## System context (what's running)

- **eiDOS** = always-on autonomous house AI (extends the Kairos tick-loop framework). Repo:
  `C:\Users\cmod\llm\Kairos`. Mind = **house-ai** (Gemma-4-12B, llama.cpp) at `http://127.0.0.1:8081`,
  OpenAI-compatible, **think-OFF** (`reasoning=0`). One eiDOS, normally already running.
- **TTS** = Chatterbox-TTS-Server (GLaDOS voice clone), repo `C:\Users\cmod\llm\Chatterbox-TTS-Server`,
  HTTP `:8004/tts`. Model = `chatterbox-turbo`, native sample rate **24000 Hz mono s16le**.
- **Dashboard** = `dashboard.py` at `:8099` (the `EidosDashboard` nssm service). It runs the watchdog
  and spawns eidos as a child. It owns the voice path: eidos POSTs text to `/api/speech/say`; the
  browser pulls `/api/speech/stream?id=…`, which runs `stream_glados()` → Chatterbox → ffmpeg GLaDOS-FX
  → browser audio.
- Services (nssm; `nssm.exe` at `C:\Users\cmod\llm\bin\nssm\nssm.exe`): `EidosDashboard`(8099),
  `HouseAI-Llama`(8081), `HouseAI-Chatterbox`(8004), `HouseAI-GladosFX`(8005), `EidosTap`(8088).
- Tailscale IP **100.113.123.91** (Dean reaches :8099 / :9100 from his MacBook).

## Hard constraints / gotchas (READ)

- **One GPU, 16 GB.** house-ai (~15.7 GB) is resident. Don't load a 2nd model copy.
- **Windows PowerShell 5.1 only** (no `&&`, no `??`). Run Python with `PYTHONUTF8=1`.
- Run eidos/dashboard with the **`.venv` python**: `C:\Users\cmod\llm\Kairos\.venv\Scripts\python.exe`.
- **Restart discipline:** dashboard is the `EidosDashboard` service → reload code with
  `Restart-Service EidosDashboard`. If killing a PID, use PowerShell `Stop-Process -Id <pid> -Force`
  (git-bash mangles `taskkill /PID`). **Never** `taskkill /T` the dashboard (eidos is its child).
- eidos boots **paused**; resume with `POST http://127.0.0.1:8099/api/control/resume` (also clears the
  `workspace/paused` sentinel). Status: `GET /api/control/status`.
- **Off-limits to eidos's self-edit** (`git_safety.PROTECT_PATHS`): dashboard.py, config*, safety files,
  llm.py, skills.py. The OPERATOR/Claude CAN edit these — the list only gates eidos's own self-edit tool.
- Repo publishes CODE ONLY; `workspace/` is gitignored. Commit to `main` (the watchdog's git-safety
  checkpoints + auto-rollback operate on `main`; do NOT branch).
- **Don't install packages** into the Chatterbox embedded python without explicit Dean authorization.
- Standing principle (`ARCHITECTURE_PRINCIPLES.md` #1): **event-driven over polled** — interrupt/notify
  → bounded blocking acquire → poll only as last resort. No `sleep(N)`-and-hope.

---

## PART 1 — TTS leg: DONE, committed `f8b31ed`, live

**Problem:** time-to-first-audio (TTFA) was 4.3 s (short) to 19 s (long) even uncontended.

**Three independent root causes, all fixed:**

1. **bf16 was OFF.** `engine.py` `BF16_ENABLED` reads env `TTS_BF16`, default **off → T3 in fp32**
   (~2× slower; decode is memory-bandwidth bound). Uncontended RTF was **1.7**.
   → Set `TTS_BF16=on` on the service:
   `nssm set HouseAI-Chatterbox AppEnvironmentExtra "PYTHONUTF8=1" "PYTHONIOENCODING=utf-8" "TTS_BF16=on"`
   then `Restart-Service HouseAI-Chatterbox`. RTF → **0.95**. Blackwell supports bf16 natively; it also
   LOWERS T3 VRAM. **NOT in git — it's service config.** Revert = remove the env var.

2. **No real sub-sentence streaming.** Chatterbox `stream:true` only yields at chunk boundaries and its
   splitter never breaks WITHIN a sentence → a single long sentence generates fully before byte 1.
   → Rewrote `stream_glados()` in `dashboard.py`: `_speech_segments()` splits at natural boundaries
   (sentence → clause → connective, word-cut only as last resort), synthesizes **segment-by-segment**,
   strips each segment's 44-byte WAV header, and splices **raw PCM** into ONE continuous ffmpeg
   GLaDOS-FX stream. First short phrase plays while the rest generates.
   - Key tuning: `_SPEECH_SEG_MAX=90`, `_SPEECH_SEG_MIN=14`, plus a connective list.
   - Physics: chunked synth runs RTF ≈ 1.0 (≈0.6 s/call overhead eats bf16 headroom). With ~zero
     buffer, a later segment **bigger than the first** underruns. BUT underruns land on punctuation
     boundaries, so they sound like natural pauses, not glitches. Verified **zero boundary clicks**.

3. **Hidden ffmpeg probe buffer (the silent killer).** ffmpeg buffered **~3.7 s** before ANY output,
   "analyzing" a raw stream whose format we already fully specify. `-probesize 32 -analyzeduration 0`
   → ~20 ms. (Do NOT use `-avioflags direct` — it truncates pipe output.) Final ffmpeg cmd in
   `stream_glados`: `-probesize 32 -analyzeduration 0 -fflags +nobuffer -f s16le -ar 24000 -ac 1
   -i pipe:0 -af <FX> -f wav -flush_packets 1 pipe:1`. Also `proc.stdin.flush()` after each segment.

**Result (live, end-to-end through `:8099`):**

| utterance            | before (fp32, no stream) | after  |
|----------------------|--------------------------|--------|
| short 1-sentence     | 4.3 s                    | 2.8 s  |
| multi-sentence       | 4.3 s                    | 1.7 s  |
| long run-on sentence | 19 s                     | 4.4 s  |
| long no-comma        | 19 s                     | 4.7 s  |

Files changed in `f8b31ed`: `dashboard.py` (stream_glados + segmenter), plus the earlier event-driven
GPU speech-gate liveness redesign in `gpu_gate.py` + `ARCHITECTURE_PRINCIPLES.md`.

---

## PART 2 — chat→voice end-to-end delay: DIAGNOSED, NOT FIXED (the live problem)

**Dean's report:** in chat, "the whole text paragraph landed, then TTS spun up, then it emitted — a
good dozen seconds." He wants the first sentence generated in <1 s (warm) so TTS can start on it.

**Root cause = serial pipeline, NOT slow TTS and NOT slow per-token LLM.** Flow in `eidos.py` (~line
824–880): the tick calls `response = complete(messages, config, …)` which generates the ENTIRE tick
output (thought + tool + `<reply>`) as one blocking call; THEN `parse_reply()`; THEN
`_auto_speak(reply_text)` (eidos.py ~159) POSTs the opening 1–2 sentences to `/api/speech/say`; THEN
TTS runs. Nothing overlaps, and `<reply>` typically TRAILS the other tokens.

**Measured LLM floor (house-ai :8081, warm, streaming):**
- Decode **~53 tok/s**. TTFT **68 ms with a cached prefix** (cache_prompt=True is already set), 717 ms
  cold for a ~2.4k prompt, 131 ms small prompt.
- Each tick generates **~256–319 completion tokens** over a **~2,400-token** prompt (`reasoning=0`).
  At 53 tok/s, 285 tokens ≈ **5.4 s before the reply text even exists**, then +2–3 s TTS ≈ the dozen s.

**The fix (not yet built): pipeline LLM→TTS.** Plumbing already exists:
- `complete()` in `llm.py` (~line 118) already supports **SSE streaming via an `on_token` callback**
  and `cache_prompt: True`. So we can watch the token stream, detect `<reply>` content as it arrives,
  and POST each completed sentence to `/api/speech/say` immediately — overlapping TTS with the rest of
  generation. Pair with getting `<reply>` emitted EARLY (reply-first) so its first sentence is among
  the first tokens (~0.5–1 s after TTFT).

**Honest physics floor:** first *text* sentence can be <1 s, but first *audio* floors at ~2–2.5 s
because Chatterbox must still SYNTHESIZE that first phrase (RTF≈1). Realistic target: **first audio
~2.5 s (from ~12 s); first text <1 s.** To push first-audio lower you'd shrink the very first spoken
chunk (e.g. speak "Boss —" first), accepting choppier prosody.

**Three candidate approaches (Dean has NOT chosen — he said hold):**
- **A. Stream tick + reply-first (recommended):** stream the existing tick; nudge the model to emit
  `<reply>` first on Boss messages; push each reply sentence to TTS as it streams. One LLM call,
  spoken text == chat text. First audio ~2.5–3 s.
- **B. Dedicated chat fast-path:** on a Boss message, a separate tiny streaming call generates a 1–2
  sentence spoken reply immediately (full agent tick runs after, for actions). Lowest perceived
  latency (~1.5–2.5 s), matches the event-driven preference, but adds an LLM call and the spoken reply
  is separate from the tick's text.
- **C. Both** (fast-path now, stream the main tick later).

Relevant code: `eidos.py` `_auto_speak` (~159), `_first_sentences` (~145), tick reply/dispatch
(~824–892); `llm.py` `complete()` (~118) and `_read_stream`; prompts in `prompts.py` / `context.py`
(chat-reply instructions, e.g. context.py ~370–382, ~574). Note: there's a 2nd tiny LLM call (~prompt
158 / ~20 tok) ~1 s after each tick — not the bottleneck, but identify it before refactoring.

---

## Current state (as of handoff)

- TTS leg committed (`f8b31ed`) and deployed; dashboard restarted on new code.
- `TTS_BF16=on` set on the `HouseAI-Chatterbox` service (persists across reboot; not in git).
- **eidos is RUNNING / unpaused** — chat works now; only the serial delay remains.
- Part-2 pipelining is **diagnosed but untouched.** Awaiting Dean's choice of approach A/B/C.
- To hear voice in chat: open `:8099`, turn the **Voice toggle ON** (browsers need one user click
  before autoplay), then ask eidos to say something "out loud" (it calls `speak()` when it wants to be
  HEARD, vs silent `<reply>`).

## Measurement scripts (in `workspace/`, gitignored — reusable)

- `ttfa_bench.py` — TTFA/RTF vs Chatterbox `:8004` directly (short/long, split variants).
- `pipeline_test.py` — simulates the dashboard segmenter + per-segment synth; reports TTFA + gap slack.
- `e2e_speak.py` — full path through `:8099`; saves WAVs to `workspace/speech_samples/`.
- `click_check.py` — scans saved WAVs for boundary clicks / silence runs.
- `ffmpeg_lat.py` — isolates ffmpeg first-output latency (how the `-analyzeduration 0` fix was found).
- `llm_speed.py` — house-ai TTFT + decode tok/s (small vs large vs cached prompt).
