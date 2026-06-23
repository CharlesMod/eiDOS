"""eiDOS voice service (phase 8.3) — standalone GLaDOS TTS + GPU speech-gate.

Split out of dashboard.py so a native TTS/ffmpeg crash can't take the watchdog down with it
(ARCHITECTURE: the supervisor must outlive every subsystem it supervises). Runs as its own
process / nssm service on config.voice_port (default 8098), separate from the dashboard (8099).

Serves exactly four routes:
  GET  /api/speech/events  — SSE: pushes each new clip id to every open browser (no polling)
  GET  /api/speech/stream  — streams GLaDOS audio (WAV) for a clip id, generated on demand
  GET  /api/gpu/wait       — the event-driven GPU speech-gate the house tick yields to
  POST /api/speech/say     — eiDOS submits text to speak (instant return; id pushed via SSE)

CORS-open: the page is served by the dashboard (8099) but the browser opens the SSE + audio
streams directly here (a different port = different origin), so responses carry
Access-Control-Allow-Origin. The browser derives this host from location.hostname, so it works
over localhost AND Tailscale; the service binds 0.0.0.0 like the dashboard. Speech submission
(/api/speech/say) and the GPU gate are called server-side by eidos on 127.0.0.1.
"""

import argparse
import json
import logging
import os
import queue as _queue
import re
import subprocess
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

logger = logging.getLogger("eidos.voice")

# --- Speech push bus: eiDOS notifies the instant it speaks; we push an SSE event to every open
#     browser, which plays immediately. No polling, no arbitrary delay. ---
_speech_subs: set = set()
_speech_subs_lock = threading.Lock()


def speech_publish(sid: str) -> int:
    """Push a new speech-clip id to all connected browsers. Returns how many got it."""
    with _speech_subs_lock:
        subs = list(_speech_subs)
    for q in subs:
        try:
            q.put_nowait(sid)
        except Exception:  # noqa: BLE001 - full/closed queue; client catches up on next event
            pass
    return len(subs)


def speech_subscribe():
    q = _queue.Queue(maxsize=16)
    with _speech_subs_lock:
        _speech_subs.add(q)
    return q


def speech_unsubscribe(q) -> None:
    with _speech_subs_lock:
        _speech_subs.discard(q)


# --- Streaming GLaDOS voice: lazy generation. eiDOS submits TEXT (instant return); the browser pulls
#     /api/speech/stream which generates via Chatterbox's streaming TTS and applies the GLaDOS FX
#     through a live ffmpeg pipe, streaming audio as it's synthesized (low time-to-first-audio,
#     GLaDOS character preserved). No 50s blocking generate, so `speak` can never time out. ---
# ffmpeg resolution, portable: explicit env override → ffmpeg on PATH (the normal case on
# mac/Linux/Pi after `brew install ffmpeg` / `apt install ffmpeg`) → the known WinGet location on
# Windows → bare "ffmpeg" (resolved on PATH at runtime). Voice is an optional feature; if ffmpeg is
# absent the stream just can't start (surfaced in the UI) — it never breaks the core mind.
import shutil as _shutil
_GLADOS_FFMPEG = os.environ.get("GLADOS_FFMPEG") or _shutil.which("ffmpeg")
if not _GLADOS_FFMPEG and os.name == "nt":
    _winget = (r"C:\Users\%s\AppData\Local\Microsoft\WinGet\Packages\Gyan.FFmpeg_Microsoft.Winget."
               r"Source_8wekyb3d8bbwe\ffmpeg-8.0.1-full_build\bin\ffmpeg.exe" % os.environ.get("USERNAME", ""))
    if os.path.isfile(_winget):
        _GLADOS_FFMPEG = _winget
if not _GLADOS_FFMPEG:
    _GLADOS_FFMPEG = "ffmpeg"
# Chain B "buzzy robot" — same FX as glados_proxy.py (keep in sync). The trailing afade kills the
# onset "bzzrrt": the aecho/chorus delay lines fill from silence and burst at sample 0, so a 70ms
# fade-in on the final output masks that transient (and any first-chunk click from the stream).
_GLADOS_FX = ("highpass=f=100,lowpass=f=7000,vibrato=f=6:d=0.12,"
              "chorus=0.6:0.9:55|65:0.5|0.4:0.3|0.45:2.2|1.5,"
              "tremolo=f=72:d=0.18,aecho=0.8:0.75:18:0.22,"
              "equalizer=f=2200:t=q:w=1.2:g=5,alimiter=limit=0.95,"
              "afade=t=in:st=0:d=0.07")

