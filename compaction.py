"""Memory compaction (/dream) — consolidate observations into working memory.

Uses a generous context budget so the LLM has full visibility of observations
when distilling them into concise working memory.

Two modes:
- Briefing: two-phase dream cycle (plan update + knowledge extraction)
"""

import json
import logging
import re
import time

from config import Config
from atomicio import replace_with_retry
from memory import (
    read_goal,
    read_plan,
    write_plan,
    read_recent_observations,
    count_observation_chars,
    truncate_observations,
    append_observation,
)
from llm import complete, ReasoningExhausted
from prompts import (
    COMPACTION_PERSONALITY_CLAUSE,
    COMPACTION_PLAN_SYSTEM,
    COMPACTION_PLAN_USER,
    COMPACTION_EXTRACT_SYSTEM,
    COMPACTION_EXTRACT_USER,
    COMPACTION_COMBINED_SYSTEM,
    COMPACTION_COMBINED_USER,
)

logger = logging.getLogger("eidos.compaction")


def should_compact(config: Config, ticks_since_last: int) -> bool:
    """Check if compaction should run based on thresholds."""
    # Token threshold (approximated by char count)
    if count_observation_chars(config) >= config.compaction_token_threshold:
        return True
    # Tick count threshold
    if ticks_since_last >= config.compaction_tick_threshold:
        return True
    return False



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
    memory = read_plan(config)

    messages = [
        {"role": "system", "content":
         f"You are eiDOS (Lv.{level}), a small autonomous agent. "
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
            replace_with_retry(tmp, flavor_path)
            logger.info("flavor text emitted: %s", flavor["text"][:60])
    except (Exception,):
        pass  # flavor is best-effort, never block on failure


def _snapshot_memory(config: Config) -> None:
    """Save a timestamped copy of plan.md (working memory) before the dream rewrites it."""
    config.snapshots_dir.mkdir(parents=True, exist_ok=True)
    current = read_plan(config)
    if not current:
        return
    ts = time.strftime("%Y%m%d_%H%M%S", time.gmtime())
    snapshot_path = config.snapshots_dir / f"plan_snapshot_{ts}.md"
    snapshot_path.write_text(current)


def _write_dream_record(config: Config, old_plan: str, new_plan: str,
                        stored: int, removed: int) -> None:
    """Write a human-readable dream record the Dream Journal can show. Captures the real output of
    the briefing dream cycle: the flavor reflection, what was distilled, and the resulting plan."""
    config.snapshots_dir.mkdir(parents=True, exist_ok=True)
    ts = time.strftime("%Y%m%d_%H%M%S", time.gmtime())

    # The flavor line is eiDOS's poetic self-reflection for this moment (emit_flavor → flavor.json).
    flavor = ""
    try:
        fj = json.loads((config.workspace / "flavor.json").read_text(encoding="utf-8"))
        flavor = (fj.get("text") or "").strip()
    except Exception:  # noqa: BLE001
        pass

    # The freshest reflections this dream extracted (newest knowledge of category 'reflections').
    learned_lines = []
    try:
        import knowledge as _kn
        for e in _kn.recent_learned(config, limit=4):
            prev = (e.get("content_preview") or e.get("content") or "").strip().replace("\n", " ")
            if prev:
                learned_lines.append(f"- {prev[:140]}")
    except Exception:  # noqa: BLE001
        pass

    parts = [f"# Dream @ {ts}"]
    if flavor:
        parts.append(f"_{flavor}_")
    parts.append(
        f"Distilled {removed} observation(s) → {stored} new knowledge entr"
        f"{'y' if stored == 1 else 'ies'}. Plan {len(old_plan)} → {len(new_plan)} chars.")
    if learned_lines:
        parts.append("**Recently learned:**\n" + "\n".join(learned_lines))
    if new_plan.strip():
        parts.append("**Plan after dreaming:**\n" + new_plan.strip()[:500])

    record = "\n\n".join(parts)
    path = config.snapshots_dir / f"dream_{ts}.md"
    tmp = path.with_suffix(".md.tmp")
    tmp.write_text(record, encoding="utf-8")
    replace_with_retry(tmp, path)



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
        with open(path, "a", encoding="utf-8") as f:
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

    # Truncate observations — they've been distilled into plan/knowledge.
    # Without this, the file grows forever and should_compact() fires every tick.
    removed = truncate_observations(config)
    logger.info("dream: truncated %d observations after distillation", removed)

    # Log the dream event (this goes into the now-clean file)
    append_observation(config, {
        "tick": "compaction",
        "tool": "dream",
        "success": True,
        "output": (
            f"Dream cycle complete. Plan: {len(current_plan)} → {len(new_plan or '')} chars. "
            f"Knowledge: {stored} entries extracted. Cleared {removed} observations."
        ),
    })

    logger.info("dream cycle: plan %d→%d chars, %d knowledge entries stored",
                len(current_plan), len(new_plan or ""), stored)

    # Write a human-readable DREAM RECORD for the dashboard's Dream Journal. The old journal read
    # plan snapshots: the briefing dream distills into plan + knowledge, so
    # those snapshots were empty 48-byte stubs and the journal showed nothing. Capture the real
    # distillation here: the flavor reflection + what was learned + the resulting plan.
    try:
        _write_dream_record(config, current_plan, new_plan or "", stored, removed)
    except Exception as exc:  # noqa: BLE001 - journaling must never disturb the dream
        logger.warning("dream record write failed: %s", exc)

    # The dream is self-bounding: prune snapshots + dream records in the same cycle
    # that creates them, so growth never depends on a separate caller's sweep.
    try:
        from rotation import cleanup_old_snapshots
        cleanup_old_snapshots(config)
    except Exception as exc:  # noqa: BLE001 - pruning must never disturb the dream
        logger.warning("snapshot prune failed: %s", exc)



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
