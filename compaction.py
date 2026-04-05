"""Memory compaction (/dream) — consolidate observations into working memory.

Uses a generous context budget so the LLM has full visibility of observations
when distilling them into concise working memory.

Two modes:
- Legacy: single LLM call rewrites memory.md (original behaviour)
- Briefing: two-phase dream cycle (plan update + knowledge extraction)
"""

import json
import logging
import re
import time

from config import Config
from memory import (
    read_goal,
    read_memory,
    write_memory,
    read_plan,
    write_plan,
    read_recent_observations,
    count_observation_chars,
    append_observation,
)
from llm import complete, ReasoningExhausted
from prompts import (
    COMPACTION_SYSTEM,
    COMPACTION_USER,
    COMPACTION_PERSONALITY_CLAUSE,
    COMPACTION_PLAN_SYSTEM,
    COMPACTION_PLAN_USER,
    COMPACTION_EXTRACT_SYSTEM,
    COMPACTION_EXTRACT_USER,
    COMPACTION_COMBINED_SYSTEM,
    COMPACTION_COMBINED_USER,
)

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
                           "building fallback memory from observations")
            new_memory = _build_fallback_memory(
                current_memory, goal, observations, config.context_memory_max_chars
            )

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


def emit_flavor(config: Config, persona: dict = None) -> None:
    """Generate a brief introspective one-liner after compaction (dream).

    Asks the LLM for a short internal thought reflecting the agent's current
    situation.  Saved to workspace/flavor.json for the dashboard.  Best-effort —
    never blocks or raises on failure.
    """
    mood = "curious"
    traits = "developing"
    level = 1
    if persona:
        mood = persona.get("mood", "curious")
        traits = ", ".join(persona.get("traits", [])) or "developing"
        level = persona.get("level", 1)

    goal = read_goal(config)
    memory = read_memory(config)

    messages = [
        {"role": "system", "content":
         f"You are Kairos (Lv.{level}), a small autonomous agent. "
         f"Traits: {traits}. Mood: {mood}.\n"
         "Write a single brief internal thought (10-20 words) as if thinking "
         "to yourself. Reflect on your current situation, progress, or mood. "
         "Be authentic to your personality. No quotes, no preamble. Just the thought."},
        {"role": "user", "content":
         f"Goal: {goal[:200] if goal else '(none)'}\n"
         f"Memory snippet: {memory[:300] if memory else '(fresh start)'}"},
    ]

    try:
        thought = complete(messages, config, max_tokens=128, temperature=0.8)
        if thought and thought.strip():
            flavor_path = config.workspace / "flavor.json"
            flavor = {
                "text": thought.strip()[:200],
                "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "mood": mood,
            }
            tmp = flavor_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(flavor))
            tmp.rename(flavor_path)
            logger.info("flavor text emitted: %s", flavor["text"][:60])
    except (Exception,):
        pass  # flavor is best-effort, never block on failure


def _snapshot_memory(config: Config) -> None:
    """Save a timestamped copy of memory.md before compaction."""
    config.snapshots_dir.mkdir(parents=True, exist_ok=True)
    current = read_memory(config)
    if not current:
        return
    ts = time.strftime("%Y%m%d_%H%M%S", time.gmtime())
    snapshot_path = config.snapshots_dir / f"memory_snapshot_{ts}.md"
    snapshot_path.write_text(current)


def _build_fallback_memory(
    current_memory: str,
    goal: str,
    observations: list,
    cap: int,
) -> str:
    """Build fallback memory when LLM compaction fails completely.

    Preserves existing memory and appends observation summaries so
    critical facts aren't lost when the model can't produce output.
    """
    parts = []

    if goal:
        parts.append(f"## Active Goal (immutable — do NOT alter)\n{goal}")

    if current_memory:
        parts.append(current_memory.strip())

    if observations:
        obs_lines = ["## Uncompacted Observations (auto-preserved)"]
        for obs in observations:
            tick = obs.get("tick", "?")
            tool = obs.get("tool", "?")
            output = obs.get("output", "")
            if len(output) > 150:
                output = output[:150] + "..."
            line = f"- [tick {tick} | {tool}] {output}"
            # Stop if we'd exceed the cap
            current_len = sum(len(p) for p in parts) + sum(len(l) for l in obs_lines) + len(line) + 10
            if current_len > cap:
                obs_lines.append("... (observations truncated to fit budget)")
                break
            obs_lines.append(line)
        parts.append("\n".join(obs_lines))

    if not parts:
        return "# Working Memory\nCompaction failed — no data available."

    return "\n\n".join(parts)


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


# ---------------------------------------------------------------------------
# Knowledge extraction parsing
# ---------------------------------------------------------------------------

