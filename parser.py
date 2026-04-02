"""Parse tool calls from LLM text output."""

import dataclasses
import json
import re
from typing import Optional

TOOL_PATTERN = re.compile(
    r'<tool>\s*([\w]+)\s*</tool>\s*<args>\s*(.*?)\s*</args>',
    re.DOTALL,
)


@dataclasses.dataclass
class ToolCall:
    tool: str
    args: dict
    raw: str


def parse_tool_call(text: str) -> Optional[ToolCall]:
    """Extract the first <tool>...</tool><args>...</args> from LLM output.

    Returns None if no valid tool call found or if JSON parsing fails.
    """
    match = TOOL_PATTERN.search(text)
    if not match:
        return None

    tool_name = match.group(1).lower().strip()
    args_str = match.group(2).strip()

    try:
        args = json.loads(args_str)
    except (json.JSONDecodeError, ValueError):
        return None

    if not isinstance(args, dict):
        return None

    return ToolCall(tool=tool_name, args=args, raw=match.group(0))
