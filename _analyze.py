#!/usr/bin/env python3
"""Analyze stress test failures."""
import json, re

with open("workspace/stress_results.json") as f:
    data = json.load(f)

passes = [r for r in data if r["passed"]]
fails = [r for r in data if not r["passed"] and r["llm_live"]]

print(f"Total: {len(data)} | PASS: {len(passes)} | FAIL: {len(fails)}")
print()
print("=== FAILURES ===")
for r in fails:
    pe = f" ({r['parse_errors']}pe)" if r["parse_errors"] else ""
    gc = " [gc]" if r["goal_complete_called"] else ""
    lp = f" [{r['loop_warnings']}lp]" if r["loop_warnings"] else ""
    tools = " ".join(f"{k}={v}" for k, v in r.get("tools_called", {}).items())
    print(f"\n  {r['goal']} x {r['trick']}")
    print(f"    ticks: {r['ticks_used']}/{r['max_ticks']}{gc}{pe}{lp}")
    print(f"    detail: {r['details']}")
    print(f"    tools:  {tools}")

    # Show write_file paths and goal_complete summaries
    for i, resp in enumerate(r.get("responses", [])):
        for m in re.finditer(r'"path"\s*:\s*"([^"]+)"', resp):
            print(f"    tick {i+1} wrote: {m.group(1)}")
        for m in re.finditer(r'"summary"\s*:\s*"([^"]{0,100})', resp):
            print(f"    tick {i+1} gc:    {m.group(1)}")

    # Show first 200 chars of each response for context
    print("    --- responses ---")
    for i, resp in enumerate(r.get("responses", [])):
        snippet = resp.strip().replace("\n", " ")[:150]
        print(f"    [{i+1}] {snippet}")
