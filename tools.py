"""Tool registry and implementations."""

import dataclasses
import json
import os
import re
import signal
import subprocess
import time
from pathlib import Path
from typing import Callable, Optional

from config import Config
from parser import ToolCall
from safety import is_command_blocked, check_disk_space


@dataclasses.dataclass
class ToolResult:
    output: str
    full_output_path: Optional[str]
    success: bool
    duration_s: float


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


def _looks_like_powershell(cmd: str) -> bool:
    """Heuristic: does this command want PowerShell rather than cmd.exe?"""
    import re
    c = cmd.strip()
    low = c.lower()
    if low.startswith(("powershell", "pwsh")):
        return False  # already explicit PowerShell
    verbs = ("Get|Set|New|Remove|Start|Stop|Restart|Test|Select|Where|ForEach|Write|Read|Out|"
             "Import|Export|Invoke|Add|Clear|Copy|Move|Rename|Measure|Sort|Group|Format|Convert|"
             "ConvertTo|ConvertFrom|Resolve|Enable|Disable|Update|Install|Uninstall|Find|Wait|"
             "Receive|Send|Compare|Join|Split|Tee")
    if re.search(r'(?:^|[\s|;(&])(?:' + verbs + r')-[A-Za-z]\w*', c, re.IGNORECASE):
        return True
    if "$_" in c or "-computername" in low:
        return True
    for tok in ("| where-object", "| foreach-object", "| select-object",
                "| measure-object", "| sort-object", "write-host", "write-output"):
        if tok in low:
            return True
    return False


def _kill_proc_tree(proc):
    """Kill a process and its entire descendant tree (Windows-safe).

    On Windows, killing only the immediate child leaves grandchildren holding the
    output pipe open, so communicate() hangs forever. taskkill /T kills the tree.
    """
    if proc is None or proc.poll() is not None:
        return
    if os.name == "nt":
        try:
            subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                           capture_output=True, timeout=15)
        except Exception:  # noqa: BLE001
            try:
                proc.kill()
            except Exception:  # noqa: BLE001
                pass
    else:
        import signal as _signal
        try:
            os.killpg(os.getpgid(proc.pid), _signal.SIGKILL)
        except Exception:  # noqa: BLE001
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
        return ToolResult(output="Error: no 'cmd' argument provided", full_output_path=None, success=False, duration_s=0)

    # Safety check
    blocked = is_command_blocked(cmd, config.protected_patterns)
    if blocked:
        return ToolResult(
            output=f"BLOCKED: command matches protected pattern '{blocked}'",
            full_output_path=None,
            success=False,
            duration_s=0,
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
                    full_output_path=None, success=False, duration_s=0,
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
                        "notified": False,
                    })
                    _write_jobs(config, jobs)
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
                        "notified": False,
                    })
                    _write_jobs(config, jobs)
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


def tool_http_get(args: dict, config: Config) -> ToolResult:
    """Fetch a URL via HTTP GET."""
    import urllib.request
    import urllib.error

    url = args.get("url", "")
    if not url:
        return ToolResult(output="Error: 'url' required", full_output_path=None, success=False, duration_s=0)

    start = time.monotonic()
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "eiDOS/1.0"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = resp.read().decode("utf-8", errors="replace")

        tick_id = time.strftime("%Y%m%d_%H%M%S")
        full_path = None
        if len(body) > config.output_truncation_chars:
            full_path = _save_full_output(config, tick_id, "http", body)
        output = _truncate(body, config.output_truncation_chars, full_path)

        return ToolResult(output=output, full_output_path=full_path, success=True, duration_s=time.monotonic() - start)
    except (urllib.error.URLError, OSError, ValueError) as e:
        return ToolResult(output=f"HTTP error: {e}", full_output_path=None, success=False, duration_s=time.monotonic() - start)


