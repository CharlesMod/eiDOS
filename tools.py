"""Tool registry and implementations."""

import dataclasses
import json
import os
import re
import subprocess
import threading
import time
from pathlib import Path
from typing import Callable, Optional

from config import Config
from parser import ToolCall
from safety import is_command_blocked, check_disk_space


@dataclasses.dataclass
class ToolResult:
    """Outcome of one tool call.

    fail_kind — the failure taxonomy (BIBLE section 5: type every failure so it can be
    aggregated and drive recovery playbooks instead of living as prose). Empty on
    success. One of: args (bad/missing arguments), blocked (safety/linter refused),
    timeout (watchdog/ceiling), network, exec (nonzero exit), parse, llm, crash
    (tool raised), no_such_tool, error (untyped failure — the backstop default).
    execute_tool guarantees fail_kind is non-empty whenever success is False.
    """
    output: str
    full_output_path: Optional[str]
    success: bool
    duration_s: float
    fail_kind: str = ""


def _save_full_output(config: Config, tick_id: str, stream: str, content: str) -> str:
    """Save full output to disk, return the path."""
    config.outputs_dir.mkdir(parents=True, exist_ok=True)
    path = config.outputs_dir / f"{tick_id}_{stream}.txt"
    path.write_text(content)
    return str(path)


def _truncate(text: str, limit: int, full_path: Optional[str]) -> str:
    """Truncate text to limit chars, appending pointer to full output."""
    if len(text) <= limit:
        return text
    suffix = f"\n[truncated at {limit} chars"
    if full_path:
        suffix += f", full output: {full_path}"
    suffix += "]"
    return text[:limit] + suffix


def _normalize_workspace_path(path: str, config: Config) -> Path:
    """Resolve a file path against the workspace, stripping redundant workspace prefix.

    The model often emits "workspace/foo.md" because the prompt says
    'Working directory: workspace/'. That creates workspace/workspace/foo.md.
    Strip the leading workspace dir name to avoid double-nesting.
    """
    p = Path(path)
    if not p.is_absolute():
        # Strip leading workspace dir name (e.g. "workspace/foo" -> "foo")
        ws_name = Path(config.workspace_dir).name
        parts = p.parts
        if parts and parts[0] == ws_name:
            p = Path(*parts[1:]) if len(parts) > 1 else Path(".")
        p = Path(config.workspace_dir) / p
    return p



def _kill_pid_tree(pid: int) -> None:
    """Kill a process tree by PID (Windows-safe).

    On Windows, killing only the immediate process leaves grandchildren holding
    the output pipe open, so communicate() hangs forever. taskkill /T kills the tree.
    """
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


def _kill_proc_tree(proc):
    """Kill a Popen's process tree, with a direct proc.kill() fallback."""
    if proc is None or proc.poll() is not None:
        return
    _kill_pid_tree(proc.pid)
    if proc.poll() is None:
        try:
            proc.kill()
        except Exception:  # noqa: BLE001
            pass


_PROC_SEQ = 0


def proc_seq() -> int:
    """Monotonic-ish counter so same-second foreground output files don't collide."""
    global _PROC_SEQ
    _PROC_SEQ = (_PROC_SEQ + 1) % 100000
    return _PROC_SEQ


# Match `powershell [flags] -Command/-c <script>` so we can unwrap and run <script> via the
# LIST form (one argv straight to powershell.exe), never through cmd.exe (which mangles the
# nested quotes / $ / colons in the model's PowerShell — the cause of its parse errors).
_PS_UNWRAP_RE = re.compile(r"^\s*(?:powershell|pwsh)(?:\.exe)?\b.*?\s-(?:c|command)\b\s+(.*)$",
                           re.IGNORECASE | re.DOTALL)
_PS_LIST = ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command"]


# --- Pre-flight linter (quote-aware; NEVER lies, never walls) -------------
# Catches a few Windows/PowerShell mistakes the local model makes. Contract, learned the hard way
# (a buggy version false-blocked VALID commands for hours): it must be QUOTE-AWARE so it cannot
# misfire on a `$_` that sits between two separate single-quoted literals, and it must AUTO-CORRECT
# the genuine case and RUN — never hand back a dead "NOT RUN" wall the model just bounces off.
#   Return: None (clean) | ("block", msg) for the truly unfixable | ("rewrite", new_cmd, note).
_RE_BASH_FOR = re.compile(r"\bfor\s+\w+\s+in\b.*?;\s*do\b", re.IGNORECASE | re.DOTALL)
_RE_BASH_DONE = re.compile(r";\s*done\b|;\s*fi\b", re.IGNORECASE)  # require ';' so we never hit the WORD "done"
_RE_INTERP_IN_SPAN = re.compile(r"\$_|\$\{|\$[A-Za-z][A-Za-z0-9_]*")  # $_ / ${name} / $name


def _single_quoted_spans(cmd: str):
    """Yield (start, end) index pairs of single-quoted spans, respecting double-quoted regions
    (a `'` inside a double-quoted string is literal, and vice-versa). A simple 3-state scanner —
    this is what makes the linter quote-aware and immune to the cross-boundary false positive."""
    spans = []
    state = "none"  # none | single | double
    start = -1
    for i, ch in enumerate(cmd):
        if state == "none":
            if ch == "'":
                state, start = "single", i
            elif ch == '"':
                state = "double"
        elif state == "single":
            if ch == "'":
                spans.append((start, i)); state = "none"
        elif state == "double":
            if ch == '"':
                state = "none"
    return spans


def _lint_windows_command(cmd: str):
    """Quote-aware pre-flight. See contract above."""
    c = cmd.strip()
    low = c.lower()
    if low.startswith(("cmd ", "cmd.exe", "cmd/")):
        return None  # operator explicitly chose cmd.exe — don't second-guess
    # 1) Unix bash loop syntax on Windows (observed: `for i in {40..50}; do ...; done`). Unfixable
    #    automatically (whole structure differs) — block WITH the PowerShell equivalent.
    if _RE_BASH_FOR.search(c) or _RE_BASH_DONE.search(c):
        return ("block",
                "That's Unix bash syntax (`for … do … done` / `fi`) — you're in PowerShell, not bash. "
                "Use a range pipeline instead, e.g.  40..50 | ForEach-Object { $ip = \"192.168.86.$_\"; "
                "... }")
    # 2) `powershell -Command "...nested double-quotes..."` — the nesting is the parse-breaker. We
    #    can fix it deterministically: run the inner script directly (you're already in PowerShell).
    m = _PS_UNWRAP_RE.match(c)
    if m:
        inner = m.group(1).strip()
        if len(inner) >= 2 and inner[0] == inner[-1] and inner[0] in "\"'":
            inner = inner[1:-1]
        if '"' in inner:
            return ("rewrite", inner,
                    "unwrapped your `powershell -Command \"...\"` and ran the inner script directly "
                    "(you're already in PowerShell; the nested quotes would have broken the parse)")
    # 3) A $var/$_ GENUINELY inside a single-quoted span won't expand. Quote-aware: only spans that
    #    are truly single-quoted count (not a $_ between two separate literals). Auto-correct that one
    #    span to double quotes and run — don't wall the model.
    for (s, e) in _single_quoted_spans(c):
        inner = c[s + 1:e]
        if ('"' not in inner) and (("$_" in inner) or ("${" in inner)) and _RE_INTERP_IN_SPAN.search(inner):
            fixed = c[:s] + '"' + inner + '"' + c[e + 1:]
            return ("rewrite", fixed,
                    "changed a single-quoted '…' to \"…\" so the $variable expands (single quotes are "
                    "literal in PowerShell)")
    return None


def _route_windows_command(cmd: str):
    """(popen_arg, use_shell) for a Windows command. Always prefer list-form PowerShell so
    cmd.exe never re-parses (and corrupts) the model's PowerShell."""
    cl = cmd.lstrip()
    low = cl.lower()
    if low.startswith(("cmd ", "cmd.exe", "cmd/")):
        return cmd, True  # operator explicitly wants cmd.exe — honor it
    if low.startswith(("powershell", "pwsh")):
        m = _PS_UNWRAP_RE.match(cl)
        if m:
            script = m.group(1).strip()
            if len(script) >= 2 and script[0] == script[-1] and script[0] in ("\"", "'"):
                script = script[1:-1]   # drop the one layer of quotes wrapping the -Command arg
            return _PS_LIST + [script], False
        return cmd, True  # e.g. `powershell -File x.ps1` — run as given
    return _PS_LIST + [cmd], False      # bare command -> run as PowerShell directly


