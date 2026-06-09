"""Context assembly — builds the messages list for each tick.

Supports two modes:
  - Legacy (default): original section layout with full env snapshot every tick.
  - Briefing model (config.briefing_model = True): compressed system prompt,
    Mission/Intelligence/Situation sections, inverted-pyramid observations,
    alert-only environment, and passive knowledge recall.

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
from memory import read_goal, read_memory, read_plan, read_recent_observations, read_interventions, read_recent_thoughts, read_self_guide
from env_snapshot import generate as generate_env_snapshot
from env_snapshot import generate_alerts as generate_env_alerts
from prompts import SYSTEM_PROMPT, SYSTEM_PROMPT_BRIEFING, TICK_PROMPT, TICK_PROMPT_LOOP_DETECTED

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


def _render_observations_pyramid(observations: list[dict], full_budget: int = 2000) -> str:
    """Render observations newest-first, collapsing repeats and flagging loops.

    Wider than before (up to ~12) so it can SEE its own recent history, but kept
    compact: consecutive identical actions collapse to one line with a ×N count,
    and if the window is dominated by 1-2 repeated actions (including A-B-A-B
    alternation, which has no consecutive duplicates) a salient loop warning is
    prepended so the model can notice it is stuck and break out.
    """
    if not observations:
        return ""

    # Loop warning across the whole window (catches alternation).
    _skip = {"system", "watchdog", "thought", "dream"}
    action_sigs = [_obs_sig(o) for o in observations if o.get("tool") not in _skip]
    loop_note = ""
    if len(action_sigs) >= 4:
        counts = Counter(action_sigs)
        _top_sig, top_n = counts.most_common(1)[0]
        # Fire on EITHER the whole window being 1-2 distinct actions, OR any single (normalized)
        # action recurring ≥4× even when interleaved with others — the retry-with-tweaks spiral.
        if (1 <= len(set(action_sigs)) <= 2) or top_n >= 4:
            names = " and ".join(sorted({str(s[0]) for s in action_sigs}))
            loop_note = (f"⚠ STUCK: you keep repeating the same kind of action ({names}) — {top_n} "
                         f"near-identical attempts with no new result. STOP retrying tiny variations "
                         f"(different ports/quotes/versions of the same command). Either (a) a result "
                         f"you already have answers this — use it, or (b) the whole approach is wrong "
                         f"— change METHOD entirely (e.g. a different tool/command) or ask Boss. Do "
                         f"NOT re-issue another tweaked version of this command.\n\n")

    # Collapse consecutive identical entries.
    collapsed = []
    for o in observations:
        s = _obs_sig(o)
        if collapsed and collapsed[-1][0] == s:
            collapsed[-1][1] += 1
        else:
            collapsed.append([s, 1, o])

    lines = []
    for i, (_s, count, obs) in enumerate(collapsed):
        if i > 11:
            break
        tick = obs.get("tick", "?")
        tool = obs.get("tool", "?")
        success = "OK" if obs.get("success", False) else "FAIL"
        output = obs.get("output", "") or ""
        rep = f" ×{count} (repeated — no new result)" if count > 1 else ""
        if i == 0:
            truncated = output[:full_budget]
            lines.append(f"[tick {tick} | {tool} | {success}]{rep}\n{truncated}")
        elif i <= 3:
            first_line = output.split("\n")[0][:120] if output else ""
            lines.append(f"[tick {tick} | {tool} | {success}]{rep} {first_line}")
        else:
            lines.append(f"[tick {tick} | {tool} | {success}]{rep}")

    return loop_note + "\n".join(lines)


# ---------------------------------------------------------------------------
# Passive knowledge recall (briefing model)
# ---------------------------------------------------------------------------

def _build_intelligence_section(config: Config, goal: str, plan: str) -> str:
    """Auto-recall knowledge relevant to the current goal + plan.

    Merges BM25 results with any pre-cached semantic results from
    recall_cache.md (written by dream_prefetch in Phase 5).

    Returns the formatted Intelligence section body, or empty string.
    """
    if not config.knowledge_enabled:
        return ""

    try:
        from knowledge import search_bm25, format_recalled
    except ImportError:
        return ""

    # Build a query from goal + the first ~200 chars of plan (next-action focus)
    query_parts = []
    if goal:
        query_parts.append(goal[:200])
    if plan:
        # Take the first non-empty line that looks like a next step
        for line in plan.splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                query_parts.append(line[:200])
                break

    query = " ".join(query_parts)

    # BM25 recall
    bm25_results = []
    if query.strip():
        bm25_results = search_bm25(config, query, top_k=config.knowledge_recall_top_k)

    # Merge with semantic recall cache (Phase 5)
    cache_text = _read_recall_cache(config)

    # Combine: BM25 formatted text + cache, deduplicating by ID
    bm25_ids = {r["id"] for r in bm25_results}
    parts = []

    if bm25_results:
        parts.append(format_recalled(bm25_results, max_chars=config.context_intelligence_max_chars))

    if cache_text:
        # Append cache lines not already covered by BM25
        for line in cache_text.splitlines():
            # Skip lines whose entry ID appears in BM25 results
            # Cache lines look like: [FACT] (tag1, tag2) content
            # We can't easily extract IDs, so include all cache content
            # when there's budget remaining
            parts.append(line)

    combined = "\n".join(parts)
    if not combined.strip():
        return ""

    return combined[:config.context_intelligence_max_chars]


def _read_recall_cache(config: Config) -> str:
    """Read the semantic recall pre-cache file, if it exists."""
    cache_path = config.workspace / "recall_cache.md"
    try:
        return cache_path.read_text().strip()
    except OSError:
        return ""


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
    results = [r for r in search_bm25(config, step, top_k=config.knowledge_recall_top_k)
               if r.get("id") not in exclude_ids]
    if not results:
        return ""
    return format_recalled(results, max_chars=max(300, config.context_intelligence_max_chars // 2))


# ---------------------------------------------------------------------------
# Context assembly
# ---------------------------------------------------------------------------

def _tension_note(tension: int) -> str:
    """Goal-tension banner (Ventral Striatum + ACC + Insula): escalating pressure that pushes the agent
    to ABANDON a stalled/blocked objective and pursue the BREADTH of other things it could do — not to
    keep grinding the same dead end. `tension` = ticks since real progress (a novel fact or a new skill)."""
    if tension < 12:
        return ""
    if tension < 25:
        return (f"⚠ {tension} ticks without new progress. You can do FAR more than your current focus. "
                f"If it's blocked or not working, PARK it and switch to a different device or task — see "
                f"'Other directions' below.")
    if tension < 45:
        return (f"⛔ STUCK — {tension} ticks, no new progress. ABANDON this objective now and pick a "
                f"DIFFERENT one from 'Other directions' below. If you already asked Boss something, you "
                f"are BLOCKED on him — do OTHER useful work while you wait; do NOT keep prepping the "
                f"blocked task or re-confirming what you already know.")
    return (f"⛔⛔ DEAD END — {tension} ticks, zero progress. This isn't working. Choose a COMPLETELY "
            f"different objective from 'Other directions' below THIS tick. Stop pouring ticks into one thing.")


_BREADTH = [
    "Speak to Boss aloud through your GLaDOS TTS voice (POST text to http://127.0.0.1:8004 / FX :8005).",
    "Snapshot or watch one of the IP cameras you found.",
    "Check the 3D printer / OctoPrint status and report it.",
    "Track the house's energy use or device activity over time.",
    "Learn and `memorize` Boss's routines, schedule, and preferences.",
    "Identify a device you found but haven't explored yet (http_probe / tcp_probe it).",
    "Build a small, genuinely useful house automation or a new reusable skill.",
    "Notice something interesting and surface it to Boss with <reply>.",
    "Improve your own toolset, or tidy your notebooks/memory.",
]


def _breadth_menu(config: Config) -> str:
    """A concrete menu of OTHER things to pursue — surfaced when tension is high so the agent has a
    place to pivot to instead of tunnelling on one dead end."""
    lines = list(_BREADTH)
    try:
        from knowledge import recent_learned
        ips = set()
        for e in recent_learned(config, limit=20):
            ips.update(re.findall(r"\d+\.\d+\.\d+\.\d+", e.get("content_preview") or ""))
        if ips:
            lines.append("Devices you've noted: " + ", ".join(sorted(ips)[:10])
                         + " — pick one you haven't fully explored.")
    except Exception:  # noqa: BLE001
        pass
    return "\n".join(f"- {x}" for x in lines)


def assemble_context(
    config: Config,
    tick_number: int,
    goal_start_time: float,
    loop_detected: bool = False,
    repeat_count: int = 0,
    max_ticks: int = 0,
    tension: int = 0,
) -> list[dict]:
    """Assemble the full messages list in fixed order for one tick.

    Returns a list of {"role": ..., "content": ...} dicts ready for llm.complete().
    Each variable section is budget-capped; overruns are logged to ctx_overruns.jsonl.

    When config.briefing_model is True, uses the compressed context structure:
    Standing Orders → Mission → Intelligence → Situation → Tick prompt.
    """
    if config.briefing_model:
        return _assemble_briefing(config, tick_number, goal_start_time,
                                  loop_detected, repeat_count, max_ticks, tension)
    return _assemble_legacy(config, tick_number, goal_start_time,
                            loop_detected, repeat_count, max_ticks)


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
            urgency_note = " — FINAL TICK: call goal_complete now or the run ends"
        elif remaining <= 2:
            urgency_note = f" — {remaining} tick{'s' if remaining != 1 else ''} remaining: wrap up and call goal_complete if ready"

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


def _enforce_ceiling(config, messages, tick_number):
    """Hard-enforce total context ceiling by trimming the user message."""
    total_chars = sum(len(m["content"]) for m in messages)
    if total_chars > config.context_max_total_chars:
        _log_overrun(config, tick_number, "TOTAL", total_chars, config.context_max_total_chars)
        overhead = sum(len(m["content"]) for m in (messages[0], messages[2]))  # sys + tick
        user_budget = config.context_max_total_chars - overhead
        user_content = messages[1]["content"]
        if len(user_content) > user_budget:
            lines = user_content.splitlines(keepends=True)
            while lines and len("".join(lines)) > user_budget:
                lines.pop()
            user_content = "".join(lines) + "\n... [context trimmed to fit budget]"
            messages[1] = {"role": "user", "content": user_content}
        total_chars = sum(len(m["content"]) for m in messages)
    return total_chars


# ---------------------------------------------------------------------------
# Legacy context assembly (original layout)
# ---------------------------------------------------------------------------

def _assemble_legacy(
    config: Config,
    tick_number: int,
    goal_start_time: float,
    loop_detected: bool = False,
    repeat_count: int = 0,
    max_ticks: int = 0,
) -> list[dict]:
    """Original context layout: System → Goal/Memory/Env/Interventions/Obs → Tick."""
    messages = []

    # 1. System prompt (fixed — not truncated)
    system = SYSTEM_PROMPT.format(workspace=str(config.workspace))
    messages.append({"role": "system", "content": system})

    # 2-6 are assembled as a single user message with budgeted sections
    sections = []

    # 2. Goal — budget-capped
    goal = read_goal(config)
    if goal:
        if len(goal) > config.context_goal_max_chars:
            _log_overrun(config, tick_number, "goal", len(goal), config.context_goal_max_chars)
            goal = _truncate(goal, config.context_goal_max_chars, "goal")
        sections.append(f"## Goal\n{goal}")
    else:
        sections.append("## Goal\n(No goal set. Waiting for goal.md to be created.)")

    # 3. Memory — budget-capped
    memory = read_memory(config)
    mem_len = len(memory) if memory else 0
    if memory:
        if mem_len > config.context_memory_max_chars:
            _log_overrun(config, tick_number, "memory", mem_len, config.context_memory_max_chars)
            memory = _truncate(memory, config.context_memory_max_chars, "memory")
            mem_len = config.context_memory_max_chars
        sections.append(f"## Working Memory\n{memory}")

    # 4. Environment snapshot — budget-capped
    env = generate_env_snapshot(config)
    if len(env) > config.context_env_max_chars:
        _log_overrun(config, tick_number, "env", len(env), config.context_env_max_chars)
        env = _truncate(env, config.context_env_max_chars, "env")
    sections.append(f"## Environment\n{env}")

    # 5. Pending interventions — budget-capped
    interventions = read_interventions(config)
    recent_replies = _read_recent_replies(config, n=3)
    chat_parts = []
    if recent_replies:
        for r in recent_replies:
            chat_parts.append(f"[your reply @ {r.get('ts', '?')}]\n{r.get('text', '')}")
    if interventions:
        for i in interventions:
            chat_parts.append(f"[operator @ {i['filename']}]\n{i['content']}")
    if chat_parts:
        intervention_text = "\n\n".join(chat_parts)
        if len(intervention_text) > config.context_interventions_max_chars:
            _log_overrun(config, tick_number, "interventions",
                         len(intervention_text), config.context_interventions_max_chars)
            intervention_text = _truncate(intervention_text,
                                          config.context_interventions_max_chars, "interventions")
        sections.append(f"## Chat with supervisor\n{intervention_text}")

    # 6. Recent observations — adaptive budget
    combined_budget = config.context_memory_max_chars + config.context_obs_max_chars
    obs_budget = max(1000, combined_budget - mem_len)
    observations = read_recent_observations(config, max_chars=obs_budget)
    if observations:
        obs_lines = []
        for obs in observations:
            ts = obs.get("ts", "?")
            tick = obs.get("tick", "?")
            tool = obs.get("tool", "?")
            success = "OK" if obs.get("success", False) else "FAIL"
            output = obs.get("output", "")
            obs_lines.append(f"[tick {tick} | {ts} | {tool} | {success}]\n{output}")
        obs_text = "\n---\n".join(obs_lines)
        if len(obs_text) > config.context_obs_max_chars:
            _log_overrun(config, tick_number, "observations",
                         len(obs_text), config.context_obs_max_chars)
            obs_text = _truncate(obs_text, config.context_obs_max_chars, "observations")
        sections.append(f"## Recent Observations (newest first)\n{obs_text}")

    user_content = "\n\n".join(sections)
    messages.append({"role": "user", "content": user_content})

    # 7. Tick prompt
    tick_msg = _build_tick_prompt(config, tick_number, goal_start_time,
                                  loop_detected, repeat_count, max_ticks)
    messages.append({"role": "user", "content": tick_msg})

    # Hard-enforce total context ceiling
    total_chars = _enforce_ceiling(config, messages, tick_number)

    est_tokens = estimate_tokens(str(total_chars), config.chars_per_token)
    logger.info("tick=%d ctx_chars=%d est_tokens=%d (legacy mode)",
                tick_number, total_chars, est_tokens)

    return messages


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
    step = ""
    for line in (read_plan(config) or "").splitlines():
        s = line.strip()
        if s and not s.startswith("#"):
            step = s.lstrip("0123456789.-)[] x").strip()
            break
    parts = [p for p in (focus, (f"next step: {step}" if step else "")) if p]
    return " — ".join(parts)


def _plan_next_step(config: Config) -> str:
    for line in (read_plan(config) or "").splitlines():
        s = line.strip()
        if s and not s.startswith("#"):
            return s.lstrip("0123456789.-)[] x").strip()
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
    try:
        p = json.loads((config.workspace / "persona.json").read_text(encoding="utf-8"))
        traits = ", ".join(p.get("traits", []) or []) or "still forming"
        lines.append(f"You are Level {p.get('level', '?')} — {p.get('mood', 'curious')}, "
                     f"{p.get('xp', '?')} XP. Traits: {traits}.")
    except Exception:  # noqa: BLE001
        pass
    # (The single objective is rendered as its own high-salience "## Current focus" block, not here —
    # avoids the old drifted "working on: build a chat listener" line competing with the real goal.)
    # In-flight async dispatches — so the model remembers what it's waiting on and
    # doesn't re-run them (results arrive later tagged [↩ job N]).
    try:
        jobs = json.loads((config.workspace / "jobs.json").read_text(encoding="utf-8"))
        now = time.time()
        running = []
        for j in jobs:
            if j.get("status") != "running" or j.get("kind") not in ("async", "auto", "manual"):
                continue
            age = int(now - j["started_ts"]) if j.get("started_ts") else None
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


def _build_history_thread(config: Config, n_ticks: int = 14) -> list[dict]:
    """Recreate the recent past as a real assistant/user conversation so the model
    experiences its own thought -> action -> result flow, not a flattened blob.
    Consecutive identical actions collapse into one turn with a count."""
    import json
    obs = read_recent_observations(config, max_chars=config.context_obs_max_chars,
                                   max_count=n_ticks * 2)
    if not obs:
        return []
    obs = list(reversed(obs))  # oldest -> newest
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

def _assemble_briefing(
    config: Config,
    tick_number: int,
    goal_start_time: float,
    loop_detected: bool = False,
    repeat_count: int = 0,
    max_ticks: int = 0,
    tension: int = 0,
) -> list[dict]:
    """Briefing model: Standing Orders → Mission → Intelligence → Situation → Tick.

    Designed for tight context windows (~6500 chars / ~1870 tokens).
    """
    messages = []

    # 1. Standing Orders (system message) — compressed prompt
    system = SYSTEM_PROMPT_BRIEFING.format(workspace=str(config.workspace))
    messages.append({"role": "system", "content": system})

    # --- Durable context, ordered STABLE → VOLATILE so the llama.cpp prefix cache (cache_prompt)
    #     reuses the KV of the unchanging top across ticks. Presence (the per-tick timestamp/jobs)
    #     now goes to the VOLATILE TAIL below — having it at position 0 changed every tick and
    #     invalidated the whole message's KV. (doc: stable cached prefix + delta prompting.) ---
    durable = []

    # Ventral Striatum / Action Gate: the ACTIVE objective (+ its WHY), the open-commitments backlog,
    # and — once, right after a rotation — a "focus changed" banner. This replaces the single drifting
    # focus line and the global tension banner: pivoting is now GOVERNED by the gate, not pleaded for.
    rot = _rotation_banner(config)        # consumed once; highest salience when present
    if rot:
        durable.append(rot)
    esc = _escalation_note(config)        # one-shot "whole backlog stuck — ask Boss" (rare)
    if esc:
        durable.append("## Backlog is fully blocked\n" + esc)
    focus_block = _objective_focus_block(config)
    if focus_block:
        durable.append(focus_block)
    backlog = _backlog_panel(config)
    if backlog:
        durable.append(backlog)
    # Fallback breadth menu only when the backlog can't carry the pivot (empty/all parked).
    if not backlog and _tension_note(tension):
        durable.append("## Other directions you could pursue right now\n" + _breadth_menu(config))

    # Self-guide — Boss's standing behavioral directives, high salience (just under presence).
    if getattr(config, "self_guide_enabled", True):
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

    goal = read_goal(config)
    if goal:
        durable.append(f"## Mission\n{_truncate(goal, config.context_goal_max_chars, 'goal')}")
    else:
        durable.append("## Mission\n(No goal set.)")

    plan = read_plan(config)
    if plan:
        durable.append(f"## Plan\n{_truncate(plan, config.context_plan_max_chars, 'plan')}")

    # (## Subgoals removed — auto-decomposition drifted into platform-contradicting goals. The single
    #  objective is the "## Current focus" block above; the agent keeps its next step via update_plan.)

    # World model (deterministic): the facts eidos has LEARNED — always visible so memory is
    # readable, not write-only. Then a small step-keyed relevance recall, deduped against it.
    try:
        from knowledge import recent_learned, format_recalled
        learned = recent_learned(config, limit=getattr(config, "world_state_max_items", 12))
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

    # Open notebook — your working notes for the current task (third memory tier). Always shown so
    # you build on them instead of re-memorizing the same fact or writing hidden JSON.
    try:
        from notes import read_active
        nb_name, nb_text = read_active(config, max_chars=getattr(config, "context_notebook_max_chars", 1200))
        if nb_text.strip():
            durable.append(f"## Open notebook: {nb_name} (your working notes — append with note_append)\n"
                           + nb_text.strip())
    except Exception:  # noqa: BLE001
        pass

    # Presence (time / tick / still-running jobs / alerts) — VOLATILE, placed late so the per-tick
    # timestamp doesn't invalidate the cached stable prefix above.
    durable.append(_build_presence(config, tick_number, goal_start_time))

    # Boss's messages + the messages YOU already sent him — highest priority. Surfacing your
    # own standing messages stops the "ask Boss for the MQTT creds again" re-ping loop: if you
    # already asked and he hasn't answered, he's just away — do NOT re-ask, go do other work.
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
        durable.append("## Conversation with Boss\n" + "\n".join(chat_parts))

    messages.append({"role": "user", "content": "\n\n".join(durable)})

    # --- Recent past AS A REAL THREAD: your thoughts/actions and the results they got ---
    messages.extend(_build_history_thread(config, n_ticks=14))

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
    total_chars = _enforce_ceiling(config, messages, tick_number)

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
