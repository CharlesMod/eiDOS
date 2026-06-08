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
import time
from pathlib import Path

from config import Config
from memory import read_goal, read_memory, read_plan, read_subgoals, read_recent_observations, read_interventions, current_subtask, read_recent_thoughts
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

def _obs_sig(o: dict):
    """Signature for detecting repeated actions (same tool + same args/output)."""
    a = o.get("args")
    if a:
        return (o.get("tool"), str(a))
    return (o.get("tool"), (o.get("output", "") or "")[:80])


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
    if len(action_sigs) >= 4 and 1 <= len(set(action_sigs)) <= 2:
        names = " and ".join(sorted({str(s[0]) for s in action_sigs}))
        loop_note = (f"⚠ You have been repeating the same action(s) — {names} — with no "
                     f"new result. You ALREADY have this information. Stop repeating it; decide "
                     f"and take a different, concrete next step.\n\n")

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
) -> list[dict]:
    """Assemble the full messages list in fixed order for one tick.

    Returns a list of {"role": ..., "content": ...} dicts ready for llm.complete().
    Each variable section is budget-capped; overruns are logged to ctx_overruns.jsonl.

    When config.briefing_model is True, uses the compressed context structure:
    Standing Orders → Mission → Intelligence → Situation → Tick prompt.
    """
    if config.briefing_model:
        return _assemble_briefing(config, tick_number, goal_start_time,
                                  loop_detected, repeat_count, max_ticks)
    return _assemble_legacy(config, tick_number, goal_start_time,
                            loop_detected, repeat_count, max_ticks)


# ---------------------------------------------------------------------------
# Tick prompt (shared)
# ---------------------------------------------------------------------------

