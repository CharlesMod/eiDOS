#!/usr/bin/env python3
"""eiDOS dashboard — operator shell: web UI + supervisor/watchdog + voice pipeline.

Four responsibilities (v2 phase 8 splits them into separate processes):
  UI         — HTML dashboard + /api/status,/api/ping,/api/activity read models
  SUPERVISOR — watchdog (spawn/respawn/crash-loop auto-rollback), /api/control/*,
               git safety, self-edit apply, self-guide apply (the trust boundary)
  VOICE      — GLaDOS TTS streaming (/api/speech/say + /api/speech/stream + SSE)
  GPU GATE   — /api/gpu/wait liveness-bounded speech arbitration

Writes: paused/should_run/pid sentinels, chat_hold.json, interventions/,
self_guide.md, watchdog crash notes, and the source tree via git restore /
self-edit apply. Stdlib only — no frameworks, no dependencies.
"""

import argparse
import json
import logging
import sys
import time
from http.server import HTTPServer, ThreadingHTTPServer, BaseHTTPRequestHandler
from pathlib import Path

# Add project root for imports
sys.path.insert(0, str(Path(__file__).parent))

logger = logging.getLogger("dashboard")

from config import load_config, Config
from ascii_art import get_creature
from persona import load_persona, compute_level
from telemetry import get_cpu_pct


def _read_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text())
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}


def _read_text(path: Path) -> str:
    try:
        return path.read_text()
    except (FileNotFoundError, OSError):
        return ""


_LAST_TOOL_SKIP = {"system", "watchdog", "dream", "thought", "planning", "__no_tool__"}


def _last_tool_call(config: Config) -> dict:
    """Most recent *real* tool call from observations.jsonl, for the tool bubble.

    Skips meta entries (thoughts, planning, watchdog/system, dream). Returns a small
    dict {tool, ok, summary, tick} or None.
    """
    path = config.workspace / "observations.jsonl"
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return None
    for ln in reversed(lines[-80:]):
        try:
            o = json.loads(ln)
        except Exception:  # noqa: BLE001
            continue
        tool = o.get("tool")
        if not tool or tool in _LAST_TOOL_SKIP:
            continue
        args = o.get("args") or {}
        summ = ""
        if isinstance(args, dict):
            summ = (args.get("cmd") or args.get("command") or args.get("path")
                    or args.get("url") or args.get("skill_name") or "")
        return {
            "tool": tool,
            "ok": bool(o.get("success")),
            "summary": str(summ)[:64],
            "tick": o.get("tick"),
        }
    return None


def _tail_jsonl(path: Path, n: int = 20) -> list:
    try:
        lines = path.read_text().strip().splitlines()
        result = []
        for line in lines[-n:]:
            try:
                result.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return result
    except (FileNotFoundError, OSError):
        return []


def _compute_narration(heartbeat: dict, persona: dict, goal: str, flavor: dict) -> str:
    """Derive a status narration from current state."""
    failures = heartbeat.get("consecutive_failures", 0)
    tick = heartbeat.get("tick", 0)
    uptime = heartbeat.get("uptime_s", 0)
    mood = persona.get("mood", "curious")
    streak = persona.get("current_streak", 0)

    if failures >= 3:
        return "Struggling... something isn't working. Might need a different approach."
    if not goal.strip():
        return "No goal set. Waiting for instructions."
    if tick <= 1:
        return "Just woke up. Getting my bearings."
    if mood == "triumphant":
        return "Just finished a goal. Feeling accomplished."
    if mood == "frustrated":
        return "Running into walls. Need to think differently."
    if mood == "struggling":
        return "Things are rough but not giving up."
    if streak > 20:
        return f"Good flow \u2014 {streak} successful actions in a row."
    if uptime and uptime > 86400:
        days = uptime / 86400
        return f"Been at this for {days:.1f} days. Steady progress."
    if mood == "focused":
        return "Locked in. Making progress."
    if mood == "determined":
        return "Working through challenges. Pushing forward."
    return "Working on it. One step at a time."


def build_knowledge_list(config: Config) -> dict:
    """Read last 10 knowledge entries from index."""
    idx_path = config.workspace / "knowledge" / "index.json"
    try:
        entries = json.loads(idx_path.read_text())
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        entries = []
    entries.sort(key=lambda e: e.get("created", ""), reverse=True)
    return {"entries": entries[:25]}



# --- Speech push bus: eiDOS notifies the dashboard the instant it speaks; the dashboard pushes an
#     SSE event to every open browser, which plays immediately. No polling, no arbitrary delay. ---
import threading as _sp_threading
import queue as _sp_queue

_speech_subs: set = set()
_speech_subs_lock = _sp_threading.Lock()


def speech_publish(sid: str) -> int:
    """Push a new speech-clip id to all connected browsers. Returns how many got it."""
    with _speech_subs_lock:
        subs = list(_speech_subs)
    for q in subs:
        try:
            q.put_nowait(sid)
        except Exception:  # noqa: BLE001 - full/closed queue; client will catch up on next event
            pass
    return len(subs)


def speech_subscribe():
    q = _sp_queue.Queue(maxsize=16)
    with _speech_subs_lock:
        _speech_subs.add(q)
    return q


def speech_unsubscribe(q) -> None:
    with _speech_subs_lock:
        _speech_subs.discard(q)


# --- Streaming GLaDOS voice: lazy generation. eiDOS submits TEXT (instant return); the browser pulls
#     /api/speech/stream which generates via Chatterbox's streaming TTS and applies the GLaDOS FX through
#     a live ffmpeg pipe, streaming audio to the browser as it's synthesized (low time-to-first-audio,
#     GLaDOS character preserved). No 50s blocking generate, so `speak` can never time out. ---
import subprocess as _sp_subprocess
import os as _sp_os
import re as _sp_re

_GLADOS_FFMPEG = _sp_os.environ.get(
    "GLADOS_FFMPEG",
    r"C:\Users\cmod\AppData\Local\Microsoft\WinGet\Packages\Gyan.FFmpeg_Microsoft.Winget.Source_8wekyb3d8bbwe\ffmpeg-8.0.1-full_build\bin\ffmpeg.exe")
if not _sp_os.path.isfile(_GLADOS_FFMPEG):
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
_speech_texts_lock = _sp_threading.Lock()


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
import time as _gpu_time
_gpu_cond = _sp_threading.Condition()
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
            _tts_last_progress = _gpu_time.monotonic()
        _tts_active += 1


def gpu_tts_progress() -> None:
    # Hot path (per audio chunk): lock-free writes (atomic under the GIL); the waiter reads under
    # the lock and tolerates a one-cycle-stale value. No notify needed — the waiter re-checks on its
    # own stall timer; only completion (gpu_tts_end) needs an immediate wake.
    global _tts_last_progress, _tts_streaming
    _tts_last_progress = _gpu_time.monotonic()
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
    start = _gpu_time.monotonic()
    with _gpu_cond:
        while _tts_active > 0:
            now = _gpu_time.monotonic()
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
        for sub in _sp_re.split(rf"\s+(?={_SPEECH_CONNECTIVES}\s)", clause):
            sub = sub.strip()
            if sub:
                out.extend(word_cut(sub))
        return out

    pieces: list = []
    for sent in _sp_re.split(r"(?<=[.!?])\s+", text):
        sent = sent.strip()
        if not sent:
            continue
        if len(sent) <= _SPEECH_SEG_MAX:
            pieces.append(sent)
            continue
        for clause in _sp_re.split(r"(?<=[,;:—–-])\s+", sent):
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
    import urllib.request
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
    proc = _sp_subprocess.Popen(
        [_GLADOS_FFMPEG, "-hide_banner", "-loglevel", "error",
         "-probesize", "32", "-analyzeduration", "0", "-fflags", "+nobuffer",
         "-f", "s16le", "-ar", "24000", "-ac", "1", "-i", "pipe:0",
         "-af", _GLADOS_FX, "-f", "wav", "-flush_packets", "1", "pipe:1"],
        stdin=_sp_subprocess.PIPE, stdout=_sp_subprocess.PIPE, stderr=_sp_subprocess.DEVNULL)

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

    t = _sp_threading.Thread(target=_pump_in, daemon=True)
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


def build_dream_list(config: Config) -> dict:
    """Read last 10 memory snapshots (dream records)."""
    snap_dir = config.workspace / "snapshots"
    if not snap_dir.exists():
        return {"dreams": []}
    # Prefer real dream records (the briefing dream cycle's distillation: flavor + learned + plan).
    # Fall back to legacy memory_snapshot_* files. The <80-char filter below drops empty stubs.
    snapshots = sorted(
        list(snap_dir.glob("dream_*.md")) + list(snap_dir.glob("memory_snapshot_*")),
        key=lambda p: p.stat().st_mtime,
        reverse=True,   # newest first -> renders newest-at-top
    )
    dreams = []
    for snap in snapshots:
        try:
            content = snap.read_text()
        except OSError:
            continue
        if len(content.strip()) < 80:
            continue  # skip empty startup/test stubs that clutter the journal
        dreams.append({
            "ts": snap.stem.replace("memory_snapshot_", "").replace("dream_", ""),
            "chars": len(content),
            "preview": content[:300],
        })
        if len(dreams) >= 10:
            break
    return {"dreams": dreams}


def _disk_total_gb() -> float:
    """Total size of the drive the dashboard runs from (for the disk gauge scale)."""
    try:
        import shutil
        return round(shutil.disk_usage(__file__).total / (1024 ** 3), 1)
    except OSError:
        return 0.0


def build_status(config: Config) -> dict:
    """Assemble full status from workspace files."""
    heartbeat = _read_json(config.workspace / "heartbeat.json")
    persona = _read_json(config.workspace / "persona.json")
    wal = _read_json(config.workspace / "wal.json")
    activity = _read_json(config.workspace / "activity.json")
    goal = _read_text(config.workspace / "goal.md")
    plan = _read_text(config.workspace / "plan.md")[:2000]
    observations = _tail_jsonl(config.workspace / "observations.jsonl", 20)
    paused = (config.workspace / "paused").exists()
    flavor = _read_json(config.workspace / "flavor.json")
    narration = _compute_narration(heartbeat, persona, goal, flavor)

    level = persona.get("level", 1)
    mood = persona.get("mood", "curious")
    traits = persona.get("traits", [])
    xp = persona.get("xp", 0)
    titles = persona.get("titles", [])

    # Determine special state
    special = None
    cf = heartbeat.get("consecutive_failures", 0)
    if cf >= 5:
        special = "dead"
    elif not goal.strip():
        special = "sleeping"

    creature = get_creature(level, mood, traits, special=special)

    return {
        "heartbeat": heartbeat,
        "persona": {
            "name": persona.get("name", "eiDOS"),
            "level": level,
            "xp": xp,
            "xp_next": ((level) ** 2) * 50,  # XP needed for next level
            "mood": mood,
            "traits": traits,
            "titles": titles,
            "goals_completed": persona.get("goals_completed", 0),
            "total_ticks": persona.get("total_ticks", 0),
            "longest_streak": persona.get("longest_streak", 0),
        },
        "creature": creature,
        "goal": goal[:500],
        "plan": plan,
        "observations": observations,
        "narration": narration,
        "flavor": flavor,
        "paused": paused,
        "disk_total_gb": _disk_total_gb(),
        "activity": activity,
        "wal": {
            "tick": wal.get("tick_number", 0),
            "consecutive_failures": wal.get("consecutive_failures", 0),
        },
        "ts": time.time(),
    }


def build_ping(config: Config) -> dict:
    """Tiny health-check response (<500 bytes)."""
    hb = _read_json(config.workspace / "heartbeat.json")
    return {
        "ts": hb.get("ts", 0),
        "tick": hb.get("tick", 0),
        "level": hb.get("level", 1),
        "mood": hb.get("mood", "unknown"),
        "ok": hb.get("consecutive_failures", 0) < 5,
        "failures": hb.get("consecutive_failures", 0),
        "disk_free_gb": hb.get("disk_free_gb"),
        "ram_pct": hb.get("ram_pct"),
        "uptime_s": hb.get("uptime_s", 0),
    }


def build_chat(config: Config) -> dict:
    """Build chat history from interventions, replies, and pending questions."""
    messages = []

    # Operator → LLM: intervention files (pending + consumed)
    idir = config.interventions_dir
    if idir.exists():
        for path in sorted(idir.iterdir()):
            if path.name.startswith("."):
                continue
            try:
                content = path.read_text().strip()
                if not content:
                    continue
                done = path.suffix == ".done"
                mtime = path.stat().st_mtime
                messages.append({
                    "direction": "outgoing",
                    "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(mtime)),
                    "text": content[:2000],
                    "status": "delivered" if done else "pending",
                })
            except OSError:
                continue

    # LLM → Operator: chat replies
    replies = _tail_jsonl(config.workspace / "chat_replies.jsonl", 50)
    for r in replies:
        messages.append({
            "direction": "incoming",
            "ts": r.get("ts", ""),
            "text": r.get("text", ""),
            "status": "delivered",
        })

    messages.sort(key=lambda m: m.get("ts", ""))
    return {"messages": messages}


def _tool_preview(name: str, args) -> str:
    """Build a human-readable preview of a tool call."""
    if not isinstance(args, dict):
        return name
    if name == "bash":
        return "$ " + (args.get("cmd", "") or "")[:100]
    if name == "write_file":
        return "writing " + (args.get("path", "") or "")
    if name == "read_file":
        return "reading " + (args.get("path", "") or "")
    if name == "memorize":
        return (args.get("fact", "") or "")[:100] or "memorizing"
    if name == "remember":
        return (args.get("note", "") or "")[:100] or "noting something"
    if name == "recall":
        return "recalling: " + (args.get("query", "") or "")[:80]
    if name == "http_request":
        return "fetching " + (args.get("url", "") or "")[:80]
    if name == "bg_run":
        return "starting: " + (args.get("cmd", "") or "")[:80]
    if name == "bg_check":
        return "checking on " + (args.get("name", "") or "")
    if name == "update_plan":
        return (args.get("note", "") or "")[:100] or "updating plan"
    return name