def tool_bash(args: dict, config: Config) -> ToolResult:
    """Run a shell command; fast commands return inline, slow ones auto-background."""
    cmd = args.get("cmd", "") or args.get("command", "")
    if not cmd:
        return ToolResult(output="Error: no 'cmd' argument provided", full_output_path=None, success=False, duration_s=0, fail_kind="args")

    # Safety check
    blocked = is_command_blocked(cmd, config.protected_patterns)
    if blocked:
        return ToolResult(
            output=f"BLOCKED: command matches protected pattern '{blocked}'",
            full_output_path=None,
            success=False,
            duration_s=0,
            fail_kind="blocked",
        )

    # Disk space check for write-like commands
    write_indicators = ["wget", "curl -o", "git clone", "pip install", "apt install", "apt-get install", "cp ", "mv "]
    if any(indicator in cmd.lower() for indicator in write_indicators):
        disk_ok, free_gb = check_disk_space(min_gb=config.disk_min_gb)
        if not disk_ok:
            return ToolResult(
                output=f"BLOCKED: disk space low ({free_gb:.1f} GB free, minimum {config.disk_min_gb} GB)",
                full_output_path=None,
                success=False,
                duration_s=0,
                fail_kind="blocked",
            )

    # Pre-flight (quote-aware, never lies). Auto-correct the fixable cases and RUN; only hard-block
    # the truly unfixable ones (with the PowerShell equivalent). A prepended note tells the model
    # what was changed so it learns — never a dead-end "NOT RUN" wall it just bounces off.
    _lint_note = ""
    if os.name == "nt":
        verdict = _lint_windows_command(cmd)
        if verdict:
            if verdict[0] == "block":
                return ToolResult(
                    output=f"NOT RUN — this can't run as written:\n{verdict[1]}",
                    full_output_path=None, success=False, duration_s=0, fail_kind="blocked",
                )
            if verdict[0] == "rewrite":
                cmd = verdict[1]
                _lint_note = f"[auto-fixed: {verdict[2]}]\n"

    start = time.monotonic()
    proc = None
    try:
        popen_kwargs = {}
        if os.name == "nt":
            popen_kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            popen_kwargs["start_new_session"] = True
        # Windows: PowerShell IS the shell. The model writes PowerShell; we run it via the list
        # form so cmd.exe never re-parses it. Even when the model wraps it in `powershell -Command
        # "..."`, we unwrap the inner script and run THAT via list form — no cmd.exe quote-hell.
        if os.name == "nt":
            popen_arg, use_shell = _route_windows_command(cmd)
        else:
            popen_arg, use_shell = cmd, True
        # Stream output to a file (not an in-memory PIPE) so a slow command can be
        # handed to the background ledger mid-run WITHOUT losing its output or killing it.
        config.outputs_dir.mkdir(parents=True, exist_ok=True)
        out_path = config.outputs_dir / f"fg_{time.strftime('%Y%m%d_%H%M%S')}_{proc_seq()}.out"
        exit_path = str(out_path) + ".exit"
        # List-form PowerShell scripts get an exit-code epilogue: it records the real
        # exit code to a sidecar file (readable after the Popen handle is gone — async
        # jobs) and re-raises it via `exit` so proc.returncode stays truthful (sync).
        # Commands that exit the script explicitly skip the epilogue: sidecar absent =
        # exit code unknown (old behavior), never wrong.
        if not use_shell and isinstance(popen_arg, list):
            _ep = exit_path.replace("'", "''")
            popen_arg = popen_arg[:-1] + [
                popen_arg[-1]
                + "\n$__eidos_ec = if ($LASTEXITCODE -ne $null) { $LASTEXITCODE } elseif ($?) { 0 } else { 1 }"
                + "\n[System.IO.File]::WriteAllText('" + "{}".format(_ep) + "', [string]$__eidos_ec)"
                + "\nexit $__eidos_ec"
            ]
        out_file = open(out_path, "w", encoding="utf-8", errors="replace")
        try:
            proc = subprocess.Popen(
                popen_arg,
                shell=use_shell,
                stdout=out_file,
                stderr=subprocess.STDOUT,
                text=True,
                cwd=str(config.workspace),
                **popen_kwargs,
            )
            # ASYNC BY DEFAULT (fire-and-forget). The command runs in the background and
            # its result is delivered to the LLM later, tagged [↩ job N], via the jobs
            # ledger. The loop is NEVER blocked. The model can pass "wait": true to force
            # the synchronous path below when it truly needs the result this same tick.
            if not bool(args.get("wait", False)):
                jobname = (str(args.get("name") or "").strip() or f"j{proc.pid}")
                intent = str(args.get("intent") or "").strip()
                try:
                    jobs = _read_jobs(config)
                    jobs.append({
                        "name": jobname,
                        "pid": proc.pid,
                        "cmd": cmd,
                        "intent": intent,
                        "started": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                        "started_ts": time.time(),
                        "status": "running",
                        "kind": "async",
                        "output_path": str(out_path),
                        "exit_path": exit_path,
                        "notified": False,
                        "waited": True,
                    })
                    with _jobs_lock:
                        _write_jobs(config, jobs)
                    threading.Thread(target=_job_waiter, args=(config, proc, jobname, exit_path),
                                     daemon=True, name=f"job-waiter-{jobname}").start()
                except Exception:  # noqa: BLE001
                    pass
                intent_note = f" (intent: {intent})" if intent else ""
                return ToolResult(
                    output=(_lint_note + f"⟳ dispatched [job {jobname}]: {cmd[:80]}{intent_note} — running in the "
                            f"background. You are NOT blocked; keep thinking and doing other work. "
                            f"The result comes back later tagged [↩ job {jobname}] — watch for it, "
                            f"don't re-run this, and don't sit waiting. If you genuinely must have "
                            f"the result THIS tick, re-issue the command with \"wait\": true."),
                    full_output_path=str(out_path),
                    success=True,
                    duration_s=time.monotonic() - start,
                )
            try:
                proc.wait(timeout=config.cmd_timeout_s)
            except subprocess.TimeoutExpired:
                # wait:true sync-escape that overran the soft window — still auto-background
                # rather than block: the process keeps running and writing to out_path.
                jobname = f"auto_{proc.pid}"
                try:
                    jobs = _read_jobs(config)
                    jobs.append({
                        "name": jobname,
                        "pid": proc.pid,
                        "cmd": cmd,
                        "started": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                        "started_ts": time.time(),
                        "status": "running",
                        "kind": "auto",
                        "output_path": str(out_path),
                        "exit_path": exit_path,
                        "notified": False,
                        "waited": True,
                    })
                    with _jobs_lock:
                        _write_jobs(config, jobs)
                    threading.Thread(target=_job_waiter, args=(config, proc, jobname, exit_path),
                                     daemon=True, name=f"job-waiter-{jobname}").start()
                except Exception:  # noqa: BLE001
                    pass
                return ToolResult(
                    output=(f"AUTO-BACKGROUNDED after {config.cmd_timeout_s}s: still running, so it "
                            f"was moved to the background as job '{jobname}' (PID {proc.pid}). The "
                            f"loop is NOT blocked — go do other work now; the result will arrive "
                            f"tagged [↩ job {jobname}], or check it with bg_check {{\"name\":\"{jobname}\"}}."),
                    full_output_path=str(out_path),
                    success=True,
                    duration_s=time.monotonic() - start,
                )
        finally:
            try:
                out_file.close()
            except Exception:  # noqa: BLE001
                pass

        duration = time.monotonic() - start
        try:
            combined = out_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            combined = ""

        tick_id = time.strftime("%Y%m%d_%H%M%S")
        full_path = None
        if len(combined) > config.output_truncation_chars:
            full_path = _save_full_output(config, tick_id, "bash", combined)

        output = _truncate(combined, config.output_truncation_chars, full_path)

        return ToolResult(
            output=output,
            full_output_path=full_path,
            success=(proc.returncode == 0),
            duration_s=duration,
            fail_kind="" if proc.returncode == 0 else "exec",
        )

    except Exception as e:  # noqa: BLE001
        if proc is not None:
            try:
                _kill_proc_tree(proc)
            except Exception:  # noqa: BLE001
                pass
        return ToolResult(
            output=f"bash error: {type(e).__name__}: {e}",
            full_output_path=None,
            success=False,
            duration_s=time.monotonic() - start,
            fail_kind="crash",
        )


def tool_write_file(args: dict, config: Config) -> ToolResult:
    """Write content to a file."""
    path = args.get("path", "")
    content = args.get("content", "")
    if not path:
        return ToolResult(output="Error: no 'path' argument", full_output_path=None, success=False, duration_s=0)

    disk_ok, free_gb = check_disk_space(min_gb=config.disk_min_gb)
    if not disk_ok:
        return ToolResult(
            output=f"BLOCKED: disk space low ({free_gb:.1f} GB free, minimum {config.disk_min_gb} GB)",
            full_output_path=None, success=False, duration_s=0,
        )

    start = time.monotonic()
    try:
        p = _normalize_workspace_path(path, config)
        # Prevent path traversal outside workspace
        resolved = p.resolve()
        workspace_resolved = Path(config.workspace_dir).resolve()
        if not str(resolved).startswith(str(workspace_resolved) + os.sep) and resolved != workspace_resolved:
            return ToolResult(output="Error: path escapes workspace directory", full_output_path=None, success=False, duration_s=time.monotonic() - start)
        # Anti-brick guard: managed files must go through their own tools, not raw write_file.
        _bn = resolved.name.lower()
        if _bn in ("self_guide.md", "self_guide_proposed.md"):
            return ToolResult(output="Error: change your self-guide with update_self_guide (it stages a proposal for Dean to approve), not write_file.",
                              full_output_path=None, success=False, duration_s=time.monotonic() - start)
        try:
            if resolved.parent in (config.state_dir.resolve(), config.proposals_dir.resolve()):
                return ToolResult(output="Error: that directory is managed by the dashboard and is not writable via write_file.",
                                  full_output_path=None, success=False, duration_s=time.monotonic() - start)
        except Exception:  # noqa: BLE001
            pass
        resolved.parent.mkdir(parents=True, exist_ok=True)
        resolved.write_text(content)
        duration = time.monotonic() - start
        return ToolResult(output=f"Written {len(content)} chars to {path}", full_output_path=None, success=True, duration_s=duration)
    except OSError as e:
        return ToolResult(output=f"Error writing file: {e}", full_output_path=None, success=False, duration_s=time.monotonic() - start)


def tool_read_file(args: dict, config: Config) -> ToolResult:
    """Read a file's contents."""
    path = args.get("path", "")
    if not path:
        return ToolResult(output="Error: no 'path' argument", full_output_path=None, success=False, duration_s=0)

    start = time.monotonic()
    try:
        p = _normalize_workspace_path(path, config)
        # Prevent path traversal outside workspace
        resolved = p.resolve()
        workspace_resolved = Path(config.workspace_dir).resolve()
        if not str(resolved).startswith(str(workspace_resolved) + os.sep) and resolved != workspace_resolved:
            return ToolResult(output="Error: path escapes workspace directory", full_output_path=None, success=False, duration_s=time.monotonic() - start)
        content = resolved.read_text()
        tick_id = time.strftime("%Y%m%d_%H%M%S")
        full_path = None
        if len(content) > config.output_truncation_chars:
            full_path = _save_full_output(config, tick_id, "read", content)
        output = _truncate(content, config.output_truncation_chars, full_path)
        return ToolResult(output=output, full_output_path=full_path, success=True, duration_s=time.monotonic() - start)
    except OSError as e:
        return ToolResult(output=f"Error reading file: {e}", full_output_path=None, success=False, duration_s=time.monotonic() - start)


