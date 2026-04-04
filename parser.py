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


def parse_tool_call(text: str) -> Optional[ToolCall]:
    """Extract the first <tool>...</tool><args>...</args> from LLM output.

    Returns None if no valid tool call found or if JSON parsing fails.
    Falls back to handle common 4B-model mistakes:
      - Missing closing </args> tag
      - Missing <args> block entirely (for tools with known defaults)
    """
    match = TOOL_PATTERN.search(text)
    if match:
        tool_name = match.group(1).lower().strip()
        args_str = match.group(2).strip()
        args = _try_parse_json(args_str)
        if args is not None:
            return ToolCall(tool=tool_name, args=args, raw=match.group(0))

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

    return None


def _try_parse_json(args_str: str) -> Optional[dict]:
    """Try raw JSON, then cleaned. Returns dict or None."""
    try:
        args = json.loads(args_str)
    except (json.JSONDecodeError, ValueError):
        try:
            args = json.loads(_clean_json(args_str))
        except (json.JSONDecodeError, ValueError):
            return None
    if not isinstance(args, dict):
        return None
    return args