def build_thoughts(config: Config, limit: int = 30) -> dict:
    """The agent's train of thought (thoughts.jsonl) for the Buddy Thoughts panel.

    Falls back to parsing llm_log.jsonl when no thought stream exists yet.
    """
    thought_entries = _tail_jsonl(config.workspace / "thoughts.jsonl", limit)
    if thought_entries:
        out = []
        for e in reversed(thought_entries):  # newest first
            text = (e.get("text") or "").strip()
            if not text:
                continue
            out.append({
                "tick": e.get("tick", 0),
                "ts": e.get("ts", ""),
                "elapsed_s": 0,
                "preview": text,
                "raw_tail": text[-60:].replace("\n", " ").strip(),
                "segments": [{"type": "thinking", "text": text}],
            })
        return {"thoughts": out}

    import re

    entries = _tail_jsonl(config.workspace / "llm_log.jsonl", limit)
    thoughts = []
    for entry in reversed(entries):  # newest first
        raw = entry.get("response_preview", "")
        if not raw:
            continue

        tick = entry.get("tick", 0)
        ts = entry.get("ts", "")
        elapsed = entry.get("elapsed_s", 0)

        # Split response into segments: thinking text vs tool calls
        segments = []
        pos = 0
        for m in re.finditer(
            r'<tool>(\w+)</tool>\s*\n?<args>(.*?)</args>',
            raw, re.DOTALL
        ):
            # Thinking text before this tool call
            thinking = raw[pos:m.start()].strip()
            if thinking:
                segments.append({"type": "thinking", "text": thinking})
            # The tool call itself
            tool_name = m.group(1)
            try:
                tool_args = json.loads(m.group(2))
            except (json.JSONDecodeError, ValueError):
                tool_args = m.group(2)
            segments.append({"type": "tool", "name": tool_name, "args": tool_args})
            pos = m.end()

        # Trailing thinking text after last tool call
        trailing = raw[pos:].strip()
        if trailing:
            segments.append({"type": "thinking", "text": trailing})

        # If no tool tags found, treat entire response as thinking
        if not segments and raw.strip():
            segments.append({"type": "thinking", "text": raw.strip()})

        # Build a short preview — prefer thinking text, else describe the tool action
        preview = ""
        for seg in segments:
            if seg["type"] == "thinking":
                preview = seg["text"][:120]
                break
        if not preview:
            for seg in segments:
                if seg["type"] == "tool":
                    preview = _tool_preview(seg["name"], seg.get("args", {}))
                    break

        # Raw tail for thought bubble display
        raw_tail = raw[-60:].replace('\n', ' ').strip() if raw else ''

        thoughts.append({
            "tick": tick,
            "ts": ts,
            "elapsed_s": elapsed,
            "preview": preview,
            "raw_tail": raw_tail,
            "segments": segments,
        })

    return {"thoughts": thoughts}


def build_metrics(config: Config, limit: int = 60) -> dict:
    """Return last N metrics points for charting."""
    entries = _tail_jsonl(config.workspace / "metrics.jsonl", limit)
    pts = []
    for e in entries:
        pts.append({
            "ts": e.get("ts", 0),
            "tick": e.get("tick", 0),
            "cpu_pct": e.get("cpu_pct", 0),
            "ram_pct": e.get("ram_pct", 0),
            "llm_elapsed_s": e.get("llm_elapsed_s", 0),
        })
    return {"metrics": pts}


# --- HTML Template ---

