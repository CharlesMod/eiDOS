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
    """
    match = TOOL_PATTERN.search(text)
    if not match:
        return None

    tool_name = match.group(1).lower().strip()
    args_str = match.group(2).strip()

    # Try raw JSON first, then cleaned
    try:
        args = json.loads(args_str)
    except (json.JSONDecodeError, ValueError):
        try:
            args = json.loads(_clean_json(args_str))
        except (json.JSONDecodeError, ValueError):
            return None

    if not isinstance(args, dict):
        return None

    return ToolCall(tool=tool_name, args=args, raw=match.group(0))
