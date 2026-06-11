#!/usr/bin/env python3
"""eiDOS dashboard — operator shell: web UI + supervisor/watchdog.

Two co-located responsibilities (phase 8.3 split VOICE out into its own process — voice.py — so a
native TTS/ffmpeg crash can't take the watchdog down; the UI HTML lives in static/dashboard.html):
  UI         — HTML dashboard (static/dashboard.html) + /api/status,/api/ping,/api/activity models
  SUPERVISOR — watchdog (spawn/respawn/crash-loop auto-rollback), /api/control/* + the event-driven
               /api/control/wait channel, git safety, self-edit apply, self-guide apply (trust boundary)

The browser loads this page from here (port 8099) but opens the speech SSE + audio streams directly
to the voice service (config.voice_port); eidos POSTs speech and yields the GPU gate there too.

Writes: paused/should_run/pid sentinels, chat_hold.json, interventions/, self_guide.md, watchdog
crash notes, and the source tree via git restore / self-edit apply. Stdlib only — no dependencies.
"""

import argparse
import json
import logging
import sys
import threading
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



# --- Control-change channel (event-driven; ARCHITECTURE_PRINCIPLES.md #1) -----------------
# The reverse of the GPU gate: the dashboard is the PRODUCER of control state (pause/resume,
# listening hold, chat arrival) and eidos is the consumer. v1 made eidos poll three sentinel
# files on timers (pause @5s, hold @2s, interventions @<=2s) — delay-based guessing. Now every
# control mutation bumps a sequence counter and notifies; eidos makes ONE long-poll to
# /api/control/wait that returns the instant anything changes (or at its bounded timeout).
# The sentinel files REMAIN the crash-survivable ground truth — eidos re-reads them on wake and
# falls back to nap-polling if this channel is down. It's the polled consumption that violated
# the principle, not the files.
_ctl_cond = threading.Condition()
_ctl_seq = 0          # bumped on every control-state change; guarded by _ctl_cond


def control_notify(reason: str = "") -> None:
    """Producer hook: call after ANY control-state mutation (pause/resume/hold/chat)."""
    global _ctl_seq
    with _ctl_cond:
        _ctl_seq += 1
        _ctl_cond.notify_all()


def control_wait(config, since: int, max_s: float = 25.0) -> dict:
    """Block until the control seq passes `since` (event) or `max_s` elapses (bounded long-poll).
    Returns the new seq + a state snapshot so the consumer never needs a second request."""
    start = time.monotonic()
    max_s = max(0.0, min(float(max_s), 60.0))
    with _ctl_cond:
        while _ctl_seq <= since:
            remaining = max_s - (time.monotonic() - start)
            if remaining <= 0:
                break
            _ctl_cond.wait(timeout=remaining)
        seq = _ctl_seq
    snap = {"seq": seq, "paused": False, "held": False, "interventions": 0}
    try:
        snap["paused"] = (config.workspace / "paused").exists()
        snap["held"] = config.chat_hold_path.exists()
        idir = config.interventions_dir
        if idir.exists():
            snap["interventions"] = sum(
                1 for p in idir.iterdir()
                if not p.name.startswith(".") and p.suffix != ".done")
    except OSError:
        pass
    return snap


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
            "spoken": bool(r.get("spoken", False)),  # spoken aloud (speak tool) vs silent <reply>
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


# --- HTML Template (served from static/dashboard.html; phase 8.3a) ---

_STATIC_DIR = Path(__file__).resolve().parent / "static"