_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>eiDOS — {{NAME}}</title>
<style>
* { margin: 0; padding: 0; box-sizing: border-box; }
body {
    background: #0a0a0a;
    color: #00ff41;
    font-family: 'Courier New', 'Menlo', monospace;
    font-size: 14px;
    line-height: 1.4;
    overflow-x: hidden;
}
/* CRT scanline effect */
body::after {
    content: '';
    position: fixed;
    top: 0; left: 0; right: 0; bottom: 0;
    background: repeating-linear-gradient(
        0deg,
        transparent,
        transparent 2px,
        rgba(0,0,0,0.08) 2px,
        rgba(0,0,0,0.08) 4px
    );
    pointer-events: none;
    z-index: 9999;
}
.container {
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: 16px;
    padding: 16px;
    max-width: 1200px;
    margin: 0 auto;
}
@media (max-width: 700px) {
    .container { grid-template-columns: 1fr; }
}
.panel {
    border: 1px solid #1a3a1a;
    padding: 12px;
    background: #0d0d0d;
    border-radius: 4px;
    overflow: hidden;
    min-width: 0;
}
.panel-title {
    color: #ffb000;
    font-size: 12px;
    text-transform: uppercase;
    letter-spacing: 2px;
    margin-bottom: 8px;
    border-bottom: 1px solid #1a3a1a;
    padding-bottom: 4px;
}
.header {
    grid-column: 1 / -1;
    text-align: center;
    padding: 8px;
    border-bottom: 2px solid #1a3a1a;
}
.header h1 {
    color: #ffb000;
    font-size: 18px;
    font-weight: normal;
    letter-spacing: 4px;
}
.header .subtitle {
    color: #555;
    font-size: 11px;
    margin-top: 4px;
}
/* Creature display */
#creature-box {
    text-align: center;
    min-height: 180px;
    display: flex;
    flex-direction: column;
    align-items: center;
    justify-content: center;
}
#creature-art {
    font-size: 16px;
    line-height: 1.2;
    color: #00ff41;
    text-shadow: 0 0 8px rgba(0,255,65,0.3);
    white-space: pre;
    text-align: left;
    transition: opacity 0.3s;
}
.creature-info {
    margin-top: 8px;
    font-size: 13px;
}
.xp-bar {
    display: inline-block;
    width: 200px;
    height: 10px;
    border: 1px solid #1a3a1a;
    margin: 4px 0;
    position: relative;
}
.xp-fill {
    height: 100%;
    background: #00ff41;
    transition: width 0.5s;
}
.trait-badge {
    display: inline-block;
    border: 1px solid #ffb000;
    color: #ffb000;
    padding: 1px 6px;
    font-size: 10px;
    margin: 2px;
    border-radius: 2px;
}
.title-badge {
    display: inline-block;
    color: #ffd700;
    font-size: 10px;
    margin: 2px 4px;
}
/* Gauges */
.gauge {
    margin: 6px 0;
    font-size: 12px;
}
.gauge-bar {
    display: inline-block;
    width: 120px;
    font-size: 12px;
}
.gauge-label {
    display: inline-block;
    width: 80px;
    color: #aaa;
}
.gauge-val {
    color: #00ff41;
    margin-left: 4px;
}
.gauge-warn { color: #ffb000; }
.gauge-crit { color: #ff4444; }
/* Activity feed */
.feed {
    max-height: 350px;
    overflow-y: auto;
    font-size: 11px;
}
.feed-entry {
    padding: 2px 0;
    border-bottom: 1px solid #111;
}
.feed-ok { color: #00ff41; }
.feed-fail { color: #ff4444; }
.feed-system { color: #ffb000; }
.feed-compact { color: #aa88ff; }
.feed-tick { color: #555; font-size: 10px; }
/* Memory panel */
.memory-view {
    max-height: 200px;
    overflow-y: auto;
    font-size: 11px;
    color: #888;
    white-space: pre-wrap;
    word-break: break-word;
}
/* Footer */
.footer {
    grid-column: 1 / -1;
    text-align: center;
    color: #333;
    font-size: 10px;
    padding: 4px;
}
/* Particle container */
#particles {
    position: fixed;
    top: 0; left: 0; right: 0; bottom: 0;
    pointer-events: none;
    z-index: 1;
    overflow: hidden;
}
.particle {
    position: absolute;
    color: rgba(0,255,65,0.4);
    font-size: 12px;
    animation: float-up 4s linear forwards;
    pointer-events: none;
}
@keyframes float-up {
    0% { opacity: 0.6; transform: translateY(0) translateX(0); }
    100% { opacity: 0; transform: translateY(-80px) translateX(20px); }
}
/* Chat */
.chat-messages {
    max-height: 300px;
    overflow-y: auto;
    display: flex;
    flex-direction: column;
    gap: 8px;
    padding: 8px 0;
}
.chat-msg {
    max-width: 75%;
    padding: 6px 10px;
    border-radius: 4px;
    font-size: 12px;
    line-height: 1.4;
    word-wrap: break-word;
    white-space: pre-wrap;
}
.chat-msg.outgoing {
    align-self: flex-end;
    background: #0a2a0a;
    border: 1px solid #1a5a1a;
    color: #00ff41;
}
.chat-msg.incoming {
    align-self: flex-start;
    background: #2a1a00;
    border: 1px solid #5a3a00;
    color: #ffb000;
}
.chat-meta {
    font-size: 9px;
    color: #555;
    margin-top: 3px;
}
.chat-status-delivered { color: #00ff41; }
.chat-status-pending { color: #ffb000; }
.chat-input-row {
    display: flex;
    gap: 8px;
    margin-top: 8px;
}
.chat-input-row textarea {
    flex: 1;
    background: #111;
    border: 1px solid #1a3a1a;
    color: #00ff41;
    font-family: inherit;
    font-size: 12px;
    padding: 6px 8px;
    resize: vertical;
    min-height: 34px;
    max-height: 120px;
    border-radius: 4px;
}
.chat-input-row textarea:focus {
    outline: none;
    border-color: #00ff41;
}
.chat-input-row button {
    background: #1a3a1a;
    color: #00ff41;
    border: 1px solid #1a5a1a;
    padding: 6px 16px;
    font-family: inherit;
    font-size: 12px;
    cursor: pointer;
    border-radius: 4px;
    white-space: nowrap;
}
.chat-input-row button:hover { background: #2a5a2a; }
.chat-input-row button:disabled { opacity: 0.4; cursor: not-allowed; }
.chat-empty {
    color: #333;
    font-size: 11px;
    text-align: center;
    padding: 20px;
}
/* Buddy Thoughts / Narration */
.narration-box {
    font-size: 12px;
    padding: 8px 0;
    min-height: 40px;
}
.narration-flavor {
    color: #aaddaa;
    font-style: italic;
}
.narration-computed {
    color: #668866;
    font-style: italic;
}
/* Buddy Thoughts expanded list */
.thoughts-list {
    max-height: 460px;
    overflow-y: auto;
    font-size: 12.5px;
}
.thought-entry {
    border-bottom: 1px solid #1a3a1a;
    cursor: pointer;
    transition: background 0.15s;
}
.thought-entry:hover {
    background: #111;
}
.thought-header {
    display: flex;
    align-items: baseline;
    gap: 8px;
    padding: 6px 4px;
}
.thought-tick {
    color: #555;
    font-size: 10px;
    flex-shrink: 0;
    min-width: 40px;
}
.thought-time {
    color: #444;
    font-size: 10px;
    flex-shrink: 0;
}
.thought-preview {
    color: #8fcf8f;
    flex: 1;
    white-space: normal;
    overflow: visible;
    word-break: break-word;
    line-height: 1.55;
}
.thought-elapsed {
    color: #444;
    font-size: 10px;
    flex-shrink: 0;
}
.thought-body {
    display: none;
    padding: 4px 8px 10px 52px;
    line-height: 1.5;
}
.thought-entry.expanded .thought-body {
    display: block;
}
.thought-entry.expanded {
    background: #0f1a0f;
}
.thought-seg-thinking {
    color: #aaddaa;
    white-space: pre-wrap;
    word-break: break-word;
    margin: 4px 0;
}
.thought-seg-tool {
    color: #ffb000;
    font-style: italic;
    margin: 4px 0;
    padding: 2px 6px;
    border-left: 2px solid #332200;
    background: rgba(255,176,0,0.05);
}
.thought-seg-tool .tool-args {
    color: #666;
    font-style: normal;
    font-size: 10px;
    display: block;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
    max-width: 600px;
}
/* Goal Progress */
.plan-progress {
    max-height: 300px;
    overflow-y: auto;
    font-size: 11px;
}
.plan-item {
    padding: 4px 0;
    cursor: pointer;
    border-bottom: 1px solid #111;
}
.plan-item:hover {
    background: #0a1a0a;
}
.plan-check {
    color: #00ff41;
    margin-right: 6px;
}
.plan-uncheck {
    color: #333;
    margin-right: 6px;
}
.plan-done-text {
    color: #555;
    text-decoration: line-through;
}
.plan-detail {
    display: none;
    padding: 4px 0 4px 24px;
    color: #555;
    font-size: 10px;
    white-space: pre-wrap;
}
.plan-item.expanded .plan-detail {
    display: block;
}
.plan-header {
    color: #aaa;
    font-size: 10px;
    padding: 2px 0;
}
/* Knowledge Nuggets */
.knowledge-list {
    max-height: 360px;
    overflow-y: auto;
    font-size: 11px;
}
.knowledge-entry {
    padding: 6px 0;
    border-bottom: 1px solid #111;
}
.knowledge-text {
    white-space: pre-wrap;
    word-break: break-word;
    line-height: 1.45;
    color: #b8d8b8;
}
.knowledge-category {
    font-size: 9px;
    padding: 1px 4px;
    border-radius: 2px;
    display: inline-block;
    margin-right: 4px;
}
.knowledge-category-facts { background: #1a2a1a; color: #00ff41; }
.knowledge-category-errors { background: #2a1a1a; color: #ff4444; }
.knowledge-category-procedures { background: #1a1a2a; color: #8888ff; }
.knowledge-category-reflections { background: #2a2a1a; color: #ffb000; }
.knowledge-tags {
    color: #555;
    font-size: 9px;
}
/* Dream Journal */
.dream-list {
    max-height: 250px;
    overflow-y: auto;
    font-size: 11px;
}
.dream-entry {
    padding: 4px 0;
    border-bottom: 1px solid #111;
    cursor: pointer;
}
.dream-entry:hover {
    background: #0a0a1a;
}
.dream-ts {
    color: #8888ff;
    font-size: 9px;
}
.dream-preview {
    display: none;
    padding: 4px 0 4px 12px;
    color: #555;
    font-size: 10px;
    white-space: pre-wrap;
    word-break: break-word;
}
.dream-entry.expanded .dream-preview {
    display: block;
}
/* Pause button */
.pause-toggle {
    background: #1a3a1a;
    color: #00ff41;
    border: 1px solid #1a5a1a;
    padding: 2px 8px;
    font-family: inherit;
    font-size: 11px;
    cursor: pointer;
    border-radius: 3px;
}
.pause-toggle:hover { background: #2a5a2a; }
.pause-toggle.paused {
    background: #3a1a1a;
    border-color: #5a1a1a;
    color: #ff4444;
}
/* Improved feed */
.feed-detail {
    color: #888;
    font-size: 10px;
    padding-left: 2px;
    margin-top: 1px;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
}
/* Thought Bubble */
.thought-bubble-wrap {
    position: relative;
    min-height: 50px;
    margin-bottom: 4px;
}
#thought-bubble {
    position: relative;
    background: #0d120d;
    border: 1px solid #1a3a1a;
    border-radius: 12px;
    padding: 8px 12px;
    width: 320px;
    height: 96px;                 /* FIXED so the creature never shifts */
    margin: 0 auto 10px auto;
    overflow: hidden;
    display: flex;
    flex-direction: column;
    text-align: left;
    transition: border-color 0.4s;
}
#thought-bubble #thought-status {
    display: flex; align-items: center; gap: 6px; flex-shrink: 0;
    font-size: 10px; text-transform: uppercase; letter-spacing: 0.6px;
    color: #7da37d; margin-bottom: 5px;
}
#thought-bubble #thought-glyph { font-size: 13px; line-height: 1; display: inline-block; }
#thought-bubble #thought-state { flex: 1; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
#thought-bubble .thought-elapsed { color: #4f6b4f; font-size: 10px; flex-shrink: 0; }
#thought-bubble #thought-text {
    flex: 1; overflow: hidden; word-break: break-word;
    font-size: 12px; line-height: 1.45; color: #d4ecd4;
    -webkit-mask-image: linear-gradient(180deg,#000 72%,transparent 100%);
            mask-image: linear-gradient(180deg,#000 72%,transparent 100%);
}
@keyframes tb-spin { to { transform: rotate(360deg); } }
@keyframes tb-pulse { 0%,100%{opacity:1} 50%{opacity:.3} }
.tb-spin { animation: tb-spin 1.1s linear infinite; }
.tb-pulse { animation: tb-pulse 1.3s ease-in-out infinite; }

/* Blue tool-call bubble — below the creature, updates independently of thoughts */
#tool-bubble {
    width: 320px; margin: 4px auto 8px auto;
    background: #0a1018; border: 1px solid #1d3a5a; border-radius: 10px;
    padding: 7px 11px; text-align: left;
    box-shadow: 0 0 8px rgba(40,120,220,0.10);
    transition: border-color 0.3s, box-shadow 0.3s;
}
#tool-bubble.tb-active { border-color: #2e7fd0; box-shadow: 0 0 14px rgba(40,120,220,0.30); }
#tool-bubble-head {
    display: flex; align-items: center; gap: 6px;
    font-size: 10px; text-transform: uppercase; letter-spacing: 0.6px; color: #5fa8e0;
}
#tool-glyph { font-size: 13px; line-height: 1; display: inline-block; color: #6fc0ff; }
#tool-now { flex: 1; color: #9fd4ff; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
#tool-elapsed { color: #3f6f9f; font-size: 10px; flex-shrink: 0; }
#tool-last {
    margin-top: 4px; color: #6f9fc4; font-size: 10px;
    white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
}
#thought-bubble.state-idle {
    border-color: #2a4a2a;
    color: #556655;
}
#thought-bubble::after {
    content: '';
    position: absolute;
    bottom: -8px;
    left: 50%;
    transform: translateX(-50%);
    width: 0; height: 0;
    border-left: 6px solid transparent;
    border-right: 6px solid transparent;
    border-top: 8px solid #1a3a1a;
}
#thought-bubble.state-thinking {
    border-color: #00ff41;
    color: #00ff41;
    text-shadow: 0 0 6px rgba(0,255,65,0.3);
}
#thought-bubble.state-dreaming {
    border-color: #aa88ff;
    color: #aa88ff;
    text-shadow: 0 0 6px rgba(170,136,255,0.3);
}
#thought-bubble.state-executing {
    border-color: #ffb000;
    color: #ffb000;
}
#thought-bubble.state-sleeping {
    border-color: #1a3a1a;
    color: #333;
}
#thought-bubble.state-listening {
    border-color: #33bbff;
    color: #33bbff;
    text-shadow: 0 0 7px rgba(51,187,255,0.35);
}
#thought-bubble.state-error {
    border-color: #ff4444;
    color: #ff4444;
}
@keyframes pulse-dots {
    0%, 80%, 100% { opacity: 0.2; }
    40% { opacity: 1; }
}
.thinking-dots span {
    animation: pulse-dots 1.4s infinite;
    display: inline-block;
    font-size: 16px;
}
.thinking-dots span:nth-child(2) { animation-delay: 0.2s; }
.thinking-dots span:nth-child(3) { animation-delay: 0.4s; }
@keyframes blink-cursor {
    0%, 100% { opacity: 1; }
    50% { opacity: 0; }
}
.blink {
    display: inline;
    animation: blink-cursor 1.2s step-end infinite;
    color: inherit;
}
.tool-verb {
    color: #557755;
    font-style: italic;
}
.thought-elapsed {
    font-size: 9px;
    color: #555;
    margin-top: 2px;
}
</style>
</head>
<body>
<div id="particles"></div>
<style>
#nexus-control{position:fixed;top:0;left:0;right:0;z-index:99999;background:#0b0e14;
  border-bottom:1px solid #1d2530;padding:7px 14px;display:flex;align-items:center;gap:10px;
  font-family:Consolas,'SFMono-Regular',monospace;font-size:13px;color:#cdd6e4;}
#nexus-control b{color:#7da3c9;letter-spacing:1px}
#nexus-control button{cursor:pointer;border:1px solid #2a3645;background:#161c26;color:#cdd6e4;
  padding:4px 12px;border-radius:6px;font:inherit}
#nexus-control button:disabled{opacity:.35;cursor:not-allowed}
#nx-go{background:#13361f;border-color:#1c7a3a;color:#9cf0b6}
#nx-stop{background:#3a1414;border-color:#7a1c1c;color:#ff9b9b}
#nx-state{padding:2px 12px;border-radius:11px;background:#1a1f29;color:#8893a5;font-weight:bold}
#nx-msg{color:#5d6b7e;margin-left:auto;font-size:12px;max-width:40%;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
</style>
<div id="nexus-control">
  <b>eiDOS</b>
  <span id="nx-state">checking…</span>
  <button id="nx-start" onclick="nxCtl('start')">&#9654; Start (paused)</button>
  <button id="nx-go" onclick="nxCtl('resume')">GO &#9654;</button>
  <button id="nx-pause" onclick="nxCtl('pause')">&#9208; Pause</button>
  <button id="nx-stop" onclick="nxStop()">&#9632; STOP</button>
  <button id="nx-voice" onclick="toggleVoice()" title="Play eiDOS's GLaDOS voice through this browser/device — click to enable">&#128263; Voice: off</button>
  <audio id="nx-audio" preload="auto" style="display:none"></audio>
  <span id="nx-msg"></span>
</div>
<div style="height:38px"></div>
<script>
async function nxCtl(action){
  document.getElementById('nx-msg').textContent=action+'…';
  try{const r=await fetch('/api/control/'+action,{method:'POST'});const d=await r.json();
    document.getElementById('nx-msg').textContent=d.message||JSON.stringify(d);}
  catch(e){document.getElementById('nx-msg').textContent='err: '+e;}
  nxStatus();
}
async function nxStop(){
  if(!confirm('Authoritative STOP: force-kill the consciousness loop and its children?'))return;
  await nxCtl('stop');
}
async function nxStatus(){
  try{const r=await fetch('/api/control/status');const s=await r.json();
    const el=document.getElementById('nx-state');
    let label='STOPPED',c='#8893a5',bg='#1a1f29';
    if(s.running&&s.paused){label='PAUSED';c='#ffcf6b';bg='#3a2f12';}
    else if(s.running){label='RUNNING';c='#7CFC9B';bg='#123a1e';}
    el.textContent=label+(s.pid?' · pid '+s.pid:'');el.style.color=c;el.style.background=bg;
    document.getElementById('nx-start').disabled=s.running;
    document.getElementById('nx-go').disabled=!(s.running&&s.paused);
    document.getElementById('nx-pause').disabled=!(s.running&&!s.paused);
    document.getElementById('nx-stop').disabled=!s.running;
  }catch(e){}
}
setInterval(nxStatus,2000);nxStatus();
// --- Voice: eiDOS pushes each utterance id over SSE; we STREAM the GLaDOS audio through THIS browser.
//     Streaming = low latency; the <audio> element is "blessed" under your click so Firefox/Safari
//     allow the later callback-triggered playback (the reason you heard nothing before). ---
const NX_SILENT='data:audio/wav;base64,UklGRiwAAABXQVZFZm10IBAAAAABAAEAQB8AAEAfAAABAAgAZGF0YQgAAACAgICAgICAgA==';
let nxVoiceOn=false, nxES=null, nxAC=null;
function nxLabel(extra){ const b=document.getElementById('nx-voice'); const base=nxVoiceOn?'&#128266; Voice: on':'&#128263; Voice: off'; b.innerHTML=extra?extra+' '+base:base; if(extra) setTimeout(()=>{b.innerHTML=base;},1400); }
function nxBlip(){ try{ nxAC=nxAC||new (window.AudioContext||window.webkitAudioContext)(); if(nxAC.state==='suspended')nxAC.resume(); const o=nxAC.createOscillator(),g=nxAC.createGain(); o.frequency.value=660; g.gain.value=0.05; o.connect(g); g.connect(nxAC.destination); o.start(); o.stop(nxAC.currentTime+0.12);}catch(_){} }
function nxPlay(id){ const a=document.getElementById('nx-audio'); a.muted=false; a.src='/api/speech/stream?id='+id+'&t='+Date.now(); a.play().then(()=>nxLabel('&#9654;')).catch(()=>nxLabel('&#9888; blocked')); }
function nxConnectSSE(){ if(nxES)return; nxES=new EventSource('/api/speech/events'); nxES.onmessage=(e)=>{ if(!nxVoiceOn)return; try{const d=JSON.parse(e.data); if(d.id) nxPlay(d.id);}catch(_){} }; }
function toggleVoice(){
  nxVoiceOn=!nxVoiceOn;
  if(nxVoiceOn){
    const a=document.getElementById('nx-audio');
    a.muted=true; a.src=NX_SILENT; a.play().catch(()=>{});   // bless under THIS click -> later play() allowed
    setTimeout(()=>{a.muted=false;},250);
    nxBlip();            // audible "armed" confirmation so you know it's working
    nxConnectSSE();
  }
  nxLabel('');
}
</script>
<div class="container">
    <div class="header">
        <h1>⟨ eiDOS ⟩</h1>
        <div class="subtitle">autonomous agent — field station monitor</div>
    </div>

    <!-- Left: Creature -->
    <div class="panel">
        <div class="panel-title">Buddy</div>
        <div id="creature-box">
            <div class="thought-bubble-wrap">
                <div id="thought-bubble" class="state-sleeping">
                    <div id="thought-status">
                        <span id="thought-glyph">z</span>
                        <span id="thought-state">resting</span>
                        <span class="thought-elapsed" id="thought-elapsed"></span>
                    </div>
                    <div id="thought-text">…</div>
                </div>
            </div>
            <pre id="creature-art"></pre>
            <div id="tool-bubble" class="tb-idle">
                <div id="tool-bubble-head">
                    <span id="tool-glyph">⌁</span>
                    <span id="tool-now">idle</span>
                    <span id="tool-elapsed"></span>
                </div>
                <div id="tool-last"></div>
            </div>
            <div class="creature-info">
                <span id="name-level"></span> · <span id="mood-display"></span><br>
                <div class="xp-bar"><div class="xp-fill" id="xp-fill"></div></div>
                <span id="xp-text" style="font-size:10px;color:#555;"></span><br>
                <span id="traits"></span><br>
                <span id="titles"></span>
            </div>
        </div>
    </div>

    <!-- Right: Health -->
    <div class="panel">
        <div class="panel-title">Health</div>
        <div id="gauges">
            <div class="gauge"><span class="gauge-label">RAM</span><span class="gauge-bar" id="g-ram"></span><span class="gauge-val" id="v-ram"></span></div>
            <div class="gauge"><span class="gauge-label">Disk Free</span><span class="gauge-bar" id="g-disk"></span><span class="gauge-val" id="v-disk"></span></div>
            <div class="gauge"><span class="gauge-label">LLM Latency</span><span class="gauge-bar" id="g-llm"></span><span class="gauge-val" id="v-llm"></span></div>
        </div>
        <div style="margin:8px 0;">
            <canvas id="cpu-chart" width="300" height="80" style="width:100%;height:80px;border:1px solid #1a3a1a;border-radius:4px;background:#0a0a0a;"></canvas>
            <div style="font-size:9px;color:#555;text-align:center;margin-top:2px;">GPU util %</div>
            <div id="gpu-readout" style="font-size:11px;color:#7CFC9B;margin-top:6px;display:flex;flex-wrap:wrap;gap:10px;justify-content:center;"></div>
            <div id="llm-readout" style="font-size:11px;color:#6ea8fe;margin-top:4px;display:flex;flex-wrap:wrap;gap:10px;justify-content:center;"></div>
        </div>
        <hr style="border-color:#1a3a1a;margin:8px 0;">
        <div style="font-size:12px;">
            <div><span style="color:#aaa;">Goal:</span> <span id="current-goal" style="color:#00ff41;word-break:break-word;"></span></div>
            <div><span style="color:#aaa;">Tick:</span> <span id="current-tick"></span> · <span style="color:#aaa;">Uptime:</span> <span id="uptime"></span></div>
            <div><span style="color:#aaa;">Failures:</span> <span id="failures"></span> · <span style="color:#aaa;">Max Tokens:</span> <span id="max-tokens"></span></div>
        </div>
    </div>

    <!-- Buddy Thoughts + Goal Progress: side by side -->
    <div class="panel">
        <div class="panel-title">Buddy Thoughts <span id="thoughts-status" style="float:right;font-size:10px;color:#555;"></span></div>
        <div id="narration" class="narration-box"></div>
        <div id="thoughts-list" class="thoughts-list"></div>
    </div>

    <div class="panel">
        <div class="panel-title">Goal Progress <span id="plan-meter" style="float:right;font-size:10px;color:#555;"></span></div>
        <div id="plan-progress" class="plan-progress"></div>
    </div>

    <!-- Activity Feed + Dream Journal: side by side -->
    <div class="panel">
        <div class="panel-title">Activity Feed</div>
        <div class="feed" id="feed"></div>
    </div>

    <div class="panel">
        <div class="panel-title">Dream Journal</div>
        <div class="dream-list" id="dream-list"></div>
    </div>

    <!-- Operator Chat: full width -->
    <div class="panel" style="grid-column: 1 / -1;">
        <div class="panel-title">Operator Chat
            <span style="float:right;">
                <button id="pause-btn" onclick="togglePause()" class="pause-toggle" title="Pause/resume tick loop">&#9208;</button>
                <span id="pause-status" style="font-size:9px;color:#555;">&#9654; running</span>
            </span>
        </div>
        <div class="chat-messages" id="chat-messages"></div>
        <div class="chat-input-row">
            <textarea id="chat-input" placeholder="Send a message to eiDOS..." rows="1"></textarea>
            <button id="chat-send" onclick="sendChat()">Send ▸</button>
        </div>
    </div>

    <!-- Self-Guide: Dean's standing directives, injected into eiDOS every tick -->
    <div class="panel" style="grid-column: 1 / -1;">
        <div class="panel-title">Self-Guide — standing directives, injected every tick
            <span style="float:right;font-size:9px;color:#555;" id="self-guide-status"></span>
        </div>
        <div id="self-guide-proposal" style="display:none;background:#0d1a26;border:1px solid #33bbff;border-radius:6px;padding:8px;margin-bottom:8px;">
            <div style="color:#33bbff;font-size:11px;margin-bottom:4px;">⟳ eiDOS proposed a change to its self-guide:</div>
            <pre id="self-guide-proposed" style="white-space:pre-wrap;color:#9fd4ff;font-size:11px;max-height:160px;overflow:auto;margin:0 0 6px 0;"></pre>
            <button onclick="acceptSelfGuideProposal()" style="background:#13384f;color:#9fd4ff;border:1px solid #33bbff;border-radius:5px;padding:4px 10px;cursor:pointer;font-size:11px;">Load into editor ▸</button>
            <button onclick="rejectSelfGuideProposal()" style="background:#3a1212;color:#ff9b9b;border:1px solid #5a2a2a;border-radius:5px;padding:4px 10px;cursor:pointer;font-size:11px;">Reject</button>
        </div>
        <textarea id="self-guide-text" placeholder="Standing directives for eiDOS — e.g. 'Always announce when a device goes offline.' These are injected into its context every tick." style="width:100%;height:150px;box-sizing:border-box;background:#0a0f0a;color:#cfeccf;border:1px solid #1a3a1a;border-radius:6px;padding:8px;font-family:monospace;font-size:12px;"></textarea>
        <div style="margin-top:6px;">
            <button onclick="saveSelfGuide()" style="background:#123a1e;color:#7CFC9B;border:1px solid #2a5a2a;border-radius:5px;padding:5px 12px;cursor:pointer;">Save (make live) ▸</button>
            <span id="self-guide-saved" style="font-size:10px;color:#7CFC9B;margin-left:8px;"></span>
        </div>
    </div>

    <!-- Git Safety: checkpoint / restore eiDOS's source (emergency reversibility) -->
    <div class="panel" style="grid-column: 1 / -1;">
        <div class="panel-title">Git Safety — checkpoint &amp; restore
            <span style="float:right;font-size:9px;color:#555;" id="git-status"></span>
        </div>
        <div style="margin-bottom:6px;">
            <input id="git-label" placeholder="checkpoint label (optional)" style="background:#0a0f0a;color:#cfeccf;border:1px solid #1a3a1a;border-radius:5px;padding:4px 8px;font-size:11px;width:260px;">
            <button onclick="doCheckpoint()" style="background:#123a1e;color:#7CFC9B;border:1px solid #2a5a2a;border-radius:5px;padding:4px 12px;cursor:pointer;">⛳ Checkpoint now</button>
            <button onclick="doRestore('')" style="background:#3a2f12;color:#ffcf6b;border:1px solid #5a4a1a;border-radius:5px;padding:4px 12px;cursor:pointer;margin-left:6px;">↩ Restore last good</button>
            <span id="git-msg" style="font-size:10px;color:#7CFC9B;margin-left:8px;"></span>
        </div>
        <div id="git-checkpoints" style="font-size:11px;color:#9fc4a0;max-height:150px;overflow:auto;font-family:monospace;"></div>
    </div>

    <!-- Self-Edit Proposals: eiDOS-proposed source changes, you approve -->
    <div class="panel" style="grid-column: 1 / -1;">
        <div class="panel-title">Self-Edit Proposals — eiDOS-proposed code changes (you approve)
            <span style="float:right;font-size:9px;color:#555;" id="selfedit-status"></span>
        </div>
        <div id="selfedit-list" style="font-size:11px;"></div>
        <pre id="selfedit-diff" style="display:none;white-space:pre-wrap;background:#0a0f0a;border:1px solid #1a3a1a;border-radius:6px;padding:8px;max-height:320px;overflow:auto;font-size:11px;color:#cfeccf;margin-top:8px;"></pre>
    </div>

    <!-- Knowledge Nuggets + Working Memory: side by side -->
    <div class="panel">
        <div class="panel-title">Knowledge Nuggets</div>
        <div class="knowledge-list" id="knowledge-list"></div>
    </div>

    <div class="panel">
        <div class="panel-title">Working Memory</div>
        <div class="memory-view" id="memory"></div>
    </div>

    <div class="footer">
        pull-only · tailscale · <span id="last-update"></span>
    </div>
</div>

<script>
let creatureFrames = [];
let creatureIdx = 0;
let creatureInterval = 1500;
let particleChars = '·';
let animTimer = null;

function escapeHtml(s) {
    return s.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
}

function makeBar(pct, width) {
    width = width || 15;
    let filled = Math.round(pct / 100 * width);
    filled = Math.max(0, Math.min(width, filled));
    return '[' + '█'.repeat(filled) + '░'.repeat(width - filled) + ']';
}

function gaugeClass(val, warnAt, critAt) {
    if (critAt !== undefined && val >= critAt) return 'gauge-crit';
    if (warnAt !== undefined && val >= warnAt) return 'gauge-warn';
    return 'gauge-val';
}

function formatUptime(s) {
    if (!s) return '—';
    let d = Math.floor(s / 86400);
    let h = Math.floor((s % 86400) / 3600);
    let m = Math.floor((s % 3600) / 60);
    if (d > 0) return d + 'd ' + h + 'h';
    if (h > 0) return h + 'h ' + m + 'm';
    return m + 'm';
}

function feedClass(entry) {
    if (entry.tool === 'system' || entry.tool === 'dream') return 'feed-system';
    if (entry.tool === 'llm_error' || entry.tool === 'parse_error') return 'feed-fail';
    if (!entry.success) return 'feed-fail';
    // Detect special events
    let out = (entry.output || '').toLowerCase();
    if (out.includes('compaction') || out.includes('compacted') || out.includes('consolidat')) return 'feed-compact';
    return 'feed-ok';
}

function renderFeedEntry(o) {
    let cls = feedClass(o);
    let tool = o.tool || '?';
    let tick = o.tick || '';
    let dur = o.duration_s ? ' ' + o.duration_s.toFixed(1) + 's' : '';
    let ts = (o.ts || '');
    if (ts.length > 16) ts = ts.substring(11, 16);
    else if (ts.length > 5) ts = ts.substring(0, 5);

    let args = o.args || {};
    let summary;
    if (tool === 'bash' && args.cmd) {
        summary = '$ ' + escapeHtml(args.cmd.substring(0, 120));
    } else if (tool === 'write_file' && args.path) {
        let out = o.output || '';
        summary = escapeHtml(args.path) + (out ? ' -- ' + escapeHtml(out.substring(0, 80)) : '');
    } else if (tool === 'read_file' && args.path) {
        summary = escapeHtml(args.path);
    } else if ((tool === 'remember' || tool === 'update_plan') && args.note) {
        summary = escapeHtml(args.note.substring(0, 120));
    } else if (tool === 'memorize' && args.fact) {
        summary = escapeHtml(args.fact.substring(0, 120));
    } else if (tool === 'objective_done' && args.summary) {
        summary = escapeHtml(args.summary.substring(0, 120));
    } else {
        summary = escapeHtml((o.output || '').substring(0, 150));
    }

    let statusIcon = '';
    if (tool !== 'system' && tool !== 'dream') {
        statusIcon = o.success ? ' <span style="color:#00ff41">ok</span>' : ' <span style="color:#ff4444">fail</span>';
    }

    return '<div class="feed-entry ' + cls + '">' +
        '<span class="feed-tick">' + ts + ' t' + tick + '</span> ' +
        '<b>' + tool + '</b>' + statusIcon + dur +
        '<div class="feed-detail">' + summary + '</div>' +
        '</div>';
}

function updateNarration(data) {
    let el = document.getElementById('narration');
    if (!el) return;
    let flavor = data.flavor;
    let narration = data.narration || '';
    if (flavor && flavor.text) {
        el.innerHTML = '<span class="narration-flavor">"' + escapeHtml(flavor.text) + '"</span>';
    } else if (narration) {
        el.innerHTML = '<span class="narration-computed">' + escapeHtml(narration) + '</span>';
    } else {
        el.innerHTML = '<span class="narration-computed">...</span>';
    }
}

function updatePlan(data) {
    let el = document.getElementById('plan-progress');
    let meterEl = document.getElementById('plan-meter');
    if (!el) return;
    let plan = data.plan || '';
    if (!plan) {
        el.innerHTML = '<span style="color:#333;">No plan yet.</span>';
        if (meterEl) meterEl.textContent = '';
        return;
    }

    let parts = [];
    parts.push(_renderChecklist(plan, ''));

    // Combine meter counts
    let totalChecked = 0, totalItems = 0;
    parts.forEach(function(p) { totalChecked += p.checked; totalItems += p.total; });
    if (totalItems > 0) {
        let pct = Math.round((totalChecked / totalItems) * 100);
        if (meterEl) meterEl.textContent = totalChecked + '/' + totalItems + ' (' + pct + '%)';
    } else {
        if (meterEl) meterEl.textContent = '';
    }

    el.innerHTML = parts.map(function(p) { return p.html; }).join('');
}

function _renderChecklist(text, heading) {
    let lines = text.split('\n');
    let items = [];
    let currentItem = null;
    let checked = 0;
    let total = 0;

    lines.forEach(function(line) {
        let checkMatch = line.match(/^(\s*)-\s*\[([ xX])\]\s*(.*)/);
        let bulletMatch = line.match(/^(\s*)-\s+(.*)/);
        let numberedMatch = line.match(/^(\s*)\d+\.\s+(.*)/);

        if (checkMatch) {
            if (currentItem) items.push(currentItem);
            let done = checkMatch[2] !== ' ';
            total++;
            if (done) checked++;
            currentItem = { text: checkMatch[3], done: done, detail: '' };
        } else if (currentItem && line.match(/^\s{2,}/)) {
            currentItem.detail += line.trim() + '\n';
        } else if (bulletMatch || numberedMatch) {
            if (currentItem) items.push(currentItem);
            total++;
            currentItem = { text: (bulletMatch ? bulletMatch[2] : numberedMatch[2]), done: false, detail: '' };
        } else if (line.trim() && currentItem) {
            currentItem.detail += line.trim() + '\n';
        } else if (line.trim() && !currentItem) {
            items.push({ text: line.trim(), done: false, detail: '', header: true });
        }
    });
    if (currentItem) items.push(currentItem);

    let headingHtml = heading ? '<div class="plan-header" style="color:#00ff41;margin-bottom:4px;">' + escapeHtml(heading) + '</div>' : '';

    let html = headingHtml + items.map(function(item) {
        if (item.header) {
            return '<div class="plan-header">' + escapeHtml(item.text) + '</div>';
        }
        let checkIcon = item.done
            ? '<span class="plan-check">&#9745;</span>'
            : '<span class="plan-uncheck">&#9744;</span>';
        let textCls = item.done ? ' class="plan-done-text"' : '';
        let detail = item.detail
            ? '<div class="plan-detail">' + escapeHtml(item.detail.trim()) + '</div>'
            : '';
        return '<div class="plan-item" onclick="this.classList.toggle(\'expanded\')">' +
            checkIcon + '<span' + textCls + '>' + escapeHtml(item.text) + '</span>' +
            detail + '</div>';
    }).join('');

    return { html: html, checked: checked, total: total };
}

function updatePauseState(paused) {
    _pausedNow = !!paused;
    let btn = document.getElementById('pause-btn');
    let status = document.getElementById('pause-status');
    if (btn) {
        btn.innerHTML = paused ? '&#9654;' : '&#9208;';
        btn.className = 'pause-toggle' + (paused ? ' paused' : '');
        btn.title = paused ? 'Resume tick loop' : 'Pause tick loop';
    }
    if (status) {
        status.innerHTML = paused ? '&#9208; paused' : '&#9654; running';
        status.style.color = paused ? '#ff4444' : '#555';
    }
}

var _hasGoal = false;

// Last real snippet from LLM output (populated by loadThoughts)
var _lastSnippet = '';

// Blue tool bubble below the creature — purely the tool-call feed, independent of thoughts.
function updateToolBubble(activity) {
    let box = document.getElementById('tool-bubble');
    if (!box) return;
    let glyph = document.getElementById('tool-glyph');
    let now = document.getElementById('tool-now');
    let elapsed = document.getElementById('tool-elapsed');
    let last = document.getElementById('tool-last');
    let state = (activity && activity.state) || 'sleeping';
    let detail = (activity && activity.detail) || '';
    let since = (activity && activity.since) || 0;
    let lt = (activity && activity.last_tool) || null;

    box.classList.remove('tb-active');
    if (state === 'executing') {
        box.classList.add('tb-active');
        glyph.textContent = '⚙'; glyph.className = 'tb-spin'; glyph.style.color = '#6fc0ff';
        now.textContent = 'running ' + (detail || 'tool');
        if (since > 0) {
            let e = Math.max(0, Math.floor(Date.now() / 1000 - since));
            elapsed.textContent = (e >= 60 ? Math.floor(e / 60) + 'm ' : '') + (e % 60) + 's';
        } else elapsed.textContent = '';
    } else if (state === 'thinking') {
        glyph.textContent = '…'; glyph.className = 'tb-pulse'; glyph.style.color = '#6fc0ff';
        now.textContent = 'choosing next tool';
        elapsed.textContent = '';
    } else if (state === 'dreaming') {
        glyph.textContent = '✦'; glyph.className = 'tb-pulse'; glyph.style.color = '#9a7fff';
        now.textContent = 'consolidating'; elapsed.textContent = '';
    } else {
        glyph.textContent = '⌁'; glyph.className = ''; glyph.style.color = '#3f6f9f';
        now.textContent = 'idle'; elapsed.textContent = '';
    }
    // Last completed tool call (persists between ticks so the feed never goes blank)
    if (lt && lt.tool) {
        last.textContent = 'last: ' + lt.tool + (lt.ok ? ' ✓' : ' ✕')
            + (lt.summary ? ' · ' + lt.summary : '');
    } else {
        last.textContent = '';
    }
}

function updateThoughtBubble(activity) {
    let bubble = document.getElementById('thought-bubble');
    let textEl = document.getElementById('thought-text');
    let elapsedEl = document.getElementById('thought-elapsed');
    if (!bubble || !textEl) return;

    let state = (activity && activity.state) || 'sleeping';
    let detail = (activity && activity.detail) || '';
    let since = (activity && activity.since) || 0;
    let glyphEl = document.getElementById('thought-glyph');
    let stateEl = document.getElementById('thought-state');

    bubble.className = '';
    let g, label, anim, gcol;
    if (state === 'thinking') {
        bubble.classList.add('state-thinking'); g='\u273a'; label='thinking'; anim='tb-pulse'; gcol='#00ff41';
    } else if (state === 'executing') {
        bubble.classList.add('state-executing'); g='\u2699'; label='running ' + (detail || 'tool'); anim='tb-spin'; gcol='#ffb000';
    } else if (state === 'dreaming') {
        bubble.classList.add('state-dreaming'); g='\u2726'; label='dreaming'; anim='tb-pulse'; gcol='#aa88ff';
    } else if (state === 'listening') {
        bubble.classList.add('state-listening'); g='\ud83d\udc42'; label='listening to Dean'; anim='tb-pulse'; gcol='#33bbff';
    } else {
        bubble.classList.add(_hasGoal ? 'state-idle' : 'state-sleeping'); g='\u00b7'; label='idle'; anim=''; gcol='#557755';
    }
    if (glyphEl) { glyphEl.textContent = g; glyphEl.className = anim; glyphEl.style.color = gcol; }
    if (stateEl) stateEl.textContent = label;

    // Stable: last COMPLETE thought, no jittery raw-token streaming (that lives on :9100)
    textEl.textContent = _lastSnippet || (state === 'sleeping' && !_hasGoal ? 'resting' : '\u2026');

    // Elapsed in the current state — so a long tool run reads "running bash 14s", not frozen
    if (since > 0 && state !== 'sleeping') {
        let elapsed = Math.max(0, Math.floor(Date.now() / 1000 - since));
        let m = Math.floor(elapsed / 60);
        let s = elapsed % 60;
        elapsedEl.textContent = (m > 0 ? m + 'm ' : '') + s + 's';
    } else {
        elapsedEl.textContent = '';
    }
}

// Poll activity — fast during active states, gentle during idle
var _pollTimer = null;
function schedulePoll(ms) {
    if (_pollTimer) clearTimeout(_pollTimer);
    _pollTimer = setTimeout(pollActivity, ms);
}

async function pollActivity() {
    try {
        let resp = await fetch('/api/activity');
        if (resp.ok) {
            let data = await resp.json();
            _lastActivity = data;
            updateThoughtBubble(data);
            updateToolBubble(data);
            if (data.gpu && typeof data.gpu.util === 'number') {
                pushCpu(data.gpu.util);
                var g = data.gpu;
                var vg = g.mem_total ? (g.mem_used/1024).toFixed(1)+'/'+(g.mem_total/1024).toFixed(1)+' GB ('+Math.round(g.mem_used/g.mem_total*100)+'%)' : '—';
                document.getElementById('gpu-readout').innerHTML =
                    '<span>'+(g.name||'GPU')+'</span><span>VRAM '+vg+'</span><span>'+Math.round(g.temp)+'°C</span><span>'+Math.round(g.power)+' W</span>';
            }
            if (data.llm) {
                var l = data.llm;
                document.getElementById('llm-readout').innerHTML =
                    '<span>'+(l.tok_s||0)+' tok/s</span><span>last tick: '+(l.prompt_tokens||0)+'→'+(l.completion_tokens||0)+' tok</span><span>'+(l.llm_elapsed_s||0)+'s</span>';
            }
            // Active states get fast polling, sleeping gets slow
            let active = data.state === 'thinking' || data.state === 'executing' || data.state === 'dreaming';
            schedulePoll(active ? 500 : 3000);
            return;
        }
    } catch(e) {}
    schedulePoll(3000);
}

// Also tick idle musings even without a network poll
setInterval(function() {
    if (_lastActivity && _lastActivity.state === 'sleeping' && _hasGoal) {
        updateThoughtBubble(_lastActivity);
    }
}, 2000);

let _pausedNow = false;
async function togglePause() {
    try {
        let ep = _pausedNow ? '/api/control/resume' : '/api/control/pause';
        let resp = await fetch(ep, { method: 'POST' });
        if (resp.ok) { updatePauseState(!_pausedNow); }
    } catch(e) {}
}

async function loadKnowledge() {
    try {
        let resp = await fetch('/api/knowledge');
        if (resp.ok) {
            let data = await resp.json();
            renderKnowledge(data.entries || []);
        }
    } catch(e) {}
}

function renderKnowledge(entries) {
    let el = document.getElementById('knowledge-list');
    if (!el) return;
    if (!entries.length) {
        el.innerHTML = '<div style="color:#333;text-align:center;padding:12px;">No knowledge entries yet.</div>';
        return;
    }
    el.innerHTML = entries.map(function(e) {
        let cat = e.category || 'facts';
        let tags = (e.tags || []).join(', ');
        return '<div class="knowledge-entry">' +
            '<span class="knowledge-category knowledge-category-' + escapeHtml(cat) + '">' + escapeHtml(cat) + '</span> ' +
            '<span class="knowledge-text">' + escapeHtml(e.content_preview || '') + '</span>' +
            '<div class="knowledge-tags">' + escapeHtml(tags) + ' &middot; ' + (e.created || '').substring(0, 10) + '</div>' +
            '</div>';
    }).join('');
}

async function loadDreams() {
    try {
        let resp = await fetch('/api/dreams');
        if (resp.ok) {
            let data = await resp.json();
            renderDreams(data.dreams || []);
        }
    } catch(e) {}
}

function renderDreams(dreams) {
    let el = document.getElementById('dream-list');
    if (!el) return;
    if (!dreams.length) {
        el.innerHTML = '<div style="color:#333;text-align:center;padding:12px;">No dreams yet. Compaction creates entries.</div>';
        return;
    }
    el.innerHTML = dreams.map(function(d) {
        let ts = (d.ts || '').replace(/_/g, ' ');
        return '<div class="dream-entry" onclick="this.classList.toggle(\'expanded\')">' +
            '<span class="dream-ts">zz ' + escapeHtml(ts) + '</span> &middot; ' +
            '<span style="color:#555;">' + (d.chars || 0) + ' chars</span>' +
            '<div class="dream-preview">' + escapeHtml(d.preview || '') + '</div>' +
            '</div>';
    }).join('');
}

async function loadThoughts() {
    try {
        let resp = await fetch('/api/thoughts');
        if (resp.ok) {
            let data = await resp.json();
            let thoughts = data.thoughts || [];
            renderThoughts(thoughts);
            // Extract last real tokens for thought bubble
            if (thoughts.length > 0) {
                let latest = thoughts[0]; // newest first
                // Full latest thought for the bubble (readable, wraps)
                let snippet = (latest.preview || latest.raw_tail || '').replace(/\s+/g, ' ').trim();
                if (snippet.length > 240) snippet = snippet.substring(0, 240) + '…';
                _lastSnippet = snippet;
            }
        }
    } catch(e) {}
}

function thoughtToolVerb(name) {
    var verbs = {
        'bash': '\u2699 tinkering',
        'memorize': '\ud83d\udcad making a mental note',
        'remember': '\ud83d\udcad noting something',
        'recall': '\ud83d\udcad trying to remember',
        'write_file': '\u270d writing something',
        'read_file': '\ud83d\udc41 reading',
        'update_plan': '\ud83d\udcdd making plans',
        'http_request': '\ud83c\udf10 reaching out',
        'bg_run': '\u2699 starting something',
        'bg_check': '\ud83d\udc41 checking',
        'objective_done': '\u2728 done!',
    };
    return verbs[name] || name;
}

function renderThoughtSegments(segments) {
    return segments.map(function(seg) {
        if (seg.type === 'thinking') {
            return '<div class="thought-seg-thinking">' + escapeHtml(seg.text) + '</div>';
        } else if (seg.type === 'tool') {
            let argStr = '';
            if (typeof seg.args === 'object') {
                // Show a compact summary of args
                var keys = Object.keys(seg.args);
                var parts = [];
                keys.forEach(function(k) {
                    var v = seg.args[k];
                    if (typeof v === 'string') v = v.substring(0, 80);
                    parts.push(k + ': ' + v);
                });
                argStr = parts.join(' · ');
            } else {
                argStr = String(seg.args).substring(0, 120);
            }
            return '<div class="thought-seg-tool">' +
                thoughtToolVerb(seg.name) +
                (argStr ? '<span class="tool-args">' + escapeHtml(argStr) + '</span>' : '') +
                '</div>';
        }
        return '';
    }).join('');
}

function renderThoughts(thoughts) {
    let el = document.getElementById('thoughts-list');
    let statusEl = document.getElementById('thoughts-status');
    if (!el) return;
    if (!thoughts.length) {
        el.innerHTML = '<div style="color:#333;text-align:center;padding:12px;">No thoughts yet.</div>';
        if (statusEl) statusEl.textContent = '';
        return;
    }
    if (statusEl) statusEl.textContent = thoughts.length + ' ticks';

    el.innerHTML = thoughts.map(function(t) {
        let ts = (t.ts || '');
        if (ts.length > 16) ts = ts.substring(11, 16);
        let elapsed = t.elapsed_s ? t.elapsed_s.toFixed(1) + 's' : '';
        let preview = t.preview || '...';

        return '<div class="thought-entry" onclick="this.classList.toggle(\'expanded\')">' +
            '<div class="thought-header">' +
                '<span class="thought-tick">t' + (t.tick || 0) + '</span>' +
                '<span class="thought-time">' + ts + '</span>' +
                '<span class="thought-preview">' + escapeHtml(preview) + '</span>' +
                '<span class="thought-elapsed">' + elapsed + '</span>' +
            '</div>' +
            '<div class="thought-body">' + renderThoughtSegments(t.segments || []) + '</div>' +
        '</div>';
    }).join('');
}

function spawnParticle() {
    let c = document.getElementById('particles');
    if (!c || !particleChars) return;
    let chars = particleChars.split(' ');
    let ch = chars[Math.floor(Math.random() * chars.length)];
    let el = document.createElement('span');
    el.className = 'particle';
    el.textContent = ch;
    el.style.left = (30 + Math.random() * 40) + '%';
    el.style.top = (30 + Math.random() * 30) + '%';
    c.appendChild(el);
    setTimeout(() => el.remove(), 4000);
}

function animateCreature() {
    if (creatureFrames.length === 0) return;
    creatureIdx = (creatureIdx + 1) % creatureFrames.length;
    let el = document.getElementById('creature-art');
    if (el) el.textContent = creatureFrames[creatureIdx];
}

function update(data) {
    let p = data.persona || {};
    let hb = data.heartbeat || {};
    let cr = data.creature || {};

    // Creature
    creatureFrames = cr.frames || [];
    creatureInterval = cr.interval_ms || 1500;
    particleChars = cr.particles || '';
    if (creatureFrames.length > 0) {
        document.getElementById('creature-art').textContent = creatureFrames[0];
    }
    if (animTimer) clearInterval(animTimer);
    if (creatureInterval > 0 && creatureFrames.length > 1) {
        animTimer = setInterval(animateCreature, creatureInterval);
    }

    // Persona info
    document.getElementById('name-level').textContent =
        (p.name || 'eiDOS') + ' ✦ Lv.' + (p.level || 1) + ' ' + (cr.stage || '');
    document.getElementById('mood-display').textContent = p.mood || 'unknown';

    let xpPct = p.xp_next > 0 ? Math.min(100, (p.xp / p.xp_next) * 100) : 0;
    document.getElementById('xp-fill').style.width = xpPct + '%';
    document.getElementById('xp-text').textContent = p.xp + ' / ' + p.xp_next + ' XP';

    let traitsEl = document.getElementById('traits');
    traitsEl.innerHTML = (p.traits || []).map(t => '<span class="trait-badge">' + t + '</span>').join('');

    let titlesEl = document.getElementById('titles');
    titlesEl.innerHTML = (p.titles || []).map(t => '<span class="title-badge">⚡' + t + '</span>').join('');

    // Gauges
    let ram = hb.ram_pct || 0;
    document.getElementById('g-ram').textContent = makeBar(ram);
    let ramEl = document.getElementById('v-ram');
    ramEl.textContent = ram.toFixed(0) + '%';
    ramEl.className = gaugeClass(ram, 70, 85);

    let disk = hb.disk_free_gb || 0;
    let diskTotal = data.disk_total_gb || 0;
    let diskPct = diskTotal > 0 ? Math.min(100, (disk / diskTotal) * 100) : 0;
    document.getElementById('g-disk').textContent = makeBar(diskPct);
    let diskEl = document.getElementById('v-disk');
    diskEl.textContent = disk.toFixed(1) + ' GB';
    diskEl.className = gaugeClass(100 - diskPct, 80, 95);

    let llm = hb.llm_elapsed_s || 0;
    let llmPct = Math.min(100, (llm / 300) * 100);
    document.getElementById('g-llm').textContent = makeBar(llmPct);
    let llmEl = document.getElementById('v-llm');
    llmEl.textContent = llm.toFixed(1) + 's';
    llmEl.className = gaugeClass(llm, 120, 240);

    // Status info
    document.getElementById('current-goal').textContent =
        data.goal ? (data.goal.length > 300 ? data.goal.substring(0, 300) + '…' : data.goal) : '(no goal — sleeping)';
    document.getElementById('current-tick').textContent = hb.tick || '—';
    document.getElementById('uptime').textContent = formatUptime(hb.uptime_s);
    let failEl = document.getElementById('failures');
    failEl.textContent = hb.consecutive_failures || 0;
    failEl.className = (hb.consecutive_failures || 0) >= 3 ? 'gauge-crit' : '';
    document.getElementById('max-tokens').textContent = hb.current_max_tokens || '—';

    // Activity feed
    let feedEl = document.getElementById('feed');
    let obs = (data.observations || []).reverse();
    feedEl.innerHTML = obs.map(renderFeedEntry).join('');

    // Memory
    document.getElementById('memory').textContent = data.plan || '(empty)';

    // New panels
    updateNarration(data);
    updatePlan(data);
    updatePauseState(data.paused);
    _hasGoal = !!(data.goal && data.goal.trim());
    updateThoughtBubble(data.activity);
    _lastActivity = data.activity || {};

    // Last update
    document.getElementById('last-update').textContent =
        'updated ' + new Date().toLocaleTimeString();
}

async function loadChat() {
    try {
        let resp = await fetch('/api/chat');
        if (resp.ok) {
            let data = await resp.json();
            renderChat(data.messages || []);
        }
    } catch(e) {}
}

function renderChat(messages) {
    let el = document.getElementById('chat-messages');
    if (!messages.length) {
        el.innerHTML = '<div class="chat-empty">No messages yet. Send a message to guide eiDOS.</div>';
        return;
    }
    el.innerHTML = messages.map(m => {
        let dir = m.direction === 'outgoing' ? 'outgoing' : 'incoming';
        let label = dir === 'outgoing' ? 'You \u2192' : '\u2190 eiDOS';
        let stCls = m.status === 'delivered' ? 'chat-status-delivered' : 'chat-status-pending';
        let stTxt = m.status === 'delivered' ? '\u2713 delivered' : '\u25cc pending';
        let ts = m.ts || '';
        if (ts.length > 16) ts = ts.substring(11, 16);
        return '<div class="chat-msg ' + dir + '">' +
            escapeHtml(m.text) +
            '<div class="chat-meta">' + label + ' \u00b7 ' + ts +
            ' <span class="' + stCls + '">' + stTxt + '</span></div></div>';
    }).join('');
    el.scrollTop = el.scrollHeight;
}

async function sendChat() {
    let input = document.getElementById('chat-input');
    let btn = document.getElementById('chat-send');
    let msg = input.value.trim();
    if (!msg) return;
    btn.disabled = true;
    btn.textContent = 'Sending...';
    try {
        let resp = await fetch('/api/chat', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({message: msg})
        });
        if (resp.ok) { input.value = ''; loadChat(); }
    } catch(e) {}
    setChatHold(false);  // message sent \u2014 let eiDOS resume / answer
    btn.disabled = false;
    btn.textContent = 'Send \u25b8';
}

