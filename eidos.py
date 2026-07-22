#!/usr/bin/env python3
"""eiDOS — the always-on autonomous agent.

Entry point: crash recovery, tick loop, signal handling.
"""

import argparse
import collections
import hashlib
import json
import logging
import os
import re
import signal
import subprocess
import sys
import time
import types
from pathlib import Path

from config import Config, load_config
from atomicio import replace_with_retry
from context import assemble_context, _norm_cmd
from compaction import should_compact, compact_briefing, emit_flavor
from llm import complete, LLMError, ReasoningExhausted
from gpu_gate import yield_to_speech, control_wait
from memory import (
    append_observation,
    append_thought,
    has_junk_run,
    is_degenerate,
    log_degeneration,
    read_goal,
    validate_observations,
    write_plan,
)
from parser import parse_tool_call, parse_reply
from persona import (
    load_persona,
    save_persona,
    record_tick,
    record_compaction,
    record_error_recovery,
    compute_traits,
    check_titles,
    format_prefix,
    format_status_line,
)
from rotation import rotate_if_needed, cleanup_old_archives, rotate_llm_log, rotate_metrics, rotate_thoughts, cleanup_old_snapshots
from safety import check_ram, check_disk_space
from telemetry import write_heartbeat, append_metrics, write_activity, get_cpu_pct, record_goal_horizon
from tools import execute_tool, refresh_jobs, collect_finished_jobs, reap_jobs, _BUILTIN_TOOL_NAMES

logger = logging.getLogger("eidos")


# --- Globals for signal handling ---
_shutdown_requested = False


def _handle_signal(signum, frame):
    global _shutdown_requested
    _shutdown_requested = True


def main():
    parser = argparse.ArgumentParser(description="eiDOS autonomous supervisor")
    parser.add_argument("--config", default="config.toml", help="Path to config file")
    parser.add_argument("--llm-url", default=None, help="Override LLM endpoint URL")
    args = parser.parse_args()

    config = load_config(args.config)
    if args.llm_url:
        config.llm_url = args.llm_url

    # eidos runs as LocalSystem (child of the EidosDashboard service, which doesn't export
    # PI_CODING_AGENT_DIR). delegate.py already sets it per-spawn, but make it process-wide so
    # ANY pi eidos ever spawns resolves the user's config (the `house` provider + subagents
    # extension). setdefault: a real service-env value, if ever added, still wins. Derived from the
    # user's home so it's correct on any machine; only set when that dir actually exists (pi is an
    # optional feature — a friend without it just doesn't get delegate, no broken env).
    _pi_agent_dir = Path.home() / ".pi" / "agent"
    if _pi_agent_dir.is_dir():
        os.environ.setdefault("PI_CODING_AGENT_DIR", str(_pi_agent_dir))

    # Configure logging
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )

    # Ensure workspace exists
    config.workspace.mkdir(parents=True, exist_ok=True)
    config.interventions_dir.mkdir(parents=True, exist_ok=True)
    config.snapshots_dir.mkdir(parents=True, exist_ok=True)
    config.outputs_dir.mkdir(parents=True, exist_ok=True)

    # Hot-load any skills eiDOS has previously authored
    try:
        from skills import load_active_skills
        loaded = load_active_skills(config)
        if loaded:
            print(f"[skills] loaded {len(loaded)}: {', '.join(loaded)}")
    except Exception as e:  # noqa: BLE001
        print(f"[skills] load failed: {e}")

    # Reap any background jobs orphaned by the previous run (bg_run/async detach into their own
    # process group, so they survive a kill of eidos and would otherwise run forever).
    try:
        n = reap_jobs(config, kill_all=True)
        if n:
            print(f"[jobs] reaped {n} orphaned background job(s) from the previous run")
    except Exception as e:  # noqa: BLE001
        print(f"[jobs] reap failed: {e}")

    # Signal handling for clean shutdown
    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    # Crash recovery
    wal = recover(config)

    # Load persona
    persona = None
    if config.persona_enabled:
        persona = load_persona(config.workspace)
        compute_traits(persona)
        pfx = format_prefix(persona)
        print(f"{pfx} Online. {format_status_line(persona)}")

    # Main loop
    run_loop(config, persona, wal=wal)


def _pfx(persona, config):
    """Return persona prefix or fallback."""
    if config.persona_enabled and persona:
        return format_prefix(persona)
    return "[eidos]"


def _write_chat_reply(config: Config, tick_number: int, reply_text: str):
    """Append a chat reply to chat_replies.jsonl. Dedup-aware: if eiDOS also `speak`s this same line
    in the tick, the two writes merge into ONE entry (marked spoken) instead of duplicating."""
    from memory import append_chat_line
    append_chat_line(config, reply_text, spoken=False, tick=tick_number)


def _first_sentences(text: str, max_sentences: int = 2, max_chars: int = 200) -> str:
    """The opening 1-2 sentences of a reply — what we voice. TTS runs ~1.5x slower than realtime here
    (Chatterbox's own pipeline; the house model now uses 64k ctx so VRAM isn't the bottleneck), so
    speaking a long paragraph would still lag. The spoken opener + readable text body is the right split."""
    import re as _re
    parts = _re.split(r"(?<=[.!?])\s+", (text or "").strip())
    out = ""
    for p in parts[:max_sentences]:
        if out and len(out) + len(p) > max_chars:
            break
        out = (out + " " + p).strip()
    return out[:max_chars]


def _post_speech(config: Config, text: str) -> bool:
    """POST one utterance to the dashboard's instant-return TTS. Best-effort; True on success."""
    if not text:
        return False
    try:
        import urllib.request as _u
        port = getattr(config, "voice_port", 8098)   # voice is its own service now (phase 8.3)
        sid = str(int(time.time() * 1000))
        req = _u.Request(f"http://127.0.0.1:{port}/api/speech/say",
                         data=json.dumps({"id": sid, "text": text}).encode("utf-8"),
                         headers={"Content-Type": "application/json"}, method="POST")
        _u.urlopen(req, timeout=4).read()
        return True
    except Exception:  # noqa: BLE001 - voice is best-effort; never disturb the tick
        return False


def _auto_speak(config: Config, text: str) -> None:
    """Voice an outgoing chat reply so Boss HEARS every response — voice is first-class, not opt-in.
    We speak only the opening 1-2 sentences; the full text stays readable in chat. Backstop for when
    the model replies with text instead of calling `speak`. Phase 3 fires this EARLY via the streaming
    pump when possible; this is the post-tick fallback for replies the pump didn't already voice."""
    _post_speech(config, _first_sentences(text))


_REPLY_OPEN_RE = re.compile(r"<reply>(.*?)(?:</reply>|$)", re.DOTALL)


class _ReplyVoicePump:
    """Streaming reply→TTS pump (phase 3, BIBLE realtime). Fed the accumulating partial text
    during generation; the instant the reply's opening 1-2 sentences are complete it fires ONE
    speech POST — overlapping TTS synthesis with the rest of the tick's generation instead of
    waiting for the whole response. With reply-first grammar (Boss waiting), the reply is among
    the first tokens, so first-audio drops from ~12s to ~2.5s. Idempotent: fires at most once;
    records what it spoke so the post-tick _auto_speak doesn't repeat it."""

    def __init__(self, config):
        self.config = config
        self.fired = False
        self.spoken_from = ""   # the reply text the early POST was derived from

    def feed(self, partial_text: str) -> None:
        if self.fired or not partial_text:
            return
        m = _REPLY_OPEN_RE.search(partial_text)
        if not m:
            return
        reply_so_far = m.group(1)
        closed = "</reply>" in partial_text
        # Fire only when there is something definitively complete to speak: the reply tag
        # closed, or a sentence terminator is followed by whitespace (the first sentence
        # ended and the next began). Never speak a half-formed fragment.
        if not (closed or re.search(r"[.!?]\s", reply_so_far)):
            return
        if closed:
            speakable = reply_so_far
        else:
            last = max(reply_so_far.rfind("."), reply_so_far.rfind("!"), reply_so_far.rfind("?"))
            speakable = reply_so_far[: last + 1]
        opener = _first_sentences(speakable)
        if opener and _post_speech(self.config, opener):
            self.fired = True
            self.spoken_from = speakable

    def already_spoke(self, final_reply: str) -> bool:
        """True if the pump already voiced this reply's opener — suppress the post-tick speak so
        the opener isn't spoken twice. (The pump only ever voices the opener; the full text stays
        readable in chat, so 'fired at all' is the right suppression signal.)"""
        return self.fired


def _has_pending_interventions(config: Config) -> bool:
    """Check if any un-consumed intervention files exist."""
    idir = config.interventions_dir
    if not idir.exists():
        return False
    for p in idir.iterdir():
        if not p.name.startswith(".") and p.suffix != ".done":
            return True
    return False


def _chat_hold_active(config: Config) -> bool:
    """Listening hold: True when Dean has the chat box focused (a soft pause distinct from
    the operator pause). The dashboard owns the flag file; eiDOS only reads it. Fails OPEN
    to autonomy on any anomaly (missing, corrupt, stale, backward clock, ceiling exceeded).
    A pending intervention overrides the hold so a sent message is answered immediately.
    """
    try:
        path = config.chat_hold_path
        raw = path.read_text(encoding="utf-8", errors="replace")
        import json as _json
        d = _json.loads(raw)
        if not d.get("held"):
            return False
        now = time.time()
        ts = float(d.get("ts", 0) or 0)
        try:
            mtime = path.stat().st_mtime
        except OSError:
            mtime = ts
        age = now - max(ts, mtime)            # freshest of payload ts / file mtime
        if age < 0:                            # backward clock → treat as stale
            return False
        if age > float(config.chat_hold_ttl_s):
            return False
        first = float(d.get("first_held_ts", ts) or ts)
        if now - first > float(config.chat_hold_max_continuous_s):
            return False                       # hard ceiling — never pin the loop forever
        if _has_pending_interventions(config):
            return False                       # a message is waiting — go answer it
        return True
    except (FileNotFoundError, ValueError, OSError):
        return False


# Control-channel seq cursor (phase 4). -1 = unsynced; the first wait syncs it.
_ctl_cursor = -1


def _control_wait_change(config: Config, max_s: float) -> bool:
    """Block until the dashboard's control state CHANGES (pause/resume/hold/chat) or `max_s`
    elapses — the event-driven replacement for the gates' fixed sleeps (ARCH #1). Returns True
    if the channel delivered (event or timeout), False if it's down (caller already slept via
    the fallback nap inside). The sentinel files remain ground truth; callers re-check them."""
    global _ctl_cursor
    res = control_wait(config, _ctl_cursor, max_s=min(max_s, 25.0))
    if res is None:
        time.sleep(min(max_s, 5.0))   # channel down: bounded nap (the old behavior)
        return False
    seq = res.get("seq", 0)
    if seq < _ctl_cursor:
        logger.info("control channel reset (dashboard restarted) — resyncing")
    _ctl_cursor = seq
    return True


def _interruptible_sleep(config: Config, interval: float = None):
    """Sleep up to `interval` (default tick_interval_s), waking EARLY on shutdown, a new Boss
    message, a listening hold, or a pause — via ONE server-side event wait on the dashboard's
    control channel (ARCH #1: notify, not nap-polls). Falls back to the bounded nap-poll when
    the channel is down, so the loop never depends on the dashboard to keep ticking."""
    global _ctl_cursor
    target = config.tick_interval_s if interval is None else float(interval)
    if target <= 0:
        time.sleep(0)   # zero-interval (fast cadence): yield the GIL once; keep ONE time.sleep
        return          # call so this stays a cooperative throttle point (and a test seam)
    deadline = time.monotonic() + target
    while not _shutdown_requested:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        res = control_wait(config, _ctl_cursor, max_s=min(remaining, 25.0))
        if res is None:
            # Channel down — the old nap-poll, bounded; re-check files like v1 did.
            time.sleep(min(2.0, max(0.1, remaining)))
            if _shutdown_requested:
                break
            if _has_pending_interventions(config):
                logger.info("Early wake: pending intervention detected")
                break
            if _chat_hold_active(config):
                break  # reach the listening gate promptly
            continue
        _ctl_cursor = res.get("seq", _ctl_cursor)
        # The snapshot rode back with the event — no extra file reads on the happy path.
        if res.get("interventions"):
            logger.info("Early wake: pending intervention (event)")
            break
        if res.get("held") and _chat_hold_active(config):   # validate TTL/ceiling rules
            break  # reach the listening gate promptly
        if res.get("paused"):
            break  # reach the pause gate promptly
        # else: long-poll timeout or an already-cleared change — keep waiting out the interval


def _adaptive_tick_interval(config: Config, tick_tool_name: str) -> float:
    """Fast cadence when there's MOMENTUM (a real action was just taken, or background jobs are
    still running → results are coming), idle cadence otherwise. A flat sleep throttles an actively
    working agent and wastes cycles when idle; this reacts to work, not a metronome."""
    active = bool(tick_tool_name) and tick_tool_name not in ("thought", "__no_tool__")
    if not active:
        try:
            from tools import _read_jobs
            active = any(j.get("status") == "running" for j in _read_jobs(config))
        except Exception:  # noqa: BLE001
            pass
    return float(getattr(config, "tick_interval_active_s", 0.4)) if active else float(config.tick_interval_s)


def _count_skills(config: Config) -> int:
    """Count authored skill files (for the goal-tension progress signal — a new skill = progress)."""
    try:
        return len([p for p in (config.workspace / "skills").glob("*.py")])
    except Exception:  # noqa: BLE001
        return 0


def _consume_sleep_now(config: Config) -> bool:
    """Operator-forced sleep: True (once) if the dashboard dropped the `eidos.sleep_now` sentinel.
    Consume-and-delete so a single request fires exactly one forced consolidation. Best-effort."""
    try:
        p = config.workspace / "eidos.sleep_now"
        if p.exists():
            p.unlink()
            return True
    except Exception:  # noqa: BLE001
        pass
    return False


def _count_artifacts(config: Config) -> int:
    """Count durable files the creature has authored in its own writable home — the cheap harness fact
    that makes 'organize the workspace' REGISTER as progress. Before this, progress was blind to
    everything but knowledge/skill counts, so an objective like "organize the cocoon" could never
    settle: every file it created was invisible, so every tick on it was a stall, frustration ratcheted
    to park, and it died un-closed (2026-07-13 run: 0/4 objectives closed, frustration pinned at 8). A
    NEW file appearing is real external change — it counts. Rewriting an existing file does not raise
    the count, so it is not a progress-farming lever. Bounded, best-effort, never raises."""
    try:
        import tools as _tools
        root = _tools._creature_root(config)
        if not root.exists():
            return 0
        n = 0
        for p in root.rglob("*"):
            if p.is_file():
                n += 1
                if n >= 100000:            # sanity ceiling — never walk unbounded
                    break
        return n
    except Exception:  # noqa: BLE001
        return 0


def write_wal(config: Config, tick_number: int, ticks_since_compaction: int,
              goal_start_time: float, consecutive_failures: int = 0,
              reasoning_exhaustions: int = 0, current_max_tokens: int = 0,
              last_progress_tick: int = 0):
    """Atomically write tick state to WAL for crash recovery."""
    wal = {
        "tick_number": tick_number,
        "ticks_since_compaction": ticks_since_compaction,
        "goal_start_time": goal_start_time,
        "consecutive_failures": consecutive_failures,
        "reasoning_exhaustions": reasoning_exhaustions,
        "current_max_tokens": current_max_tokens,
        "last_progress_tick": last_progress_tick,
        "ts": time.time(),
    }
    tmp = config.wal_path.with_suffix(".tmp")
    tmp.write_text(json.dumps(wal))
    replace_with_retry(tmp, config.wal_path)


def read_wal(config: Config) -> dict:
    """Read WAL state, return empty dict on missing/corrupt."""
    try:
        return json.loads(config.wal_path.read_text())
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}


def clear_wal(config: Config):
    """Remove WAL after clean shutdown."""
    try:
        config.wal_path.unlink()
    except FileNotFoundError:
        pass


def recover(config: Config) -> dict:
    """Crash recovery: validate state, fix corruption, log restart.
    Returns WAL state dict (may be empty on fresh start).
    """
    print("[eidos] Running crash recovery...")

    # 0. Read WAL (tick state from before crash)
    wal = read_wal(config)
    if wal:
        print(f"[eidos] WAL recovered: tick={wal.get('tick_number')}, "
              f"compaction_gap={wal.get('ticks_since_compaction')}")

    # 1. Verify goal.md
    goal = read_goal(config)
    if not goal:
        print("[eidos] WARNING: No goal.md found. Agent will idle until one is created.")

    # 2. Create plan.md (working memory) if missing, or restore from snapshot if empty
    plan_missing = not config.plan_path.exists()
    plan_empty = False
    if not plan_missing:
        try:
            plan_empty = config.plan_path.stat().st_size == 0
        except OSError:
            plan_empty = True

    if plan_missing or plan_empty:
        # Try restoring from most recent dream snapshot (either filename generation)
        restored = False
        if config.snapshots_dir.exists():
            snapshots = sorted(
                list(config.snapshots_dir.glob("plan_snapshot_*"))
                + list(config.snapshots_dir.glob("memory_snapshot_*")),
                key=lambda p: p.stat().st_mtime,
                reverse=True,
            )
            if snapshots:
                try:
                    content = snapshots[0].read_text()
                    if content.strip():
                        write_plan(config, content)
                        restored = True
                        print(f"[eidos] Restored plan.md from snapshot: {snapshots[0].name}")
                        append_observation(config, {
                            "tick": 0,
                            "tool": "system",
                            "success": True,
                            "output": f"Restored plan from snapshot {snapshots[0].name} after {'missing' if plan_missing else 'empty'} plan.md.",
                        })
                except OSError:
                    pass
        if not restored:
            write_plan(config, "# Plan\nFresh start. No prior context.")
            print("[eidos] Created initial plan.md")

    # 3. Validate observations.jsonl
    truncated = validate_observations(config)
    if truncated:
        print(f"[eidos] Truncated {truncated} malformed line(s) from observations.jsonl")
        append_observation(config, {
            "tick": 0,
            "tool": "system",
            "success": False,
            "output": (f"Crash recovery: {truncated} corrupted observation(s) "
                       f"removed from observations.jsonl. Recent history may be incomplete."),
        })

    # 4. Scan background jobs, mark dead ones
    jobs = refresh_jobs(config)
    dead = [j for j in jobs if j["status"] != "running"]
    if dead:
        print(f"[eidos] Found {len(dead)} completed/dead background jobs")
        dead_names = ", ".join(j.get("cmd", "?")[:60] for j in dead)
        append_observation(config, {
            "tick": 0,
            "tool": "system",
            "success": False,
            "output": (f"Background jobs died during downtime: {dead_names}. "
                       f"Their results are unavailable. Re-launch if still needed."),
        })

    # 5. Log recovery with full crash context
    if wal:
        recovery_detail = (
            f"eiDOS recovered from crash. Resuming at tick {wal.get('tick_number', '?')}. "
            f"State before crash: {wal.get('consecutive_failures', 0)} consecutive LLM failures, "
            f"{wal.get('reasoning_exhaustions', 0)} reasoning exhaustions, "
            f"max_tokens was {wal.get('current_max_tokens', config.llm_max_tokens)}. "
            f"Review recent observations — the last action may not have completed."
        )
    else:
        recovery_detail = "eiDOS starting fresh. No prior crash state found."
    append_observation(config, {
        "tick": 0,
        "tool": "system",
        "success": True,
        "output": recovery_detail,
    })

    # 6. Rotate logs and clean old archives
    if rotate_if_needed(config):
        print("[eidos] Rotated observations.jsonl")
    deleted = cleanup_old_archives(config)
    if deleted:
        print(f"[eidos] Cleaned {deleted} old archive(s)")

    return wal


_THOUGHT_TAG_RE = re.compile(r"<tool>.*?</tool>|<args>.*?</args>|<reply>.*?</reply>",
                             re.DOTALL | re.IGNORECASE)


_LEADING_ELLIPSIS_RE = re.compile(r"^\s*(?:\.{2,}|…)\s*")


def _extract_thought(response: str) -> str:
    """This tick's reasoning — the model's raw output minus the action/reply tags."""
    if not response:
        return ""
    thought = _THOUGHT_TAG_RE.sub("", response).strip()
    # The 'continuous stream / mid-thought' framing makes the model open nearly EVERY thought with a
    # leading ellipsis (its "I'm continuing the stream" marker) — 100% of thoughts, incl. the very
    # first. A thought is a thought, not a perpetual mid-sentence; strip the artifact. (The tick-prompt
    # framing that induces it is also softened, so this is a backstop, not the only fix.)
    return _LEADING_ELLIPSIS_RE.sub("", thought)


