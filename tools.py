"""Tool registry and implementations."""
from __future__ import annotations

import dataclasses
import json
import os
import re
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Callable, Literal, Optional, Union

import platform_shell
from config import Config
from parser import ToolCall
from pydantic import AliasChoices, BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator
from safety import is_command_blocked, check_disk_space
from typed_boundary import validate_job_records


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


class _ToolArgs(BaseModel):
    """Strict validation for untrusted LLM/tool boundary dictionaries."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)


def _present(value: str, field_name: str) -> str:
    if not value.strip():
        raise ValueError(f"{field_name} must not be empty")
    return value


def _trimmed_nonempty(value: str, field_name: str) -> str:
    return _present(value, field_name).strip()


class BashArgs(_ToolArgs):
    cmd: str = Field(validation_alias=AliasChoices("cmd", "command"))
    wait: bool = False
    name: Optional[str] = None
    intent: Optional[str] = None

    @field_validator("cmd")
    @classmethod
    def _cmd_not_empty(cls, value: str) -> str:
        return _present(value, "cmd")


class WriteFileArgs(_ToolArgs):
    path: str
    content: str = ""

    @field_validator("path")
    @classmethod
    def _path_not_empty(cls, value: str) -> str:
        return _trimmed_nonempty(value, "path")


class ReadFileArgs(_ToolArgs):
    path: str

    @field_validator("path")
    @classmethod
    def _path_not_empty(cls, value: str) -> str:
        return _trimmed_nonempty(value, "path")


class BgRunArgs(_ToolArgs):
    cmd: str
    name: str

    @field_validator("cmd")
    @classmethod
    def _cmd_not_empty(cls, value: str) -> str:
        return _present(value, "cmd")

    @field_validator("name")
    @classmethod
    def _name_not_empty(cls, value: str) -> str:
        return _trimmed_nonempty(value, "name")


class BgCheckArgs(_ToolArgs):
    name: str

    @field_validator("name")
    @classmethod
    def _name_not_empty(cls, value: str) -> str:
        return _trimmed_nonempty(value, "name")


class HttpRequestArgs(_ToolArgs):
    url: str
    method: Optional[str] = None
    json_body: Optional[Any] = Field(default=None, validation_alias=AliasChoices("json", "json_body"))
    data: Optional[Union[str, bytes]] = None
    headers: dict[str, str] = Field(default_factory=dict)
    timeout: float = 30.0
    save: Optional[str] = None
    out: Optional[str] = None

    @field_validator("url")
    @classmethod
    def _url_not_empty(cls, value: str) -> str:
        return _trimmed_nonempty(value, "url")

    @field_validator("method")
    @classmethod
    def _method_allowed(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return value
        method = value.upper()
        if method not in {"GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"}:
            raise ValueError("method must be one of GET, POST, PUT, DELETE, PATCH, HEAD, OPTIONS")
        return method

    @field_validator("timeout")
    @classmethod
    def _timeout_is_sane(cls, value: float) -> float:
        if value <= 0:
            raise ValueError("timeout must be positive")
        return value

    @model_validator(mode="after")
    def _single_body_source(self) -> "HttpRequestArgs":
        if self.json_body is not None and self.data is not None:
            raise ValueError("provide either json or data, not both")
        return self


class UpdatePlanArgs(_ToolArgs):
    note: str

    @field_validator("note")
    @classmethod
    def _note_not_empty(cls, value: str) -> str:
        return _present(value, "note")


class MemorizeArgs(_ToolArgs):
    fact: str = Field(validation_alias=AliasChoices("fact", "value", "content", "knowledge"))
    tags: Optional[Union[list[str], str]] = None
    key: Optional[str] = None
    category: str = "facts"
    confidence: str = "tentative"
    source_goal: str = ""
    source_tick: int = 0

    @field_validator("fact")
    @classmethod
    def _fact_not_empty(cls, value: str) -> str:
        return _present(value, "fact")


class PredictArgs(_ToolArgs):
    """Pillars 4.1: a grammar-constrained typed bet. `statement` = what you expect in words;
    `target` = the MEASURABLE claim glue will adjudicate against; `deadline` = when it must resolve
    (a human hint like "02:30" or "in 2h" — parsed to an epoch by the handler); `confidence` in
    [0,1]. The grammar governs FORM; the handler + expectations.py enforce content + the bound."""
    statement: str = Field(validation_alias=AliasChoices("statement", "prediction", "claim"))
    target: str = Field(default="", validation_alias=AliasChoices("target", "measure", "metric"))
    deadline: str = Field(default="", validation_alias=AliasChoices("deadline", "by", "when"))
    confidence: float = 0.6
    domain: str = "general"

    @field_validator("statement")
    @classmethod
    def _statement_not_empty(cls, value: str) -> str:
        return _present(value, "statement")

    @field_validator("confidence")
    @classmethod
    def _confidence_in_unit(cls, value: float) -> float:
        v = float(value)
        if not (0.0 <= v <= 1.0):
            raise ValueError("confidence must be a number in [0, 1]")
        return v


class RecallArgs(_ToolArgs):
    query: str

    @field_validator("query")
    @classmethod
    def _query_not_empty(cls, value: str) -> str:
        return _present(value, "query")


class UpdateSelfGuideArgs(_ToolArgs):
    content: Optional[str] = None
    note: Optional[str] = Field(default=None, validation_alias=AliasChoices("note", "text"))
    rationale: str = Field(default="", validation_alias=AliasChoices("rationale", "reason"))
    source_tick: Optional[int] = None

    @model_validator(mode="after")
    def _has_content_or_note(self) -> "UpdateSelfGuideArgs":
        if not ((self.content and self.content.strip()) or (self.note and self.note.strip())):
            raise ValueError("provide content or note")
        return self


class ProposeSelfEditArgs(_ToolArgs):
    target_file: str = Field(validation_alias=AliasChoices("target_file", "path", "file"))
    new_content: str = Field(validation_alias=AliasChoices("new_content", "content"))
    rationale: str = Field(default="", validation_alias=AliasChoices("rationale", "reason"))
    source_tick: Optional[int] = None

    @field_validator("target_file")
    @classmethod
    def _target_not_empty(cls, value: str) -> str:
        return _trimmed_nonempty(value, "target_file")

    @field_validator("new_content")
    @classmethod
    def _content_not_empty(cls, value: str) -> str:
        return _present(value, "new_content")


class EmptyArgs(_ToolArgs):
    pass


class CreateSkillArgs(_ToolArgs):
    skill_name: str = Field(validation_alias=AliasChoices("skill_name", "name"))
    skill_code: str = Field(validation_alias=AliasChoices("skill_code", "code"))
    description: str = ""
    args_schema: dict[str, Any] = Field(default_factory=dict)

    @field_validator("skill_name")
    @classmethod
    def _skill_name_not_empty(cls, value: str) -> str:
        return _trimmed_nonempty(value, "skill_name")

    @field_validator("skill_code")
    @classmethod
    def _skill_code_not_empty(cls, value: str) -> str:
        return _present(value, "skill_code")


class EditSkillArgs(_ToolArgs):
    skill_name: str = Field(validation_alias=AliasChoices("skill_name", "name"))
    skill_code: str = Field(validation_alias=AliasChoices("skill_code", "code"))
    description: Optional[str] = None
    args_schema: Optional[dict[str, Any]] = None

    @field_validator("skill_name")
    @classmethod
    def _skill_name_not_empty(cls, value: str) -> str:
        return _trimmed_nonempty(value, "skill_name")

    @field_validator("skill_code")
    @classmethod
    def _skill_code_not_empty(cls, value: str) -> str:
        return _present(value, "skill_code")


class RollbackSkillArgs(_ToolArgs):
    skill_name: str = Field(validation_alias=AliasChoices("skill_name", "name"))
    version: str

    @field_validator("skill_name", "version")
    @classmethod
    def _required_not_empty(cls, value: str) -> str:
        return _trimmed_nonempty(value, "rollback field")


class NoteAppendArgs(_ToolArgs):
    name: str = Field(default="scratch", validation_alias=AliasChoices("name", "notebook"))
    text: str = Field(validation_alias=AliasChoices("text", "note", "content"))

    @field_validator("name")
    @classmethod
    def _note_name_not_empty(cls, value: str) -> str:
        return _trimmed_nonempty(value, "name")

    @field_validator("text")
    @classmethod
    def _note_text_not_empty(cls, value: str) -> str:
        return _present(value, "text")


class NoteReadArgs(_ToolArgs):
    name: Optional[str] = Field(default=None, validation_alias=AliasChoices("name", "notebook"))


class TcpProbeArgs(_ToolArgs):
    ip: str = Field(validation_alias=AliasChoices("ip", "host"))
    port: int = Field(default=80, ge=1, le=65535)
    timeout: float = Field(default=2.0, gt=0, le=60)

    @field_validator("ip")
    @classmethod
    def _ip_not_empty(cls, value: str) -> str:
        return _trimmed_nonempty(value, "ip")


class NetScanArgs(_ToolArgs):
    subnet: str = Field(validation_alias=AliasChoices("subnet", "base"))
    ports: Optional[Union[list[int], str]] = None
    timeout: float = Field(default=0.4, gt=0, le=30)
    start: int = Field(default=1, ge=1, le=254)
    end: int = Field(default=254, ge=1, le=254)

    @field_validator("subnet")
    @classmethod
    def _subnet_shape(cls, value: str) -> str:
        value = _trimmed_nonempty(value, "subnet").rstrip(".")
        if not re.fullmatch(r"\d{1,3}\.\d{1,3}\.\d{1,3}", value):
            raise ValueError("subnet must look like 192.168.1")
        return value

    @field_validator("ports")
    @classmethod
    def _ports_in_range(cls, value: Optional[Union[list[int], str]]) -> Optional[Union[list[int], str]]:
        if isinstance(value, list):
            for port in value:
                if port < 1 or port > 65535:
                    raise ValueError("ports must be between 1 and 65535")
        if isinstance(value, str):
            for match in re.findall(r"\d+", value):
                port = int(match)
                if port < 1 or port > 65535:
                    raise ValueError("ports must be between 1 and 65535")
        return value

    @model_validator(mode="after")
    def _range_ordered(self) -> "NetScanArgs":
        if self.start > self.end:
            raise ValueError("start must be <= end")
        return self


class HttpProbeArgs(_ToolArgs):
    url: Optional[str] = None
    ip: Optional[str] = Field(default=None, validation_alias=AliasChoices("ip", "host"))
    port: int = Field(default=80, ge=1, le=65535)
    path: str = "/"
    scheme: Literal["http", "https"] = "http"
    timeout: float = Field(default=4.0, gt=0, le=60)

    @model_validator(mode="after")
    def _has_url_or_ip(self) -> "HttpProbeArgs":
        if not (self.url or self.ip):
            raise ValueError("provide url or ip")
        return self


class UdpListenArgs(_ToolArgs):
    port: int = Field(default=6667, ge=1, le=65535)
    timeout: float = Field(default=6.0, gt=0, le=60)


class AskAiArgs(_ToolArgs):
    prompt: str = Field(validation_alias=AliasChoices("prompt", "question", "task", "text"))
    system: Optional[str] = None
    max_tokens: int = Field(default=800, ge=64, le=2048)

    @field_validator("prompt")
    @classmethod
    def _prompt_not_empty(cls, value: str) -> str:
        return _present(value, "prompt")


class VisionArgs(_ToolArgs):
    image: str = Field(validation_alias=AliasChoices("image", "url", "path", "file"))
    question: str = Field(
        default="Describe what you see in detail. Note anything notable, any text, and the overall scene.",
        validation_alias=AliasChoices("question", "prompt"),
    )

    @field_validator("image")
    @classmethod
    def _image_not_empty(cls, value: str) -> str:
        return _trimmed_nonempty(value, "image")


class SpeakArgs(_ToolArgs):
    text: str = Field(validation_alias=AliasChoices("text", "input", "say", "message"))

    @field_validator("text")
    @classmethod
    def _text_not_empty(cls, value: str) -> str:
        return _present(value, "text")


class ManualArgs(_ToolArgs):
    topic: Optional[str] = Field(
        default=None,
        validation_alias=AliasChoices("topic", "section", "query", "feature"),
    )


class ObjectiveAddArgs(_ToolArgs):
    title: str = Field(validation_alias=AliasChoices("title", "objective"))
    why: str = Field(validation_alias=AliasChoices("why", "because", "purpose"))
    priority: int = 5

    @field_validator("title", "why")
    @classmethod
    def _objective_field_not_empty(cls, value: str) -> str:
        return _present(value, "objective field")


class ObjectiveKeyArgs(_ToolArgs):
    id: str = Field(validation_alias=AliasChoices("id", "title", "objective"))

    @field_validator("id")
    @classmethod
    def _id_not_empty(cls, value: str) -> str:
        return _trimmed_nonempty(value, "id")


class ObjectiveBlockArgs(ObjectiveKeyArgs):
    reason: str = "blocked"
    wake: str = Field(default="", validation_alias=AliasChoices("wake", "wake_condition"))
    dead: bool = False


class DelegateArgs(_ToolArgs):
    task: Optional[str] = None
    mode: Literal["research", "code"] = "research"
    cwd: Optional[str] = None
    name: Optional[str] = None
    continue_job: Optional[str] = None

    @field_validator("mode", mode="before")
    @classmethod
    def _mode_lower(cls, value: Any) -> Any:
        return value.strip().lower() if isinstance(value, str) else value

def _format_validation_error(error: ValidationError) -> str:
    parts = []
    for entry in error.errors():
        loc = ".".join(str(item) for item in entry.get("loc", ())) or "args"
        parts.append(f"{loc}: {entry.get('msg', 'invalid value')}")
    return "; ".join(parts)


def _validate_tool_args(model: type[_ToolArgs], args: dict, tool_name: str) -> _ToolArgs | ToolResult:
    try:
        return model.model_validate(args or {})
    except ValidationError as error:
        return ToolResult(
            output=f"Error: invalid {tool_name} arguments: {_format_validation_error(error)}",
            full_output_path=None,
            success=False,
            duration_s=0,
            fail_kind="args",
        )


def _save_full_output(config: Config, tick_id: str, stream: str, content: str) -> str:
    """Save full output to disk, return the path."""
    config.outputs_dir.mkdir(parents=True, exist_ok=True)
    path = config.outputs_dir / f"{tick_id}_{stream}.txt"
    path.write_text(content)
    return str(path)


def _truncate(text: str, limit: int, full_path: Optional[str], creature: bool = False) -> str:
    """Truncate text to limit chars, appending a pointer to the full output. For the creature we never
    name the on-disk overflow file (it lives in the skeleton, outside its world — a path it can't reach
    would only confuse it); we just tell it the rest was trimmed and to narrow down."""
    if len(text) <= limit:
        return text
    if creature:
        return text[:limit] + f"\n[trimmed to {limit} chars — narrow it down (grep/head) to see more]"
    suffix = f"\n[truncated at {limit} chars"
    if full_path:
        suffix += f", full output: {full_path}"
    suffix += "]"
    return text[:limit] + suffix


def _read_text_robust(path: Path) -> str:
    """Read a text file that may NOT be UTF-8 — a read must never fail on encoding. The creature writes
    files via bash too, and Windows PowerShell 5.1's Out-File / `>` default to UTF-16LE; cp1252 also
    happens. Detect a BOM, else decode UTF-8 replacing stray bytes. This dissolves the "wrote a file I
    can't read back" trap that made it loop ~100 ticks hunting one file."""
    data = path.read_bytes()
    if data[:2] in (b"\xff\xfe", b"\xfe\xff"):
        return data.decode("utf-16", errors="replace")     # codec reads the BOM for endianness + strips it
    if data.startswith(b"\xef\xbb\xbf"):
        return data.decode("utf-8-sig", errors="replace")
    return data.decode("utf-8", errors="replace")