// --- Listening hold: focusing the chat box quiets eiDOS's loop so you can type ---
var _holdRefresh = null;
function setChatHold(held) {
    try {
        fetch('/api/chat_hold', {method:'POST', headers:{'Content-Type':'application/json'},
                                 body: JSON.stringify({held: !!held})});
    } catch(e) {}
    if (held) {
        if (_holdRefresh) clearInterval(_holdRefresh);
        _holdRefresh = setInterval(function(){ setChatHold(true); }, 20000); // keep fresh while focused
    } else if (_holdRefresh) {
        clearInterval(_holdRefresh); _holdRefresh = null;
    }
}

// --- CPU chart (real-time, fed by /api/activity polling) ---
var _cpuData = [];
var _cpuMax = 120; // ~2min at 1s polling
function pushCpu(pct) {
    if (typeof pct !== 'number') return;
    _cpuData.push(pct);
    if (_cpuData.length > _cpuMax) _cpuData.shift();
    drawCpuChart(_cpuData);
}
function drawCpuChart(pts) {
    var canvas = document.getElementById('cpu-chart');
    if (!canvas) return;
    var dpr = window.devicePixelRatio || 1;
    var rect = canvas.getBoundingClientRect();
    if (canvas.width !== rect.width * dpr || canvas.height !== rect.height * dpr) {
        canvas.width = rect.width * dpr;
        canvas.height = rect.height * dpr;
    }
    var ctx = canvas.getContext('2d');
    var W = canvas.width, H = canvas.height;
    ctx.clearRect(0, 0, W, H);
    // Grid lines at 25, 50, 75%
    ctx.strokeStyle = '#1a3a1a';
    ctx.lineWidth = 0.5 * dpr;
    for (var g = 0.25; g < 1; g += 0.25) {
        var gy = H - g * H;
        ctx.beginPath(); ctx.moveTo(0, gy); ctx.lineTo(W, gy); ctx.stroke();
    }
    if (pts.length < 2) return;
    // Fill under the curve
    ctx.fillStyle = 'rgba(0,255,65,0.08)';
    ctx.beginPath();
    ctx.moveTo(0, H);
    for (var i = 0; i < pts.length; i++) {
        var x = (i / (_cpuMax - 1)) * W;
        var y = H - (pts[i] / 100) * H;
        ctx.lineTo(x, y);
    }
    ctx.lineTo(((pts.length - 1) / (_cpuMax - 1)) * W, H);
    ctx.closePath();
    ctx.fill();
    // Draw line
    ctx.strokeStyle = '#00ff41';
    ctx.lineWidth = 1.5 * dpr;
    ctx.beginPath();
    for (var i = 0; i < pts.length; i++) {
        var x = (i / (_cpuMax - 1)) * W;
        var y = H - (pts[i] / 100) * H;
        if (i === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
    }
    ctx.stroke();
    // Latest value label
    var last = pts[pts.length - 1];
    ctx.fillStyle = '#00ff41';
    ctx.font = (10 * dpr) + 'px monospace';
    ctx.textAlign = 'right';
    ctx.fillText(Math.round(last) + '%', W - 4 * dpr, 14 * dpr);
}

async function poll() {
    try {
        let resp = await fetch('/api/status');
        if (resp.ok) {
            let data = await resp.json();
            update(data);
            spawnParticle();
        }
    } catch(e) { /* silent */ }
    loadChat();
    loadKnowledge();
    loadDreams();
    loadThoughts();
}

// Initial load + periodic poll
let _lastActivity = {};
poll();
loadChat();
loadKnowledge();
loadDreams();
loadThoughts();
setInterval(poll, {{INTERVAL_MS}});
setInterval(spawnParticle, 3000);
// Kick off activity polling (self-scheduling: fast when active, slow when idle)
pollActivity();
document.getElementById('chat-input').addEventListener('keydown', function(e) {
    if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); sendChat(); }
});
// Focus the chat box -> eiDOS enters its "listening" hold; blur -> it resumes.
document.getElementById('chat-input').addEventListener('focus', function() { setChatHold(true); });
document.getElementById('chat-input').addEventListener('blur', function() { setChatHold(false); });