_speech_texts: dict = {}            # id -> text (ephemeral, capped)
_speech_texts_lock = threading.Lock()


def speech_remember(sid: str, text: str) -> None:
    with _speech_texts_lock:
        _speech_texts[sid] = text
        if len(_speech_texts) > 64:  # drop oldest
            for k in list(_speech_texts)[:-64]:
                _speech_texts.pop(k, None)


def speech_text(sid: str) -> str:
    with _speech_texts_lock:
        return _speech_texts.get(sid, "")


# --- GPU speech-gate (event-driven; see ARCHITECTURE_PRINCIPLES.md #1) -------------------
# One GPU. TTS synthesis (here) and the house-model tick (eidos) both want it; when they overlap,
# both slow ~2x. Rather than a sleep/cooldown, we expose an event: while TTS is synthesizing,
# `_tts_active` is raised; eidos blocks on /api/gpu/wait, which returns the instant we notify on
# completion. No polling, no fixed delay — the tick yields to live speech and resumes on the event.
_gpu_cond = threading.Condition()
_tts_active = 0           # in-flight synthesis count (supports overlap); guarded by _gpu_cond
_tts_last_progress = 0.0  # monotonic ts of the last audio byte emitted by ANY active synthesis
_tts_streaming = False    # has audio actually started flowing since the gate became busy?
# Liveness, not duration. A healthy synthesis keeps _tts_last_progress fresh as bytes stream, so
# the gate holds for the WHOLE utterance no matter how long. Startup and steady-state have DIFFERENT
# liveness: time-to-first-byte is variable (model warmup, GPU contention), but once audio flows it
# should be smooth. So we wait generously for speech to START (_GPU_STARTUP_S) and tightly for a
# mid-stream STALL (_GPU_STALL_S). No guess about how long speech "should" take — PRINCIPLES.md #1.
_GPU_STARTUP_S = 12.0  # no first audio byte within this of begin => synthesis never started, bail
_GPU_STALL_S = 5.0     # audio was flowing then stopped for this long => wedged mid-stream, bail
_GPU_MAX_S = 60.0      # absolute ceiling so a tick can never hang even if tracking misfires


def gpu_tts_begin() -> None:
    global _tts_active, _tts_last_progress, _tts_streaming
    with _gpu_cond:
        if _tts_active == 0:           # 0->1: a fresh speech burst; start the startup clock
            _tts_streaming = False
            _tts_last_progress = time.monotonic()
        _tts_active += 1


def gpu_tts_progress() -> None:
    # Hot path (per audio chunk): lock-free writes (atomic under the GIL); the waiter reads under
    # the lock and tolerates a one-cycle-stale value. No notify needed — the waiter re-checks on its
    # own stall timer; only completion (gpu_tts_end) needs an immediate wake.
    global _tts_last_progress, _tts_streaming
    _tts_last_progress = time.monotonic()
    _tts_streaming = True              # audio is flowing -> switch from startup grace to stall watch


def gpu_tts_end() -> None:
    global _tts_active
    with _gpu_cond:
        _tts_active = max(0, _tts_active - 1)
        if _tts_active == 0:
            _gpu_cond.notify_all()  # wake any waiting tick the moment the GPU is free


def gpu_wait_idle(stall_s: float = _GPU_STALL_S, max_s: float = _GPU_MAX_S,
                  startup_s: float = _GPU_STARTUP_S) -> dict:
    """Yield the GPU until TTS synthesis finishes. Holds while audio streams; releases on completion
    (event), if speech never STARTS within `startup_s`, if a flowing stream STALLS for `stall_s`, or
    at `max_s` (backstop). Returns the reason so the caller/telemetry sees why it resumed."""
    start = time.monotonic()
    with _gpu_cond:
        while _tts_active > 0:
            now = time.monotonic()
            if now - start >= max_s:
                return {"idle": False, "reason": "max", "active": _tts_active}
            grace = stall_s if _tts_streaming else startup_s
            if now - _tts_last_progress >= grace:
                return {"idle": False, "reason": "stalled" if _tts_streaming else "no_start",
                        "active": _tts_active}
            # sleep until the sooner of (grace deadline, max deadline); woken early by end()'s notify
            wake = min(_tts_last_progress + grace, start + max_s) - now
            _gpu_cond.wait(timeout=max(0.05, wake))
        return {"idle": True, "reason": "done", "active": 0}


