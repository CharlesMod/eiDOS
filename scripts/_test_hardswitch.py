#!/usr/bin/env python3
"""Test: chat_template_kwargs and other hard-switch approaches."""
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
    "CHAT_TEMPLATE_KWARGS": {"chat_template_kwargs": {"enable_thinking": False}},
    "EXTRA_BODY": {"extra_body": {"enable_thinking": False}},
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
        print(f"  reasoning ({len(reasoning)} chars): {reasoning[:80]!r}")
        print()
    except Exception as e:
        print(f"--- {label} --- ERROR: {e}")
        print()
