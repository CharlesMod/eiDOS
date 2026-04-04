#!/usr/bin/env python3
"""Kairos dashboard — single-file HTTP server for monitoring via Tailscale.

Serves:
  GET /          → HTML dashboard (auto-refreshing)
  GET /api/status → full JSON status blob
  GET /api/ping   → tiny health-check JSON (<500 bytes)

All data is read-only from workspace files. Kairos is the sole writer.
Stdlib only — no frameworks, no dependencies.
"""

import argparse
import json
import sys
import time
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path

# Add project root for imports
sys.path.insert(0, str(Path(__file__).parent))

from config import load_config, Config
from ascii_art import get_creature
from persona import load_persona, compute_level


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


def build_status(config: Config) -> dict:
    """Assemble full status from workspace files."""
    heartbeat = _read_json(config.workspace / "heartbeat.json")
    persona = _read_json(config.workspace / "persona.json")
    wal = _read_json(config.workspace / "wal.json")
    goal = _read_text(config.workspace / "goal.md")
    memory = _read_text(config.workspace / "memory.md")
    observations = _tail_jsonl(config.workspace / "observations.jsonl", 20)

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
    elif heartbeat.get("cpu_temp_c") and heartbeat["cpu_temp_c"] > 75:
        special = "thermal"
    elif not goal.strip():
        special = "sleeping"

    creature = get_creature(level, mood, traits, special=special)

    return {
        "heartbeat": heartbeat,
        "persona": {
            "name": persona.get("name", "Kairos"),
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
        "memory": memory[:3000],
        "observations": observations,
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
        "temp_c": hb.get("cpu_temp_c"),
        "disk_pct": round(100 - (hb.get("disk_free_gb", 0) / max(hb.get("disk_free_gb", 1), 0.01)) * 100, 1) if hb else None,
        "ram_pct": hb.get("ram_pct"),
        "uptime_s": hb.get("uptime_s", 0),
    }


def build_chat(config: Config) -> dict:
    """Build chat history from interventions and pending questions."""
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

    # LLM → Operator: pending questions
    questions = _tail_jsonl(config.workspace / "pending_questions.jsonl", 50)
    for q in questions:
        messages.append({
            "direction": "incoming",
            "ts": q.get("ts", ""),
            "text": q.get("question", ""),
            "status": q.get("status", "pending"),
        })

    messages.sort(key=lambda m: m.get("ts", ""))
    return {"messages": messages}


# --- HTML Template ---

_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Kairos — {{NAME}}</title>
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
</style>
</head>
<body>
<div id="particles"></div>
<div class="container">
    <div class="header">
        <h1>⟨ KAIROS ⟩</h1>
        <div class="subtitle">autonomous agent — field station monitor</div>
    </div>

    <!-- Left: Creature -->
    <div class="panel">
        <div class="panel-title">Buddy</div>
        <div id="creature-box">
            <pre id="creature-art"></pre>
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
            <div class="gauge"><span class="gauge-label">CPU Temp</span><span class="gauge-bar" id="g-temp"></span><span class="gauge-val" id="v-temp"></span></div>
            <div class="gauge"><span class="gauge-label">RAM</span><span class="gauge-bar" id="g-ram"></span><span class="gauge-val" id="v-ram"></span></div>
            <div class="gauge"><span class="gauge-label">Disk Free</span><span class="gauge-bar" id="g-disk"></span><span class="gauge-val" id="v-disk"></span></div>
            <div class="gauge"><span class="gauge-label">LLM Latency</span><span class="gauge-bar" id="g-llm"></span><span class="gauge-val" id="v-llm"></span></div>
        </div>
        <hr style="border-color:#1a3a1a;margin:8px 0;">
        <div style="font-size:12px;">
            <div><span style="color:#aaa;">Goal:</span> <span id="current-goal" style="color:#00ff41;"></span></div>
            <div><span style="color:#aaa;">Tick:</span> <span id="current-tick"></span> · <span style="color:#aaa;">Uptime:</span> <span id="uptime"></span></div>
            <div><span style="color:#aaa;">Failures:</span> <span id="failures"></span> · <span style="color:#aaa;">Max Tokens:</span> <span id="max-tokens"></span></div>
        </div>
    </div>

    <!-- Activity Feed -->
    <div class="panel" style="grid-column: 1 / -1;">
        <div class="panel-title">Activity Feed</div>
        <div class="feed" id="feed"></div>
    </div>

    <!-- Operator Chat -->
    <div class="panel" style="grid-column: 1 / -1;">
        <div class="panel-title">Operator Chat <span style="font-size:9px;color:#555;float:right;">async · delivered on next tick</span></div>
        <div class="chat-messages" id="chat-messages"></div>
        <div class="chat-input-row">
            <textarea id="chat-input" placeholder="Send a message to Kairos..." rows="1"></textarea>
            <button id="chat-send" onclick="sendChat()">Send ▸</button>
        </div>
    </div>

    <!-- Memory -->
    <div class="panel" style="grid-column: 1 / -1;">
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
        (p.name || 'Kairos') + ' ✦ Lv.' + (p.level || 1) + ' ' + (cr.stage || '');
    document.getElementById('mood-display').textContent = p.mood || 'unknown';

    let xpPct = p.xp_next > 0 ? Math.min(100, (p.xp / p.xp_next) * 100) : 0;
    document.getElementById('xp-fill').style.width = xpPct + '%';
    document.getElementById('xp-text').textContent = p.xp + ' / ' + p.xp_next + ' XP';

    let traitsEl = document.getElementById('traits');
    traitsEl.innerHTML = (p.traits || []).map(t => '<span class="trait-badge">' + t + '</span>').join('');

    let titlesEl = document.getElementById('titles');
    titlesEl.innerHTML = (p.titles || []).map(t => '<span class="title-badge">⚡' + t + '</span>').join('');

    // Gauges
    let temp = hb.cpu_temp_c;
    let tempPct = temp != null ? Math.min(100, (temp / 85) * 100) : 0;
    document.getElementById('g-temp').textContent = temp != null ? makeBar(tempPct) : '[  n/a   ]';
    let tempEl = document.getElementById('v-temp');
    tempEl.textContent = temp != null ? temp + '°C' : 'n/a';
    tempEl.className = temp != null ? gaugeClass(temp, 65, 75) : 'gauge-val';

    let ram = hb.ram_pct || 0;
    document.getElementById('g-ram').textContent = makeBar(ram);
    let ramEl = document.getElementById('v-ram');
    ramEl.textContent = ram.toFixed(0) + '%';
    ramEl.className = gaugeClass(ram, 70, 85);

    let disk = hb.disk_free_gb || 0;
    let diskPct = Math.min(100, (disk / 64) * 100);
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
        data.goal ? data.goal.substring(0, 120) : '(no goal — sleeping)';
    document.getElementById('current-tick').textContent = hb.tick || '—';
    document.getElementById('uptime').textContent = formatUptime(hb.uptime_s);
    let failEl = document.getElementById('failures');
    failEl.textContent = hb.consecutive_failures || 0;
    failEl.className = (hb.consecutive_failures || 0) >= 3 ? 'gauge-crit' : '';
    document.getElementById('max-tokens').textContent = hb.current_max_tokens || '—';

    // Activity feed
    let feedEl = document.getElementById('feed');
    let obs = (data.observations || []).reverse();
    feedEl.innerHTML = obs.map(o => {
        let cls = feedClass(o);
        let tool = o.tool || '?';
        let out = escapeHtml((o.output || '').substring(0, 200));
        let tick = o.tick || '';
        let dur = o.duration_s ? ' (' + o.duration_s.toFixed(1) + 's)' : '';
        return '<div class="feed-entry ' + cls + '">' +
            '<span class="feed-tick">t' + tick + '</span> ' +
            '<b>' + tool + '</b>' + dur + ' — ' + out +
            '</div>';
    }).join('');

    // Memory
    document.getElementById('memory').textContent = data.memory || '(empty)';

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
        el.innerHTML = '<div class="chat-empty">No messages yet. Send a message to guide Kairos.</div>';
        return;
    }
    el.innerHTML = messages.map(m => {
        let dir = m.direction === 'outgoing' ? 'outgoing' : 'incoming';
        let label = dir === 'outgoing' ? 'You \u2192' : '\u2190 Kairos';
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
    btn.disabled = false;
    btn.textContent = 'Send \u25b8';
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
}

// Initial load + periodic poll
poll();
loadChat();
setInterval(poll, {{INTERVAL_MS}});
setInterval(spawnParticle, 3000);
document.getElementById('chat-input').addEventListener('keydown', function(e) {
    if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); sendChat(); }
});
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
                html = _HTML.replace("{{NAME}}", "Kairos")
                html = html.replace("{{INTERVAL_MS}}", str(config.tick_interval_s * 1000))
                self._respond(200, "text/html; charset=utf-8", html)

            elif self.path == "/api/status":
                status = build_status(config)
                self._respond(200, "application/json", json.dumps(status))

            elif self.path == "/api/ping":
                ping = build_ping(config)
                self._respond(200, "application/json", json.dumps(ping))

            elif self.path == "/api/chat":
                chat = build_chat(config)
                self._respond(200, "application/json", json.dumps(chat))

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
            else:
                self._respond(404, "text/plain", "not found")

    return Handler


def main():
    parser = argparse.ArgumentParser(description="Kairos dashboard server")
    parser.add_argument("--config", default="config.toml", help="Path to config file")
    parser.add_argument("--port", type=int, default=None, help="Override dashboard port")
    args = parser.parse_args()

    config = load_config(args.config)
    port = args.port or config.dashboard_port

    handler = _make_handler(config)
    server = HTTPServer(("0.0.0.0", port), handler)
    print(f"[dashboard] Serving on http://0.0.0.0:{port}")
    print(f"[dashboard] Reading from {config.workspace}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[dashboard] Shutting down")
        server.shutdown()


if __name__ == "__main__":
    main()