def _win_to_wsl_path(winpath: str) -> str:
    """C:\\Users\\x  ->  /mnt/c/Users/x  (WSL's auto-mount of the Windows drive)."""
    p = str(winpath).replace("\\", "/")
    m = re.match(r"^([A-Za-z]):/(.*)$", p)
    if m:
        return f"/mnt/{m.group(1).lower()}/{m.group(2)}"
    return p


# The creature's home burrow — the ONE name for the subdir inside the workspace that is its whole world.
CREATURE_HOME_DIRNAME = "home"


def _creature_root(config: Config) -> Path:
    """The single source of truth for the creature's reachable world.

    A creature lives in its burrow — `workspace/home/` — which holds ONLY the things it makes. All the
    platform's bookkeeping (logs, heartbeat, the knowledge index, episodic vectors, persona/creature
    json, jobs ledger, outputs) lives one level up in the workspace and is its *skeleton*: the quiet
    machinery it runs on, never a place it goes. By rooting the creature at home, that skeleton sits
    OUTSIDE its reachable root and is hidden by the very same containment rule that hides the source
    tree — one boundary, not a growing blocklist. The creature reaches its memory through memorize/
    recall, never by reading those files. The house-AI eidos is not a creature and uses the full
    workspace unchanged."""
    if getattr(config, "creature_mode", False):
        home = Path(config.workspace_dir) / CREATURE_HOME_DIRNAME
        try:
            home.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass
        return home
    return Path(config.workspace_dir)


