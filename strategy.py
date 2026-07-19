"""Strategy memory (ReasoningBank, SOTA #3): distil a closed quest/objective into a compact,
retrievable GUARDRAIL — a "trigger → principle" — so the live recall cascade can surface it the next
time the creature is in a matching situation. Success becomes "reuse this"; a derailment becomes
"avoid this / do instead". This is how each closed goal — especially each doom-loop death — is turned
into a guardrail the creature actually reads before acting.

Position in the architecture:
  - It is the EVENT-DRIVEN, quest-close analog of the sleep-time SettlementLessonsJob (nervous/sleep.py):
    where that mechanically turns commission verdicts into procedure/error engrams during sleep, this
    turns the outcome of a self-goal into a guardrail the MOMENT it closes (ARCHITECTURE_PRINCIPLES #1:
    event over timer). It does not wait for the arousal-gated sleep window.
  - It OWNS no store. The guardrail is an ordinary `strategy`-kind Engram committed through the single
    Consolidator (engram.py §I6) and surfaced by the existing MemoryManager recall cascade — no parallel
    jsonl, no second "## Learned" reader. (Reinventing a memory store is the documented eiDOS trap.)

This module produces only the guardrail BODY string (+ a couple of tuning constants). It prefers a tiny
grammar-constrained call to the local mind, but ALWAYS falls back to a deterministic template when the
mind is absent (mock/test) or declines — a closed quest is ground truth and a guardrail is cheap, so a
close is never dropped silently. The caller (eidos.Pillars) does the encode.
"""

from __future__ import annotations

import logging
import re
from typing import Callable, Optional

logger = logging.getLogger("eidos.strategy")

# Birth strength — the only lever that makes a guardrail outlive the UNIFORM per-sleep decay
# (there is no per-kind decay rate; StrengthDecayPruneJob decays every engram by one global constant).
# Mirror the settlement-lesson constants: a failure/derailment guardrail (a scar) encodes ABOVE a win,
# so it persists longer and is likelier to be recalled before the creature repeats the mistake.
STRATEGY_STRENGTH_WIN = 0.55    # a success guardrail — just above neutral (cf. LESSON_STRENGTH_WIN)
STRATEGY_STRENGTH_LOSS = 0.6    # a derailment guardrail — above wins; scars persist (cf. LESSON_STRENGTH_ERROR)

# Keep a guardrail terse — recall budget is shared and a bloated guardrail crowds out the rest.
STRATEGY_TRIGGER_MAX = 90
STRATEGY_PRINCIPLE_MAX = 170
STRATEGY_BODY_MAX = 240

# Closes we do NOT distil: a merge death is a duplicate goal folded into another (no lesson), and a
# title-less record is noise. Everything else — success, release/death, abandon, archive, quest
# pass/expire — carries a usable guardrail.
_SKIP_OUTCOMES = frozenset({"merged"})


def should_distill(closure: dict) -> bool:
    """A cheap gate so the loop only pays the distillation cost on closes that carry a lesson."""
    if not isinstance(closure, dict):
        return False
    if not (closure.get("title") or "").strip():
        return False
    if (closure.get("outcome") or "").strip().lower() in _SKIP_OUTCOMES:
        return False
    return True


def strength_for(closure: dict) -> float:
    """Loss guardrails (scars) are born stronger than wins so they persist to be recalled."""
    return STRATEGY_STRENGTH_WIN if closure.get("success") else STRATEGY_STRENGTH_LOSS


# --- LLM distillation -------------------------------------------------------------------------------
# One bounded line: "TRIGGER: <cue> || PRINCIPLE: <do/avoid>". EVERY repetition axis is bounded — the
# 2026-07-06 sleep-distillation incident (nervous/sleep.py) showed an unbounded repetition looping
# degenerate tokens for hundreds of tokens on a 12B; the grammar must not leave that path open. If the
# server rejects the grammar, llm.complete fails OPEN (retries unconstrained) and parse_strategy still
# salvages a well-formed line or hands us a drop to log — then the template fallback fires.
_STRATEGY_GRAMMAR = (
    'root ::= "TRIGGER: " trigger " || PRINCIPLE: " principle "\\n"\n'
    f'trigger ::= char{{4,{STRATEGY_TRIGGER_MAX}}}\n'
    f'principle ::= char{{4,{STRATEGY_PRINCIPLE_MAX}}}\n'
    'char ::= [^\\n|]\n'
)

_LINE_RE = re.compile(r"TRIGGER:\s*(.+?)\s*\|\|\s*PRINCIPLE:\s*(.+?)\s*$", re.IGNORECASE | re.DOTALL)


def build_strategy_grammar() -> str:
    return _STRATEGY_GRAMMAR