def _render_html(config: Config) -> str:
    """Load the dashboard page from static/dashboard.html and fill placeholders. Read per
    request so UI edits go live without a dashboard restart (the page is fetched once per
    browser load, so the disk read is negligible)."""
    html = (_STATIC_DIR / "dashboard.html").read_text(encoding="utf-8")
    html = html.replace("{{NAME}}", "eiDOS")
    html = html.replace("{{INTERVAL_MS}}", str(config.tick_interval_s * 1000))
    html = html.replace("{{VOICE_PORT}}", str(getattr(config, "voice_port", 8098)))
    return html


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
                self._respond(200, "text/html; charset=utf-8", _render_html(config))

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

            elif self.path.startswith("/api/control/wait"):
                # eidos's event-driven control channel: blocks until pause/hold/chat state
                # changes past ?since= (or ?max_s= elapses), then returns seq + snapshot.
                from urllib.parse import urlparse, parse_qs
                q = parse_qs(urlparse(self.path).query)
                try:
                    since = int((q.get("since") or ["-1"])[0])
                except (TypeError, ValueError):
                    since = -1
                try:
                    max_s = float((q.get("max_s") or ["25"])[0])
                except (TypeError, ValueError):
                    max_s = 25.0
                self._respond(200, "application/json",
                              json.dumps(control_wait(config, since, max_s)))

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
            # Uniform auth (phase 8.1): when a token is configured, EVERY state-changing POST
            # requires it — including /api/control/* (the kill-switch), /api/chat (the agent's
            # input channel), and /api/speech/* — which were previously ungated even with a
            # token set. Default empty token = open (accident-safety, trusted-LAN/Tailscale).
            if not _token_ok(self.headers, self.path, config):
                self._respond(401, "application/json", '{"error":"unauthorized"}')
                return
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
                control_notify("chat")   # wake eidos instantly — a Boss message is the top event
                self._respond(200, "application/json", json.dumps({"ok": True, "filename": fname}))
            elif self.path == "/api/control/start":
                self._respond(200, "application/json", json.dumps(_ctrl_start(config)))
            elif self.path == "/api/control/stop":
                self._respond(200, "application/json", json.dumps(_ctrl_stop(config)))
            elif self.path == "/api/control/resume":
                self._respond(200, "application/json", json.dumps(_ctrl_resume(config)))
            elif self.path == "/api/control/pause":
                self._respond(200, "application/json", json.dumps(_ctrl_pause(config)))
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
    import hmac
    return hmac.compare_digest(given, tok)   # constant-time; never short-circuits on prefix


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
            control_notify("hold_release")   # the loop resumes the instant Dean unfocuses/sends
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
        control_notify("hold")
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


# Death event (phase 4b, ARCH #1): the spawn HOLDS the Popen handle and a daemon thread
# wait()s on it, so a child exit is an interrupt to the watchdog — not something a 5s
# tasklist poll discovers late. The pid file + tasklist liveness remain ground truth for
# children this dashboard run didn't spawn (e.g. eidos surviving a dashboard restart).
_child_died = threading.Event()


def _watch_child(proc):
    try:
        proc.wait()
    finally:
        _child_died.set()


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
    # Floor for the stale-heartbeat watchdog: a fresh process hasn't written a heartbeat yet, so the
    # OLD heartbeat ts is stale — record spawn time so we don't flag a still-booting eidos as wedged.
    try:
        (config.workspace / "eidos_spawn.ts").write_text(str(time.time()))
    except OSError:
        pass
    _child_died.clear()
    threading.Thread(target=_watch_child, args=(proc,), daemon=True,
                         name="eidos-death-watch").start()
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
    control_notify("resume")   # wake eidos's control channel the instant the operator resumes
    return {"ok": True, "message": "resumed - consciousness running", **_ctrl_status(config)}


def _ctrl_pause(config):
    _, pausefile, _ = _ctrl_paths(config)
    pausefile.write_text("paused by operator")
    control_notify("pause")
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


def _current_heartbeat_ts(config) -> float:
    try:
        return float(_read_json(config.workspace / "heartbeat.json").get("ts", 0) or 0)
    except (ValueError, TypeError):
        return 0.0


def _eidos_spawn_ts(config) -> float:
    """When eidos was last spawned — the floor for the stale-heartbeat check, so a freshly-booting
    process (heartbeat not yet written) isn't mistaken for a wedged one."""
    try:
        return float((config.workspace / "eidos_spawn.ts").read_text().strip() or 0)
    except (OSError, ValueError):
        return 0.0


def _eidos_is_stuck(config, now: float = None) -> tuple:
    """(stuck, stale_for_seconds): True when eidos is ALIVE but not TICKING — its heartbeat (which
    only advances on a SUCCESSFUL tick) has been frozen longer than eidos_stuck_threshold_s. Floored
    by the spawn time so a still-booting eidos isn't flagged, and skipped while paused (a paused eidos
    legitimately doesn't tick). Pure read; the watchdog decides what to do with it."""
    import time as _t
    thr = getattr(config, "eidos_stuck_threshold_s", 600)
    if thr <= 0 or (config.workspace / "paused").exists():
        return False, 0.0
    last_progress = max(_current_heartbeat_ts(config), _eidos_spawn_ts(config))
    stale_for = (now or _t.time()) - last_progress
    return (stale_for > thr), stale_for