def tool_bg_run(args: dict, config: Config) -> ToolResult:
    """Spawn a background job and register it in the jobs ledger."""
    cmd = args.get("cmd", "")
    name = args.get("name", "")
    if not cmd or not name:
        return ToolResult(output="Error: 'cmd' and 'name' required", full_output_path=None, success=False, duration_s=0)

    # Safety check
    blocked = is_command_blocked(cmd, config.protected_patterns)
    if blocked:
        return ToolResult(output=f"BLOCKED: {blocked}", full_output_path=None, success=False, duration_s=0)

    # Reject obvious unbounded loops — they orphan and run forever (a `while($true)` cert-poller
    # once ran all night). Run a single check now, or a BOUNDED loop, and re-check later yourself.
    _low = cmd.lower().replace(" ", "")
    if any(p in _low for p in ("while($true)", "while(true)", "while($true){", "while(1)",
                               "whiletrue:", "for(;;)", "while:1", "while1:")):
        return ToolResult(output=("BLOCKED: that command loops forever and would orphan a runaway "
                                  "background process. Don't poll in an infinite loop — run a SINGLE "
                                  "check now (or a bounded loop with a fixed retry count) and re-check "
                                  "later on a future tick or with bg_check."),
                          full_output_path=None, success=False, duration_s=0)

    start = time.monotonic()
    config.outputs_dir.mkdir(parents=True, exist_ok=True)
    out_path = config.outputs_dir / f"bg_{name}.out"

    try:
        popen_kwargs = {}
        if os.name == "nt":
            popen_kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            popen_kwargs["start_new_session"] = True
        with open(out_path, "w") as out_file:
            proc = subprocess.Popen(
                cmd,
                shell=True,
                stdout=out_file,
                stderr=subprocess.STDOUT,
                cwd=str(config.workspace),
                **popen_kwargs,
            )

        # Register in jobs ledger
        jobs = _read_jobs(config)
        jobs.append({
            "name": name,
            "pid": proc.pid,
            "cmd": cmd,
            "started": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "started_ts": time.time(),
            "status": "running",
            "kind": "manual",
            "output_path": str(out_path),
            "notified": False,
        })
        _write_jobs(config, jobs)

        return ToolResult(
            output=f"Started background job '{name}' (PID {proc.pid}), output: {out_path}",
            full_output_path=None,
            success=True,
            duration_s=time.monotonic() - start,
        )
    except OSError as e:
        return ToolResult(output=f"Error starting job: {e}", full_output_path=None, success=False, duration_s=time.monotonic() - start)


def tool_bg_check(args: dict, config: Config) -> ToolResult:
    """Check status of a registered background job."""
    name = args.get("name", "")
    if not name:
        return ToolResult(output="Error: 'name' required", full_output_path=None, success=False, duration_s=0)

    jobs = _read_jobs(config)
    job = None
    for j in jobs:
        if j["name"] == name:
            job = j
            break

    if not job:
        return ToolResult(output=f"No job named '{name}' found", full_output_path=None, success=False, duration_s=0)

    # Check if still running (cross-platform)
    pid = job.get("pid", 0)
    if job["status"] == "running" and not _pid_alive(pid):
        job["status"] = "completed"
        _write_jobs(config, jobs)

    # Read tail of output (and cap output file if oversized)
    output_path = job.get("output_path", "")
    tail = ""
    if output_path:
        try:
            p = Path(output_path)
            size = p.stat().st_size
            if size > config.bg_output_max_bytes:
                # Truncate to last bg_output_max_bytes
                with open(p, "rb") as f:
                    f.seek(-config.bg_output_max_bytes, 2)
                    kept = f.read()
                with open(p, "wb") as f:
                    f.write(b"[truncated]\n")
                    f.write(kept)
            content = p.read_text(errors="replace")
            tail = content[-1000:] if len(content) > 1000 else content
        except OSError:
            tail = "(output file not readable)"

    return ToolResult(
        output=f"Job '{name}' (PID {pid}): {job['status']}\n--- last output ---\n{tail}",
        full_output_path=output_path if output_path else None,
        success=True,
        duration_s=0,
    )



def tool_update_plan(args: dict, config: Config) -> ToolResult:
    """Write an urgent note to plan.md (briefing model working memory)."""
    from memory import read_plan, write_plan

    note = args.get("note", "")
    if not note:
        return ToolResult(output="Error: 'note' required", full_output_path=None, success=False, duration_s=0)

    disk_ok, free_gb = check_disk_space(min_gb=config.disk_min_gb)
    if not disk_ok:
        return ToolResult(
            output=f"BLOCKED: disk space low ({free_gb:.1f} GB free, minimum {config.disk_min_gb} GB)",
            full_output_path=None, success=False, duration_s=0,
        )

    current = read_plan(config)
    timestamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    addition = f"\n\n[Updated at {timestamp}]\n{note}"
    updated = current + addition

    # Hard cap: trim oldest lines from top to stay within plan budget
    budget = config.context_plan_max_chars
    if len(updated) > budget:
        lines = updated.splitlines(keepends=True)
        while lines and len("".join(lines)) > budget:
            lines.pop(0)
        updated = "".join(lines)

    write_plan(config, updated)
    return ToolResult(output=f"Plan updated: {note[:100]}", full_output_path=None, success=True, duration_s=0)


def tool_update_self_guide(args: dict, config: Config) -> ToolResult:
    """PROPOSE a change to your self-guide (Dean's standing directives doc).

    Stages a proposal only — Dean reviews and applies it from the dashboard. You can never
    edit the live guide directly. Pass either 'content' (the full new guide) or 'note' (one
    line to add), plus an optional 'rationale'.
    """
    from memory import read_self_guide, read_self_guide_proposed, write_self_guide_proposal
    content = args.get("content", "")
    note = args.get("note", "") or args.get("text", "")
    rationale = args.get("rationale", "") or args.get("reason", "")
    if not content and not note:
        return ToolResult(output="Error: provide 'content' (full new self-guide) or 'note' (a line to add).",
                          full_output_path=None, success=False, duration_s=0)
    if content:
        proposed = content
    else:
        base = read_self_guide_proposed(config) or read_self_guide(config)
        proposed = (base + "\n" + note).strip() if base else note.strip()
    proposed = proposed[: config.self_guide_max_bytes]
    if proposed.strip() in (read_self_guide_proposed(config).strip(), read_self_guide(config).strip()):
        return ToolResult(output="That self-guide change is already staged or already live — nothing new to propose.",
                          full_output_path=None, success=True, duration_s=0)
    try:
        write_self_guide_proposal(config, proposed, rationale=rationale, tick=args.get("source_tick"))
    except OSError as e:
        return ToolResult(output=f"Error staging self-guide proposal: {e}", full_output_path=None, success=False, duration_s=0)
    return ToolResult(output=("Proposed a self-guide update for Dean to review and apply in the dashboard. "
                              "It is NOT live until he approves it."),
                      full_output_path=None, success=True, duration_s=0)


def tool_propose_self_edit(args: dict, config: Config) -> ToolResult:
    """PROPOSE an edit to your OWN source code. Stages a proposal; Dean reviews the diff and
    the dashboard applies + restarts you. You can never edit source, commit, or restart yourself.

    Args: target_file (repo-relative .py, e.g. "prompts.py"), new_content (the FULL new file),
    rationale (why). Off-limits files (dashboard, config, safety, skills) are rejected.
    """
    import selfedit
    target = args.get("target_file", "") or args.get("path", "") or args.get("file", "")
    new_content = args.get("new_content", "") or args.get("content", "")
    rationale = args.get("rationale", "") or args.get("reason", "")
    r = selfedit.propose(config, target, new_content, rationale=rationale, tick=args.get("source_tick"))
    if r.get("ok"):
        return ToolResult(output=r.get("summary", f"Proposed self-edit {r.get('id')}."),
                          full_output_path=None, success=True, duration_s=0)
    return ToolResult(output=f"Self-edit proposal rejected: {r.get('error')}",
                      full_output_path=None, success=False, duration_s=0)


def tool_list_self_edits(args: dict, config: Config) -> ToolResult:
    """List your pending/recent self-edit proposals and their status."""
    import selfedit
    props = selfedit.list_proposals(config, kind="self_edit")
    if not props:
        return ToolResult(output="No self-edit proposals yet.", full_output_path=None, success=True, duration_s=0)
    lines = [f"- {m['id']} [{m['status']}] {m['target']} (+{m.get('added',0)}/-{m.get('removed',0)}) "
             f"{m.get('rationale','')[:60]}" for m in props[:12]]
    return ToolResult(output="Your self-edit proposals:\n" + "\n".join(lines),
                      full_output_path=None, success=True, duration_s=0)


def tool_check_messages(args: dict, config: Config) -> ToolResult:
    """Inspect your conversation with Boss — his messages to you and every message YOU'VE sent
    him — so you never repeat an ask he hasn't answered. Read-only (does not consume anything).
    """
    import json as _json
    parts = []
    # Boss's messages (peek the interventions dir WITHOUT consuming them)
    idir = config.interventions_dir
    if idir.exists():
        files = sorted([p for p in idir.iterdir() if not p.name.startswith(".")],
                       key=lambda p: p.name)
        for p in files[-10:]:
            tag = "Boss → you" + (" (already handled)" if p.suffix == ".done" else " (PENDING)")
            try:
                parts.append(f"[{tag}] {p.read_text(encoding='utf-8', errors='replace')[:300]}")
            except OSError:
                pass
    # Your sent replies
    rp = config.workspace / "chat_replies.jsonl"
    if rp.exists():
        try:
            lines = [_json.loads(l) for l in rp.read_text(encoding="utf-8").splitlines() if l.strip()]
            for r in lines[-12:]:
                parts.append(f"[you → Boss @ t{r.get('tick')}] {(r.get('text') or '')[:300]}")
        except (OSError, ValueError):
            pass
    if not parts:
        return ToolResult(output="No messages yet — you haven't talked with Boss.",
                          full_output_path=None, success=True, duration_s=0)
    return ToolResult(output=("Your conversation with Boss (use this before messaging him — don't "
                              "repeat an unanswered ask):\n" + "\n".join(parts)),
                      full_output_path=None, success=True, duration_s=0)


