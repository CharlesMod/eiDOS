"""Mastery portfolio — levels are earned from FRESH, adjudicated, novelty-weighted evidence.

The gate wall this replaces (level_gates.can_level's all-AND checks) failed the first live
creature three ways: one stuck gate jailed all growth (the genesis-03 deadlock), the checks
measured counts rather than quality (two trivial file-listers satisfied trusted_in_tier; Brier
0.02 from self-fulfilling bets satisfied calibration), and lifetime aggregates meant one early
burst of evidence satisfied a gate forever. Design decisions (Charlie, 2026-07-20):

  - PORTFOLIO, not wall: crossing needs K score from >= M distinct EVIDENCE CLASSES (breadth
    with slack — a broken subsystem slows growth, it can never jail it), plus the hard floors
    that stay in level_gates (sleeps, no suspensions).
  - FRESH per level: apply_level_up SPENDS the portfolio (archived, bounded); the next level
    starts empty. One burst can never carry two levels.
  - XP pays on ADJUDICATED EVENTS ONLY (the +1-per-successful-tool-call trickle goes dark
    under the flag): the bar the creature watches finally measures growth, not volume.
  - Suspension-only regression (unchanged; level_gates keeps that machinery).

Every item is glue-adjudicated at its source seam — the model cannot narrate evidence into
this ledger (§0.5), and the single writer is record_evidence. Novelty is mechanical: an item
whose key/title near-duplicates an already-held same-class item (token Jaccard) earns
DUP_WEIGHT, and each class's countable score is capped, so grinding one shape cannot fill the
portfolio — the same anti-vacuity lever the reward habituation uses.

DARK behind `pillars_portfolio_gates_enabled` (flag off = byte-identical legacy gates).
"""
from __future__ import annotations

import json
import logging
import time
from typing import Any, Optional

logger = logging.getLogger("eidos.mastery")

# --- Declared knobs (§0.4) -----------------------------------------------------------------------

STATE_NAME = "mastery_portfolio.json"

# The six evidence classes and the XP each pays WHEN RECORDED. Zero for events that already
# carry their own reward (quest reward legs, commission payouts) — evidence is still recorded,
# XP is never paid twice for one event.
CLASSES: dict[str, int] = {
    "skill_trusted": 15,        # a skill carried to trusted through real use (skills.py seam)
    "quest_passed": 0,          # quest closed PASSED — pays its own reward via the sink
    "objective_completed": 40,  # a self-chosen commitment finished (persona.record_goal_complete)
    "commission_confirmed": 0,  # operator-confirmed commission task — pays its own XP + feed
    "prediction_settled": 10,   # a non-trivial bet the world proved right (glue settlement)
    "error_recovery": 5,        # a failed tick followed by an adjudicated success
}

CLASS_SCORE_CAP = 3.0     # max score any ONE class contributes toward K — breadth is the point;
                          # eight error-recoveries must not buy a level.
DUP_WEIGHT = 0.25         # near-duplicate of an already-held same-class item earns this weight —
                          # repetition never counts like novelty (cf. reward habituation).
NOVELTY_SIM = 0.5         # token-Jaccard at/above this vs an existing same-class item = duplicate
                          # (knowledge.token_jaccard — the same one tokenizer the goal/skill
                          # economies use; symmetric, so subset titles don't auto-collide).
PRED_CONF_LO = 0.55       # a countable prediction is a real bet: above coin-flip conviction...
PRED_CONF_HI = 0.95       # ...but below near-certainty (the live creature farmed 0.9 "my own
                          # file exists" bets; the band + novelty dedup kill that pattern).
ARCHIVE_MAX = 200         # spent-item history kept for the dashboard/dossier, bounded.

# K/M schedule: score needed and minimum distinct classes for crossing INTO `next_level`.
# Early levels small by design — the genesis arc itself (skill trusted + quest passed +
# objective completed) is exactly a level-2 portfolio. Later levels want breadth.


def requirement(next_level: int) -> tuple[float, int]:
    """(K score, M distinct classes) required to cross into `next_level` (declared curve:
    K climbs one per level from 3, capped at 8; M steps 2 -> 3 -> 4 as the creature matures)."""
    nl = max(2, int(next_level))
    k = float(min(3 + (nl - 2), 8))
    m = 2 if nl <= 3 else (3 if nl <= 6 else 4)
    # M can never exceed the class count; K can never be unreachable under the per-class cap.
    m = min(m, len(CLASSES))
    k = min(k, CLASS_SCORE_CAP * len(CLASSES))
    return k, m


def _enabled(config) -> bool:
    return bool(getattr(config, "pillars_portfolio_gates_enabled", False))


