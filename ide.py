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
import io
import json
import logging
import mimetypes
import os
import queue as _queue
import shutil
import subprocess
import threading
import time
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs

# Dirs never shown in the tree or included in a repo zip (noise / huge / internal).
_TREE_SKIP = {".git", "node_modules", "__pycache__", ".venv", "venv",
              "sessions", ".pytest_cache", ".mypy_cache", "dist", "build"}


def _safe_path(work: Path, rel: str):
    """Resolve `rel` under the stint's work dir, or None if it escapes (sandbox)."""
    rel = (rel or "").lstrip("/\\")
    try:
        wr = work.resolve()
        p = (wr / rel).resolve()
        if p == wr or str(p).startswith(str(wr) + os.sep):
            return p
    except OSError:
        pass
    return None

logger = logging.getLogger("eidos.ide")

REPO_ROOT = Path(__file__).resolve().parent


# Known install location — the nssm services run as LocalSystem with no user PATH,
# so shutil.which("pi") fails there; fall back to the absolute launcher.
_PI_FALLBACK = r"C:\Users\cmod\AppData\Local\pi-node\current\pi.cmd"

# Some local models (Gemma) paste a whole file into chat instead of calling write —
# leaving work/ empty so there's nothing to preview or download. Insist on real files.
_PI_SYS = (
    "You are building software collaboratively in the current working directory. "
    "ALWAYS create and edit REAL files with the write/edit tools — never paste a full "
    "file's contents into chat as the deliverable. For a web app, write index.html (plus "
    "any css/js/assets) at the working-directory root so it can be previewed and downloaded. "
    "Keep chat replies short; let the files on disk be the work."
)

# Inline content types for the raw preview serve (mimetypes can be wrong/absent on Windows).
_RAW_MIME = {
    ".html": "text/html", ".htm": "text/html", ".js": "text/javascript",
    ".mjs": "text/javascript", ".css": "text/css", ".json": "application/json",
    ".svg": "image/svg+xml", ".wasm": "application/wasm", ".pdf": "application/pdf",
    ".glb": "model/gltf-binary", ".gltf": "model/gltf+json",
    ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
    ".gif": "image/gif", ".webp": "image/webp", ".ico": "image/x-icon",
}