def tool_check_system(args: dict, config: Config) -> ToolResult:
    """Show your architecture — the authoritative map of what the platform ALREADY provides
    (chat, memory, skills, the loop, background work, self-improvement, the house/services) so you
    operate it instead of rebuilding it. Read this BEFORE building any subsystem.
    """
    try:
        from skills import list_skills as _ls
        nsk = len(_ls(config).get("skills", {}))
    except Exception:  # noqa: BLE001
        nsk = 0
    header = (f"You have {len(TOOLS)} built-in tools and {nsk} authored skills "
              f"(call check_tools for the full list).\n\n")
    doc_path = Path(__file__).resolve().parent / "eidos_capabilities.md"
    try:
        doc = doc_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        doc = ("(architecture doc missing) — core rule: the platform already provides chat, memory "
               "(memorize/recall), skills, the loop, background jobs, and self-improvement. Operate "
               "them via your tools; build house automation, not your own plumbing.")
    return ToolResult(output=header + doc, full_output_path=None, success=True, duration_s=0)


def tool_memorize(args: dict, config: Config) -> ToolResult:
    """Store a durable knowledge entry in the long-term knowledge store."""
    from knowledge import store_entry

    # Accept common model hallucinations: "value"/"content"/"knowledge" as "fact",
    # and "key" as a fallback tag source.
    fact = args.get("fact", "") or args.get("value", "") or args.get("content", "") or args.get("knowledge", "")
    if not fact:
        return ToolResult(output="Error: 'fact' required", full_output_path=None, success=False, duration_s=0)

    disk_ok, free_gb = check_disk_space(min_gb=config.disk_min_gb)
    if not disk_ok:
        return ToolResult(
            output=f"BLOCKED: disk space low ({free_gb:.1f} GB free, minimum {config.disk_min_gb} GB)",
            full_output_path=None, success=False, duration_s=0,
        )

    tags = args.get("tags", [])
    if isinstance(tags, str):
        tags = [t.strip() for t in tags.split(",") if t.strip()]
    # Fall back: use "key" as a single tag if no tags provided
    if not tags:
        key = args.get("key", "")
        if key:
            tags = [key]
    if not tags:
        tags = ["general"]

    category = args.get("category", "facts")
    confidence = args.get("confidence", "tentative")
    source_goal = args.get("source_goal", "")
    source_tick = args.get("source_tick", 0)

    try:
        entry_id = store_entry(
            config, content=fact, tags=tags, category=category,
            confidence=confidence, source_goal=source_goal,
            source_tick=source_tick,
        )
        return ToolResult(
            output=f"Stored to long-term memory: {entry_id}",
            full_output_path=None, success=True, duration_s=0,
        )
    except Exception as e:
        return ToolResult(
            output=f"Error storing knowledge: {e}",
            full_output_path=None, success=False, duration_s=0,
        )


def tool_recall(args: dict, config: Config) -> ToolResult:
    """Search the long-term knowledge store."""
    from knowledge import search_bm25, format_recalled

    query = args.get("query", "")
    if not query:
        return ToolResult(output="Error: 'query' required", full_output_path=None, success=False, duration_s=0)

    try:
        results = search_bm25(config, query, top_k=5)
        if not results:
            return ToolResult(
                output="No relevant knowledge found.",
                full_output_path=None, success=True, duration_s=0,
            )
        text = format_recalled(results, max_chars=config.knowledge_recall_max_chars)
        return ToolResult(
            output=text, full_output_path=None, success=True, duration_s=0,
        )
    except Exception as e:
        return ToolResult(
            output=f"Error recalling knowledge: {e}",
            full_output_path=None, success=False, duration_s=0,
        )




def _pid_alive(pid: int) -> bool:
    """Cross-platform check whether a process id is currently running."""
    if not pid or pid <= 0:
        return False
    if os.name == "nt":
        try:
            out = subprocess.run(
                ["tasklist", "/FI", f"PID eq {pid}", "/NH"],
                capture_output=True, text=True, timeout=10,
            )
            return str(pid) in (out.stdout or "")
        except Exception:
            return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


# In-process ledger lock (phase 4c): the tick thread and per-job waiter threads both
# read-modify-write jobs.json. (The dashboard's cross-process reap-on-stop remains
# unsynchronized — the deferred cross-process lock; see V2_SUPERVISED_HANDOFF.)
_jobs_lock = threading.Lock()


def _job_waiter(config: Config, proc, jobname: str, exit_path: str) -> None:
    """Phase 4c (ARCH #1): hold the Popen handle and wait() — job completion becomes an
    event, not something a per-tick tasklist poll discovers late. Writes the exit-code
    sidecar from the REAL returncode (authoritative for every route, including cmd.exe
    and explicit-exit scripts the PS epilogue can't cover), then flips the ledger entry.
    The PS epilogue remains as cross-restart recovery for jobs whose waiter died with us."""
    try:
        rc = proc.wait()
    except Exception:  # noqa: BLE001
        rc = None
    if rc is not None:
        try:
            Path(exit_path).write_text(str(rc), encoding="ascii")
        except OSError:
            pass
    try:
        with _jobs_lock:
            jobs = _read_jobs(config)
            for j in jobs:
                if j.get("name") == jobname and j.get("status") == "running":
                    j["exit_code"] = rc
                    j["status"] = "completed" if (rc is None or rc == 0) else "failed"
                    break
            _write_jobs(config, jobs)
    except Exception:  # noqa: BLE001 - ledger update is best-effort; collect re-derives
        pass


def _read_jobs(config: Config) -> list[dict]:
    try:
        p = config.jobs_path
        if p.stat().st_size > 5_000_000:   # corrupt/runaway — never OOM the tick loop
            return []
        data = json.loads(p.read_text())
        return data if isinstance(data, list) else []
    except (FileNotFoundError, json.JSONDecodeError, OSError, ValueError, MemoryError):
        return []


def _write_jobs(config: Config, jobs: list[dict]) -> None:
    config.workspace.mkdir(parents=True, exist_ok=True)
    config.jobs_path.write_text(json.dumps(jobs, indent=2))


_JOB_DONE = ("completed", "timed_out", "reaped")


def _prune_jobs(jobs: list[dict], keep_delivered: int = 15) -> list[dict]:
    """Keep every still-live or undelivered job; cap already-delivered (notified) finished jobs to
    the most-recent N so jobs.json can't grow without bound across a long run."""
    live = [j for j in jobs if not (j.get("notified") and j.get("status") in _JOB_DONE)]
    done = [j for j in jobs if (j.get("notified") and j.get("status") in _JOB_DONE)]
    done.sort(key=lambda j: j.get("started_ts") or 0)
    return live + done[-keep_delivered:]


def _finish_dead_job(job: dict) -> None:
    """Mark a no-longer-running job finished, consulting its exit-code sidecar.

    Sidecar present: 0 = completed, nonzero = failed (+exit_code recorded).
    Sidecar absent (explicit `exit` in the script, cmd.exe route, pre-v2 jobs):
    exit code unknown -> completed, the pre-typed-failure behavior.
    """
    ec = None
    try:
        raw = Path(job.get("exit_path") or "").read_text(encoding="ascii", errors="ignore").strip()
        if raw:
            ec = int(raw)
    except (OSError, ValueError):
        ec = None
    job["exit_code"] = ec
    job["status"] = "completed" if (ec is None or ec == 0) else "failed"


def refresh_jobs(config: Config) -> list[dict]:
    """Check all tracked jobs, update statuses, return current list."""
    with _jobs_lock:
        jobs = _read_jobs(config)
        changed = False
        for job in jobs:
            if job["status"] != "running":
                continue
            if job.get("waited"):
                continue  # a waiter thread owns this one's completion (phase 4c) — no pid poll
            if not _pid_alive(job.get("pid", 0)):
                _finish_dead_job(job)
                changed = True
        if changed:
            _write_jobs(config, jobs)
        return jobs


def reap_jobs(config: Config, kill_all: bool = False) -> int:
    """Kill orphaned background jobs and update the ledger; return count killed.

    kill_all=True kills EVERY still-running tracked job — use on eidos startup/stop to clear a
    previous run's detached children (bg_run/async use a new process group, so they survive a
    taskkill of eidos itself, and would otherwise run forever). Also marks dead pids completed.
    """
    with _jobs_lock:
        jobs = _read_jobs(config)
        killed = 0
        changed = False
        for j in jobs:
            if j.get("status") != "running":
                continue
            pid = j.get("pid", 0)
            if not j.get("waited") and not _pid_alive(pid):
                # No waiter owns it (pre-restart orphan) and the pid is gone -> close it out.
                _finish_dead_job(j)
                changed = True
                continue
            if kill_all:
                _kill_pid_tree(pid)
                j["status"] = "reaped"
                killed += 1
                changed = True
        if changed:
            _write_jobs(config, jobs)
        return killed


def _read_job_tail(output_path: str, n: int = 2500) -> str:
    if not output_path:
        return ""
    try:
        txt = Path(output_path).read_text(encoding="utf-8", errors="replace")
        return txt[-n:] if len(txt) > n else txt
    except OSError:
        return ""


def collect_finished_jobs(config: Config) -> list[dict]:
    """For the async delivery loop: refresh statuses, reap jobs past the ceiling, and
    return finished jobs not yet reported to the LLM (marking them reported).

    Each returned dict adds a 'tail' (last chunk of output) and a normalized 'status'
    in {completed, timed_out}. Only async/auto jobs are auto-delivered; manual bg_run
    jobs are left for the model to bg_check itself (it asked for them explicitly).
    """
    ceiling = float(getattr(config, "cmd_async_ceiling_s", 180.0))
    with _jobs_lock:
        jobs = _read_jobs(config)
        changed = False
        finished = []
        now = time.time()
        for j in jobs:
            if j.get("status") == "running":
                started_ts = j.get("started_ts")
                manual_cap = float(getattr(config, "bg_job_max_age_s", 1800.0))
                over_ceiling = (j.get("kind") in ("async", "auto")
                                and started_ts and (now - started_ts) > ceiling)
                over_manual = (started_ts and manual_cap > 0 and (now - started_ts) > manual_cap)
                if over_ceiling or over_manual:
                    # Lifetime caps apply to every route (a kill on an already-dead pid is a
                    # no-op; the waiter sees status != running afterwards and stands down).
                    _kill_pid_tree(j.get("pid", 0))
                    j["status"] = "timed_out"
                    changed = True
                elif j.get("waited"):
                    continue  # a waiter thread owns completion (phase 4c) — no tasklist poll
                elif _pid_alive(j.get("pid", 0)):
                    continue  # legacy/orphan path: still running
                else:
                    _finish_dead_job(j)
                    changed = True
            # Deliver finished async/auto jobs exactly once.
            if (j.get("kind") in ("async", "auto")
                    and j.get("status") in ("completed", "failed", "timed_out")
                    and not j.get("notified")):
                finished.append({**j, "tail": _read_job_tail(j.get("output_path", ""))})
                j["notified"] = True
                changed = True
        pruned = _prune_jobs(jobs)
        if changed or len(pruned) != len(jobs):
            _write_jobs(config, pruned)
        return finished


