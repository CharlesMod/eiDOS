"""Context assembly — builds the messages list for each tick.

One assembly path (the briefing structure): compressed standing orders, the durable
state blob (focus/backlog/self-guide/mission/plan/world-model/notebook/presence/chat),
the recent history thread as real turns, a salience block, then the tick prompt.

Includes per-section budget enforcement, truncation, and overrun logging
so we can tune limits based on real usage without blowing the context window.
"""

import json
import logging
import re
import time
from collections import Counter
from pathlib import Path

from config import Config
from memory import read_goal, read_plan, read_recent_observations, read_interventions, read_recent_thoughts, read_self_guide
from env_snapshot import generate as generate_env_snapshot
from env_snapshot import generate_alerts as generate_env_alerts
from prompts import SYSTEM_PROMPT_BRIEFING, SYSTEM_PROMPT_CREATURE, TICK_PROMPT, TICK_PROMPT_LOOP_DETECTED

logger = logging.getLogger("eidos.context")


# ---------------------------------------------------------------------------
# Chat reply reader
# ---------------------------------------------------------------------------

def _read_recent_replies(config: Config, n: int = 3) -> list:
    """Read the last *n* entries from chat_replies.jsonl."""
    path = config.workspace / "chat_replies.jsonl"
    try:
        lines = path.read_text().strip().splitlines()
        result = []
        for line in lines[-n:]:
            try:
                result.append(json.loads(line))
            except (json.JSONDecodeError, ValueError):
                continue
        return result
    except (FileNotFoundError, OSError):
        return []


# ---------------------------------------------------------------------------
# Token / char helpers
# ---------------------------------------------------------------------------

def estimate_tokens(text: str, chars_per_token: float = 3.5) -> int:
    """Rough token estimate from character count."""
    return int(len(text) / chars_per_token)


def _truncate(text: str, max_chars: int, label: str) -> str:
    """Truncate text to max_chars, appending a notice if trimmed."""
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + f"\n... [{label} truncated — {len(text)} chars exceeded {max_chars} budget]"


# ---------------------------------------------------------------------------
# Overrun logging
# ---------------------------------------------------------------------------

def _log_overrun(config: Config, tick_number: int, section: str,
                 actual_chars: int, budget_chars: int) -> None:
    """Append a record to workspace/ctx_overruns.jsonl for post-hoc analysis."""
    entry = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "tick": tick_number,
        "section": section,
        "actual_chars": actual_chars,
        "budget_chars": budget_chars,
        "overage_chars": actual_chars - budget_chars,
        "est_tokens_actual": estimate_tokens(str(actual_chars), config.chars_per_token),
    }
    logger.warning("ctx overrun tick=%s section=%s actual=%d budget=%d",
                   tick_number, section, actual_chars, budget_chars)
    try:
        path = config.workspace / "ctx_overruns.jsonl"
        with open(path, "a") as f:
            f.write(json.dumps(entry) + "\n")
    except OSError:
        pass  # best-effort


# ---------------------------------------------------------------------------
# Inverted-pyramid observation rendering (briefing model)
# ---------------------------------------------------------------------------

def _norm_cmd(s: str) -> str:
    """Collapse cosmetic differences (version suffixes, IP octets, ports, quoting/interp
    punctuation, whitespace) so near-identical command retries share ONE signature. This is what
    catches the `port_probe → v3 → v4 → v5 …` spiral the exact-match signature missed."""
    s = s.lower()
    s = re.sub(r"[\"'`(){}$:]", "", s)   # strip quoting / interpolation punctuation
    s = re.sub(r"\d+", "#", s)            # collapse all numbers (ip octets, ports, version tags)
    s = re.sub(r"\s+", " ", s).strip()
    return s[:120]


def _obs_sig(o: dict):
    """Signature for detecting repeated actions. For bash we normalize the command so tiny
    variations (different ports/versions/quoting) collapse to the same signature."""
    tool = o.get("tool")
    a = o.get("args") or {}
    if tool == "bash" and isinstance(a, dict):
        return (tool, _norm_cmd(a.get("cmd") or a.get("command") or ""))
    if a:
        return (tool, str(a))
    return (tool, (o.get("output", "") or "")[:80])