# Chatterbox streams only at chunk boundaries and its own splitter never breaks WITHIN a sentence,
# so a single long sentence generates fully before the first audio byte (TTFA == whole gen). We
# segment here instead, ONE segment per natural boundary (sentence, then clause). Two facts drive
# the design: (1) chunked synthesis runs ~1.0x realtime (the ~0.6s/call overhead eats bf16's
# headroom), so the pipeline has almost no slack -- a later segment bigger than the first WILL
# underrun. (2) But an underrun AT a punctuation boundary is inaudible: it just lengthens a pause
# the listener already expects after a comma or period. So we split only at natural boundaries and
# never pack across them: the first audio lands after just the first short phrase (low TTFA), and
# any catch-up stalls fall on pauses that sound deliberate. We word-cut only a punctuation-free run
# longer than the cap (rare) -- the one place a stall could sound abrupt.
_SPEECH_SEG_MAX = 90   # clause/run longer than this gets split further so no single piece blocks too long
_SPEECH_SEG_MIN = 14   # a fragment this short (e.g. "Boss,") joins the NEXT piece, not its own synth
# Soft boundaries inside a long, comma-free clause: breaking just before one of these reads as a
# natural breath, so a run-on sentence still gets an early first segment (lower TTFA).
_SPEECH_CONNECTIVES = (r"(?:and|but|or|nor|so|yet|because|which|that|while|when|then|after|before|"
                       r"if|though|although|since|with|to)")


def _speech_segments(text: str) -> list:
    """Split `text` into ordered speech segments, one per natural boundary, for low time-to-first-audio.
    Sentence-split; clause-split any over-long sentence; word-cut any punctuation-free run over the cap;
    fold sub-_SPEECH_SEG_MIN fragments into the next piece so there are no clipped micro-bursts."""
    text = " ".join((text or "").split())
    if not text:
        return []

    def word_cut(s: str) -> list:
        out = []
        while len(s) > _SPEECH_SEG_MAX:
            wb = s.rfind(" ", _SPEECH_SEG_MIN, _SPEECH_SEG_MAX)
            cut = wb if wb > 0 else _SPEECH_SEG_MAX
            out.append(s[:cut].strip())
            s = s[cut:].strip()
        if s:
            out.append(s)
        return out

    def split_long(clause: str) -> list:
        # over-long, comma-free clause: break before connectives first (natural), then word-cut
        if len(clause) <= _SPEECH_SEG_MAX:
            return [clause]
        out = []
        for sub in re.split(rf"\s+(?={_SPEECH_CONNECTIVES}\s)", clause):
            sub = sub.strip()
            if sub:
                out.extend(word_cut(sub))
        return out

    pieces: list = []
    for sent in re.split(r"(?<=[.!?])\s+", text):
        sent = sent.strip()
        if not sent:
            continue
        if len(sent) <= _SPEECH_SEG_MAX:
            pieces.append(sent)
            continue
        for clause in re.split(r"(?<=[,;:—–-])\s+", sent):
            clause = clause.strip()
            if clause:
                pieces.extend(split_long(clause))

    # fold tiny fragments into the following piece (or the previous one if it's the trailing piece)
    segs: list = []
    carry = ""
    for p in pieces:
        p = f"{carry} {p}".strip() if carry else p
        carry = ""
        if len(p) < _SPEECH_SEG_MIN:
            carry = p          # too short to stand alone -> prepend to the next piece
        else:
            segs.append(p)
    if carry:
        if segs:
            segs[-1] = f"{segs[-1]} {carry}".strip()
        else:
            segs.append(carry)
    return segs