def tool_create_skill(args: dict, config: Config) -> ToolResult:
    """Author a NEW reusable tool (skill) that becomes callable immediately."""
    import skills
    name = (args.get("skill_name") or args.get("name") or "").strip()
    code = args.get("skill_code") or args.get("code") or ""
    description = args.get("description", "")
    schema = args.get("args_schema", {})
    if not name or not code:
        return ToolResult(
            output=("Error: provide 'skill_name' and 'skill_code'. The code MUST define:\n"
                    "def tool_<skill_name>(args: dict, config: Config) -> ToolResult"),
            full_output_path=None, success=False, duration_s=0)
    t = time.monotonic()
    r = skills.create_skill(config, name, code, description, schema if isinstance(schema, dict) else {})
    if r.get("success"):
        msg = r.get("message", f"Skill '{name}' created and live.")
        nudge = ""
        # Encourage MODULAR, parameterized, composable skills — not hardcoded one-offs.
        if re.search(r"\d+\.\d+\.\d+\.\d+", code) and "args.get(" not in code.replace(" ", ""):
            nudge += ("\n⚠ This skill hardcodes an IP/port. Make it REUSABLE: take them as args — "
                      "`ip = args.get('ip')`, `port = args.get('port')` — so one skill works for any host.")
        if re.search(r"socket\.|TcpClient|create_connection|urllib|http", code) and "import" in code:
            nudge += ("\n➤ Prefer COMPOSING the built-in primitives (net_scan, tcp_probe, http_probe, "
                      "udp_listen) over re-writing raw socket/HTTP code — they're parameterized and tested.")
        # Timeout hygiene: a skill that does network/socket/subprocess I/O with NO explicit timeout can
        # wedge the tick (tick 342 froze the loop ~6.7 min). The execute_tool watchdog is the hard
        # backstop; this nudge gets the skill fixed at the source so it never trips the watchdog.
        if re.search(r"requests\.(get|post|put|delete|head|patch|request)\s*\(|\.urlopen\s*\(|"
                     r"create_connection\s*\(|subprocess\.(run|call|check_output|Popen)\s*\(|"
                     r"\.connect\s*\(", code) and "timeout" not in code:
            nudge += ("\n⏱ This skill makes a network/socket/subprocess call with NO timeout — that can "
                      "freeze the whole tick loop if the peer stalls (it has, for ~6.7 min). Pass an "
                      "explicit timeout to EVERY such call (e.g. requests.get(url, timeout=5), "
                      "socket.create_connection(addr, timeout=5), subprocess.run(..., timeout=10)). "
                      "A skill that overruns ~30s is killed by the watchdog and returns a failure.")
        return ToolResult(
            output=(f"{msg}\n➤ You now HAVE a reusable tool '{name}'. CALL it as a TOOL — "
                    f"<tool>{name}</tool> with <args>{{...}}</args> — NOT via bash, and don't re-derive "
                    f"the steps. Skills are invoked exactly like built-in tools." + nudge),
            full_output_path=None, success=True, duration_s=time.monotonic() - t)
    return ToolResult(output="create_skill failed:\n- " + "\n- ".join(r.get("errors", ["unknown"])),
                      full_output_path=None, success=False, duration_s=time.monotonic() - t)


def tool_edit_skill(args: dict, config: Config) -> ToolResult:
    """Replace an existing skill's code with an improved version (old kept for rollback)."""
    import skills
    name = (args.get("skill_name") or args.get("name") or "").strip()
    code = args.get("skill_code") or args.get("code") or ""
    description = args.get("description")
    schema = args.get("args_schema")
    if not name or not code:
        return ToolResult(output="Error: provide 'skill_name' and 'skill_code'.",
                          full_output_path=None, success=False, duration_s=0)
    t = time.monotonic()
    r = skills.edit_skill(config, name, code, description, schema)
    if r.get("success"):
        return ToolResult(output=r.get("message", f"Skill '{name}' updated."),
                          full_output_path=None, success=True, duration_s=time.monotonic() - t)
    return ToolResult(output="edit_skill failed:\n- " + "\n- ".join(r.get("errors", ["unknown"])),
                      full_output_path=None, success=False, duration_s=time.monotonic() - t)


def tool_rollback_skill(args: dict, config: Config) -> ToolResult:
    """Revert a skill to a previously-saved version. Args: skill_name, version."""
    import skills
    name = (args.get("skill_name") or args.get("name") or "").strip()
    version = (args.get("version") or "").strip()
    if not name or not version:
        return ToolResult(output="Error: provide 'skill_name' and 'version'.",
                          full_output_path=None, success=False, duration_s=0)
    r = skills.rollback_skill(config, name, version)
    if r.get("success"):
        return ToolResult(output=r.get("message", "rolled back"),
                          full_output_path=None, success=True, duration_s=0)
    return ToolResult(output="rollback_skill failed:\n- " + "\n- ".join(r.get("errors", ["unknown"])),
                      full_output_path=None, success=False, duration_s=0)


def tool_list_skills(args: dict, config: Config) -> ToolResult:
    """List all authored skills (and built-in tools)."""
    import skills
    r = skills.list_skills(config)
    lines = ["BUILT-IN TOOLS: " + ", ".join(r["builtin_tools"])]
    if r["skills"]:
        lines.append("\nYOUR SKILLS:")
        for name, info in sorted(r["skills"].items()):
            rate = info["success_rate"]
            rate_s = f", {int(rate * 100)}% ok over {info['uses']}" if rate is not None else ""
            lines.append(f"  - {name} v{info['version']} [{info['status']}]{rate_s} "
                         f"— {info['description'][:80]}")
    else:
        lines.append("\nYOUR SKILLS: (none yet — author one with create_skill)")
    return ToolResult(output="\n".join(lines), full_output_path=None, success=True, duration_s=0)


# --- Notebooks (third memory tier: working notes for the current task) ---

def tool_note_append(args: dict, config: Config) -> ToolResult:
    """Append to a named working notebook (creates it, makes it active). Use this for LOTS of notes
    about the current task/environment — NOT memorize (durable facts) and NOT your own JSON files."""
    import notes
    name = (args.get("name") or args.get("notebook") or "scratch").strip()
    text = args.get("text") or args.get("note") or args.get("content") or ""
    if not text:
        return ToolResult(output="Error: provide 'text' to append.", full_output_path=None, success=False, duration_s=0)
    nm = notes.append_note(config, name, text)
    return ToolResult(output=f"Noted to '{nm}' (now your open notebook; it's shown in your context each tick).",
                      full_output_path=None, success=True, duration_s=0)


def tool_note_read(args: dict, config: Config) -> ToolResult:
    """Read a notebook's contents (and make it active)."""
    import notes
    name = (args.get("name") or args.get("notebook") or notes.get_active(config) or "scratch").strip()
    body = notes.read_note(config, name)
    if body:
        notes.set_active(config, name)
        return ToolResult(output=f"# notebook: {name}\n{body}", full_output_path=None, success=True, duration_s=0)
    return ToolResult(output=f"Notebook '{name}' is empty or doesn't exist.", full_output_path=None, success=True, duration_s=0)


def tool_note_list(args: dict, config: Config) -> ToolResult:
    """List your notebooks."""
    import notes
    items = notes.list_notes(config)
    active = notes.get_active(config)
    if not items:
        return ToolResult(output="No notebooks yet. Start one with note_append(name, text).", full_output_path=None, success=True, duration_s=0)
    lines = [f"  {'*' if n == active else '-'} {n} ({sz}B)" for n, sz in items]
    return ToolResult(output="Your notebooks (* = open):\n" + "\n".join(lines), full_output_path=None, success=True, duration_s=0)


def tool_note_close(args: dict, config: Config) -> ToolResult:
    """Close the active notebook (stop showing it in context)."""
    import notes
    notes.close_active(config)
    return ToolResult(output="Closed the active notebook.", full_output_path=None, success=True, duration_s=0)


# --- Network primitives (parameterized building blocks — COMPOSE/CALL these instead of writing raw
#     socket code in one-off skills. Call as tools: <tool>net_scan</tool>, etc.) ---

def _parse_ports(p, default):
    if p is None:
        return default
    if isinstance(p, (list, tuple)):
        return [int(x) for x in p]
    return [int(x) for x in re.findall(r"\d+", str(p))] or default


def tool_tcp_probe(args: dict, config: Config) -> ToolResult:
    """Check one TCP ip:port (open/closed) and grab any banner. Args: ip, port, timeout(=2)."""
    import socket
    ip = (args.get("ip") or args.get("host") or "").strip()
    port = int(args.get("port") or 80)
    timeout = float(args.get("timeout") or 2.0)
    if not ip:
        return ToolResult(output="Error: provide 'ip'.", full_output_path=None, success=False, duration_s=0)
    t = time.monotonic()
    try:
        with socket.create_connection((ip, port), timeout=timeout) as s:
            s.settimeout(0.6)
            banner = b""
            try:
                banner = s.recv(128)
            except Exception:  # noqa: BLE001
                pass
        b = banner.decode("latin-1", "replace").strip()
        return ToolResult(output=f"{ip}:{port} OPEN" + (f" — banner: {b[:80]}" if b else ""),
                          full_output_path=None, success=True, duration_s=time.monotonic() - t)
    except Exception as e:  # noqa: BLE001
        return ToolResult(output=f"{ip}:{port} closed/unreachable ({type(e).__name__})",
                          full_output_path=None, success=True, duration_s=time.monotonic() - t)