def _selfedit_apply_endpoint(config, pid):
    """Operator-approved self-edit apply: checkpoint+write+commit, then restart eidos paused.
    Arms the HEALTH PROBE (a pending_apply marker) before restarting, so the watchdog can
    auto-rollback a self-edit that boots-but-misbehaves — not just one that crash-loops."""
    import selfedit
    with _LIFECYCLE_LOCK:
        res = selfedit.apply(config, pid)
        if res.get("ok"):
            probe_s = float(getattr(config, "self_edit_health_probe_s", 90) or 90)
            selfedit.write_pending_apply(
                config, pid, res.get("prev_sha", ""),
                baseline_heartbeat_ts=_current_heartbeat_ts(config),
                deadline_epoch=time.time() + probe_s)
            newpid = _restart_eidos_keep_armed(config, reason=f"self-edit {pid}")
            res["restarted_pid"] = newpid
            res["message"] = (res.get("message", "") +
                              f" eiDOS restarting (paused) on the new code as pid {newpid}. "
                              f"Health probe armed ({probe_s:.0f}s).")
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


def _selfedit_probe(config):
    """Resolve or roll back an in-flight self-edit. Called from the watchdog's alive branch.

    Healthy when the booted code dropped its applied_ok breadcrumb (matching id) AND it is
    either paused (awaiting operator GO — the normal post-apply state) or has ticked past the
    pre-apply heartbeat baseline. If the deadline passes without that signal — the new code hung
    mid-boot (no breadcrumb) or wedged-alive (heartbeat never advanced) — revert to prev_sha and
    restart on the reverted code, paused. This is the gap the crash-loop path can't see: a bad
    self-edit that does NOT crash."""
    import selfedit
    pend = selfedit.read_pending_apply(config)
    if not pend:
        return
    crumb = selfedit.read_applied_ok(config)
    booted = bool(crumb and crumb.get("id") == pend.get("id"))
    paused = (config.workspace / "paused").exists()
    hb_ts = _current_heartbeat_ts(config)
    progressed = hb_ts > float(pend.get("baseline_heartbeat_ts", 0) or 0)
    if booted and (paused or progressed):
        selfedit.clear_pending_apply(config)
        _watchdog_event(config, f"self-edit {pend.get('id')} passed health probe "
                                f"({'paused, awaiting GO' if paused else 'ticking on new code'})")
        return
    if time.time() < float(pend.get("deadline_epoch", 0) or 0):
        return  # still within the probe window — keep watching
    prev = pend.get("prev_sha", "")
    res = selfedit.autorollback(config, prev, pend.get("id"))
    with _LIFECYCLE_LOCK:
        newpid = _restart_eidos_keep_armed(config, reason=f"self-edit {pend.get('id')} rolled back")
    why = "never reached run_loop (no breadcrumb)" if not booted else "heartbeat never advanced (wedged)"
    _watchdog_note(config,
        f"Self-edit {pend.get('id')} FAILED its health probe — {why}. Reverted source to "
        f"{prev[:9]} ({res.get('restored', 0)} files) and restarted you (paused) on the reverted "
        f"code as pid {newpid}. The change is rolled back; review the proposal before retrying.")
    _watchdog_event(config, f"HEALTH-PROBE ROLLBACK of self-edit {pend.get('id')} -> {prev[:9]} ({why})")


