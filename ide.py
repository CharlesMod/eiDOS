"""EidosCodeIDE — a browser mini-IDE over the pi coding agent ("code with my AI buddy").

A standalone service (modeled on voice.py — own process / nssm service, never folded into
the dashboard, so an experimental IDE crash can't wound the watchdog). Each "stint" is a
persistent interactive `pi --mode rpc` process with its own working dir, chat, and resumable
session. The browser talks to it over HTTP + SSE.

D1 (this file) ships the backend spine + a minimal chat page: create/switch stints, send a
turn, watch pi stream its thinking + tool calls live. Code tree / tabs / viewer / repo-zip
download land in D2/D3.

RPC protocol (captured in runs/pi_rpc_capture.py): send {"type":"prompt","message":...} on
stdin; pi streams JSONL events (response/agent_start/turn_start/message_update{
assistantMessageEvent:text_delta|toolcall_*}/tool_execution_start/_end/message_end/turn_end/
agent_end) on stdout; closing stdin makes pi exit 0.

CORS-open like voice.py so the page (served here) and any tooling can reach it over localhost
or Tailscale. The IDE writes only under workspace/ide/; the Kairos repo is off-limits as a cwd.
"""

import argparse
import json
import logging
import os
import queue as _queue
import shutil
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs

logger = logging.getLogger("eidos.ide")

REPO_ROOT = Path(__file__).resolve().parent


def _resolve_pi(config) -> str:
    p = (getattr(config, "delegate_pi_path", "") or "").strip()
    if p and Path(p).exists():
        return p
    return shutil.which("pi") or ""


def _kill_tree(pid: int) -> None:
    if not pid:
        return
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                           capture_output=True, timeout=15)
        else:
            os.kill(pid, 9)
    except Exception:  # noqa: BLE001
        pass


class Stint:
    """One persistent pi rpc process + its transcript + live subscribers."""

    def __init__(self, sid: str, title: str, sdir: Path, work: Path, proc):
        self.sid = sid
        self.title = title
        self.sdir = sdir
        self.work = work
        self.proc = proc
        self.transcript = sdir / "transcript.jsonl"
        self.events: list = []
        self.subs: set = set()
        self.lock = threading.Lock()
        self.turn_active = False
        self.created = time.time()
        self.last_activity = time.time()
        self.status = "running"

    def publish(self, event: dict) -> None:
        self.last_activity = time.time()
        try:
            with open(self.transcript, "a", encoding="utf-8") as f:
                f.write(json.dumps(event) + "\n")
        except OSError:
            pass
        with self.lock:
            self.events.append(event)
            if len(self.events) > 4000:
                self.events = self.events[-4000:]
            subs = list(self.subs)
        for q in subs:
            try:
                q.put_nowait(event)
            except Exception:  # noqa: BLE001 — slow/closed client catches up via replay
                pass

    def subscribe(self):
        q = _queue.Queue(maxsize=512)
        with self.lock:
            snapshot = list(self.events)
            self.subs.add(q)
        return q, snapshot

    def unsubscribe(self, q) -> None:
        with self.lock:
            self.subs.discard(q)

    def meta(self) -> dict:
        return {"id": self.sid, "title": self.title, "status": self.status,
                "cwd": str(self.work), "created": self.created,
                "turn_active": self.turn_active}