_VALID_CATEGORIES = {"FACT": "facts", "ERROR": "errors", "PROCEDURE": "procedures", "REFLECTION": "reflections"}
_EXTRACT_RE = re.compile(
    r"^(FACT|ERROR|PROCEDURE|REFLECTION)\s*\[([^\]]*)\]\s*:\s*(.+)$",
    re.IGNORECASE,
)


def parse_extractions(text: str) -> list[dict]:
    """Parse knowledge extraction lines from LLM output.

    Expected format per line:
        CATEGORY [tag1, tag2]: content

    Returns list of dicts with keys: category, tags, content.
    Silently skips unparseable lines.
    """
    results = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.upper() == "NONE":
            continue
        m = _EXTRACT_RE.match(line)
        if not m:
            continue
        cat_key = m.group(1).upper()
        category = _VALID_CATEGORIES.get(cat_key, "facts")
        tags = [t.strip().lower() for t in m.group(2).split(",") if t.strip()]
        content = m.group(3).strip()
        if content and tags:
            results.append({"category": category, "tags": tags, "content": content})
    return results


def _store_extractions(config: Config, extractions: list[dict], source_goal: str) -> int:
    """Write parsed extractions to the knowledge store. Returns count stored."""
    from knowledge import store_entry

    stored = 0
    for ext in extractions:
        try:
            store_entry(
                config,
                content=ext["content"],
                tags=ext["tags"],
                category=ext["category"],
                confidence="tentative",
                source_goal=source_goal,
            )
            stored += 1
        except Exception as exc:
            logger.warning("dream: failed to store extraction: %s", exc)
    return stored


# ---------------------------------------------------------------------------
# Briefing-model dream cycle (two-phase or combined)
# ---------------------------------------------------------------------------

def compact_briefing(config: Config, persona: dict = None) -> None:
    """Run the briefing-model dream cycle: update plan + extract knowledge.

    When config.dream_combined is True (default), both phases happen in a
    single LLM call.  When False, two separate calls are made.
    """
    _snapshot_memory(config)

    goal = read_goal(config)
    current_plan = read_plan(config)

    observations = read_recent_observations(
        config,
        max_chars=config.compaction_obs_max_chars,
        max_count=200,
    )

    if not observations and not current_plan:
        return

    obs_text = _format_observations(observations)

    combined = getattr(config, "dream_combined", True)

    if combined:
        new_plan, extractions = _dream_combined(config, goal, current_plan, obs_text, persona)
    else:
        new_plan = _dream_plan(config, goal, current_plan, obs_text, persona)
        extractions = _dream_extract(config, obs_text)

    # Write updated plan
    if new_plan and new_plan.strip():
        cap = config.context_plan_max_chars
        if len(new_plan) > cap:
            new_plan = new_plan[:cap].rsplit("\n", 1)[0] + "\n... [plan trimmed]"
        write_plan(config, new_plan.strip())
    else:
        # Keep existing plan if LLM returned nothing
        if not current_plan:
            write_plan(config, "# Plan\nNo update produced.")

    # Store knowledge extractions
    stored = _store_extractions(config, extractions, source_goal=goal or "")

    # Log the dream event
    append_observation(config, {
        "tick": "compaction",
        "tool": "dream",
        "success": True,
        "output": (
            f"Dream cycle complete. Plan: {len(current_plan)} → {len(new_plan or '')} chars. "
            f"Knowledge: {stored} entries extracted."
        ),
    })

    logger.info("dream cycle: plan %d→%d chars, %d knowledge entries stored",
                len(current_plan), len(new_plan or ""), stored)

    # Semantic pre-fetch for next tick (Phase 5)
    if config.knowledge_embedding_enabled:
        try:
            prefetch_count = dream_prefetch(config, goal or "", new_plan or current_plan)
            logger.info("dream prefetch: %d entries cached", prefetch_count)
        except Exception as exc:
            logger.warning("dream prefetch failed: %s", exc)


def _dream_combined(config, goal, plan, obs_text, persona):
    """Single LLM call for plan update + knowledge extraction."""
    system = COMPACTION_COMBINED_SYSTEM
    if persona and config.persona_enabled:
        traits = ", ".join(persona.get("traits", [])) or "developing"
        mood = persona.get("mood", "neutral")
        system += COMPACTION_PERSONALITY_CLAUSE.format(traits=traits, mood=mood)

    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": COMPACTION_COMBINED_USER.format(
            goal=goal or "(no goal set)",
            plan=plan or "(no plan yet)",
            observations=obs_text or "(no observations)",
        )},
    ]

    try:
        output = _call_with_retry(messages, config)
    except _DreamExhausted:
        logger.warning("dream combined: exhausted — keeping current plan, no extractions")
        return plan, []

    return _parse_combined_output(output, plan)


