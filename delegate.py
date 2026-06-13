"""Delegate — hand a long-horizon task to the pi coding agent as a background job.

The tick loop is built for presence, not deep work: a multi-step investigation,
multi-file edit, or environment repair needs a worker that can hold ONE problem for
minutes. `delegate` spawns the pi coding agent (same house-ai mind, its own context
window, read/bash/edit/write tools) detached through the shared jobs ledger; the
result returns later as a compact [↩ delegate N] observation. ARCH #2: never block.

Trust stance matches the rest of the platform (accident-safety, not adversary-proof):
the Kairos repo is hard-denied as a working directory regardless of the allowlist,
and house rules ride along as a system-prompt addendum.
"""

import json
import os
import re
import shutil
import subprocess
import threading
import time
from pathlib import Path

from config import Config
from tools import (
    ToolResult,
    _job_waiter,
    _jobs_lock,
    _read_job_tail,
    _read_jobs,
    _write_jobs,
)

REPO_ROOT = Path(__file__).resolve().parent

HOUSE_RULES = """You are a delegated worker for eiDOS, the autonomous house AI on this machine.
Hard rules, in addition to the task you were given:
- NEVER create, modify, or delete anything under C:/Users/cmod/llm/Kairos except inside
  your own working directory (a sandbox under .../Kairos/workspace/delegate/).
- No `git push`. No stopping/restarting/installing services (nssm, Stop-Service,
  Restart-Service, taskkill against services).
- Software installs are USER-SCOPE ONLY: pip inside a venv in your cwd, `winget install
  --scope user`, or portable binaries dropped into your cwd. Never system-wide installs.
- This is Windows; prefer PowerShell-compatible commands and set PYTHONUTF8=1 for Python.
- End with a concise final summary: what you did, every file you changed, and anything
  you could not verify.
"""

_NAME_SANITIZE = re.compile(r"[^A-Za-z0-9_\-]+")
_RESEARCH_TOOLS = "read,grep,find,ls"
# Tool names whose events indicate a file was touched, and the arg keys that carry paths.
_FILE_TOOLS = ("write", "edit")
_PATH_KEYS = ("path", "file_path", "filePath", "filename")


def _delegate_root(config: Config) -> Path:
    return config.workspace / "delegate"


def _norm(p) -> str:
    return os.path.normcase(str(Path(p).resolve()))


def _under(child: str, parent: str) -> bool:
    return child == parent or child.startswith(parent.rstrip(os.sep) + os.sep)


# Known install location — under the nssm services (LocalSystem) shutil.which("pi")
# fails (no user PATH), so fall back to the absolute launcher.
_PI_FALLBACK = r"C:\Users\cmod\AppData\Local\pi-node\current\pi.cmd"

# eidos runs as LocalSystem (child of the EidosDashboard service, which — unlike the IDE
# service — does NOT export PI_CODING_AGENT_DIR). Without this, a delegated pi can't find
# cmod's `house` provider extension OR the @tintinweb/pi-subagents Agent tool, so it fails
# to reach the model / can't fan out subagents. Point pi at cmod's config + home explicitly.
_PI_ENV = {
    "PI_CODING_AGENT_DIR": r"C:\Users\cmod\.pi\agent",
    "USERPROFILE": r"C:\Users\cmod", "HOMEDRIVE": "C:", "HOMEPATH": r"\Users\cmod",
}


def _resolve_pi(config: Config) -> str:
    """Path to the pi launcher, or '' if unresolvable."""
    p = (getattr(config, "delegate_pi_path", "") or "").strip()
    if p:
        return p if Path(p).exists() else ""
    found = shutil.which("pi")
    if found:
        return found
    return _PI_FALLBACK if Path(_PI_FALLBACK).exists() else ""


def _cwd_denied(config: Config, cwd: Path) -> str:
    """'' if cwd is permitted, else the reason. The repo hard-deny beats the allowlist."""
    c = _norm(cwd)
    sandbox = _norm(_delegate_root(config))
    if _under(c, sandbox):
        return ""
    if _under(c, _norm(REPO_ROOT)):
        return ("the Kairos repo is off-limits to the delegate (only its own sandbox "
                "under workspace/delegate/) — pick a different cwd or omit it")
    for d in (getattr(config, "delegate_allowed_dirs", None) or []):
        try:
            if _under(c, _norm(d)):
                return ""
        except OSError:
            continue
    return "cwd is not under any allowed root (see config.toml [delegate] allowed_dirs)"


