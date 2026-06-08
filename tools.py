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


def tool_bash(args: dict, config: Config) -> ToolResult:
    """Run a shell command with safety checks and output truncation."""
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
        p = _normalize_workspace_path(path, config)
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
        if not _pid_alive(job.get("pid", 0)):
            job["status"] = "completed"
            changed = True
    if changed:
        _write_jobs(config, jobs)
    return jobs


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
        return ToolResult(output=r.get("message", f"Skill '{name}' created and live."),
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
    "recall": tool_recall,
    "goal_complete": tool_goal_complete,
    "ask_supervisor": tool_ask_supervisor,
    "plan_goal": tool_plan_goal,
    "create_skill": tool_create_skill,
    "edit_skill": tool_edit_skill,
    "list_skills": tool_list_skills,
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