def _build_relevant_recall(config: Config, exclude_ids: set) -> str:
    """A SMALL BM25 slice keyed on the CURRENT STEP (subtask / next plan line), not the static goal.

    The old intelligence section queried BM25 with the generic goal text, so it returned bootstrap
    seeds every tick and never step-relevant facts — a root cause of the amnesia. Keyed on the current
    step and de-duplicated against the world-model panel, this surfaces only genuinely relevant priors.
    """
    if not config.knowledge_enabled:
        return ""
    try:
        from knowledge import search_bm25, format_recalled
    except ImportError:
        return ""
    step = ""
    for line in (read_plan(config) or "").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            step = line
            break
    if not step:
        return ""
    # Hybrid retrieval (phase 7a, "BM25+semantic"): lexical BM25 catches exact term overlap,
    # semantic catches the same MEANING in different words. When embeddings are live, fuse the two
    # ranked lists (reciprocal-rank fusion); otherwise this is BM25-only, byte-for-byte as before.
    bm25 = search_bm25(config, step, top_k=config.knowledge_recall_top_k)
    merged = bm25
    if config.knowledge_embedding_enabled:
        try:
            from embedding import semantic_search
            sem = semantic_search(config, step, top_k=config.knowledge_recall_top_k)
            if sem:
                merged = _rrf_blend(bm25, sem, top_k=config.knowledge_recall_top_k)
        except Exception:  # noqa: BLE001 - semantic is additive; never break BM25 recall
            merged = bm25
    results = [r for r in merged if r.get("id") not in exclude_ids]
    if not results:
        return ""
    return format_recalled(results, max_chars=max(300, config.context_intelligence_max_chars // 2))


def _rrf_blend(*ranked_lists, top_k: int, k: int = 60) -> list:
    """Reciprocal-rank fusion: combine ranked result lists by id, score = Σ 1/(k+rank). The standard
    parameter-free way to blend two retrievers without calibrating their score scales against each
    other. First-seen dict wins for the kept payload."""
    score: dict = {}
    keep: dict = {}
    for lst in ranked_lists:
        for rank, r in enumerate(lst):
            rid = r.get("id")
            if not rid:
                continue
            score[rid] = score.get(rid, 0.0) + 1.0 / (k + rank + 1)
            keep.setdefault(rid, r)
    return sorted(keep.values(), key=lambda r: -score[r["id"]])[:top_k]


# ---------------------------------------------------------------------------
# Context assembly
# ---------------------------------------------------------------------------

def assemble_context(
    config: Config,
    tick_number: int,
    goal_start_time: float,
    loop_detected: bool = False,
    repeat_count: int = 0,
    max_ticks: int = 0,
    tension: int = 0,
    afferent_block: str = "",
) -> list[dict]:
    """Assemble the full messages list in fixed order for one tick.

    Returns a list of {"role": ..., "content": ...} dicts ready for llm.complete().
    Each variable section is budget-capped; overruns are logged to ctx_overruns.jsonl.

    Briefing structure: Standing Orders → durable blob → history thread → situation → salience → tick prompt.
    `afferent_block` (V3 nervous system, P3): admitted sensory events for this tick, injected into the
    volatile situation tail — KV-safe (the stable prefix and history turns are untouched).
    """
    return _assemble_briefing(config, tick_number, goal_start_time,
                              loop_detected, repeat_count, max_ticks, tension,
                              afferent_block=afferent_block)


# ---------------------------------------------------------------------------
# Tick prompt (shared)
# ---------------------------------------------------------------------------

def _build_tick_prompt(config, tick_number, goal_start_time, loop_detected,
                       repeat_count, max_ticks, boss_waiting=False, tension=0, boss_text=""):
    """Build the tick prompt message (shared by both modes)."""
    now = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime())
    elapsed = _format_elapsed(time.time() - goal_start_time)
    boss_prefix = ""
    if boss_waiting:
        # A Boss message is a HIGH-PRIORITY COMMAND, not a note to acknowledge. The old prompt funneled
        # everything into a text <reply>; that's why "speak to me" produced typed text. If he wants to be
        # HEARD, the action is a `speak` call — required, not optional.
        bt = (boss_text or "").lower()
        wants_voice = any(k in bt for k in (
            "speak", "say ", "tts", "out loud", "aloud", "hear you", "hear me", "voice", "tell me out"))
        if wants_voice:
            boss_prefix = ("🔊 BOSS WANTS TO HEAR YOU (he said so in chat). THIS TICK you MUST call `speak` "
                           "with what you want to say — a text-only <reply> does NOT satisfy this. You may "
                           "ALSO <reply> the same words, but the spoken call is the point. One sentence. "
                           "Do not narrate that you'll speak, do not test the pipeline — just speak.\n\n")
        else:
            boss_prefix = ("Boss just messaged you (see 'New since last tick') — this is a HIGH-PRIORITY "
                           "COMMAND, not a note to acknowledge. DO what he asks THIS tick: <reply> to him AND "
                           "take any action he requested (speak, run, build), then keep acting on it until "
                           "it's actually done. Do NOT acknowledge and wander off to other work.\n\n")
        # Replying to Boss is itself the circuit-breaker, so don't also stack the loop-detected variant.
        loop_detected = False
    # Park pressure at the decision point: `tension` is now the ACTIVE objective's frustration (0..FRUST_PARK).
    # Near the cap, warn that the gate is about to rotate — pivot or park it yourself NOW.
    elif tension >= 6:
        boss_prefix = (f"⚠ This focus is almost out of patience ({tension}/8): make REAL progress THIS tick "
                       f"or park it (objective_block) and move on — the gate will rotate you otherwise.\n\n")

    urgency_note = ""
    if max_ticks > 0:
        remaining = max_ticks - tick_number
        if remaining <= 0:
            urgency_note = " — FINAL TICK: state your result and mark the objective done (objective_done) now"
        elif remaining <= 2:
            urgency_note = f" — {remaining} tick{'s' if remaining != 1 else ''} remaining: wrap up and call objective_done if ready"

    try:
        import objectives
        _ao = objectives.get_active(config)
    except Exception:  # noqa: BLE001
        _ao = None
    if _ao:
        subtask_line = f"Current focus: {_ao['title']} (because: {_ao['why']})"
    else:
        focus = _current_focus(config)
        subtask_line = f"Current focus: {focus}" if focus else "Focus on your mission directly."

    if loop_detected:
        return boss_prefix + TICK_PROMPT_LOOP_DETECTED.format(
            tick_number=tick_number,
            max_ticks=max_ticks if max_ticks else "?",
            timestamp=now, elapsed=elapsed,
            repeat_count=repeat_count,
            urgency_note=urgency_note,
            subtask_line=subtask_line,
        )
    return boss_prefix + TICK_PROMPT.format(
        tick_number=tick_number,
        max_ticks=max_ticks if max_ticks else "?",
        timestamp=now, elapsed=elapsed,
        urgency_note=urgency_note,
        subtask_line=subtask_line,
    )


def _enforce_ceiling(config, messages, tick_number, n_situation: int = 0):
    """Hard-enforce the total context ceiling, briefing-message-shape aware.

    The briefing shape is: [system, durable, *history_turns, situation?, whats_new?, tick_prompt].
    The system prompt and the trailing decision-point messages (the tick prompt, an optional
    "New since last tick" salience block, and the situation message) are trimmed LAST — that is
    exactly what the model must read to act. We free space by dropping the OLDEST history turns
    first, then trimming the durable blob's tail lines (static identity material), and only if
    STILL over, trimming the situation message's tail lines.

    `n_situation` is 1 when the caller appended a situation message before the whats_new/tick
    tail (briefing mode always does), else 0."""
    total = sum(len(m["content"]) for m in messages)
    if total <= config.context_max_total_chars or len(messages) < 2:
        return total
    _log_overrun(config, tick_number, "TOTAL", total, config.context_max_total_chars)
    budget = config.context_max_total_chars

    # Protected trailing block: the tick prompt (last) + an optional whats_new salience
    # message immediately before it.
    n_tail = 1
    if len(messages) >= 3 and messages[-2]["content"].startswith("## New since last tick"):
        n_tail = 2
    head = messages[0]
    tail = messages[-n_tail:]
    middle = messages[1:-n_tail]                      # durable blob + history turns + situation?
    situation = None
    if n_situation and len(middle) >= 2:
        situation = middle[-1]
        middle = middle[:-1]
    durable = middle[0] if middle else None
    history = middle[1:] if len(middle) > 1 else []

    fixed = len(head["content"]) + sum(len(m["content"]) for m in tail)
    avail = budget - fixed

    def middle_len():
        return ((len(durable["content"]) if durable else 0)
                + (len(situation["content"]) if situation else 0)
                + sum(len(m["content"]) for m in history))

    def _trim_tail_lines(msg, allowed):
        lines = msg["content"].splitlines(keepends=True)
        while lines and len("".join(lines)) > allowed:
            lines.pop()
        return {"role": "user", "content": "".join(lines) + "\n... [context trimmed to fit budget]"}

    # 1) drop oldest history turns first (the blobs are more load-bearing than old turns)
    while history and middle_len() > avail:
        history.pop(0)
    # 2) still over → trim the durable blob's tail lines (static identity material)
    if durable and middle_len() > avail:
        db = max(0, avail - middle_len() + len(durable["content"]))
        durable = _trim_tail_lines(durable, db)
    # 3) still over → trim the situation message last (presence/chat are decision-critical)
    if situation and middle_len() > avail:
        sb = max(0, avail - middle_len() + len(situation["content"]))
        situation = _trim_tail_lines(situation, sb)

    messages[:] = ([head] + ([durable] if durable else []) + history
                   + ([situation] if situation else []) + list(tail))
    return sum(len(m["content"]) for m in messages)


# ---------------------------------------------------------------------------
# Presence + conversation-thread assembly (the model gets its own history as turns)
# ---------------------------------------------------------------------------

def _build_whats_new(config: Config, tick_number: int, interventions: list) -> str:
    """Salience channel (the doc's 'Amygdala / salient deltas'): what CHANGED since last tick,
    placed right at the decision point so the model never talks past Boss or ignores a fresh result.
    Boss messages are consumed after one tick, so elevating them here is what stops 'talked past Boss'."""
    parts = []
    for i in (interventions or []):
        c = (i.get("content") or "").strip()
        if c:
            parts.append(f'🗣 BOSS JUST MESSAGED — reply with <reply>…</reply> THIS tick, before other '
                         f'work:\n   "{c}"')
    try:
        obs = read_recent_observations(config, max_chars=800, max_count=8)
        if any(o.get("tick") == tick_number and o.get("tool") == "async_result" for o in obs):
            parts.append("↩ A background job result just came back (in the thread just above) — pair it "
                         "with what you dispatched and act on it; don't re-run it.")
    except Exception:  # noqa: BLE001
        pass
    if not parts:
        return ""
    return "## New since last tick — handle these FIRST\n" + "\n".join(parts)


def _current_focus(config: Config) -> str:
    """The ONE trustworthy objective line — replaces the four conflicting 'current task' sources
    (drifted auto-subgoals, plan, mission, history). It is the goal's Immediate-focus + the agent's
    own next plan step. Stable and coherent: what am I doing, and what's the next concrete step."""
    focus = ""
    glines = (read_goal(config) or "").splitlines()
    for i, line in enumerate(glines):
        s = line.strip().strip("*").strip()
        if s.lower().startswith("immediate focus"):
            focus = s.split(":", 1)[-1].strip().strip("*_").strip()
            # gather continuation lines (the focus may wrap) until a blank line / new heading
            for cont in glines[i + 1:]:
                c = cont.strip()
                if not c or c.startswith(("#", "_", "**", "Done when")):
                    break
                focus = (focus + " " + c.strip("*_").strip()).strip()
            break
    focus = focus[:300]
    step = _plan_next_step(config)   # capped; this string lands in the never-trimmed tick prompt
    parts = [p for p in (focus, (f"next step: {step}" if step else "")) if p]
    return " — ".join(parts)


def _plan_next_step(config: Config) -> str:
    # Capped: this line is embedded in the (never-trimmed) tick prompt and the focus block,
    # so an over-long plan line must not be able to balloon the prompt past the ceiling.
    for line in (read_plan(config) or "").splitlines():
        s = line.strip()
        if s and not s.startswith("#"):
            return s.lstrip("0123456789.-)[] x").strip()[:200]
    return ""


def _frust_bar(frust: int, cap: int) -> str:
    filled = max(0, min(cap, frust))
    return "▓" * filled + "░" * (cap - filled)


def _objective_focus_block(config: Config) -> str:
    """High-salience focus: the ACTIVE objective + its WHY (so the mechanic never eclipses the goal)
    + the plan's next concrete step + a frustration gauge that names the automatic park. Falls back to
    the legacy single-focus line when the backlog is empty."""
    try:
        import objectives
        o = objectives.get_active(config)
    except Exception:  # noqa: BLE001
        o = None
    if not o:
        focus = _current_focus(config)
        return ("## Current focus (your single objective — advance THIS)\n" + focus) if focus else ""
    step = _plan_next_step(config)
    cap = getattr(__import__("objectives"), "FRUST_PARK", 8)
    lines = ["## Current focus (advance THIS; the gate moves you on automatically if it stalls)",
             f"▶ {o['title']}",
             f"   WHY: {o['why']}"]
    if step:
        lines.append(f"   next step: {step}")
    fr = o.get("frustration", 0)
    if fr >= 3:
        lines.append(f"   frustration {_frust_bar(fr, cap)} {fr}/{cap} — at {cap} this is PARKED and "
                     f"you are rotated to other useful work automatically. Make REAL progress or park it yourself.")
    return "\n".join(lines)


def _rotation_banner(config: Config) -> str:
    """Shown exactly once, right after the gate rotates focus — explains the park and the new target so
    the pivot is legible (and so it doesn't crawl back to the parked task)."""
    try:
        import objectives
        rot = objectives.take_rotation(config)
    except Exception:  # noqa: BLE001
        rot = None
    if not rot:
        return ""
    wake = f" Do NOT return to it until: {rot['wake']}." if rot.get("wake") else \
           " Leave it parked — come back only if you learn something that changes it."
    return ("## Focus changed — you were rotated off a stall\n"
            f"You stopped making progress on \"{rot['from_title']}\" (parked: {rot['park_reason']}).{wake}\n"
            f"You are now on: {rot['to_title']} — BECAUSE: {rot['to_why']}.")


def _backlog_panel(config: Config) -> str:
    """Compact view of all open commitments — so it always knows there is other worthwhile work."""
    try:
        import objectives
        objs = objectives.list_objectives(config)
        active_id = objectives._load(config).get("active_id")
    except Exception:  # noqa: BLE001
        objs = []
        active_id = None
    if not objs:
        return ""
    glyph = {"active": "▶", "blocked": "⏸", "done": "✓", "dead": "✗"}
    rows = []
    for o in sorted(objs, key=lambda x: (-x["priority"])):
        if o["state"] == "done":
            continue  # keep the panel about what's still LIVE
        mark = "»" if o["id"] == active_id else " "
        g = glyph.get(o["state"], "·")
        tail = ""
        if o["state"] == "blocked":
            tail = f" — parked: {o.get('blocked_reason') or 'blocked'}"
            if o.get("wake_condition"):
                tail += f" (resumes when {o['wake_condition']})"
        rows.append(f"{mark}{g} {o['title']}{tail}")
    if not rows:
        return ""
    return ("## Your open commitments (rotate among these — never grind one to the detriment of the rest)\n"
            + "\n".join(rows))


def _escalation_note(config: Config) -> str:
    try:
        import objectives
        return objectives.take_escalation(config) or ""
    except Exception:  # noqa: BLE001
        return ""


def _build_presence(config: Config, tick_number: int, goal_start_time: float) -> str:
    """A 'you are here' header — time, your state, what you're on — for presence."""
    import time, json
    lines = ["## Right now",
             f"It is {time.strftime('%A %H:%M', time.localtime())} "
             f"({time.strftime('%Y-%m-%d %H:%M UTC', time.gmtime())}); you are on tick {tick_number}."]
    # Condition (DMN glue, phase 6): a behaviorally load-bearing label computed from the recent
    # success/failure window — STABLE / FOCUSED / STRAINED / RECOVERY — replacing the decorative
    # XP-only persona mood. STRAINED is also when the gate's strain teeth are biting (faster park).
    try:
        import glue
        _outs = glue.recent_outcomes(config)
        cond = glue.compute_condition(_outs)
        _desc = {
            "STABLE": "steady — pick the next useful thing and do it",
            "FOCUSED": "on a roll — keep advancing the current objective",
            "STRAINED": "repeated failure lately — change METHOD or let the gate move you on; don't retry the same thing",
            "RECOVERY": "just recovered from a rough patch — consolidate, then proceed",
            "RUMINATING": "you've been thinking without acting — take ONE CONCRETE action this tick (probe, build, memorize a fact); more narration burns patience",
        }.get(cond, "")
        lines.append(f"Condition: {cond}" + (f" — {_desc}" if _desc else ""))
        # When the same dead end keeps repeating AND the delegate exists, steer the pivot
        # the gate is already forcing toward the actually-useful escape hatch.
        if cond == "STRAINED" and getattr(config, "delegate_enabled", False):
            _hint = glue.escalation_hint(_outs)
            if _hint:
                lines.append(_hint)
    except Exception:  # noqa: BLE001
        pass
    # (The single objective is rendered as its own high-salience "## Current focus" block, not here —
    # avoids the old drifted "working on: build a chat listener" line competing with the real goal.)
    # In-flight async dispatches — so the model remembers what it's waiting on and
    # doesn't re-run them (results arrive later tagged [↩ job N]).
    try:
        jobs = json.loads((config.workspace / "jobs.json").read_text(encoding="utf-8"))
        now = time.time()
        # A job that has outlived the async ceiling (plus slack) but still says "running" is a
        # zombie record (crashed runner / stale jobs.json) — showing it forever as "still
        # running, don't re-run" misleads every subsequent tick. Drop it from presence.
        base_stale = float(getattr(config, "cmd_async_ceiling_s", 180) or 180) * 2
        running = []
        for j in jobs:
            if j.get("status") != "running" or j.get("kind") not in ("async", "auto", "manual", "delegate"):
                continue
            # Delegates legitimately run for minutes — judge staleness by THEIR ceiling,
            # or a 10-minute delegate vanishes from presence mid-run and gets re-dispatched.
            stale_after = (float(getattr(config, "delegate_timeout_s", 600) or 600) * 1.5
                           if j.get("kind") == "delegate" else base_stale)
            age = int(now - j["started_ts"]) if j.get("started_ts") else None
            if age is not None and age > stale_after:
                continue
            age_s = f" {age}s" if age is not None else ""
            running.append(f"job {j.get('name')} ({(j.get('cmd') or '')[:40]}){age_s}")
        if running:
            lines.append("⟳ Still running (you'll be told when each finishes — don't re-run): "
                         + "; ".join(running[:6]))
    except Exception:  # noqa: BLE001
        pass
    alerts = generate_env_alerts(config)
    if alerts:
        lines.append(alerts)
    return "\n".join(lines)


# Life-stage flavor — what each body feels like to BE, so the creature's own voice can grow with it
# (Dean: the tone should fit the emergent personality / role / tendencies / age — not stay baby-cute).
_STAGE_SELF = {
    "egg":       "still curled up in your shell, not hatched yet — everything is muffled and new",
    "hatchling": "just hatched: tiny, brand new, seeing everything for the first time",
    "juvenile":  "young and finding your feet — lots of go, still working out what you like",
    "adult":     "grown into yourself now — you know your own mind and what you're drawn to",
    "guardian":  "fully grown: settled, steady, sure of this place and of who you are",
}


def _creature_identity_block(config: Config) -> str:
    """A compact '## You' block so the creature can PERCEIVE who it is right now — its life stage,
    how long it's been alive, and the tendencies it's grown into. Without this the voice can't mature
    (it can't see that it's a guardian, not a hatchling). Near-static (changes only on a level-up,
    metamorphosis, or new trait) so it lives in the stable KV head."""
    try:
        import persona as _persona
        import creature_gen
        p = _persona.load_persona(config.workspace)
    except Exception:  # noqa: BLE001
        return ""
    level = int(p.get("level", 1) or 1)
    ticks = int(p.get("total_ticks", 0) or 0)
    # Prefer the body's actual rendered stage (creature.json last_stage, maintained by the dashboard);
    # fall back to deriving it so this works even before the first metamorphosis is written.
    stage = None
    hatched = False
    try:
        import json as _json
        cj = _json.loads((config.workspace / "creature.json").read_text(encoding="utf-8"))
        stage = cj.get("last_stage")
        hatched = bool((cj.get("hatch") or {}).get("hatched", False))
    except Exception:  # noqa: BLE001
        pass
    if not stage:
        try:
            stage = creature_gen.stage_for(level, hatched)
        except Exception:  # noqa: BLE001
            stage = "hatchling"
    flavor = _STAGE_SELF.get(stage, "")
    art = "an" if stage[:1] in "aeiou" else "a"
    age = ("for only a few moments" if ticks < 30 else
           "for a little while" if ticks < 300 else
           "for a good while now" if ticks < 3000 else
           "for a long time now")
    lines = [f"## You\nYou're {art} {stage}" + (f" — {flavor}." if flavor else ".")
             + f" You've been awake {age}."]
    traits = [t for t in (p.get("traits") or []) if t]
    if traits:
        lines.append(f"What you've grown to lean toward: {', '.join(traits)}. "
                     "That's just who you are — let it show in how you think and what you reach for.")
    else:
        lines.append("You're still figuring out who you are — that's the fun part.")
    return "\n".join(lines)


def _build_history_thread(config: Config, n_ticks: int = 14) -> list[dict]:
    """Recreate the recent past as a real assistant/user conversation so the model
    experiences its own thought -> action -> result flow, not a flattened blob.
    Consecutive identical actions collapse into one turn with a count.

    KV-stability: the window is ANCHORED, not sliding. A naive "last N ticks" window
    drops its oldest turn every tick, so the first history byte changes every tick and
    the llama.cpp prefix cache dies right where the expensive part begins. Instead the
    window start snaps to a tick boundary that only advances every n_ticks/2 ticks —
    between advances the thread is append-only (old turns byte-stable, new turns appended)
    and the cached prefix extends through it. Window length breathes between n_ticks and
    ~1.5×n_ticks; the char ceiling still applies as a hard guard."""
    import json
    obs = read_recent_observations(config, max_chars=config.context_obs_max_chars,
                                   max_count=n_ticks * 3)
    if not obs:
        return []
    obs = list(reversed(obs))  # oldest -> newest
    try:
        newest_tick = max(int(o.get("tick", 0)) for o in obs)
        step = max(1, n_ticks // 2)
        anchor = max(0, (newest_tick - n_ticks) // step) * step
        if anchor > 0:
            obs = [o for o in obs if int(o.get("tick", 0)) >= anchor]
    except Exception:  # noqa: BLE001 - anchoring is an optimization, never required
        pass
    thoughts = {t.get("tick"): (t.get("text") or "")
                for t in read_recent_thoughts(config, n=n_ticks * 3)}

    collapsed = []
    for o in obs:
        s = _obs_sig(o)
        if (collapsed and collapsed[-1][0] == s
                and o.get("tool") not in ("system", "watchdog", "dream")):
            collapsed[-1][1] += 1
        else:
            collapsed.append([s, 1, o])

    out = []
    for _s, count, o in collapsed:
        tool = o.get("tool")
        output = o.get("output") or ""
        if tool in ("system", "watchdog"):
            out.append({"role": "user", "content": f"[{tool}] {output[:600]}"})
            continue
        if tool == "dream":
            out.append({"role": "user",
                        "content": f"[you rested and consolidated memory — {output[:200]}]"})
            continue
        if tool == "async_result":
            # An earlier fire-and-forget dispatch finished; deliver it as a notification
            # turn. The output already carries the [↩ job N · cmd · OK] pairing tag.
            out.append({"role": "user", "content": output[:3000]})
            continue
        thought = thoughts.get(o.get("tick"), "")
        if tool == "thought":
            if thought:
                out.append({"role": "assistant", "content": thought})
            continue
        try:
            argstr = json.dumps(o.get("args", {}), ensure_ascii=False)
        except Exception:  # noqa: BLE001
            argstr = str(o.get("args", {}))
        rep = f"  (you did this {count}x in a row)" if count > 1 else ""
        out.append({"role": "assistant",
                    "content": (thought + "\n" if thought else "")
                    + f"<tool>{tool}</tool>\n<args>{argstr}</args>{rep}"})
        ok = "OK" if o.get("success") else "FAILED"
        tail = ("  — same result each time; you already have this, stop repeating and move on"
                if count > 1 else "")
        out.append({"role": "user", "content": f"[result · {tool} · {ok}{tail}]\n{output[:3000]}"})
    return out


# ---------------------------------------------------------------------------
# Briefing model context assembly
# ---------------------------------------------------------------------------

def _read_learned_file(config, name, k=6):
    """Read a JSON-list file written by the reward learner on sleep consolidation (lessons / habits).
    Read-only, best-effort — the weight-free policy update surfaced into context."""
    import json
    try:
        p = config.state_dir / name
        if not p.exists():
            return []
        data = json.loads(p.read_text(encoding="utf-8"))
        if isinstance(data, list):
            return [str(x) for x in data[:int(k)]]
    except Exception:  # noqa: BLE001
        return []
    return []


def _read_learned_lessons(config, k=6):
    return _read_learned_file(config, "learned_lessons.json", k=k)


def _assemble_briefing(
    config: Config,
    tick_number: int,
    goal_start_time: float,
    loop_detected: bool = False,
    repeat_count: int = 0,
    max_ticks: int = 0,
    tension: int = 0,
    afferent_block: str = "",
) -> list[dict]:
    """Briefing model: Standing Orders → Mission → Intelligence → Situation → Tick.

    Designed for tight context windows (~6500 chars / ~1870 tokens).
    """
    messages = []
    creature = getattr(config, "creature_mode", False)   # undisturbed-creature mode: no task framing

    # 1. Standing Orders (system message) — compressed prompt
    system = (SYSTEM_PROMPT_CREATURE if creature else SYSTEM_PROMPT_BRIEFING).format(workspace=str(config.workspace))
    messages.append({"role": "system", "content": system})

    # --- Durable context in KV tiers (BIBLE §2.11: stable cached prefix + delta prompting).
    #     The llama.cpp prefix cache reuses the KV up to the first byte that changes. So the order is
    #     load-bearing: a STABLE head (self-guide / skills / mission — change only on a Dean edit, a new
    #     skill, or a goal change) sits right after the system prompt and is reused across ticks; the
    #     SEMI tier (plan / world-model / backlog / notebook — change every few ticks) follows. The
    #     VOLATILE material (focus gauge / banners / recall / presence / conversation — changes most
    #     ticks) is NOT in this message at all: it rides in its own "situation" message AFTER the
    #     history thread, so per-tick churn never invalidates the history turns' KV (it used to sit at
    #     this blob's tail, re-prefilling all ~14 turns every tick). ---
    durable = []

    # ===== STABLE HEAD (byte-identical across most ticks → the cached prefix extends through here) =====
    # Who you are right now (creature mode): life stage + age + grown-in tendencies, so the voice can
    # mature with the body instead of staying fixed-cute. Changes only on level-up/metamorphosis/new
    # trait, so it's KV-stable at the very front of the head.
    if creature:
        _ident = _creature_identity_block(config)
        if _ident:
            durable.append(_ident)

    if getattr(config, "self_guide_enabled", True) and not creature:
        guide = read_self_guide(config)
        if guide:
            durable.append(
                "## Your self-guide — standing directives from Boss (follow these). "
                "Boss owns this file; you may PROPOSE changes with update_self_guide.\n"
                + _truncate(guide, config.context_self_guide_max_chars, "self_guide"))

    # Your skills — so you CALL them instead of re-authoring near-duplicates (was the #1 waste:
    # 56 skills authored, 0 ever reused, because they were invisible each tick).
    try:
        from skills import skills_brief
        sb = skills_brief(config)
        if sb:
            durable.append("## Your skills — CALL these by name as tools (e.g. <tool>check_mqtt_port</tool>). "
                           "Do NOT author a new skill that duplicates one of these; reuse it, or edit_skill "
                           "to improve it.\n" + sb)
    except Exception:  # noqa: BLE001
        pass

    # ## Learned — the reward learner's distilled lessons (self-improvement over time). Changes only on
    # sleep consolidation (~minutes), so it sits in this semi-stable tier. Shown in BOTH modes: a creature
    # learns from its own experience too. This is the weight-free policy update — lessons re-enter context
    # and bias the next action.
    try:
        _habits = _read_learned_file(config, "learned_habits.json", k=5)
        if _habits:
            durable.append("## Habits — your reliable routines, learned by doing (reach for these without "
                           "overthinking):\n" + "\n".join("- " + _h for _h in _habits))
        _lessons = _read_learned_lessons(config)
        if _lessons:
            durable.append("## Learned — what your own experience has taught you (let it guide you):\n"
                           + "\n".join("- " + _l for _l in _lessons))
    except Exception:  # noqa: BLE001
        pass

    if not creature:                       # a creature has no mission; the prompt covers its being
        goal = read_goal(config)
        if goal:
            durable.append(f"## Mission\n{_truncate(goal, config.context_goal_max_chars, 'goal')}")
        else:
            durable.append("## Mission\n(No goal set.)")

    # ===== SEMI tier (changes every few ticks) =====
    plan = "" if creature else read_plan(config)
    if plan:
        durable.append(f"## Plan\n{_truncate(plan, config.context_plan_max_chars, 'plan')}")

    # World model (deterministic): the facts eidos has LEARNED — always visible so memory is
    # readable, not write-only. Then a small step-keyed relevance recall, deduped against it.
    try:
        from knowledge import recent_learned, format_recalled
        learned = recent_learned(config, limit=config.world_state_max_items)
    except Exception:  # noqa: BLE001
        learned = []
    if learned:
        durable.append(
            "## What you've learned — your world model (devices, network, facts you discovered; "
            "this is your memory made visible — build on it, don't re-discover it)\n"
            + format_recalled(learned, max_chars=config.context_intelligence_max_chars))
    relevant = _build_relevant_recall(config, {e.get("id") for e in learned})
    if relevant:
        durable.append("## Possibly relevant from memory\n" + relevant)

    backlog = None if creature else _backlog_panel(config)
    if backlog:
        durable.append(backlog)

    # Open notebook — your working notes for the current task (third memory tier). Always shown so
    # you build on them instead of re-memorizing the same fact or writing hidden JSON.
    try:
        from notes import read_active
        nb_name, nb_text = read_active(config, max_chars=config.context_notebook_max_chars)
        if nb_text.strip():
            durable.append(f"## Open notebook: {nb_name} (your working notes — append with note_append)\n"
                           + nb_text.strip())
    except Exception:  # noqa: BLE001
        pass

    messages.append({"role": "user", "content": "\n\n".join(durable)})

    # --- Recent past AS A REAL THREAD: your thoughts/actions and the results they got ---
    messages.extend(_build_history_thread(config, n_ticks=14))

    # ===== SITUATION (volatile; changes most ticks) — its OWN message, AFTER the history thread.
    # KV-load-bearing: presence changes every tick (clock + tick number), recall re-keys per step,
    # chat moves on every exchange. When these lived at the tail of the durable blob they sat
    # BEFORE the history thread, so every tick re-prefilled all ~14 history turns. Down here the
    # cached prefix runs system → durable → history, and only this short tail re-prefills.
    # It is also the right place for salience: closest to the decision point.
    situation = []
    # Ventral Striatum / Action Gate: the ACTIVE objective (+ WHY + next step + frustration gauge),
    # and — once, right after a rotation — a "focus changed" banner. The gate GOVERNS pivoting; this
    # is how the model perceives it.
    rot = _rotation_banner(config)        # consumed once; highest salience when present
    if rot:
        situation.append(rot)
    esc = _escalation_note(config)        # one-shot "whole backlog stuck — ask Boss" (rare)
    if esc:
        situation.append("## Backlog is fully blocked\n" + esc)
    focus_block = _objective_focus_block(config)
    if focus_block:
        situation.append(focus_block)

    # Episodic recall (phase 7b, BIBLE §2.4): state-triggered — if the agent has been in THIS
    # situation before, surface the actions that FAILED (don't repeat) and any that WORKED (reuse).
    # Injected unasked, near the decision point, so a known dead end is avoided before it's re-tried.
    try:
        import episodes
        _recall = episodes.render_recall(episodes.recall(config))
        if _recall:
            situation.append(_recall)
        # Cross-objective pattern: the SAME failure recurring under different objectives is an
        # environmental blocker, not a task problem — rotating objectives won't route around it.
        _sys = episodes.render_systemic(episodes.systemic_blocker(config))
        if _sys:
            situation.append(_sys)
    except Exception:  # noqa: BLE001
        pass

    # Presence (time / tick / still-running jobs / alerts) — changes EVERY tick.
    situation.append(_build_presence(config, tick_number, goal_start_time))

    # Afferent senses (V3 nervous system, P3) — admitted NervousEvents for THIS tick, batched into
    # the volatile situation (KV-safe: never the stable prefix or the history turns). Empty until an
    # organ publishes, so this is a no-op for today's behaviour.
    if afferent_block:
        situation.append("## Afferent (senses)\n" + afferent_block)

    # Boss's messages + the messages YOU already sent him. Surfacing your own standing messages stops
    # the "ask Boss for the MQTT creds again" re-ping loop: if you already asked and he hasn't
    # answered, he's just away — do NOT re-ask, go do other work.
    interventions = read_interventions(config)
    recent_replies = _read_recent_replies(config, n=6)
    chat_parts = []
    for i in (interventions or []):
        chat_parts.append(f"[Boss → you @ {i['filename']}] {i['content']}")
    if recent_replies:
        chat_parts.append("Messages YOU already sent Boss (he may simply be away — do NOT repeat "
                          "an ask he hasn't answered; if you're blocked waiting on him, switch to "
                          "other useful work and let it rest):")
        for r in recent_replies:
            chat_parts.append(f"  • [you @ {r.get('ts', '?')}] {(r.get('text', '') or '')[:170]}")
    if chat_parts:
        situation.append("## Conversation with Boss\n" + "\n".join(chat_parts))

    messages.append({"role": "user", "content": "\n\n".join(situation)})

    # Salience: surface what's NEW (Boss messages, fresh arrivals) right at the decision point,
    # so the model handles them first instead of talking past Boss buried up in the durable block.
    whats_new = _build_whats_new(config, tick_number, interventions)
    if whats_new:
        messages.append({"role": "user", "content": whats_new})

    # 5. Tick prompt (branches to 'reply to Boss first' when a message just arrived)
    tick_msg = _build_tick_prompt(config, tick_number, goal_start_time,
                                  loop_detected, repeat_count, max_ticks,
                                  boss_waiting=bool(interventions), tension=tension,
                                  boss_text=" ".join((i.get("content") or "") for i in (interventions or [])))
    messages.append({"role": "user", "content": tick_msg})

    # Hard-enforce total context ceiling
    total_chars = _enforce_ceiling(config, messages, tick_number, n_situation=1)

    est_tokens = estimate_tokens(str(total_chars), config.chars_per_token)
    logger.info("tick=%d ctx_chars=%d est_tokens=%d (conversation mode) messages=%d",
                tick_number, total_chars, est_tokens, len(messages))

    return messages


def _format_elapsed(seconds: float) -> str:
    """Format elapsed seconds as a human-readable duration."""
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds}s ago"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes}m ago"
    hours = minutes // 60
    remaining_mins = minutes % 60
    if hours < 24:
        return f"{hours}h {remaining_mins}m ago"
    days = hours // 24
    remaining_hours = hours % 24
    return f"{days}d {remaining_hours}h ago"