def _build_tick_prompt(config, tick_number, goal_start_time, loop_detected,
                       repeat_count, max_ticks):
    """Build the tick prompt message (shared by both modes)."""
    now = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime())
    elapsed = _format_elapsed(time.time() - goal_start_time)

    urgency_note = ""
    if max_ticks > 0:
        remaining = max_ticks - tick_number
        if remaining <= 0:
            urgency_note = " — FINAL TICK: call goal_complete now or the run ends"
        elif remaining <= 2:
            urgency_note = f" — {remaining} tick{'s' if remaining != 1 else ''} remaining: wrap up and call goal_complete if ready"

    subtask = current_subtask(config)
    subtask_line = f"Current task: {subtask}" if subtask else "No subtasks defined — focus on the goal directly."

    if loop_detected:
        return TICK_PROMPT_LOOP_DETECTED.format(
            tick_number=tick_number,
            max_ticks=max_ticks if max_ticks else "?",
            timestamp=now, elapsed=elapsed,
            repeat_count=repeat_count,
            urgency_note=urgency_note,
            subtask_line=subtask_line,
        )
    return TICK_PROMPT.format(
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

    # Subgoals
    subgoals = read_subgoals(config)
    if subgoals:
        if len(subgoals) > config.context_subgoals_max_chars:
            _log_overrun(config, tick_number, "subgoals", len(subgoals), config.context_subgoals_max_chars)
            subgoals = _truncate(subgoals, config.context_subgoals_max_chars, "subgoals")
        sections.append(f"## Subgoals\n{subgoals}")

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
# Briefing model context assembly
# ---------------------------------------------------------------------------

def _assemble_briefing(
    config: Config,
    tick_number: int,
    goal_start_time: float,
    loop_detected: bool = False,
    repeat_count: int = 0,
    max_ticks: int = 0,
) -> list[dict]:
    """Briefing model: Standing Orders → Mission → Intelligence → Situation → Tick.

    Designed for tight context windows (~6500 chars / ~1870 tokens).
    """
    messages = []

    # 1. Standing Orders (system message) — compressed prompt
    system = SYSTEM_PROMPT_BRIEFING.format(workspace=str(config.workspace))
    messages.append({"role": "system", "content": system})

    sections = []

    # 2. MISSION — goal + plan.md
    goal = read_goal(config)
    if goal:
        if len(goal) > config.context_goal_max_chars:
            _log_overrun(config, tick_number, "goal", len(goal), config.context_goal_max_chars)
            goal = _truncate(goal, config.context_goal_max_chars, "goal")
        sections.append(f"## Mission\n{goal}")
    else:
        sections.append("## Mission\n(No goal set. Waiting for goal.md to be created.)")

    plan = read_plan(config)
    if plan:
        if len(plan) > config.context_plan_max_chars:
            _log_overrun(config, tick_number, "plan", len(plan), config.context_plan_max_chars)
            plan = _truncate(plan, config.context_plan_max_chars, "plan")
        sections.append(f"## Plan\n{plan}")

    # Subgoals (immune to dream cycle rewrites)
    subgoals = read_subgoals(config)
    if subgoals:
        if len(subgoals) > config.context_subgoals_max_chars:
            _log_overrun(config, tick_number, "subgoals", len(subgoals), config.context_subgoals_max_chars)
            subgoals = _truncate(subgoals, config.context_subgoals_max_chars, "subgoals")
        sections.append(f"## Subgoals\n{subgoals}")

    # 3. INTELLIGENCE — auto-recalled knowledge from the knowledge store
    intel = _build_intelligence_section(config, goal, plan or "")
    if intel:
        sections.append(f"## Intelligence\n{intel}")

    # 4. SITUATION — inverted-pyramid observations + alert-only env + interventions

    # Interventions first (urgent)
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

    # Environment — alerts only (zero chars when normal)
    env_alerts = generate_env_alerts(config)
    if env_alerts:
        sections.append(f"## Environment\n{env_alerts}")

    # Observations — wider window (so it can see its own recent history), collapsed
    observations = read_recent_observations(
        config, max_chars=config.context_obs_max_chars, max_count=12)
    if observations:
        obs_text = _render_observations_pyramid(observations, full_budget=2000)
        if len(obs_text) > config.context_obs_max_chars:
            _log_overrun(config, tick_number, "observations",
                         len(obs_text), config.context_obs_max_chars)
            obs_text = _truncate(obs_text, config.context_obs_max_chars, "observations")
        sections.append(f"## Recent Observations\n{obs_text}")

    # Your own train of thought — the salient point to continue FROM (kept last).
    # Consecutive near-identical thoughts collapse so a stuck loop reads as one line.
    thoughts = read_recent_thoughts(config, n=10)
    if thoughts:
        def _tnorm(t):
            return "".join(c for c in (t.get("text", "") or "").lower()
                           if c.isalnum() or c == " ")[:45]
        groups = []
        for t in thoughts:
            txt = (t.get("text", "") or "").strip()
            if not txt:
                continue
            k = _tnorm(t)
            if groups and groups[-1][0] == k:
                groups[-1][1] += 1
            else:
                groups.append([k, 1, txt])
        tlines = []
        for _k, count, txt in groups:
            rep = f"  ×{count} (you keep thinking this — act on it or move on)" if count > 1 else ""
            tlines.append(f"- {txt}{rep}")
        thought_text = "\n".join(tlines)
        if len(thought_text) > 2200:
            thought_text = "…\n" + thought_text[-2200:]
        sections.append(f"## Your recent train of thought (continue from the last one)\n{thought_text}")

    user_content = "\n\n".join(sections)
    messages.append({"role": "user", "content": user_content})

    # 5. Tick prompt
    tick_msg = _build_tick_prompt(config, tick_number, goal_start_time,
                                  loop_detected, repeat_count, max_ticks)
    messages.append({"role": "user", "content": tick_msg})

    # Hard-enforce total context ceiling
    total_chars = _enforce_ceiling(config, messages, tick_number)

    est_tokens = estimate_tokens(str(total_chars), config.chars_per_token)
    logger.info("tick=%d ctx_chars=%d est_tokens=%d (briefing mode) sections=[sys=%d goal=%d plan=%d intel=%d obs=%d tick=%d]",
                tick_number, total_chars, est_tokens,
                len(system), len(goal) if goal else 0,
                len(plan) if plan else 0, len(intel) if intel else 0,
                len(observations) if observations else 0,
                len(tick_msg))

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
