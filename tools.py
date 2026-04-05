"""Tool registry and implementations."""

import dataclasses
import json
import os
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


def tool_bash(args: dict, config: Config) -> ToolResult:
    """Run a shell command with safety checks and output truncation."""
    cmd = args.get("cmd", "")
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

    start = time.monotonic()
    try:
        result = subprocess.run(
            cmd,
            shell=True,
            capture_output=True,
            text=True,
            timeout=config.cmd_timeout_s,
            cwd=str(config.workspace),
        )
        duration = time.monotonic() - start

        stdout = result.stdout or ""
        stderr = result.stderr or ""
        combined = stdout
        if stderr:
            combined += f"\n[stderr]\n{stderr}"

        tick_id = time.strftime("%Y%m%d_%H%M%S")
        full_path = None
        if len(combined) > config.output_truncation_chars:
            full_path = _save_full_output(config, tick_id, "bash", combined)

        output = _truncate(combined, config.output_truncation_chars, full_path)

        return ToolResult(
            output=output,
            full_output_path=full_path,
            success=(result.returncode == 0),
            duration_s=duration,
        )

    except subprocess.TimeoutExpired:
        duration = time.monotonic() - start
        return ToolResult(
            output=f"TIMEOUT: command exceeded {config.cmd_timeout_s}s limit",
            full_output_path=None,
            success=False,
            duration_s=duration,
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
        p = Path(path)
        # Resolve relative paths against workspace dir
        if not p.is_absolute():
            p = Path(config.workspace_dir) / p
        # Prevent path traversal outside workspace
        resolved = p.resolve()
        workspace_resolved = Path(config.workspace_dir).resolve()
        if not str(resolved).startswith(str(workspace_resolved) + os.sep) and resolved != workspace_resolved:
            return ToolResult(output="Error: path escapes workspace directory", full_output_path=None, success=False, duration_s=time.monotonic() - start)
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
        p = Path(path)
        # Resolve relative paths against workspace dir
        if not p.is_absolute():
            p = Path(config.workspace_dir) / p
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

    start = time.monotonic()
    config.outputs_dir.mkdir(parents=True, exist_ok=True)
    out_path = config.outputs_dir / f"bg_{name}.out"

    try:
        with open(out_path, "w") as out_file:
            proc = subprocess.Popen(
                cmd,
                shell=True,
                stdout=out_file,
                stderr=subprocess.STDOUT,
                cwd=str(config.workspace),
                start_new_session=True,
            )

        # Register in jobs ledger
        jobs = _read_jobs(config)
        jobs.append({
            "name": name,
            "pid": proc.pid,
            "cmd": cmd,
            "started": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "status": "running",
            "output_path": str(out_path),
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

    # Check if still running
    pid = job.get("pid", 0)
    if job["status"] == "running":
        try:
            # Try to reap zombie first (Popen children without wait)
            wpid, status = os.waitpid(pid, os.WNOHANG)
            if wpid != 0:
                job["status"] = "completed"
                _write_jobs(config, jobs)
            else:
                pass  # Still running
        except ChildProcessError:
            # Not our child — fall back to kill(0) probe
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                job["status"] = "completed"
                _write_jobs(config, jobs)
            except PermissionError:
                pass

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
        req = urllib.request.Request(url, headers={"User-Agent": "Kairos/1.0"})
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


def tool_memorize(args: dict, config: Config) -> ToolResult:
    """Store a durable knowledge entry in the long-term knowledge store."""
    from knowledge import store_entry

    fact = args.get("fact", "")
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
    if not tags:
        return ToolResult(output="Error: 'tags' required (list of strings)", full_output_path=None, success=False, duration_s=0)

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


# --- Jobs ledger helpers ---

def _read_jobs(config: Config) -> list[dict]:
    try:
        return json.loads(config.jobs_path.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return []


def _write_jobs(config: Config, jobs: list[dict]) -> None:
    config.workspace.mkdir(parents=True, exist_ok=True)
    config.jobs_path.write_text(json.dumps(jobs, indent=2))


def refresh_jobs(config: Config) -> list[dict]:
    """Check all tracked jobs, update statuses, return current list."""
    jobs = _read_jobs(config)
    changed = False
    for job in jobs:
        if job["status"] != "running":
            continue
        pid = job["pid"]
        try:
            wpid, status = os.waitpid(pid, os.WNOHANG)
            if wpid != 0:
                job["status"] = "completed"
                changed = True
        except ChildProcessError:
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                job["status"] = "completed"
                changed = True
            except PermissionError:
                pass
            changed = True
        except PermissionError:
            pass
    if changed:
        _write_jobs(config, jobs)
    return jobs


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
    "recall": tool_recall,
    "goal_complete": tool_goal_complete,
    "ask_supervisor": tool_ask_supervisor,
}


def execute_tool(call: ToolCall, config: Config) -> ToolResult:
    """Look up and execute a tool by name."""
    handler = TOOLS.get(call.tool)
    if not handler:
        return ToolResult(
            output=f"Unknown tool: '{call.tool}'. Available: {', '.join(TOOLS.keys())}",
            full_output_path=None,
            success=False,
            duration_s=0,
        )
    return handler(call.args, config)