def _resolve_pi(config) -> str:
    p = (getattr(config, "delegate_pi_path", "") or "").strip()
    if p and Path(p).exists():
        return p
    found = shutil.which("pi")
    if found:
        return found
    return _PI_FALLBACK if Path(_PI_FALLBACK).exists() else ""


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

    def _spawn(self, sdir: Path, work: Path, resume: bool = False):
        pi = _resolve_pi(self.config)
        if not pi:
            raise RuntimeError("pi is not installed / resolvable")
        argv = [pi, "--mode", "rpc",
                "--provider", getattr(self.config, "ide_pi_provider", "house-tap"),
                "--model", getattr(self.config, "ide_pi_model", "house-ai"),
                "--session-dir", str(sdir / "sessions"),
                "--append-system-prompt", _PI_SYS, "-a"]
        if resume:
            argv.append("--continue")    # resume the saved pi session context
        kw = {}
        if os.name == "nt":
            kw["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            kw["start_new_session"] = True
        proc = subprocess.Popen(
            argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, cwd=str(work), text=True,
            encoding="utf-8", errors="replace", bufsize=1,
            env={**os.environ, "PYTHONUTF8": "1"}, **kw)
        self._register_pid(proc.pid)
        return proc

    # --- pid ledger: survive a service restart without leaking detached pi ---

    def _pidfile(self) -> Path:
        return self.root.parent / "pids.json"

    def _register_pid(self, pid: int) -> None:
        try:
            pids = json.loads(self._pidfile().read_text()) if self._pidfile().exists() else []
        except (OSError, ValueError):
            pids = []
        pids.append(pid)
        try:
            self._pidfile().write_text(json.dumps(pids))
        except OSError:
            pass

    def reap_orphans(self) -> None:
        """Kill pi processes left detached by a previous (crashed) service run."""
        try:
            pids = json.loads(self._pidfile().read_text())
        except (OSError, ValueError):
            pids = []
        for pid in pids:
            _kill_tree(pid)
        try:
            self._pidfile().write_text("[]")
        except OSError:
            pass

    # --- cold stints: prior sessions on disk, resumable across restarts ---

    def load_cold(self) -> None:
        for d in sorted(p for p in self.root.glob("*") if p.is_dir()):
            sid = d.name
            if sid in self.stints or not (d / "meta.json").exists():
                continue
            try:
                meta = json.loads((d / "meta.json").read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            stint = Stint(sid, meta.get("title", sid), d, d / "work", None)
            stint.status = "cold"
            try:
                lines = (d / "transcript.jsonl").read_text(encoding="utf-8").splitlines()
                stint.events = [json.loads(x) for x in lines[-500:] if x.strip()]
            except (OSError, ValueError):
                pass
            self.stints[sid] = stint

    def resume(self, sid: str):
        stint = self.stints.get(sid)
        if not stint:
            return None, "no such stint"
        if stint.status == "running":
            return True, None
        with self.lock:
            live = sum(1 for s in self.stints.values() if s.status == "running")
        if live >= int(getattr(self.config, "ide_max_stints", 8)):
            return None, "too many live stints (close one first)"
        try:
            stint.proc = self._spawn(stint.sdir, stint.work, resume=True)
        except Exception as exc:  # noqa: BLE001
            return None, str(exc)
        stint.status = "running"
        stint.turn_active = False
        threading.Thread(target=self._reader, args=(stint,), daemon=True,
                         name=f"ide-reader-{sid}").start()
        stint.publish({"type": "stint_resumed"})
        return True, None

    def reap_idle(self) -> None:
        timeout = float(getattr(self.config, "ide_stint_idle_timeout_s", 1800.0))
        now = time.time()
        for s in list(self.stints.values()):
            if (s.status == "running" and not s.turn_active
                    and now - s.last_activity > timeout):
                logger.info("reaping idle stint %s", s.sid)
                self.close(s.sid)

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
        if stint.status == "running":     # a deliberate close already set it "cold"
            stint.status = "exited"
            stint.turn_active = False
            stint.publish({"type": "stint_exit"})

    def prompt(self, sid: str, message: str):
        stint = self.stints.get(sid)
        if not stint:
            return None, "no such stint"
        if stint.status == "cold":
            return None, "stint is paused — resume it first"
        if stint.status != "running" or stint.proc is None:
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
        if not stint or stint.proc is None:
            if stint:
                stint.status = "cold"
            return
        pid = stint.proc.pid
        try:
            stint.proc.stdin.close()       # closing stdin makes pi exit 0
        except OSError:
            pass
        try:
            stint.proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            _kill_tree(pid)
        self._unregister_pid(pid)
        stint.proc = None
        stint.status = "cold"              # resumable again, not gone

    def delete(self, sid: str):
        """Permanently remove a stint: stop pi, drop it from the registry, and delete its
        on-disk dir (sessions + transcript + work). Unlike close(), it does NOT come back."""
        self.close(sid)                      # stop the pi process if running
        with self.lock:
            stint = self.stints.pop(sid, None)
        if not stint:
            return False, "no such stint"
        try:
            if stint.sdir.exists():
                shutil.rmtree(stint.sdir, ignore_errors=True)
        except OSError as exc:
            return False, str(exc)
        return True, None

    def _unregister_pid(self, pid: int) -> None:
        try:
            pids = [p for p in json.loads(self._pidfile().read_text()) if p != pid]
            self._pidfile().write_text(json.dumps(pids))
        except (OSError, ValueError):
            pass

    def reap_all(self) -> None:
        for sid in list(self.stints):
            self.close(sid)

    # --- read-only code surfaces (sandboxed to the stint's work dir) ---

    def tree(self, sid: str, rel: str):
        stint = self.stints.get(sid)
        if not stint:
            return None
        base = _safe_path(stint.work, rel)
        if not base or not base.is_dir():
            return []
        out = []
        try:
            for e in sorted(base.iterdir(), key=lambda e: (e.is_file(), e.name.lower())):
                if e.name in _TREE_SKIP:
                    continue
                out.append({"name": e.name,
                            "path": str(e.relative_to(stint.work)).replace("\\", "/"),
                            "type": "dir" if e.is_dir() else "file"})
        except OSError:
            pass
        return out

    def read_file(self, sid: str, rel: str, cap: int = 262144):
        stint = self.stints.get(sid)
        if not stint:
            return None, "no such stint"
        p = _safe_path(stint.work, rel)
        if not p or not p.is_file():
            return None, "no such file"
        try:
            data = p.read_bytes()
        except OSError as exc:
            return None, str(exc)
        if b"\x00" in data[:4096]:
            return None, "binary file"
        return {"path": rel, "truncated": len(data) > cap,
                "content": data[:cap].decode("utf-8", errors="replace")}, None

    def zip_work(self, sid: str):
        stint = self.stints.get(sid)
        if not stint:
            return None, "no such stint"
        buf = io.BytesIO()
        try:
            with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
                for root, dirs, files in os.walk(stint.work):
                    dirs[:] = [d for d in dirs if d not in _TREE_SKIP]
                    for f in files:
                        fp = Path(root) / f
                        z.write(fp, fp.relative_to(stint.work))
        except OSError as exc:
            return None, str(exc)
        return buf.getvalue(), None


MANAGER: StintManager = None   # set in main()


# --- mini-IDE page: stints · chat · code (tree + tabs + viewer) + repo download ---
_PAGE = r"""<!doctype html><html><head><meta charset="utf-8"><title>EidosCodeIDE</title>
<style>
 :root{--bg:#0a0a0a;--fg:#00ff41;--amber:#ffb000;--blue:#33bbff;--dim:#1a3a1a;--mut:#888}
 *{box-sizing:border-box} body{margin:0;background:var(--bg);color:var(--fg);
   font:14px 'Courier New',monospace;height:100vh;display:flex;flex-direction:column}
 header{padding:6px 12px;border-bottom:1px solid var(--dim);color:var(--amber);
   display:flex;gap:10px;align-items:center}
 header b{letter-spacing:2px} .grow{flex:1}
 main{flex:1;display:flex;min-height:0}
 #stints{width:180px;border-right:1px solid var(--dim);overflow:auto;padding:6px}
 .stint{padding:6px;cursor:pointer;border:1px solid transparent;border-radius:4px;
   white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
 .stint:hover{border-color:var(--dim)} .stint.active{background:var(--dim);color:var(--amber)}
 #chatwrap{flex:1;display:flex;flex-direction:column;min-width:0;border-right:1px solid var(--dim)}
 #log{flex:1;overflow:auto;padding:10px;white-space:pre-wrap;line-height:1.4}
 .msg-user{color:var(--blue);margin:8px 0} .msg-pi{color:var(--fg);margin:8px 0}
 .tool{color:var(--amber);margin:4px 0 4px 8px;font-size:13px}
 .sys{color:var(--mut);font-size:12px;margin:4px 0}
 #composer{display:flex;border-top:1px solid var(--dim)}
 #inp{flex:1;background:#000;color:var(--fg);border:none;padding:10px;font:14px monospace;resize:none}
 #code{width:420px;display:flex;flex-direction:column;min-width:0}
 #tree{height:38%;overflow:auto;padding:6px;border-bottom:1px solid var(--dim);font-size:13px}
 .row{padding:1px 4px;cursor:pointer;white-space:nowrap} .row:hover{background:var(--dim)}
 .row.dir{color:var(--amber)} .row.file{color:var(--fg)}
 #tabs{display:flex;flex-wrap:wrap;gap:2px;padding:4px;border-bottom:1px solid var(--dim);min-height:28px}
 .tab{padding:2px 8px;cursor:pointer;border:1px solid var(--dim);font-size:12px;color:var(--mut)}
 .tab.active{color:var(--amber);border-color:var(--amber)}
 #viewer{flex:1;overflow:auto;padding:8px;white-space:pre;font-size:13px;tab-size:4}
 button{background:var(--dim);color:var(--fg);border:1px solid var(--fg);padding:6px 12px;
   cursor:pointer;font:13px monospace} button:hover{background:var(--fg);color:#000}
 button:disabled{opacity:.4;cursor:default} input.ti{background:#000;color:var(--fg);
   border:1px solid var(--dim);padding:4px;font:13px monospace;width:130px}
</style></head><body>
<header><b>&lt; EidosCodeIDE &gt;</b><span id="curname" class="mut"></span><span class="grow"></span>
 <button id="dl" onclick="dl()" disabled>download .zip</button>
 <input class="ti" id="newtitle" placeholder="new stint"><button onclick="newStint()">+ stint</button>
</header>
<main>
 <div id="stints"></div>
 <div id="chatwrap">
   <div id="log"><div class="sys">pick or create a stint to start coding with pi.</div></div>
   <div id="composer">
     <textarea id="inp" rows="3" placeholder="describe what to build… (Enter to send · Shift+Enter for newline)"></textarea>
     <button id="send" onclick="send()" disabled>send</button>
   </div>
 </div>
 <div id="code">
   <div id="tree"></div><div id="tabs"></div><pre id="viewer"></pre>
 </div>
</main>
<script>
let cur=null, es=null, turnActive=false, curText=null, pendWrite=null;
let tabs=[], active=null;
const log=document.getElementById('log');
const $=id=>document.getElementById(id);
function add(cls,txt){const d=document.createElement('div');d.className=cls;d.textContent=txt;
  log.appendChild(d);log.scrollTop=log.scrollHeight;return d;}
async function listStints(){const j=await (await fetch('/api/stints')).json();
  const box=$('stints');box.innerHTML='';
  j.stints.forEach(s=>{const d=document.createElement('div');
    d.className='stint'+(s.id===cur?' active':'');d.textContent=(s.status==='running'?'● ':'○ ')+s.title;
    d.onclick=()=>open(s.id,s.title,s.status);box.appendChild(d);});}
async function newStint(){const t=$('newtitle').value||'';
  const j=await (await fetch('/api/stints',{method:'POST',body:JSON.stringify({title:t})})).json();
  if(j.id){$('newtitle').value='';await listStints();open(j.id,t||j.id);}
  else add('sys','could not create stint: '+(j.error||'?'));}
async function open(id,title,status){cur=id;curText=null;log.innerHTML='';turnActive=false;
  tabs=[];active=null;renderTabs();$('viewer').textContent='';
  $('send').disabled=false;$('dl').disabled=false;$('curname').textContent='· '+(title||id);
  if(status&&status!=='running'){add('sys','resuming…');await fetch('/api/stints/'+id+'/resume',{method:'POST'});}
  listStints();loadTree();if(es)es.close();
  es=new EventSource('/api/stints/'+id+'/events');es.onmessage=e=>handle(JSON.parse(e.data));}
function handle(ev){const t=ev.type;
  if(t==='user_prompt'){add('msg-user','you ▸ '+ev.message);curText=null;turnActive=true;$('send').disabled=true;}
  else if(t==='message_update'&&ev.assistantMessageEvent){const a=ev.assistantMessageEvent;
    if(a.type==='text_delta'){if(!curText)curText=add('msg-pi','pi ▸ ');curText.textContent+=a.delta||'';log.scrollTop=log.scrollHeight;}}
  else if(t==='tool_execution_start'){const ar=ev.args||{};const d=ar.path||ar.command||ar.pattern||'';
    add('tool','⚙ '+ev.toolName+(d?' '+String(d).slice(0,80):''));curText=null;
    if((ev.toolName==='write'||ev.toolName==='edit')&&ar.path)pendWrite=ar.path;}
  else if(t==='tool_execution_end'){if(ev.isError)add('tool','  ✗ error');
    else if(pendWrite){loadTree();openFile(pendWrite);pendWrite=null;}}
  else if(t==='agent_end'){turnActive=false;$('send').disabled=false;curText=null;}
  else if(t==='stint_exit'){add('sys','— stint exited —');$('send').disabled=true;}
}
async function send(){const m=$('inp').value.trim();if(!m||!cur||turnActive)return;$('inp').value='';
  const j=await (await fetch('/api/stints/'+cur+'/prompt',{method:'POST',body:JSON.stringify({message:m})})).json();
  if(!j.ok)add('sys','✗ '+(j.error||'send failed'));}
$('inp').addEventListener('keydown',e=>{if(e.key==='Enter'&&!e.shiftKey&&!e.isComposing){e.preventDefault();send();}});
// --- code tree ---
async function loadTree(){if(!cur){$('tree').innerHTML='';return;}
  $('tree').innerHTML='';await renderDir($('tree'),'');}
async function renderDir(parent,path){
  const j=await (await fetch('/api/stints/'+cur+'/tree?path='+encodeURIComponent(path))).json();
  (j.items||[]).forEach(it=>{const r=document.createElement('div');r.className='row '+it.type;
    r.textContent=(it.type==='dir'?'▸ ':'  ')+it.name;parent.appendChild(r);
    if(it.type==='dir'){let open=false,kids=null;
      r.onclick=async()=>{open=!open;if(open){r.textContent='▾ '+it.name;
        kids=document.createElement('div');kids.style.marginLeft='12px';parent.insertBefore(kids,r.nextSibling);
        await renderDir(kids,it.path);}else{r.textContent='▸ '+it.name;if(kids)kids.remove();}};}
    else r.onclick=()=>openFile(it.path);});}
async function openFile(path){
  const j=await (await fetch('/api/stints/'+cur+'/file?path='+encodeURIComponent(path))).json();
  if(j.error){add('sys','('+path+': '+j.error+')');return;}
  const ex=tabs.find(t=>t.path===path);const body=j.content+(j.truncated?'\n\n… [truncated]':'');
  if(ex)ex.body=body;else tabs.push({path:path,body:body});active=path;renderTabs();renderViewer();}
function renderTabs(){const box=$('tabs');box.innerHTML='';
  tabs.forEach(t=>{const d=document.createElement('span');d.className='tab'+(t.path===active?' active':'');
    d.textContent=t.path.split('/').pop()+' ✕';
    d.onclick=ev=>{if(ev.offsetX>d.offsetWidth-16){tabs=tabs.filter(x=>x!==t);
      if(active===t.path)active=tabs.length?tabs[tabs.length-1].path:null;}
      else active=t.path;renderTabs();renderViewer();};box.appendChild(d);});}
function renderViewer(){const t=tabs.find(t=>t.path===active);$('viewer').textContent=t?t.body:'';}
function dl(){if(!cur)return;const a=document.createElement('a');
  a.href='/api/stints/'+cur+'/download?zip=1';a.download=cur+'.zip';
  document.body.appendChild(a);a.click();a.remove();}
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
        elif path.startswith("/api/stints/") and path.endswith("/tree"):
            rel = (parse_qs(urlparse(self.path).query).get("path") or [""])[0]
            items = MANAGER.tree(self._stint_id("/api/stints/"), rel)
            self._respond(200, "application/json", json.dumps({"items": items or []}))
        elif path.startswith("/api/stints/") and path.endswith("/file"):
            rel = (parse_qs(urlparse(self.path).query).get("path") or [""])[0]
            res, err = MANAGER.read_file(self._stint_id("/api/stints/"), rel)
            self._respond(200, "application/json", json.dumps(res or {"error": err}))
        elif path.startswith("/api/stints/") and path.endswith("/download"):
            self._download(self._stint_id("/api/stints/"),
                           parse_qs(urlparse(self.path).query))
        elif path.startswith("/api/stints/") and "/raw/" in path:
            # /api/stints/<id>/raw/<relpath> — serve a work file INLINE (not as an
            # attachment) so the preview iframe/img can render it, with relative assets.
            after = path[len("/api/stints/"):]
            sid, _, rel = after.partition("/raw/")
            self._serve_raw(sid, rel)
        else:
            self._respond(404, "text/plain", "not found")

    def _serve_raw(self, sid: str, rel: str):
        from urllib.parse import unquote
        stint = MANAGER.stints.get(sid)
        if not stint:
            self._respond(404, "text/plain", "no such stint"); return
        p = _safe_path(stint.work, unquote(rel or ""))
        if not p or not p.is_file():
            self._respond(404, "text/plain", "not found"); return
        ctype = _RAW_MIME.get(p.suffix.lower()) \
            or mimetypes.guess_type(p.name)[0] or "application/octet-stream"
        try:
            data = p.read_bytes()
        except OSError:
            self._respond(404, "text/plain", "unreadable"); return
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        try:
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass

    def _download(self, sid: str, q: dict):
        if (q.get("zip") or ["0"])[0] in ("1", "true"):
            data, err = MANAGER.zip_work(sid)
            if data is None:
                self._respond(404, "text/plain", err or "not found")
                return
            self._respond_file(200, "application/zip", data, f"{sid}.zip")
            return
        rel = (q.get("path") or [""])[0]
        res, err = MANAGER.read_file(sid, rel, cap=10_000_000)
        if not res:
            self._respond(404, "text/plain", err or "not found")
            return
        name = rel.rsplit("/", 1)[-1] or "file.txt"
        self._respond_file(200, "application/octet-stream",
                           res["content"].encode("utf-8"), name)

    def _respond_file(self, code, ctype, data, filename):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        try:
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass

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
        elif path.startswith("/api/stints/") and path.endswith("/resume"):
            ok, err = MANAGER.resume(self._stint_id("/api/stints/"))
            self._respond(200, "application/json",
                          json.dumps({"ok": True} if ok else {"ok": False, "error": err}))
        elif path.startswith("/api/stints/") and path.endswith("/delete"):
            ok, err = MANAGER.delete(self._stint_id("/api/stints/"))
            self._respond(200, "application/json",
                          json.dumps({"ok": True} if ok else {"ok": False, "error": err}))
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
    MANAGER.reap_orphans()      # kill pi left detached by a prior (crashed) run
    MANAGER.load_cold()         # surface prior stints as resumable

    def _idle_loop():
        while True:
            time.sleep(60)
            try:
                MANAGER.reap_idle()
            except Exception:  # noqa: BLE001
                logger.exception("idle reaper")
    threading.Thread(target=_idle_loop, daemon=True, name="ide-idle-reaper").start()

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