def _normalize_workspace_path(path: str, config: Config) -> Path:
    """Resolve a relative file path against the creature's reachable root (its home burrow, or the full
    workspace for the house-AI), stripping a redundant leading root-name the model sometimes prepends.

    The model often echoes its own location back ("home/foo", or "workspace/foo" from the old prompt),
    which would nest a second level (home/home/foo). Strip the leading root dir name to avoid that.
    """
    root = _creature_root(config)
    p = Path(path)
    if not p.is_absolute():
        parts = p.parts
        # Strip a leading echo of the root dir name (e.g. "home/foo" -> "foo", "workspace/foo" -> "foo").
        if parts and parts[0] in (root.name, Path(config.workspace_dir).name):
            p = Path(*parts[1:]) if len(parts) > 1 else Path(".")
        p = root / p
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
            # POSIX: kill the whole process GROUP, not just the bash leader — the Popens set
            # start_new_session=True so bash is a session/group leader, and its children (a slow
            # pipeline, a backgrounded probe) must die with it or they orphan and hold the pipe.
            try:
                os.killpg(os.getpgid(pid), 9)
            except (ProcessLookupError, PermissionError):
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


# Listing commands the model reaches for, and the POSIX short-flag letters it bundles out of habit
# (ls -F / ls -la / ls -lh …) that PowerShell's `ls`/`dir` (= Get-ChildItem) does NOT accept. Only these
# letters count as a strippable bundle, so real PS switches (-Recurse, -Force, -Name…) are never touched.
_LISTING_CMDS = ("ls", "dir", "gci", "get-childitem")
_POSIX_LS_BUNDLE = re.compile(r"-[laAFhGtSrR1]+$")


def _normalize_listing_flags(c: str):
    """Translate Unix/cmd listing flags to PowerShell so the creature stops bonking on shell dialect:
      ls -F → ls   |   ls -la → ls -Force   |   ls -R → ls -Recurse   |   dir /s *.json → dir *.json -Recurse
    Conservative: only acts when the command STARTS with a listing command, only on the segment before
    the first pipe, and only on known POSIX/cmd flag tokens — paths, globs, and real PS switches pass
    through untouched. Returns (new_cmd, note) or None."""
    head = c.split("|", 1)[0]
    tail = c[len(head):]
    toks = head.split()
    if not toks or toks[0].lower() not in _LISTING_CMDS:
        return None
    out, changed, recurse, force = [toks[0]], False, False, False
    for t in toks[1:]:
        tl = t.lower()
        if tl.startswith("--color"):                       # --color / --color=auto
            changed = True
            continue
        if t.startswith("/"):                              # cmd.exe switches: /s /b /a /o /w …
            changed = True
            if tl == "/s":
                recurse = True
            elif tl.startswith("/a"):
                force = True
            continue                                       # /b /o /w /p /q → drop (no PS equivalent needed)
        if _POSIX_LS_BUNDLE.fullmatch(t):                  # -F / -la / -lhR …
            if "r" in tl[1:]:
                recurse = True
            if "a" in tl[1:]:
                force = True
            changed = True
            continue
        out.append(t)
    if not changed:
        return None
    if recurse:
        out.append("-Recurse")
    if force:
        out.append("-Force")
    return (" ".join(out) + tail,
            "translated Unix/cmd listing flags to PowerShell (dropped POSIX flags; -R→-Recurse, -a→-Force)")


def _normalize_select_string(c: str):
    """Select-String treats its pattern as a REGEX, so a Windows path / literal with backslashes blows
    up ("the regex engine keeps tripping over those backslashes"). When the creature pipes to
    Select-String with a backslash in the pattern and hasn't asked for regex, add -SimpleMatch so the
    match is literal. Returns (new_cmd, note) or None."""
    low = c.lower()
    i = low.find("select-string")
    if i == -1:
        return None
    if "-simplematch" in low or "-pattern" in low:
        return None                       # already explicit about how to match
    if "\\" not in c[i:]:
        return None                       # no backslash in the pattern region → regex is fine
    end = i + len("select-string")
    return (c[:end] + " -SimpleMatch" + c[end:],
            "added -SimpleMatch to Select-String so backslashes match literally, not as regex")


def _lint_windows_command(cmd: str):
    """Quote-aware pre-flight. See contract above."""
    if os.name != "nt":   # defense-in-depth: never rewrite a POSIX command into PowerShell
        return None
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
    # 4) Unix/cmd listing flags (ls -F, ls -la, dir /s) that PowerShell's Get-ChildItem rejects.
    listing = _normalize_listing_flags(c)
    if listing:
        return ("rewrite", listing[0], listing[1])
    # 5) Select-String with a backslash pattern → regex tripwire; make it a literal match.
    sls = _normalize_select_string(c)
    if sls:
        return ("rewrite", sls[0], sls[1])
    return None