# A newborn should THINK like a newborn — a fragment, not a treatise. But the stored thought is what
# the creature re-reads as its own recent voice (the history thread) and what thoughts.jsonl / the
# dashboard show, so a young creature that writes a 70-word essay then marinates in a dozen of its own
# essays next tick locks into an over-elaborate register (self-imitation). We clamp the STORED thought
# by life-stage — the young remember a fragment; depth (length) is EARNED, so adult/guardian are never
# clamped. This runs AFTER parse_tool_call has read the FULL response, so it NEVER truncates an action;
# it only shortens the memory, which is what starves the self-imitation loop. (Register — word choice —
# is shaped elsewhere: the stage tone-cue, the flatter base prompt, and the no-projects gate.)
_STAGE_THOUGHT_MAX_SENTENCES = {"egg": 2, "hatchling": 2, "juvenile": 3}   # adult/guardian: unclamped
_STAGE_THOUGHT_MAX_WORDS = {"egg": 22, "hatchling": 22, "juvenile": 55}
_SENTENCE_SPLIT_RE = re.compile(r'(?<=[.!?…])\s+')


def _clamp_thought_for_stage(thought: str, stage: str) -> str:
    """Trim a stored thought to a life-stage-appropriate length — whole sentences first (never a
    mid-word cut), then a hard word backstop for a single run-on. Young stages only; a mature creature
    keeps the full thought it has earned. Fail-open: unknown stage or empty text → returned unchanged."""
    if not thought or stage not in _STAGE_THOUGHT_MAX_WORDS:
        return thought
    parts = _SENTENCE_SPLIT_RE.split(thought.strip())
    clipped = " ".join(parts[:_STAGE_THOUGHT_MAX_SENTENCES[stage]]).strip()
    words = clipped.split()
    cap = _STAGE_THOUGHT_MAX_WORDS[stage]
    if len(words) > cap:
        clipped = " ".join(words[:cap]).rstrip(",;:—- ") + "…"
    return clipped or thought


# --- Phase 1.1: per-tick hooks for the 2 drive organs migrated onto the organ registry
#     (goal-tension, curiosity). Each is a pure f(ctx) closure over the tick's locals, which the loop
#     packs into `ctx` (a SimpleNamespace) and hands to `organ_registry.run_post_tick(ctx)`. The
#     bodies are lifted VERBATIM from the old inline call sites — same inputs, same effects, same bus
#     events — so this is a strictly behaviour-preserving change of *dispatch*, not of behaviour. Each
#     is guarded by the registry (I5), so its logging matches the old per-organ try/except. ---

def _goaltension_post_tick(ctx):
    """Goal-tension drive (Ventral Striatum): fold this tick's objective state into the incompletion/
    regret pressure. Lifted from the old inline block: an OPEN objective with no progress charges the
    tension (a frustrated one harder); progress discharges it; past threshold it raises a bounded
    arousal floor. Initiative temperament scales how hard it bites."""
    _active = ctx.gate.get("active")
    _open = bool(_active)
    _frac = (float(_active.get("frustration", 0)) / max(1, (ctx.park_at or ctx.obj.FRUST_PARK))
             if _active else 0.0)
    _init = ctx.temperament.initiative if ctx.temperament is not None else 0.5
    ctx.goaltension.observe(made_progress=ctx.made_progress, open_objective=_open,
                            frustration_frac=_frac, initiative=_init,
                            open_commission=bool(getattr(ctx, "commission_open", False)))


def _curiosity_post_tick(ctx):
    """Curiosity drive: turn the world-model's LEARNING PROGRESS at this transition into a small
    intrinsic-reward bonus + restlessness. Lifted from the old inline block inside the reward-learner
    step: observe the (prev_sit, prev_act -> this_sit) transition, read last_progress, fold it into
    curiosity. The intrinsic bonus is written back onto ctx for the (non-migrated) learner to consume
    this same tick — so the value still flows exactly as before."""
    if ctx.worldmodel is not None and ctx.wm_prev_sit is not None:
        ctx.worldmodel.observe(ctx.wm_prev_sit, ctx.wm_prev_act, ctx.tick_situation)
        _progress = float(getattr(ctx.worldmodel, "last_progress", 0.0) or 0.0)
        ctx.intrinsic = ctx.curiosity.observe(_progress)


# ================================================================================================
# Pillars 5.5 — the wiring pass: every dark organ's call sites, STILL DARK (PILLARS_TODO 5.5).
#
# The hub below is constructed ONLY when at least one pillars flag is on; with every flag off (the
# default) run_loop keeps `pillars = None`, the tick body's new branches are all `if pillars is not
# None`, and NO pillars module is even imported — the flags-off loop is byte-identical to the
# unwired code. Every method is guarded per subsystem (I5): one organ's exception is logged and
# swallowed, never breaking the tick. Flipping flags one at a time (the 5.5 schedule) is what
# actually brings each organ online; this class only provides the call sites.
#
# Registry note (1.1): run_pre_tick is invoked here (behind the salience flag — the gate is its
# only registrant, so single execution is preserved); run_on_sleep runs inside the sleep engine's
# OrganSleepHooksJob (behind the sleep flag). run_post_tick is invoked in the tick body (the
# deferred seam, closed): the old inline goal-tension/curiosity blocks are retired and the
# registry's hooks are the ONE dispatch — the loop packs their inputs into a per-tick ctx and
# curiosity's intrinsic bonus rides that ctx to the (non-migrated) reward learner.
# ================================================================================================
_PILLARS_WIRED_FLAGS = (
    "pillars_memory_engram_enabled",     # 2.1 engram economy (consolidator for the sleep jobs)
    "pillars_memory_manager_enabled",    # 2.2 importer + 4-layer recall + encode-through-manager
    "pillars_bet_ledger_enabled",        # 2.3 open_bets on recall injection + glue.settle_bets
    "pillars_sleep_engine_enabled",      # 2.4 run_sleep at the sleep window + adenosine accounting
    "pillars_expectations_enabled",      # 4.1 predict tool + glue.settle_predictions + awaiting block
    "pillars_salience_gate_enabled",     # 1.3 gate organ registered + relevance_set published
    "pillars_quests_enabled",            # 5.1 quest window + event-driven cadence + adjudication
    "pillars_news_enabled",              # 4.4 three-source ingest + presence-gated surfacing
    "pillars_mastery_gates_enabled",     # 4.3 tier outcomes + level candidacy through the gate
    "pillars_learning_xp_enabled",       # 4.2 progress tracker fed from adjudicated wrongness
    "pillars_administrator_enabled",     # 5.2 event-driven check-ins (lazy llm; never on a timer)
    "pillars_tool_unlocks_enabled",      # 5.x TOOL_PROGRESSION ladder: unit grants at the quest
                                         #     seams, milestone adjudication + I8 probe, the felt
                                         #     moment, stage-expressed alleles + phenotype artifact
    "pillars_commission_enabled",        # COMMISSION_PLAN.md: standing orders — verbs registered,
                                         #     verdicts/claims settled at the after_outcome beat
    "operator_directives_enabled",       # OPERATOR_DIRECTIVES: the System frames Charlie's command
                                         #     as a priority objective (needs the hub for _live_llm)
    "reminders_enabled",                 # the `remind` primitive: tool registration + per-tick
                                         #     due-check both live behind the hub construction
)


def _pillars_any_enabled(config) -> bool:
    """True iff any wired pillars flag is on. False (the default) keeps the hub un-constructed —
    the flags-off ground state adds zero work and zero imports to the tick."""
    return any(getattr(config, f, False) for f in _PILLARS_WIRED_FLAGS)


# --- TOOL_PROGRESSION I8: the organ-reachability probe (decision #1) -----------------------------
# A granted limb that 500s is a felt lie: a service-gated unit (senses) holds PENDING until the
# organ actually answers. The probe is a bounded HTTP round-trip, memoized per process so per-tick
# adjudication never hammers a dead port (voice :8098 is down on Sprinter today — the hold is the
# expected steady state there). Tests inject their own probe through the hub's `unlock_probe` seam.
_UNLOCK_PROBE_TIMEOUT_S = 1.0    # declared: the reachability check costs the adjudicator at most
                                 # ~1s per TTL window — bounded, never a stalled tick
_UNLOCK_PROBE_TTL_S = 60.0       # declared: memoize the answer ~60s; a just-started organ lands
                                 # its held grant within a minute, a dead one costs ~1s/minute
_unlock_probe_cache: dict = {}   # service -> (monotonic_ts, answered)


def _probe_service(config, service: str) -> bool:
    """Does the named organ actually answer (I8)? `voice` = an HTTP round-trip to the voice
    service (config.voice_port, default 8098). ANY HTTP status counts as an answer — a 404 is
    still a live socket — while refusal/timeout is silence. Unknown service names never answer
    (an organ is never guessed back). Never raises."""
    now = time.monotonic()
    hit = _unlock_probe_cache.get(service)
    if hit is not None and (now - hit[0]) < _UNLOCK_PROBE_TTL_S:
        return bool(hit[1])
    answered = False
    if service == "voice":
        try:
            import urllib.error
            import urllib.request
            port = int(getattr(config, "voice_port", 8098) or 8098)
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/",
                                            timeout=_UNLOCK_PROBE_TIMEOUT_S):
                    answered = True
            except urllib.error.HTTPError:
                answered = True    # an HTTP error IS an answer — the organ is alive
            except Exception:  # noqa: BLE001 - refused / timed out / unreachable: no answer
                answered = False
        except Exception:  # noqa: BLE001 - probing must never wound the adjudicator
            answered = False
    _unlock_probe_cache[service] = (now, answered)
    return answered