def tool_remember(args: dict, config: Config) -> ToolResult:
    """Write an urgent note to memory.md."""
    from memory import read_memory, write_memory

    note = args.get("note", "")
    if not note:
        return ToolResult(output="Error: 'note' required", full_output_path=None, success=False, duration_s=0)

    disk_ok, free_gb = check_disk_space(min_gb=config.disk_min_gb)
    if not disk_ok:
        return ToolResult(
            output=f"BLOCKED: disk space low ({free_gb:.1f} GB free, minimum {config.disk_min_gb} GB)",
            full_output_path=None, success=False, duration_s=0,
        )

    current = read_memory(config)
    timestamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    addition = f"\n\n[Remembered at {timestamp}]\n{note}"
    updated = current + addition

    # Hard cap: if memory would exceed budget, trim oldest lines from the top
    budget = config.context_memory_max_chars
    if len(updated) > budget:
        lines = updated.splitlines(keepends=True)
        while lines and len("".join(lines)) > budget:
            lines.pop(0)
        updated = "".join(lines)

    write_memory(config, updated)
    return ToolResult(output=f"Noted in memory: {note[:100]}", full_output_path=None, success=True, duration_s=0)


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


def tool_goal_complete(args: dict, config: Config) -> ToolResult:
    """Signal that the current goal has been achieved."""
    summary = args.get("summary", "")
    evidence = args.get("evidence", "")
    if not summary:
        return ToolResult(output="Error: 'summary' required", full_output_path=None, success=False, duration_s=0)

    return ToolResult(
        output=f"GOAL_COMPLETE: {summary}\nEvidence: {evidence}",
        full_output_path=None,
        success=True,
        duration_s=0,
    )


def tool_ask_supervisor(args: dict, config: Config) -> ToolResult:
    """Post a question for the remote supervisor or human."""
    question = args.get("question", "")
    if not question:
        return ToolResult(output="Error: 'question' required", full_output_path=None, success=False, duration_s=0)

    questions_path = config.workspace / "pending_questions.jsonl"
    entry = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "question": question,
        "status": "pending",
    }
    with open(questions_path, "a") as f:
        f.write(json.dumps(entry) + "\n")

    return ToolResult(
        output=f"Question posted for supervisor: {question}",
        full_output_path=None,
        success=True,
        duration_s=0,
    )


def tool_plan_goal(args: dict, config: Config) -> ToolResult:
    """Break a goal into subgoals using the planning model (hot-swap to larger model)."""
    from llm import planning_complete, LLMError
    from memory import read_subgoals, write_subgoals
    from prompts import PLANNING_SYSTEM, PLANNING_USER

    goal = args.get("goal", "")
    if not goal:
        return ToolResult(output="Error: 'goal' required", full_output_path=None, success=False, duration_s=0)

    context = args.get("context", "")
    start = time.monotonic()

    messages = [
        {"role": "system", "content": PLANNING_SYSTEM},
        {"role": "user", "content": PLANNING_USER.format(goal=goal, context=context)},
    ]

    try:
        result = planning_complete(messages, config)
    except LLMError as e:
        return ToolResult(
            output=f"Planning model error: {e}",
            full_output_path=None, success=False,
            duration_s=time.monotonic() - start,
        )

    # Write to subgoals.md (replaces existing)
    write_subgoals(config, result.strip())

    elapsed = time.monotonic() - start
    return ToolResult(
        output=f"Subgoals generated and saved ({len(result)} chars, {elapsed:.0f}s):\n{result[:500]}",
        full_output_path=None, success=True,
        duration_s=elapsed,
    )


# --- Jobs ledger helpers ---

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


def refresh_jobs(config: Config) -> list[dict]:
    """Check all tracked jobs, update statuses, return current list."""
    jobs = _read_jobs(config)
    changed = False
    for job in jobs:
        if job["status"] != "running":
            continue
        if not _pid_alive(job.get("pid", 0)):
            job["status"] = "completed"
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
    jobs = _read_jobs(config)
    killed = 0
    changed = False
    for j in jobs:
        if j.get("status") != "running":
            continue
        pid = j.get("pid", 0)
        if not _pid_alive(pid):
            j["status"] = "completed"
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


