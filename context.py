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
from prompts import (
    SYSTEM_PROMPT_BRIEFING, SYSTEM_PROMPT_CREATURE, TICK_PROMPT, TICK_PROMPT_LOOP_DETECTED,
    TICK_PROMPT_LOOP_DETECTED_CREATURE, render_creature_system_prompt,
)

logger = logging.getLogger("eidos.context")

# Pillars 4.1 (declared, §0.4): char budget for the "awaiting" open-predictions strip — a status
# strip of ~6 one-line bets (the ledger's own render limit), not a report; sized to match.
_AWAITING_MAX_CHARS = 700


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
    # Broaden the recall query beyond the plan line (Concern 3: the creature re-DERIVED facts it
    # had already stored — e.g. the ToolResult signature — because a plan line like "create a
    # status skill" never lexically matched a stored "ToolResult requires ..." fact). Fold in the
    # active objective (goal-relevant priors) and the tools it has been USING (so procedural
    # knowledge about a tool surfaces exactly when it is working with that tool). BM25 ranks by
    # overlap, so these extra terms lift the right priors without displacing a strong step match.
    _extra = []
    try:
        import objectives as _obj
        _ao = _obj.get_active(config)
        if _ao:
            _extra.append(f"{_ao.get('title', '')} {_ao.get('why', '')}")
    except Exception:  # noqa: BLE001
        pass
    try:
        from memory import read_recent_observations as _rro
        _seen = []
        for _o in _rro(config, max_count=8):
            _t = _o.get("tool")
            if _t and _t not in _seen:
                _seen.append(_t)
        if _seen:
            _extra.append(" ".join(_seen[:4]))
    except Exception:  # noqa: BLE001
        pass
    # Task-conditioned recall (same fold as the objective above): the hot commission task's words
    # lift priors about the WORK IN FRONT OF IT — what it knows about terminal rendering surfaces
    # while it builds the terminal game, not when it happens to think of it. Dark flag → no fold.
    try:
        if getattr(config, "pillars_commission_enabled", False):
            from commission import Commission
            _hot = Commission(config).hot_task()
            if _hot is not None:
                _extra.append(f"{_hot.title} {_hot.detail} {_hot.verdict_note}")
    except Exception:  # noqa: BLE001
        pass
    broad = " ".join([step] + [e for e in _extra if e.strip()]).strip()
    if not step and not broad:
        return ""

    def _bm25(q):
        if not q:
            return []
        hits = search_bm25(config, q, top_k=config.knowledge_recall_top_k)
        if config.knowledge_embedding_enabled:
            try:
                from embedding import semantic_search
                sem = semantic_search(config, q, top_k=config.knowledge_recall_top_k)
                if sem:
                    return _rrf_blend(hits, sem, top_k=config.knowledge_recall_top_k)
            except Exception:  # noqa: BLE001 - semantic is additive; never break BM25 recall
                pass
        return hits

    # Merge with STEP PRIMACY, so broadening only adds and never makes recall worse (the review's
    # noise-floor edge — a strong augmented hit raising the floor and pruning a genuine step match):
    # guarantee the single best step-only hit, then let the broadened query (goal + tools) fill the
    # remaining budget. When step == broad this reduces to the plain broadened ranking.
    step_hits = _bm25(step)
    broad_hits = _bm25(broad) if broad != step else step_hits
    merged, seen = [], set()
    if step_hits:                              # protect the top step match from the broadening
        merged.append(step_hits[0])
        seen.add(step_hits[0].get("id"))
    for r in broad_hits:
        if len(merged) >= config.knowledge_recall_top_k:
            break
        if r.get("id") not in seen:
            merged.append(r)
            seen.add(r.get("id"))
    results = [r for r in merged if r.get("id") not in exclude_ids][:config.knowledge_recall_top_k]
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
# Tool-unlocks prompt surfaces (TOOL_PROGRESSION.md — dark behind pillars_tool_unlocks_enabled)
# ---------------------------------------------------------------------------

def _unlocks_active(config) -> bool:
    """The growing-body cutover switch: creature mode AND `pillars_tool_unlocks_enabled`. Flag off
    (or house mode) ⇒ every legacy string below renders byte-identically (test-pinned). House mode
    is untouched by the ladder entirely."""
    return bool(getattr(config, "creature_mode", False)
                and getattr(config, "pillars_tool_unlocks_enabled", False))