# --- The ledger ----------------------------------------------------------------------------------

def _path(config):
    return config.state_dir / STATE_NAME


def _load(config) -> dict:
    try:
        d = json.loads(_path(config).read_text(encoding="utf-8"))
        if isinstance(d, dict) and isinstance(d.get("items"), list):
            d.setdefault("archive", [])
            return d
    except Exception:  # noqa: BLE001 - missing/corrupt => fresh books (fail-open)
        pass
    return {"items": [], "archive": []}


def _save(config, data: dict) -> None:
    try:
        config.state_dir.mkdir(parents=True, exist_ok=True)
        p = _path(config)
        tmp = p.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        tmp.replace(p)
    except Exception:  # noqa: BLE001 - best-effort persistence; the tick outranks the books
        pass


def _similar(a: str, b: str) -> float:
    try:
        from knowledge import token_jaccard
        return token_jaccard(a, b)
    except Exception:  # noqa: BLE001 - similarity fault => treat as novel (never block evidence)
        return 0.0


def record_evidence(config, persona: Optional[dict], cls: str, key: str, *,
                    title: str = "", tick: int = 0) -> Optional[dict]:
    """THE single writer. Records one adjudicated evidence item; pays the class XP through
    persona.award_xp. Returns the item, or None (flag off / unknown class / exact dup).

    `key` is the event's stable identity (quest id, skill name, objective id...) — exact-key
    re-records are dropped whole (an event happens once). `title` is the human text used for
    novelty scoring; falls back to the key."""
    if not _enabled(config) or cls not in CLASSES:
        return None
    key = (key or "").strip()
    if not key:
        return None
    data = _load(config)
    if any(i["cls"] == cls and i["key"] == key for i in data["items"]):
        return None  # the same event never counts twice
    text = (title or key).strip()
    dup = any(i["cls"] == cls and _similar(text, i.get("title") or i["key"]) >= NOVELTY_SIM
              for i in data["items"])
    item = {"cls": cls, "key": key, "title": text[:200],
            "weight": DUP_WEIGHT if dup else 1.0,
            "tick": int(tick), "ts": time.time()}
    data["items"].append(item)
    _save(config, data)
    xp = int(CLASSES[cls])
    if xp > 0 and persona is not None:
        try:
            import persona as persona_mod
            persona_mod.award_xp(persona, xp, reason=f"mastery:{cls}:{key}", config=config)
        except Exception:  # noqa: BLE001 - the evidence stands even if the payout hiccups
            logger.warning("mastery XP payout failed for %s:%s", cls, key)
    logger.info("mastery evidence: %s %s (weight %.2f)", cls, key, item["weight"])
    return item


def prediction_counts(confidence: float, reason: str) -> bool:
    """Quality filter for prediction evidence: a real bet (confidence in the declared band)
    that stood until its DEADLINE — claim/event closures can settle the instant they're placed,
    which is how the self-fulfilling 'my own file exists' pattern farmed calibration."""
    try:
        c = float(confidence)
    except (TypeError, ValueError):
        return False
    return reason == "deadline" and PRED_CONF_LO <= c <= PRED_CONF_HI


def portfolio_report(config, next_level: int) -> dict:
    """The glue-adjudicated standing: per-class capped scores, total, distinct classes, and the
    K/M requirement — every value visible so the creature and the dashboard see the same truth
    (ARCHITECTURE_PRINCIPLES #4)."""
    k_need, m_need = requirement(next_level)
    data = _load(config)
    by_cls: dict[str, float] = {}
    for i in data["items"]:
        by_cls[i["cls"]] = by_cls.get(i["cls"], 0.0) + float(i.get("weight", 1.0))
    capped = {c: min(v, CLASS_SCORE_CAP) for c, v in by_cls.items()}
    score = round(sum(capped.values()), 2)
    classes = sum(1 for v in capped.values() if v > 0)
    return {"score": score, "score_need": k_need,
            "classes": classes, "classes_need": m_need,
            "by_class": capped, "items": len(data["items"]),
            "ok": score >= k_need and classes >= m_need}


def spend(config, level_reached: int) -> int:
    """Fresh-per-level: crossing consumes the portfolio. Items move to the bounded archive
    stamped with the level they bought; the live set empties. Returns items spent."""
    data = _load(config)
    spent = data["items"]
    if not spent:
        return 0
    for i in spent:
        i["spent_at_level"] = int(level_reached)
    data["archive"] = (data["archive"] + spent)[-ARCHIVE_MAX:]
    data["items"] = []
    _save(config, data)
    return len(spent)
