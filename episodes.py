"""Episodic memory (BIBLE §2.4) — one typed (situation→action→outcome→fix) store with
state-triggered recall.

The episodic material used to be shredded across four surfaces that couldn't be recalled by
situation: observations.jsonl (truncated every dream), thoughts.jsonl (unbounded, never recalled
by similarity), the knowledge "errors" category (free prose), and dream records (write-only).
This is the typed home for it: one episode recorded per acting tick, and recall that fires
INVOLUNTARILY — surfaced in context BEFORE the model acts — when the current situation resembles
a past one. The doctrine's "this is like last time → do X (or don't repeat what failed)", as a
mechanism, not a query the model has to remember to run.

An episode = {tick, key, obj, step, tool, sig, fail_kind, success, summary, ts}:
  - SITUATION = key = "<active objective id>|<normalized next step>" — what I was doing, stably.
  - ACTION    = tool + sig (the normalized action signature; bash collapses port/version/quoting
                variants so v3/v4/v5 retries share one sig — reused from the loop detector).
  - OUTCOME   = success + fail_kind (the phase-1 taxonomy).
  - FIX       = derived at recall: a sig that FAILED and later SUCCEEDED in the same situation.

Recall prioritizes the two things that change the next decision: repeated FAILURES (so a known
dead end isn't re-tried) and RECOVERIES (so what worked is reused). Deterministic, embedding-free
(situation key match); phase 7a can layer semantic similarity on top.
"""

from __future__ import annotations

import json
import re

_MAX_EPISODES = 600          # self-bounding ring (no separate rotation needed)
_RECALL_WINDOW = 400         # how far back recall scans
_MISFIRE = ("system", "watchdog", "dream", "")   # tools that aren't real "actions"


def _path(config):
    return config.workspace / "episodes.jsonl"


def _norm_step(text: str) -> str:
    s = (text or "").strip().lower()
    s = re.sub(r"\d+", "#", s)               # collapse numbers (ips/ports/counts)
    s = re.sub(r"\s+", " ", s)
    return s[:80]


def situation_key(config) -> str:
    """The current SITUATION digest: active objective + normalized next step. Computed the same
    way at record time and recall time so a recurring situation produces a recurring key."""
    obj_id = ""
    try:
        import objectives
        a = objectives.get_active(config)
        if a:
            obj_id = a.get("id", "")
    except Exception:  # noqa: BLE001
        pass
    step = ""
    try:
        from memory import read_plan
        for line in (read_plan(config) or "").splitlines():
            s = line.strip()
            if s and not s.startswith("#"):
                step = s
                break
    except Exception:  # noqa: BLE001
        pass
    return f"{obj_id}|{_norm_step(step)}"


def _read(config, limit: int = _RECALL_WINDOW) -> list[dict]:
    try:
        lines = _path(config).read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    out = []
    for line in lines[-limit:]:
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except Exception:  # noqa: BLE001
            continue
    return out


def record_episode(config, *, tick: int, tool: str, sig: str, fail_kind: str,
                   success: bool, summary: str = "", key: str = None) -> None:
    """Append one acting tick as an episode. Best-effort; never raises into the loop.
    Non-action ticks (system/watchdog/dream/thought-only) are skipped — they aren't decisions."""
    if not tool or tool in _MISFIRE:
        return
    try:
        import time as _t
        if key is None:
            key = situation_key(config)
        ep = {"tick": int(tick), "key": key, "tool": tool, "sig": str(sig or tool),
              "fail_kind": fail_kind or "", "success": bool(success),
              "summary": (summary or "")[:160], "ts": _t.time()}
        config.workspace.mkdir(parents=True, exist_ok=True)
        with open(_path(config), "a", encoding="utf-8") as f:
            f.write(json.dumps(ep) + "\n")
        _trim(config)
    except Exception:  # noqa: BLE001 - episodic recording is best-effort
        pass


def _trim(config) -> None:
    """Keep the file bounded to the most recent _MAX_EPISODES lines."""
    try:
        p = _path(config)
        lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
        if len(lines) > _MAX_EPISODES:
            p.write_text("\n".join(lines[-_MAX_EPISODES:]) + "\n", encoding="utf-8")
    except OSError:
        pass


def recall(config, key: str = None, *, max_items: int = 4) -> dict:
    """State-triggered recall for the CURRENT situation, triggered by past FAILURE. Returns the
    episodes that should change the next decision: the actions that FAILED here and never worked
    (don't re-try), and — as the alternative — the actions that WORKED here (reuse them, even if
    a different approach). Empty when this situation has no failures (no warning needed → no
    noise). Exact situation-key match first, else same-objective. Pure read; cheap."""
    if key is None:
        key = situation_key(config)
    obj = key.split("|", 1)[0]
    eps = _read(config)
    if not eps:
        return {"failures": [], "worked": []}

    # Prefer episodes in the exact situation; fall back to the same objective if the exact key
    # has no history yet (a slightly different step under the same goal is still relevant).
    exact = [e for e in eps if e.get("key") == key]
    pool = exact if exact else [e for e in eps if e.get("key", "").split("|", 1)[0] == obj and obj]
    if not pool:
        return {"failures": [], "worked": []}

    # Per signature: failure count + last fail_kind/summary, and whether it ever SUCCEEDED here.
    by_sig: dict[str, dict] = {}
    for e in pool:
        sig = e.get("sig") or e.get("tool")
        d = by_sig.setdefault(sig, {"sig": sig, "tool": e.get("tool"), "fails": 0,
                                    "fail_kind": "", "succeeded": False,
                                    "fail_summary": "", "ok_summary": ""})
        if e.get("success"):
            d["succeeded"] = True
            d["ok_summary"] = e.get("summary", "") or d["ok_summary"]
        else:
            d["fails"] += 1
            d["fail_kind"] = e.get("fail_kind", "") or d["fail_kind"]
            d["fail_summary"] = e.get("summary", "") or d["fail_summary"]

    failures = [d for d in by_sig.values() if d["fails"] >= 1 and not d["succeeded"]]
    if not failures:
        return {"failures": [], "worked": []}   # only fire when something failed here
    worked = [d for d in by_sig.values() if d["succeeded"]]   # the alternatives that DID work
    failures.sort(key=lambda d: -d["fails"])
    worked.sort(key=lambda d: -d.get("fails", 0))             # a recovered approach ranks first
    return {"failures": failures[:max_items], "worked": worked[:max_items]}


def render_recall(rec: dict) -> str:
    """Render recall() output as a compact context block, or '' if nothing relevant."""
    failures, worked = rec.get("failures", []), rec.get("worked", [])
    if not failures:
        return ""
    lines = ["## Episodic recall — you have been in this situation before:"]
    for d in failures:
        kind = f" ({d['fail_kind']})" if d.get("fail_kind") else ""
        times = f" ×{d['fails']}" if d["fails"] > 1 else ""
        tail = f" — {d['fail_summary']}" if d.get("fail_summary") else ""
        lines.append(f"  ✗ `{d['tool']}` here FAILED{kind}{times}{tail}. Don't repeat it — try a different approach.")
    for d in worked:
        what = f": {d['ok_summary']}" if d.get("ok_summary") else ""
        lines.append(f"  ✓ `{d['tool']}` WORKED here{what}. Prefer that.")
    return "\n".join(lines)