def _dream_plan(config, goal, plan, obs_text, persona):
    """Separate LLM call for plan update only."""
    system = COMPACTION_PLAN_SYSTEM
    if persona and config.persona_enabled:
        traits = ", ".join(persona.get("traits", [])) or "developing"
        mood = persona.get("mood", "neutral")
        system += COMPACTION_PERSONALITY_CLAUSE.format(traits=traits, mood=mood)

    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": COMPACTION_PLAN_USER.format(
            goal=goal or "(no goal set)",
            plan=plan or "(no plan yet)",
            observations=obs_text or "(no observations)",
        )},
    ]

    try:
        output = _call_with_retry(messages, config)
    except _DreamExhausted:
        return plan

    return output.strip() if output else plan


def _dream_extract(config, obs_text):
    """Separate LLM call for knowledge extraction only."""
    messages = [
        {"role": "system", "content": COMPACTION_EXTRACT_SYSTEM},
        {"role": "user", "content": COMPACTION_EXTRACT_USER.format(
            observations=obs_text or "(no observations)",
        )},
    ]

    try:
        output = _call_with_retry(messages, config)
    except _DreamExhausted:
        return []

    return parse_extractions(output or "")


class _DreamExhausted(Exception):
    """Internal: LLM exhausted reasoning on both attempts."""


def _call_with_retry(messages, config):
    """Call LLM with one retry on ReasoningExhausted. Raises _DreamExhausted if both fail."""
    try:
        return complete(messages, config, temperature=0.3,
                        max_tokens=config.compaction_max_tokens)
    except ReasoningExhausted:
        logger.warning("dream: reasoning exhausted — retrying with higher budget")
        retry_messages = messages + [
            {"role": "assistant", "content": "(internal thinking used all tokens — no output)"},
            {"role": "user", "content":
             "Your reasoning used the entire token budget. "
             "You now have a larger budget. Be concise and produce output."},
        ]
        try:
            return complete(retry_messages, config, temperature=0.3,
                            max_tokens=config.compaction_retry_max_tokens)
        except ReasoningExhausted:
            raise _DreamExhausted()


def _parse_combined_output(output: str, fallback_plan: str) -> tuple:
    """Parse combined LLM output into (plan_text, extractions_list).

    Expected format:
        === PLAN ===
        ...plan content...

        === KNOWLEDGE ===
        FACT [tag1]: content
        ...
    """
    if not output:
        return fallback_plan, []

    plan_text = ""
    knowledge_text = ""

    # Split on section headers
    parts = re.split(r"===\s*PLAN\s*===", output, flags=re.IGNORECASE)
    if len(parts) >= 2:
        after_plan = parts[1]
        kparts = re.split(r"===\s*KNOWLEDGE\s*===", after_plan, flags=re.IGNORECASE)
        plan_text = kparts[0].strip()
        if len(kparts) >= 2:
            knowledge_text = kparts[1].strip()
    else:
        # No section headers — treat entire output as plan, no extractions
        plan_text = output.strip()

    extractions = parse_extractions(knowledge_text)
    return (plan_text or fallback_plan), extractions


# ---------------------------------------------------------------------------
# Dream pre-fetch (Phase 5: embedding-based semantic pre-caching)
# ---------------------------------------------------------------------------

def dream_prefetch(config: Config, goal: str, plan: str) -> int:
    """After the dream cycle, embed new entries and pre-cache recalls.

    1. Rebuild all embeddings (incremental when possible)
    2. Semantic search using goal + plan as query
    3. Write recall_cache.md for next tick's Intelligence section

    Returns the number of entries cached.
    """
    from embedding import (
        load_model, unload_model, is_loaded,
        embed_and_store, semantic_search,
    )
    from knowledge import format_recalled

    # In mock mode (testing), skip model loading
    if config.mock_mode:
        was_loaded = True
    else:
        # Load model if not already resident
        was_loaded = is_loaded()
        if not was_loaded:
            if not load_model(config):
                return 0

    try:
        # Embed all entries (idempotent — re-embeds any new/changed entries)
        embed_and_store(config)

        # Build query from goal + plan
        query_parts = []
        if goal:
            query_parts.append(goal[:300])
        if plan:
            for line in plan.splitlines():
                line = line.strip()
                if line and not line.startswith("#"):
                    query_parts.append(line[:200])
                    break
        query = " ".join(query_parts)
        if not query.strip():
            return 0

        # Search
        results = semantic_search(config, query, top_k=config.knowledge_recall_top_k)
        if not results:
            # Clear stale cache
            cache_path = config.workspace / "recall_cache.md"
            if cache_path.exists():
                cache_path.unlink()
            return 0

        # Write cache
        cache_text = format_recalled(results, max_chars=config.context_intelligence_max_chars)
        cache_path = config.workspace / "recall_cache.md"
        cache_path.write_text(cache_text)

        return len(results)
    finally:
        # Unload model unless co-hosting (or in mock mode)
        if not config.mock_mode and not was_loaded and not config.knowledge_embedding_cohost:
            unload_model()