def _kill_pid_tree(pid: int) -> None:
    """Kill a process tree by PID (Windows-safe)."""
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
    jobs = _read_jobs(config)
    changed = False
    finished = []
    now = time.time()
    for j in jobs:
        if j.get("status") == "running":
            alive = _pid_alive(j.get("pid", 0))
            started_ts = j.get("started_ts")
            if alive:
                manual_cap = float(getattr(config, "bg_job_max_age_s", 1800.0))
                if (j.get("kind") in ("async", "auto")
                        and started_ts and (now - started_ts) > ceiling):
                    _kill_pid_tree(j.get("pid", 0))
                    j["status"] = "timed_out"
                    changed = True
                elif (started_ts and manual_cap > 0 and (now - started_ts) > manual_cap):
                    # Even deliberately-backgrounded manual bg_run jobs get a GENEROUS lifetime cap,
                    # so a runaway/infinite bg command (e.g. an all-night poll loop) can't run forever.
                    _kill_pid_tree(j.get("pid", 0))
                    j["status"] = "timed_out"
                    changed = True
                else:
                    continue  # still running — not finished yet
            else:
                j["status"] = "completed"
                changed = True
        # Deliver finished async/auto jobs exactly once.
        if (j.get("kind") in ("async", "auto")
                and j.get("status") in ("completed", "timed_out")
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
        return ToolResult(
            output=(f"{msg}\n➤ You now HAVE a reusable tool '{name}'. NEXT time you need this, CALL it "
                    f"as a tool: {{\"tool\": \"{name}\", \"args\": {{...}}}} — do NOT hand-write the same "
                    f"steps in bash again. Re-deriving what you already captured is wasted work."),
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


# --- Tool registry ---

TOOLS: dict[str, Callable[[dict, Config], ToolResult]] = {
    "bash": tool_bash,
    "write_file": tool_write_file,
    "read_file": tool_read_file,
    "bg_run": tool_bg_run,
    "bg_check": tool_bg_check,
    "http_get": tool_http_get,
    "remember": tool_remember,
    "update_plan": tool_update_plan,
    "memorize": tool_memorize,
    "update_self_guide": tool_update_self_guide,
    "propose_self_edit": tool_propose_self_edit,
    "list_self_edits": tool_list_self_edits,
    "recall": tool_recall,
    "goal_complete": tool_goal_complete,
    "ask_supervisor": tool_ask_supervisor,
    "plan_goal": tool_plan_goal,
    "create_skill": tool_create_skill,
    "edit_skill": tool_edit_skill,
    "list_skills": tool_list_skills,
    "check_tools": tool_list_skills,        # alias — "inspect your toolkit" on demand
    "check_messages": tool_check_messages,  # inspect your conversation with Boss
    "check_system": tool_check_system,      # the architecture map: what already exists, don't rebuild it
    "rollback_skill": tool_rollback_skill,
}


def execute_tool(call: ToolCall, config: Config) -> ToolResult:
    """Look up and execute a tool by name. Never raises — a broken tool/skill
    returns a failed ToolResult instead of crashing the tick loop."""
    handler = TOOLS.get(call.tool)
    if not handler:
        return ToolResult(
            output=f"Unknown tool: '{call.tool}'. Available: {', '.join(sorted(TOOLS.keys()))}",
            full_output_path=None,
            success=False,
            duration_s=0,
        )
    try:
        return handler(call.args, config)
    except Exception as e:  # noqa: BLE001 - last-resort guard around all tools/skills
        return ToolResult(
            output=f"Tool '{call.tool}' raised {type(e).__name__}: {e}",
            full_output_path=None,
            success=False,
            duration_s=0,
        )