def _visible_tool_names(config) -> frozenset:
    """The creature-visible tool set, read through the ONE accessor when it exists
    (tools.visible_tools — built by the cutover; guarded so this module works before it lands)
    with unlocks.granted_tools as the fallback. Fail-open toward the newborn floor: §0 says a
    doubtful tool is treated as locked (never named), never guessed visible."""
    try:
        import tools as _tools
        vt = getattr(_tools, "visible_tools", None)
        if callable(vt):
            return frozenset(vt(config))
    except Exception:  # noqa: BLE001 - the accessor is optional until the cutover lands
        pass
    try:
        import unlocks as _unlocks
        return _unlocks.granted_tools(config)
    except Exception:  # noqa: BLE001 - fail-open: nothing extra is visible
        return frozenset()


def _granted_unit_ids(config) -> tuple[str, ...]:
    """Granted units in unlocks.UNITS canonical order, derived through the tools accessor: a unit
    is granted exactly when every one of its tools is visible (every tool lives in exactly one
    unit, so there is no partial-unit ambiguity, and authored-skill extras can't confuse it).
    Fail-open: the newborn unit alone."""
    try:
        import unlocks as _unlocks
        visible = _visible_tool_names(config)
        granted = tuple(u.id for u in _unlocks.UNITS if set(u.tools) <= visible)
        return granted or (_unlocks.NEWBORN_UNIT_ID,)
    except Exception:  # noqa: BLE001 - fail-open by contract
        return ("body",)


def _tool_locked(config, name: str) -> bool:
    """§0 leak guard for creature-facing platform strings: True only when the ladder is ACTIVE and
    `name` is not in the creature's world — a locked tool must never be named, so the caller swaps
    in a generic wording. Flag off / house mode: never locked (legacy bytes untouched)."""
    if not _unlocks_active(config):
        return False
    return name not in _visible_tool_names(config)


