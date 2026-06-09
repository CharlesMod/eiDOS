"""Parse tool calls from LLM text output."""

import dataclasses
import html
import json
import re
from typing import Optional

TOOL_PATTERN = re.compile(
    r'<tool>\s*([\w]+)\s*</tool>\s*<args>\s*(.*?)\s*</args>',
    re.DOTALL | re.IGNORECASE,
)

# Fallback: <tool>name</tool><args>{...} without closing </args>
TOOL_PATTERN_UNCLOSED = re.compile(
    r'<tool>\s*([\w]+)\s*</tool>\s*<args>\s*(\{.*)',
    re.DOTALL | re.IGNORECASE,
)

# Fallback: <tool>name</tool> with no <args> block at all (for no-arg tools)
TOOL_PATTERN_NO_ARGS = re.compile(
    r'<tool>\s*([\w]+)\s*</tool>(?!\s*<args>)',
    re.IGNORECASE,
)

# Tools that can meaningfully run with empty/default args
_NO_ARG_DEFAULTS = {
    "goal_complete": {"summary": "(auto-completed)", "evidence": "(none provided)"},
}

# Fallback: alternate formats the model might produce when confused
# TOOL: name PARAMS: {...}  or  TOOL: name\nPARAMS: {...}
TOOL_ALT_FORMAT = re.compile(
    r'TOOL:\s*([\w]+)\s*(?:PARAMS|ARGUMENTS|ARGS):\s*(\{.*)',
    re.DOTALL | re.IGNORECASE,
)

# Builtin tool names the model sometimes emits AS the tag (shorthand),
# e.g. <bash>{"cmd": "..."}</bash> instead of <tool>bash</tool><args>...</args>.
_KNOWN_TOOL_TAGS = {
    "bash", "read_file", "write_file", "http_get", "remember", "update_plan",
    "memorize", "recall", "goal_complete", "ask_supervisor",
    "bg_run", "bg_check", "create_skill", "edit_skill", "list_skills", "rollback_skill",
}


def _known_tool_tags() -> set:
    tags = set(_KNOWN_TOOL_TAGS)
    try:
        from tools import TOOLS
        tags.update(TOOLS.keys())
    except Exception:  # noqa: BLE001
        pass
    return tags


@dataclasses.dataclass
class ToolCall:
    tool: str
    args: dict
    raw: str


def _clean_json(s: str) -> str:
    """Best-effort cleanup of sloppy JSON from small language models.

    Handles the most common 4B-model failure modes:
      - Markdown fences:  ```json\n{...}\n```
      - Trailing junk:    {"cmd": "ls"}>  or  {"cmd": "ls"},
      - HTML entities:    {&quot;cmd&quot;: &quot;ls&quot;}
      - Single quotes:   {'cmd': 'ls'}  (only when no double quotes in values)
      - Extra braces:    {"cmd": "ls"}}
    """
    s = s.strip()

    # Strip markdown code fences
    s = re.sub(r'^```(?:json)?\s*', '', s)
    s = re.sub(r'\s*```$', '', s)
    s = s.strip()

    # Decode HTML entities (&quot; &amp; etc.)
    if '&' in s:
        s = html.unescape(s)

    # Strip trailing junk after the last }
    last_brace = s.rfind('}')
    if last_brace != -1 and last_brace < len(s) - 1:
        s = s[:last_brace + 1]

    # Fix extra closing brace: {"cmd": "ls"}}  →  {"cmd": "ls"}
    if s.endswith('}}') and s.count('{') < s.count('}'):
        s = s[:-1]

    # Single quotes → double quotes (only safe when values don't contain doubles)
    if "'" in s and '"' not in s:
        s = s.replace("'", '"')

    return s


# Tools whose first (required) arg is a plain text string.
# When the model emits raw text instead of JSON, we wrap it automatically.
_TEXT_ARG_TOOLS = {
    "bash": "cmd",
    "remember": "note",
    "update_plan": "note",
}