class _Pillars:
    """The Pillars 5.5 wiring hub: owns the flag-on subsystem instances and exposes the loop's
    call sites (pre_tick / recall_block / open_bets / after_outcome / sleep_window / on_presence).
    Each subsystem is built and driven ONLY behind its own flag; each call is guarded (I5)."""

    def __init__(self, config, *, bus=None, neuromod=None, organ_registry=None,
                 curiosity=None, learner=None):
        self.config = config
        self.bus = bus
        self.neuromod = neuromod
        self.organ_registry = organ_registry
        self.curiosity = curiosity
        self.learner = learner
        self.temperament = None      # DMN Temperament — set by run_loop after construction so the
                                     # sleep calibration job can apply its bounded caution step
        self.metabolism = None       # Metabolism — set by run_loop so a confirmed commission task
                                     # can feed the reserve (work earns food)
        self.manager = None          # 2.2 MemoryManager
        self.bets = None             # 2.3 BetLedger
        self.news = None             # 4.4 NewsQueue
        self.quests = None           # 5.1 quests.System
        self.tracker = None          # 4.2 ProgressTracker
        self.salience = None         # 1.3 SalienceGate
        self.llm = None              # lazy (messages, grammar=None) -> str; TEST SEAM: inject a
                                     # mock here — it is never constructed in mock mode, so tests
                                     # can never reach a live model by accident.
        self.injected = []           # engrams this tick's recall injected (the bet slate, 2.3)
        self._persona = None         # the live persona dict, refreshed each after_outcome
        self._level_snapshot = None  # 4.3: the gate-authoritative level (only apply_level_up moves it)
        self._candidacy_fired_for = None   # 5.2: level_candidacy is EDGE-triggered — once per level
                                           # crossing, not every tick past the floor (18 proposal
                                           # bricks/hour came from the level-triggered flood)
        self.unlock_probe = None     # I8 TEST SEAM: inject a callable(service)->bool; None = the
                                     # process-memoized voice probe (_probe_service)
        self._stage_seen = None      # stage-transition memo: skip the genome read while the
                                     # derived stage hasn't moved (ground truth stays the genome)
        self._unlock_books_checked = False   # load-or-birth migration runs once per process
        self._aden_mark = time.monotonic()   # wake-time accounting anchor for adenosine (2.4)
        self._last_anomaly_sig = ""  # 4.4 anomaly source de-dup (report by exception, once per streak)
        c = config

        # 2.2 — the memory manager (+ the idempotent importer, run once at boot; read-only on legacy)
        if getattr(c, "pillars_memory_manager_enabled", False):
            try:
                from memory_manager import MemoryManager
                self.manager = MemoryManager(c, neuromod=neuromod)
                counts = self.manager.import_all()
                if any(counts.values()):
                    logger.info("pillars memory import: %s", counts)
            except Exception as e:  # noqa: BLE001 - one organ's fault never blocks the others (I5)
                logger.warning("pillars memory manager init failed: %s", e)
                self.manager = None

        # 2.3 — the bet ledger (shares the manager's consolidator so strength writes stay single-writer)
        if getattr(c, "pillars_bet_ledger_enabled", False):
            try:
                import bets as _bets
                self.bets = (_bets.BetLedger(c, consolidator=self.manager.consolidator)
                             if self.manager is not None else _bets.BetLedger(c))
            except Exception as e:  # noqa: BLE001
                logger.warning("pillars bet ledger init failed: %s", e)
                self.bets = None

        # 4.1 — the predict tool joins the registry (register_predict_tool is itself flag-gated)
        if getattr(c, "pillars_expectations_enabled", False):
            try:
                from tools import register_predict_tool
                register_predict_tool(c)
            except Exception as e:  # noqa: BLE001
                logger.warning("pillars predict tool registration failed: %s", e)

        # WORLD_PLAN §5 (W1) — the `go` movement tool joins the registry (register_world_tool is
        # itself flag-gated on `world_enabled`). Flag off (default) → absent from TOOLS, never in
        # the grammar; the world stays fully dark (W7). Exception-guarded like every flag organ.
        if getattr(c, "world_enabled", False):
            try:
                from tools import register_world_tool
                register_world_tool(c)
            except Exception as e:  # noqa: BLE001
                logger.warning("world go tool registration failed: %s", e)

        # OPERATOR_DIRECTIVES — the `remind` tool joins the registry (register_reminders_tool is
        # flag-gated on `reminders_enabled`). Flag off (default) → absent, dark.
        if getattr(c, "reminders_enabled", False):
            try:
                from tools import register_reminders_tool
                register_reminders_tool(c)
            except Exception as e:  # noqa: BLE001
                logger.warning("remind tool registration failed: %s", e)

        # The Commission (COMMISSION_PLAN.md) — the standing-order organ: verbs join the registry
        # (register_commission_tools is itself flag-gated) and the engine settles verdicts/claims
        # at the after_outcome beat.
        self.commission = None
        self._commission_open = False        # memo for the goal-tension drive (a fact, not a file)
        self._commission_claimed_seen: set = set()   # claimed-task ids already announced as news
        if getattr(c, "pillars_commission_enabled", False):
            try:
                from tools import register_commission_tools
                register_commission_tools(c)
                from commission import Commission
                self.commission = Commission(c)
                live = self.commission.live()
                self._commission_open = any(t.state == "open" for t in live)
                self._commission_claimed_seen = {t.id for t in live
                                                 if t.state == "done_claimed"}
            except Exception as e:  # noqa: BLE001
                logger.warning("pillars commission init failed: %s", e)
                self.commission = None

        # 1.3 — the salience gate registers with the 1.1 organ registry (pre_tick intake)
        if getattr(c, "pillars_salience_gate_enabled", False) and bus is not None:
            try:
                from nervous.salience import SalienceGate
                self.salience = SalienceGate(bus, config=c)
                if organ_registry is not None:
                    self.salience.register(organ_registry)
            except Exception as e:  # noqa: BLE001
                logger.warning("pillars salience gate init failed: %s", e)
                self.salience = None

        # 4.4 — the news queue (engram writes ride the same single consolidator)
        if getattr(c, "pillars_news_enabled", False):
            try:
                from news import NewsQueue
                self.news = (NewsQueue(c, consolidator=self.manager.consolidator)
                             if self.manager is not None else NewsQueue(c))
            except Exception as e:  # noqa: BLE001
                logger.warning("pillars news queue init failed: %s", e)
                self.news = None

        # 5.1 — the quest System (reward sink threads config through award_xp — 4.3's gate hold)
        if getattr(c, "pillars_quests_enabled", False):
            try:
                import quests as _quests
                self.quests = _quests.System(c, reward_sink=self._quest_reward_sink)
            except Exception as e:  # noqa: BLE001
                logger.warning("pillars quest system init failed: %s", e)
                self.quests = None

        # 4.2 — the learning-progress tracker
        if getattr(c, "pillars_learning_xp_enabled", False):
            try:
                from learning_progress import ProgressTracker
                self.tracker = ProgressTracker(c)
            except Exception as e:  # noqa: BLE001
                logger.warning("pillars progress tracker init failed: %s", e)
                self.tracker = None

    def describe(self) -> str:
        """One line for the boot print: which organs this hub actually wired."""
        parts = []
        for name, obj in (("memory", self.manager), ("bets", self.bets), ("news", self.news),
                          ("quests", self.quests), ("progress", self.tracker),
                          ("salience", self.salience)):
            if obj is not None:
                parts.append(name)
        c = self.config
        for name, flag in (("sleep", "pillars_sleep_engine_enabled"),
                           ("expectations", "pillars_expectations_enabled"),
                           ("gates", "pillars_mastery_gates_enabled"),
                           ("administrator", "pillars_administrator_enabled"),
                           ("unlocks", "pillars_tool_unlocks_enabled")):
            if getattr(c, flag, False):
                parts.append(name)
        return ", ".join(parts) or "(none)"

    # --- the lazy mind (5.2 administrator + 2.4 distillation) ------------------------------------
    def _live_llm(self):
        """The (messages, grammar=None) -> str callable over the EXISTING llm client (llm.py),
        built lazily on first need. In mock mode / the isolated test env it stays None — the
        distillation job no-ops cleanly and the administrator stays quiet, so no test can ever
        reach a live model (the mock seam: tests inject `self.llm` directly)."""
        if self.llm is not None:
            return self.llm
        if getattr(self.config, "mock_mode", False) or os.environ.get("EIDOS_NO_DASHBOARD"):
            return None
        cfg = self.config
        try:
            from llm import complete as _complete
        except Exception as e:  # noqa: BLE001
            logger.warning("pillars llm client unavailable: %s", e)
            return None

        def _call(messages, grammar=None):
            return _complete(messages, cfg, grammar=grammar)

        self.llm = _call
        return _call

    # --- focus derivation (mechanical: objective title + plan step + hot commission task) --------
    def _focus_terms(self) -> list:
        terms = []
        try:
            import objectives as _obj
            a = _obj.get_active(self.config)
            if a:
                terms += str(a.get("title", "")).split()
        except Exception:  # noqa: BLE001
            pass
        try:
            from context import _plan_next_step
            terms += _plan_next_step(self.config).split()
        except Exception:  # noqa: BLE001
            pass
        # Task-conditioned recall: the HOT commission task's own words join the focus, so memory
        # and salience surface what the creature knows about the work in front of it (a reopened
        # task's feedback rides along — Charlie's words become recall keys). Bounded sub-slice so
        # the immediate step always keeps the head of the query.
        try:
            if self.commission is not None:
                _hot = self.commission.hot_task()
                if _hot is not None:
                    terms += f"{_hot.title} {_hot.detail} {_hot.verdict_note}".split()[:12]
        except Exception:  # noqa: BLE001
            pass
        return [t for t in terms if t][:32]

    def focus_query(self) -> str:
        return " ".join(self._focus_terms())

    # --- pre-deliberation call site ---------------------------------------------------------------
    def pre_tick(self, tick_number: int) -> None:
        """Before context assembly: adenosine wake-time accounting (2.4), relevance_set publication
        + gate intake via the registry's pre_tick phase (1.3). Guarded per subsystem (I5)."""
        c = self.config
        if self.neuromod is not None and getattr(c, "pillars_sleep_engine_enabled", False):
            try:
                aden = getattr(self.neuromod, "adenosine", None)
                now = time.monotonic()
                if aden is not None:
                    aden.accumulate((now - self._aden_mark) / 3600.0)
                self._aden_mark = now
            except Exception as e:  # noqa: BLE001
                logger.warning("pillars adenosine accounting failed: %s", e)
        if self.salience is not None:
            try:
                terms = self._focus_terms()
                if terms:
                    from nervous.salience import publish_relevance_set
                    publish_relevance_set(self.bus, terms)
            except Exception as e:  # noqa: BLE001
                logger.warning("pillars relevance publish failed: %s", e)
            try:
                if self.organ_registry is not None:
                    # Only the gate registers a pre_tick hook today; the registry guards each hook.
                    self.organ_registry.run_pre_tick(types.SimpleNamespace(tick=tick_number))
            except Exception as e:  # noqa: BLE001
                logger.warning("pillars gate pre-tick failed: %s", e)

    # --- recall (2.2 takes over the legacy cascade when its flag is on) ---------------------------
    def recall_block(self, *, situation: str, query: str) -> str:
        """The manager's 4-layer recall, rendered for the situation section of context. Records the
        injected engrams on `self.injected` so open_bets can wager on exactly what was injected.
        Returns '' (and an empty slate) when the manager is off or recall faults."""
        self.injected = []
        if self.manager is None:
            return ""
        try:
            got = self.manager.recall(query or "", situation=situation or None)
            self.injected = list(got)
            if not got:
                return ""
            lines = ["## Recalled from memory (ranked relevance × strength)"]
            for e in got:
                lines.append(f"- [{e.kind}] {e.body}")
            return "\n".join(lines)
        except Exception as e:  # noqa: BLE001
            logger.warning("pillars recall failed: %s", e)
            self.injected = []
            return ""

    def open_bets(self, tick_number: int) -> None:
        """2.3: every engram injected into this tick's decision is an open wager on the outcome."""
        if self.bets is None:
            return
        try:
            self.bets.open_bets(tick_number, self.injected)
        except Exception as e:  # noqa: BLE001
            logger.warning("pillars open_bets failed: %s", e)

    # --- post-adjudication call site --------------------------------------------------------------
    def after_outcome(self, *, tick: int, tool: str, args, success: bool, fail_kind: str,
                      situation: str, summary: str, event_text: str, persona) -> None:
        """After glue.record_outcome for this tick: settle bets (2.3) + predictions (4.1), feed
        learning progress (4.2), encode the experience (2.2), ingest news (4.4), adjudicate the
        active quest (5.1), feed tier outcomes + level candidacy (4.3). Guarded per subsystem."""
        c = self.config
        self._persona = persona
        import glue as _glue

        # 2.3 — settle this tick's memory bets against the adjudicated outcome (glue-only settler)
        if getattr(c, "pillars_bet_ledger_enabled", False):
            try:
                _glue.settle_bets(c, tick=tick, action_tool=tool or "", action_args=args,
                                  ledger=self.bets)
            except Exception as e:  # noqa: BLE001
                logger.warning("pillars settle_bets failed: %s", e)

        # 4.1 — the closure pass (claim-measurement + deadline + matching-event grounds; glue is
        # the only closer). Each settlement is then WRITTEN INTO THE STREAM: a verdict the creature
        # never experiences is a verdict it can never learn from — the old silent closures left it
        # unable to discover why its bets kept dying, and its calibration never had a chance.
        closures = []
        if getattr(c, "pillars_expectations_enabled", False):
            try:
                closures = _glue.settle_predictions(c, event_text=event_text or "", tick=tick,
                                                    reward=self.learner, curiosity=self.curiosity,
                                                    stats=self._quest_stats(persona))
            except Exception as e:  # noqa: BLE001
                logger.warning("pillars settle_predictions failed: %s", e)
                closures = []
        for cl in closures:
            try:
                p = cl.prediction
                measured = "" if cl.actual is None else f"; measured {cl.actual}"
                how = {"claim": "the claim came true", "event": "a matching event landed",
                       "deadline": "the deadline passed"}.get(cl.reason, cl.reason)
                verdict = "CAME TRUE" if cl.outcome else "DID NOT COME TRUE"
                append_observation(c, {
                    "tick": tick, "tool": "bet_settled", "success": bool(cl.outcome),
                    "output": (f"[bet settled] \"{p.statement}\" → {verdict} "
                               f"({how}; claim: {p.target or '—'}{measured}; "
                               f"you held it at {p.confidence:.0%})")})
            except Exception as e:  # noqa: BLE001 - the notice is best-effort; the close stands
                logger.warning("bet settlement notice failed: %s", e)

        # 4.2 — learning progress observes closure wrongness + this tick's adjudicated outcome
        if self.tracker is not None:
            try:
                import learning_progress as _lp
                for cl in closures:
                    conf = float(getattr(cl.prediction, "confidence", 0.5) or 0.5)
                    wrong = (1.0 - conf) if cl.outcome else conf
                    self.tracker.observe(_lp.domain_key(getattr(cl.prediction, "domain", "")), wrong)
                if tool and tool not in ("thought", "parse_error", "chat_reply"):
                    obj_part = situation.split("|", 1)[0] if situation else ""
                    dom = _lp.domain_key(obj_part, tool)
                    self.tracker.observe(dom, 0.0 if success else 1.0)
            except Exception as e:  # noqa: BLE001
                logger.warning("pillars progress observe failed: %s", e)

        # 2.2 — encode this tick as fresh experience (emotional stamp read live inside the manager).
        # The body is what a future recall injects verbatim — the step/summary shards are cleaned
        # (plan-list markers, mid-word slices) or the recalled line reads back as a malformed
        # thought; a step that cleans away entirely gets no "While ," shard.
        if self.manager is not None and tool and tool not in ("parse_error",):
            try:
                import episodes as _ep
                outcome = "succeeded" if success else (
                    f"failed ({fail_kind})" if fail_kind else "failed")
                step = _ep.clean_fragment(
                    situation.split("|", 1)[1] if situation and "|" in situation else "",
                    _ep.STEP_CHARS)
                note = _ep.clean_fragment(summary, _ep.SUMMARY_CHARS)
                parts = ([f"While {step},"] if step else []) + [f"`{tool}` {outcome}."]
                if note:
                    parts.append(note)
                self.manager.encode("episode", " ".join(parts), tick=tick,
                                    stats={"situation": situation or ""})
            except Exception as e:  # noqa: BLE001
                logger.warning("pillars encode failed: %s", e)

        # 4.4 — news sources: high-surprise closures + the repeated-dead-end anomaly
        if self.news is not None:
            try:
                for cl in closures:
                    self.news.ingest(cl.residue, "expectation", surprise=cl.surprise)
                outs = _glue.recent_outcomes(c)
                sig = _glue.repeated_failure_signature(outs)
                if sig and sig != self._last_anomaly_sig:
                    self._last_anomaly_sig = sig
                    self.news.ingest({"summary": f"anomaly: the same action failed 3+ times in a "
                                                 f"row (sig {sig})", "surprise": 3.0}, "anomaly")
                elif not sig:
                    self._last_anomaly_sig = ""
            except Exception as e:  # noqa: BLE001
                logger.warning("pillars news ingest failed: %s", e)

        # Commission (COMMISSION_PLAN.md): settle at the same beat quests adjudicate — pending
        # operator verdicts first (the chat channel), then any checkable claims measured against
        # the SAME typed stats dict. Payouts go through the existing single writers.
        if self.commission is not None:
            try:
                settlements = self.commission.consume_verdicts()
                settlements += self.commission.settle_claims(self._quest_stats(persona))
                for s in settlements:
                    self._commission_settle(s, persona, tick=tick)
                live = self.commission.live()
                self._commission_open = any(t.state == "open" for t in live)
                # Newly-claimed tasks ride the news queue ONCE — so the operator's next check-in
                # says "these await your verdict" instead of the claim quietly going stale (the
                # reinforcement schedule stays honest even across days away).
                claimed = {t.id: t for t in live if t.state == "done_claimed"}
                fresh = sorted(set(claimed) - self._commission_claimed_seen)
                self._commission_claimed_seen = set(claimed)
                if fresh and self.news is not None:
                    ids = ", ".join(f"#{i}" for i in fresh)
                    self.news.ingest({"summary": f"commission task(s) {ids} claimed done — "
                                                 "awaiting your verdict (/commission done|reject)",
                                      "surprise": 1.5}, "commission")
            except Exception as e:  # noqa: BLE001
                logger.warning("pillars commission settle failed: %s", e)

        # 5.1 — adjudicate the active quest against typed stats. Tier standing moves ONLY on
        # quest ADJUDICATIONS (inside _on_quest_closed) — never on a tick's tool-call success.
        # (The old per-tool-call record_tier_outcome here suspended tier 1 after any 5 clumsy
        # calls in a row — a hatchling's syntax fumbles were being graded as failed mastery.)
        if self.quests is not None:
            try:
                active = self.quests.store.active()
                if active is not None:
                    r = self.quests.check(active, self._quest_stats(persona))
                    if r.get("passed") or r.get("expired"):
                        self._on_quest_closed(r, persona, tick=tick)
            except Exception as e:  # noqa: BLE001
                logger.warning("pillars quest adjudication failed: %s", e)

        # 4.3 — the level moves ONLY through the gate; candidacy checked when the XP floor crosses
        if getattr(c, "pillars_mastery_gates_enabled", False) and persona is not None:
            try:
                import level_gates as _lg
                if (self._level_snapshot is not None
                        and persona.get("level") != self._level_snapshot):
                    # a config-less award path recomputed level-from-XP; the gate holds it still
                    persona["level"] = self._level_snapshot
                cur = int(persona.get("level", 1) or 1)
                if int(persona.get("xp", 0) or 0) >= _lg.xp_for_level(cur + 1):
                    ok, _report = _lg.can_level(persona, c)
                    if ok:
                        _lg.apply_level_up(persona, c)
                        if self.news is not None:
                            try:
                                self.news.ingest({"summary": f"level up: {cur} → "
                                                             f"{persona.get('level')}",
                                                  "surprise": 2.0}, "quest")
                            except Exception:  # noqa: BLE001
                                pass
                    # EDGE-triggered (once per level crossing): the floor stays crossed for
                    # hours while the gate holds, and a per-tick event had the Administrator
                    # drafting 18 quests an hour. The crossing is the event; the state isn't.
                    if self._candidacy_fired_for != cur:
                        self._candidacy_fired_for = cur
                        self._event("level_candidacy", persona)
                self._level_snapshot = int(persona.get("level", 1) or 1)
            except Exception as e:  # noqa: BLE001
                logger.warning("pillars level gate failed: %s", e)

        # TOOL_PROGRESSION milestones + the felt moment: glue adjudicates the growing body over
        # the SAME typed stats dict quest criteria read (a quest pass THIS tick already counts),
        # and any granted-but-unrendered unit lands in the observation stream this tick. Then the
        # CREATURE_GENETICS stage seam: metamorphosis reads the environment at a stage crossing.
        # Both fail-open (I5), both dark unless the ladder flag is on.
        if getattr(c, "pillars_tool_unlocks_enabled", False):
            try:
                self._unlock_milestones(persona, tick=tick)
            except Exception as e:  # noqa: BLE001
                logger.warning("pillars unlock milestones failed: %s", e)
            try:
                self._stage_transition(persona)
            except Exception as e:  # noqa: BLE001
                logger.warning("pillars stage transition failed: %s", e)

    # --- quests: stats surface, reward sink, closure bookkeeping, event-driven cadence ------------
    def _quest_stats(self, persona) -> dict:
        """The typed stats dict criteria are checked against (§0.5: glue judges — persona counters,
        the drill/remedial stat files their machinery writes, and the adjudicated sections below).
        Every counter here is a glue-settled FACT from a manifest/ledger/store, never a tools_used
        attempt (genesis-01 once passed on a FAILED create_skill call — attempt counters increment
        on failure; manifest/ledger facts cannot). An absent stat never passes; every failed read
        degrades to zero, which no >=1 criterion can mistake for evidence."""
        stats = {"persona": dict(persona or {})}
        for name in ("drills", "remedial"):
            try:
                p = self.config.state_dir / f"{name}.json"
                stats[name] = json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
                if not isinstance(stats[name], dict):
                    stats[name] = {}
            except Exception:  # noqa: BLE001
                stats[name] = {}
        # skills: manifest facts — a skill counts only once it is LIVE in the manifest.
        try:
            import skills as _skills
            ents = (_skills._load_manifest(self.config).get("skills") or {}).values()
            live = [e for e in ents if e.get("status") in _skills._LIVE_STATUSES]
            stats["skills"] = {"live_count": len(live),
                               "trusted_count": sum(1 for e in live
                                                    if e.get("status") == "trusted")}
        except Exception:  # noqa: BLE001 - no manifest => no evidence
            stats["skills"] = {"live_count": 0, "trusted_count": 0}
        # expectations: bets EVER PLACED — the ledger's monotonic counter (a bet IN the ledger).
        try:
            import expectations as _exp
            stats["expectations"] = {"total": _exp.ExpectationLedger(self.config).total_placed()}
        except Exception:  # noqa: BLE001
            stats["expectations"] = {"total": 0}
        # sleeps: COMPLETED sleep cycles ever — GateState's monotonic total (single writer at
        # the sleep boundary; since-level resets on level-up, the total never walks back).
        try:
            import level_gates as _lg
            stats["sleeps"] = {"total": int(_lg.GateState(self.config).sleeps_total)}
        except Exception:  # noqa: BLE001
            stats["sleeps"] = {"total": 0}
        # quests: adjudicated PASSES from the store (live file + rotated archives).
        try:
            import quests as _quests
            store = self.quests.store if self.quests is not None else _quests.QuestStore(self.config)
            stats["quests"] = {"passed": store.passed_count()}
        except Exception:  # noqa: BLE001
            stats["quests"] = {"passed": 0}
        # commission: settled FACTS from the store (a confirm is glue/operator ground truth —
        # the creature's own done-claim moves nothing here). Zeros with no store/flag, so a
        # >=1 criterion can never mistake absence for evidence.
        try:
            from commission import Commission as _Commission
            _tasks = (self.commission or _Commission(self.config)).load()
            stats["commission"] = {
                "confirmed_total": sum(1 for t in _tasks if t.state == "confirmed"),
                "open": sum(1 for t in _tasks if t.state == "open"),
            }
        except Exception:  # noqa: BLE001
            stats["commission"] = {"confirmed_total": 0, "open": 0}
        return stats

    def _quest_reward_sink(self, cfg, quest) -> None:
        """Payout through the standard XP path WITH config threaded (4.3: the gate holds the level;
        persona.award_xp only consults the mastery flag when it can see config).

        With the ladder on, "it pays" pays LIMBS, not just XP (TOOL_PROGRESSION): an
        unlock-kind reward grants its unit through unlocks.grant — the REWARD_UNLOCK seam
        quests.py reserves (U6 workshop = genesis-03's pass reward) — and its riding XP leg
        (quests.reward_xp_amount) still pays through the standard path. Ladder off keeps the
        pre-ladder behavior byte-identical: only plain XP rewards pay, non-XP kinds are recorded
        on the quest and nothing else moves."""
        try:
            reward = quest.reward or {}
            import quests as _quests
            if reward.get("kind") == _quests.REWARD_XP:
                if self._persona is None:
                    return
                import persona as _persona_mod
                _persona_mod.award_xp(self._persona, int(reward.get("amount", 0)),
                                      reason=f"quest:{quest.id}", config=cfg)
                return
            if not getattr(self.config, "pillars_tool_unlocks_enabled", False):
                return                      # dark: non-XP legs are a later phase's job (as before)
            xp_leg = _quests.reward_xp_amount(reward)   # the XP riding alongside an unlock reward
            if xp_leg > 0 and self._persona is not None:
                import persona as _persona_mod
                _persona_mod.award_xp(self._persona, xp_leg,
                                      reason=f"quest:{quest.id}", config=cfg)
            if reward.get("kind") == _quests.REWARD_UNLOCK:
                what = str(reward.get("what") or "")
                if what:
                    import unlocks as _unlocks
                    _unlocks.grant(cfg if cfg is not None else self.config, what,
                                   f"quest_reward:{quest.id}")
        except Exception as e:  # noqa: BLE001
            logger.warning("pillars quest reward failed: %s", e)
        finally:
            # 4.3b: the PASSED quest itself is mastery evidence, whatever its reward kind
            # (class pays 0 XP — the legs above are the payout). Flag-gated inside.
            try:
                import mastery
                mastery.record_evidence(self.config, self._persona, "quest_passed", quest.id,
                                        title=getattr(quest, "directive", "") or quest.id)
            except Exception:  # noqa: BLE001
                pass

    def _cadence_path(self):
        return self.config.state_dir / "quest_cadence.json"

    def sleeps_since_close(self) -> int:
        """The digestion counter issue_next feeds on. A newborn (no file) has digested — 1 — so the
        very first quest can issue instead of being gated forever by a close that never happened."""
        try:
            return int(json.loads(self._cadence_path().read_text(encoding="utf-8"))
                       .get("sleeps_since_close", 1))
        except Exception:  # noqa: BLE001
            return 1

    def _set_sleeps_since_close(self, n: int) -> None:
        try:
            self.config.state_dir.mkdir(parents=True, exist_ok=True)
            tmp = self._cadence_path().with_suffix(".tmp")
            tmp.write_text(json.dumps({"sleeps_since_close": int(n)}), encoding="utf-8")
            tmp.replace(self._cadence_path())
        except Exception as e:  # noqa: BLE001
            logger.warning("pillars cadence persist failed: %s", e)

    def _commission_settle(self, s, persona, *, tick: int = 0) -> None:
        """One commission settlement lands as lived experience: the System's window states what
        settled (on-screen, always), a CONFIRMED task pays XP through the gate-held award path and
        feeds the metabolism reserve (work earns food), and the news queue carries it to Charlie's
        next check-in. A REJECTED task pays nothing — its note already rode the task back to open."""
        t = s.task
        if s.outcome == "confirmed":
            text = f"[SYSTEM] COMMISSION TASK #{t.id} CONFIRMED — {s.note}. PAID {s.xp} XP."
        elif s.outcome == "rejected":
            text = f"[SYSTEM] COMMISSION TASK #{t.id} RETURNED — {s.note}."
        else:
            text = f"[SYSTEM] COMMISSION TASK #{t.id} WITHDRAWN — {s.note}."
        try:
            append_observation(self.config, {"tick": tick, "tool": "system_window",
                                             "success": s.outcome == "confirmed",
                                             "output": text})
        except Exception as e:  # noqa: BLE001
            logger.warning("commission settlement notice failed: %s", e)
        if s.xp > 0:
            try:
                import persona as _persona
                _persona.award_xp(persona, int(s.xp), f"commission:{t.id}", config=self.config)
                _persona.save_persona(self.config.workspace, persona)
            except Exception as e:  # noqa: BLE001
                logger.warning("commission XP award failed: %s", e)
        if s.outcome == "confirmed":
            # 4.3b: an operator-CONFIRMED commission task is mastery evidence (class pays 0 XP —
            # the settlement above is the payout). Flag-gated inside; best-effort.
            try:
                import mastery
                mastery.record_evidence(self.config, persona, "commission_confirmed",
                                        f"commission-{t.id}",
                                        title=getattr(t, "title", "") or f"task {t.id}", tick=tick)
            except Exception:  # noqa: BLE001
                pass
        if s.feed > 0 and self.metabolism is not None:
            try:
                self.metabolism.feed(float(s.feed))
            except Exception as e:  # noqa: BLE001
                logger.warning("commission feed failed: %s", e)
        if self.news is not None:
            try:
                self.news.ingest({"summary": f"commission task #{t.id} "
                                             f"{s.outcome}: {t.title}",
                                  "surprise": 1.0}, "commission")
            except Exception:  # noqa: BLE001
                pass

    def _distill_strategy(self, closure: dict, *, tick: int = 0) -> None:
        """SOTA#3 strategy memory: distil a CLOSED quest/objective into a compact trigger→principle
        GUARDRAIL engram, so the recall cascade surfaces it the next time the creature is in a
        matching situation — each success becomes 'reuse this', each derailment becomes 'avoid this'.
        Event-driven at close (ARCHITECTURE_PRINCIPLES #1). No-op unless the flag is on AND the memory
        manager is live (a guardrail that can't be recalled isn't worth minting). Fail-open: a distiller
        fault must never block close bookkeeping (I5). Reused by both the quest and objective close paths."""
        if not getattr(self.config, "pillars_strategy_memory_enabled", False) or self.manager is None:
            return
        try:
            import strategy as _strategy
            body = _strategy.distill_strategy(closure, llm=self._live_llm())
            if not body:
                return
            self.manager.encode("strategy", body, tick=tick, provenance="experienced",
                                strength=_strategy.strength_for(closure),
                                stats={"situation": str(closure.get("situation") or ""),
                                       "strategy": True})
        except Exception as e:  # noqa: BLE001
            logger.warning("pillars strategy distill failed: %s", e)

    def _on_quest_closed(self, r: dict, persona, *, tick: int = 0) -> None:
        """A quest closed (passed or expired): reset the digestion counter, write the settlement
        into the observation stream (the System pays ON-SCREEN — a lived turn, not bookkeeping),
        record the failure-lite episode, ingest the news event, restore a remedial'd tier, wake the
        trainer, and make the event-driven issue attempt (cadence still demands a sleep first)."""
        q = r.get("quest")
        self._set_sleeps_since_close(0)
        # 4.3 — tier standing moves on ADJUDICATED quest outcomes, the unit of attempted mastery
        # (a pass resets the failure streak; an expiry counts against the tier). Remedials are
        # excluded: an already-suspended tier's remedial expiring must not double-punish, and its
        # pass restores the tier below via record_remedial_completion.
        if (q is not None and getattr(self.config, "pillars_mastery_gates_enabled", False)
                and not str(getattr(q, "id", "")).startswith("remedial-tier")):
            try:
                import level_gates as _lg
                remedial = _lg.record_tier_outcome(
                    self.config, int(getattr(q, "tier", 1) or 1), bool(r.get("passed")))
                if remedial:
                    self._event("suspension", persona)
            except Exception as e:  # noqa: BLE001
                logger.warning("pillars tier outcome failed: %s", e)
        # Settlement notice: ONE system_window observation in the System's own register. The paid
        # amount is read from the quest's reward — exactly what the sink pays through award_xp
        # (glue settled it; this only reports the settlement). Non-XP rewards are the level-gate
        # phase's job, so the notice states the reward without claiming a payout.
        if q is not None:
            try:
                import quests as _quests
                if r.get("passed"):
                    reward = getattr(q, "reward", None) or {}
                    paid = (f"PAID {int(reward.get('amount', 0))} XP"
                            if reward.get("kind") == _quests.REWARD_XP
                            else f"REWARD {_quests._reward_str(reward)}")
                    text = f"[SYSTEM] QUEST PASSED — {paid}."
                elif r.get("abandoned"):
                    text = "[SYSTEM] QUEST FAILED — STALLED TOO LONG, RELEASED. NOTHING PAID."
                else:
                    text = "[SYSTEM] QUEST EXPIRED — NOTHING PAID."
                append_observation(self.config, {"tick": tick, "tool": "system_window",
                                                 "success": bool(r.get("passed")),
                                                 "output": text})
            except Exception as e:  # noqa: BLE001
                logger.warning("pillars quest settlement notice failed: %s", e)
        ep = r.get("episode")
        if ep:
            try:
                if self.manager is not None:
                    # Engram path: the failure-lite quest episode lands in the same store every
                    # other memory does — one economy, one lifecycle (no legacy double-write).
                    outcome = "succeeded" if ep.get("success") else (
                        f"failed ({ep.get('fail_kind')})" if ep.get("fail_kind") else "failed")
                    body = f"quest `{ep.get('tool', 'quest')}` {outcome}. {ep.get('summary', '')}".strip()
                    self.manager.encode("episode", body, tick=tick,
                                        stats={"situation": str(ep.get("key") or "")})
                else:
                    import episodes as _ep
                    _ep.record_episode(self.config, tick=tick, tool=str(ep.get("tool", "quest")),
                                       sig=str(ep.get("sig", "")), fail_kind=str(ep.get("fail_kind", "")),
                                       success=bool(ep.get("success", False)),
                                       summary=str(ep.get("summary", "")), key=ep.get("key"))
            except Exception as e:  # noqa: BLE001
                logger.warning("pillars quest episode record failed: %s", e)
        # SOTA#3: distil this closed quest into a guardrail the recall cascade will surface next time
        # (after the episode encode so the situation key is in hand). Own flag; fail-open inside.
        if q is not None:
            self._distill_strategy({
                "title": str(getattr(q, "directive", "") or getattr(q, "id", "") or "a quest"),
                "outcome": ("passed" if r.get("passed")
                            else ("abandoned" if r.get("abandoned") else "expired")),
                "reason": str((ep or {}).get("summary", "")),
                "success": bool(r.get("passed")),
                "situation": str((ep or {}).get("key") or ""),
                "trajectory": str((ep or {}).get("summary", "")),
            }, tick=tick)
        if self.news is not None and q is not None:
            try:
                self.news.ingest(q, "quest")
            except Exception:  # noqa: BLE001
                pass
        if (r.get("passed") and q is not None
                and getattr(self.config, "pillars_mastery_gates_enabled", False)
                and str(getattr(q, "id", "")).startswith("remedial-tier")):
            try:
                import level_gates as _lg
                tier = int(str(q.id).split("-")[1].replace("tier", "") or 0)
                _lg.record_remedial_completion(self.config, tier)
            except Exception as e:  # noqa: BLE001
                logger.warning("pillars remedial restore failed: %s", e)
        self._event("quest_closed", persona)
        self._issue_next(persona, tick=tick)

    def _issue_next(self, persona, *, tick: int = 0) -> None:
        """Event-driven issue attempt (after sleep completion / quest closure — never a timer).
        An ACTUAL issuance also lands in the observation stream as ONE system_window turn — the
        System speaks when it issues, not just as standing furniture in the focus block."""
        if self.quests is None:
            return
        try:
            import glue as _glue
            cond = _glue.compute_condition(_glue.recent_outcomes(self.config))
            issued = self.quests.issue_next(sleeps_since_close=self.sleeps_since_close(),
                                            condition=cond)
        except Exception as e:  # noqa: BLE001
            logger.warning("pillars issue_next failed: %s", e)
            return
        if issued is None:
            return    # silence — the correct default (§7)
        # TOOL_PROGRESSION issuance-grant (U2/U3/U5): the unit this quest carries starts existing
        # BEFORE the window that names it is written — the System's window IS the moment the tool
        # exists; a window naming a not-yet-real name would be a felt lie (§0). The grant's own
        # felt moment ([SYSTEM] GRANTED: …) renders first, then the quest window that trains it.
        if getattr(self.config, "pillars_tool_unlocks_enabled", False):
            unit_id = str(getattr(issued, "grants_unit", "") or "")
            if unit_id:
                try:
                    import unlocks as _unlocks
                    _unlocks.grant(self.config, unit_id, f"quest_issue:{issued.id}")
                except Exception as e:  # noqa: BLE001
                    logger.warning("pillars issuance grant failed: %s", e)
            self._announce_unlocks(tick)
        if getattr(issued, "hidden", False):
            return    # hidden — achievements announce only on completion (§7)
        try:
            import quests as _quests
            text = (f"[SYSTEM] QUEST ISSUED [T{issued.tier}] — REWARD "
                    f"{_quests._reward_str(issued.reward)}.\n{issued.directive}")
            append_observation(self.config, {"tick": tick, "tool": "system_window",
                                             "success": True, "output": text})
        except Exception as e:  # noqa: BLE001
            logger.warning("pillars quest issuance notice failed: %s", e)

    # --- TOOL_PROGRESSION: the growing body (milestones, the felt moment, stage expression) ------
    def _unlock_evidence(self, persona) -> dict:
        """The migration evidence dict (unlocks.EVIDENCE_KEYS): every count an ADJUDICATED record —
        the same stores _quest_stats reads, plus the lived tools_used facts for organ use that
        predates the ladder. Fail-open per key: missing store = no evidence, never a crash."""
        import unlocks as _unlocks
        ev: dict = {}
        stats = {}
        try:
            stats = self._quest_stats(persona)
        except Exception:  # noqa: BLE001
            pass
        ev["sleeps"] = int((stats.get("sleeps") or {}).get("total", 0) or 0)
        ev["live_skills"] = int((stats.get("skills") or {}).get("live_count", 0) or 0)
        ev["predictions"] = int((stats.get("expectations") or {}).get("total", 0) or 0)
        used = (persona or {}).get("tools_used", {}) or {}
        ev["spoke_or_saw"] = int(used.get("speak", 0) or 0) + int(used.get("vision", 0) or 0) \
            + int(used.get("see", 0) or 0)
        ev["objectives"] = int(used.get("objective_add", 0) or 0)
        ev["delegate_jobs"] = int(used.get("delegate", 0) or 0)
        # The two NEW milestone units (reach, self-authorship) were unit-less until now, so their
        # tools have NO tools_used history to migrate from. Their evidence is the SAME adjudicated
        # maturity their live criterion reads — quests passed + sleeps — so a creature that has
        # already earned the depth inherits the organ on migration instead of re-walking to it.
        q_passed = int((stats.get("quests") or {}).get("passed", 0) or 0)
        sleeps_total = int((stats.get("sleeps") or {}).get("total", 0) or 0)
        ev["reach_earned"] = int(q_passed >= _unlocks.REACH_QUESTS_REQUIRED
                                 and sleeps_total >= _unlocks.REACH_SLEEPS_REQUIRED)
        ev["selfauthor_earned"] = int(q_passed >= _unlocks.SELFAUTHOR_QUESTS_REQUIRED
                                      and sleeps_total >= _unlocks.SELFAUTHOR_SLEEPS_REQUIRED)
        try:
            ev["commission_tasks"] = (len(self.commission.load())
                                      if self.commission is not None else 0)
        except Exception:  # noqa: BLE001
            ev["commission_tasks"] = 0
        return ev

    def _unlock_probe_fn(self):
        """The I8 organ-reachability probe handed to unlocks.adjudicate: the injected test seam
        (self.unlock_probe) when present, else the process-memoized voice probe — bounded ~1s,
        cached ~60s, so adjudication never hammers a dead port (voice is down on Sprinter today)."""
        if self.unlock_probe is not None:
            return self.unlock_probe
        cfg = self.config
        return lambda service: _probe_service(cfg, service)

    def _unlock_milestones(self, persona, *, tick: int) -> None:
        """Milestone adjudication (§0.5: glue judges over the SAME typed stats dict quest criteria
        read — never the model's word), then render any waiting felt moments. A service-gated unit
        whose organ doesn't answer the probe holds PENDING and is retried here every call (I8)."""
        if not getattr(self.config, "pillars_tool_unlocks_enabled", False):
            return
        try:
            import unlocks as _unlocks
            # Load-or-birth migration (TOOL_PROGRESSION): a creature that LIVED before the ladder
            # (ticks on the clock, no books) keeps every organ its record evidences — flipping
            # the flag must never amputate a lived body. Fresh books on a fresh creature stay
            # newborn. Runs once per process; seeding is silent (no felt moment for history).
            if not self._unlock_books_checked:
                self._unlock_books_checked = True
                books = self.config.state_dir / _unlocks.STATE_NAME
                if not books.exists() and int((persona or {}).get("total_ticks", 0) or 0) > 0:
                    _unlocks.seed_from_evidence(self.config, self._unlock_evidence(persona))
            # Self-heal the issuance-grant crash window: a quest persisted ACTIVE before its
            # grant committed would otherwise stand as a window naming a tool that doesn't
            # exist — permanently (the issue seam never re-fires for an already-active quest).
            # grant() is idempotent, so reconciling the active quest's unit here on every call
            # makes that wedge impossible to keep.
            if self.quests is not None:
                active = self.quests.store.active()
                unit_id = str(getattr(active, "grants_unit", "") or "") if active else ""
                if unit_id:
                    _unlocks.grant(self.config, unit_id, f"quest_issue:{active.id}")
            _unlocks.adjudicate(self.config, self._quest_stats(persona),
                                probe=self._unlock_probe_fn())
        except Exception as e:  # noqa: BLE001
            logger.warning("pillars unlock adjudication failed: %s", e)
        self._announce_unlocks(tick)

    def _announce_unlocks(self, tick: int) -> None:
        """Drain the one-shot announcement queue into the observation stream — the felt moment.
        Register "system" → ONE system_window observation (VERBATIM in the history thread, the
        same mechanism as quest issuance/settlement: the System pays capability on-screen).
        Register "body" → the plain-notice kind ("dream"), which the history thread renders as a
        bracketed body-fact user turn exactly like the sleep notice — a maturation, felt, never a
        payment. The unit's bracketed text is unwrapped so it reads as the notice's own clause.
        Render-then-mark (peek/mark two-phase): a crash between the write and the flag merely
        re-announces once — a rare duplicate beats a moment eaten forever."""
        if not getattr(self.config, "pillars_tool_unlocks_enabled", False):
            return
        try:
            import unlocks as _unlocks
            for note in _unlocks.peek_unannounced(self.config):
                text = str(note.get("text") or "")
                if not text:
                    _unlocks.mark_announced(self.config, note.get("unit") or "")
                    continue
                if note.get("register") == _unlocks.REGISTER_SYSTEM:
                    append_observation(self.config, {"tick": tick, "tool": "system_window",
                                                     "success": True, "output": text})
                else:
                    append_observation(self.config, {"tick": tick, "tool": "dream",
                                                     "success": True,
                                                     "output": text.strip("[]")})
                _unlocks.mark_announced(self.config, note.get("unit") or "")
        except Exception as e:  # noqa: BLE001
            logger.warning("pillars unlock announcement failed: %s", e)

    def _stage_transition(self, persona) -> None:
        """CREATURE_GENETICS phase D at the loop seam: derive the life stage EXACTLY the way the
        nap curve does (creature_gen.stage_for over the live persona level + creature.json's
        hatched flag — read-only, no new stage system), and on a crossing — once — adjudicate the
        dormant alleles over the typed stats dict (metamorphosis reads the environment), persist
        the genome, and rewrite workspace/phenotype.json. The single writer stays this loop; a
        creature with no genome on record is skipped (this seam never births one)."""
        import creature_gen
        level = int((persona or {}).get("level", 1) or 1)
        try:
            cj = json.loads((self.config.workspace / "creature.json").read_text(encoding="utf-8"))
            hatched = bool(cj.get("hatched", False))
        except Exception:  # noqa: BLE001 - no creature.json yet: infer from level (neuromod's
            hatched = level > 1              # rule — egg and hatchling behave the same here)
        stage = creature_gen.stage_for(level, hatched)
        if stage == self._stage_seen:
            return                           # cheap per-tick path: the stage hasn't moved
        import genome as _genome
        g = _genome.Genome.load(self.config)
        if g is None:
            return                           # no genome on record — nothing to express, retry later
        last = g.stage_history[-1].get("stage") if g.stage_history else None
        if last != stage:
            _genome.express_alleles(g, self._quest_stats(persona), stage)
            g.save()                         # the caller persists at the seam (express_alleles' contract)
            import phenotype as _phenotype
            _phenotype.write_phenotype(self.config, g, stage)
        self._stage_seen = stage

    # --- the sleep window (2.4 cutover entrypoint + the sleep-completion events) ------------------
    def sleep_window(self, *, tick: int, persona, observations) -> object:
        """At the loop's sleep window: expire an ignored quest (5.1), run the real sleep engine
        (2.4 — run_sleep clears adenosine itself: the creature wakes rested), then on a COMPLETED
        sleep advance the digestion counters (4.3 record_sleep_cycle + 5.1 sleeps_since_close),
        make the event-driven issue attempt, and wake the trainer (5.2). Guarded throughout."""
        c = self.config
        if not getattr(c, "pillars_sleep_engine_enabled", False):
            return None
        # DREAM vs NAP — the body decides, never a clock. This window is a NAP only when real
        # wake pressure had accumulated by the boundary (NAP_PRESSURE_MIN of the stage ceiling);
        # otherwise it is a DREAM: a context-compaction doze. Both run the memory jobs and keep
        # the System responsive (quest settlement below, cadence + issuance later). Only a NAP
        # rests the body (adenosine clear), advances the gates' sleep counters and the ladder's
        # sleeps.total, and wakes the Administrator — an infant that dozes every few minutes
        # must still get genuinely tired, genuinely nap, and genuinely digest between levels.
        # No adenosine organ → NAP (fail-open to the pre-split semantics). Computed FIRST because the
        # quest stall-clock below must advance on NAPS only, never on frequent context-compaction
        # dreams — else a hatchling (dreams every few min, naps every ~2.5 h) burns a quest's whole
        # QUEST_STALL_SLEEPS budget in minutes and FAILS it before its first nap, suspending the tier
        # and welding the level gate shut (the 2026-07-14 stuck-at-Lv.1 stall).
        nap = True
        try:
            from nervous.neuromod import NAP_PRESSURE_MIN as _nap_min
            _aden = getattr(self.neuromod, "adenosine", None)
            if _aden is not None:
                nap = float(_aden.pressure()) >= _nap_min
        except Exception:  # noqa: BLE001 - classification failure must never skip a sleep
            nap = True
        if self.quests is not None:
            try:
                active = self.quests.store.active()
                if active is not None:
                    # Adjudicate BEFORE the expiry path: expire_if_due passes an already-met
                    # quest internally and returns None, which used to settle it SILENTLY —
                    # no [SYSTEM] QUEST PASSED window, no digestion reset, an invisible payout.
                    # Routing the pass through the same closure path as after_outcome keeps
                    # every settlement on-screen (the System pays on-screen, always).
                    r = self.quests.check(active, self._quest_stats(persona))
                    if r.get("passed") or r.get("expired"):
                        self._on_quest_closed(r, persona, tick=tick)
                    else:
                        ep = self.quests.expire_if_due(self._quest_stats(persona))
                        if ep is not None:
                            self._on_quest_closed({"expired": True, "episode": ep,
                                                   "quest": active}, persona, tick=tick)
                        elif nap:
                            # K-sleep abandon (TOOL_PROGRESSION stall handling): a quest whose
                            # criteria haven't moved across QUEST_STALL_SLEEPS *naps* closes FAILED,
                            # unfreezing the quest line for a smaller re-attack. NAPS only — a dream
                            # is not a chance the quest was given, so it must not burn the stall clock.
                            ep = self.quests.abandon_if_stalled(self._quest_stats(persona))
                            if ep is not None:
                                self._on_quest_closed({"abandoned": True, "episode": ep,
                                                       "quest": active}, persona, tick=tick)
            except Exception as e:  # noqa: BLE001
                logger.warning("pillars quest expiry failed: %s", e)
        report = None
        try:
            from nervous.sleep import SleepContext, run_sleep
            cons = self.manager.consolidator if self.manager is not None else self._consolidator()
            ctx = SleepContext(config=c, consolidator=cons, neuromod=self.neuromod,
                               organ_registry=self.organ_registry, llm=self._live_llm(),
                               observations=list(observations or []),
                               temperament=self.temperament)
            report = run_sleep(ctx, clear_adenosine=nap)   # a dream does NOT rest the body (2.4)
            self._aden_mark = time.monotonic()   # wake-time accounting restarts at the boundary
        except Exception as e:  # noqa: BLE001
            logger.warning("pillars sleep engine failed: %s", e)
            return None
        if report is None or not getattr(report, "results", None):
            return report
        if nap:
            try:
                import level_gates as _lg
                _lg.record_sleep_cycle(c)        # flag-gated internally (4.3); NAPS only
            except Exception as e:  # noqa: BLE001
                logger.warning("pillars record_sleep_cycle failed: %s", e)
            try:
                # Goal-backlog consolidation is the objective analog of memory consolidation:
                # a nap merges near-duplicate goals and archives long-stale ones (the same
                # similarity economy skills/knowledge use), so the working set stays small.
                import objectives as _obj
                _rep = _obj.consolidate(c, tick=tick)
                if _rep.get("merged") or _rep.get("archived"):
                    logger.info("nap: goal consolidation merged %d, archived %d",
                                len(_rep["merged"]), len(_rep["archived"]))
            except Exception as e:  # noqa: BLE001
                logger.warning("pillars goal consolidation failed: %s", e)
        if self.quests is not None:
            try:
                self._set_sleeps_since_close(self.sleeps_since_close() + 1)
            except Exception as e:  # noqa: BLE001
                logger.warning("pillars cadence advance failed: %s", e)
            self._issue_next(persona, tick=tick)
        # TOOL_PROGRESSION milestones at the boundary (same guarded pattern as the organ hooks):
        # on a nap sleeps.total just advanced, so U1 lands on the first wake; on a dream this
        # still retries the senses service hold — the probe answers whenever the organ comes up.
        if getattr(c, "pillars_tool_unlocks_enabled", False):
            try:
                self._unlock_milestones(persona, tick=tick)
            except Exception as e:  # noqa: BLE001
                logger.warning("pillars unlock milestones failed: %s", e)
        if nap:
            self._event("sleep_complete", persona)
        return report

    def _consolidator(self):
        """The single long-term writer for the sleep jobs when the manager is off but the engram
        economy is on. None otherwise — the jobs no-op cleanly without one."""
        if not getattr(self.config, "pillars_memory_engram_enabled", False):
            return None
        try:
            from engram import Consolidator
            return Consolidator(self.config)
        except Exception as e:  # noqa: BLE001
            logger.warning("pillars consolidator init failed: %s", e)
            return None

    # --- presence (4.4: the listening hold IS the presence signal) --------------------------------
    def on_presence(self) -> list:
        """Dean is present (chat focus / listening hold): surface the ranked news digest and
        snapshot it to state so the dashboard can fetch it (glue.surfaced_news — the accessor)."""
        if self.news is None:
            return []
        try:
            items = self.news.surface(True)
            snap = [it.to_dict() for it in items]
            self.config.state_dir.mkdir(parents=True, exist_ok=True)
            p = self.config.state_dir / "news_surfaced.json"
            tmp = p.with_suffix(".tmp")
            tmp.write_text(json.dumps(snap, ensure_ascii=False), encoding="utf-8")
            tmp.replace(p)
            return items
        except Exception as e:  # noqa: BLE001
            logger.warning("pillars news surface failed: %s", e)
            return []

    # --- the Administrator (5.2): event-driven check-ins only (ARCH #1 — never a timer) -----------
    def _event(self, kind: str, persona) -> None:
        if not getattr(self.config, "pillars_administrator_enabled", False):
            return
        try:
            import administrator as _adm
            if not _adm.should_check_in(self.config, kind):
                return
            llm = self._live_llm()
            if llm is None:
                return        # no mind available (mock/test env) — the trainer stays quiet
            _adm.check_in(self.config, llm, kind, persona=persona)
        except Exception as e:  # noqa: BLE001
            logger.warning("pillars administrator check-in failed: %s", e)

    def _operator_directive(self, persona, tick_number: int) -> None:
        """OPERATOR_DIRECTIVES: when Charlie has a message pending THIS tick, the System (same gemma,
        System role) classifies it and — if it's a request — adopts it as a priority origin:"operator"
        objective BEFORE context assembly, so the creature both replies promptly AND sees the new
        focus this same tick (instead of the message being consumed after one reply and forgotten).
        Peeks non-consumingly; the normal reply-banner path still fires. Fail-open."""
        if not getattr(self.config, "operator_directives_enabled", False):
            return
        if not getattr(self.config, "pillars_administrator_enabled", False):
            return
        try:
            from memory import peek_interventions
            pending = peek_interventions(self.config)
            if not pending:
                return
            llm = self._live_llm()
            if llm is None:
                return
            import administrator as _adm
            for iv in pending:
                msg = (iv.get("content") or "").strip()
                if not msg:
                    continue
                directive = _adm.classify_operator_message(self.config, llm, msg, persona=persona)
                if directive:
                    obj = _adm.apply_operator_directive(self.config, directive,
                                                        tick=tick_number, source_key=iv.get("filename", ""))
                    if obj:
                        logger.info("operator directive adopted as focus: %s", obj.get("title"))
        except Exception as e:  # noqa: BLE001 - the trainer failing never wounds the tick
            logger.warning("operator directive pass failed: %s", e)

    def _deliver_due_reminders(self, tick_number: int) -> None:
        """OPERATOR_DIRECTIVES: fire any due reminders into the stream as high salience, so the
        creature sees '⏰ REMINDER: …' this tick (the persistent, nap/restart-surviving replacement
        for a fragile bg_run sleep). A reminder tied to an operator directive re-raises it to focus.
        Event-driven in spirit: the fire-time is the event; we check due-ness at this one gate."""
        if not getattr(self.config, "reminders_enabled", False):
            return
        try:
            import reminders as _rem
            fired = _rem.due(self.config, time.time())
            for r in fired:
                note = (r.get("note") or "").strip()
                append_observation(self.config, {"tick": tick_number, "tool": "system_window",
                                                 "success": True,
                                                 "output": f"⏰ REMINDER: {note}"})
                src = (r.get("source_key") or "").strip()
                if r.get("origin") == "operator" and src:
                    try:
                        import objectives as _obj
                        _obj.activate(self.config, src, tick=tick_number) or _obj.activate(self.config, note, tick=tick_number)
                    except Exception:  # noqa: BLE001
                        pass
        except Exception as e:  # noqa: BLE001 - a reminder fault never wounds the tick
            logger.warning("reminder delivery failed: %s", e)