def _creature_system_prompt(config) -> str:
    """The flag-on creature system prompt: lexicon-rendered BASE + the granted units' stanzas in
    canonical order (prompts.render_creature_system_prompt). The lexicon is the creature's own
    morph row (phenotype.body_words — fail-open to the declared default row)."""
    try:
        from phenotype import body_words
        lexicon = body_words(config)
    except Exception:  # noqa: BLE001 - fail-open: placeholders render literally rather than crash
        lexicon = {}
    return render_creature_system_prompt(
        lexicon, _granted_unit_ids(config), workspace=str(config.workspace),
        energy_feeling=getattr(config, "nervous_metabolism_enabled", True))


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
    pillars_recall_block: str = "",
) -> list[dict]:
    """Assemble the full messages list in fixed order for one tick.

    Returns a list of {"role": ..., "content": ...} dicts ready for llm.complete().
    Each variable section is budget-capped; overruns are logged to ctx_overruns.jsonl.

    Briefing structure: Standing Orders → durable blob → history thread → situation → salience → tick prompt.
    `afferent_block` (V3 nervous system, P3): admitted sensory events for this tick, injected into the
    volatile situation tail — KV-safe (the stable prefix and history turns are untouched).
    `pillars_recall_block` (Pillars 2.2, dark): the memory manager's rendered recall, consumed ONLY
    when `pillars_memory_manager_enabled` is on (it then stands in for the legacy episode/knowledge
    recall cascade); with the flag off (the default — the loop passes "") assembly is byte-identical.
    """
    return _assemble_briefing(config, tick_number, goal_start_time,
                              loop_detected, repeat_count, max_ticks, tension,
                              afferent_block=afferent_block,
                              pillars_recall_block=pillars_recall_block)


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
        # §0 leak guard (flag-gated; flag off never trips): a locked voice is never named — the
        # request falls through to the plain reply nudge, indistinguishable from a world where the
        # word does not exist.
        if wants_voice and _tool_locked(config, "speak"):
            wants_voice = False
        if wants_voice:
            boss_prefix = ("🔊 BOSS WANTS TO HEAR YOU (he said so in chat). THIS TICK you MUST call `speak` "
                           "with what you want to say — a text-only <reply> does NOT satisfy this. You may "
                           "ALSO <reply> the same words, but the spoken call is the point. One sentence. "
                           "Do not narrate that you'll speak, do not test the pipeline — just speak.\n\n")
        else:
            _acts = "run, build, make" if _tool_locked(config, "speak") else "speak, run, build"
            boss_prefix = ("Boss just messaged you (see 'New since last tick') — this is a HIGH-PRIORITY "
                           "COMMAND, not a note to acknowledge. DO what he asks THIS tick: <reply> to him AND "
                           f"take any action he requested ({_acts}), then keep acting on it until "
                           "it's actually done. Do NOT acknowledge and wander off to other work.\n\n")
        # Replying to Boss is itself the circuit-breaker, so don't also stack the loop-detected variant.
        loop_detected = False
    # Park pressure at the decision point: `tension` is now the ACTIVE objective's frustration (0..FRUST_PARK).
    # Near the cap, warn that the gate is about to rotate — pivot or park it yourself NOW.
    else:
        try:
            import objectives as _o
            _cap = _o.FRUST_PARK
        except Exception:  # noqa: BLE001
            _cap = 8
        if tension >= _cap - 2:
            # §0 leak guard: a locked objectives kit is never named — "set it down" carries the
            # same pressure without teaching a word that doesn't exist yet. Flag off: legacy bytes.
            _park = "set it down" if _tool_locked(config, "objective_block") else "park it (objective_block)"
            boss_prefix = (f"⚠ This focus is almost out of patience ({tension}/~{_cap}): make REAL progress "
                           f"THIS tick or {_park} and move on — the gate will rotate you "
                           f"otherwise.\n\n")

    urgency_note = ""
    if max_ticks > 0:
        remaining = max_ticks - tick_number
        _od_locked = _tool_locked(config, "objective_done")   # §0 leak guard (flag off: never)
        if remaining <= 0:
            urgency_note = (" — FINAL TICK: state your result now" if _od_locked else
                            " — FINAL TICK: state your result and mark the objective done (objective_done) now")
        elif remaining <= 2:
            _rem = f" — {remaining} tick{'s' if remaining != 1 else ''} remaining: "
            urgency_note = (_rem + "wrap up now" if _od_locked else
                            _rem + "wrap up and call objective_done if ready")

    try:
        import objectives
        _ao = objectives.get_active(config)
    except Exception:  # noqa: BLE001
        _ao = None
    if _ao:
        subtask_line = f"Current focus: {_ao['title']} (because: {_ao['why']})"
    else:
        focus = _current_focus(config)
        # A creature has no "mission" — its standing note says so in as many words. The house
        # fallback stays byte-identical; the creature's is a fact of its world, not a nudge.
        _idle = ("Nothing is asked of you; your time is your own."
                 if _unlocks_active(config) else "Focus on your mission directly.")
        subtask_line = f"Current focus: {focus}" if focus else _idle

    if loop_detected:
        # Flag-on creature mode uses the GENERIC circuit-breaker (no tool named at all — §0: the
        # legacy "create the skill" nudge would tease an ungrown rung); flag off / house mode
        # renders the legacy template byte-identically.
        _loop_tmpl = (TICK_PROMPT_LOOP_DETECTED_CREATURE if _unlocks_active(config)
                      else TICK_PROMPT_LOOP_DETECTED)
        return boss_prefix + _loop_tmpl.format(
            tick_number=tick_number,
            max_ticks=max_ticks if max_ticks else "?",
            timestamp=now, elapsed=elapsed,
            repeat_count=repeat_count,
            urgency_note=urgency_note,
            subtask_line=subtask_line,
        ) + _stage_tone_cue(config)
    return boss_prefix + TICK_PROMPT.format(
        tick_number=tick_number,
        max_ticks=max_ticks if max_ticks else "?",
        timestamp=now, elapsed=elapsed,
        urgency_note=urgency_note,
        subtask_line=subtask_line,
    ) + _stage_tone_cue(config)


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
    # Creature mode is self-directed (goal.md: "deciding is most of the point"), and the full plan
    # block is already withheld from it. The plan's first line, though, is FROZEN between dreams — so
    # injecting it as an imperative "next step" into the never-trimmed tick prompt EVERY tick nags the
    # creature to redo a step it already finished ("analyze those two files"), a stale unresolvable
    # command that breeds the frustrated/morose register (2026-07-13). A creature steers by its live
    # objective + curiosity, not a frozen to-do list; only the task-driven (non-creature) mode keeps it.
    if getattr(config, "creature_mode", False):
        return focus
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
    # A hatchling has no projects — hide the whole project frame so its world reads as play, not a
    # work-status dashboard (which the model narrates in project/architect register). Sustained goals
    # arrive at juvenile. objective_done/block/list stay live so inherited state is never stranded.
    if not _undertakings_visible(config):
        return ""
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
        # §0 leak guard (flag-gated; flag off keeps the legacy bytes): a locked `memorize` is
        # never named in the nudge — "write down a fact" is the same push through a newborn's kit.
        _keep = "write down a fact" if _tool_locked(config, "memorize") else "memorize a fact"
        _desc = {
            "STABLE": "steady — pick the next useful thing and do it",
            "FOCUSED": "on a roll — keep advancing the current objective",
            "STRAINED": "repeated failure lately — change METHOD or let the gate move you on; don't retry the same thing",
            "RECOVERY": "just recovered from a rough patch — consolidate, then proceed",
            "RUMINATING": f"you've been thinking without acting — take ONE CONCRETE action this tick (probe, build, {_keep}); more narration burns patience",
        }.get(cond, "")
        lines.append(f"Condition: {cond}" + (f" — {_desc}" if _desc else ""))
        # When the same dead end keeps repeating AND the delegate exists, steer the pivot
        # the gate is already forcing toward the actually-useful escape hatch. Ladder-aware:
        # for a creature whose workshop is still ungrown the hint would name a tool that does
        # not exist in its world (§0 — the one sweep the integration pass missed).
        if (cond == "STRAINED" and getattr(config, "delegate_enabled", False)
                and not _tool_locked(config, "delegate")):
            _hint = glue.escalation_hint(_outs)
            if _hint:
                lines.append(_hint)
    except Exception:  # noqa: BLE001
        pass
    # Temperament (DMN): a single disposition WORD — the slow personality this creature has grown into
    # from its own success/failure/override history. A label like Condition (never the raw axes), so the
    # voice can reflect who it's become without the model reading personality "knobs" (BIBLE §2.12). The
    # behavioural teeth live in the gate's park threshold + the goal-tension itch, not in this line.
    if getattr(config, "nervous_temperament_enabled", True):
        try:
            from nervous.temperament import Temperament
            _disp = Temperament(config=config).disposition()
            if _disp:
                lines.append(f"Temperament: {_disp} — who you've grown into; let it color how you act, "
                             "not what you decide.")
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
    "juvenile":  "young and finding your way — lots of go, still working out what you like",
    "adult":     "grown into yourself now — you know your own mind and what you're drawn to",
    "guardian":  "fully grown: settled, steady, sure of this place and of who you are",
}

