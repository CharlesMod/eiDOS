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

_MAX_EPISODES = 2400         # self-bounding ring (no separate rotation needed) — at ~1 episode per
                             # acting tick this is days of memory, not hours; ~600 KB, ms to scan
_RECALL_WINDOW = 1200        # how far back recall scans
_MISFIRE = ("system", "watchdog", "dream", "")   # tools that aren't real "actions"

# Phase 7a-2: semantic situation similarity. Vectors are stored per DISTINCT situation key (not per
# episode) — keys are normalized, so the distinct set is small (tens) and self-bounds cheaply.
_MAX_SITUATIONS = 256        # distinct situations to keep embedded (ring)
_SIM_THRESHOLD = 0.45        # cosine floor to call a past situation "like this one" (calibrated for
                             # MiniLM AND the mock embedder; below this is noise, not resemblance)


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
        _remember_situation(config, key)   # build the similarity index from live ticks (gated)
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


# ---------------------------------------------------------------------------
# Situation similarity (phase 7a-2) — the semantic layer the 7b docstring promised.
# A novel situation rarely has an exact OR same-objective match, but it may RESEMBLE a past one
# (a network scan under objective B is like the network scan that timed out under objective A).
# We embed the normalized STEP of each distinct situation key (the opaque objective id is dropped —
# it carries no meaning), so resemblance crosses objective boundaries. Embedding-gated: when
# embeddings are off this whole layer is inert and recall is exactly the 7b deterministic store.
# ---------------------------------------------------------------------------

def _step_of(key: str) -> str:
    return key.split("|", 1)[1] if "|" in key else key


def _sit_vec_path(config):
    return config.workspace / "situation_vectors.npy"


def _sit_key_path(config):
    return config.workspace / "situation_keys.json"


def _load_situations(config):
    """Return (vectors_ndarray, keys_list) or (None, []) — parallel arrays, vectors[i] ↔ keys[i]."""
    vp, kp = _sit_vec_path(config), _sit_key_path(config)
    if not vp.exists() or not kp.exists():
        return None, []
    try:
        import numpy as np
        v = np.load(str(vp))
        k = json.loads(kp.read_text(encoding="utf-8"))
        if v.shape[0] != len(k):
            return None, []
        return v, k
    except Exception:  # noqa: BLE001
        return None, []


def _save_situations(config, vectors, keys: list) -> None:
    import numpy as np
    config.workspace.mkdir(parents=True, exist_ok=True)
    np.save(str(_sit_vec_path(config)), vectors)
    _sit_key_path(config).write_text(json.dumps(keys), encoding="utf-8")


def _remember_situation(config, key: str) -> None:
    """Embed and store a situation the FIRST time it's seen, so future novel situations can match it.
    Best-effort, embedding-gated, cheap on the common path: a tiny keys-json read short-circuits when
    the key is already known (which it usually is), so only a genuinely new situation pays an embed."""
    if not getattr(config, "knowledge_embedding_enabled", False) or not key:
        return
    try:
        kp = _sit_key_path(config)
        if kp.exists():
            keys = json.loads(kp.read_text(encoding="utf-8"))
            if key in keys:
                return  # already embedded — the hot path
        import numpy as np
        import embedding
        vec = embedding.embed_query(config, _step_of(key))
        if vec is None:
            return
        vectors, keys = _load_situations(config)
        if vectors is None:
            vectors, keys = vec.reshape(1, -1), [key]
        else:
            vectors, keys = np.vstack([vectors, vec.reshape(1, -1)]), keys + [key]
        if len(keys) > _MAX_SITUATIONS:          # ring: drop oldest distinct situations
            vectors, keys = vectors[-_MAX_SITUATIONS:], keys[-_MAX_SITUATIONS:]
        _save_situations(config, vectors, keys)
    except Exception:  # noqa: BLE001 - situation memory is best-effort, never raises into the loop
        pass


def _nearest_situation(config, key: str) -> str:
    """The stored situation key most semantically like `key` (excluding itself), or '' if none clears
    _SIM_THRESHOLD. The trigger for cross-situation recall."""
    if not getattr(config, "knowledge_embedding_enabled", False) or not key:
        return ""
    try:
        vectors, keys = _load_situations(config)
        if vectors is None or not keys:
            return ""
        import embedding
        q = embedding.embed_query(config, _step_of(key))
        if q is None:
            return ""
        scores = vectors @ q
        best_i, best = -1, _SIM_THRESHOLD
        for i, k in enumerate(keys):
            if k == key:
                continue
            if float(scores[i]) >= best:
                best, best_i = float(scores[i]), i
        return keys[best_i] if best_i >= 0 else ""
    except Exception:  # noqa: BLE001
        return ""