def _running_delegate(config: Config) -> dict | None:
    for j in _read_jobs(config):
        if j.get("kind") == "delegate" and j.get("status") == "running":
            return j
    return None


def _write_house_rules(config: Config) -> Path:
    root = _delegate_root(config)
    root.mkdir(parents=True, exist_ok=True)
    rules = root / "house_rules.md"
    if not rules.exists():
        rules.write_text(HOUSE_RULES, encoding="utf-8")
    return rules


def _prune_old_jobs(config: Config) -> None:
    """Keep the newest delegate_max_sessions job sandboxes; never a running job's."""
    try:
        root = _delegate_root(config)
        keep = int(getattr(config, "delegate_max_sessions", 12))
        running = {j.get("name", "") for j in _read_jobs(config)
                   if j.get("kind") == "delegate" and j.get("status") == "running"}
        # A continue-run "dlg_x-r2" still uses dlg_x's dir — protect the base name too.
        running |= {n.split("-r")[0] for n in running}
        dirs = sorted((d for d in root.iterdir() if d.is_dir()),
                      key=lambda d: d.stat().st_mtime, reverse=True)
        for d in dirs[keep:]:
            if d.name in running:
                continue
            shutil.rmtree(d, ignore_errors=True)
    except OSError:
        pass


def tool_delegate(args: dict, config: Config) -> ToolResult:
    """Validate, build the pi invocation, spawn detached, register in the jobs ledger."""
    start = time.monotonic()

    def fail(msg: str, kind: str) -> ToolResult:
        return ToolResult(output=msg, full_output_path=None, success=False,
                          duration_s=time.monotonic() - start, fail_kind=kind)

    # --- Gates: no side effects until every one passes (registry smoke tests dispatch
    # --- every tool with empty args, and a half-spawned job must be impossible).
    if not getattr(config, "delegate_enabled", False):
        return fail("delegate is disabled (config.toml [delegate] enabled=false). "
                    "Ask Boss to enable it.", "blocked")
    task = str(args.get("task") or "").strip()
    if not task:
        return fail('delegate needs {"task": "..."} — a SELF-CONTAINED brief: the goal, '
                    "constraints, and everything you already tried. The agent has none "
                    "of your context.", "args")
    mode = str(args.get("mode") or "research").strip().lower()
    if mode not in ("research", "code"):
        return fail('mode must be "research" (read-only investigation) or "code" '
                    "(can write files and run commands)", "args")
    pi_path = _resolve_pi(config)
    if not pi_path:
        return fail("the pi coding agent is not installed/resolvable — set [delegate] "
                    "pi_path in config.toml or ask Boss", "exec")
    running = _running_delegate(config)
    if running:
        return fail(f"a delegate is already working ([job {running.get('name')}], "
                    f"intent: {str(running.get('intent') or '')[:80]}). One at a time — "
                    f"its result arrives tagged [↩ delegate {running.get('name')}]; do "
                    "other work until then.", "blocked")

    # --- continue_job: follow-up turn in an existing session.
    cont = str(args.get("continue_job") or "").strip()
    prior_meta: dict = {}
    if cont:
        base = cont.split("-r")[0]
        job_dir = _delegate_root(config) / base
        meta_path = job_dir / "job.json"
        if not meta_path.exists():
            return fail(f"no delegate job '{cont}' to continue (its sandbox is gone — "
                        "it may have been pruned). Start a fresh delegate.", "args")
        try:
            prior_meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return fail(f"job '{cont}' metadata is unreadable; start a fresh delegate.",
                        "args")
        name = f"{base}-r{int(prior_meta.get('runs', 1)) + 1}"
        mode = prior_meta.get("mode", mode)
        cwd = Path(prior_meta["cwd"])
    else:
        raw = str(args.get("name") or "").strip() or f"d{int(time.time()) % 100000}"
        base = "dlg_" + _NAME_SANITIZE.sub("_", raw).strip("_")[:40]
        name = base
        job_dir = _delegate_root(config) / base
        cwd = Path(str(args.get("cwd"))) if args.get("cwd") else job_dir

    denied = _cwd_denied(config, cwd if cont or args.get("cwd") else job_dir)
    if denied:
        return fail(f"cwd refused: {denied}", "blocked")

    # --- Side effects begin: sandbox, task file, rules, spawn.
    try:
        job_dir.mkdir(parents=True, exist_ok=True)
        (job_dir / "sessions").mkdir(exist_ok=True)
        cwd.mkdir(parents=True, exist_ok=True)
        rules_path = _write_house_rules(config)
        task_path = job_dir / ("task.md" if not cont else f"task_{name}.md")
        task_path.write_text(task, encoding="utf-8")

        argv = [pi_path, "-p", "--mode", "json",
                "--provider", getattr(config, "delegate_pi_provider", "house"),
                "--model", getattr(config, "delegate_pi_model", "house-ai"),
                "--session-dir", str(job_dir / "sessions"),
                "-a", "--append-system-prompt", str(rules_path)]
        if cont:
            argv += ["--continue"]
        if mode == "research":
            argv += ["--tools", _RESEARCH_TOOLS]
        if _under(_norm(cwd), _norm(_delegate_root(config))):
            # Sandbox cwd sits inside the Kairos tree — don't let pi ingest Kairos's
            # CLAUDE.md/AGENTS.md (wrong audience). External repos keep their own.
            argv += ["--no-context-files"]
        argv += ["@" + str(task_path)]

        config.outputs_dir.mkdir(parents=True, exist_ok=True)
        out_path = config.outputs_dir / f"dlg_{name}.out"
        exit_path = str(out_path) + ".exit"
        popen_kwargs = {}
        if os.name == "nt":
            popen_kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            popen_kwargs["start_new_session"] = True
        out_file = open(out_path, "w", encoding="utf-8", errors="replace")
        try:
            proc = subprocess.Popen(
                argv,
                stdin=subprocess.DEVNULL,   # pi hangs forever on an open non-TTY stdin
                stdout=out_file,
                stderr=subprocess.STDOUT,
                cwd=str(cwd),
                env={**os.environ, "PYTHONUTF8": "1", **_PI_ENV},
                **popen_kwargs,
            )
        finally:
            try:
                out_file.close()
            except OSError:
                pass

        (job_dir / "job.json").write_text(json.dumps({
            "name": base, "mode": mode, "cwd": str(cwd),
            "runs": int(prior_meta.get("runs", 0)) + 1 if cont else 1,
            "created": prior_meta.get("created") or time.strftime(
                "%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }, indent=2), encoding="utf-8")

        with _jobs_lock:
            jobs = _read_jobs(config)
            jobs.append({
                "name": name,
                "pid": proc.pid,
                "cmd": subprocess.list2cmdline(argv)[:300],
                "intent": task[:120],
                "kind": "delegate",
                "mode": mode,
                "cwd": str(cwd),
                "job_dir": str(job_dir),
                "started": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "started_ts": time.time(),
                "status": "running",
                "output_path": str(out_path),
                "exit_path": exit_path,
                "notified": False,
                "waited": True,
            })
            _write_jobs(config, jobs)
        threading.Thread(target=_job_waiter, args=(config, proc, name, exit_path),
                         daemon=True, name=f"job-waiter-{name}").start()
        _prune_old_jobs(config)
    except OSError as exc:
        return fail(f"could not start the delegate: {exc}", "crash")

    timeout_s = int(float(getattr(config, "delegate_timeout_s", 600.0)))
    return ToolResult(
        output=(f"⟳ delegated [job {name} · {mode} mode] to your coding agent — it works "
                f"in the background for up to {timeout_s}s. You are NOT blocked; keep "
                f"doing other work. The result arrives tagged [↩ delegate {name}]. "
                f"Don't re-delegate this and don't sit waiting."),
        full_output_path=str(out_path),
        success=True,
        duration_s=time.monotonic() - start,
    )


# ---------------------------------------------------------------------------
# Result delivery


def _extract_from_events(out_path: str, max_bytes: int = 2_000_000) -> tuple[str, list[str]]:
    """Best-effort parse of pi's --mode json event stream: (final assistant text,
    files touched). Tolerant of schema drift — returns ('', []) when nothing parses."""
    try:
        raw = Path(out_path).read_bytes()
    except OSError:
        return "", []
    if len(raw) > max_bytes:
        raw = raw[-max_bytes:]
        raw = raw[raw.find(b"\n") + 1:]  # drop the partial first line
    last_text = ""
    files: list[str] = []

    def harvest_text(node) -> str:
        # pi message content is either a string or a list of {type:"text", text:...} parts.
        if isinstance(node, str):
            return node
        if isinstance(node, list):
            return "\n".join(p.get("text", "") for p in node
                             if isinstance(p, dict) and p.get("type") == "text").strip()
        return ""

    def walk(obj) -> None:
        nonlocal last_text
        if not isinstance(obj, dict):
            return
        role = obj.get("role")
        if role == "assistant":
            text = harvest_text(obj.get("content"))
            if text:
                last_text = text
        tool = obj.get("toolName") or obj.get("tool_name") or obj.get("name")
        if isinstance(tool, str) and tool.lower() in _FILE_TOOLS:
            a = obj.get("args") or obj.get("arguments") or obj.get("input") or {}
            if isinstance(a, dict):
                for k in _PATH_KEYS:
                    v = a.get(k)
                    if isinstance(v, str) and v and v not in files:
                        files.append(v)
        for v in obj.values():
            if isinstance(v, dict):
                walk(v)
            elif isinstance(v, list):
                for item in v:
                    walk(item)

    for line in raw.splitlines():
        line = line.strip()
        if not line.startswith(b"{"):
            continue
        try:
            walk(json.loads(line.decode("utf-8", errors="replace")))
        except (json.JSONDecodeError, UnicodeDecodeError):
            continue
    return last_text.strip(), files[:10]


def format_result_observation(config: Config, job: dict) -> tuple[str, bool]:
    """Compact observation text for a finished delegate job, and a success flag.
    Full output is preserved in the job sandbox; the observation carries a digest."""
    name = job.get("name", "?")
    mode = job.get("mode", "?")
    status = job.get("status", "?")
    elapsed = ""
    if job.get("started_ts"):
        elapsed = f" · {int(time.time() - float(job['started_ts']))}s"
    base = str(name).split("-r")[0]
    resume = (f'(follow up with delegate {{"continue_job":"{base}", "task":"..."}} — '
              f"the session is preserved)")

    if status == "timed_out":
        return (f"[↩ delegate {name} · {mode} · TIMED OUT{elapsed}] the watchdog killed "
                f"it at {int(float(getattr(config, 'delegate_timeout_s', 600.0)))}s. "
                f"Its partial work is on disk in {job.get('cwd', '?')}. {resume}"), False
    if status == "reaped":
        return (f"[↩ delegate {name} · {mode} · INTERRUPTED] a restart stopped it "
                f"mid-run; its session survived. {resume}"), False

    text, files = _extract_from_events(job.get("output_path", ""))
    job_dir = Path(job.get("job_dir") or (_delegate_root(config) / base))
    result_path = job_dir / "result.md"
    if text:
        try:
            job_dir.mkdir(parents=True, exist_ok=True)
            body = text
            if files:
                body += "\n\n## Files touched\n" + "\n".join(f"- {f}" for f in files)
            result_path.write_text(body, encoding="utf-8")
        except OSError:
            pass

    if status == "failed":
        tail = job.get("tail") or _read_job_tail(job.get("output_path", ""), 900)
        detail = text[:900] if text else tail[-900:]
        return (f"[↩ delegate {name} · {mode} · FAILED{elapsed} · "
                f"exit {job.get('exit_code')}] {detail} {resume}"), False

    if not text:
        tail = job.get("tail") or _read_job_tail(job.get("output_path", ""), 1200)
        return (f"[↩ delegate {name} · {mode} · OK{elapsed}] (its output was not "
                f"parseable as events — raw tail follows)\n{tail[-1200:]} {resume}"), True

    digest = text if len(text) <= 1200 else text[:1200] + "…"
    parts = [f"[↩ delegate {name} · {mode} · OK{elapsed}]", digest]
    if files:
        parts.append("files: " + ", ".join(files))
    parts.append(f"full: {result_path}")
    parts.append(resume)
    return "\n".join(parts)[:1500], True