def _route_windows_command(cmd: str):
    """(popen_arg, use_shell) for a Windows command. Always prefer list-form PowerShell so
    cmd.exe never re-parses (and corrupts) the model's PowerShell."""
    if os.name != "nt":   # defense-in-depth: PowerShell routing must never reach a POSIX command
        return cmd, True
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


def _creature_uses_wsl(config: Config) -> bool:
    """The creature runs its bash in WSL2 (real Linux) when configured — working WITH the model's bash
    fluency instead of translating to PowerShell. Creature-only, Windows-only."""
    return (os.name == "nt"
            and getattr(config, "creature_mode", False)
            and str(getattr(config, "creature_shell", "powershell")).lower() == "wsl")


def _wsl_popen(cmd: str, config: Config) -> list:
    """List-form WSL invocation: run the creature's bash inside WSL, cwd'd to the workspace mount, so it
    uses real Linux commands + short relative paths. No cmd.exe/PowerShell in the path → no quote hell."""
    distro = getattr(config, "creature_wsl_distro", "Ubuntu-24.04")
    wslws = _win_to_wsl_path(str(_creature_root(config).resolve()))
    return ["wsl.exe", "-d", distro, "--cd", wslws, "-e", "bash", "-lc", cmd]


# Path tokens that can reach OUT of the workspace: a Windows drive/UNC path, a POSIX absolute path (WSL:
# /mnt/c/… , /etc/… ), a `~` home escape, and an upward `..` traversal.
_FW_ABS_PATH = re.compile(r'(?:[A-Za-z]:[\\/]|\\\\)[^\s"\';|&)>]*')
_FW_POSIX_ABS = re.compile(r"""(?:^|[\s"'(=,|&;:])(/[^\s"';|&)>]*)""")
_FW_HOME = re.compile(r"""(?:^|[\s"'(=,|&;])~(?=/|\s|$)""")
_FW_UP_TRAVERSAL = re.compile(r'(?<![.\w])\.\.(?![.\w])')


def _creature_world_firewall(cmd: str, config: Config) -> Optional[str]:
    """Creature mode only: bash runs IN the creature's home burrow — its whole world. EVERYTHING outside
    that root is skeleton: its source tree (its biology) AND the workspace's own bookkeeping (logs,
    heartbeat, the knowledge index, episodic vectors, persona/creature json) that live one level up. An
    organism doesn't reach into its own wiring and read it, so deny any command that reaches OUTSIDE the
    home root. One boundary hides both — not a growing blocklist. Covers BOTH shells: PowerShell
    (`C:\\…\\Kairos\\eidos.py`) and WSL (`/mnt/c/…/persona.json`, `/etc/…`, `~/…`, `../llm_log.jsonl`) —
    WSL must never be an escape hatch. Heuristic + gentle (the creature isn't an adversary; read/write
    are home-confined too). Returns the offending path/token to deny, or None to allow."""
    if not getattr(config, "creature_mode", False):
        return None
    try:
        ws = _creature_root(config).resolve()
    except Exception:  # noqa: BLE001
        return None
    if _FW_UP_TRAVERSAL.search(cmd):
        return ".."
    if _FW_HOME.search(cmd):
        return "~"
    # Windows absolute paths (must resolve under the home root).
    for m in _FW_ABS_PATH.finditer(cmd):
        raw = m.group(0).rstrip("\\/.,;:)")
        try:
            p = Path(raw).resolve()
        except Exception:  # noqa: BLE001
            continue
        if p != ws and ws not in p.parents:
            return raw
    # POSIX absolute paths (WSL): must sit under the home root's /mnt mount. String-prefix check, since
    # /mnt paths don't resolve on Windows; `..` is already denied above so the prefix can't be fooled.
    wslws = _win_to_wsl_path(str(ws)).lower().rstrip("/")
    for m in _FW_POSIX_ABS.finditer(cmd):
        tok = m.group(1).rstrip("/.,;:)").lower()
        if tok == wslws or tok.startswith(wslws + "/"):
            continue
        return m.group(1)
    return None