// --- Self-guide editor (Dean owns the live file; eiDOS proposes) ---
var _selfGuideDirty = false;
async function loadSelfGuide() {
    try {
        let r = await fetch('/api/self_guide'); if (!r.ok) return;
        let d = await r.json();
        let ta = document.getElementById('self-guide-text');
        if (document.activeElement !== ta && !_selfGuideDirty) ta.value = d.content || '';
        document.getElementById('self-guide-status').textContent =
            (d.content ? (d.content.length + ' chars') : 'empty') + (d.has_proposal ? ' · proposal pending' : '');
        let pbox = document.getElementById('self-guide-proposal');
        if (d.has_proposal) {
            document.getElementById('self-guide-proposed').textContent = d.proposed || '';
            pbox._proposed = d.proposed || '';
            pbox.style.display = 'block';
        } else { pbox.style.display = 'none'; }
    } catch(e) {}
}
async function saveSelfGuide() {
    let ta = document.getElementById('self-guide-text');
    try {
        let r = await fetch('/api/self_guide', {method:'POST', headers:{'Content-Type':'application/json'},
                            body: JSON.stringify({content: ta.value})});
        document.getElementById('self-guide-saved').textContent = r.ok ? 'saved · live next tick' : 'save failed';
        _selfGuideDirty = false;
        setTimeout(function(){ document.getElementById('self-guide-saved').textContent=''; }, 4000);
        loadSelfGuide();
    } catch(e) {}
}
function acceptSelfGuideProposal() {
    let pbox = document.getElementById('self-guide-proposal');
    document.getElementById('self-guide-text').value = pbox._proposed || document.getElementById('self-guide-proposed').textContent;
    _selfGuideDirty = true;
    document.getElementById('self-guide-saved').textContent = 'loaded — review, then Save to make it live';
}
async function rejectSelfGuideProposal() {
    try { await fetch('/api/self_guide/reject', {method:'POST'}); } catch(e){}
    loadSelfGuide();
}
document.getElementById('self-guide-text').addEventListener('input', function(){ _selfGuideDirty = true; });
setInterval(loadSelfGuide, 10000); loadSelfGuide();