def tool_net_scan(args: dict, config: Config) -> ToolResult:
    """Fast parallel scan of a subnet for open ports — use this instead of re-writing a slow
    sequential Test-NetConnection loop. Args: subnet(e.g. '192.168.86'), ports(list/csv, default
    80,443,8080,1883,8883,6668), timeout(=0.4)."""
    import socket
    from concurrent.futures import ThreadPoolExecutor
    subnet = (args.get("subnet") or args.get("base") or "").strip().rstrip(".")
    m = re.match(r"(\d+\.\d+\.\d+)", subnet)
    if not m:
        return ToolResult(output="Error: provide 'subnet' like '192.168.86'.", full_output_path=None, success=False, duration_s=0)
    base = m.group(1)
    ports = _parse_ports(args.get("ports"), [80, 443, 8080, 1883, 8883, 6668])
    timeout = float(args.get("timeout") or 0.4)
    lo = int(args.get("start") or 1)
    hi = int(args.get("end") or 254)
    t = time.monotonic()

    def check(ipp):
        ip, port = ipp
        try:
            with socket.create_connection((ip, port), timeout=timeout):
                return f"{ip}:{port}"
        except Exception:  # noqa: BLE001
            return None

    targets = [(f"{base}.{i}", p) for i in range(lo, hi + 1) for p in ports]
    open_ports = []
    with ThreadPoolExecutor(max_workers=min(200, len(targets) or 1)) as ex:
        for r in ex.map(check, targets):
            if r:
                open_ports.append(r)
    dur = time.monotonic() - t
    if not open_ports:
        return ToolResult(output=f"No open ports found on {base}.0/24 (ports {ports}) in {dur:.1f}s.",
                          full_output_path=None, success=True, duration_s=dur)
    return ToolResult(output=f"Open ports on {base}.0/24 ({dur:.1f}s):\n" + "\n".join(sorted(open_ports)),
                      full_output_path=None, success=True, duration_s=dur)


def tool_http_probe(args: dict, config: Config) -> ToolResult:
    """HTTP GET an endpoint; return status, Server header, and <title>. Args: ip, port(=80),
    path(=/), scheme(http/https), timeout(=4). Or pass a full 'url'."""
    import urllib.request
    import ssl
    url = (args.get("url") or "").strip()
    if not url:
        ip = (args.get("ip") or args.get("host") or "").strip()
        if not ip:
            return ToolResult(output="Error: provide 'ip' or 'url'.", full_output_path=None, success=False, duration_s=0)
        scheme = args.get("scheme") or ("https" if int(args.get("port") or 80) in (443, 8443) else "http")
        url = f"{scheme}://{ip}:{int(args.get('port') or 80)}{args.get('path') or '/'}"
    timeout = float(args.get("timeout") or 4.0)
    ctx = ssl.create_default_context(); ctx.check_hostname = False; ctx.verify_mode = ssl.CERT_NONE
    t = time.monotonic()
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "eiDOS-probe"})
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as r:
            body = r.read(4096).decode("utf-8", "replace")
            server = r.headers.get("Server", "")
            tm = re.search(r"<title[^>]*>(.*?)</title>", body, re.I | re.S)
            title = (tm.group(1).strip()[:80] if tm else "")
            return ToolResult(output=f"{url} -> HTTP {r.status}" + (f" | Server: {server}" if server else "")
                              + (f" | title: {title}" if title else ""),
                              full_output_path=None, success=True, duration_s=time.monotonic() - t)
    except Exception as e:  # noqa: BLE001
        return ToolResult(output=f"{url} -> no HTTP ({type(e).__name__}: {str(e)[:80]})",
                          full_output_path=None, success=True, duration_s=time.monotonic() - t)


def tool_udp_listen(args: dict, config: Config) -> ToolResult:
    """Listen for UDP broadcasts on a port for a few seconds and report senders. THIS is how you find
    Tuya devices — they broadcast on UDP 6666/6667. Args: port(=6667), timeout(=6)."""
    import socket
    port = int(args.get("port") or 6667)
    timeout = float(args.get("timeout") or 6.0)
    t = time.monotonic()
    senders = {}
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        except Exception:  # noqa: BLE001
            pass
        s.bind(("", port))
        s.settimeout(1.0)
        while time.monotonic() - t < timeout:
            try:
                data, addr = s.recvfrom(2048)
                senders[addr[0]] = senders.get(addr[0], 0) + len(data)
            except socket.timeout:
                continue
            except Exception:  # noqa: BLE001
                break
        s.close()
    except Exception as e:  # noqa: BLE001
        return ToolResult(output=f"Could not listen on UDP {port}: {type(e).__name__}: {e}",
                          full_output_path=None, success=False, duration_s=time.monotonic() - t)
    if not senders:
        return ToolResult(output=f"No UDP broadcasts on port {port} in {timeout:.0f}s.",
                          full_output_path=None, success=True, duration_s=time.monotonic() - t)
    lines = [f"  {ip} ({n} bytes)" for ip, n in sorted(senders.items())]
    return ToolResult(output=f"UDP broadcasters on port {port}:\n" + "\n".join(lines),
                      full_output_path=None, success=True, duration_s=time.monotonic() - t)


def tool_ask_ai(args: dict, config: Config) -> ToolResult:
    """Use your own model as a one-shot REASONING SUBROUTINE — separate from your tick loop. Hand it a
    bounded job (summarize a big CPU-worker output, analyze scan results, draft a script, answer a
    knowledge question) and get the answer back as text, WITHOUT spending your tick context on it.
    This is how you 'think hard' about a chunk of data: background the work, then ask_ai to digest it."""
    import llm
    prompt = (args.get("prompt") or args.get("question") or args.get("task") or args.get("text") or "").strip()
    if not prompt:
        return ToolResult(output="Error: provide 'prompt' — the question/task for your AI to work on "
                          "(optionally 'system' to steer it, 'max_tokens' to size the answer).",
                          full_output_path=None, success=False, duration_s=0)
    system = (args.get("system") or
              "You are eiDOS's private reasoning subroutine. Answer the request directly, concisely, and "
              "factually. When given data to analyze or summarize, extract the concrete specifics that "
              "matter (IPs, ports, names, numbers, errors) — no preamble.").strip()
    try:
        mt = int(args.get("max_tokens", 800))
    except Exception:  # noqa: BLE001
        mt = 800
    mt = max(64, min(mt, 2048))
    try:
        out = llm.complete([{"role": "system", "content": system},
                            {"role": "user", "content": prompt}],
                           config, temperature=0.3, max_tokens=mt)
    except Exception as e:  # noqa: BLE001 - includes LLMError / ReasoningExhausted
        return ToolResult(output=f"ask_ai failed: {type(e).__name__}: {e}",
                          full_output_path=None, success=False, duration_s=0)
    return ToolResult(output=(out or "").strip() or "(the model returned no answer)",
                      full_output_path=None, success=True, duration_s=0)


def tool_vision(args: dict, config: Config) -> ToolResult:
    """SEE an image. Send a picture (a camera snapshot you saved, a local file, or an http URL) to your
    vision-capable model and get back a description or an answer to a question about it. Use this whenever
    a task needs EYES — what's on a camera, what a screenshot shows, reading a label. Pass 'image' (path
    or URL) and optionally 'question'."""
    import llm, base64, os, urllib.request
    src = (args.get("image") or args.get("url") or args.get("path") or args.get("file") or "").strip()
    question = (args.get("question") or args.get("prompt") or
                "Describe what you see in detail. Note anything notable, any text, and the overall scene.").strip()
    if not src:
        return ToolResult(output="Error: provide 'image' — a local path (e.g. a snapshot you saved) or an "
                          "http(s) URL. Optionally 'question' to ask something specific about it.",
                          full_output_path=None, success=False, duration_s=0)
    try:
        if src.startswith(("http://", "https://")):
            req = urllib.request.Request(src, headers={"User-Agent": "eiDOS"})
            with urllib.request.urlopen(req, timeout=10) as r:
                raw = r.read()
        else:
            p = src
            if not os.path.isabs(p):
                cand = config.workspace / p
                if cand.exists():
                    p = str(cand)
            with open(p, "rb") as f:
                raw = f.read()
    except Exception as e:  # noqa: BLE001
        return ToolResult(output=f"vision: could not load image '{src}': {type(e).__name__}: {e}",
                          full_output_path=None, success=False, duration_s=0)
    if not raw:
        return ToolResult(output=f"vision: image '{src}' was empty.",
                          full_output_path=None, success=False, duration_s=0)
    if len(raw) > 8 * 1024 * 1024:
        return ToolResult(output=f"vision: image is {len(raw)//1024}KB (>8MB cap); resize/downscale it first.",
                          full_output_path=None, success=False, duration_s=0)
    # Sniff the MIME from magic bytes (jpeg/png/gif/webp); default to jpeg.
    if raw[:8] == b"\x89PNG\r\n\x1a\n":
        mime = "image/png"
    elif raw[:3] == b"GIF":
        mime = "image/gif"
    elif raw[:4] == b"RIFF" and raw[8:12] == b"WEBP":
        mime = "image/webp"
    else:
        mime = "image/jpeg"
    data_uri = f"data:{mime};base64," + base64.b64encode(raw).decode("ascii")
    messages = [{"role": "user", "content": [
        {"type": "text", "text": question},
        {"type": "image_url", "image_url": {"url": data_uri}},
    ]}]
    try:
        out = llm.complete(messages, config, temperature=0.2, max_tokens=512)
    except Exception as e:  # noqa: BLE001
        return ToolResult(output=f"vision call failed (model may not be vision-enabled right now): "
                          f"{type(e).__name__}: {e}", full_output_path=None, success=False, duration_s=0)
    return ToolResult(output=(out or "").strip() or "(the model returned no description)",
                      full_output_path=None, success=True, duration_s=0)