def _reflex_stats(config, persona) -> dict:
    """The typed stats dict a reflex GUARD is checked against (WIS1 — the SAME Criterion vocabulary
    quest criteria use). A lean, self-contained build so the reflex path never depends on the
    pillars hub existing: persona counters plus the same glue-settled facts _quest_stats reads
    (skills manifest, quest passes, commission confirms). Fail-open per source — an absent stat is
    zero, which no >=1 criterion can mistake for evidence."""
    stats: dict = {"persona": dict(persona or {})}
    try:
        import skills as _skills
        ents = (_skills._load_manifest(config).get("skills") or {}).values()
        live = [e for e in ents if e.get("status") in _skills._LIVE_STATUSES]
        stats["skills"] = {"live_count": len(live),
                           "trusted_count": sum(1 for e in live if e.get("status") == "trusted")}
    except Exception:  # noqa: BLE001
        stats["skills"] = {"live_count": 0, "trusted_count": 0}
    try:
        import quests as _quests
        stats["quests"] = {"passed": _quests.QuestStore(config).passed_count()}
    except Exception:  # noqa: BLE001
        stats["quests"] = {"passed": 0}
    try:
        from commission import Commission as _Commission
        _tasks = _Commission(config).load()
        stats["commission"] = {"confirmed_total": sum(1 for t in _tasks if t.state == "confirmed"),
                               "open": sum(1 for t in _tasks if t.state == "open")}
    except Exception:  # noqa: BLE001
        stats["commission"] = {"confirmed_total": 0, "open": 0}
    return stats