def recall(config, key: str = None, *, max_items: int = 4) -> dict:
    """State-triggered recall for the CURRENT situation. Returns the episodes that should change
    the next decision: the actions that FAILED here and never worked (don't re-try), the actions
    that WORKED here (reuse them), and — when nothing failed but an approach has proven reliable
    (2+ successes) — that known-good approach, so success is recalled too, not only pain.
    Match order: exact situation key → same normalized STEP under any objective (episodes survive
    an objective being parked/killed/re-cut — the work is the same even when the goal bookkeeping
    changed) → same objective → semantic resemblance. Pure read; cheap."""
    if key is None:
        key = situation_key(config)
    obj = key.split("|", 1)[0]
    step = _step_of(key)
    eps = _read(config)
    if not eps:
        return {"failures": [], "worked": []}

    # Prefer episodes in the exact situation; fall back to the same STEP regardless of which
    # objective it was filed under (objective ids churn — park/kill/re-cut — but the normalized
    # step is the stable part of the situation); then to the same objective.
    exact = [e for e in eps if e.get("key") == key]
    pool = exact or [e for e in eps if step and _step_of(e.get("key", "")) == step]
    pool = pool or [e for e in eps if e.get("key", "").split("|", 1)[0] == obj and obj]
    similar_via = ""
    if not pool:
        # Novel situation deterministically — but is it LIKE a past one? (phase 7a-2). Cross-situation
        # semantic match: pull the episodes of the nearest resembling situation instead of nothing.
        nearest = _nearest_situation(config, key)
        if nearest:
            pool = [e for e in eps if e.get("key") == nearest]
            similar_via = nearest
    if not pool:
        return {"failures": [], "worked": []}

    # Per signature: failure/success counts + last fail_kind/summary.
    by_sig: dict[str, dict] = {}
    for e in pool:
        sig = e.get("sig") or e.get("tool")
        d = by_sig.setdefault(sig, {"sig": sig, "tool": e.get("tool"), "fails": 0, "oks": 0,
                                    "fail_kind": "", "succeeded": False,
                                    "fail_summary": "", "ok_summary": ""})
        if e.get("success"):
            d["succeeded"] = True
            d["oks"] += 1
            d["ok_summary"] = e.get("summary", "") or d["ok_summary"]
        else:
            d["fails"] += 1
            d["fail_kind"] = e.get("fail_kind", "") or d["fail_kind"]
            d["fail_summary"] = e.get("summary", "") or d["fail_summary"]

    failures = [d for d in by_sig.values() if d["fails"] >= 1 and not d["succeeded"]]
    worked = [d for d in by_sig.values() if d["succeeded"]]
    if not failures:
        # Nothing failed here — but if an approach has PROVEN itself (2+ successes), recall it
        # proactively: "you have done this before, reuse that" instead of re-deriving from
        # scratch. Single successes stay silent (could be luck; would be noise every tick).
        proven = [d for d in worked if d["oks"] >= 2]
        if not proven:
            return {"failures": [], "worked": []}
        proven.sort(key=lambda d: -d["oks"])
        out = {"failures": [], "worked": proven[:max_items], "proven": True}
        if similar_via:
            out["similar"] = True
            out["via_step"] = _step_of(similar_via)
        return out
    failures.sort(key=lambda d: -d["fails"])
    worked.sort(key=lambda d: (-d.get("fails", 0), -d["oks"]))  # recovered approaches rank first
    out = {"failures": failures[:max_items], "worked": worked[:max_items]}
    if similar_via:                              # matched by resemblance, not exact situation
        out["similar"] = True
        out["via_step"] = _step_of(similar_via)
    return out


_SYSTEMIC_WINDOW = 200       # recent episodes scanned for cross-objective patterns
_SYSTEMIC_SIG_FAILS = 4      # same signature failing this often...
_SYSTEMIC_SIG_OBJS = 2       # ...under this many DISTINCT objectives = systemic
_SYSTEMIC_KIND_FAILS = 8     # same failure KIND this often...
_SYSTEMIC_KIND_OBJS = 3      # ...across this many objectives = environmental pattern