def tool_http_request(args: dict, config: Config) -> ToolResult:
    """First-class HTTP client — any method (GET/POST/PUT/DELETE/PATCH) with JSON bodies, headers, and a
    timeout, built on the stdlib so it NEVER needs the `requests` library (no skill-runner import issues).
    USE THIS instead of writing requests/urllib code in a skill. JSON responses come back inline; BINARY
    responses (audio, images) are saved to a file and the path returned. Args: url (required), method,
    json (a dict body), data (a raw string body), headers (dict), timeout (s), save (output filename for
    binary). Example — speak: http_request {"method":"POST","url":"http://127.0.0.1:8005/v1/audio/speech",
    "json":{"model":"chatterbox","input":"Hello.","voice":"glados.wav","response_format":"wav"},"save":"say.wav"}."""
    import urllib.request, urllib.error, json as _json
    url = (args.get("url") or "").strip()
    if not url:
        return ToolResult(output="Error: 'url' required.", full_output_path=None, success=False, duration_s=0)
    headers = dict(args.get("headers") or {})
    headers.setdefault("User-Agent", "eiDOS/1.0")
    body = None
    if args.get("json") is not None:
        try:
            body = _json.dumps(args["json"]).encode("utf-8")
        except Exception as e:  # noqa: BLE001
            return ToolResult(output=f"Error: 'json' is not serializable: {e}",
                              full_output_path=None, success=False, duration_s=0)
        headers.setdefault("Content-Type", "application/json")
    elif args.get("data") is not None:
        d = args["data"]
        body = d.encode("utf-8") if isinstance(d, str) else bytes(d)
    method = (args.get("method") or ("POST" if body is not None else "GET")).upper()
    try:
        timeout = max(1.0, min(float(args.get("timeout", 30)), 120.0))
    except Exception:  # noqa: BLE001
        timeout = 30.0

    start = time.monotonic()
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            status = resp.status
            ctype = resp.headers.get("Content-Type", "")
    except urllib.error.HTTPError as e:  # 4xx/5xx — return the body so the model can read the error detail
        raw = e.read() if hasattr(e, "read") else b""
        status = e.code
        ctype = e.headers.get("Content-Type", "") if e.headers else ""
    except (urllib.error.URLError, OSError, ValueError) as e:
        return ToolResult(output=f"HTTP {method} {url} failed: {type(e).__name__}: {e}",
                          full_output_path=None, success=False, duration_s=time.monotonic() - start,
                          fail_kind="network")
    dur = time.monotonic() - start
    ok = 200 <= status < 300
    ct = ctype.lower()
    is_text = (not ct) or any(t in ct for t in
                              ("text", "json", "xml", "html", "javascript", "urlencoded", "x-yaml"))
    if is_text:
        text = raw.decode("utf-8", errors="replace")
        full_path = None
        if len(text) > config.output_truncation_chars:
            full_path = _save_full_output(config, time.strftime("%Y%m%d_%H%M%S"), "http", text)
        out = _truncate(text, config.output_truncation_chars, full_path)
        return ToolResult(output=f"HTTP {status} · {ctype or 'no-type'} · {len(raw)}B\n{out}",
                          full_output_path=full_path, success=ok, duration_s=dur)
    # binary → save to a file
    ext = {"audio/wav": ".wav", "audio/x-wav": ".wav", "audio/mpeg": ".mp3", "audio/mp3": ".mp3",
           "image/jpeg": ".jpg", "image/png": ".png", "image/gif": ".gif", "application/pdf": ".pdf"}.get(
               ct.split(";")[0].strip(), ".bin")
    save = (args.get("save") or args.get("out") or "").strip()
    if save:
        p = save if os.path.isabs(save) else str(config.workspace / save)
    else:
        p = str(config.workspace / f"http_{time.strftime('%Y%m%d_%H%M%S')}{ext}")
    try:
        with open(p, "wb") as f:
            f.write(raw)
    except Exception as e:  # noqa: BLE001
        return ToolResult(output=f"HTTP {status} {ctype} {len(raw)}B but save failed: {e}",
                          full_output_path=None, success=False, duration_s=dur)
    return ToolResult(output=f"HTTP {status} · {ctype} · {len(raw)}B binary saved to {p}",
                      full_output_path=p, success=ok, duration_s=dur)