def _watchdog_loop(config):
    """Supervise eiDOS: when it should be running but has died, record why and respawn it.

    Distinguishes an intentional Stop (eidos.should_run removed) from a crash, and backs
    off if it crash-loops so it never thrashes.
    """
    import time, os, subprocess
    restarts = []
    stuck_restarts = []   # timestamps of stale-heartbeat restarts (bounded so an external cause can't thrash)
    while True:
        try:
            # Event-driven (phase 4b): a spawned child's death fires _child_died instantly;
            # the 5s timeout retains every periodic check (should_run, stability re-arm,
            # children from a previous dashboard run that we have no handle for).
            died = _child_died.wait(timeout=5)
            if died:
                _child_died.clear()
                _pid_cache.clear()   # bypass the liveness cache — react to the death NOW
            if not _eidos_should_run_path(config).exists():
                continue  # operator stopped it — do not resurrect
            try:
                pid = int((config.workspace / "eidos.pid").read_text().strip())
            except Exception:  # noqa: BLE001
                pid = 0
            if pid and _ctrl_pid_alive(pid):
                # Self-edit HEALTH PROBE: a process being alive isn't proof a just-applied self-edit
                # is healthy — it could be hung mid-boot (never reaching run_loop) or wedged-alive
                # after resume. Resolve when the new code dropped its applied_ok breadcrumb AND is
                # either awaiting operator GO (paused) or ticking (heartbeat past the pre-apply
                # baseline). Roll back to prev_sha if the probe deadline passes without that.
                try:
                    _selfedit_probe(config)
                except Exception as _pe:  # noqa: BLE001 - probe must never crash the watchdog
                    _watchdog_event(config, f"health-probe error: {_pe}")
                # STALE-HEARTBEAT watchdog: alive != ticking. If eidos should be running and isn't
                # paused, but its heartbeat hasn't advanced in eidos_stuck_threshold_s, it's WEDGED —
                # a hung dream, or the LLM persistently timing out (the heartbeat only advances on a
                # SUCCESSFUL tick). Restart it: a fresh eidos recovers from the WAL and, if the LLM is
                # the cause, cleanly waits for /health instead of staying stuck. Bounded (3/30min) so
                # a persistent external cause (LLM down/slow) can't make it thrash.
                try:
                    _now = time.time()
                    _stuck, _stale = _eidos_is_stuck(config, _now)
                    if _stuck:
                        stuck_restarts = [t for t in stuck_restarts if _now - t < 1800]
                        if len(stuck_restarts) < 3:
                            stuck_restarts.append(_now)
                            _watchdog_note(config,
                                f"eiDOS is alive but hasn't completed a tick in {int(_stale)}s — wedged "
                                f"(a hung dream, or the LLM persistently timing out). Auto-restarting you; "
                                f"recover from the WAL and note what stalled so it doesn't recur.")
                            _watchdog_event(config, f"STALE-RESTART (heartbeat {int(_stale)}s old, pid {pid})")
                            if os.name == "nt":
                                subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True, timeout=15)
                            else:
                                subprocess.run(["kill", "-9", str(pid)], capture_output=True, timeout=15)
                            new_pid = _spawn_eidos(config)
                            _child_died.clear(); _pid_cache.clear()
                            _watchdog_event(config, f"stale-restart respawned pid {new_pid}")
                            print(f"[watchdog] STALE-RESTART: killed wedged {pid}, respawned {new_pid} (stale {int(_stale)}s)")
                            continue
                        elif len(stuck_restarts) == 3:
                            stuck_restarts.append(_now)  # log the stand-down once, then go quiet for the window
                            _watchdog_event(config, "STALE-RESTART bound hit (3/30min) — persistent wedge, "
                                            "likely external (LLM down/slow). Holding; eidos waits for /health.")
                except Exception as _se:  # noqa: BLE001 - the stale check must never crash the watchdog
                    _watchdog_event(config, f"stale-check error: {_se}")
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
                            try:
                                import selfedit
                                selfedit.clear_pending_apply(config)  # last_good IS the pre-apply floor
                            except Exception:  # noqa: BLE001
                                pass
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

    # Boot-reconcile a dangling self-edit health probe BEFORE arming the watchdog: if the
    # dashboard itself restarted mid-probe and the deadline has since passed without a healthy
    # signal, roll the edit back now rather than leaving it unresolved (the marker outlives any
    # one dashboard process — it's in state_dir).
    try:
        import selfedit
        _pend = selfedit.read_pending_apply(config)
        if _pend and time.time() >= float(_pend.get("deadline_epoch", 0) or 0):
            _crumb = selfedit.read_applied_ok(config)
            if not (_crumb and _crumb.get("id") == _pend.get("id")):
                res = selfedit.autorollback(config, _pend.get("prev_sha", ""), _pend.get("id"))
                _watchdog_event(config, f"BOOT-RECONCILE rolled back stranded self-edit "
                                        f"{_pend.get('id')} -> {_pend.get('prev_sha','')[:9]} "
                                        f"({res.get('restored',0)} files)")
            else:
                selfedit.clear_pending_apply(config)  # it had booted OK; just clear the stale marker
    except Exception as _bre:  # noqa: BLE001
        print(f"[dashboard] boot-reconcile error: {_bre}")

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
