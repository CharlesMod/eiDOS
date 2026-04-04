"""Memory compaction (/dream) — consolidate observations into working memory.

Uses a generous context budget so the LLM has full visibility of observations
when distilling them into concise working memory.
"""

import json
import logging
import time

from config import Config
from memory import (
    read_goal,
    read_memory,
    write_memory,
    read_recent_observations,
    count_observation_chars,
    append_observation,
)
from llm import complete, ReasoningExhausted
from prompts import COMPACTION_SYSTEM, COMPACTION_USER, COMPACTION_PERSONALITY_CLAUSE

logger = logging.getLogger("kairos.compaction")


def should_compact(config: Config, ticks_since_last: int) -> bool:
    """Check if compaction should run based on thresholds."""
    # Token threshold (approximated by char count)
    if count_observation_chars(config) >= config.compaction_token_threshold:
        return True
    # Tick count threshold
    if ticks_since_last >= config.compaction_tick_threshold:
        return True
    return False


def compact(config: Config, persona: dict = None) -> None:
    """Run a compaction pass: snapshot memory, call LLM, atomic rewrite.

    Reads all recent observations (with a generous budget for compaction)
    and the current memory, then asks the LLM to produce a consolidated
    new memory.md.  Uses separate (larger) context budgets than a normal tick
    so the distillation LLM sees the full picture.
    """
    # Snapshot current memory before overwriting
    _snapshot_memory(config)

    current_memory = read_memory(config)

    # Truncate memory if it exceeds the compaction budget
    if current_memory and len(current_memory) > config.compaction_memory_max_chars:
        logger.warning("compaction: memory exceeds budget (%d > %d), truncating",
                       len(current_memory), config.compaction_memory_max_chars)
        current_memory = current_memory[:config.compaction_memory_max_chars] + "\n... [truncated]"

    # For compaction, read MORE observations than a normal tick —
    # use the dedicated compaction budget so distillation sees everything.
    observations = read_recent_observations(
        config,
        max_chars=config.compaction_obs_max_chars,
        max_count=200,
    )

    if not observations and not current_memory:
        return  # Nothing to compact

    # Format observations for the prompt
    obs_text = _format_observations(observations)

    # Build system prompt with optional personality clause
    system_content = COMPACTION_SYSTEM
    if persona and config.persona_enabled:
        traits = ", ".join(persona.get("traits", [])) or "developing"
        mood = persona.get("mood", "neutral")
        system_content += COMPACTION_PERSONALITY_CLAUSE.format(traits=traits, mood=mood)

    goal = read_goal(config)
    messages = [
        {"role": "system", "content": system_content},
        {"role": "user", "content": COMPACTION_USER.format(
            goal=goal or "(no goal set)",
            memory=current_memory or "(empty — first compaction)",
            observations=obs_text or "(no observations)",
        )},
    ]

    # Log compaction context size
    total_chars = sum(len(m["content"]) for m in messages)
    est_tokens = int(total_chars / config.chars_per_token)
    logger.info("compaction ctx_chars=%d est_tokens=%d (memory=%d obs=%d)",
                total_chars, est_tokens, len(current_memory or ""), len(obs_text or ""))

    if total_chars > config.compaction_context_max_chars:
        logger.warning("compaction ctx overrun: %d chars > %d budget",
                       total_chars, config.compaction_context_max_chars)
        _log_compaction_overrun(config, total_chars)

    # Thinking models (Qwen 3.5, etc.) may exhaust max_tokens on reasoning
    # and produce zero content tokens.  We keep thinking enabled (it helps
    # small models) but catch the exhaustion and retry with:
    #   1. A larger token budget
    #   2. A prompt nudge telling the model to keep reasoning brief
    try:
        new_memory = complete(messages, config, temperature=0.3,
                              max_tokens=config.compaction_max_tokens)
    except ReasoningExhausted as e:
        logger.warning("compaction: reasoning exhausted %d/%d tokens — "
                       "retrying with %d max_tokens and budget feedback",
                       e.reasoning_tokens, e.max_tokens,
                       config.compaction_retry_max_tokens)
        # Add budget feedback so the model knows what happened
        retry_messages = messages + [
            {"role": "assistant", "content": "(internal thinking used all available tokens — no output produced)"},
            {"role": "user", "content":
             "Your reasoning used the entire token budget and produced no output. "
             "You now have a larger budget. Keep your thinking concise and "
             "focus on producing the memory document."},
        ]
        try:
            new_memory = complete(retry_messages, config, temperature=0.3,
                                  max_tokens=config.compaction_retry_max_tokens)
        except ReasoningExhausted:
            logger.warning("compaction: reasoning exhausted even on retry — "
                           "keeping previous memory")
            new_memory = ""

    # Some models occasionally return empty content for chat completions.
    # Keep at least the existing memory to avoid destructive compaction.
    if not new_memory or not new_memory.strip():
        new_memory = current_memory or "# Working Memory\nNo consolidated update produced in this pass."

    # Hard cap: compaction output must fit in the tick-level memory budget.
    # This guarantees memory reliably shrinks back to a usable size.
    cap = config.context_memory_max_chars
    if len(new_memory) > cap:
        logger.warning("compaction output too large (%d > %d), trimming",
                       len(new_memory), cap)
        new_memory = new_memory[:cap].rsplit("\n", 1)[0] + "\n... [compaction trimmed]"

    # Atomic write
    write_memory(config, new_memory.strip())

    # Log the compaction event
    append_observation(config, {
        "tick": "compaction",
        "tool": "dream",
        "success": True,
        "output": f"Compacted memory. Before: {len(current_memory)} chars, after: {len(new_memory)} chars.",
    })


def _snapshot_memory(config: Config) -> None:
    """Save a timestamped copy of memory.md before compaction."""
    config.snapshots_dir.mkdir(parents=True, exist_ok=True)
    current = read_memory(config)
    if not current:
        return
    ts = time.strftime("%Y%m%d_%H%M%S", time.gmtime())
    snapshot_path = config.snapshots_dir / f"memory_before_{ts}.md"
    snapshot_path.write_text(current)


def _format_observations(observations: list[dict]) -> str:
    """Format observation entries for the compaction prompt."""
    lines = []
    for obs in observations:
        ts = obs.get("ts", "?")
        tick = obs.get("tick", "?")
        tool = obs.get("tool", "?")
        success = "OK" if obs.get("success", False) else "FAIL"
        output = obs.get("output", "")
        # Keep output concise for compaction
        if len(output) > 500:
            output = output[:500] + "..."
        lines.append(f"[tick {tick} | {ts} | {tool} | {success}] {output}")
    return "\n".join(lines)


def _log_compaction_overrun(config: Config, total_chars: int) -> None:
    """Append compaction overrun to ctx_overruns.jsonl."""
    entry = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "tick": "compaction",
        "section": "COMPACTION_TOTAL",
        "actual_chars": total_chars,
        "budget_chars": config.compaction_context_max_chars,
        "overage_chars": total_chars - config.compaction_context_max_chars,
    }
    try:
        path = config.workspace / "ctx_overruns.jsonl"
        with open(path, "a") as f:
            f.write(json.dumps(entry) + "\n")
    except OSError:
        pass