// --- Git safety (checkpoint / restore eiDOS's source) ---
async function loadGitLog() {
    try {
        let r = await fetch('/api/git/log'); if (!r.ok) return;
        let d = await r.json();
        document.getElementById('git-status').textContent =
            d.branch + ' @ ' + (d.head||'?') + (d.last_good ? ' · last good: ' + d.last_good : '');
        let cps = (d.checkpoints||[]).map(function(c){
            return '<div style="padding:2px 0;">⛳ <span style="color:#7CFC9B;">'+c.tag+'</span> <span style="color:#666;">('+c.when+')</span> '+
                   '<button onclick="doRestore(\''+c.tag+'\')" style="background:#2a2410;color:#ffcf6b;border:1px solid #4a3a1a;border-radius:4px;padding:1px 7px;cursor:pointer;font-size:10px;margin-left:6px;">restore</button></div>';
        }).join('');
        document.getElementById('git-checkpoints').innerHTML = cps || '<span style="color:#555;">no checkpoints yet</span>';
    } catch(e) {}
}
async function doCheckpoint() {
    let label = document.getElementById('git-label').value || '';
    document.getElementById('git-msg').textContent = 'checkpointing...';
    try {
        let r = await fetch('/api/git/checkpoint', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({label: label})});
        let d = await r.json();
        document.getElementById('git-msg').textContent = d.ok ? ('✓ ' + d.tag) : ('failed: ' + (d.error||''));
    } catch(e) { document.getElementById('git-msg').textContent='error'; }
    document.getElementById('git-label').value='';
    setTimeout(function(){ document.getElementById('git-msg').textContent=''; }, 5000);
    loadGitLog();
}
async function doRestore(tag) {
    let what = tag ? ('checkpoint ' + tag) : 'the last good checkpoint';
    if (!confirm('Restore eiDOS source to ' + what + '? Reverts code (not workspace memory) and restarts eiDOS paused.')) return;
    document.getElementById('git-msg').textContent = 'restoring...';
    try {
        let r = await fetch('/api/git/restore', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({tag: tag})});
        let d = await r.json();
        document.getElementById('git-msg').textContent = d.ok ? ('✓ ' + (d.message||'restored')) : ('failed: ' + (d.error||''));
    } catch(e) { document.getElementById('git-msg').textContent='error'; }
    loadGitLog();
}
setInterval(loadGitLog, 15000); loadGitLog();