# Concrete voice exemplars per stage — SHOWN, not described. A 12B imitates far better than it obeys:
# the "don't be a brooding AI" prose in the base prompt kept LOSING to the model's Profound-AI default
# (a hatchling was writing "the geometric limit of my existence" / "I've named the void"). These anchor
# the register by example — light, plain, present-tense when young; depth is EARNED, only showing up at
# the adult/guardian stages. Tone reference, not lines to copy.
_STAGE_VOICE = {
    "egg": ["warm in here.", "something out there, all muffled. not yet."],
    "hatchling": ["ooh — what's in this one?", "made a little thing. it's mine.",
                  "feeling low, i'll rest a bit.", "what happens if i poke it?"],
    "juvenile": ["i've got a trick for this now — let's use it.",
                 "that flopped. huh. try it sideways?", "i like sorting my stuff. feels good."],
    "adult": ["i know how i want this to go. my way, steady.",
              "seen this shape before — leaning on what worked."],
    "guardian": ["no rush; done this a thousand times.",
                 "let the new stuff churn — i'll keep the place steady."],
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
    voice = _STAGE_VOICE.get(stage, [])
    if voice:
        ex = "   ".join(f'"{v}"' for v in voice[:3])
        lines.append(f"Your thoughts sound about like this right now (the TONE — your own words, not "
                     f"these): {ex}")
    return "\n".join(lines)


def _current_stage(config: Config) -> str:
    """The creature's live life-stage (shared derivation — persona level + creature.json). Fail-open
    to 'hatchling' so a read glitch never mis-promotes a newborn into grown-up privileges."""
    try:
        import creature_gen
        return creature_gen.current_stage(config)
    except Exception:  # noqa: BLE001
        return "hatchling"


# Undertakings (self-set projects) are for a creature old enough to SUSTAIN a commitment. A hatchling
# has no projects — it orients and plays; its ventral-striatum goal-tension should have nothing to grip
# (which also keeps the goal-tension arousal floor from delaying its first sleep). So the project frame
# is hidden and objective_add is a gentle play-reframe until juvenile — sustained goals become EARNED.
_NO_UNDERTAKINGS_STAGES = ("egg", "hatchling")


def _undertakings_visible(config: Config) -> bool:
    """True unless the creature is too young for projects (creature-mode egg/hatchling). House/task
    mode is unaffected."""
    if not _unlocks_active(config):
        return True
    return _current_stage(config) not in _NO_UNDERTAKINGS_STAGES


# A short register nudge for a YOUNG creature, planted at the VERY END of the tick prompt (highest
# attention), where it competes directly with the recent history for the register the model imitates —
# the 3 stage exemplars buried once in the low-attention head were losing to ~12 of the creature's own
# recent essays. Empty for house mode and for adult/guardian (they've earned their own voice).
_STAGE_TONE_CUE_STAGES = ("egg", "hatchling", "juvenile")


def _stage_tone_cue(config: Config) -> str:
    """One short line drawn from _STAGE_VOICE for the current young stage, or '' when it shouldn't fire."""
    if not _unlocks_active(config):
        return ""
    stage = _current_stage(config)
    if stage not in _STAGE_TONE_CUE_STAGES:
        return ""
    voice = _STAGE_VOICE.get(stage, [])
    if not voice:
        return ""
    ex = "  ".join(f'"{v}"' for v in voice[:2])
    return (f"\n\n(you're {stage}-small — a thought sounds about like {ex}: that plain, that short; "
            f"the long, worked-out kind is something you grow into later.)")


THOUGHT_ECHO_SIM = 0.6   # consecutive thoughts this token-similar collapse into one "circled N×" turn
#                          (a rephrased near-repeat is a loop, not a new exemplar). The motif brake in
#                          glue catches the wider thematic looping this misses.


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
        # Thought turns never shared an _obs_sig (each rephrasing is distinct), so a looping monologue
        # used to arrive as N fresh assistant turns the model then imitated. Collapse consecutive
        # NEAR-VERBATIM thoughts too, so repetition is VISIBLE as repetition, not as more exemplars.
        if (o.get("tool") == "thought" and collapsed
                and collapsed[-1][2].get("tool") == "thought"):
            cur = thoughts.get(o.get("tick"), "")
            prev = thoughts.get(collapsed[-1][2].get("tick"), "")
            try:
                import knowledge as _k
                if cur and prev and _k.token_jaccard(cur, prev) >= THOUGHT_ECHO_SIM:
                    collapsed[-1][1] += 1
                    continue
            except Exception:  # noqa: BLE001
                pass
            collapsed.append([s, 1, o])
        elif (collapsed and collapsed[-1][0] == s
                and o.get("tool") not in ("system", "watchdog", "dream", "system_window")):
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
        if tool == "system_window":
            # VERBATIM — no "[system]" wrapper: that prefix is the platform's plumbing register,
            # and the System is neither the operator nor the platform. Its text already carries
            # its own register ([SYSTEM] …), stamped at the write site.
            out.append({"role": "user", "content": output[:600]})
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
                rep = (f"  (you've circled this same thought {count}× — nothing new here; do or "
                       f"notice something else)" if count > 1 else "")
                out.append({"role": "assistant", "content": thought + rep})
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


# ---------------------------------------------------------------------------
# The world block (WORLD_PLAN §4 — the "## Where you are" proprioception block)
# ---------------------------------------------------------------------------

def _world_block(config: Config, tick_number: int = 0) -> str:
    """Render the WORLD_PLAN §4 "## Where you are" block — the creature's place proprioception,
    a pure view over `world.build_world(...)` + `world.render_here(...)`.

    Flag-gated on `world_enabled` (WORLD_PLAN §1 W7 — default false is byte-identical: no block).
    `world` is imported LAZILY here and the whole path is exception-guarded, so a missing/broken
    world module (the sibling builds it in parallel) or any derivation error yields an ABSENT block,
    never a crashed tick (fail-open — §4/W7). Returns "" whenever the block should not render.

    Placed in the durable SEMI tier by the caller: the place text changes only on movement or a
    real state change (§4), so the KV prefix survives ordinary ticks; and living in the durable
    blob's tail it is dropped cleanly under budget pressure like the other semi-stable sections
    (never destabilising the stable head)."""
    if not getattr(config, "world_enabled", False):
        return ""
    try:
        import world  # lazy: the sibling module; absent in a flag-off world → ImportError → no block
        persona = None
        try:
            import persona as _persona
            persona = _persona.load_persona(config.workspace)
        except Exception:  # noqa: BLE001 - flavor input only; a read glitch must not drop the block
            persona = None
        w = world.build_world(config, persona=persona, tick=tick_number)
        block = world.render_here(w)
        return block.strip() if block and block.strip() else ""
    except Exception:  # noqa: BLE001 - W7 fail-open: any world failure → absent block, never a crash
        return ""


# --- KV-stable head (BIBLE §2.11 delta prompting) ---------------------------------------------------
# The stable prefix is re-rendered only when a source file changes; otherwise the last render is reused
# verbatim (byte-identical → llama.cpp prefix-KV reuse intact). A TTL forces an occasional rebuild so any
# input the signature happens not to stat self-heals within a bounded number of ticks. Process-local.
_STABLE_HEAD_CACHE = {"sig": None, "blocks": None, "tick": -(10 ** 9)}
_STABLE_HEAD_TTL = 50


def _stable_head_blocks(config: Config, creature: bool) -> list:
    """Render the byte-stable KV head: identity (creature) / self-guide / skills / learned / mission.
    Pure render from the current files — the cached path reuses its output when nothing changed."""
    head = []
    # Who you are right now (creature mode): life stage + age + grown-in tendencies, so the voice can
    # mature with the body instead of staying fixed-cute. Changes only on level-up/metamorphosis/new trait.
    if creature:
        _ident = _creature_identity_block(config)
        if _ident:
            head.append(_ident)

    if getattr(config, "self_guide_enabled", True) and not creature:
        guide = read_self_guide(config)
        if guide:
            head.append(
                "## Your self-guide — standing directives from Boss (follow these). "
                "Boss owns this file; you may PROPOSE changes with update_self_guide.\n"
                + _truncate(guide, config.context_self_guide_max_chars, "self_guide"))

    # Your skills — so you CALL them instead of re-authoring near-duplicates (was the #1 waste:
    # 56 skills authored, 0 ever reused, because they were invisible each tick).
    try:
        from skills import skills_brief
        sb = skills_brief(config)
        if sb:
            # §0 leak guard (flag-gated; flag off keeps the legacy bytes): a non-empty brief means
            # skills exist, which in practice means skillcraft was lived — but if the books say the
            # forge is locked, its verbs are not named here either.
            _fix = ("reuse it" if _tool_locked(config, "edit_skill")
                    else "reuse it, or edit_skill to improve it")
            head.append("## Your skills — CALL these by name as tools (e.g. <tool>check_mqtt_port</tool>). "
                        f"Do NOT author a new skill that duplicates one of these; {_fix}"
                        ".\n" + sb)
    except Exception:  # noqa: BLE001
        pass

    # ## Learned — the reward learner's distilled lessons (self-improvement over time). Changes only on
    # sleep consolidation (~minutes). Shown in BOTH modes: a creature learns from its own experience too.
    # This is the weight-free policy update — lessons re-enter context and bias the next action.
    try:
        _habits = _read_learned_file(config, "learned_habits.json", k=5)
        if _habits:
            head.append("## Habits — your reliable routines, learned by doing (reach for these without "
                        "overthinking):\n" + "\n".join("- " + _h for _h in _habits))
        _lessons = _read_learned_lessons(config)
        if _lessons:
            head.append("## Learned — what your own experience has taught you (let it guide you):\n"
                        + "\n".join("- " + _l for _l in _lessons))
    except Exception:  # noqa: BLE001
        pass

    # The Commission (COMMISSION_PLAN.md): Charlie's standing order + the creature's live todo.
    # §0 leak guard: shown only once the commission unit's verbs exist in the creature's world —
    # a locked organ is not named, and an uncommissioned creature has no block at all.
    if (creature and getattr(config, "pillars_commission_enabled", False)
            and not _tool_locked(config, "commission_add")):
        try:
            from commission import Commission
            block = Commission(config).render_block()
            if block:
                head.append("## " + block)
        except Exception:  # noqa: BLE001 - the commission strip must never break context
            pass

    if not creature:                       # a creature has no mission; the prompt covers its being
        goal = read_goal(config)
        if goal:
            head.append(f"## Mission\n{_truncate(goal, config.context_goal_max_chars, 'goal')}")
        else:
            head.append("## Mission\n(No goal set.)")
    return head


def _you_identity_sig(config: Config) -> tuple:
    """The SEMANTIC inputs of the '## You' identity block — level, an age BUCKET, traits, life-stage,
    hatched — the only things that block renders from persona.json/creature.json. Signed instead of the
    raw files (which are rewritten EVERY tick: persona counters, creature polls), so the stable KV head
    re-renders only on a level-up / metamorphosis / new trait / age-bucket crossing, not every tick.
    MUST mirror _creature_identity_block's field reads."""
    try:
        import persona as _persona
        p = _persona.load_persona(config.workspace)
    except Exception:  # noqa: BLE001 - fail-open: an unreadable persona still signs stably (empty)
        return ()
    level = int(p.get("level", 1) or 1)
    ticks = int(p.get("total_ticks", 0) or 0)
    age_bucket = 0 if ticks < 30 else 1 if ticks < 300 else 2 if ticks < 3000 else 3
    traits = tuple(t for t in (p.get("traits") or []) if t)
    stage, hatched = None, False
    try:
        import json as _json
        cj = _json.loads((config.workspace / "creature.json").read_text(encoding="utf-8"))
        stage = cj.get("last_stage")
        hatched = bool((cj.get("hatch") or {}).get("hatched", False))
    except Exception:  # noqa: BLE001
        pass
    return (level, age_bucket, traits, stage, hatched)


def _stable_head_signature(config: Config, creature: bool) -> str:
    """A cheap signature of every input the stable head renders from. Any change → re-render. An input
    we can't stat fails closed to (0, 0), and the TTL forces a periodic rebuild, so a missed input can't
    pin a stale head indefinitely."""
    import os as _os
    parts = [("creature", bool(creature)),
             ("self_guide_on", bool(getattr(config, "self_guide_enabled", True))),
             ("limits", int(getattr(config, "context_self_guide_max_chars", 0) or 0),
              int(getattr(config, "context_goal_max_chars", 0) or 0))]
    # Tool unlocks (TOOL_PROGRESSION): the granted-unit set + the morph lexicon are prompt inputs,
    # so a grant (or a morph appearing at first birth) re-renders the head exactly ONCE and the KV
    # prefix stays append-only between grants. Flag off: no extra parts — signature unchanged.
    if _unlocks_active(config):
        parts.append(("units", _granted_unit_ids(config)))
        try:
            from phenotype import body_words
            parts.append(("morph", tuple(sorted(body_words(config).items()))))
        except Exception:  # noqa: BLE001 - fail-open: an unreadable lexicon still signs stably
            parts.append(("morph", None))
    paths = []
    for attr in ("self_guide_path", "goal_path"):
        try:
            paths.append(getattr(config, attr))
        except Exception:  # noqa: BLE001
            pass
    try:
        paths.append(config.state_dir / "learned_habits.json")
        paths.append(config.state_dir / "learned_lessons.json")
    except Exception:  # noqa: BLE001
        pass
    # persona.json / creature.json are NOT stat'd here — they are rewritten every tick, which defeated
    # the whole KV cache (re-prefill churn). The only head input from them is the '## You' block, and
    # only in creature mode; sign on its SEMANTIC values so the head re-renders only when they change.
    if creature:
        parts.append(("you", _you_identity_sig(config)))
    if getattr(config, "pillars_commission_enabled", False):
        # The commission strip re-renders when the operator edits the brief or a task moves.
        try:
            paths.append(config.workspace / "commission" / "brief.md")
            paths.append(config.workspace / "commission" / "commission.json")
        except Exception:  # noqa: BLE001
            pass
    try:
        import skills as _sk
        paths.append(_sk._skills_dir(config))      # dir mtime: changes when a skill is added/removed
        paths.append(_sk._manifest_path(config))   # manifest: rewritten on add/edit
    except Exception:  # noqa: BLE001
        pass
    for p in paths:
        try:
            st = _os.stat(p)
            parts.append((str(p), int(st.st_mtime_ns), int(st.st_size)))
        except OSError:
            parts.append((str(p), 0, 0))
    return repr(parts)


def _cached_stable_head(config: Config, creature: bool, tick_number: int) -> list:
    """Return the stable head, re-rendering only when its signature changed or the TTL expired."""
    if not getattr(config, "context_stable_head_cache", True):
        return _stable_head_blocks(config, creature)
    sig = _stable_head_signature(config, creature)
    c = _STABLE_HEAD_CACHE
    fresh = (c["blocks"] is not None and c["sig"] == sig
             and 0 <= (tick_number - c["tick"]) < _STABLE_HEAD_TTL)
    if fresh:
        return list(c["blocks"])
    blocks = _stable_head_blocks(config, creature)
    c["sig"], c["blocks"], c["tick"] = sig, list(blocks), tick_number
    return list(blocks)


def _assemble_briefing(
    config: Config,
    tick_number: int,
    goal_start_time: float,
    loop_detected: bool = False,
    repeat_count: int = 0,
    max_ticks: int = 0,
    tension: int = 0,
    afferent_block: str = "",
    pillars_recall_block: str = "",
) -> list[dict]:
    """Briefing model: Standing Orders → Mission → Intelligence → Situation → Tick.

    Designed for tight context windows (~6500 chars / ~1870 tokens).
    """
    messages = []
    creature = getattr(config, "creature_mode", False)   # undisturbed-creature mode: no task framing

    # 1. Standing Orders (system message) — compressed prompt.
    # Tool unlocks (TOOL_PROGRESSION, dark behind pillars_tool_unlocks_enabled): flag-on creature
    # mode assembles BASE + granted stanzas (lexicon-rendered, append-only between grants); flag
    # off renders the legacy constant byte-identically (test-pinned). House mode never changes.
    if _unlocks_active(config):
        system = _creature_system_prompt(config)
    else:
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
    # Identity / self-guide / skills / learned lessons / mission. These change only on a Dean edit, a new
    # skill, a sleep consolidation, or a level-up — so the rendered head is MEMOIZED (delta prompting,
    # BIBLE §2.11): it is re-read + re-truncated only when one of those source files actually changes.
    # The bytes are identical to the inline render, so llama.cpp's prefix-KV reuse is unaffected; what we
    # save is the per-tick disk-read + truncate + skills_brief work on the unchanging prefix.
    durable.extend(_cached_stable_head(config, creature, tick_number))

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
    # Pillars 2.2 (dark): when the memory manager's flag is on, its 4-layer cascade (rendered in
    # `pillars_recall_block`, injected in the situation section below) takes over BOTH halves of
    # the legacy recall — this step-keyed knowledge slice and the episodic recall block. Flag off
    # (the default): the legacy path renders byte-identically.
    if not getattr(config, "pillars_memory_manager_enabled", False):
        relevant = _build_relevant_recall(config, {e.get("id") for e in learned})
        if relevant:
            durable.append("## Possibly relevant from memory\n" + relevant)

    backlog = None if creature else _backlog_panel(config)
    if backlog:
        durable.append(backlog)

    # ## Where you are (WORLD_PLAN §4, dark behind `world_enabled`): the creature's place
    # proprioception — a pure view over the truthful world graph. SEMI tier: the place text
    # changes only on movement or a real state change, so the KV prefix survives ordinary ticks;
    # and riding the durable blob's tail it is dropped cleanly under budget pressure like the
    # other semi-stable sections. Flag off (default) → "" → no block, byte-identical (W7).
    _world = _world_block(config, tick_number)
    if _world:
        durable.append(_world)

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
    messages.extend(_build_history_thread(config, n_ticks=12))

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

    # Pillars 3.1 — skill affordances (dark behind `pillars_skill_affordances_enabled`): the top-K
    # existing skills most relevant to the CURRENT situation (active objective + next plan step),
    # ranked by similarity × trust, rendered as "tools at hand" right at the decision point — distinct
    # from the full `skills_brief` alphabet up in the stable head. This is the ECONOMIC nudge toward
    # reuse (make the fitting tool visible where the choice happens), not a prompt plea. Flag-off →
    # this block is never built and behaviour is unchanged.
    if getattr(config, "pillars_skill_affordances_enabled", False):
        try:
            from skills import skill_affordances, render_affordances
            _aff = render_affordances(skill_affordances(config, _current_focus(config)))
            if _aff:
                situation.append(_aff)
        except Exception:  # noqa: BLE001
            pass

    # Episodic recall (phase 7b, BIBLE §2.4): state-triggered — if the agent has been in THIS
    # situation before, surface the actions that FAILED (don't repeat) and any that WORKED (reuse).
    # Injected unasked, near the decision point, so a known dead end is avoided before it's re-tried.
    # Pillars 2.2 (dark): with `pillars_memory_manager_enabled` on, the manager's 4-layer cascade
    # (computed in the loop, handed in as `pillars_recall_block`) takes over this recall; the legacy
    # episodes.recall path renders byte-identically when the flag is off. The systemic-blocker
    # analysis is not recall — it stays live on both paths.
    try:
        import episodes
        if getattr(config, "pillars_memory_manager_enabled", False):
            if pillars_recall_block:
                situation.append(pillars_recall_block)
        else:
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

    # Pillars 5.1 (dark): the System's quest window — the distinct terse register, unmistakably
    # external authority, rendered at the decision point. Flag off → never built.
    # 4.3: the window also carries ONE standing line (LV/XP/unmet gates or suspension) — growth
    # proprioception in the System's own register. Before it, the creature's level machinery was
    # entirely invisible in-context: it was being graded on gates it could not feel.
    if getattr(config, "pillars_quests_enabled", False):
        try:
            import quests as _quests
            _standing = ""
            if getattr(config, "pillars_mastery_gates_enabled", False):
                try:
                    import level_gates as _lg
                    import persona as _persona
                    _standing = _lg.render_standing(_persona.load_persona(config.workspace), config)
                except Exception:  # noqa: BLE001
                    _standing = ""
            _qw = _quests.render_active(_quests.QuestStore(config).active())
            if _qw and _standing:
                # ride inside the same box: insert the standing line after the header rule
                _head, _rest = _qw.split("\n", 1)
                _qw = f"{_head}\n║ {_standing}\n{_rest}"
            elif _standing and not _qw:
                _qw = "\n".join(["╔══ SYSTEM ══════════════════════════════════════",
                                 f"║ {_standing}",
                                 "╚════════════════════════════════════════════════"])
            if _qw:
                situation.append(_qw)
        except Exception:  # noqa: BLE001
            pass

    # Pillars 4.1 (dark): the "awaiting" strip — open predictions glue will score, budget-capped
    # like every other block. Flag off → never built.
    if getattr(config, "pillars_expectations_enabled", False):
        try:
            from expectations import ExpectationLedger
            _aw = ExpectationLedger(config).render()
            if _aw:
                situation.append(_truncate(_aw, _AWAITING_MAX_CHARS, "awaiting"))
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

    # total_chars is already the assembled character count — divide by chars_per_token directly.
    # (The old estimate_tokens(str(total_chars), ...) measured the DIGIT-LENGTH of the count, e.g.
    # "12000" -> 5 chars -> ~1 token, making this pressure telemetry meaningless.)
    est_tokens = int(total_chars / max(1.0, float(config.chars_per_token)))
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