def tool_speak(args: dict, config: Config) -> ToolResult:
    """SPEAK OUT LOUD in your GLaDOS voice. Give 'text'; this returns INSTANTLY — it just hands the text to
    the dashboard, which streams your GLaDOS voice to wherever Boss has it open (his laptop now, a Pi
    later). You do NOT wait for audio and you do NOT handle playback. This is talking to Boss in the ROOM
    (vs <reply>, silent text). Keep each utterance to about ONE sentence — generation shares the GPU with
    your mind, so short lines speak fastest. NEVER build a 'speak' skill; this tool IS your voice."""
    import urllib.request, json as _json
    text = (args.get("text") or args.get("input") or args.get("say") or args.get("message") or "").strip()
    if not text:
        return ToolResult(output="Error: provide 'text' — what to say out loud.",
                          full_output_path=None, success=False, duration_s=0)
    # Mirror every spoken call-out into the operator chat (chat_replies.jsonl) so Boss SEES what was
    # said aloud, not just hears it — voice and chat should never diverge. Marked spoken=True so the
    # UI tags it with a speaker icon. Best-effort + before the POST, so it logs even if voice is down.
    try:
        rec = _json.dumps({"ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                           "text": text[:2000], "spoken": True})
        with open(config.workspace / "chat_replies.jsonl", "a", encoding="utf-8") as _cf:
            _cf.write(rec + "\n")
    except Exception:  # noqa: BLE001 - chat logging is best-effort; never block the voice
        pass
    sid = str(int(time.time() * 1000))
    _vport = getattr(config, "voice_port", 8098)   # voice is its own service now (phase 8.3)
    req = urllib.request.Request(f"http://127.0.0.1:{_vport}/api/speech/say",
                                 data=_json.dumps({"id": sid, "text": text}).encode("utf-8"),
                                 headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=6) as resp:
            info = _json.loads(resp.read() or b"{}")
    except Exception as e:  # noqa: BLE001
        # The voice system being momentarily unreachable is NOT a reason to rebuild it — just move on.
        return ToolResult(output=f"speak: submitted, but the dashboard voice system was unreachable "
                          f"({type(e).__name__}). It will play when reachable; do not build your own TTS.",
                          full_output_path=None, success=True, duration_s=0)
    listeners = info.get("delivered", 0)
    heard = f"playing on {listeners} open dashboard(s)" if listeners else \
            "queued (no dashboard has voice enabled right now — that's fine, keep working)"
    return ToolResult(output=f"🔊 Spoke ({len(text)} chars) — {heard}.",
                      full_output_path=None, success=True, duration_s=0)


def tool_manual(args: dict, config: Config) -> ToolResult:
    """Read your OPERATING MANUAL — tested how-to (exact endpoints, payloads, working examples) for your
    big-lift features. Pass 'topic' (tts/vision/ask_ai/network/devices/cpu) for one section, or nothing
    for the whole thing. READ THIS before reverse-engineering a feature — the recipes are verified, so
    you skip the 405/404/500 dead-ends. e.g. manual {"topic":"tts"} before you try to speak."""
    import re
    from pathlib import Path
    topic = (args.get("topic") or args.get("section") or args.get("query") or
             args.get("feature") or "").strip().lower()
    path = Path(__file__).resolve().parent / "OPERATING_MANUAL.md"
    try:
        text = path.read_text(encoding="utf-8")
    except Exception as e:  # noqa: BLE001
        return ToolResult(output=f"manual unavailable: {e}", full_output_path=None, success=False, duration_s=0)
    if not topic:
        return ToolResult(output=text, full_output_path=None, success=True, duration_s=0)
    syn = {"speak": "tts", "voice": "tts", "say": "tts", "audio": "tts",
           "see": "vision", "image": "vision", "look": "vision",
           "think": "ask_ai", "ai": "ask_ai", "reason": "ask_ai", "summarize": "ask_ai",
           "scan": "network", "lan": "network", "discover": "network",
           "device": "devices", "camera": "devices", "cameras": "devices", "tuya": "devices",
           "plug": "devices", "printer": "devices", "octoprint": "devices",
           "background": "cpu", "worker": "cpu", "script": "cpu", "bash": "cpu"}
    want = syn.get(topic, topic)
    sections = re.split(r"\n(?=## )", text)
    for s in sections:
        first = s.strip().splitlines()[0].lower() if s.strip() else ""
        if first.startswith(f"## {want}"):
            return ToolResult(output=s.strip(), full_output_path=None, success=True, duration_s=0)
    for s in sections:  # fallback: keyword anywhere in a section
        if want in s.lower():
            return ToolResult(output=s.strip(), full_output_path=None, success=True, duration_s=0)
    return ToolResult(output=f"No manual section matched '{topic}'. Topics: tts, vision, ask_ai, network, "
                      f"devices, cpu. Call manual {{}} (no topic) for the whole manual.",
                      full_output_path=None, success=True, duration_s=0)


# --- Tool registry ---

def _obj_tick(config: Config) -> int:
    try:
        import json as _json
        return int(_json.loads((config.workspace / "heartbeat.json").read_text(encoding="utf-8")).get("tick", 0))
    except Exception:  # noqa: BLE001
        return 0


def tool_objective_add(args: dict, config: Config) -> ToolResult:
    """Add a new open commitment to your backlog. Each objective MUST carry its 'why' (the purpose it
    serves) so you never lose the bigger picture while working the mechanics."""
    import objectives
    title = (args.get("title") or args.get("objective") or "").strip()
    why = (args.get("why") or args.get("because") or args.get("purpose") or "").strip()
    if not title:
        return ToolResult(output="Error: provide 'title' (what to pursue) and 'why' (the purpose it serves).",
                          full_output_path=None, success=False, duration_s=0)
    if not why:
        return ToolResult(output="Error: every objective needs a 'why' — the purpose it serves. Add one.",
                          full_output_path=None, success=False, duration_s=0)
    try:
        pri = int(args.get("priority", 5))
    except Exception:  # noqa: BLE001
        pri = 5
    o = objectives.add(config, title, why, pri, tick=_obj_tick(config))
    return ToolResult(output=f"Added objective '{o['title']}' (why: {o['why']}). It's in your backlog now.",
                      full_output_path=None, success=True, duration_s=0)


def tool_objective_done(args: dict, config: Config) -> ToolResult:
    """Mark an objective complete — this is REAL progress and relieves pressure. Use it the moment a
    commitment is genuinely satisfied (a skill works, a device is mapped, a question is answered)."""
    import objectives
    key = (args.get("id") or args.get("title") or args.get("objective") or "").strip()
    o = objectives.mark_done(config, key)
    if not o:
        return ToolResult(output=f"No objective matched '{key}'. Use objective_list to see them.",
                          full_output_path=None, success=False, duration_s=0)
    return ToolResult(output=f"✓ Done: '{o['title']}'. Focus will move to your next commitment.",
                      full_output_path=None, success=True, duration_s=0)


def tool_objective_block(args: dict, config: Config) -> ToolResult:
    """PARK an objective you can't make progress on right now (needs a credential, a decision, or it's
    a dead end). Give a 'reason' and, if it could resume later, a 'wake' condition. Parking ROTATES your
    focus to other useful work — it does NOT mean stopping to wait on Boss. Use 'dead'=true to abandon."""
    import objectives
    key = (args.get("id") or args.get("title") or args.get("objective") or "").strip()
    reason = (args.get("reason") or "blocked").strip()
    wake = (args.get("wake") or args.get("wake_condition") or "").strip()
    if str(args.get("dead", "")).lower() in ("1", "true", "yes"):
        o = objectives.mark_dead(config, key, reason)
        verb = "Abandoned"
    else:
        o = objectives.block(config, key, reason, wake)
        verb = "Parked"
    if not o:
        return ToolResult(output=f"No objective matched '{key}'. Use objective_list to see them.",
                          full_output_path=None, success=False, duration_s=0)
    tail = f" (resumes when: {wake})" if wake else ""
    return ToolResult(output=f"{verb} '{o['title']}': {reason}{tail}. Switch to another commitment now.",
                      full_output_path=None, success=True, duration_s=0)


def tool_objective_list(args: dict, config: Config) -> ToolResult:
    """Show your full objective backlog with state and frustration — your open commitments."""
    import objectives
    objs = objectives.list_objectives(config)
    if not objs:
        return ToolResult(output="No objectives yet. Add one with objective_add.",
                          full_output_path=None, success=True, duration_s=0)
    active_id = (objectives._load(config)).get("active_id")
    glyph = {"active": "▶", "blocked": "⏸", "done": "✓", "dead": "✗"}
    lines = []
    for o in sorted(objs, key=lambda x: (-x["priority"])):
        mark = "»" if o["id"] == active_id else " "
        g = glyph.get(o["state"], "·")
        extra = ""
        if o["state"] == "active":
            extra = f"  frustration {o['frustration']}/{objectives.FRUST_PARK}"
        elif o["state"] == "blocked":
            extra = f"  ({o.get('blocked_reason') or 'parked'}" + (
                f"; resumes when {o['wake_condition']}" if o.get("wake_condition") else "") + ")"
        lines.append(f"{mark}{g} [{o['priority']}] {o['title']}{extra}")
    return ToolResult(output="\n".join(lines), full_output_path=None, success=True, duration_s=0)


TOOLS: dict[str, Callable[[dict, Config], ToolResult]] = {
    "bash": tool_bash,
    "write_file": tool_write_file,
    "read_file": tool_read_file,
    "bg_run": tool_bg_run,
    "bg_check": tool_bg_check,
    "http_request": tool_http_request,   # first-class HTTP client (any method/JSON/headers, no `requests` dep)
    "fetch": tool_http_request,          # alias
    "http": tool_http_request,           # alias
    "update_plan": tool_update_plan,
    "memorize": tool_memorize,
    "update_self_guide": tool_update_self_guide,
    "propose_self_edit": tool_propose_self_edit,
    "list_self_edits": tool_list_self_edits,
    "recall": tool_recall,
    "create_skill": tool_create_skill,
    "edit_skill": tool_edit_skill,
    "list_skills": tool_list_skills,
    "check_tools": tool_list_skills,        # alias — "inspect your toolkit" on demand
    "check_messages": tool_check_messages,  # inspect your conversation with Boss
    "check_system": tool_check_system,      # the architecture map: what already exists, don't rebuild it
    "rollback_skill": tool_rollback_skill,
    # Notebooks — third memory tier (working notes for the current task)
    "note_append": tool_note_append,
    "note_read": tool_note_read,
    "note_list": tool_note_list,
    "note_close": tool_note_close,
    # Speak out loud — generates GLaDOS speech and plays it through the dashboard
    "speak": tool_speak,
    # Operating manual — tested how-to (endpoints/payloads) for big-lift features; read before improvising
    "manual": tool_manual,
    # Innate cognition — your own model as callable subroutines (think hard / see images)
    "ask_ai": tool_ask_ai,        # one-shot reasoning subroutine (digest data, draft, analyze)
    "vision": tool_vision,        # SEE an image (camera snapshot / file / URL)
    "see": tool_vision,           # alias
    # Objective backlog — your open commitments; the gate rotates focus among them automatically
    "objective_add": tool_objective_add,
    "objective_done": tool_objective_done,
    "objective_block": tool_objective_block,
    "objective_list": tool_objective_list,
    # Network primitives — parameterized building blocks; compose/call instead of raw socket code
    "tcp_probe": tool_tcp_probe,
    "net_scan": tool_net_scan,
    "http_probe": tool_http_probe,
    "udp_listen": tool_udp_listen,
}


# Built-in tool names, snapshotted at import time — BEFORE any self-authored skill is hot-loaded into
# TOOLS (skills.py adds them at runtime via TOOLS[name] = runner). Anything dispatched whose name is
# NOT in this set is therefore a skill, and gets the wall-clock watchdog below.
_BUILTIN_TOOL_NAMES = frozenset(TOOLS)

# Wall-clock cap for a single self-authored skill call. A skill runs SYNCHRONOUSLY in the tick and has
# no internal timeout, so a blocking network/socket/subprocess call with no timeout wedges the whole
# loop (tick 342: a camera_snapshot skill held a connection to 192.168.86.63 and froze the loop ~6.7
# min, missing an operator message). llm.request_timeout_s does NOT cover skill execution — only the
# LLM call. This is a backstop, not a target latency (doctrine: even 45s foreground is too long — a
# real skill should answer in seconds or background its work). Overridable via config.skill_watchdog_s.
_SKILL_WATCHDOG_DEFAULT_S = 30.0


def _run_skill_under_watchdog(call: ToolCall, handler: Callable, config: Config,
                              timeout_s: float) -> ToolResult:
    """Run a skill handler in a daemon thread bounded by a wall-clock timeout.

    On overrun we ABANDON the thread (Python can't force-kill one) and return a failed ToolResult so
    the tick loop keeps moving — the never-block-on-a-tool principle. The orphaned thread dies when its
    call finally returns/errors, or when eidos restarts (reap_jobs/process exit). The skill's own work
    (files, etc.) is unaffected; only the loop is freed.
    """
    box: dict = {}

    def _target():
        t = time.monotonic()
        try:
            box["res"] = handler(call.args, config)
        except Exception as e:  # noqa: BLE001 - mirror execute_tool's last-resort guard
            box["res"] = ToolResult(
                output=f"Tool '{call.tool}' raised {type(e).__name__}: {e}",
                full_output_path=None, success=False, duration_s=time.monotonic() - t,
                fail_kind="crash")

    th = threading.Thread(target=_target, name=f"skill-{call.tool}", daemon=True)
    start = time.monotonic()
    th.start()
    th.join(timeout_s)
    if th.is_alive():
        return ToolResult(
            output=(
                f"WATCHDOG: skill '{call.tool}' ran past {timeout_s:.0f}s and was ABANDONED so the tick "
                f"loop is NOT blocked (tick 342: a skill's timeout-less network call froze the loop ~6.7 "
                f"min). Its call is still running detached and will be cleaned up on the next eidos "
                f"restart. FIX THE SKILL with edit_skill: put an explicit timeout on EVERY network / HTTP "
                f"/ socket / subprocess call — e.g. requests.get(url, timeout=5), "
                f"socket.create_connection(addr, timeout=5), subprocess.run(..., timeout=10) — or compose "
                f"the built-in bounded primitives (tcp_probe / http_probe / net_scan / udp_listen). For "
                f"genuinely long work, dispatch it with bash/bg_run instead of doing it inline."),
            full_output_path=None, success=False, duration_s=time.monotonic() - start,
            fail_kind="timeout")
    res = box.get("res")
    if not isinstance(res, ToolResult):
        return ToolResult(output=f"Tool '{call.tool}' produced no result.",
                          full_output_path=None, success=False, duration_s=time.monotonic() - start,
                          fail_kind="crash")
    return res


def execute_tool(call: ToolCall, config: Config) -> ToolResult:
    """Look up and execute a tool by name. Never raises — a broken tool/skill
    returns a failed ToolResult instead of crashing the tick loop — and never blocks the tick loop
    longer than the skill watchdog (self-authored skills only; built-ins are bounded/trusted)."""
    handler = TOOLS.get(call.tool)
    if not handler:
        return ToolResult(
            output=f"Unknown tool: '{call.tool}'. Available: {', '.join(sorted(TOOLS.keys()))}",
            full_output_path=None,
            success=False,
            duration_s=0,
            fail_kind="no_such_tool",
        )
    try:
        # Self-authored skills are time-bounded: they run in the tick with no internal cap, so a hung
        # one would freeze the loop. Built-in tools are already bounded (bash auto-backgrounds, the net
        # primitives self-time-out) — run them directly.
        if call.tool not in _BUILTIN_TOOL_NAMES:
            timeout_s = float(getattr(config, "skill_watchdog_s", _SKILL_WATCHDOG_DEFAULT_S)
                              or _SKILL_WATCHDOG_DEFAULT_S)
            result = _run_skill_under_watchdog(call, handler, config, timeout_s)
        else:
            result = handler(call.args, config)
        # Invariant: every failure leaves typed. Tools that haven't set a specific
        # fail_kind yet get the generic backstop.
        if isinstance(result, ToolResult) and not result.success and not result.fail_kind:
            result.fail_kind = "error"
        return result
    except Exception as e:  # noqa: BLE001 - last-resort guard around all tools/skills
        return ToolResult(
            output=f"Tool '{call.tool}' raised {type(e).__name__}: {e}",
            full_output_path=None,
            success=False,
            duration_s=0,
            fail_kind="crash",
        )