def _maybe_fire_reflex(config, persona, tick_number: int) -> bool:
    """The reflex execution hook (WISDOM_PLAN §1). Returns True IFF a reflex fired AND
    wisdom_reflex_saves_tick is on (the caller then ends the tick — the LLM is skipped). Returns
    False in every other case (flag off, no match, soak mode, or any error) — the normal tick then
    runs. Fully inert unless wisdom_reflexes_enabled (WIS7); wrapped so a reflex fault NEVER breaks
    the tick (I5)."""
    if not getattr(config, "wisdom_reflexes_enabled", False):
        return False
    try:
        import reflexes as _rfx
        import episodes as _ep
        situation = _ep.situation_key(config)
        if not situation:
            _rfx.record_tick_outcome(config, handled_by_reflex=False)
            return False
        stats = _reflex_stats(config, persona)
        reflex = _rfx.match(config, situation, stats)
        if reflex is None:
            # No armed reflex answers this situation — the model handles the tick (not below-model).
            _rfx.reset_consecutive(config)
            _rfx.record_tick_outcome(config, handled_by_reflex=False)
            return False

        rid = reflex.get("id", "")
        # Rabbit-hole bound (mirrors the loop detector's spirit): a reflex that has fired this many
        # times in a row without the situation changing is disarmed — crystallized wisdom must not
        # loop any more than the model may. reset_consecutive below zeroes every OTHER reflex, so
        # this counter tracks an uninterrupted run of THIS reflex only.
        loop_bound = int(getattr(config, "wisdom_reflex_loop_bound", 3) or 3)
        if _rfx.consecutive_fires(config, rid) >= loop_bound:
            _rfx.demote(config, rid, tick=tick_number, reason="loop_bound")
            append_observation(config, {
                "tick": tick_number, "tool": "reflex", "success": False, "fail_kind": "blocked",
                "output": (f"[REFLEX] {rid} disarmed: fired {loop_bound}x in the same situation "
                           f"without change (rabbit-hole bound) — situation unresolved, "
                           f"handing back to the mind."),
            })
            _rfx.reset_consecutive(config)
            _rfx.record_tick_outcome(config, handled_by_reflex=False)
            return False

        _rfx.reset_consecutive(config, except_id=rid)   # only THIS reflex's run may accumulate

        action = reflex.get("action") or {}
        tool = action.get("tool") or ""
        args = action.get("args") or {}
        expected_sig = str(action.get("sig") or tool)
        from parser import ToolCall
        result = execute_tool(ToolCall(tool=tool, args=args, raw=""), config)

        # WIS2: record the AUTOMATED outcome to the SAME episodic ledger the model's ticks feed —
        # but marked `automated: true`, and WITHOUT any economy feed. This branch simply does not
        # call record_tick (XP/streaks), pillars.after_outcome (bets/mastery/learning/quests/tier/
        # level), or the reward learner — they only run on the model's own tick path below. The
        # episode's automated flag makes the promotion scanner EXCLUDE it, so a reflex can never
        # count its own firings toward its own re-promotion.
        summary = _ep.clean_fragment(result.output or "", _ep.SUMMARY_CHARS)
        _rfx.record_automated_episode(config, tick=tick_number, situation_key=situation,
                                      tool=tool, sig=expected_sig, fail_kind=result.fail_kind,
                                      success=result.success, summary=summary)

        _rfx.record_fire(config, rid, success=result.success, tick=tick_number)

        # WIS3: render the firing AS a reflex — visible to creature and operator.
        status = "handled" if result.success else "FAILED"
        append_observation(config, {
            "tick": tick_number, "tool": "reflex", "success": result.success,
            "fail_kind": result.fail_kind,
            "output": (f"[REFLEX] {status} `{situation}` via `{tool}` "
                       f"(automated, no economy){'' if result.success else ' — demoting'}\n"
                       f"{summary}"),
        })

        # WIS3: a reflex whose action FAILS adjudication demotes immediately — disarmed + scarred
        # as an error engram. Crystallized wisdom that stops working stops firing.
        if not result.success:
            _rfx.demote(config, rid, tick=tick_number, reason=result.fail_kind or "failure")
            try:
                import knowledge as _kn
                _kn.store_entry(config,
                                content=(f"Reflex {rid} stopped working in situation '{situation}': "
                                         f"`{tool}` failed ({result.fail_kind or 'failure'}). "
                                         f"Disarmed; re-earns arming only after fresh successes."),
                                tags=["reflex", "demoted"], category="errors",
                                confidence="verified", source_tick=tick_number)
            except Exception:  # noqa: BLE001 - the scar is best-effort; the demotion stands
                pass

        # This tick WAS handled below the model — count it toward the below-model fraction (§1).
        _rfx.record_tick_outcome(config, handled_by_reflex=True)

        # reflex_saves_tick decides whether the LLM is skipped. In soak mode (off) the reflex result
        # is now in-stream and the normal tick proceeds (returns False → caller runs the model).
        return bool(getattr(config, "wisdom_reflex_saves_tick", False))
    except Exception as e:  # noqa: BLE001 - a reflex fault must never break the tick (I5)
        logger.warning("reflex hook failed: %s", e)
        return False