// --- Self-edit proposals (eiDOS proposes source changes; you approve) ---
var _seBtn = 'border-radius:4px;padding:2px 9px;cursor:pointer;font-size:10px;margin-right:5px;';
async function loadSelfEdits() {
    try {
        let r = await fetch('/api/selfedit/list'); if (!r.ok) return;
        let d = await r.json();
        document.getElementById('selfedit-status').textContent = d.enabled ? 'enabled' : 'disabled (eiDOS cannot propose)';
        let props = (d.proposals||[]).filter(function(p){return p.status==='pending';});
        if (!props.length) { document.getElementById('selfedit-list').innerHTML = '<span style="color:#555;">no pending proposals</span>'; return; }
        document.getElementById('selfedit-list').innerHTML = props.map(function(p){
            return '<div style="border:1px solid #1a3a1a;border-radius:6px;padding:6px;margin-bottom:6px;">'+
              '<b style="color:#9fd4ff;">'+p.target+'</b> <span style="color:#7CFC9B;">+'+(p.added||0)+'</span>/<span style="color:#ff9b9b;">-'+(p.removed||0)+'</span> '+
              '<span style="color:#888;font-size:10px;">'+p.id+'</span><br>'+
              '<span style="color:#aaa;">'+(p.rationale||'')+'</span><br><div style="margin-top:4px;">'+
              '<button onclick="viewSelfEditDiff(\''+p.id+'\')" style="background:#13384f;color:#9fd4ff;border:1px solid #2e5f7f;'+_seBtn+'">View diff</button>'+
              '<button onclick="approveSelfEdit(\''+p.id+'\')" style="background:#123a1e;color:#7CFC9B;border:1px solid #2a5a2a;'+_seBtn+'">Approve &amp; apply ▸</button>'+
              '<button onclick="rejectSelfEdit(\''+p.id+'\')" style="background:#3a1212;color:#ff9b9b;border:1px solid #5a2a2a;'+_seBtn+'">Reject</button>'+
              '</div></div>';
        }).join('');
    } catch(e) {}
}
async function viewSelfEditDiff(id) {
    try {
        let r = await fetch('/api/selfedit/diff?id='+encodeURIComponent(id)); let d = await r.json();
        let el = document.getElementById('selfedit-diff');
        el.textContent = (d.stale?'[STALE — the file changed since this was proposed]\n\n':'')+(d.diff||d.error||'(no diff)');
        el.style.display='block';
    } catch(e){}
}
async function approveSelfEdit(id) {
    if (!confirm('Approve & apply self-edit '+id+'?\\n\\nThis rewrites eiDOS source, commits a pre-apply checkpoint, and restarts eiDOS PAUSED. If the new code crash-loops, the watchdog auto-restores the checkpoint.')) return;
    try {
        let r = await fetch('/api/selfedit/apply', {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({id:id})});
        let d = await r.json(); alert(d.ok ? (d.message||'applied') : ('failed: '+(d.error||'')));
        loadSelfEdits(); loadGitLog();
    } catch(e){}
}
async function rejectSelfEdit(id) {
    try { await fetch('/api/selfedit/reject', {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({id:id})}); } catch(e){}
    loadSelfEdits();
}
setInterval(loadSelfEdits, 12000); loadSelfEdits();
</script>
</body>
</html>"""


def _make_handler(config: Config):
    """Create a request handler class bound to the given config."""

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format, *args):
            pass  # suppress default stderr logging

        def _respond(self, code, content_type, body):
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            if isinstance(body, str):
                body = body.encode("utf-8")
            self.wfile.write(body)

        def do_GET(self):
            if self.path == "/":
                html = _HTML.replace("{{NAME}}", "eiDOS")
                html = html.replace("{{INTERVAL_MS}}", str(config.tick_interval_s * 1000))
                self._respond(200, "text/html; charset=utf-8", html)

            elif self.path == "/api/status":
                status = build_status(config)
                self._respond(200, "application/json", json.dumps(status))

            elif self.path == "/api/ping":
                ping = build_ping(config)
                self._respond(200, "application/json", json.dumps(ping))

            elif self.path == "/api/activity":
                activity = _read_json(config.workspace / "activity.json")
                activity["gpu"] = get_gpu_stats()
                activity["llm"] = get_llm_stats(config)
                activity["last_tool"] = _last_tool_call(config)
                self._respond(200, "application/json", json.dumps(activity))

            elif self.path == "/api/chat":
                chat = build_chat(config)
                self._respond(200, "application/json", json.dumps(chat))

            elif self.path == "/api/knowledge":
                data = build_knowledge_list(config)
                self._respond(200, "application/json", json.dumps(data))

            elif self.path == "/api/dreams":
                data = build_dream_list(config)
                self._respond(200, "application/json", json.dumps(data))

            elif self.path == "/api/speech/events":
                # SSE: push the id of each new speech clip the instant eiDOS speaks (no polling).
                try:
                    self.send_response(200)
                    self.send_header("Content-Type", "text/event-stream")
                    self.send_header("Cache-Control", "no-cache")
                    self.send_header("Connection", "keep-alive")
                    self.send_header("X-Accel-Buffering", "no")
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
                        except _sp_queue.Empty:
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
                        self.end_headers()
                        stream_glados(text, self.wfile)  # streams GLaDOS audio until done/disconnect
                    except (BrokenPipeError, ConnectionResetError, OSError):
                        pass
                return

            elif self.path.startswith("/api/gpu/wait"):
                # Event-driven GPU speech-gate: yield until TTS synthesis finishes. Holds while audio
                # streams (liveness), releases on completion (notify), a stall, or the max backstop.
                # Optional ?stall= / ?max= overrides; sane defaults otherwise (no duration guess).
                from urllib.parse import urlparse, parse_qs
                q = parse_qs(urlparse(self.path).query)
                def _qf(name, default, lo, hi):
                    try:
                        return min(max(float((q.get(name) or [default])[0]), lo), hi)
                    except (TypeError, ValueError):
                        return default
                stall = _qf("stall", _GPU_STALL_S, 1.0, 30.0)
                mx = _qf("max", _GPU_MAX_S, 2.0, 180.0)
                startup = _qf("startup", _GPU_STARTUP_S, 1.0, 60.0)
                self._respond(200, "application/json", json.dumps(gpu_wait_idle(stall, mx, startup)))

            elif self.path == "/api/thoughts":
                data = build_thoughts(config)
                self._respond(200, "application/json", json.dumps(data))

            elif self.path == "/api/metrics":
                data = build_metrics(config)
                self._respond(200, "application/json", json.dumps(data))

            elif self.path == "/api/control/status":
                self._respond(200, "application/json", json.dumps(_ctrl_status(config)))

            elif self.path == "/api/self_guide":
                self._respond(200, "application/json", json.dumps(build_self_guide(config)))

            elif self.path == "/api/git/log":
                import git_safety
                self._respond(200, "application/json", json.dumps(git_safety.git_log_summary(config)))

            elif self.path == "/api/selfedit/list":
                import selfedit
                self._respond(200, "application/json",
                              json.dumps({"proposals": selfedit.list_proposals(config, kind="self_edit"),
                                          "enabled": bool(getattr(config, "self_edit_enabled", False))}))

            elif self.path.startswith("/api/selfedit/diff"):
                import selfedit
                from urllib.parse import urlparse, parse_qs
                pid = parse_qs(urlparse(self.path).query).get("id", [""])[0]
                self._respond(200, "application/json", json.dumps(selfedit.get_diff(config, pid)))

            else:
                self._respond(404, "text/plain", "not found")

        def do_POST(self):
            if self.path == "/api/chat":
                length = int(self.headers.get("Content-Length", 0))
                if length > 10_000:
                    self._respond(413, "application/json", '{"error":"too large"}')
                    return
                body = self.rfile.read(length)
                try:
                    data = json.loads(body)
                except (json.JSONDecodeError, ValueError):
                    self._respond(400, "application/json", '{"error":"invalid json"}')
                    return
                message = str(data.get("message", "")).strip()
                if not message:
                    self._respond(400, "application/json", '{"error":"empty message"}')
                    return
                message = message[:2000]
                idir = config.interventions_dir
                idir.mkdir(parents=True, exist_ok=True)
                ts = time.strftime("%Y%m%d_%H%M%S", time.gmtime())
                fname = f"dash_{ts}.md"
                fpath = idir / fname
                n = 0
                while fpath.exists():
                    n += 1
                    fname = f"dash_{ts}_{n}.md"
                    fpath = idir / fname
                fpath.write_text(message)
                self._respond(200, "application/json", json.dumps({"ok": True, "filename": fname}))
            elif self.path == "/api/control/start":
                self._respond(200, "application/json", json.dumps(_ctrl_start(config)))
            elif self.path == "/api/control/stop":
                self._respond(200, "application/json", json.dumps(_ctrl_stop(config)))
            elif self.path == "/api/control/resume":
                self._respond(200, "application/json", json.dumps(_ctrl_resume(config)))
            elif self.path == "/api/control/pause":
                self._respond(200, "application/json", json.dumps(_ctrl_pause(config)))
            elif self.path == "/api/speech/say":
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
            elif self.path == "/api/chat_hold":
                # Listening hold — focusing the chat box quiets the loop. Best-effort,
                # never 500s; token-gated if a token is configured.
                if not _token_ok(self.headers, self.path, config):
                    self._respond(401, "application/json", '{"ok":false,"error":"auth"}'); return
                length = int(self.headers.get("Content-Length", 0) or 0)
                held = False
                if 0 < length <= 1000:
                    try:
                        held = bool(json.loads(self.rfile.read(length)).get("held"))
                    except (json.JSONDecodeError, ValueError):
                        held = False
                self._respond(200, "application/json", json.dumps(_write_chat_hold(config, held)))
            elif self.path == "/api/self_guide":
                # Operator saves the LIVE self-guide (clears any pending eiDOS proposal).
                if not _token_ok(self.headers, self.path, config):
                    self._respond(401, "application/json", '{"ok":false,"error":"auth"}'); return
                length = int(self.headers.get("Content-Length", 0) or 0)
                if length > 20000:
                    self._respond(413, "application/json", '{"ok":false,"error":"too large"}'); return
                try:
                    data = json.loads(self.rfile.read(length)) if length else {}
                except (json.JSONDecodeError, ValueError):
                    self._respond(400, "application/json", '{"ok":false,"error":"invalid json"}'); return
                from memory import write_self_guide
                try:
                    write_self_guide(config, str(data.get("content", "")))
                    try:
                        config.self_guide_proposed_path.unlink()
                    except FileNotFoundError:
                        pass
                    self._respond(200, "application/json", json.dumps({"ok": True}))
                except OSError as e:
                    self._respond(500, "application/json", json.dumps({"ok": False, "error": str(e)}))
            elif self.path == "/api/self_guide/reject":
                if not _token_ok(self.headers, self.path, config):
                    self._respond(401, "application/json", '{"ok":false,"error":"auth"}'); return
                try:
                    config.self_guide_proposed_path.unlink()
                except FileNotFoundError:
                    pass
                self._respond(200, "application/json", json.dumps({"ok": True}))
            elif self.path == "/api/git/checkpoint":
                if not _token_ok(self.headers, self.path, config):
                    self._respond(401, "application/json", '{"ok":false,"error":"auth"}'); return
                length = int(self.headers.get("Content-Length", 0) or 0)
                label = ""
                if 0 < length <= 2000:
                    try:
                        label = str(json.loads(self.rfile.read(length)).get("label", ""))[:80]
                    except (json.JSONDecodeError, ValueError):
                        label = ""
                self._respond(200, "application/json", json.dumps(_git_checkpoint_endpoint(config, label)))
            elif self.path == "/api/git/restore":
                if not _token_ok(self.headers, self.path, config):
                    self._respond(401, "application/json", '{"ok":false,"error":"auth"}'); return
                length = int(self.headers.get("Content-Length", 0) or 0)
                tag = ""
                if 0 < length <= 2000:
                    try:
                        tag = str(json.loads(self.rfile.read(length)).get("tag", ""))[:120]
                    except (json.JSONDecodeError, ValueError):
                        tag = ""
                self._respond(200, "application/json", json.dumps(_git_restore_endpoint(config, tag)))
            elif self.path == "/api/selfedit/apply":
                if not _token_ok(self.headers, self.path, config):
                    self._respond(401, "application/json", '{"ok":false,"error":"auth"}'); return
                length = int(self.headers.get("Content-Length", 0) or 0)
                pid = ""
                if 0 < length <= 2000:
                    try:
                        pid = str(json.loads(self.rfile.read(length)).get("id", ""))[:80]
                    except (json.JSONDecodeError, ValueError):
                        pid = ""
                self._respond(200, "application/json", json.dumps(_selfedit_apply_endpoint(config, pid)))
            elif self.path == "/api/selfedit/reject":
                if not _token_ok(self.headers, self.path, config):
                    self._respond(401, "application/json", '{"ok":false,"error":"auth"}'); return
                import selfedit
                length = int(self.headers.get("Content-Length", 0) or 0)
                pid, reason = "", ""
                if 0 < length <= 2000:
                    try:
                        d = json.loads(self.rfile.read(length)); pid = str(d.get("id",""))[:80]; reason = str(d.get("reason",""))[:200]
                    except (json.JSONDecodeError, ValueError):
                        pass
                self._respond(200, "application/json", json.dumps(selfedit.reject(config, pid, reason)))
            else:
                self._respond(404, "text/plain", "not found")

    return Handler


# --- Self-improvement: token gate, self-guide, listening hold ---

import threading as _threading
_LIFECYCLE_LOCK = _threading.RLock()  # serialize privileged ops (checkpoint/restore/apply/restart)


def _token_ok(headers, path, config) -> bool:
    """Pragmatic auth: if a dashboard token is configured, require it (header or ?token=)
    on state-changing POSTs. Default empty token = off (trusted-LAN/Tailscale buddy)."""
    tok = (getattr(config, "dashboard_token", "") or "").strip()
    if not tok:
        return True
    given = headers.get("X-EiDOS-Token", "") or ""
    if not given:
        try:
            from urllib.parse import urlparse, parse_qs
            given = parse_qs(urlparse(path).query).get("token", [""])[0]
        except Exception:  # noqa: BLE001
            given = ""
    return given == tok


def build_self_guide(config) -> dict:
    """Self-guide panel payload: live content + any pending eiDOS proposal."""
    from memory import read_self_guide, read_self_guide_proposed
    live = read_self_guide(config)
    proposed = read_self_guide_proposed(config)
    mtime = None
    try:
        mtime = config.self_guide_path.stat().st_mtime
    except OSError:
        pass
    return {
        "content": live,
        "proposed": proposed,
        "has_proposal": bool(proposed) and proposed.strip() != live.strip(),
        "mtime": mtime,
        "max_bytes": config.self_guide_max_bytes,
    }


def _write_chat_hold(config, held: bool) -> dict:
    """Dashboard owns the chat_hold flag file (single writer). Carries first_held_ts forward."""
    import json as _json
    from atomicio import replace_with_retry
    path = config.chat_hold_path
    try:
        if not held:
            try:
                path.unlink()
            except FileNotFoundError:
                pass
            return {"ok": True, "held": False}
        config.state_dir.mkdir(parents=True, exist_ok=True)
        now = time.time()
        first = now
        try:
            prev = _json.loads(path.read_text(encoding="utf-8"))
            if prev.get("held") and (now - float(prev.get("ts", 0) or 0)) <= float(config.chat_hold_ttl_s):
                first = float(prev.get("first_held_ts", now) or now)
        except (FileNotFoundError, ValueError, OSError):
            pass
        tmp = path.with_suffix(".tmp")
        tmp.write_text(_json.dumps({"held": True, "ts": now, "first_held_ts": first,
                                    "source": "chat_focus"}), encoding="utf-8")
        replace_with_retry(str(tmp), str(path))
        return {"ok": True, "held": True}
    except OSError as e:
        return {"ok": False, "error": str(e)}


# --- eiDOS process control (start paused / go / pause / stop) ---

def _ctrl_paths(config):
    from pathlib import Path
    ws = config.workspace
    return ws / "eidos.pid", ws / "paused", Path(__file__).resolve().parent


_pid_cache = {}  # pid -> (checked_at, alive); tasklist is slow, cache briefly


def _ctrl_pid_alive(pid):
    import subprocess, time
    if not pid or pid <= 0:
        return False
    now = time.time()
    hit = _pid_cache.get(pid)
    if hit and now - hit[0] < 2.5:
        return hit[1]
    try:
        out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"],
                             capture_output=True, text=True, timeout=10)
        alive = str(pid) in (out.stdout or "")
    except Exception:
        alive = False
    _pid_cache[pid] = (now, alive)
    return alive


def _ctrl_status(config):
    pidfile, pausefile, _ = _ctrl_paths(config)
    pid = 0
    try:
        pid = int(pidfile.read_text().strip())
    except Exception:
        pid = 0
    running = _ctrl_pid_alive(pid)
    return {"running": running, "paused": pausefile.exists(), "pid": (pid if running else 0)}


def _eidos_should_run_path(config):
    """Desired-state flag: present = the watchdog should keep eiDOS alive."""
    return config.workspace / "eidos.should_run"


def _spawn_eidos(config):
    """Spawn the eidos process detached, record its pid, return the pid."""
    import subprocess, sys, os
    _, _, kdir = _ctrl_paths(config)
    logf = open(config.workspace / "eidos_console.log", "ab")
    try:
        env = {**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8", "PYTHONUNBUFFERED": "1"}
        flags = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
        proc = subprocess.Popen(
            [sys.executable, str(kdir / "eidos.py"), "--config", str(kdir / "config.toml")],
            cwd=str(kdir), stdout=logf, stderr=subprocess.STDOUT, env=env, creationflags=flags,
        )
    finally:
        # The child inherits its own handle at CreateProcess; closing the parent's copy
        # avoids leaking a handle (and a Windows file lock) on every respawn.
        try:
            logf.close()
        except OSError:
            pass
    (config.workspace / "eidos.pid").write_text(str(proc.pid))
    return proc.pid


def _ctrl_start(config):
    pidfile, pausefile, kdir = _ctrl_paths(config)
    st = _ctrl_status(config)
    if st["running"]:
        return {"ok": False, "message": f"already running (pid {st['pid']})", **st}
    pausefile.write_text("paused on start - click GO to begin")  # boot PAUSED
    _eidos_should_run_path(config).write_text("1")   # arm the watchdog
    try:
        (config.state_dir / "rollback_attempted").unlink()  # fresh operator start re-arms auto-recovery
    except OSError:
        pass
    pid = _spawn_eidos(config)
    return {"ok": True, "message": f"started PAUSED (pid {pid}) - click GO to wake it",
            **_ctrl_status(config)}


def _ctrl_stop(config):
    import subprocess, os
    pidfile, pausefile, _ = _ctrl_paths(config)
    try:
        _eidos_should_run_path(config).unlink()   # disarm watchdog: this is an intentional stop
    except OSError:
        pass
    st = _ctrl_status(config)
    if not st["running"]:
        try:
            pidfile.unlink()
        except OSError:
            pass
        return {"ok": True, "message": "not running", **_ctrl_status(config)}
    pid = st["pid"]
    if os.name == "nt":
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                       capture_output=True, text=True, timeout=15)
    else:
        subprocess.run(["kill", "-9", str(pid)], capture_output=True, timeout=15)
    try:
        pidfile.unlink()
    except OSError:
        pass
    # Reap eidos's detached background jobs — they survive its kill and would otherwise orphan.
    reaped = 0
    try:
        import tools
        reaped = tools.reap_jobs(config, kill_all=True)
    except Exception:  # noqa: BLE001
        pass
    msg = f"force-killed pid {pid} (and children)" + (f"; reaped {reaped} bg job(s)" if reaped else "")
    return {"ok": True, "message": msg, **_ctrl_status(config)}


def _ctrl_resume(config):
    _, pausefile, _ = _ctrl_paths(config)
    try:
        pausefile.unlink()
    except OSError:
        pass
    return {"ok": True, "message": "resumed - consciousness running", **_ctrl_status(config)}


def _ctrl_pause(config):
    _, pausefile, _ = _ctrl_paths(config)
    pausefile.write_text("paused by operator")
    return {"ok": True, "message": "paused", **_ctrl_status(config)}


def _restart_eidos_keep_armed(config, reason="restart"):
    """Kill eidos but LEAVE the watchdog armed so it respawns with fresh code, booted PAUSED.
    Used after a git restore / self-edit apply. (Distinct from _ctrl_stop, which disarms.)"""
    import subprocess, os
    pidfile, pausefile, _ = _ctrl_paths(config)
    try:
        pausefile.write_text(f"paused: {reason}")   # boot paused for operator review
    except OSError:
        pass
    st = _ctrl_status(config)
    pid = st.get("pid")
    if st.get("running") and pid:
        if os.name == "nt":
            subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True, timeout=15)
        else:
            subprocess.run(["kill", "-9", str(pid)], capture_output=True, timeout=15)
    try:
        pidfile.unlink()
    except OSError:
        pass
    _pid_cache.clear()
    return pid


def _git_checkpoint_endpoint(config, label=""):
    import git_safety
    with _LIFECYCLE_LOCK:
        return git_safety.make_checkpoint(config, label or "manual checkpoint")


def _git_restore_endpoint(config, tag=""):
    import git_safety
    with _LIFECYCLE_LOCK:
        res = git_safety.restore_to(config, tag)
        if res.get("ok"):
            pid = _restart_eidos_keep_armed(config, reason=f"git restore {res.get('tag','')}")
            res["restarted_pid"] = pid
            res["message"] = (f"Restored {res.get('restored',0)} files to {res.get('tag')}. "
                              f"eiDOS restarting (paused) on the restored code.")
        return res


def _selfedit_apply_endpoint(config, pid):
    """Operator-approved self-edit apply: checkpoint+write+commit, then restart eidos paused.
    If the applied code crash-loops, the watchdog auto-restores the pre-apply checkpoint."""
    import selfedit
    with _LIFECYCLE_LOCK:
        res = selfedit.apply(config, pid)
        if res.get("ok"):
            newpid = _restart_eidos_keep_armed(config, reason=f"self-edit {pid}")
            res["restarted_pid"] = newpid
            res["message"] = (res.get("message", "") +
                              f" eiDOS restarting (paused) on the new code as pid {newpid}.")
        return res


# --- Watchdog: auto-restart eiDOS on unexpected death + record the crash so it learns ---

def _read_console_tail(config, n=30):
    try:
        lines = (config.workspace / "eidos_console.log").read_text(
            encoding="utf-8", errors="replace").splitlines()
        return "\n".join(lines[-n:])[-1200:]
    except Exception:  # noqa: BLE001
        return "(no console output captured)"


def _watchdog_note(config, msg):
    """Record a crash/recovery note where eiDOS will see it: observation + durable knowledge."""
    import json, time
    try:
        obs = {"tick": 0, "tool": "watchdog", "fail_kind": "crash", "success": False,
               "output": msg[:1500],
               "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
        with open(config.workspace / "observations.jsonl", "a", encoding="utf-8") as f:
            f.write(json.dumps(obs) + "\n")
    except Exception:  # noqa: BLE001
        pass
    try:
        import knowledge
        knowledge.store_entry(config, msg[:600], ["crash", "watchdog", "recovery"], "errors")
    except Exception:  # noqa: BLE001
        pass
    print(f"[watchdog] {msg[:140]}")


def _watchdog_event(config, msg):
    """Append a one-line watchdog event (rollback/standdown) for the operator / babysit check."""
    import time
    try:
        config.state_dir.mkdir(parents=True, exist_ok=True)
        with open(config.state_dir / "watchdog_events.log", "a", encoding="utf-8") as f:
            f.write(time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()) + "  " + str(msg)[:300] + "\n")
    except OSError:
        pass


def _watchdog_loop(config):
    """Supervise eiDOS: when it should be running but has died, record why and respawn it.

    Distinguishes an intentional Stop (eidos.should_run removed) from a crash, and backs
    off if it crash-loops so it never thrashes.
    """
    import time
    restarts = []
    while True:
        try:
            time.sleep(5)
            if not _eidos_should_run_path(config).exists():
                continue  # operator stopped it — do not resurrect
            try:
                pid = int((config.workspace / "eidos.pid").read_text().strip())
            except Exception:  # noqa: BLE001
                pid = 0
            if pid and _ctrl_pid_alive(pid):
                # Healthy. If we auto-rolled-back earlier and eidos has since been stable
                # for >10 min, clear the guard so a *future* unrelated break can recover too.
                try:
                    rb = config.state_dir / "rollback_attempted"
                    if rb.exists() and (time.time() - rb.stat().st_mtime) > 600:
                        rb.unlink()
                        _watchdog_note(config, "eiDOS stable for 10 min after rollback — re-arming auto-recovery.")
                except OSError:
                    pass
                continue  # healthy
            now = time.time()
            restarts = [t for t in restarts if now - t < 180]
            if len(restarts) >= 5:
                # Crash-loop. Likeliest cause is a bad code change (ours or a self-edit).
                # Before standing down, auto-restore last_good and retry on known-good code —
                # the core of unattended overnight resilience. Bounded to 2 attempts so a
                # persistent failure (or a failed respawn) can never loop forever OR die after
                # a single try: the attempt is counted UP FRONT so even a thrown restore counts.
                rb_marker = config.state_dir / "rollback_attempted"
                attempts = 0
                try:
                    attempts = int((rb_marker.read_text() or "0").split(",")[0])
                except (OSError, ValueError):
                    attempts = 0
                if attempts < 2:
                    try:
                        config.state_dir.mkdir(parents=True, exist_ok=True)
                        rb_marker.write_text(f"{attempts + 1},{now}")  # count the attempt up front
                    except OSError:
                        pass
                    try:
                        import git_safety
                        lg = git_safety.read_last_good(config)
                        if lg:
                            with _LIFECYCLE_LOCK:
                                res = git_safety.restore_to(config, lg)
                            _watchdog_note(config,
                                f"eiDOS crash-looped (5x/3min). Auto-restored last good checkpoint {lg} "
                                f"({res.get('restored', 0)} source files) — attempt {attempts + 1}/2 on "
                                f"known-good code. If this recurs the watchdog stands down for the operator.")
                            _watchdog_event(config, f"AUTO-ROLLBACK ({attempts + 1}/2) to {lg}")
                            restarts = []  # fresh chance on good code
                            new_pid = _spawn_eidos(config)
                            time.sleep(3)
                            alive = _ctrl_pid_alive(new_pid)
                            _watchdog_event(config, f"respawned pid {new_pid} alive={alive}")
                            print(f"[watchdog] rolled back to {lg}, respawned pid {new_pid} alive={alive}")
                        else:
                            _watchdog_event(config, "auto-rollback: no last_good checkpoint available")
                    except Exception as e:  # noqa: BLE001
                        print(f"[watchdog] auto-rollback error: {e}")
                        _watchdog_event(config, f"auto-rollback error: {e}")
                    continue  # retries are bounded by the attempt counter
                # Attempts exhausted (or no checkpoint) and still crash-looping → stand down.
                try:
                    _eidos_should_run_path(config).unlink()
                except OSError:
                    pass
                _watchdog_note(config, "eiDOS crash-looped even after rollback. Watchdog standing "
                                       "down — needs operator attention.")
                _watchdog_event(config, "STAND DOWN — crash-loop persisted after 2 rollbacks")
                continue
            tail = _read_console_tail(config, 30)
            _watchdog_note(config,
                           "eiDOS process died unexpectedly. Last console output before death:\n"
                           + tail + "\n\nThe watchdog is auto-restarting you. Note what happened "
                           "above and adapt so it does not recur.")
            restarts.append(now)
            new_pid = _spawn_eidos(config)
            print(f"[watchdog] respawned eiDOS as pid {new_pid}")
        except Exception:  # noqa: BLE001 — the watchdog must never die
            pass


# --- GPU + LLM telemetry for the dashboard (nvidia-smi + metrics.jsonl tail) ---

def get_gpu_stats(_cache={"t": 0.0, "v": {}}):
    import subprocess, time
    now = time.time()
    if _cache["v"] and now - _cache["t"] < 1.0:
        return _cache["v"]
    v = {}
    try:
        out = subprocess.run(
            ["nvidia-smi",
             "--query-gpu=utilization.gpu,memory.used,memory.total,temperature.gpu,power.draw,name",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=5,
        )
        line = (out.stdout or "").strip().splitlines()[0]
        p = [x.strip() for x in line.split(",")]
        v = {"util": float(p[0]), "mem_used": float(p[1]), "mem_total": float(p[2]),
             "temp": float(p[3]), "power": float(p[4]),
             "name": p[5] if len(p) > 5 else "GPU"}
    except Exception:
        v = {}
    _cache["t"] = now
    _cache["v"] = v
    return v


def get_llm_stats(config, _cache={"t": 0.0, "v": {}}):
    import time
    now = time.time()
    if _cache["v"] and now - _cache["t"] < 1.0:
        return _cache["v"]
    v = {}
    try:
        p = config.workspace / "metrics.jsonl"
        with open(p, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - 4096))
            tail = f.read().decode("utf-8", "replace")
        lines = [ln for ln in tail.splitlines() if ln.strip()]
        if lines:
            m = json.loads(lines[-1])
            ct = m.get("completion_tokens", 0) or 0
            el = m.get("llm_elapsed_s", 0) or 0
            v = {"tok_s": round(ct / el, 1) if el > 0 else 0,
                 "completion_tokens": ct,
                 "prompt_tokens": m.get("prompt_tokens", 0),
                 "llm_elapsed_s": round(el, 2),
                 "tick": m.get("tick", 0)}
    except Exception:
        v = {}
    _cache["t"] = now
    _cache["v"] = v
    return v


def main():
    parser = argparse.ArgumentParser(description="eiDOS dashboard server")
    parser.add_argument("--config", default="config.toml", help="Path to config file")
    parser.add_argument("--port", type=int, default=None, help="Override dashboard port")
    args = parser.parse_args()

    config = load_config(args.config)
    port = args.port or config.dashboard_port

    handler = _make_handler(config)
    server = ThreadingHTTPServer(("0.0.0.0", port), handler)
    server.daemon_threads = True
    import threading
    threading.Thread(target=_watchdog_loop, args=(config,), daemon=True).start()
    print("[watchdog] armed — eiDOS auto-restart-on-crash enabled")
    print(f"[dashboard] Serving on http://0.0.0.0:{port}")
    print(f"[dashboard] Reading from {config.workspace}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[dashboard] Shutting down")
        server.shutdown()


if __name__ == "__main__":
    main()
