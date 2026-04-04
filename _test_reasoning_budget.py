#!/usr/bin/env python3
"""Test: reasoning_effort / budget params to suppress thinking."""
import json
import urllib.request
from config import load_config

config = load_config("config.toml")
url = config.llm_url.rstrip("/") + "/v1/chat/completions"

msgs = [
    {"role": "system", "content": "Summarize the input concisely."},
    {"role": "user", "content": "The cat sat on the mat. The dog barked. The bird flew away."},
]

tests = {
    "BASELINE": {},
    "REASONING_EFFORT_NONE": {"reasoning_effort": "none"},
    "REASONING_EFFORT_LOW": {"reasoning_effort": "low"},
    "MAX_COMPLETION_TOKENS": {"max_completion_tokens": 512, "max_tokens": None},
    "BUDGET_TOKENS_0": {"budget_tokens": 0},
    "THINKING_FALSE": {"thinking": {"type": "disabled"}},
}

for label, extra in tests.items():
    payload = {
        "model": config.llm_model,
        "messages": msgs,
        "temperature": 0.3,
        "max_tokens": 512,
        "stream": False,
    }
    payload.update(extra)
    # Remove None values
    payload = {k: v for k, v in payload.items() if v is not None}
    body = json.dumps(payload).encode()
    req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            data = json.loads(resp.read())
        msg = data["choices"][0]["message"]
        usage = data.get("usage", {})
        details = usage.get("completion_tokens_details", {})
        reasoning = msg.get("reasoning_content") or ""
        content = msg.get("content") or ""
        print(f"--- {label} ---")
        print(f"  reasoning_tokens: {details.get('reasoning_tokens', 0)}")
        print(f"  completion_tokens: {usage.get('completion_tokens', 0)}")
        print(f"  content ({len(content)} chars): {content[:150]!r}")
        print()
    except urllib.error.HTTPError as e:
        err = e.read().decode("utf-8", errors="replace")[:200]
        print(f"--- {label} --- HTTP {e.code}: {err}")
        print()
    except Exception as e:
        print(f"--- {label} --- ERROR: {e}")
        print()