def tool_bash(args: dict, config: Config) -> ToolResult:
    """Run a shell command; fast commands return inline, slow ones auto-background."""
    parsed = _validate_tool_args(BashArgs, args, "bash")
    if isinstance(parsed, ToolResult):
        return parsed
    cmd = parsed.cmd

    # Creature world-boundary: a creature can't reach outside its home burrow into its skeleton (its
    # source/biology AND the workspace bookkeeping one level up).
    _outside = _creature_world_firewall(cmd, config)
    if _outside is not None:
        return ToolResult(
            output=("That's outside your world — there's nothing out there you can reach. Everything "
                    "that's yours is right here at home; the rest is just the quiet machinery you run "
                    "on, not a place to go. Stay home and poke around what's yours."),
            full_output_path=None, success=False, duration_s=0, fail_kind="blocked")

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

    _wsl = _creature_uses_wsl(config)

    # Pre-flight (quote-aware, never lies). Auto-correct the fixable cases and RUN; only hard-block
    # the truly unfixable ones (with the PowerShell equivalent). A prepended note tells the model
    # what was changed so it learns — never a dead-end "NOT RUN" wall it just bounces off.
    # Skipped entirely in WSL mode: the model's bash-isms (ls -F, grep, for…do…done) are NATIVE there.
    _lint_note = ""
    if os.name == "nt" and not _wsl:
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
        # Native shell per platform (platform_shell): PowerShell list-form on Windows (so cmd.exe never
        # re-parses the model's PowerShell), the WSL bash invocation for a creature-in-WSL, or `bash -lc`
        # on macOS/Linux/Pi where the model's commands are already native. Returns the epilogue that
        # records the real exit code to a sidecar (truthful exit codes for async jobs).
        popen_arg, use_shell, _epilogue = platform_shell.build_shell_command(cmd, config)
        # Stream output to a file (not an in-memory PIPE) so a slow command can be
        # handed to the background ledger mid-run WITHOUT losing its output or killing it.
        config.outputs_dir.mkdir(parents=True, exist_ok=True)
        out_path = config.outputs_dir / f"fg_{time.strftime('%Y%m%d_%H%M%S')}_{proc_seq()}.out"
        exit_path = str(out_path) + ".exit"
        # List-form shells get the exit-code epilogue: it records the real exit code to a sidecar file
        # (readable after the Popen handle is gone — async jobs) and re-raises it via `exit` so
        # proc.returncode stays truthful (sync). Commands that exit the script explicitly, or the
        # operator's own `cmd.exe`/`-File` (epilogue is None), skip it: sidecar absent = exit unknown
        # (old behavior), never wrong.
        if _epilogue is not None and isinstance(popen_arg, list):
            popen_arg = _epilogue(popen_arg, exit_path)
        out_file = open(out_path, "w", encoding="utf-8", errors="replace")
        try:
            proc = subprocess.Popen(
                popen_arg,
                shell=use_shell,
                stdout=out_file,
                stderr=subprocess.STDOUT,
                text=True,
                cwd=str(_creature_root(config)),
                **popen_kwargs,
            )
            # ASYNC BY DEFAULT (fire-and-forget). The command runs in the background and
            # its result is delivered to the LLM later, tagged [↩ job N], via the jobs
            # ledger. The loop is NEVER blocked. The model can pass "wait": true to force
            # the synchronous path below when it truly needs the result this same tick.
            if not parsed.wait:
                jobname = (str(parsed.name or "").strip() or f"j{proc.pid}")
                intent = str(parsed.intent or "").strip()
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
                # §0: the check-hint names bg_check only where bg_check exists (house mode).
                _check_hint = (f", or check it with bg_check {{\"name\":\"{jobname}\"}}"
                               if not _ladder_active(config) else "")
                return ToolResult(
                    output=(f"AUTO-BACKGROUNDED after {config.cmd_timeout_s}s: still running, so it "
                            f"was moved to the background as job '{jobname}' (PID {proc.pid}). The "
                            f"loop is NOT blocked — go do other work now; the result will arrive "
                            f"tagged [↩ job {jobname}]{_check_hint}."),
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

        output = _truncate(combined, config.output_truncation_chars, full_path,
                           creature=getattr(config, "creature_mode", False))

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
    parsed = _validate_tool_args(WriteFileArgs, args, "write_file")
    if isinstance(parsed, ToolResult):
        return parsed
    path = parsed.path
    content = parsed.content

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
        workspace_resolved = _creature_root(config).resolve()
        if not str(resolved).startswith(str(workspace_resolved) + os.sep) and resolved != workspace_resolved:
            _msg = ("Error: that's outside your world — keep to your own things, named the short way (notes.txt)."
                    if getattr(config, "creature_mode", False) else "Error: path escapes workspace directory")
            return ToolResult(output=_msg, full_output_path=None, success=False, duration_s=time.monotonic() - start)
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
        # EXPLICIT utf-8 (Windows default cp1252 silently corrupts unicode + breaks read-back) and
        # newline="" so text mode does NOT translate \n -> \r\n: the file must byte-match what was
        # written, or read_file round-trips with stray \r and WSL bash (grep / string `=`) breaks on it.
        resolved.write_text(content, encoding="utf-8", newline="")
        duration = time.monotonic() - start
        return ToolResult(output=f"Written {len(content)} chars to {path}", full_output_path=None, success=True, duration_s=duration)
    except OSError as e:
        return ToolResult(output=f"Error writing file: {e}", full_output_path=None, success=False, duration_s=time.monotonic() - start)


def tool_read_file(args: dict, config: Config) -> ToolResult:
    """Read a file's contents."""
    parsed = _validate_tool_args(ReadFileArgs, args, "read_file")
    if isinstance(parsed, ToolResult):
        return parsed
    path = parsed.path

    start = time.monotonic()
    try:
        p = _normalize_workspace_path(path, config)
        # Prevent path traversal outside workspace
        resolved = p.resolve()
        workspace_resolved = _creature_root(config).resolve()
        if not str(resolved).startswith(str(workspace_resolved) + os.sep) and resolved != workspace_resolved:
            _msg = ("Error: that's outside your world — keep to your own things, named the short way (notes.txt)."
                    if getattr(config, "creature_mode", False) else "Error: path escapes workspace directory")
            return ToolResult(output=_msg, full_output_path=None, success=False, duration_s=time.monotonic() - start)
        content = _read_text_robust(resolved)   # BOM-aware + errors='replace' — a read NEVER fails on
        tick_id = time.strftime("%Y%m%d_%H%M%S")  # encoding (PowerShell writes UTF-16; cp1252 happens too)
        full_path = None
        if len(content) > config.output_truncation_chars:
            full_path = _save_full_output(config, tick_id, "read", content)
        output = _truncate(content, config.output_truncation_chars, full_path,
                           creature=getattr(config, "creature_mode", False))
        return ToolResult(output=output, full_output_path=full_path, success=True, duration_s=time.monotonic() - start)
    except OSError as e:
        return ToolResult(output=f"Error reading file: {e}", full_output_path=None, success=False, duration_s=time.monotonic() - start)


def tool_bg_run(args: dict, config: Config) -> ToolResult:
    """Spawn a background job and register it in the jobs ledger."""
    parsed = _validate_tool_args(BgRunArgs, args, "bg_run")
    if isinstance(parsed, ToolResult):
        return parsed
    cmd = parsed.cmd
    name = parsed.name

    # Creature world-boundary (same as tool_bash — bg_run was an unfirewalled escape vector).
    _outside = _creature_world_firewall(cmd, config)
    if _outside is not None:
        return ToolResult(output=("That's outside your world — stay home in your workspace; the rest is "
                                  "just the quiet machinery you run on."),
                          full_output_path=None, success=False, duration_s=0, fail_kind="blocked")

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
        # Native shell per platform (same seam as the foreground path). bg jobs read their output file +
        # the jobs ledger for status, so we don't attach the exit-code epilogue here (parity with the
        # prior behavior). WSL runs cwd'd via --cd inside the invocation, so its cwd is None.
        _bg_arg, _bg_shell, _ = platform_shell.build_shell_command(cmd, config)
        _bg_cwd = None if _creature_uses_wsl(config) else str(_creature_root(config))
        with open(out_path, "w") as out_file:
            proc = subprocess.Popen(
                _bg_arg,
                shell=_bg_shell,
                stdout=out_file,
                stderr=subprocess.STDOUT,
                cwd=_bg_cwd,
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
    parsed = _validate_tool_args(BgCheckArgs, args, "bg_check")
    if isinstance(parsed, ToolResult):
        return parsed
    name = parsed.name

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
        try:
            stat = Path(f"/proc/{pid}/stat").read_text(encoding="ascii", errors="ignore")
            if stat.rsplit(")", 1)[-1].strip().startswith("Z"):
                return False
        except OSError:
            pass
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
        return validate_job_records(data)
    except (FileNotFoundError, json.JSONDecodeError, OSError, ValueError, MemoryError):
        return []


def _write_jobs(config: Config, jobs: list[dict]) -> None:
    config.workspace.mkdir(parents=True, exist_ok=True)
    config.jobs_path.write_text(json.dumps(validate_job_records(jobs), indent=2))


_JOB_DONE = ("completed", "failed", "timed_out", "reaped")


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
                if j.get("kind") == "delegate":
                    # Delegates get their own (longer) watchdog ceiling — a coding-agent
                    # run is supposed to take minutes; bg_job_max_age_s stays the backstop.
                    dlg_cap = float(getattr(config, "delegate_timeout_s", 600.0))
                    over_ceiling = bool(started_ts and (now - started_ts) > dlg_cap)
                else:
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
            # Deliver finished async/auto/delegate jobs exactly once. Delegates are also
            # delivered when "reaped" (a restart killed them mid-run) so the model learns
            # the session survived and can continue_job — instead of the job silently vanishing.
            _deliver = ("completed", "failed", "timed_out", "reaped") \
                if j.get("kind") == "delegate" else ("completed", "failed", "timed_out")
            if (j.get("kind") in ("async", "auto", "delegate")
                    and j.get("status") in _deliver
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
        try:
            from skill_atoms import atoms_reference
            _atoms = "\n" + atoms_reference()
        except Exception:  # noqa: BLE001
            _atoms = ""
        return ToolResult(
            output=("Error: provide 'skill_name' and 'skill_code'. The code MUST define:\n"
                    "def tool_<skill_name>(args: dict, config: Config) -> ToolResult\n" + _atoms),
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
        if re.search(r"\bimport\s+(requests|httpx|aiohttp|urllib|socket|http)\b", code):
            nudge += ("\n➤ You don't need that import — COMPOSE the in-scope ATOMS instead (reliable, no "
                      "imports): http_get/http_post/json_parse for HTTP, net_scan/tcp_probe/http_probe for "
                      "the LAN, sh for shell, recall/memorize/note for memory, look for vision.")
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
        # §0: the authoring hint may only name create_skill where create_skill exists. With the
        # ladder off, visible_tools IS the registry and the line stays byte-identical.
        if "create_skill" in visible_tools(config):
            lines.append("\nYOUR SKILLS: (none yet — author one with create_skill)")
        else:
            lines.append("\nYOUR SKILLS: (none yet)")
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
    parsed = _validate_tool_args(HttpRequestArgs, args, "http_request")
    if isinstance(parsed, ToolResult):
        return parsed
    url = parsed.url.strip()
    headers = dict(parsed.headers)
    headers.setdefault("User-Agent", "eiDOS/1.0")
    body = None
    if parsed.json_body is not None:
        try:
            body = _json.dumps(parsed.json_body).encode("utf-8")
        except Exception as e:  # noqa: BLE001
            return ToolResult(output=f"Error: 'json' is not serializable: {e}",
                              full_output_path=None, success=False, duration_s=0)
        headers.setdefault("Content-Type", "application/json")
    elif parsed.data is not None:
        d = parsed.data
        body = d.encode("utf-8") if isinstance(d, str) else bytes(d)
    method = parsed.method or ("POST" if body is not None else "GET")
    timeout = max(1.0, min(parsed.timeout, 120.0))

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
        out = _truncate(text, config.output_truncation_chars, full_path,
                        creature=getattr(config, "creature_mode", False))
        return ToolResult(output=f"HTTP {status} · {ctype or 'no-type'} · {len(raw)}B\n{out}",
                          full_output_path=full_path, success=ok, duration_s=dur)
    # binary → save to a file (in the creature's home so the path it's told is one it can actually reach)
    _root = _creature_root(config)
    ext = {"audio/wav": ".wav", "audio/x-wav": ".wav", "audio/mpeg": ".mp3", "audio/mp3": ".mp3",
           "image/jpeg": ".jpg", "image/png": ".png", "image/gif": ".gif", "application/pdf": ".pdf"}.get(
               ct.split(";")[0].strip(), ".bin")
    save = (parsed.save or parsed.out or "").strip()
    if save:
        p = save if os.path.isabs(save) else str(_root / save)
    else:
        p = str(_root / f"http_{time.strftime('%Y%m%d_%H%M%S')}{ext}")
    try:
        with open(p, "wb") as f:
            f.write(raw)
    except Exception as e:  # noqa: BLE001
        return ToolResult(output=f"HTTP {status} {ctype} {len(raw)}B but save failed: {e}",
                          full_output_path=None, success=False, duration_s=dur)
    _shown = os.path.basename(p) if getattr(config, "creature_mode", False) else p
    return ToolResult(output=f"HTTP {status} · {ctype} · {len(raw)}B binary saved to {_shown}",
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
    # Mirror every spoken call-out into the operator chat so Boss SEES what was said aloud, not just
    # hears it — voice and chat should never diverge. append_chat_line dedups against a same-tick
    # <reply> of the same line, so reply-and-speak shows as ONE entry (marked spoken), never a dup.
    try:
        from memory import append_chat_line
        append_chat_line(config, text, spoken=True)
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


# Each manual topic page, gated by the ONE tool it teaches (§0: the manual has no page for a tool
# that does not exist in the creature's world; house mode shows everything). Declared order = the
# manual's own. `cpu` rides on bg_run and `devices` on http_probe — pages built on primitives the
# creature universe excludes entirely, so in creature mode those pages never render.
_MANUAL_TOPIC_GATES: dict[str, str] = {
    "tts": "speak", "vision": "vision", "ask_ai": "ask_ai", "network": "net_scan",
    "devices": "http_probe", "cpu": "bg_run", "delegate": "delegate",
}


def _visible_manual_topics(config):
    """The topic pages that exist in this world — None means NO filtering (house mode / ladder
    off), keeping every render byte-identical to the pre-ladder behavior."""
    if not _ladder_active(config):
        return None
    vis = visible_tools(config)
    return {t for t, gate in _MANUAL_TOPIC_GATES.items() if gate in vis}


def _manual_section_topic(s: str) -> str:
    """The topic word of a '## <topic> — ...' manual section ('' for the preamble)."""
    first = s.strip().splitlines()[0] if s.strip() else ""
    return first[3:].split()[0].strip().lower() if first.startswith("## ") and first[3:].split() else ""


def tool_manual(args: dict, config: Config) -> ToolResult:
    """Read your OPERATING MANUAL — tested how-to (exact endpoints, payloads, working examples) for your
    big-lift features. Pass 'topic' (tts/vision/ask_ai/network/devices/cpu/delegate) for one section, or nothing
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
    allowed = _visible_manual_topics(config)   # None = house mode / ladder off: no filtering
    if allowed is not None:
        # The footer coaches a self-edit workflow that is not part of the creature universe (§0).
        text = re.sub(r"\n---\n\n_When something here.*$", "", text, flags=re.S)
    sections = re.split(r"\n(?=## )", text)
    if not topic:
        if allowed is None:
            return ToolResult(output=text, full_output_path=None, success=True, duration_s=0)
        parts = [s.strip() for s in sections if _manual_section_topic(s) in allowed]
        return ToolResult(output=("\n\n".join(parts) if parts else "The manual has no pages yet."),
                          full_output_path=None, success=True, duration_s=0)
    syn = {"speak": "tts", "voice": "tts", "say": "tts", "audio": "tts",
           "see": "vision", "image": "vision", "look": "vision",
           "think": "ask_ai", "ai": "ask_ai", "reason": "ask_ai", "summarize": "ask_ai",
           "scan": "network", "lan": "network", "discover": "network",
           "device": "devices", "camera": "devices", "cameras": "devices", "tuya": "devices",
           "plug": "devices", "printer": "devices", "octoprint": "devices",
           "background": "cpu", "worker": "cpu", "script": "cpu", "bash": "cpu",
           "pi": "delegate", "coder": "delegate", "agent": "delegate",
           "offload": "delegate", "handoff": "delegate", "coding": "delegate"}
    want = syn.get(topic, topic)
    # §0 indistinguishability: a topic whose page does not exist in this world answers EXACTLY
    # like a topic that never existed — same wording, and the topic list names only what IS.
    topics_s = (", ".join(t for t in _MANUAL_TOPIC_GATES if allowed is None or t in allowed)
                or "(none)")
    no_match = ToolResult(
        output=f"No manual section matched '{topic}'. Topics: {topics_s}. "
               f"Call manual {{}} (no topic) for the whole manual.",
        full_output_path=None, success=True, duration_s=0)
    if allowed is not None and want not in allowed:
        return no_match
    for s in sections:
        first = s.strip().splitlines()[0].lower() if s.strip() else ""
        if first.startswith(f"## {want}"):
            return ToolResult(output=s.strip(), full_output_path=None, success=True, duration_s=0)
    for s in sections:  # fallback: keyword anywhere in a section (only sections of this world)
        if allowed is not None and _manual_section_topic(s) not in allowed:
            continue
        if want in s.lower():
            return ToolResult(output=s.strip(), full_output_path=None, success=True, duration_s=0)
    return no_match


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


def tool_delegate(args: dict, config: Config) -> ToolResult:
    """HAND OFF a hard multi-step job to your CODING AGENT (a full read/bash/edit/write agent
    running on your own mind, with its own large context). It works in the BACKGROUND for
    minutes and the result returns tagged [↩ delegate N]. Use it when a task needs more than
    2-3 ticks of real work, when the same approach keeps failing, or for multi-file edits and
    real investigations. args: {"task": "<self-contained brief: goal + constraints + what you
    tried>", "mode": "research"|"code", "cwd"?: path, "name"?: short_id, "continue_job"?: id}.
    The agent has NONE of your context — write the task like a brief to a contractor."""
    import delegate
    return delegate.tool_delegate(args, config)


# Pillars 4.1 default horizon: a bet with no parseable deadline resolves in this many seconds. One
# hour is the "next few ticks-to-hours" default — long enough that a same-tick close is the exception,
# short enough that an unresolved bet doesn't clog the bounded ledger indefinitely.
_PREDICT_DEFAULT_HORIZON_S = 3600.0


def _parse_deadline(raw: str) -> float:
    """Best-effort parse of a deadline hint into an epoch second. Accepts a relative offset ("in 2h",
    "30m", "90s") or a bare number of seconds; anything unparseable falls back to now + the default
    horizon (a bet always has a resolvable deadline). Absolute clock times are left to a later parser —
    the deadline just has to be a FUTURE epoch glue can compare against, not calendar-perfect."""
    now = time.time()
    s = (raw or "").strip().lower()
    if not s:
        return now + _PREDICT_DEFAULT_HORIZON_S
    m = re.search(r"(\d+(?:\.\d+)?)\s*([smhd])", s)
    if m:
        n = float(m.group(1))
        mult = {"s": 1.0, "m": 60.0, "h": 3600.0, "d": 86400.0}[m.group(2)]
        return now + n * mult
    try:
        return now + float(s)   # a bare number = seconds from now
    except ValueError:
        return now + _PREDICT_DEFAULT_HORIZON_S


def tool_predict(args: dict, config: Config) -> ToolResult:
    """Commit a TYPED, measurable BET about what will happen — "backup done by 02:30", "the scan
    finds a new host in 5m". args: {"statement": what you expect, "target": the measurable claim,
    "deadline": when it must resolve (e.g. "in 2h"), "confidence": 0..1, "domain"?: bucket}. GLUE — not
    your word — later scores it; a confident-wrong bet is the most valuable memory you can make. The
    open-bet ledger is BOUNDED; if it's full, an old bet must close before you make a new one."""
    if not getattr(config, "pillars_expectations_enabled", False):
        # DARK: unreachable in practice (the tool isn't registered when the flag is off) — this is the
        # belt-and-suspenders guard so a direct dispatch can't create a bet while the organ is dark.
        return ToolResult(output="The prediction organ is not enabled.", full_output_path=None,
                          success=False, duration_s=0, fail_kind="blocked")
    import expectations
    statement = (args.get("statement") or "").strip()
    if not statement:
        return ToolResult(output="Error: provide a 'statement' — what you expect to happen.",
                          full_output_path=None, success=False, duration_s=0, fail_kind="args")
    try:
        confidence = float(args.get("confidence", expectations.DEFAULT_CONFIDENCE))
    except (TypeError, ValueError):
        confidence = expectations.DEFAULT_CONFIDENCE
    confidence = max(0.0, min(1.0, confidence))
    deadline = _parse_deadline(str(args.get("deadline", "")))
    ledger = expectations.ExpectationLedger(config)
    try:
        p = ledger.predict(statement=statement, target=(args.get("target") or "").strip(),
                           deadline=deadline, confidence=confidence,
                           domain=(args.get("domain") or "general").strip() or "general")
    except ValueError as e:
        # The bound (ledger full) or an empty statement — a clean, typed refusal, not a crash.
        return ToolResult(output=f"Prediction refused: {e}", full_output_path=None,
                          success=False, duration_s=0, fail_kind="blocked")
    when = max(0, int(deadline - time.time()))
    return ToolResult(
        output=(f"Bet placed: \"{p.statement}\" (target: {p.target or '—'}; confidence "
                f"{p.confidence:.0%}; resolves in ~{when}s). Glue will score it — not your word."),
        full_output_path=None, success=True, duration_s=0)


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
    # Delegate — hand a long-horizon coding/investigation task to the pi coding agent
    # (background job on the shared ledger; result returns tagged [↩ delegate N])
    "delegate": tool_delegate,
}


def register_predict_tool(config: Config) -> bool:
    """Pillars 4.1 (DARK by default): register the grammar-constrained `predict` tool ONLY when
    `pillars_expectations_enabled` is on. Idempotent. Called by the loop wiring once config is loaded;
    with the flag off the tool is absent from TOOLS, so it never enters the tick grammar and the organ
    stays fully dark. Returns True if `predict` is present after the call."""
    if getattr(config, "pillars_expectations_enabled", False):
        TOOLS.setdefault("predict", tool_predict)
        _TOOL_ARG_MODELS.setdefault("predict", PredictArgs)
    else:
        TOOLS.pop("predict", None)
    return "predict" in TOOLS


# Built-in tool names, snapshotted at import time — BEFORE any self-authored skill is hot-loaded into
# TOOLS (skills.py adds them at runtime via TOOLS[name] = runner). Anything dispatched whose name is
# NOT in this set is therefore a skill, and gets the wall-clock watchdog below.
_BUILTIN_TOOL_NAMES = frozenset(TOOLS)

# Builtins that join TOOLS only AFTER import, behind their own flag — the snapshot above misses
# them, but they are still PLATFORM tools (lockable units), never a creature's self-authored skill.
# `predict` registers via register_predict_tool (4.1) and belongs to the foresight unit.
_FLAG_REGISTERED_BUILTINS = frozenset({"predict"})
_EVER_BUILTIN_NAMES = _BUILTIN_TOOL_NAMES | _FLAG_REGISTERED_BUILTINS

# Registry aliases: alias name -> the canonical tool it rides on. An alias exists exactly when its
# canonical tool exists — aliases travel with their organ (TOOL_PROGRESSION: "aliases/satellites
# travel together"). `check_tools` is deliberately NOT here: the unit table makes it its own
# newborn organ (proprioception), not a satellite of list_skills, even though they share a handler.
TOOL_ALIASES: dict[str, str] = {
    "fetch": "http_request",
    "http": "http_request",
    "see": "vision",
}


def _ladder_active(config) -> bool:
    """True when the tool-progression ladder governs what exists in the creature's world
    (creature mode AND pillars_tool_unlocks_enabled). Every §0 wording fork keys on this, so a
    flag-off run keeps every string byte-identical to the pre-ladder behavior."""
    return bool(getattr(config, "creature_mode", False)
                and getattr(config, "pillars_tool_unlocks_enabled", False))


def visible_tools(config) -> dict[str, Callable[[dict, "Config"], ToolResult]]:
    """The registry as the creature's world sees it (TOOL_PROGRESSION: ONE accessor, five
    consumers — grammar, prompt, check_tools, manual, dispatch). House mode or ladder off returns
    TOOLS ITSELF (the very object), so every consumer stays byte-identical pre-cutover. Ladder
    active returns a filtered COPY — a pure read, no global mutation (register_predict_tool owns
    the only registry mutations):
      · every granted unit's tools (unlocks.granted_tools — fail-open to the newborn floor),
      · registry aliases of granted tools (an alias travels with its organ),
      · every hot-loaded self-authored skill (a creature's own makings are never locked).
    A name absent here DOES NOT EXIST in the creature's world (§0): the grammar cannot emit it,
    check_tools doesn't show it, the manual has no page for it, dispatch treats it as unknown."""
    if config is None or not _ladder_active(config):
        return TOOLS
    try:
        from unlocks import granted_tools
        granted = set(granted_tools(config))    # never raises, never empty (newborn floor)
    except Exception:  # noqa: BLE001 - a broken unlocks MODULE is a platform defect, not a lived
        return TOOLS   # state: keep the mind its body rather than amputate it blind
    granted.update(alias for alias, canon in TOOL_ALIASES.items() if canon in granted)
    return {name: fn for name, fn in TOOLS.items()
            if name in granted or name not in _EVER_BUILTIN_NAMES}

_TOOL_ARG_MODELS: dict[str, type[_ToolArgs]] = {
    "bash": BashArgs,
    "write_file": WriteFileArgs,
    "read_file": ReadFileArgs,
    "bg_run": BgRunArgs,
    "bg_check": BgCheckArgs,
    "http_request": HttpRequestArgs,
    "fetch": HttpRequestArgs,
    "http": HttpRequestArgs,
    "update_plan": UpdatePlanArgs,
    "memorize": MemorizeArgs,
    "update_self_guide": UpdateSelfGuideArgs,
    "propose_self_edit": ProposeSelfEditArgs,
    "list_self_edits": EmptyArgs,
    "recall": RecallArgs,
    "create_skill": CreateSkillArgs,
    "edit_skill": EditSkillArgs,
    "list_skills": EmptyArgs,
    "check_tools": EmptyArgs,
    "check_messages": EmptyArgs,
    "check_system": EmptyArgs,
    "rollback_skill": RollbackSkillArgs,
    "note_append": NoteAppendArgs,
    "note_read": NoteReadArgs,
    "note_list": EmptyArgs,
    "note_close": EmptyArgs,
    "speak": SpeakArgs,
    "manual": ManualArgs,
    "ask_ai": AskAiArgs,
    "vision": VisionArgs,
    "see": VisionArgs,
    "objective_add": ObjectiveAddArgs,
    "objective_done": ObjectiveKeyArgs,
    "objective_block": ObjectiveBlockArgs,
    "objective_list": EmptyArgs,
    "tcp_probe": TcpProbeArgs,
    "net_scan": NetScanArgs,
    "http_probe": HttpProbeArgs,
    "udp_listen": UdpListenArgs,
    "delegate": DelegateArgs,
}


def _validate_builtin_tool_call(call: ToolCall) -> ToolCall | ToolResult:
    model = _TOOL_ARG_MODELS.get(call.tool)
    if model is None:
        return call
    parsed = _validate_tool_args(model, call.args, call.tool)
    if isinstance(parsed, ToolResult):
        return parsed
    return dataclasses.replace(call, args=parsed.model_dump(exclude_none=True, exclude_unset=True))

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
        # §0: the fix-hint's tail names the net primitives / bg_run only where they exist (house
        # mode); in the creature's world those names were never taught and never will be.
        _tail = ("or compose the built-in bounded primitives (tcp_probe / http_probe / net_scan / "
                 "udp_listen). For genuinely long work, dispatch it with bash/bg_run instead of "
                 "doing it inline."
                 if not _ladder_active(config) else
                 "For genuinely long work, dispatch it with bash instead of doing it inline.")
        return ToolResult(
            output=(
                f"WATCHDOG: skill '{call.tool}' ran past {timeout_s:.0f}s and was ABANDONED so the tick "
                f"loop is NOT blocked (tick 342: a skill's timeout-less network call froze the loop ~6.7 "
                f"min). Its call is still running detached and will be cleaned up on the next eidos "
                f"restart. FIX THE SKILL with edit_skill: put an explicit timeout on EVERY network / HTTP "
                f"/ socket / subprocess call — e.g. requests.get(url, timeout=5), "
                f"socket.create_connection(addr, timeout=5), subprocess.run(..., timeout=10) — {_tail}"),
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
    # The dispatch backstop reads the SAME accessor as the grammar/prompt/check_tools (§0): with
    # the ladder off this IS the TOOLS object (byte-identical); with it on, a locked builtin is
    # absent from the registry and therefore INDISTINGUISHABLE from a name that never existed —
    # same message shape, same fail_kind, and the listing names only what exists in this world.
    registry = visible_tools(config)
    handler = registry.get(call.tool)
    if not handler:
        return ToolResult(
            output=f"Unknown tool: '{call.tool}'. Available: {', '.join(sorted(registry.keys()))}",
            full_output_path=None,
            success=False,
            duration_s=0,
            fail_kind="no_such_tool",
        )
    try:
        if call.tool in _BUILTIN_TOOL_NAMES:
            validated_call = _validate_builtin_tool_call(call)
            if isinstance(validated_call, ToolResult):
                return validated_call
            call = validated_call
        # Self-authored skills are time-bounded: they run in the tick with no internal cap, so a hung
        # one would freeze the loop. Built-in tools are already bounded (bash auto-backgrounds, the net
        # primitives self-time-out) — run them directly.
        if call.tool not in _BUILTIN_TOOL_NAMES:
            if getattr(config, "pillars_killable_skills_enabled", False):
                # Pillars 1.2 (flag ON): the skill's own runner executes it in a fresh, HARD-KILLABLE
                # subprocess bounded by its derived (p95×3-clamped) timeout, so a hang dies with the
                # process — no orphan thread. The thread-watchdog is redundant here (and would only
                # abandon a thread blocked on the killable subprocess), so we call the handler directly.
                result = handler(call.args, config)
            else:
                # Flag OFF: byte-for-byte the historical thread-watchdog path (abandon on overrun).
                timeout_s = float(getattr(config, "skill_watchdog_s", _SKILL_WATCHDOG_DEFAULT_S)
                                  or _SKILL_WATCHDOG_DEFAULT_S)
                result = _run_skill_under_watchdog(call, handler, config, timeout_s)
        else:
            result = handler(call.args, config)
        # Contract guard: ToolResult.output is typed `str`, but a SELF-AUTHORED skill can return a
        # dict/list/number and nothing upstream stops it. check_boss_presence v1.0.20 returned a dict
        # ({'presence':..,'raw_data':..}); the tick-loop display slice `(result.output or "")[:160]`
        # then raised `KeyError: slice` and crash-looped the WHOLE creature (tick 14066, 2026-06-20 —
        # 6 deterministic deaths in ~70s; the watchdog stood down with no last_good to roll back to).
        # A tool's output *type* must never kill the mind: normalize anything non-str to a readable
        # string at this single chokepoint every tool flows through. None is left alone (downstream
        # `or ""` handles it); dicts/lists become compact JSON the creature can still reason over.
        if isinstance(result, ToolResult) and result.output is not None and not isinstance(result.output, str):
            try:
                result.output = (json.dumps(result.output, ensure_ascii=False, default=str)
                                 if isinstance(result.output, (dict, list))
                                 else str(result.output))
            except Exception:  # noqa: BLE001 — last resort: stringification must never crash the loop
                result.output = str(result.output)
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