def stream_glados(text: str, out) -> None:
    """Generate `text` as streaming GLaDOS speech and write WAV bytes to `out` (the browser) as they come:
    Chatterbox /tts stream=true  ->  ffmpeg GLaDOS-FX pipe  ->  out. Best-effort; closes cleanly on error."""
    gpu_tts_begin()  # raise the speech-gate: the house tick will yield until this synthesis ends
    segments = _speech_segments(text)
    if not segments:
        gpu_tts_end()
        return
    # Concatenate per-segment syntheses into ONE continuous GLaDOS-FX stream. ffmpeg reads RAW PCM
    # (s16le 24kHz mono = Chatterbox's native output) so multiple segment WAVs splice seamlessly into
    # one stream — the short first segment plays while later segments are still being generated.
    # CRITICAL for low latency: `-probesize 32 -analyzeduration 0`. The input format is fully
    # specified (raw s16le 24kHz mono), but ffmpeg's DEFAULT is to read ~5s of input to "analyze"
    # the stream before emitting anything -> that alone added ~3.7s to time-to-first-audio. With no
    # probing it starts in ~20ms. `+nobuffer`/`-flush_packets 1` keep it emitting per segment.
    proc = subprocess.Popen(
        [_GLADOS_FFMPEG, "-hide_banner", "-loglevel", "error",
         "-probesize", "32", "-analyzeduration", "0", "-fflags", "+nobuffer",
         "-f", "s16le", "-ar", "24000", "-ac", "1", "-i", "pipe:0",
         "-af", _GLADOS_FX, "-f", "wav", "-flush_packets", "1", "pipe:1"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)

    def _pump_in():
        # Synthesize each segment in order and forward its PCM (minus the 44-byte WAV header) to ffmpeg
        # as it arrives. First-byte latency = generation time of the SHORT first segment only.
        try:
            for seg in segments:
                payload = json.dumps({"text": seg, "voice_mode": "clone",
                                      "reference_audio_filename": "glados.wav",
                                      "output_format": "wav", "stream": True}).encode("utf-8")
                req = urllib.request.Request("http://127.0.0.1:8004/tts", data=payload,
                                             headers={"Content-Type": "application/json"}, method="POST")
                try:
                    resp = urllib.request.urlopen(req, timeout=300)
                except Exception as e:  # noqa: BLE001
                    logger.warning("stream_glados: TTS open failed for segment: %s", e)
                    continue
                skipped = 0  # drop the streaming WAV's 44-byte header; the rest is raw PCM
                try:
                    while True:
                        buf = resp.read(8192)
                        if not buf:
                            break
                        if skipped < 44:
                            drop = min(44 - skipped, len(buf))
                            skipped += drop
                            buf = buf[drop:]
                            if not buf:
                                continue
                        proc.stdin.write(buf)
                    proc.stdin.flush()  # push this segment to ffmpeg now; don't wait on the next synth
                finally:
                    resp.close()
            proc.stdin.close()
        except Exception:  # noqa: BLE001 - browser hung up or ffmpeg died; close and let main exit
            try:
                proc.stdin.close()
            except Exception:  # noqa: BLE001
                pass

    t = threading.Thread(target=_pump_in, daemon=True)
    t.start()
    try:
        while True:
            data = proc.stdout.read(8192)
            if not data:
                break
            out.write(data)
            out.flush()
            gpu_tts_progress()  # audio is flowing -> keep the speech-gate held (liveness, not a timer)
    except (BrokenPipeError, ConnectionResetError, OSError):
        pass
    finally:
        try:
            proc.stdout.close()
        except Exception:  # noqa: BLE001
            pass
        try:
            proc.kill()
        except Exception:  # noqa: BLE001
            pass
        gpu_tts_end()  # lower the speech-gate and notify the waiting tick (synthesis done)


# ---------------------------------------------------------------------------
# HTTP service
# ---------------------------------------------------------------------------

def _qf(q: dict, name: str, default: float, lo: float, hi: float) -> float:
    try:
        return min(max(float((q.get(name) or [default])[0]), lo), hi)
    except (TypeError, ValueError):
        return default


class VoiceHandler(BaseHTTPRequestHandler):

    def log_message(self, *args):  # silence default stderr access log
        pass

    def _respond(self, code: int, ctype: str, body) -> None:
        data = body.encode("utf-8") if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        try:
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass

    def do_OPTIONS(self):  # CORS preflight (harmless; browser uses simple GETs)
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_GET(self):
        if self.path in ("/", "/api/health"):
            self._respond(200, "application/json", json.dumps({"ok": True, "service": "voice"}))

        elif self.path == "/api/speech/events":
            # SSE: push the id of each new speech clip the instant eiDOS speaks (no polling).
            try:
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-cache")
                self.send_header("Connection", "keep-alive")
                self.send_header("X-Accel-Buffering", "no")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(b": connected\n\n")
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, OSError):
                return
            q = speech_subscribe()
            try:
                while True:
                    try:
                        sid = q.get(timeout=15)
                        self.wfile.write(("data: " + json.dumps({"id": sid}) + "\n\n").encode("utf-8"))
                    except _queue.Empty:
                        self.wfile.write(b": ping\n\n")  # heartbeat — also detects disconnects
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass
            finally:
                speech_unsubscribe(q)
            return

        elif self.path.startswith("/api/speech/stream"):
            from urllib.parse import urlparse, parse_qs
            sid = (parse_qs(urlparse(self.path).query).get("id") or [""])[0]
            text = speech_text(sid)
            if not text:
                self._respond(404, "text/plain", "no such utterance")
            else:
                try:
                    self.send_response(200)
                    self.send_header("Content-Type", "audio/wav")
                    self.send_header("Cache-Control", "no-cache")
                    self.send_header("Connection", "close")
                    self.send_header("Access-Control-Allow-Origin", "*")
                    self.end_headers()
                    stream_glados(text, self.wfile)  # streams GLaDOS audio until done/disconnect
                except (BrokenPipeError, ConnectionResetError, OSError):
                    pass
            return

        elif self.path.startswith("/api/gpu/wait"):
            # Event-driven GPU speech-gate: yield until TTS synthesis finishes. Holds while audio
            # streams (liveness), releases on completion (notify), a stall, or the max backstop.
            from urllib.parse import urlparse, parse_qs
            q = parse_qs(urlparse(self.path).query)
            stall = _qf(q, "stall", _GPU_STALL_S, 1.0, 30.0)
            mx = _qf(q, "max", _GPU_MAX_S, 2.0, 180.0)
            startup = _qf(q, "startup", _GPU_STARTUP_S, 1.0, 60.0)
            self._respond(200, "application/json", json.dumps(gpu_wait_idle(stall, mx, startup)))

        else:
            self._respond(404, "text/plain", "not found")

    def do_POST(self):
        if self.path == "/api/speech/say":
            # eiDOS submits TEXT to speak (instant). We remember it + push the id; the browser pulls
            # /api/speech/stream which generates the streaming GLaDOS audio on demand.
            try:
                length = int(self.headers.get("Content-Length", 0) or 0)
                body = json.loads(self.rfile.read(length) or b"{}") if length else {}
                sid = str(body.get("id") or "")
                text = (body.get("text") or "").strip()
            except Exception:  # noqa: BLE001
                sid, text = "", ""
            if sid and text:
                speech_remember(sid, text)
                n = speech_publish(sid)
                self._respond(200, "application/json", json.dumps({"ok": True, "id": sid, "delivered": n}))
            else:
                self._respond(400, "application/json", json.dumps({"ok": False, "error": "need id+text"}))
        else:
            self._respond(404, "text/plain", "not found")


def main():
    parser = argparse.ArgumentParser(description="eiDOS voice service (GLaDOS TTS + GPU speech-gate)")
    parser.add_argument("--config", default="config.toml", help="Path to config file")
    parser.add_argument("--port", type=int, default=None, help="Override voice port")
    args = parser.parse_args()

    port = args.port
    if port is None:
        try:
            from config import load_config
            port = getattr(load_config(args.config), "voice_port", 8098)
        except Exception:  # noqa: BLE001 - voice must boot even if config is unreadable
            port = 8098

    server = ThreadingHTTPServer(("0.0.0.0", port), VoiceHandler)
    server.daemon_threads = True
    print(f"[voice] GLaDOS TTS + GPU speech-gate on http://0.0.0.0:{port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[voice] shutting down")
        server.shutdown()


if __name__ == "__main__":
    main()
