#!/usr/bin/env python3
"""eiDOS — the always-on house AI (Windows host gamingPC).

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
from telemetry import write_heartbeat, append_metrics, write_activity, get_cpu_pct
from tools import execute_tool, refresh_jobs, collect_finished_jobs, reap_jobs

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
    """Append a chat reply to chat_replies.jsonl."""
    path = config.workspace / "chat_replies.jsonl"
    entry = json.dumps({
        "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "tick": tick_number,
        "text": reply_text[:2000],
    })
    with open(path, "a") as f:
        f.write(entry + "\n")


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
        port = getattr(config, "dashboard_port", 8099)
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


def _extract_thought(response: str) -> str:
    """This tick's reasoning — the model's raw output minus the action/reply tags."""
    if not response:
        return ""
    return _THOUGHT_TAG_RE.sub("", response).strip()


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
    # Objective backlog (Ventral Striatum / Action Gate): seed the open commitments once, then the
    # gate rotates focus among them each tick so a stalled task never starves the rest of the system.
    try:
        import objectives as _obj
        _obj.ensure_seeded(config, tick_number)
    except Exception as _e:  # noqa: BLE001
        logger.warning("objective seed failed: %s", _e)

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
            if _emb.model_available(config) and _emb.load_model(config):
                _nsync = _emb.sync_knowledge_vectors(config)
                print(f"{pfx} Embedding model loaded; synced {_nsync} knowledge vector(s)")
            else:
                logger.info("embedding enabled but model unavailable — recall stays lexical-only")
        except Exception as _emb_e:  # noqa: BLE001 - semantic recall is additive, never blocks boot
            logger.warning("embedding init failed: %s", _emb_e)

    # --- Wait for LLM health before entering tick loop (cold-boot safety) ---
    # Skipped in the isolated test env (EIDOS_NO_DASHBOARD): tests mock `complete`, so probing a
    # real LLM endpoint only adds multi-second urlopen timeouts (a port may be up but lack /health).
    if not config.mock_mode and not os.environ.get("EIDOS_NO_DASHBOARD"):
        import urllib.request as _ur
        health_url = config.llm_url.rstrip("/") + "/health"
        print(f"{pfx} Waiting for LLM server health at {health_url}")
        _health_wait = 0
        while not _shutdown_requested:
            try:
                with _ur.urlopen(health_url, timeout=5) as _resp:
                    _data = json.loads(_resp.read().decode())
                    if _data.get("status") == "ok":
                        print(f"{pfx} LLM server healthy (waited {_health_wait}s)")
                        break
            except Exception:
                pass
            _health_wait += 5
            if _health_wait % 60 == 0:
                print(f"{pfx} Still waiting for LLM server... ({_health_wait}s)")
            time.sleep(5)

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
            held_s = int(time.time() - listening_since)
            write_activity(config, "listening", detail=f"listening to Dean ({held_s}s)")
            # Event-driven: wakes the instant the hold releases (blur/send) or chat arrives;
            # the short bound keeps the "listening Ns" display fresh and re-applies TTL rules.
            _control_wait_change(config, max_s=5.0)
            continue
        elif listening_since is not None:
            logger.info("Listening hold released — resuming autonomous loop")
            listening_since = None

        # --- Check for goal ---
        goal = read_goal(config)
        if not goal:
            if idle_since is None:
                idle_since = time.time()
            if config.mock_mode:
                print("[eidos] No goal.md — exiting (mock mode)")
                break
            _interruptible_sleep(config)
            continue
        else:
            idle_since = None

        # --- Goal change detection (hash tracking only) ---
        import hashlib
        goal_hash = hashlib.md5(goal.encode()).hexdigest()
        goal_changed = last_goal_hash is not None and goal_hash != last_goal_hash
        if goal_changed:
            goal_start_time = time.time()
        last_goal_hash = goal_hash

        # --- Compaction check ---
        tick_compacted = False
        if should_compact(config, ticks_since_compaction):
            print(f"{pfx} Dreaming... consolidating memories.")
            write_activity(config, "dreaming", detail="consolidating memories")
            try:
                compact_briefing(config, persona=persona)
                emit_flavor(config, persona)
                ticks_since_compaction = 0
                tick_compacted = True
                if persona and config.persona_enabled:
                    record_compaction(persona)
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

        # --- Assemble context ---
        # `tension` is now the ACTIVE objective's frustration (the gate's per-objective counter),
        # falling back to the legacy global stall count if the backlog isn't available.
        try:
            import objectives as _obj
            _act = _obj.get_active(config)
            tension = int(_act["frustration"]) if _act else max(0, tick_number - last_progress_tick)
        except Exception:  # noqa: BLE001
            tension = max(0, tick_number - last_progress_tick)
        messages = assemble_context(
            config,
            tick_number=tick_number,
            goal_start_time=goal_start_time,
            loop_detected=loop_detected,
            repeat_count=repeat_count,
            tension=tension,
        )

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
        tick_summary = ""
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
        yield_to_speech(config)

        tick_grammar = None
        if getattr(config, "llm_grammar_enabled", True) and not config.mock_mode:
            try:
                from grammar import tick_grammar_cached
                from tools import TOOLS as _live_tools
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
                        record_compaction(persona)
                        pfx = _pfx(persona, config)
                except LLMError as ce:
                    logger.error("Forced compaction failed: %s", ce)

            write_wal(config, tick_number, ticks_since_compaction,
                      goal_start_time, consecutive_failures,
                      reasoning_exhaustions, current_max_tokens,
                      last_progress_tick)
            # Interruptible (ARCH #1): during a failure storm a Boss message still wakes the loop.
            _interruptible_sleep(config)
            tick_number += 1
            ticks_since_compaction += 1
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

            write_wal(config, tick_number, ticks_since_compaction,
                      goal_start_time, consecutive_failures,
                      reasoning_exhaustions, current_max_tokens,
                      last_progress_tick)
            # Interruptible (ARCH #1): during a failure storm a Boss message still wakes the loop.
            _interruptible_sleep(config)
            tick_number += 1
            ticks_since_compaction += 1
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
        if thought:
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
                    record_tick(persona, "chat_reply", True)
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
                    record_tick(persona, "thought", True)
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
                    record_tick(persona, None, False)
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
            tick_summary = (result.output or "")[:160].replace("\n", " ")

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
                record_tick(persona, call.tool, result.success)
                if result.success and last_tick_failed:
                    record_error_recovery(persona)
                last_tick_failed = not result.success
                pfx = _pfx(persona, config)

            # --- Loop detection hash (NORMALIZED) ---
            # Hash bash on the normalized command so v3/v4/v5 variations of the SAME command collapse
            # to one signature — exact-match on full args missed the real rumination at tick 969.
            if call.tool == "bash" and isinstance(call.args, dict):
                _cmd = call.args.get("cmd") or call.args.get("command") or ""
                call_hash = hashlib.md5(("bash:" + _norm_cmd(_cmd)).encode()).hexdigest()
            else:
                call_hash = hashlib.md5(
                    json.dumps({"tool": call.tool, "args": call.args}, sort_keys=True).encode()
                ).hexdigest()
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
        # Progress = genuinely NEW knowledge or a NEW skill only. Re-asking Boss the same question or
        # prepping a blocked task does NOT count — so tension keeps climbing until it actually pivots.
        _made_progress = (_kc > prev_knowledge_count or _sc > prev_skill_count)
        if _made_progress:
            last_progress_tick = tick_number
        prev_knowledge_count, prev_skill_count = _kc, _sc

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
                                 fail_kind=tick_fail_kind, signature=str(_fail_sig))
            _strain_bump = _glue.gate_frustration_bump(_glue.recent_outcomes(config))
        except Exception as _ge:  # noqa: BLE001 - glue is best-effort
            logger.warning("strain glue failed: %s", _ge)

        # --- Episodic memory (phase 7b): file this acting tick as a typed (situation→action→
        #     outcome→fix) episode, so a future tick in the SAME situation recalls it BEFORE acting
        #     ("this is like last time"). The action signature is the loop detector's normalized sig
        #     (bash v3/v4/v5 collapse to one), so repeated-approach failures aggregate correctly. ---
        try:
            import episodes as _ep
            _ep.record_episode(config, tick=tick_number, tool=tick_tool_name, sig=str(_act_sig),
                               fail_kind=tick_fail_kind, success=tick_tool_success,
                               summary=tick_summary, key=tick_situation or None)
        except Exception as _ee:  # noqa: BLE001 - episodic recording is best-effort
            logger.warning("episode record failed: %s", _ee)

        # --- Action Gate: update the active objective's frustration from this tick's outcome (+ strain
        #     bump), and ROTATE focus deterministically if it has stalled/parked/finished. This is the
        #     structural anti-rabbit-hole: the harness moves focus, the model doesn't keep grinding. ---
        try:
            import objectives as _obj
            _gate = _obj.record_tick(config, made_progress=_made_progress,
                                     tool_failed=(not tick_tool_success), tick_number=tick_number,
                                     extra_frustration=_strain_bump)
            if _gate.get("rotated") and _gate.get("active"):
                print(f"{pfx} Gate: rotated focus → {_gate['active']['title']}")
            if _gate.get("escalate"):
                print(f"{pfx} Gate: whole backlog blocked — surfacing to Boss once")
        except Exception as _e:  # noqa: BLE001
            logger.warning("objective gate failed: %s", _e)

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