def parse_tool_call(text: str) -> Optional[ToolCall]:
    """Extract the first <tool>...</tool><args>...</args> from LLM output.

    Returns None if no valid tool call found or if JSON parsing fails.
    Falls back to handle common 4B-model mistakes:
      - Missing closing </args> tag
      - Missing <args> block entirely (for tools with known defaults)
      - Raw text args instead of JSON (e.g. <args>df -h</args> for bash)
    """
    match = TOOL_PATTERN.search(text)
    if match:
        tool_name = match.group(1).lower().strip()
        args_str = match.group(2).strip()
        args = _try_parse_json(args_str)
        if args is not None:
            return ToolCall(tool=tool_name, args=args, raw=match.group(0))
        # Raw text args (not JSON at all): if it's a known text-arg tool, wrap it.
        # Only apply when the text doesn't look like a JSON attempt (no leading { or [).
        if tool_name in _TEXT_ARG_TOOLS and args_str and not args_str.startswith(("{", "[")):
            key = _TEXT_ARG_TOOLS[tool_name]
            return ToolCall(tool=tool_name, args={key: args_str}, raw=match.group(0))

    # Fallback: <tool>name</tool><args>{...} without closing </args>
    match = TOOL_PATTERN_UNCLOSED.search(text)
    if match:
        tool_name = match.group(1).lower().strip()
        args_str = match.group(2).strip()
        args = _try_parse_json(args_str)
        if args is not None:
            return ToolCall(tool=tool_name, args=args, raw=match.group(0))

    # Fallback: <tool>name</tool> with no args at all
    match = TOOL_PATTERN_NO_ARGS.search(text)
    if match:
        tool_name = match.group(1).lower().strip()
        if tool_name in _NO_ARG_DEFAULTS:
            return ToolCall(tool=tool_name, args=_NO_ARG_DEFAULTS[tool_name],
                            raw=match.group(0))

    # Fallback: alternate format (TOOL: name PARAMS: {...})
    match = TOOL_ALT_FORMAT.search(text)
    if match:
        tool_name = match.group(1).lower().strip()
        args_str = match.group(2).strip()
        args = _try_parse_json(args_str)
        if args is not None:
            return ToolCall(tool=tool_name, args=args, raw=match.group(0))

    # Fallback: shorthand where the model uses the TOOL NAME as the tag,
    # e.g. <bash>{"cmd": "..."}</bash>  or  <bash>ls -la</bash>  or unclosed <bash>{...
    known = _known_tool_tags()
    _reserved = {"tool", "args", "reply", "think", "thinking", "thought"}
    for m in re.finditer(r'<([a-z_]\w*)>\s*(.*?)\s*</\1>', text, re.DOTALL | re.IGNORECASE):
        name = m.group(1).lower().strip()
        if name in _reserved or name not in known:
            continue
        body = m.group(2).strip()
        args = _try_parse_json(body)
        if args is not None:
            return ToolCall(tool=name, args=args, raw=m.group(0))
        if name in _TEXT_ARG_TOOLS and body and not body.startswith(("{", "[")):
            return ToolCall(tool=name, args={_TEXT_ARG_TOOLS[name]: body}, raw=m.group(0))
    m = re.search(r'<([a-z_]\w*)>\s*(\{.*)', text, re.DOTALL | re.IGNORECASE)
    if m:
        name = m.group(1).lower().strip()
        if name in known and name not in _reserved:
            args = _try_parse_json(m.group(2).strip())
            if args is not None:
                return ToolCall(tool=name, args=args, raw=m.group(0))

    return None


def _try_parse_json(args_str: str) -> Optional[dict]:
    """Try raw JSON, then cleaned, then cmd-extraction fallback. Returns dict or None."""
    try:
        args = json.loads(args_str)
    except (json.JSONDecodeError, ValueError):
        try:
            args = json.loads(_clean_json(args_str))
        except (json.JSONDecodeError, ValueError):
            # Last resort: extract {"cmd": "..."} with unescaped inner quotes
            # Handles e.g. {"cmd": "grep -v "pattern""}
            extracted = _extract_cmd_fallback(args_str)
            if extracted is not None:
                return extracted
            return None
    if not isinstance(args, dict):
        return None
    return args


# Regex for {"cmd": "...anything..."} where the value may contain unescaped quotes
_CMD_EXTRACT = re.compile(
    r'\{\s*"cmd"\s*:\s*"(.*)"',
    re.DOTALL,
)


def _extract_cmd_fallback(s: str) -> Optional[dict]:
    """Extract a cmd value from malformed JSON where internal quotes aren't escaped.

    For a 4B model producing {"cmd": "grep -v "^-""}, greedily capture everything
    between the opening quote after "cmd": and the last quote before }.
    """
    s = _clean_json(s)
    m = _CMD_EXTRACT.search(s)
    if not m:
        return None
    # The greedy .* captured everything between first and last quote
    cmd = m.group(1).strip()
    # Remove any trailing " that got included (extra closing quote)
    cmd = cmd.rstrip('"').strip()
    if not cmd:
        return None
    # Escape internal quotes so the value is clean for downstream use
    return {"cmd": cmd}


# --- Reply parsing ---

REPLY_PATTERN = re.compile(
    r'<reply>\s*(.*?)\s*</reply>',
    re.DOTALL | re.IGNORECASE,
)


def parse_reply(text: str) -> Optional[str]:
    """Extract the first <reply>...</reply> from LLM output.

    Returns the reply text or None if no reply tag found.
    """
    match = REPLY_PATTERN.search(text)
    if match:
        reply = match.group(1).strip()
        if reply:
            return reply
    return None
