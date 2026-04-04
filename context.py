"""Context assembly — builds the messages list for each tick.

Includes per-section budget enforcement, truncation, and overrun logging
so we can tune limits based on real usage without blowing the context window.
"""

import json
import logging
import time

from config import Config
from memory import read_goal, read_memory, read_recent_observations, read_interventions
from env_snapshot import generate as generate_env_snapshot
from prompts import SYSTEM_PROMPT, TICK_PROMPT, TICK_PROMPT_LOOP_DETECTED

logger = logging.getLogger("kairos.context")


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
    """
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
    if interventions:
        intervention_text = "\n\n".join(
            f"[{i['filename']}]\n{i['content']}" for i in interventions
        )
        if len(intervention_text) > config.context_interventions_max_chars:
            _log_overrun(config, tick_number, "interventions",
                         len(intervention_text), config.context_interventions_max_chars)
            intervention_text = _truncate(intervention_text,
                                          config.context_interventions_max_chars, "interventions")
        sections.append(f"## Interventions (from supervisor)\n{intervention_text}")

    # 6. Recent observations — adaptive budget.
    #    When memory is large, reduce obs budget so total stays bounded;
    #    when memory is small, give more room to observations.
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
        # Observations already bounded by read_recent_observations, but double-check
        if len(obs_text) > config.context_obs_max_chars:
            _log_overrun(config, tick_number, "observations",
                         len(obs_text), config.context_obs_max_chars)
            obs_text = _truncate(obs_text, config.context_obs_max_chars, "observations")
        sections.append(f"## Recent Observations (newest first)\n{obs_text}")

    user_content = "\n\n".join(sections)
    messages.append({"role": "user", "content": user_content})

    # 7. Tick prompt (fixed — not truncated)
    now = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime())
    elapsed = _format_elapsed(time.time() - goal_start_time)

    # Urgency nudge: when 3 or fewer ticks remain, remind the model to finish.
    urgency_note = ""
    if max_ticks > 0:
        remaining = max_ticks - tick_number
        if remaining <= 0:
            urgency_note = " — FINAL TICK: call goal_complete now or the run ends"
        elif remaining <= 2:
            urgency_note = f" — {remaining} tick{'s' if remaining != 1 else ''} remaining: wrap up and call goal_complete if ready"

    if loop_detected:
        tick_msg = TICK_PROMPT_LOOP_DETECTED.format(
            tick_number=tick_number,
            max_ticks=max_ticks if max_ticks else "?",
            timestamp=now,
            elapsed=elapsed,
            repeat_count=repeat_count,
            urgency_note=urgency_note,
        )
    else:
        tick_msg = TICK_PROMPT.format(
            tick_number=tick_number,
            max_ticks=max_ticks if max_ticks else "?",
            timestamp=now,
            elapsed=elapsed,
            urgency_note=urgency_note,
        )

    messages.append({"role": "user", "content": tick_msg})

    # --- Hard-enforce total context ceiling ---
    # If over budget, shrink the user message (middle message) by trimming
    # observations first, then trimming oldest memory lines. System and tick
    # prompts are never touched — the model needs both to function.
    total_chars = sum(len(m["content"]) for m in messages)
    if total_chars > config.context_max_total_chars:
        _log_overrun(config, tick_number, "TOTAL", total_chars, config.context_max_total_chars)
        overhead = sum(len(m["content"]) for m in (messages[0], messages[2]))  # sys + tick
        user_budget = config.context_max_total_chars - overhead
        user_content = messages[1]["content"]
        if len(user_content) > user_budget:
            # Trim from the end of the user message (observations are last)
            # Use a clean line boundary so we don't split inside a JSON blob
            lines = user_content.splitlines(keepends=True)
            while lines and len("".join(lines)) > user_budget:
                lines.pop()
            user_content = "".join(lines) + "\n... [context trimmed to fit budget]"
            messages[1] = {"role": "user", "content": user_content}
        total_chars = sum(len(m["content"]) for m in messages)

    est_tokens = estimate_tokens(str(total_chars), config.chars_per_token)
    logger.info("tick=%d ctx_chars=%d est_tokens=%d sections=[sys=%d goal=%d mem=%d env=%d obs=%d tick=%d]",
                tick_number, total_chars, est_tokens,
                len(system), len(goal) if goal else 0,
                len(memory) if memory else 0, len(env),
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