def run_loop(config: Config, persona=None, wal=None):
    """Main tick loop with compaction."""
    global _shutdown_requested

    # Restore state from WAL or start fresh
    wal = wal or {}
    tick_number = wal.get("tick_number", 1)
    ticks_since_compaction = wal.get("ticks_since_compaction", 0)
    goal_start_time = wal.get("goal_start_time", time.time())
    consecutive_failures = wal.get("consecutive_failures", 0)
    reasoning_exhaustions = wal.get("reasoning_exhaustions", 0)
    current_max_tokens = wal.get("current_max_tokens", 0) or config.llm_max_tokens
    recent_hashes: collections.deque = collections.deque(maxlen=config.loop_detect_window)
    goal_horizon = 0   # SOTA#9 autonomy KPI: consecutive on-track acting ticks since the last derail
    last_tick_failed = False
    idle_since = None  # timestamp when goal went missing
    operator_paused = False
    listening_since = None  # set while the chat-focus "listening" hold is engaged
    loop_start = time.monotonic()
    last_goal_hash = None  # track goal changes
    # Goal-tension: ticks since REAL progress (a novel fact learned, a new skill, a Boss exchange).
    # Near-dup dedup means the knowledge count only rises on genuinely new facts → a clean signal.
    last_progress_tick = wal.get("last_progress_tick", tick_number)
    try:
        import knowledge as _kn
        prev_knowledge_count = _kn.count_entries(config)
    except Exception:  # noqa: BLE001
        prev_knowledge_count = 0
    prev_skill_count = _count_skills(config)
    prev_artifact_count = _count_artifacts(config)   # workspace files authored — widens the progress signal
    # Objective backlog (Ventral Striatum / Action Gate): seed the open commitments once, then the
    # gate rotates focus among them each tick so a stalled task never starves the rest of the system.
    try:
        import objectives as _obj
        _obj.ensure_seeded(config, tick_number)
    except Exception as _e:  # noqa: BLE001
        logger.warning("objective seed failed: %s", _e)

    # Nest signpost: a read-only START_HERE.md that points the creature's ls/read instinct at the
    # doc TOOLS (manual/check_system/recall) instead of hunting for files that live outside its world.
    try:
        import tools as _tools
        _tools.ensure_nest_signpost(config)
    except Exception as _e:  # noqa: BLE001
        logger.warning("nest signpost seed failed: %s", _e)

    pfx = _pfx(persona, config)
    print(f"{pfx} Starting tick loop (interval={config.tick_interval_s}s, mock={config.mock_mode})")

    # Self-edit health-probe breadcrumb: if this boot follows an operator-applied self-edit, drop
    # an applied_ok marker NOW — reaching run_loop proves the new code imported and started. A
    # paused eidos never writes a post-tick heartbeat, so the watchdog keys its probe on this.
    try:
        import selfedit as _se
        _se.write_applied_ok(config)
    except Exception as _se_e:  # noqa: BLE001 - breadcrumb is best-effort, never blocks boot
        logger.warning("applied_ok breadcrumb failed: %s", _se_e)

    # --- Semantic recall substrate (phase 7a): load the embedding model ONCE here, before the loop,
    #     and bring the knowledge vectors into sync. One load serves both recall surfaces (knowledge
    #     hybrid + episode situation similarity). CPU-only (no VRAM contention with house-ai); cohost
    #     keeps it resident. Fail-open: if anything here trips, recall degrades to lexical-only. ---
    if config.knowledge_embedding_enabled and not config.mock_mode:
        try:
            import embedding as _emb
            # HTTP backend (Sprinter's resident :8082) needs no local model load; the ONNX backend
            # loads a model into this process. Either path then syncs the knowledge vectors.
            _ready = bool(getattr(config, "embedding_endpoint", "")) or (
                _emb.model_available(config) and _emb.load_model(config))
            if _ready:
                _nsync = _emb.sync_knowledge_vectors(config)
                print(f"{pfx} Semantic recall online; synced {_nsync} knowledge vector(s)")
            else:
                logger.info("embedding enabled but no backend available — recall stays lexical-only")
        except Exception as _emb_e:  # noqa: BLE001 - semantic recall is additive, never blocks boot
            logger.warning("embedding init failed: %s", _emb_e)

    # --- Wait for LLM health before entering tick loop (cold-boot safety) ---
    # Skipped in the isolated test env (EIDOS_NO_DASHBOARD): tests mock `complete`, so probing a
    # real LLM endpoint only adds multi-second urlopen timeouts (a port may be up but lack /health).
    if not config.mock_mode and not os.environ.get("EIDOS_NO_DASHBOARD"):
        import urllib.request as _ur
        # Probe the UNIVERSAL OpenAI endpoint /v1/models (every OpenAI-compatible server exposes it:
        # llama.cpp, Ollama, LM Studio), not just llama.cpp's /health {"status":"ok"} — which Ollama and
        # LM Studio lack, and which would otherwise wedge a friend's boot at "waiting for LLM" forever.
        # Healthy on any 2xx from either probe. Base is normalized so a `…/v1` URL doesn't double up.
        _base = config.llm_url.rstrip("/")
        for _suf in ("/v1/chat/completions", "/chat/completions", "/v1"):
            if _base.endswith(_suf):
                _base = _base[: -len(_suf)].rstrip("/")
                break
        _probes = (_base + "/v1/models", _base + "/health")
        print(f"{pfx} Waiting for an OpenAI-compatible LLM server at {_base} ...")
        _health_wait = 0
        while not _shutdown_requested:
            _ok = False
            for _u in _probes:
                try:
                    with _ur.urlopen(_u, timeout=5) as _resp:
                        _code = getattr(_resp, "status", None) or _resp.getcode()
                        if 200 <= int(_code) < 300:
                            print(f"{pfx} LLM server reachable at {_u} (waited {_health_wait}s)")
                            _ok = True
                            break
                except Exception:
                    pass
            if _ok:
                break
            _health_wait += 5
            if _health_wait % 60 == 0:
                print(f"{pfx} Still waiting for LLM server... ({_health_wait}s) - is it running at {config.llm_url}?")
            time.sleep(5)

    # --- V3 nervous system (P3): the deliberative core's afferent intake. Inert until an organ
    #     publishes (no organs at P3, so this is a no-op for behaviour). A sensory init/drain failure
    #     must NEVER break the tick loop (I5), so everything here is guarded. ---
    nervous_bus = None
    afferent = None
    nervous_gpu = None
    nervous_neuromod = None
    nervous_learner = None
    nervous_worldmodel = None
    nervous_curiosity = None
    nervous_metabolism = None
    nervous_power = None
    if getattr(config, "nervous_enabled", False):
        try:
            import nervous
            nervous_bus = nervous.build_bus(config)
            afferent = nervous.AfferentContext.from_config(nervous_bus, config)
            # P2: the GPU lease arbiter (mind / TTS / escalated perception). DISPOSITION on this host:
            # deliberately a MONITOR-only holder display, NOT wired into the mind's decode. The host
            # keeps ONE model resident (llama-swap, ttl:0) and the ONLY real GPU contender is TTS,
            # which is already arbitrated event-driven by voice.py's speech-gate (gpu_gate.py +
            # /api/gpu/wait, ARCH#1). Routing the mind's blocking decode through acquire() here would
            # add a lease round-trip for zero benefit (nothing else holds the GPU) and risk serializing
            # the hot path. Leases become load-bearing only under MULTI-model residency or escalated
            # foveal perception (P6) — a future-host manifest, not this one. Constructed so the
            # behind-the-curtain monitor can show the holder; inert by design until such a claimant exists.
            nervous_gpu = nervous.GpuArbiter(bus=nervous_bus, log_path=str(config.nervous_gpu_leases_log_path))
            try:
                # config threads the genome's ±10% wake_budget gene into adenosine (fail-open ×1.0).
                nervous_neuromod = nervous.NeuromodulatoryState(nervous_bus, config=config)
                nervous_neuromod.start(2.0)   # Pillar 6: arousal + affect (mood)
            except Exception:  # noqa: BLE001
                nervous_neuromod = None
            print(f"{pfx} nervous bus up ({getattr(config, 'nervous_transport', 'inproc')}); afferent + GPU arbiter + neuromod ready")
        except Exception as _e:  # noqa: BLE001
            print(f"{pfx} nervous bus init failed (continuing without afferent): {_e}")
            nervous_bus = None
            afferent = None
            nervous_gpu = None

    # M0: metabolism — the energy economy (the organism's stakes). Food = literal battery power
    # (2026-06-20 pivot): drains with living + cognition (the dearest act) + action; recharges from
    # environmental power. This node is a "plant" (stationary + solar) → recharges from solar charge_in;
    # an "animal" would recharge by resting/docking. Real power source = Renogy BLE (SOC + PV watts);
    # interim = the solar daylight placeholder. Created BEFORE interoception so its hunger folds in.
    if nervous_bus is not None and getattr(config, "nervous_metabolism_enabled", True):
        try:
            from nervous.metabolism import Metabolism
            nervous_metabolism = Metabolism(bus=nervous_bus, config=config,
                                            archetype=getattr(config, "nervous_metabolism_archetype", "plant"),
                                            basal=getattr(config, "nervous_metabolism_basal_drain", 0.0006),
                                            cognition=getattr(config, "nervous_metabolism_cognition_drain", 0.002),
                                            action=getattr(config, "nervous_metabolism_action_drain", 0.001))
            print(f"{pfx} metabolism started — energy {nervous_metabolism.energy:.2f}; "
                  f"archetype={nervous_metabolism.archetype}; it can tire and hunger")
        except Exception as _e:  # noqa: BLE001
            print(f"{pfx} metabolism start failed (continuing): {_e}")

    # M4: real power — anchor the reserve to real battery SOC. The always-on DASHBOARD owns the single
    # Renogy BLE radio and writes a shared cache; eidos CONSUMES that cache (no second BLE contender) and
    # anchors its metabolism to it. So battery/solar stays live on the panel even when eidos is paused/
    # stopped, the radio has one owner, and Dean using the Renogy app just makes the cache go stale → the
    # reserve fails-open to the internal solar sim until the link frees. Default OFF; opt in via config.
    if (nervous_bus is not None and nervous_metabolism is not None
            and getattr(config, "power_enabled", False) and getattr(config, "power_mppt_address", "")):
        try:
            from nervous.power import PowerMonitor, cache_reader
            _pstale = getattr(config, "power_stale_after_s", 600.0)
            nervous_power = PowerMonitor(
                nervous_bus, config=config, metabolism=nervous_metabolism,
                reader=cache_reader(config, str(config.power_cache_path), max_age_s=_pstale),
                interval_s=getattr(config, "power_poll_interval_s", 60.0),
                stale_after_s=_pstale,
                backoff_max_s=getattr(config, "power_backoff_max_s", 600.0)).start()
            print(f"{pfx} power monitor started — anchoring to the dashboard's shared MPPT cache "
                  f"(dashboard owns the radio; falls back to the solar sim if the cache goes stale)")
        except Exception as _e:  # noqa: BLE001
            print(f"{pfx} power monitor start failed (continuing on internal energy sim): {_e}")

    # Phase 1.1: the organ registry — organs plug in via lifecycle hooks (pre/post_tick, on_sleep)
    # instead of being hand-called through the god-loop. The 4 migrated organs (interoception,
    # neuromod, goal-tension, curiosity) register below; the loop iterates the registry for their
    # per-tick work (see `organ_registry.run_post_tick` in the tick body). Incremental: the other
    # organs stay hand-called for now. Guarded per-hook inside the registry (I5).
    organ_registry = nervous.OrganRegistry() if nervous_bus is not None else None
    nervous_interoception = None

    # P1a: start interoception — the first organ. The creature feels its body: host telemetry ->
    # coarse felt bars on the bus -> surfaced in context via the afferent intake. Guarded (I5).
    if nervous_bus is not None and getattr(config, "nervous_interoception_enabled", True):
        try:
            from nervous.interoception import Interoception
            nervous_interoception = Interoception(
                nervous_bus,
                interval_s=getattr(config, "nervous_interoception_interval_s", 5.0),
                config=config, metabolism=nervous_metabolism)
            nervous_interoception.start()
            print(f"{pfx} interoception organ started — the creature feels its body")
        except Exception as _e:  # noqa: BLE001
            print(f"{pfx} interoception start failed (continuing): {_e}")

    # The nervous-system monitor — the operator's read-only "behind the curtain" window. It SUBSCRIBES
    # to the bus (I6, never recomputes) and writes a compact snapshot the dashboard serves to its tab.
    # Pure observer, fully guarded: a monitor fault can never touch the creature.
    if nervous_bus is not None and getattr(config, "nervous_monitor_enabled", True):
        try:
            from nervous.monitor import NervousMonitor
            NervousMonitor(nervous_bus, arbiter=nervous_gpu, config=config,
                           snapshot_path=str(config.nervous_snapshot_path),
                           interval_s=getattr(config, "nervous_monitor_interval_s", 1.0),
                           feed_max=getattr(config, "nervous_monitor_feed_max", 48)).start()
            print(f"{pfx} nervous monitor started — behind-the-curtain snapshot live")
        except Exception as _e:  # noqa: BLE001
            print(f"{pfx} nervous monitor start failed (continuing): {_e}")

    # The learning keystone (the dopaminergic basal-ganglia loop): a value cache + reward-prediction-error
    # that learns from the OUTCOMES of actions, with a sleep cycle that REPLAYS the tagged experiences into
    # durable lessons during calm lulls. The creature improves itself over time, in the memory substrate
    # (the LLM weights are frozen). Guarded — a learning fault can never break the tick.
    if nervous_bus is not None and getattr(config, "nervous_learning_enabled", True):
        try:
            from nervous.reward import RewardLearner
            from nervous.worldmodel import WorldModel
            from nervous.curiosity import CuriosityDrive
            from nervous.sleep import SleepCycle
            nervous_learner = RewardLearner(bus=nervous_bus, neuromod=nervous_neuromod, config=config)
            nervous_worldmodel = WorldModel(config=config)            # predicts situation transitions (T2)
            # levity (v3 playfulness gene) scales HOW HARD a predictable lull presses the body to move —
            # the curiosity restless-arousal floor cap. Congenital, bounded (gene clamps [0.6,1.6]); the
            # accessor is fail-open ×1.0, so an absent genome leaves behavior byte-identical.
            try:
                from genome import gene as _gene
                _levity = float(_gene(config, "levity"))
            except Exception:  # noqa: BLE001
                _levity = 1.0
            nervous_curiosity = CuriosityDrive(bus=nervous_bus, neuromod=nervous_neuromod,
                                               restless_arousal_max=0.5 * _levity)  # novelty → intrinsic reward
            SleepCycle(nervous_bus, neuromod=nervous_neuromod, learner=nervous_learner,
                       sleep_arousal=getattr(config, "nervous_learning_sleep_arousal", 0.32),
                       min_consolidate_interval_s=getattr(config, "nervous_learning_consolidate_interval_s", 120.0)
                       ).start(getattr(config, "nervous_learning_sleep_interval_s", 10.0))
            print(f"{pfx} reward learning + world-model + curiosity + dream replay started — the creature learns")
        except Exception as _e:  # noqa: BLE001
            print(f"{pfx} reward learning start failed (continuing): {_e}")
    _wm_prev_sit = None   # previous tick's situation/action — for the world-model transition + curiosity
    _wm_prev_act = None

    # Temperament (DMN): the slow personality drift — initiative / persistence / caution, learned from
    # this creature's own success / failure / override history. Persisted, so it survives a restart; it
    # needs no bus (it's pure state + setpoints), so it loads even when the nervous system is off. Feeds
    # the objectives gate's park threshold and the goal-tension itch — MECHANISM, not prompt knobs.
    nervous_temperament = None
    if getattr(config, "nervous_temperament_enabled", True):
        try:
            from nervous.temperament import Temperament
            nervous_temperament = Temperament(config=config)
            print(f"{pfx} temperament loaded — disposition: {nervous_temperament.disposition()} "
                  f"(init {nervous_temperament.initiative:.2f} / persist {nervous_temperament.persistence:.2f} "
                  f"/ caution {nervous_temperament.caution:.2f})")
        except Exception as _e:  # noqa: BLE001
            print(f"{pfx} temperament load failed (continuing neutral): {_e}")
            nervous_temperament = None

    # Goal-tension drive (Ventral Striatum): incompletion / regret pressure → a bounded arousal floor
    # that keeps the creature awake-and-acting while an objective is unfinished (the structural form of
    # "initiative when idle"). Needs the bus + neuromod for its teeth (arousal → sleep/cadence); inert
    # without them. Relieved by real progress.
    nervous_goaltension = None
    if nervous_bus is not None and getattr(config, "nervous_goaltension_enabled", True):
        try:
            from nervous.goaltension import GoalTensionDrive
            # press_scale (v3 boldness gene) scales how hard an unfinished goal presses — the
            # goal-tension arousal floor cap. Congenital, bounded (gene clamps [0.7,1.4]); fail-open ×1.0.
            try:
                from genome import gene as _gene
                _press = float(_gene(config, "press_scale"))
            except Exception:  # noqa: BLE001
                _press = 1.0
            nervous_goaltension = GoalTensionDrive(bus=nervous_bus, neuromod=nervous_neuromod,
                                                   tension_arousal_max=0.4 * _press)
            print(f"{pfx} goal-tension drive started — an unfinished objective now keeps it awake")
        except Exception as _e:  # noqa: BLE001
            print(f"{pfx} goal-tension start failed (continuing): {_e}")

    # --- Phase 1.1: register the 4 migrated organs with the registry, IN PER-TICK ORDER, so the
    #     loop iterates the registry for their per-tick work instead of hand-calling them. Order
    #     matters and matches the pre-refactor call sequence: interoception → neuromod (both
    #     thread-driven, so no per-tick hook — declared topics only), then goal-tension, then
    #     curiosity (goal-tension fired before curiosity in the old loop). reads/writes are the
    #     declared bus topics for future conflict-checking (inert today). The organs' hooks read
    #     everything they need from the per-tick `ctx` handed to run_post_tick below. ---
    if organ_registry is not None:
        if nervous_interoception is not None:
            # Self-runs on its own thread (I6, single writer of the felt-state); no per-tick hook.
            organ_registry.register(nervous_interoception, name="interoception",
                                    writes=("interoceptive/intero",))
        if nervous_neuromod is not None:
            # Self-runs on its own thread (drains interoception, publishes modulation); no per-tick hook.
            organ_registry.register(nervous_neuromod, name="neuromod",
                                    reads=("interoceptive/intero",), writes=("modulation/system",))
        if nervous_goaltension is not None:
            organ_registry.register(nervous_goaltension, name="goal_tension",
                                    post_tick=_goaltension_post_tick, writes=("drive/goal_tension",))
        if nervous_curiosity is not None:
            organ_registry.register(nervous_curiosity, name="curiosity",
                                    post_tick=_curiosity_post_tick, writes=("drive/curiosity",))

    # --- Causal ledger (Pillars 0.3): one record of the full pressure field per tick, so any
    #     action is replayable as "show me the field that produced this" (§8 pitfall #12). Ships
    #     DARK — instantiated only when the flag is on, so it is a strict no-op otherwise. ---
    pressure_ledger = None
    if getattr(config, "pillars_causal_ledger_enabled", False):
        try:
            import pressures as _pressures
            pressure_ledger = _pressures.PressureLedger(
                config, max_bytes=config.pillars_causal_ledger_max_bytes)
            print(f"{pfx} causal ledger on — logging the pressure field each tick (pressures.jsonl)")
        except Exception as _e:  # noqa: BLE001 - the ledger must never break boot (I5)
            print(f"{pfx} causal ledger start failed (continuing without it): {_e}")

    # --- Pillars 5.5: the wiring hub — every dark organ's call sites, still dark. Constructed
    #     ONLY when at least one pillars flag is on; `pillars = None` is the flags-off ground
    #     state, so the tick body's new branches add ZERO work (and zero imports) to today's
    #     loop. Guarded (I5): a wiring fault at boot degrades to the unwired loop, never a crash. ---
    pillars = None
    if _pillars_any_enabled(config):
        try:
            pillars = _Pillars(config, bus=nervous_bus, neuromod=nervous_neuromod,
                               organ_registry=organ_registry, curiosity=nervous_curiosity,
                               learner=nervous_learner)
            pillars.temperament = nervous_temperament
            pillars.metabolism = nervous_metabolism
            # H4 cutover: when the salience gate is live, route the core's afferent intake THROUGH its
            # ranked admission (top-down relevance × arousal gain × habituation × exploration floor),
            # instead of the core reading a separate raw-bus-order subscription the gate never fed.
            # attach_gate unsubscribes afferent's own sub so nothing is double-delivered; flag-off has
            # no gate so this never fires and the intake stays byte-identical.
            if afferent is not None and getattr(pillars, "salience", None) is not None:
                try:
                    afferent.attach_gate(pillars.salience)
                    print(f"{pfx} afferent intake routed through the salience gate (ranked admission live)")
                except Exception as _ge:  # noqa: BLE001 - a wiring fault must never break boot
                    print(f"{pfx} salience-gate afferent routing failed (raw intake continues): {_ge}")
            print(f"{pfx} pillars wiring up — {pillars.describe()}")
        except Exception as _e:  # noqa: BLE001 - the hub must never break boot (I5)
            print(f"{pfx} pillars wiring init failed (continuing dark): {_e}")
            pillars = None

    while not _shutdown_requested:
        # --- Operator pause check ---
        pause_path = config.workspace / "paused"
        if pause_path.exists():
            if not operator_paused:
                print(f"{pfx} Operator paused — waiting for resume")
                append_observation(config, {
                    "tick": tick_number,
                    "tool": "system",
                    "success": True,
                    "output": "Paused by operator via dashboard. Tick loop suspended.",
                })
                operator_paused = True
            listening_since = None  # operator pause supersedes a soft listening hold
            # Event-driven: held server-side until the operator resumes (or a bounded timeout);
            # the pause file above stays the crash-survivable ground truth we re-check each pass.
            _control_wait_change(config, max_s=25.0)
            continue
        elif operator_paused:
            print(f"{pfx} Resuming from operator pause")
            append_observation(config, {
                "tick": tick_number,
                "tool": "system",
                "success": True,
                "output": "Resumed by operator. Resuming tick loop.",
            })
            operator_paused = False

        # --- Listening hold (soft pause: Dean has the chat box focused) ---
        # Distinct from operator pause. The in-flight tick already finished; we simply do
        # NOT start a new generation while Dean is composing. Fails open to autonomy.
        if _chat_hold_active(config):
            if listening_since is None:
                listening_since = time.time()
                logger.info("Listening hold engaged (Dean focused chat) — quieting the loop")
                # Pillars 4.4: the hold IS the presence signal — surface the news digest once per
                # engagement (dark: no-op unless the news flag is on). Guarded inside (I5).
                if pillars is not None:
                    pillars.on_presence()
            held_s = int(time.time() - listening_since)
            write_activity(config, "listening", detail=f"listening to Dean ({held_s}s)")
            # Event-driven: wakes the instant the hold releases (blur/send) or chat arrives;
            # the short bound keeps the "listening Ns" display fresh and re-applies TTL rules.
            _control_wait_change(config, max_s=5.0)
            continue
        elif listening_since is not None:
            logger.info("Listening hold released — resuming autonomous loop")
            listening_since = None

        # --- Check for goal --- (creature mode has no assignment: it runs regardless)
        goal = read_goal(config) or ""
        if not goal and not getattr(config, "creature_mode", False):
            if idle_since is None:
                idle_since = time.time()
            if config.mock_mode:
                print("[eidos] No goal.md — exiting (mock mode)")
                break
            _interruptible_sleep(config)
            continue
        idle_since = None

        # Causal ledger (0.3): snapshot XP now so end-of-tick can log the DELTA this tick paid.
        # Cheap and flag-independent (a plain int read); the ledger only consumes it when on.
        _xp_at_tick_start = persona.get("xp", 0) if persona else 0
        _tick_admitted_events = 0

        # --- Goal change detection (hash tracking only) ---
        import hashlib
        goal_hash = hashlib.md5(goal.encode()).hexdigest()
        goal_changed = last_goal_hash is not None and goal_hash != last_goal_hash
        if goal_changed:
            goal_start_time = time.time()
        last_goal_hash = goal_hash

        # --- Compaction check (or an operator-forced sleep) ---
        tick_compacted = False
        _forced_sleep = _consume_sleep_now(config)
        if _forced_sleep:
            print(f"{pfx} Operator-forced sleep — consolidating now.")
        if should_compact(config, ticks_since_compaction) or _forced_sleep:
            print(f"{pfx} Dreaming... consolidating memories.")
            write_activity(config, "dreaming", detail="consolidating memories")
            try:
                compact_briefing(config, persona=persona)
                emit_flavor(config, persona)
                ticks_since_compaction = 0
                tick_compacted = True
                # H3: bring the dream's newly-distilled knowledge into the ENGRAM store. With
                # memory_manager on, the relevance-recall cascade the model sees each tick reads
                # ENGRAMS — but the importer otherwise runs only once at boot, so everything learned
                # since the last restart (dream-distilled facts, memorized facts) was invisible to
                # relevance recall (the "wakes amnesiac / re-derives what it stored" failure). Re-run
                # the idempotent importer here so the engram store stays current with new learning.
                if pillars is not None and getattr(pillars, "manager", None) is not None:
                    try:
                        _n = pillars.manager.import_knowledge()
                        if _n:
                            logger.info("dream: imported %d new knowledge entries into engrams", _n)
                    except Exception as _ie:  # noqa: BLE001 - a memory sync fault never breaks the dream
                        logger.warning("post-dream knowledge import failed: %s", _ie)
                if persona and config.persona_enabled:
                    record_compaction(persona, config=config)
                    pfx = _pfx(persona, config)
                print(f"{pfx} Memories consolidated.")
            except LLMError as e:
                print(f"{pfx} Compaction failed: {e}")
                append_observation(config, {
                    "tick": tick_number,
                    "tool": "dream",
                    "success": False,
                    "output": f"Compaction failed: {e}",
                })
            # Pillars 2.4 (dark): the real sleep engine runs ALONGSIDE the legacy compaction at the
            # same sleep window — the legacy path above is untouched; run_sleep is a logged no-op
            # unless pillars_sleep_engine_enabled is on. Guarded (I5): a sleep fault never wounds
            # the tick. Sleep completion also drives 4.3/5.1/5.2's sleep-boundary events inside.
            if pillars is not None:
                try:
                    from memory import read_recent_observations as _rro
                    _sleep_obs = _rro(config, max_chars=8000, max_count=60)
                except Exception:  # noqa: BLE001
                    _sleep_obs = []
                try:
                    pillars.sleep_window(tick=tick_number, persona=persona,
                                         observations=_sleep_obs)
                except Exception as _pse:  # noqa: BLE001 - the window is guarded end-to-end (I5)
                    logger.warning("pillars sleep window failed: %s", _pse)
            # An operator-forced sleep also re-distills the REWARD lessons now — the background
            # SleepCycle only replays at low arousal, so without this a forced digest could miss
            # the very signal we force it for (does the value cache still say "thought goes well"?).
            if _forced_sleep and nervous_learner is not None:
                try:
                    _rp = nervous_learner.replay()
                    print(f"{pfx} Forced reward replay: re-distilled "
                          f"{len((_rp or {}).get('lessons') or [])} lessons.")
                except Exception as _re:  # noqa: BLE001 - never let a forced digest wound the tick
                    logger.warning("forced reward replay failed: %s", _re)

        # --- RAM check (observation only; the model is the big consumer and it's a
        # service we don't own — there are no expendable children worth killing) ---
        ram_ok, ram_pct = check_ram(config.ram_max_pct)
        if not ram_ok:
            append_observation(config, {
                "tick": tick_number,
                "tool": "system",
                "success": False,
                "output": f"RAM pressure: {ram_pct:.0f}% used (threshold {config.ram_max_pct:.0f}%). "
                          f"Avoid dispatching heavy new jobs until it falls.",
            })
            print(f"{pfx} RAM pressure: {ram_pct:.0f}%")

        # --- Loop detection ---
        loop_detected = False
        repeat_count = 0
        if len(recent_hashes) >= config.loop_detect_window:
            uniq = set(recent_hashes)
            if len(uniq) <= 2:
                loop_detected = True   # repeating one action, or cycling between two (A-B-A-B)
                repeat_count = len(recent_hashes)
            elif all(str(h).startswith("th_") or h == "__no_tool__" for h in recent_hashes):
                loop_detected = True   # ruminating: thinking without acting
                repeat_count = len(recent_hashes)

        # --- Deliver async tool results that finished since last tick ---
        # Fire-and-forget bash dispatches land here when done, tagged [↩ job N], and flow
        # into context as normal result-turns so the model pairs them with its dispatch.
        try:
            for fin in collect_finished_jobs(config):
                status = fin.get("status")
                if fin.get("kind") == "delegate":
                    # Delegate (pi coding agent) results get their own compact formatting:
                    # digest + files touched + resume hint, not a raw output tail.
                    try:
                        import delegate as _dlg
                        d_out, d_ok = _dlg.format_result_observation(config, fin)
                    except Exception:  # noqa: BLE001 — formatting must never lose the result
                        d_out = (f"[↩ delegate {fin.get('name')}] (result formatting failed)\n"
                                 f"{(fin.get('tail') or '')[:1200]}")
                        d_ok = status == "completed"
                    append_observation(config, {
                        "tick": tick_number,
                        "tool": "async_result",
                        "args": {"job": fin.get("name")},
                        "fail_kind": "" if d_ok else ("timeout" if status == "timed_out" else "exec"),
                        "success": d_ok,
                        "output": d_out,
                    })
                    continue
                if status == "completed":
                    ok, f_kind = "OK", ""
                elif status == "timed_out":
                    ok, f_kind = "TIMED OUT", "timeout"
                else:
                    ec = fin.get("exit_code")
                    ok = f"FAILED (exit {ec})" if ec is not None else "FAILED"
                    f_kind = "exec"
                cmd_s = (fin.get("cmd") or "")[:70]
                body = (fin.get("tail") or "").strip() or "(no output)"
                intent = fin.get("intent")
                intent_s = f" (you wanted: {intent})" if intent else ""
                append_observation(config, {
                    "tick": tick_number,
                    "tool": "async_result",
                    "args": {"job": fin.get("name")},
                    "fail_kind": f_kind,
                    "success": status == "completed",
                    "output": f"[↩ job {fin.get('name')} · {cmd_s} · {ok}]{intent_s}\n{body}",
                })
        except Exception as e:  # noqa: BLE001
            logger.warning("async result delivery failed: %s", e)

        # --- Reflex rung (WISDOM_PLAN §1): compile thinking BELOW the model. BEFORE context
        #     assembly, match ARMED reflexes against the CURRENT typed situation; on a guard-true
        #     match, execute the action through execute_tool (the one chokepoint), record the
        #     outcome marked `"automated": true` (WIS2 — pays NO economy: no record_tick XP, no
        #     pillars.after_outcome bets/mastery/learning, no reward learner — this branch simply
        #     does not call them), render "[REFLEX]" into the stream (WIS3). When reflex_saves_tick
        #     is on, the LLM call is skipped entirely this tick; else (the conservative soak) the
        #     reflex result rides in-stream and the normal tick runs. Flag-dark: pillars-independent,
        #     fully inert unless wisdom_reflexes_enabled (WIS7). ---
        reflex_saved_tick = _maybe_fire_reflex(config, persona, tick_number)
        if reflex_saved_tick:
            # A reflex handled this tick and reflex_saves_tick is on — end the tick here, the same
            # bounded tail the LLM-error early-continue uses (increment, WAL, interruptible sleep).
            # No LLM ran, so no economy feed runs: WIS2 holds by construction.
            tick_number += 1
            ticks_since_compaction += 1
            write_wal(config, tick_number, ticks_since_compaction,
                      goal_start_time, consecutive_failures,
                      reasoning_exhaustions, current_max_tokens,
                      last_progress_tick)
            _interruptible_sleep(config)
            continue

        # --- Assemble context ---
        # `tension` is now the ACTIVE objective's frustration (the gate's per-objective counter),
        # falling back to the legacy global stall count if the backlog isn't available.
        try:
            import objectives as _obj
            _act = _obj.get_active(config)
            tension = int(_act["frustration"]) if _act else max(0, tick_number - last_progress_tick)
        except Exception:  # noqa: BLE001
            tension = max(0, tick_number - last_progress_tick)
        # V3 afferent intake (P3): drain admitted sensory events for this tick (non-blocking,
        # batched into the volatile situation tail). Empty until an organ publishes.
        afferent_block = ""
        if afferent is not None:
            try:
                afferent_block, _aff_n = afferent.drain_block()
                _tick_admitted_events = int(_aff_n or 0)
            except Exception:  # noqa: BLE001 - a sensory bug must never break the tick (I5)
                afferent_block = ""
        # Pillars 5.5 (dark): pre-deliberation wiring — adenosine accounting (2.4), relevance_set
        # publication + gate intake (1.3), and the manager's recall (2.2, which context.py swaps in
        # for the legacy cascade ONLY when its flag is on). All no-ops with flags off (pillars=None).
        # OPERATOR_DIRECTIVES: before building context, let the System turn a pending Charlie
        # message into a priority focus, so this tick's context already carries it as the active
        # objective (and the creature still replies via the normal boss-waiting path). No-op unless
        # operator_directives_enabled + a message is pending. Both live on the pillars hub (they
        # need its llm seam / config); no-ops when the hub is off or the flags are dark.
        if pillars is not None:
            pillars._operator_directive(persona, tick_number)
            # A due reminder ("check in in 10 min") surfaces here too — persistent, restart-surviving.
            pillars._deliver_due_reminders(tick_number)

        pillars_recall_block = ""
        if pillars is not None:
            try:
                pillars.pre_tick(tick_number)
                try:
                    import episodes as _ep_sit
                    _p_sit = _ep_sit.situation_key(config)
                except Exception:  # noqa: BLE001
                    _p_sit = ""
                pillars_recall_block = pillars.recall_block(situation=_p_sit,
                                                            query=pillars.focus_query())
            except Exception as _ppe:  # noqa: BLE001 - wiring faults never break the tick (I5)
                logger.warning("pillars pre-tick wiring failed: %s", _ppe)
                pillars_recall_block = ""
        messages = assemble_context(
            config,
            tick_number=tick_number,
            goal_start_time=goal_start_time,
            loop_detected=loop_detected,
            repeat_count=repeat_count,
            tension=tension,
            afferent_block=afferent_block,
            pillars_recall_block=pillars_recall_block,
        )
        # Pillars 2.3 (dark): every engram the recall injected is an open wager on this tick.
        if pillars is not None:
            pillars.open_bets(tick_number)

        # Log context size for monitoring
        ctx_chars = sum(len(m["content"]) for m in messages)
        ctx_tokens_est = int(ctx_chars / config.chars_per_token)
        print(f"{pfx} Tick {tick_number}: ctx={ctx_chars} chars ~{ctx_tokens_est} tokens")

        # --- Mock mode: print context ---
        if config.mock_mode:
            print(f"\n{'='*60}")
            print(f"TICK {tick_number}")
            print(f"{'='*60}")
            for msg in messages:
                role = msg["role"].upper()
                content = msg["content"]
                print(f"\n--- {role} ---")
                print(content[:2000] if len(content) > 2000 else content)

        # --- LLM call ---
        get_cpu_pct()  # prime CPU counter so post-LLM read captures active period
        llm_start = time.monotonic()
        tick_tool_name = ""
        tick_tool_success = False
        tick_tool_duration = 0.0
        tick_fail_kind = ""
        # Human-readable action label for the reward learner + world model: "bash: ls holt/",
        # never a loop-detector hash — the value cache's distilled lessons are RENDERED back into
        # the creature's context, and a lesson about "a129652c…" teaches nothing (live bug: the
        # whole Learned block was MD5 hashes). Falls back to the tool name for tool-less ticks.
        tick_action_label = ""
        tick_summary = ""
        tick_output = ""      # the full tool output (Pillars 4.1's event-closure text; unused dark)
        tick_situation = ""   # the SITUATION digest for this tick's episode (captured pre-action)
        write_activity(config, "thinking", detail=f"tick {tick_number}")

        # Capture the SITUATION the model is deciding in (phase 7b) — the same key episodic recall
        # used during context assembly — so this tick's episode is filed under the situation it acted in.
        try:
            import episodes as _ep
            tick_situation = _ep.situation_key(config)
        except Exception:  # noqa: BLE001
            tick_situation = ""

        # Streaming reply→voice (phase 3): when Boss is waiting, the reply streams first and the
        # pump fires TTS on its opening sentence mid-generation — first-audio ~2.5s, not ~12s.
        boss_waiting = _has_pending_interventions(config)
        voice_pump = _ReplyVoicePump(config)

        def _on_token(partial_text):
            write_activity(config, "thinking", detail=f"tick {tick_number}",
                           partial=partial_text)
            voice_pump.feed(partial_text)

        # GPU speech-gate (ARCHITECTURE_PRINCIPLES.md #1): if the dashboard is mid-TTS, yield the
        # GPU and resume the instant synthesis finishes (event-driven, bounded). Speech preempts the
        # background tick so voice stays crisp; returns immediately when no speech is in flight.
        # Timed + surfaced in the heartbeat so speech-contention delays are visible, not silent.
        _gate_t = time.monotonic()
        _gate = yield_to_speech(config)
        gate_wait_s = time.monotonic() - _gate_t
        gate_reason = str((_gate or {}).get("reason", "")) if gate_wait_s >= 0.25 else ""
        if gate_wait_s >= 0.25:
            logger.info("gpu gate held the tick %.1fs (%s)", gate_wait_s, gate_reason or "tts")

        tick_grammar = None
        if getattr(config, "llm_grammar_enabled", True) and not config.mock_mode:
            try:
                from grammar import tick_grammar_cached
                from tools import visible_tools
                # The grammar is built from the ONE accessor every surface reads (TOOL_PROGRESSION):
                # a locked name is UNREPRESENTABLE at the sampler. Ladder off → visible_tools IS
                # the registry object, so the cached grammar is byte-identical to the pre-ladder
                # build (tick_grammar_cached keys on the sorted name tuple).
                _live_tools = visible_tools(config)
                # Boss waiting → require_reply so the reply is generated FIRST and streams to
                # TTS while the rest of the tick (tool call) is still decoding.
                tick_grammar = tick_grammar_cached(_live_tools.keys(), require_reply=boss_waiting)
            except Exception as _ge:  # noqa: BLE001 - grammar is an enhancement, never a blocker
                logger.warning("tick grammar build failed (running unconstrained): %s", _ge)

        try:
            response = complete(messages, config, max_tokens=current_max_tokens,
                                on_token=_on_token, tick=tick_number,
                                grammar=tick_grammar)
            llm_elapsed = time.monotonic() - llm_start
            consecutive_failures = 0  # reset on success

            # Successful content — decay max_tokens back toward baseline
            if current_max_tokens > config.llm_max_tokens:
                current_max_tokens = max(
                    config.llm_max_tokens,
                    current_max_tokens - config.llm_token_backoff_step,
                )
            reasoning_exhaustions = 0

        except ReasoningExhausted as e:
            llm_elapsed = time.monotonic() - llm_start
            reasoning_exhaustions += 1

            # Bump max_tokens for next tick (up to ceiling)
            current_max_tokens = min(
                current_max_tokens + config.llm_token_backoff_step,
                config.llm_max_tokens_ceiling,
            )
            logger.warning(
                "Reasoning exhausted (%d/%d tokens, attempt %d). "
                "Next tick max_tokens=%d.",
                e.reasoning_tokens, e.max_tokens,
                reasoning_exhaustions, current_max_tokens,
            )

            append_observation(config, {
                "tick": tick_number,
                "tool": "system",
                "fail_kind": "llm",
                "success": False,
                "output": (
                    f"Token budget exhausted by reasoning "
                    f"({e.reasoning_tokens}/{e.max_tokens} tokens used, 0 content). "
                    f"Next tick budget raised to {current_max_tokens}. "
                    f"Keep your thinking brief and go straight to the tool call."
                ),
            })

            # After repeated exhaustions, force compaction to shrink context
            if (reasoning_exhaustions >= config.llm_reasoning_exhaust_compaction_trigger
                    and ticks_since_compaction > 0):
                logger.warning(
                    "Forcing compaction after %d consecutive reasoning exhaustions",
                    reasoning_exhaustions)
                try:
                    compact_briefing(config, persona=persona)
                    ticks_since_compaction = 0
                    if persona and config.persona_enabled:
                        record_compaction(persona, config=config)
                        pfx = _pfx(persona, config)
                except LLMError as ce:
                    logger.error("Forced compaction failed: %s", ce)

            # Increment BEFORE persisting so the WAL records the NEXT tick, matching the happy
            # path (2975-2982). Otherwise a crash in this failure window resumes and re-runs this
            # same tick number, replaying its pre-LLM organ effects.
            tick_number += 1
            ticks_since_compaction += 1
            write_wal(config, tick_number, ticks_since_compaction,
                      goal_start_time, consecutive_failures,
                      reasoning_exhaustions, current_max_tokens,
                      last_progress_tick)
            # Interruptible (ARCH #1): during a failure storm a Boss message still wakes the loop.
            _interruptible_sleep(config)
            continue

        except LLMError as e:
            llm_elapsed = time.monotonic() - llm_start
            consecutive_failures += 1
            print(f"{pfx} LLM error on tick {tick_number} "
                  f"({consecutive_failures}/{config.llm_max_consecutive_failures}): {e}")
            append_observation(config, {
                "tick": tick_number,
                "tool": "llm_error",
                "fail_kind": "llm",
                "success": False,
                "output": f"LLM call failed ({consecutive_failures}x): {e}",
            })

            # The model is an nssm service owned outside eidos (HouseAI-Llama); eidos cannot
            # restart it. After repeated failures, note it loudly — the operator/watchdog owns
            # recovery. (v2 phase 4 turns this into a typed event to the supervisor.)
            if consecutive_failures >= config.llm_max_consecutive_failures:
                print(f"{pfx} LLM unreachable after {consecutive_failures} consecutive failures "
                      f"— it is an external service; waiting for it to return")

            # Increment BEFORE persisting so the WAL records the NEXT tick, matching the happy
            # path (2975-2982). Otherwise a crash in this failure window resumes and re-runs this
            # same tick number, replaying its pre-LLM organ effects.
            tick_number += 1
            ticks_since_compaction += 1
            write_wal(config, tick_number, ticks_since_compaction,
                      goal_start_time, consecutive_failures,
                      reasoning_exhaustions, current_max_tokens,
                      last_progress_tick)
            # Interruptible (ARCH #1): during a failure storm a Boss message still wakes the loop.
            _interruptible_sleep(config)
            continue

        if config.mock_mode:
            print(f"\n--- RESPONSE ---")
            print(response)

        # --- Parse reply (chat response to operator) ---
        reply_text = parse_reply(response)
        if reply_text:
            _write_chat_reply(config, tick_number, reply_text)
            print(f"{pfx} Tick {tick_number}: chat reply sent ({len(reply_text)} chars)")

        # --- Capture this tick's reasoning as a thought (the continuity chain) ---
        thought = _extract_thought(response)
        # The byte-collapse (¥¥¡…) is dropped downstream by append_thought/append_chat_line, but it
        # doesn't reproduce synthetically — so log the RAW response that triggered it for analysis.
        if is_degenerate(response) or has_junk_run(response):
            log_degeneration(config, tick_number, response,
                             reason="junk_run" if has_junk_run(response) else "loop")
        if thought:
            # Clamp the STORED thought to the creature's life-stage (a newborn remembers a fragment,
            # not an essay) — post-parse, so the action already read the FULL response and is untouched.
            # This starves the self-imitation loop that makes a hatchling drift into architect-essays.
            if getattr(config, "pillars_tool_unlocks_enabled", False):
                import creature_gen as _cg
                thought = _clamp_thought_for_stage(thought, _cg.current_stage(config))
            append_thought(config, tick_number, thought)

        # --- Parse tool call ---
        call = parse_tool_call(response)
        # Auto-speak: voice the reply so Boss HEARS every response (first-class voice + backstop for when
        # the model hedges with text instead of calling `speak`). Skip if the model called `speak`
        # itself, or if the streaming pump already voiced this reply's opener mid-generation (phase 3).
        if (reply_text and not (call and getattr(call, "tool", "") == "speak")
                and not voice_pump.already_spoke(reply_text)):
            _auto_speak(config, reply_text)
        if not call:
            if reply_text:
                # Reply-only turn: no tool call needed — valid chat response
                append_observation(config, {
                    "tick": tick_number,
                    "tool": "chat_reply",
                    "success": True,
                    "output": f"Replied to operator: {reply_text[:200]}",
                })
                recent_hashes.append("__chat_reply__")
                if persona and config.persona_enabled:
                    record_tick(persona, "chat_reply", True, config=config)
                    tick_tool_name = "chat_reply"
                    tick_tool_success = True
            elif thought and len(thought) > 8:
                # Pure thought — a valid moment of reflection with no action. This is
                # normal stream-of-consciousness; do NOT nag for a tool call.
                append_observation(config, {
                    "tick": tick_number,
                    "tool": "thought",
                    "success": True,
                    "output": thought[:300],
                })
                recent_hashes.append("th_" + hashlib.md5(thought[:120].encode("utf-8", "ignore")).hexdigest()[:8])
                if persona and config.persona_enabled:
                    record_tick(persona, "thought", True, config=config)
                    tick_tool_name = "thought"
                    tick_tool_success = True
                print(f"{pfx} Tick {tick_number}: thought (no action)")
            else:
                # Give the model actionable feedback so it can self-correct.
                raw_snippet = response[:300].replace('\n', ' ').strip()
                feedback = (
                    f"Could not parse a tool call from your response. "
                    f"Your output began with: {raw_snippet!r}\n\n"
                    f"Required format (exactly):\n"
                    f"<tool>TOOL_NAME</tool>\n"
                    f"<args>{{\"key\": \"value\"}}</args>\n\n"
                    f"Common mistakes: unescaped quotes inside JSON strings, "
                    f"missing </args> tag, arguments not valid JSON. "
                    f"Try again with a single, correctly-formatted tool call."
                )
                append_observation(config, {
                    "tick": tick_number,
                    "tool": "parse_error",
                    "fail_kind": "parse",
                    "success": False,
                    "output": feedback,
                })
                print(f"{pfx} Tick {tick_number}: no valid tool call parsed")
                # Hash as empty for loop detection
                recent_hashes.append("__no_tool__")
                tick_fail_kind = "parse"
                if persona and config.persona_enabled:
                    record_tick(persona, None, False, config=config)
                    last_tick_failed = True
                    tick_tool_name = "parse_error"
        else:
            # --- Execute tool ---
            write_activity(config, "executing", detail=call.tool)
            result = execute_tool(call, config)
            tick_tool_name = call.tool
            tick_tool_success = result.success
            tick_tool_duration = result.duration_s
            tick_fail_kind = result.fail_kind
            # The summary is the one-line digest that lands in episode/engram bodies — cut at a
            # word boundary (a mid-word slice here survives every downstream cap unhealed).
            import episodes as _ep
            tick_summary = _ep.clean_fragment(result.output or "", _ep.SUMMARY_CHARS)
            tick_output = (result.output or "")[:2000]

            # --- Log observation ---
            append_observation(config, {
                "tick": tick_number,
                "tool": call.tool,
                "args": call.args,
                "fail_kind": result.fail_kind,
                "success": result.success,
                "output": result.output,
                "duration_s": result.duration_s,
            })

            if config.mock_mode:
                status = "OK" if result.success else "FAIL"
                print(f"\n--- TOOL RESULT ({call.tool} | {status}) ---")
                print(result.output[:1000])

            # --- Persona update ---
            if persona and config.persona_enabled:
                record_tick(persona, call.tool, result.success, config=config)
                if result.success and last_tick_failed:
                    record_error_recovery(persona, config=config)
                if call.tool == "objective_done" and result.success:
                    # goals_completed's ONLY production writer. Without this the counter is dead
                    # and any quest keyed on it (genesis-03) can never pass — and an eternally
                    # ACTIVE quest freezes the mastery gate's quest_line_closed check: a level
                    # brick. A finished self-chosen objective IS the completed goal.
                    from persona import record_goal_complete
                    _summary = ""
                    if isinstance(call.args, dict):
                        _summary = str(call.args.get("key") or call.args.get("title") or "")
                    record_goal_complete(persona, _summary, config=config)
                    # SOTA#3: a finished self-goal → a "reuse this" guardrail the recall cascade
                    # can surface next time (event-driven, own flag, fail-open inside).
                    if pillars is not None:
                        pillars._distill_strategy({
                            "title": _summary or "a self-chosen objective", "outcome": "done",
                            "success": True, "reason": tick_summary,
                            "situation": tick_situation or "", "trajectory": tick_summary,
                        }, tick=tick_number)
                last_tick_failed = not result.success
                pfx = _pfx(persona, config)

            # --- Loop detection hash (NORMALIZED) ---
            # Hash bash on the normalized command so v3/v4/v5 variations of the SAME command collapse
            # to one signature — exact-match on full args missed the real rumination at tick 969.
            if call.tool == "bash" and isinstance(call.args, dict):
                _cmd = call.args.get("cmd") or call.args.get("command") or ""
                call_hash = hashlib.md5(("bash:" + _norm_cmd(_cmd)).encode()).hexdigest()
                tick_action_label = f"bash: {_norm_cmd(_cmd)[:60]}"
            else:
                # Normalize the args (collapse digit runs / quoting, like bash) so an ARG-VARIED loop of
                # the SAME tool — read_file a1.txt/a2.txt…, port_probe :8001/:8002…, a re-numbered
                # notebook — collapses to ONE signature and is caught as rumination, instead of looking
                # novel every tick and slipping past the loop detector.
                try:
                    _args_json = json.dumps(call.args, sort_keys=True, ensure_ascii=False)
                except (TypeError, ValueError):
                    _args_json = str(call.args)
                call_hash = hashlib.md5(_norm_cmd(call.tool + ":" + _args_json).encode()).hexdigest()
                _args_terse = _args_json[:60]
                tick_action_label = f"{call.tool} {_args_terse}".strip()
            recent_hashes.append(call_hash)

        # --- Log rotation check (every 50 ticks) ---
        if tick_number % 50 == 0:
            rotated = rotate_if_needed(config)
            rotate_llm_log(config)
            rotate_metrics(config)
            rotate_thoughts(config)
            cleanup_old_snapshots(config)
            if rotated:
                append_observation(config, {
                    "tick": tick_number,
                    "tool": "system",
                    "success": True,
                    "output": ("Observation log rotated. Older entries archived. "
                               "Your recent observation history starts from this point — "
                               "consult working memory for earlier context."),
                })

        # --- Persona periodic save (every 10 ticks) ---
        if persona and config.persona_enabled and tick_number % 10 == 0:
            compute_traits(persona)
            check_titles(persona)
            persona["uptime_total_s"] = persona.get("uptime_total_s", 0) + int(time.monotonic() - loop_start)
            save_persona(config.workspace, persona)

        # --- Telemetry ---
        _disk_ok, _disk_free = check_disk_space(min_gb=0)
        _ram_ok, _ram_pct = check_ram(config.ram_max_pct)
        _cpu_pct = get_cpu_pct()
        _uptime = time.monotonic() - loop_start
        _p_level = persona.get("level", 1) if persona else 1
        _p_mood = persona.get("mood", "neutral") if persona else "neutral"
        _p_xp = persona.get("xp", 0) if persona else 0
        _goal_snip = goal if goal else ""
        _mem_chars = 0
        try:
            _mem_chars = config.plan_path.stat().st_size
        except OSError:
            pass
        _obs_count = 0
        try:
            with open(config.observations_path) as _f:
                _obs_count = sum(1 for _ in _f)
        except OSError:
            pass

        _telem_kw = dict(
            tick=tick_number, level=_p_level, mood=_p_mood, xp=_p_xp,
            consecutive_failures=consecutive_failures,
            current_max_tokens=current_max_tokens,
            disk_free_gb=_disk_free, ram_pct=_ram_pct,
            cpu_pct=_cpu_pct, llm_elapsed_s=llm_elapsed,
            tool_name=tick_tool_name, tool_success=tick_tool_success,
            uptime_s=_uptime,
            gate_wait_s=round(gate_wait_s, 2), gate_reason=gate_reason,
        )
        write_heartbeat(config, goal_snippet=_goal_snip,
                        idle_since=idle_since, **_telem_kw)
        append_metrics(config, ctx_chars=ctx_chars, memory_chars=_mem_chars,
                       obs_count=_obs_count, tool_duration_s=tick_tool_duration,
                       compacted=tick_compacted, **_telem_kw)

        # --- Goal-tension: did THIS tick make real progress? A new fact learned (knowledge count
        #     rises only on novel facts, thanks to near-dup dedup), a new skill, or a Boss exchange.
        #     Re-probing and re-confirming known facts do NOT count → tension climbs. ---
        try:
            import knowledge as _kn
            _kc = _kn.count_entries(config)
        except Exception:  # noqa: BLE001
            _kc = prev_knowledge_count
        _sc = _count_skills(config)
        _ac = _count_artifacts(config)
        # Progress = a durable EXTERNAL change this tick: genuinely new knowledge, a new skill, a new
        # workspace file authored, or (below) a settled commission/objective. Re-asking Boss the same
        # question or re-writing an existing file does NOT count — so tension keeps climbing until the
        # creature actually changes something. Widened 2026-07-13: the old signal saw only knowledge/
        # skill counts, so ordinary work (building files, organizing a workspace) was invisible and
        # every such objective stalled to a frustrated death; a new file is real progress.
        # STRONG progress = new knowledge or a new skill (a durable, controllability-proving change);
        # WEAK = a new workspace file only. Both relieve frustration, but only STRONG refutes a block
        # and resets the exposure budget — so a despairing diary file can't masquerade as controlling
        # an impossible goal (the doom-loop fix, objectives.EXPOSURE_CAP).
        _made_progress_strong = (_kc > prev_knowledge_count or _sc > prev_skill_count)
        _made_progress = _made_progress_strong or (_ac > prev_artifact_count)
        if _made_progress:
            last_progress_tick = tick_number
        prev_knowledge_count, prev_skill_count, prev_artifact_count = _kc, _sc, _ac

        # --- Strain glue (Insula/ACC, phase 6): record this tick's TYPED outcome, then compute a
        #     frustration bump from chronic / repeated-signature failure. Feeding it to the gate is
        #     the mechanical teeth — a repeated dead end parks and rotates FASTER, instead of the old
        #     advisory "you seem stuck" prose the model ignored. ---
        _strain_bump = 0
        _act_sig = (recent_hashes[-1] if recent_hashes else tick_tool_name)
        try:
            import glue as _glue
            _fail_sig = "" if tick_tool_success else _act_sig
            _glue.record_outcome(config, success=tick_tool_success,
                                 fail_kind=tick_fail_kind, signature=str(_fail_sig),
                                 tool=tick_tool_name)
            _outcomes = _glue.recent_outcomes(config)
            _strain_bump = _glue.gate_frustration_bump(_outcomes)
            # Rumination teeth: a window dominated by thought-only ticks burns patience too —
            # analysis-paralysis parks/rotates just like a repeated dead end does.
            _rum_bump = _glue.rumination_bump(_outcomes)
            if _rum_bump:
                print(f"{pfx} Glue: ruminating ({_glue.rumination_streak(_outcomes)} thought ticks "
                      f"in the last {_glue.RUMINATE_WINDOW}) — frustration +{_rum_bump}")
            _strain_bump += _rum_bump
            # Motif brake (content-aware): a loop that journals ITS one theme through action tools
            # evades the thought-only counter above. Scan recent thoughts + the open notebook for a
            # dominant content-token pair and nudge rotation off the fixation.
            try:
                from memory import read_recent_thoughts as _rrt
                _bodies = [t.get("text", "") for t in _rrt(config, _glue.MOTIF_WINDOW)]
                import notes as _notes
                _an = _notes.get_active(config)
                if _an:
                    _bodies += _notes._recent_lines(config, _an, _glue.MOTIF_WINDOW)
                _motif = _glue.motif_bump(_bodies)
                if _motif:
                    print(f"{pfx} Glue: circling one theme (motif dominance "
                          f"{_glue.motif_dominance(_bodies):.2f}) — frustration +{_motif}")
                _strain_bump += _motif
            except Exception:  # noqa: BLE001
                pass
        except Exception as _ge:  # noqa: BLE001 - glue is best-effort
            logger.warning("strain glue failed: %s", _ge)

        # --- Pillars 5.5 (dark): the post-adjudication wiring — settle bets (2.3) + predictions
        #     (4.1) against the outcome glue just recorded, feed learning progress (4.2), encode
        #     the experience through the manager (2.2), ingest news (4.4), adjudicate the active
        #     quest (5.1), feed tier standing + level candidacy (4.3). Every subsystem is guarded
        #     inside; this outer guard is the I5 backstop. No-op with flags off (pillars=None). ---
        if pillars is not None:
            try:
                pillars.after_outcome(
                    tick=tick_number, tool=tick_tool_name,
                    args=(call.args if call else None), success=tick_tool_success,
                    fail_kind=tick_fail_kind, situation=tick_situation,
                    summary=tick_summary, event_text=tick_output, persona=persona)
            except Exception as _pae:  # noqa: BLE001 - wiring faults never break the tick (I5)
                logger.warning("pillars post-tick wiring failed: %s", _pae)

        # --- Episodic memory (phase 7b): file this acting tick as a typed (situation→action→
        #     outcome→fix) episode, so a future tick in the SAME situation recalls it BEFORE acting
        #     ("this is like last time"). The action signature is the loop detector's normalized sig
        #     (bash v3/v4/v5 collapse to one), so repeated-approach failures aggregate correctly.
        #     LEGACY PATH — runs only while the engram manager is OFF: with
        #     pillars_memory_manager_enabled the hub's after_outcome encodes this same tick as an
        #     engram (backtick-quoted tool, outcome, fail kind, summary, situation), and
        #     double-writing two memory systems breeds divergence, not redundancy. ---
        if not getattr(config, "pillars_memory_manager_enabled", False):
            try:
                import episodes as _ep
                _ep.record_episode(config, tick=tick_number, tool=tick_tool_name, sig=str(_act_sig),
                                   fail_kind=tick_fail_kind, success=tick_tool_success,
                                   summary=tick_summary, key=tick_situation or None)
            except Exception as _ee:  # noqa: BLE001 - episodic recording is best-effort
                logger.warning("episode record failed: %s", _ee)

        # --- Reflex promotion scan (WISDOM_PLAN §1): with this tick's episode now in the ledger,
        #     scan for a (situation, action) run that has reached the promotion threshold and write
        #     a PROPOSED reflex (armed only when wisdom_reflex_auto_arm). WIS1: the scanner reads the
        #     adjudicated episodic ledger and EXCLUDES automated rows, so a reflex can't self-promote.
        #     Flag-dark (WIS7): no-op unless wisdom_reflexes_enabled. ---
        if getattr(config, "wisdom_reflexes_enabled", False):
            try:
                import reflexes as _rfx
                promoted = _rfx.scan_promotions(
                    config,
                    promote_at=int(getattr(config, "wisdom_reflex_promote_successes", 5) or 5),
                    auto_arm=bool(getattr(config, "wisdom_reflex_auto_arm", False)))
                if promoted:
                    _armed = getattr(config, "wisdom_reflex_auto_arm", False)
                    append_observation(config, {
                        "tick": tick_number, "tool": "reflex", "success": True,
                        "output": (f"[REFLEX] {len(promoted)} reflex(es) "
                                   f"{'armed' if _armed else 'proposed'} from a clean success "
                                   f"streak: {', '.join(promoted[:4])}"
                                   f"{'' if _armed else ' — awaiting operator arm'}"),
                    })
            except Exception as _re:  # noqa: BLE001 - promotion is best-effort; never break the tick
                logger.warning("reflex promotion scan failed: %s", _re)

        # --- Action Gate: update the active objective's frustration from this tick's outcome (+ strain
        #     bump), and ROTATE focus deterministically if it has stalled/parked/finished. This is the
        #     structural anti-rabbit-hole: the harness moves focus, the model doesn't keep grinding. ---
        _gate = {}
        _park_at = None
        _obj = None   # pre-bound: the post-tick ctx below references it even if this gate fails
        try:
            import objectives as _obj
            # DMN temperament feeds the gate's park threshold: a persistent creature grinds a little
            # longer before the gate rotates it, a deferential one lets go sooner. None => default.
            _park_at = (nervous_temperament.park_threshold(_obj.FRUST_PARK)
                        if nervous_temperament is not None else None)
            _gate = _obj.record_tick(config, made_progress=_made_progress,
                                     tool_failed=(not tick_tool_success), tick_number=tick_number,
                                     extra_frustration=_strain_bump, park_threshold=_park_at,
                                     progress_strong=_made_progress_strong)
            if _gate.get("rotated") and _gate.get("active"):
                print(f"{pfx} Gate: rotated focus → {_gate['active']['title']}")
            if _gate.get("escalate"):
                print(f"{pfx} Gate: whole backlog blocked — surfacing to Boss once")
            # BELIEF REFUTED: a goal the creature had declared blocked ("I can't do this") just
            # made progress — the "confident-wrong is gold" case. Fire a near-maximal surprise so
            # curiosity encodes the correction strongly (it competes with the stale false belief),
            # and record it as knowledge so avoidance can never quietly re-form the same wall.
            _ref = _gate.get("refuted_block")
            if _ref:
                print(f"{pfx} Belief refuted: '{_ref['title']}' was NOT blocked after all.")
                if nervous_curiosity is not None:
                    try:
                        from nervous.worldmodel import SURPRISE_MAX as _SMAX
                        nervous_curiosity.observe(_SMAX)     # maximal novelty → strong encoding
                    except Exception:  # noqa: BLE001
                        pass
                try:
                    import knowledge as _kn
                    _kn.store_entry(
                        config,
                        f"REFUTED a block on '{_ref['title']}': I had believed \"{_ref['reason']}\" "
                        f"but progress proved it wrong. When something seems impossible, TEST it "
                        f"before concluding — a wall I avoid is not a wall I verified.",
                        tags=["refuted", "belief", "self-diagnosis", "exposure"],
                        category="reflections", confidence="verified",
                        source_tick=tick_number)
                except Exception:  # noqa: BLE001
                    pass
            # RELEASED: the gate let a futile self-goal die (exposure budget spent, never controllable).
            # Write the OBITUARY as a strong verified memory so recall surfaces the SETTLEMENT — "it's a
            # fact, not a task, done with me" — instead of only the struggle engrams (the memory-loop
            # cure: without this, high-arousal brooding keeps winning recall and re-seeds the fixation).
            # The felt RELIEF is the tension floor lifting as the backlog empties — never a paid event.
            _died = _gate.get("died")
            if _died:
                print(f"{pfx} Released '{_died['title']}': {_died['reason']}")
                try:
                    import knowledge as _kn
                    _kn.store_entry(
                        config,
                        f"RELEASED '{_died['title']}': {_died['reason']}. It is a settled fact, not a "
                        f"task — it needs nothing more from me. Letting go was right; my time is my own.",
                        tags=["released", "closure", "self-diagnosis", "let-go"],
                        category="reflections", confidence="verified",
                        source_tick=tick_number)
                except Exception:  # noqa: BLE001
                    pass
                # SOTA#3: this derailment → an "avoid this" guardrail, so the recall cascade warns
                # the creature BEFORE it re-attempts the same doom-loop goal shape (the whole point:
                # convert each derailment into a retrieved guardrail). Failure → born strong (a scar).
                if pillars is not None:
                    pillars._distill_strategy({
                        "title": _died["title"], "outcome": "released", "success": False,
                        "reason": _died["reason"], "situation": tick_situation or "",
                        "trajectory": tick_summary,
                    }, tick=tick_number)
        except Exception as _e:  # noqa: BLE001
            logger.warning("objective gate failed: %s", _e)

        # --- Autonomy KPI (SOTA#9): the coherent-goal-pursuit HORIZON. Count consecutive on-track
        #     acting ticks; when the creature DERAILS (a detected loop, a forced rotation/park, a gate
        #     death, or a whole-backlog escalation) record the horizon that just ended and reset. The
        #     distribution over these samples is the yardstick for "persists toward a goal without
        #     derailing" — so any future anti-derailment change can actually be judged. ---
        _derail = ("loop" if loop_detected else
                   "goal_died" if _gate.get("died") else
                   "forced_rotation" if _gate.get("rotated") else
                   "parked" if _gate.get("parked") else
                   "backlog_blocked" if _gate.get("escalate") else None)
        if _derail:
            try:
                record_goal_horizon(config, goal_horizon, _derail, tick_number)
            except Exception:  # noqa: BLE001 - a KPI write must never break the tick
                pass
            goal_horizon = 0
        else:
            goal_horizon += 1

        # --- Temperament (DMN): drift the slow personality setpoints from this tick. A forced park is
        #     an "override" of the model's choice to keep going (the strongest teacher); progress is
        #     autonomy paying off; a failure teaches caution. Slow — one tick barely moves it. ---
        if nervous_temperament is not None:
            try:
                # A forced PARK is an override (persistence didn't pay → the temperament learns). A
                # gate DEATH is not: it's the evidence-complete release of a proven-futile goal, not a
                # live choice overruled — counting it would drift persistence down on every impossible
                # goal and breed a quitter. (Death already keeps parked=False; this is the explicit guard.)
                nervous_temperament.observe(success=_made_progress,
                                            failed=(not tick_tool_success),
                                            overridden=bool(_gate.get("parked")) and not _gate.get("died"))
            except Exception as _te:  # noqa: BLE001
                logger.warning("temperament update failed: %s", _te)

        # --- Post-tick organ phase (1.1 — the run_post_tick seam, closed): the goal-tension and
        #     curiosity hooks the registry holds ARE the old inline blocks; the loop packs their
        #     inputs into one ctx and dispatches ONCE through the registry (guarded per-hook, I5).
        #     Goal-tension fires before curiosity (registration order = the old call order), and
        #     curiosity writes ctx.intrinsic for the (non-migrated) learner to consume below —
        #     the value flows exactly as it did inline. ---
        _post_ctx = types.SimpleNamespace(
            gate=_gate, park_at=_park_at, obj=_obj, temperament=nervous_temperament,
            goaltension=nervous_goaltension, made_progress=_made_progress,
            worldmodel=nervous_worldmodel, wm_prev_sit=_wm_prev_sit, wm_prev_act=_wm_prev_act,
            tick_situation=tick_situation, curiosity=nervous_curiosity, intrinsic=0.0,
            # An open commission task is an open commitment (COMMISSION_PLAN.md): the hub memoizes
            # the live count at its settle beat so the drive reads a fact, not a file, every tick.
            commission_open=bool(pillars is not None
                                 and getattr(pillars, "_commission_open", False)))
        if organ_registry is not None:
            organ_registry.run_post_tick(_post_ctx)

        # --- Reward learning (the dopaminergic keystone): learn from THIS tick's outcome — compute the
        #     reward (success + real progress + how the felt body changed − strain), update the value
        #     cache via reward-prediction-error, log the experience, and fire dopamine. The learner reads
        #     the felt body + mood itself. Sleep replays the tagged experiences into lessons. Guarded. ---
        if nervous_learner is not None:
            try:
                # world-model + curiosity ran in the post-tick organ phase above (the curiosity hook:
                # LEARNING PROGRESS, not raw surprise, so chaos/noise can't drive it — Loop A); it
                # wrote the intrinsic-reward bonus onto the ctx for this learner to consume, exactly
                # the value the old inline block computed here. (Pre-pivot this also FED the
                # metabolism; post-pivot food = literal battery power — only curiosity pays.)
                _intrinsic = float(getattr(_post_ctx, "intrinsic", 0.0) or 0.0)
                _act_readable = tick_action_label or tick_tool_name or str(_act_sig)
                # A structurally-riskless tick (a bare thought, a chat reply) has no outcome in doubt,
                # so its success channel pays nothing — otherwise the free +0.40 trains the creature to
                # narrate instead of act (reward.CANT_FAIL_ACTIONS). tick_tool_name is the reliable
                # discriminator (the richer _act_readable label is for the value key, not the gate).
                from nervous.reward import CANT_FAIL_ACTIONS as _CANT_FAIL
                _can_fail = tick_tool_name not in _CANT_FAIL
                # Result hash for verb-agnostic novelty gating: a re-read/re-cat/re-skill/re-write that
                # returns identical output taught/changed nothing, so its success channel pays 0.
                # Normalize volatile scalars (job PIDs in the async dispatch ack, timestamps, $RANDOM,
                # byte counts) first — otherwise `date`/`uptime`/ANY async bash looks novel every tick
                # and books a free +0.40 that can crystallize into a "when idle, run <probe>" habit.
                from nervous.reward import normalize_result as _norm_result
                _result_sig = (hashlib.md5(
                    _norm_result(tick_output).encode("utf-8", "ignore")).hexdigest()
                    if tick_output else None)
                nervous_learner.observe(situation=tick_situation, action=_act_readable,
                                        success=tick_tool_success, made_progress=_made_progress,
                                        strain=_strain_bump, intrinsic=_intrinsic, tick=tick_number,
                                        can_fail=_can_fail, result_sig=_result_sig)
                _wm_prev_sit, _wm_prev_act = tick_situation, _act_readable
            except Exception as _le:  # noqa: BLE001 - learning must never break the tick
                logger.warning("reward learning failed: %s", _le)

        # --- Metabolism (M0): spend this tick's energy, and take in power. Thinking is the dearest act;
        #     a world-touching action costs more. Food = literal battery power (pivot): a PLANT (this
        #     node) recharges from solar `charge_in`; an ANIMAL would recharge by resting/docking. When
        #     arousal collapses to torpor the body is dormant (pays only basal). Low energy feeds back
        #     into neuromod as tiredness, dragging arousal toward sleep before empty (hibernation, not
        #     death). Solar is the interim placeholder until the Renogy BLE reader supplies real PV. ---
        if nervous_metabolism is not None:
            try:
                _arousal = float(getattr(nervous_neuromod, "arousal", 0.3) or 0.3)
                _resting = _arousal <= getattr(config, "nervous_metabolism_rest_arousal", 0.2)
                _acted = tick_tool_name not in ("", "thought", "parse_error")
                # Real power wins: when the MPPT reader has a FRESH reading it re-anchors the reserve to
                # true SOC (in the monitor thread), so we don't add the fake solar curve — only the
                # per-tick cognition/action drift since the last anchor. When power is stale/absent (e.g.
                # Dean's app holds the BLE link), fall back to the solar placeholder so a plant still has
                # a plausible diurnal rhythm.
                _charge_in = 0.0
                _power_fresh = nervous_power is not None and nervous_power.is_fresh()
                if (not _power_fresh and nervous_metabolism.archetype == "plant"
                        and getattr(config, "nervous_metabolism_solar_enabled", True)):
                    from nervous.metabolism import solar_charge_in
                    _hour = time.localtime().tm_hour + time.localtime().tm_min / 60.0
                    _charge_in = solar_charge_in(
                        _hour,
                        peak=getattr(config, "nervous_metabolism_solar_peak", 0.03),
                        sunrise=getattr(config, "nervous_metabolism_solar_sunrise_h", 6.0),
                        sunset=getattr(config, "nervous_metabolism_solar_sunset_h", 20.0))
                nervous_metabolism.metabolize(thought=True, acted=_acted,
                                              resting=_resting, charge_in=_charge_in)
                if nervous_neuromod is not None:
                    nervous_neuromod.observe_energy(nervous_metabolism.energy)
            except Exception as _me:  # noqa: BLE001 - metabolism must never break the tick
                logger.warning("metabolism failed: %s", _me)

        # --- Causal ledger (Pillars 0.3): with every organ updated, append ONE record of the full
        #     pressure field for THIS tick — the sole collection site. It is the last thing the tick
        #     does before the counter advances, so tick_number still names the tick that produced the
        #     field. Ships dark: a strict no-op when pressure_ledger is None (flag off). Guarded — a
        #     ledger fault must never wound the tick (I5). Signal→source map lives in pressures.py. ---
        if pressure_ledger is not None:
            try:
                import pressures as _pressures
                _xp_delta = (persona.get("xp", 0) if persona else 0) - _xp_at_tick_start
                # Derive the payout's source from the tick's typed events (glue judges, §0.5): a
                # dreamed compaction, a climbed-out-of-failure recovery, or an ordinary tool success.
                if _xp_delta <= 0:
                    _xp_src = ""
                elif tick_compacted:
                    _xp_src = "compaction"
                elif not tick_tool_success:
                    _xp_src = "error_recovery"
                else:
                    _xp_src = tick_tool_name or "tool"
                # The condition label + strain are pure functions of this tick's outcome window,
                # already gathered above; recompute defensively from _outcomes (empty if the strain
                # glue block failed to run this tick).
                _cond, _strain_val = "", 0
                try:
                    import glue as _glue_mod
                    _win = _outcomes  # noqa: F821 - set in the strain block above
                    _cond = _glue_mod.compute_condition(_win)
                    _strain_val = _glue_mod.compute_strain(_win)
                except Exception:  # noqa: BLE001 - condition is decorative here, never fatal
                    pass
                _field = _pressures.collect_field(
                    tick=tick_number,
                    neuromod=nervous_neuromod, goaltension=nervous_goaltension,
                    curiosity=nervous_curiosity, metabolism=nervous_metabolism,
                    active_objective=(_gate.get("active") if isinstance(_gate, dict) else None),
                    condition=_cond, strain=_strain_val,
                    admitted_events=_tick_admitted_events,
                    xp_delta=_xp_delta, xp_source=_xp_src,
                )
                pressure_ledger.append(_field)
            except Exception as _pe:  # noqa: BLE001 - the ledger must never break the tick (I5)
                logger.warning("causal ledger append failed: %s", _pe)

        # --- Sleep ---
        tick_number += 1
        ticks_since_compaction += 1

        # --- Persist tick state to WAL ---
        write_wal(config, tick_number, ticks_since_compaction,
                  goal_start_time, consecutive_failures,
                  reasoning_exhaustions, current_max_tokens,
                  last_progress_tick)

        if not _shutdown_requested:
            interval = _adaptive_tick_interval(config, tick_tool_name)
            write_activity(config, "sleeping", detail=f"next tick in {interval:.1f}s")
            _interruptible_sleep(config, interval)

    # --- Shutdown ---
    clear_wal(config)  # clean exit — no stale WAL
    if persona and config.persona_enabled:
        persona["uptime_total_s"] = persona.get("uptime_total_s", 0) + int(time.monotonic() - loop_start)
        compute_traits(persona)
        check_titles(persona)
        save_persona(config.workspace, persona)
        pfx = _pfx(persona, config)
        print(f"{pfx} Shutting down. See you next time.")
    else:
        print("[eidos] Shutting down...")
    append_observation(config, {
        "tick": tick_number,
        "tool": "system",
        "success": True,
        "output": "eiDOS shutting down cleanly.",
    })


if __name__ == "__main__":
    # Add project root to path so imports work
    sys.path.insert(0, str(Path(__file__).parent))
    main()
