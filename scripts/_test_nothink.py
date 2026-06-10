#!/usr/bin/env python3
"""Quick test: does /no_think soft switch work with current LM Studio model?"""
import json
import urllib.request
from config import load_config

config = load_config("config.toml")
url = config.llm_url.rstrip("/") + "/v1/chat/completions"

tests = {
    "THINK": "The cat sat on the mat. The dog barked. The bird flew away.",
    "NO_THINK": "/no_think\nThe cat sat on the mat. The dog barked. The bird flew away.",
}

for label, user_content in tests.items():
    msgs = [
        {"role": "system", "content": "Summarize the input concisely."},
        {"role": "user", "content": user_content},
    ]
    payload = {
        "model": config.llm_model,
        "messages": msgs,
        "temperature": 0.3,
        "max_tokens": 512,
        "stream": False,
    }
    body = json.dumps(payload).encode()
    req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as resp:
        data = json.loads(resp.read())

    msg = data["choices"][0]["message"]
    usage = data.get("usage", {})
    details = usage.get("completion_tokens_details", {})
    reasoning = msg.get("reasoning_content") or ""
    content = msg.get("content") or ""

    print(f"--- {label} ---")
    print(f"  reasoning_content ({len(reasoning)} chars): {reasoning[:150]!r}")
    print(f"  content ({len(content)} chars): {content[:200]!r}")
    print(f"  reasoning_tokens: {details.get('reasoning_tokens', 0)}")
    print(f"  completion_tokens: {usage.get('completion_tokens', 0)}")
    print(f"  prompt_tokens: {usage.get('prompt_tokens', 0)}")
    print()