def parse_strategy(text: str) -> tuple[Optional[dict], list[str]]:
    """Inverse of the grammar. Returns ({trigger, principle}, dropped_lines) — a malformed output is
    never silently swallowed (mirrors parse_distillations); it is returned in `dropped` for the caller
    to log, and the caller then falls back to the template."""
    raw = (text or "").strip()
    if not raw:
        return None, []
    # Take the first non-empty line that matches; ignore any extra chatter a failed-open (unconstrained)
    # call may have produced.
    for line in raw.splitlines():
        m = _LINE_RE.search(line.strip())
        if m:
            trig = _clip(m.group(1), STRATEGY_TRIGGER_MAX)
            prin = _clip(m.group(2), STRATEGY_PRINCIPLE_MAX)
            if trig and prin:
                return {"trigger": trig, "principle": prin}, []
    return None, [raw[:200]]


def _render_prompt(closure: dict) -> list[dict]:
    frame = "succeeded" if closure.get("success") else "did NOT succeed"
    parts = [f"Goal: {closure.get('title', '').strip()}"]
    if (closure.get("why") or "").strip():
        parts.append(f"Why it mattered: {closure['why'].strip()}")
    parts.append(f"Outcome: it {frame} ({closure.get('outcome', 'closed')}).")
    if (closure.get("reason") or "").strip():
        parts.append(f"What happened: {closure['reason'].strip()}")
    if (closure.get("trajectory") or "").strip():
        parts.append(f"Trajectory: {closure['trajectory'].strip()}")
    system = (
        "You are distilling one CLOSED goal into a single reusable guardrail for your future self. "
        "Output EXACTLY one line and nothing else, in this form:\n"
        "TRIGGER: <the situation or cue that means this guardrail applies> || "
        "PRINCIPLE: <what to DO if it succeeded, or what to AVOID / do instead if it failed>\n"
        "Be concrete and brief. The TRIGGER should name recognisable situation words (so it can be "
        "matched later); the PRINCIPLE is the actionable lesson. No preamble, no quotes."
    )
    return [{"role": "system", "content": system},
            {"role": "user", "content": "\n".join(parts)}]


def _distill_via_llm(closure: dict, llm: Callable) -> Optional[dict]:
    """Try the local mind. Returns {trigger, principle} or None (caller falls back to the template).
    Fail-open: any LLM error or malformed output degrades to None, never raises."""
    try:
        out = llm(_render_prompt(closure), grammar=build_strategy_grammar())
    except Exception as e:  # noqa: BLE001 — a distiller must never break quest-close bookkeeping
        logger.warning("strategy distiller LLM call failed: %s", e)
        return None
    parsed, dropped = parse_strategy(out or "")
    for d in dropped:
        logger.warning("strategy distiller dropped malformed line: %r", d)
    return parsed


# --- Deterministic template fallback ---------------------------------------------------------------

def _template(closure: dict) -> dict:
    """A mechanical guardrail when the mind is unavailable — a closed goal is ground truth, so we still
    capture something useful (mirrors SettlementLessonsJob's no-LLM distillation)."""
    title = (closure.get("title") or "a goal").strip()
    reason = (closure.get("reason") or "").strip()
    outcome = (closure.get("outcome") or "closed").strip().lower()
    if closure.get("success"):
        principle = "this approach worked — reuse it here"
        if reason:
            principle = f"worked via {reason} — reuse this approach"
    elif outcome in ("released", "dead", "died"):
        principle = ("repeated tries made no real progress — this goal shape is a dead end; "
                     "decompose it or drop it, don't retry as-is")
    elif outcome in ("abandoned",):
        principle = f"abandoned ({reason or 'dead end'}) — recognise this sooner and pivot"
    elif outcome in ("archived", "expired"):
        principle = "went stale without progress — take a smaller concrete step or let it go"
    else:
        principle = reason or "closed without a clear win — reassess the approach"
    return {"trigger": _clip(title, STRATEGY_TRIGGER_MAX),
            "principle": _clip(principle, STRATEGY_PRINCIPLE_MAX)}


def distill_strategy(closure: dict, llm: Optional[Callable] = None) -> Optional[str]:
    """Turn a closed quest/objective into a guardrail body string, or None if it isn't worth one.

    `closure` keys: title (required), why, outcome, reason, success(bool), situation, trajectory.
    `llm` is the (messages, *, grammar=None) -> str callable (eidos.Pillars._live_llm()); None → the
    deterministic template. The returned string is the Engram body the caller commits as kind='strategy'.
    """
    if not should_distill(closure):
        return None
    parsed = _distill_via_llm(closure, llm) if llm is not None else None
    if parsed is None:
        parsed = _template(closure)
    body = f"When {parsed['trigger']}: {parsed['principle']}"
    return _clip(body, STRATEGY_BODY_MAX)


def _clip(s: str, n: int) -> str:
    s = " ".join((s or "").split())   # collapse whitespace/newlines
    return s if len(s) <= n else s[: n - 1].rstrip() + "…"