class StintManager:
    def __init__(self, config):
        self.config = config
        self.stints: dict = {}
        self.lock = threading.Lock()
        self.root = config.workspace / "ide" / "stints"
        self.root.mkdir(parents=True, exist_ok=True)

    # --- lifecycle ---

    def _spawn(self, sdir: Path, work: Path):
        pi = _resolve_pi(self.config)
        if not pi:
            raise RuntimeError("pi is not installed / resolvable")
        argv = [pi, "--mode", "rpc",
                "--provider", getattr(self.config, "ide_pi_provider", "house-tap"),
                "--model", getattr(self.config, "ide_pi_model", "house-ai"),
                "--session-dir", str(sdir / "sessions"), "-a"]
        kw = {}
        if os.name == "nt":
            kw["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            kw["start_new_session"] = True
        return subprocess.Popen(
            argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, cwd=str(work), text=True,
            encoding="utf-8", errors="replace", bufsize=1,
            env={**os.environ, "PYTHONUTF8": "1"}, **kw)

    def create(self, title: str):
        with self.lock:
            live = sum(1 for s in self.stints.values() if s.status == "running")
            if live >= int(getattr(self.config, "ide_max_stints", 8)):
                return None, "too many live stints (close one first)"
        sid = "s%d" % int(time.time() * 1000)
        sdir = self.root / sid
        work = sdir / "work"
        work.mkdir(parents=True, exist_ok=True)
        (sdir / "sessions").mkdir(parents=True, exist_ok=True)
        try:
            proc = self._spawn(sdir, work)
        except Exception as exc:  # noqa: BLE001
            return None, str(exc)
        stint = Stint(sid, title or sid, sdir, work, proc)
        (sdir / "meta.json").write_text(json.dumps(
            {"id": sid, "title": stint.title, "created": stint.created}), encoding="utf-8")
        threading.Thread(target=self._reader, args=(stint,), daemon=True,
                         name=f"ide-reader-{sid}").start()
        with self.lock:
            self.stints[sid] = stint
        logger.info("stint %s created (pid %s)", sid, proc.pid)
        return stint, None

    def _reader(self, stint: Stint) -> None:
        """Own pi's stdout: parse each JSONL line into a live event."""
        try:
            for line in stint.proc.stdout:
                line = line.strip()
                if not line:
                    continue
                try:
                    ev = json.loads(line)
                except json.JSONDecodeError:
                    ev = {"type": "raw", "line": line[:2000]}
                if ev.get("type") == "agent_end":
                    stint.turn_active = False
                stint.publish(ev)
        except (OSError, ValueError):
            pass
        stint.status = "exited"
        stint.turn_active = False
        stint.publish({"type": "stint_exit"})

    def prompt(self, sid: str, message: str):
        stint = self.stints.get(sid)
        if not stint:
            return None, "no such stint"
        if stint.status != "running":
            return None, "stint has exited"
        if stint.turn_active:
            return None, "a turn is already in flight — wait for it to finish"
        stint.turn_active = True
        stint.publish({"type": "user_prompt", "message": message})
        try:
            stint.proc.stdin.write(json.dumps({"type": "prompt", "message": message}) + "\n")
            stint.proc.stdin.flush()
        except OSError as exc:
            stint.turn_active = False
            return None, str(exc)
        return True, None

    def close(self, sid: str):
        with self.lock:
            stint = self.stints.get(sid)
        if not stint:
            return
        try:
            stint.proc.stdin.close()       # closing stdin makes pi exit 0
        except OSError:
            pass
        try:
            stint.proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            _kill_tree(stint.proc.pid)
        stint.status = "closed"

    def reap_all(self) -> None:
        for sid in list(self.stints):
            self.close(sid)


MANAGER: StintManager = None   # set in main()


# --- minimal D1 chat page (D2 adds tree/tabs/viewer, D3 adds download) ---
_PAGE = r"""<!doctype html><html><head><meta charset="utf-8"><title>EidosCodeIDE</title>
<style>
 :root{--bg:#0a0a0a;--fg:#00ff41;--amber:#ffb000;--blue:#33bbff;--dim:#1a3a1a;--mut:#888}
 *{box-sizing:border-box} body{margin:0;background:var(--bg);color:var(--fg);
   font:14px 'Courier New',monospace;height:100vh;display:flex;flex-direction:column}
 header{padding:6px 12px;border-bottom:1px solid var(--dim);color:var(--amber);
   display:flex;gap:12px;align-items:center}
 header b{letter-spacing:2px} .grow{flex:1}
 main{flex:1;display:flex;min-height:0}
 #stints{width:200px;border-right:1px solid var(--dim);overflow:auto;padding:6px}
 .stint{padding:6px;cursor:pointer;border:1px solid transparent;border-radius:4px;
   white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
 .stint:hover{border-color:var(--dim)} .stint.active{background:var(--dim);color:var(--amber)}
 #chatwrap{flex:1;display:flex;flex-direction:column;min-width:0}
 #log{flex:1;overflow:auto;padding:10px;white-space:pre-wrap;line-height:1.4}
 .msg-user{color:var(--blue);margin:8px 0} .msg-pi{color:var(--fg);margin:8px 0}
 .tool{color:var(--amber);margin:4px 0 4px 8px;font-size:13px}
 .sys{color:var(--mut);font-size:12px;margin:4px 0}
 #composer{display:flex;border-top:1px solid var(--dim)}
 #inp{flex:1;background:#000;color:var(--fg);border:none;padding:10px;font:14px monospace;resize:none}
 button{background:var(--dim);color:var(--fg);border:1px solid var(--fg);padding:6px 12px;
   cursor:pointer;font:13px monospace} button:hover{background:var(--fg);color:#000}
 button:disabled{opacity:.4;cursor:default} input.ti{background:#000;color:var(--fg);
   border:1px solid var(--dim);padding:4px;font:13px monospace}
</style></head><body>
<header><b>&lt; EidosCodeIDE &gt;</b><span class="grow"></span>
 <input class="ti" id="newtitle" placeholder="new stint name"><button onclick="newStint()">+ stint</button>
</header>
<main>
 <div id="stints"></div>
 <div id="chatwrap">
   <div id="log"><div class="sys">pick or create a stint to start coding with pi.</div></div>
   <div id="composer">
     <textarea id="inp" rows="3" placeholder="describe what to build… (Ctrl+Enter to send)"></textarea>
     <button id="send" onclick="send()">send</button>
   </div>
 </div>
</main>
<script>
let cur=null, es=null, turnActive=false, curText=null;
const log=document.getElementById('log');
function add(cls,txt){const d=document.createElement('div');d.className=cls;d.textContent=txt;
  log.appendChild(d);log.scrollTop=log.scrollHeight;return d;}
async function listStints(){const r=await fetch('/api/stints');const j=await r.json();
  const box=document.getElementById('stints');box.innerHTML='';
  j.stints.forEach(s=>{const d=document.createElement('div');
    d.className='stint'+(s.id===cur?' active':'');d.textContent=(s.status==='running'?'● ':'○ ')+s.title;
    d.onclick=()=>open(s.id);box.appendChild(d);});}
async function newStint(){const t=document.getElementById('newtitle').value||'';
  const r=await fetch('/api/stints',{method:'POST',body:JSON.stringify({title:t})});
  const j=await r.json();if(j.id){document.getElementById('newtitle').value='';await listStints();open(j.id);}
  else add('sys','could not create stint: '+(j.error||'?'));}
function open(id){cur=id;curText=null;log.innerHTML='';turnActive=false;document.getElementById('send').disabled=false;
  listStints();if(es)es.close();es=new EventSource('/api/stints/'+id+'/events');
  es.onmessage=e=>handle(JSON.parse(e.data));}
function handle(ev){const t=ev.type;
  if(t==='user_prompt'){add('msg-user','you ▸ '+ev.message);curText=null;turnActive=true;document.getElementById('send').disabled=true;}
  else if(t==='message_update'&&ev.assistantMessageEvent){const a=ev.assistantMessageEvent;
    if(a.type==='text_delta'){if(!curText)curText=add('msg-pi','pi ▸ ');curText.textContent+=a.delta||'';log.scrollTop=log.scrollHeight;}}
  else if(t==='tool_execution_start'){const ar=ev.args||{};const d=ar.path||ar.command||ar.pattern||'';
    add('tool','⚙ '+ev.toolName+(d?' '+String(d).slice(0,80):''));curText=null;}
  else if(t==='tool_execution_end'){if(ev.isError)add('tool','  ✗ error');}
  else if(t==='agent_end'){turnActive=false;document.getElementById('send').disabled=false;curText=null;}
  else if(t==='stint_exit'){add('sys','— stint exited —');document.getElementById('send').disabled=true;}
}
async function send(){const inp=document.getElementById('inp');const m=inp.value.trim();
  if(!m||!cur||turnActive)return;inp.value='';
  const r=await fetch('/api/stints/'+cur+'/prompt',{method:'POST',body:JSON.stringify({message:m})});
  const j=await r.json();if(!j.ok)add('sys','✗ '+(j.error||'send failed'));}
document.getElementById('inp').addEventListener('keydown',e=>{
  if(e.key==='Enter'&&(e.ctrlKey||e.metaKey)){e.preventDefault();send();}});
listStints();
</script></body></html>"""


class IDEHandler(BaseHTTPRequestHandler):

    def log_message(self, *args):
        pass

    def _respond(self, code, ctype, body):
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

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def _body(self) -> dict:
        try:
            n = int(self.headers.get("Content-Length", 0) or 0)
            return json.loads(self.rfile.read(n) or b"{}") if n else {}
        except (ValueError, json.JSONDecodeError):
            return {}

    def _stint_id(self, prefix: str) -> str:
        # /api/stints/<id>/<verb>
        rest = urlparse(self.path).path[len(prefix):]
        return rest.split("/", 1)[0]

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/":
            self._respond(200, "text/html; charset=utf-8", _PAGE)
        elif path == "/api/health":
            self._respond(200, "application/json", json.dumps({"ok": True, "service": "ide"}))
        elif path == "/api/stints":
            stints = [s.meta() for s in MANAGER.stints.values()]
            stints.sort(key=lambda m: m["created"], reverse=True)
            self._respond(200, "application/json", json.dumps({"stints": stints}))
        elif path.startswith("/api/stints/") and path.endswith("/events"):
            self._sse(self._stint_id("/api/stints/"))
        else:
            self._respond(404, "text/plain", "not found")

    def _sse(self, sid: str):
        stint = MANAGER.stints.get(sid)
        if not stint:
            self._respond(404, "text/plain", "no such stint")
            return
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
        q, snapshot = stint.subscribe()
        try:
            for ev in snapshot:                       # replay transcript so far (no live replay needed)
                self.wfile.write(("data: " + json.dumps(ev) + "\n\n").encode("utf-8"))
            self.wfile.flush()
            while True:
                try:
                    ev = q.get(timeout=15)
                    self.wfile.write(("data: " + json.dumps(ev) + "\n\n").encode("utf-8"))
                except _queue.Empty:
                    self.wfile.write(b": ping\n\n")
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            stint.unsubscribe(q)

    def do_POST(self):
        path = urlparse(self.path).path
        if path == "/api/stints":
            stint, err = MANAGER.create(str(self._body().get("title") or "").strip())
            if stint:
                self._respond(200, "application/json", json.dumps({"id": stint.sid}))
            else:
                self._respond(200, "application/json", json.dumps({"error": err}))
        elif path.startswith("/api/stints/") and path.endswith("/prompt"):
            sid = self._stint_id("/api/stints/")
            ok, err = MANAGER.prompt(sid, str(self._body().get("message") or ""))
            self._respond(200, "application/json",
                          json.dumps({"ok": bool(ok), "error": err} if not ok else {"ok": True}))
        elif path.startswith("/api/stints/") and path.endswith("/close"):
            MANAGER.close(self._stint_id("/api/stints/"))
            self._respond(200, "application/json", json.dumps({"ok": True}))
        else:
            self._respond(404, "text/plain", "not found")


def main():
    global MANAGER
    parser = argparse.ArgumentParser(description="EidosCodeIDE — pi coding-agent GUI")
    parser.add_argument("--config", default="config.toml")
    parser.add_argument("--port", type=int, default=None)
    args = parser.parse_args()

    from config import load_config
    config = load_config(args.config)
    port = args.port or getattr(config, "ide_port", 8100)
    MANAGER = StintManager(config)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    server = ThreadingHTTPServer(("0.0.0.0", port), IDEHandler)
    server.daemon_threads = True
    print(f"[ide] EidosCodeIDE on http://0.0.0.0:{port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        MANAGER.reap_all()


if __name__ == "__main__":
    main()