def systemic_blocker(config) -> dict | None:
    """Detect a SYSTEM-LEVEL blocker: the same failure recurring across DIFFERENT objectives.

    Per-objective frustration treats each goal's failures separately, so an environmental
    cause (network hardening, missing credentials, a service down) accrues patience N times —
    once per objective — and is never recognized as one blocker. This scans recent episodes
    for (a) one action signature failing under 2+ distinct objectives with no success, and
    (b) one failure kind dominating across 3+ objectives. Deterministic, pure read."""
    eps = _read(config, limit=_SYSTEMIC_WINDOW)
    if not eps:
        return None
    sig_fail: dict[str, dict] = {}
    sig_ok: set = set()
    kind_fail: dict[str, dict] = {}
    for e in eps:
        sig = str(e.get("sig") or e.get("tool") or "")
        obj = str(e.get("key", "")).split("|", 1)[0]
        if e.get("success"):
            sig_ok.add(sig)
            continue
        kind = e.get("fail_kind") or "error"
        d = sig_fail.setdefault(sig, {"fails": 0, "objs": set(), "kind": kind,
                                      "tool": e.get("tool"), "summary": ""})
        d["fails"] += 1
        d["objs"].add(obj)
        d["summary"] = e.get("summary", "") or d["summary"]
        k = kind_fail.setdefault(kind, {"fails": 0, "objs": set()})
        k["fails"] += 1
        k["objs"].add(obj)

    # (a) one exact approach dead under multiple objectives (and never worked in the window)
    best = None
    for sig, d in sig_fail.items():
        if (sig not in sig_ok and d["fails"] >= _SYSTEMIC_SIG_FAILS
                and len(d["objs"]) >= _SYSTEMIC_SIG_OBJS):
            if best is None or d["fails"] > best[1]["fails"]:
                best = (sig, d)
    if best:
        sig, d = best
        return {"scope": "sig", "sig": sig, "tool": d["tool"], "kind": d["kind"],
                "fails": d["fails"], "objectives": len(d["objs"]), "summary": d["summary"]}

    # (b) one failure KIND dominating across many objectives (different commands, same wall)
    for kind, k in kind_fail.items():
        if k["fails"] >= _SYSTEMIC_KIND_FAILS and len(k["objs"]) >= _SYSTEMIC_KIND_OBJS:
            return {"scope": "kind", "kind": kind, "fails": k["fails"],
                    "objectives": len(k["objs"])}
    return None


def render_systemic(blk: dict) -> str:
    """Render a systemic_blocker() result as a high-salience context block."""
    if not blk:
        return ""
    if blk.get("scope") == "sig":
        tail = f" — {blk['summary']}" if blk.get("summary") else ""
        return ("## ⚠ Systemic blocker — this is NOT specific to your current objective\n"
                f"`{blk.get('tool')}` has failed ({blk.get('kind')}) ×{blk['fails']} across "
                f"{blk['objectives']} different objectives{tail}. The same wall is blocking "
                "everything — fix the ROOT CAUSE (or tell Boss ONCE what you need and park "
                "the affected objectives); switching objectives will NOT route around it.")
    return ("## ⚠ Systemic failure pattern\n"
            f"Most recent failures are `{blk.get('kind')}` (×{blk['fails']} across "
            f"{blk['objectives']} objectives). This looks environmental (network/credentials/"
            "service down), not task-specific — diagnose the environment before retrying tasks.")


def render_recall(rec: dict) -> str:
    """Render recall() output as a compact context block, or '' if nothing relevant."""
    failures, worked = rec.get("failures", []), rec.get("worked", [])
    if not failures and rec.get("proven") and worked:
        # Success-only recall: a known-good approach for this situation. One compact line per
        # approach — reuse beats re-derivation (the observed skill-reuse gap).
        lines = ["## Episodic recall — you have done this before; reuse what worked:"]
        for d in worked:
            what = f": {d['ok_summary']}" if d.get("ok_summary") else ""
            lines.append(f"  ✓ `{d['tool']}` worked here ×{d['oks']}{what}. Use it — don't re-derive.")
        return "\n".join(lines)
    if not failures:
        return ""
    if rec.get("similar"):
        via = rec.get("via_step", "")
        tail = f' ("{via}")' if via else ""
        lines = [f"## Episodic recall — this resembles a situation you've been in before{tail}:"]
    else:
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
