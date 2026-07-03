"""Pillars 4.4: the news queue — the deferred-communication store (PILLARS_PLAN §2 M-5,
PILLARS_TODO 4.4; §8 pitfall #7 "report by exception").

The plan's M-5: *items worth telling Dean, ranked by a learned model of what he engages with,
held until presence.* The mechanism is exactly that — a bounded queue plus a relevance model —
and "it has news when you come home" (dream-test D1) is the emergent READ of a creature running
this store, never a code path here (§0.2: no line names the behavior it hopes to produce).

Three parts:

  1. INGESTION — `NewsQueue.ingest(engram_or_event, source)` accepts the three news sources
     (high-surprise expectation closures, quest/level events, anomalies), normalizes each into a
     kind='news' engram, and commits it through the Consolidator (§I6: EVERY long-term write flows
     through the single writer — news is memory first, message second). A small queue entry
     (engram id + ranking features) rides in `workspace/state/news_queue.json`, BOUNDED by
     `config.pillars_news_max_items` with TTL expiry + lowest-rank eviction — no unbounded growth
     (§M-3). Pitfall #7's damper is structural: routine output never pages anyone — an expectation
     closure below the surprise floor is not news at all (report by exception), and everything
     else waits in the queue.

  2. PRESENCE-GATED SURFACING — `surface(presence, budget_chars)` returns the ranked queue ONLY
     when `presence` is True; absence returns nothing, always, regardless of queue depth. That IS
     the gate: deferred communication never interrupts absence. Presence is the listening-hold /
     chat-focus signal (dashboard's chat_hold.json), passed in as a bool by the caller — this
     module never reads the hold file itself (library discipline; the cutover wires the signal).

  3. ENGAGEMENT RANKING — items are ranked by a small linear engagement model over declared
     features (source type, surprise magnitude, recency) whose weights are TRAINED ON DEAN'S
     ACTUAL RESPONSES via `record_outcome(item_id, engaged)` (replied/acted = engaged;
     ignored/expired = not). The update is a bounded logistic step with weight floors/ceilings
     (§0.6: stability guards on every self-tuning loop — a bad week of outcomes cannot run the
     ranker away). The prior only orders the queue until outcomes exist; after that, what Dean
     actually engages with owns the ordering.

Ships DARK behind `config.pillars_news_enabled` (default False): with the flag off, ingest /
surface / record_outcome are no-ops and NOTHING is persisted. Pure LIBRARY — not imported by
eidos.py / context.py / dashboard.py; a later cutover wires the sources and the presence signal.

Doctrine bindings (PILLARS_PLAN §0):
  §0.2  Mechanism only: a queue + a relevance model. "Having news" lives in the dream-tests.
  §0.3  Closed loop: every surfaced item is a bet on Dean's attention, settled by his actual
        response (record_outcome) or by expiry-after-surfacing (auto-settled as not-engaged).
  §0.4  Every constant below is a DECLARED knob with a one-line justification.
  §0.6  Bounded step size + weight clamps on the self-tuning engagement model.
  §I6   News engrams reach long-term memory only through the Consolidator.
"""

from __future__ import annotations

import json
import math
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

import engram
from engram import Consolidator, Engram, LongTermStore

# --- Declared knobs (§0.4: each a labeled design knob with its one-line justification) -----------
NEWS_TTL_S = 3 * 86400          # declared: an item stops being news after 3 days — long enough to
                                # survive a weekend absence ("news when you come home"), short
                                # enough that last week's minutiae never greet a homecoming.
RECENCY_HALF_LIFE_S = 12 * 3600.0  # declared: the recency feature halves every 12 h — this
                                # morning's event outranks yesterday's at equal source/surprise,
                                # but a big yesterday still beats a trivial now (it's one feature
                                # among three, not a hard sort).
SURPRISE_NORM_BITS = 6.0        # declared: normalizes surprise into [0,1] for the feature vector.
                                # Same units/cap as expectations.SURPRISE_MAX (Shannon bits, ~6 =
                                # maximally novel) so closure surprise needs no rescaling seam.
                                # Local copy, not an import: news.py depends only on engram.py.
EXPECTATION_SURPRISE_FLOOR_BITS = 1.0  # declared: pitfall #7's "report by exception" made
                                # mechanical — expectation closures fire on every deadline, so
                                # only one carrying ≥1 bit of surprise (the creature was actually
                                # wrong-footed) qualifies as news; routine confirmations do not.
                                # Quest/anomaly sources are rare by construction and have no floor.
SURFACE_BUDGET_CHARS_DEFAULT = 1200  # declared: default surfacing budget — a chat-sized digest
                                # (a few short paragraphs), not a report dump; the caller may pass
                                # its own budget from live context math.
ENGAGEMENT_STEP = 0.3           # declared (§0.6): max per-outcome learning-rate on the engagement
                                # weights. One outcome moves a weight ≤ 0.3·|error| ≤ 0.3 — a
                                # handful of consistent responses measurably reorder the queue,
                                # while a single stray click cannot capsize the ranking.
WEIGHT_FLOOR = -2.0             # declared (§0.6): hard clamp on every learned weight — the model
WEIGHT_CEIL = 2.0               # stays a mild preference ordering, never a runaway amplifier
                                # (|score| stays small enough that the logistic never saturates
                                # into a zero-gradient dead zone).
PRIOR_SOURCE_WEIGHT = {         # declared: the sane prior over source types, holding rank order
    "anomaly": 0.6,             # only until real outcomes exist. Anomalies edge quests edge
    "quest": 0.5,               # routine closures — "something is off" historically prompts a
    "expectation": 0.4,         # reply more than a milestone, which prompts more than a scored
}                               # bet. Outcomes own the ordering from the first response on.
PRIOR_FEATURE_WEIGHT = {        # declared: prior on the scalar features — surprise is the best
    "surprise": 1.0,            # single predictor of tell-worthiness (§M-4: residue is the
    "recency": 0.5,             # highest-value input), recency matters but must not drown a big
}                               # older item.

# The closed set of news sources (PILLARS_TODO 4.4). Plain strings, house convention.
SOURCES = frozenset(PRIOR_SOURCE_WEIGHT)


def _now_epoch() -> float:
    return time.time()


def _queue_path(config) -> Path:
    # state_dir (workspace/state/): queue bookkeeping is working state, not knowledge — the
    # MEMORY of each item is its engram in long-term; this file is just the deferred-send slots.
    return config.state_dir / "news_queue.json"


# ============================================================================================
# One queue entry — a deferred-communication slot pointing at its kind='news' engram
# ============================================================================================
@dataclass
class NewsItem:
    """One item worth telling Dean, awaiting presence. `id` is the backing news engram's id
    (the engram in long-term memory IS the durable record; this entry is the send-slot with the
    ranking features riding along). `surfaced_ts` marks the first time it was actually shown —
    an item that was shown and then expired settles as not-engaged (§0.3: the loop closes)."""
    id: str
    body: str
    source: str
    created_ts: float
    expires_ts: float
    surprise: float = 0.0
    surfaced_ts: Optional[float] = None

    def to_dict(self) -> dict:
        return {"id": self.id, "body": self.body, "source": self.source,
                "created_ts": self.created_ts, "expires_ts": self.expires_ts,
                "surprise": self.surprise, "surfaced_ts": self.surfaced_ts}

    @staticmethod
    def from_dict(d: dict) -> Optional["NewsItem"]:
        try:
            return NewsItem(
                id=str(d["id"]), body=str(d["body"]), source=str(d["source"]),
                created_ts=float(d["created_ts"]), expires_ts=float(d["expires_ts"]),
                surprise=float(d.get("surprise", 0.0)),
                surfaced_ts=(float(d["surfaced_ts"]) if d.get("surfaced_ts") is not None else None),
            )
        except (KeyError, TypeError, ValueError):
            return None   # corrupt entry → skipped, best-effort read (house convention)


# ============================================================================================
# Normalizing the three sources into (body, surprise)
# ============================================================================================
def _body_of(obj: Any) -> str:
    """Extract the human-readable body from whatever a source hands us. Duck-typed so news.py
    never imports expectations/quests (read-only consumers stay decoupled):
      - str                         → itself
      - Engram                      → its body
      - expectations.Closure        → its residue engram's body (the "what I bet vs what happened")
      - quests.Quest                → "quest <state>: <directive>"
      - dict (anomaly record)       → body/summary/text field, else the whole record as json
    """
    if isinstance(obj, str):
        return obj.strip()
    if isinstance(obj, Engram):
        return (obj.body or "").strip()
    residue = getattr(obj, "residue", None)          # expectations.Closure shape
    if isinstance(residue, Engram):
        return (residue.body or "").strip()
    directive = getattr(obj, "directive", None)      # quests.Quest shape
    if isinstance(directive, str) and directive.strip():
        state = getattr(obj, "state", "") or ""
        return f"quest {state}: {directive}".strip()
    if isinstance(obj, dict):
        for key in ("body", "summary", "text"):
            v = obj.get(key)
            if isinstance(v, str) and v.strip():
                return v.strip()
        return json.dumps(obj, ensure_ascii=False)
    return str(obj).strip()


def _surprise_of(obj: Any, explicit: Optional[float]) -> float:
    """Surprise magnitude in bits: an explicit kwarg wins; else a Closure-shaped `.surprise`
    attr; else a dict field; else 0 (a quest event is not 'surprising', it is just tellable)."""
    if explicit is not None:
        return max(0.0, float(explicit))
    attr = getattr(obj, "surprise", None)
    if isinstance(attr, (int, float)):
        return max(0.0, float(attr))
    if isinstance(obj, dict) and isinstance(obj.get("surprise"), (int, float)):
        return max(0.0, float(obj["surprise"]))
    return 0.0


# ============================================================================================
# The news queue
# ============================================================================================
class NewsQueue:
    """The deferred-communication store (M-5). Bounded, presence-gated, engagement-ranked.
    All engram writes go through the Consolidator (§I6); queue + engagement-weight state lives in
    one small json file, atomically rewritten (temp+replace, house pattern)."""

    def __init__(self, config, *, consolidator: Optional[Consolidator] = None):
        self.config = config
        self.consolidator = consolidator or Consolidator(config)

    # --- gates / bounds -------------------------------------------------------------------------
    def _enabled(self) -> bool:
        return bool(getattr(self.config, "pillars_news_enabled", False))

    @property
    def max_items(self) -> int:
        # The declared bound; read live from config so an edit takes effect without a reload.
        return int(getattr(self.config, "pillars_news_max_items", 20))

    # --- state persistence ------------------------------------------------------------------
    def _load_state(self) -> dict:
        try:
            raw = json.loads(_queue_path(self.config).read_text(encoding="utf-8"))
        except (OSError, ValueError, json.JSONDecodeError):
            raw = {}
        items = []
        for d in raw.get("items") or []:
            it = NewsItem.from_dict(d) if isinstance(d, dict) else None
            if it is not None:
                items.append(it)
        weights = raw.get("weights") or {}
        src_w = dict(PRIOR_SOURCE_WEIGHT)
        for k, v in (weights.get("source") or {}).items():
            if k in SOURCES and isinstance(v, (int, float)):
                src_w[k] = float(v)
        state = {
            "items": items,
            "weights": {
                "source": src_w,
                "surprise": float(weights.get("surprise", PRIOR_FEATURE_WEIGHT["surprise"])),
                "recency": float(weights.get("recency", PRIOR_FEATURE_WEIGHT["recency"])),
            },
            "outcomes": int(raw.get("outcomes", 0)),
        }
        return state

    def _save_state(self, state: dict) -> None:
        path = _queue_path(self.config)
        path.parent.mkdir(parents=True, exist_ok=True)
        out = {
            "items": [it.to_dict() for it in state["items"]],
            "weights": state["weights"],
            "outcomes": state["outcomes"],
        }
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(path)

    # --- the engagement model (linear score → logistic update, §0.6-bounded) -----------------
    def _features(self, item: NewsItem, now: float) -> dict:
        age = max(0.0, now - item.created_ts)
        return {
            "surprise": max(0.0, min(1.0, item.surprise / SURPRISE_NORM_BITS)),
            "recency": 0.5 ** (age / RECENCY_HALF_LIFE_S),
        }

    def _score(self, item: NewsItem, weights: dict, now: float) -> float:
        x = self._features(item, now)
        return (weights["source"].get(item.source, 0.0)
                + weights["surprise"] * x["surprise"]
                + weights["recency"] * x["recency"])

    def _apply_outcome(self, state: dict, item: NewsItem, engaged: bool, now: float) -> None:
        """One bounded logistic step on the engagement weights (§0.6). error = actual − predicted
        engagement probability; each weight moves by ENGAGEMENT_STEP · error · its feature value,
        clamped into [WEIGHT_FLOOR, WEIGHT_CEIL]. Feedback on what Dean actually does is the ONLY
        thing that moves these weights — no self-report path exists (§0.5)."""
        w = state["weights"]
        x = self._features(item, now)
        p = 1.0 / (1.0 + math.exp(-self._score(item, w, now)))
        err = (1.0 if engaged else 0.0) - p

        def _clamp(v: float) -> float:
            return max(WEIGHT_FLOOR, min(WEIGHT_CEIL, v))

        src = item.source if item.source in w["source"] else None
        if src is not None:
            w["source"][src] = _clamp(w["source"][src] + ENGAGEMENT_STEP * err * 1.0)
        w["surprise"] = _clamp(w["surprise"] + ENGAGEMENT_STEP * err * x["surprise"])
        w["recency"] = _clamp(w["recency"] + ENGAGEMENT_STEP * err * x["recency"])
        state["outcomes"] = int(state.get("outcomes", 0)) + 1

    # --- expiry + eviction (the bound, §M-3: no unbounded growth) -----------------------------
    def _prune_expired(self, state: dict, now: float) -> None:
        """Drop expired items. An item that was SURFACED and then expired settles as not-engaged
        (Dean saw it and let it die — that is the 'ignored' half of the outcome signal, §0.3).
        An item that expired UNSEEN records nothing: absence is not a verdict on the item."""
        kept: list[NewsItem] = []
        for it in state["items"]:
            if now >= it.expires_ts:
                if it.surfaced_ts is not None:
                    self._apply_outcome(state, it, engaged=False, now=now)
                continue
            kept.append(it)
        state["items"] = kept

    def _evict_over_bound(self, state: dict, now: float) -> None:
        """Past the declared bound, the LOWEST-RANKED item is evicted first (ties → oldest).
        Eviction records no outcome — an item never offered was never judged."""
        while len(state["items"]) > self.max_items:
            w = state["weights"]
            lowest = min(state["items"], key=lambda it: (self._score(it, w, now), -(now - it.created_ts)))
            state["items"].remove(lowest)

    # --- 1. ingestion --------------------------------------------------------------------------
    def ingest(self, engram_or_event: Any, source: str, *,
               surprise: Optional[float] = None,
               encoded_at: Optional[engram.EncodedAt] = None,
               now: Optional[float] = None) -> Optional[NewsItem]:
        """Admit one item from a news source. The item becomes (or wraps) a kind='news' engram
        committed through the Consolidator (§I6), and a bounded queue entry is appended.

        `source` must be one of SOURCES ('expectation' | 'quest' | 'anomaly'). An expectation
        closure below EXPECTATION_SURPRISE_FLOOR_BITS is refused — report by exception (pitfall
        #7): a routine deadline settling as expected is not news. Returns the queued NewsItem, or
        None (flag off / below floor / empty body).

        DARK GATE: a no-op returning None when `pillars_news_enabled` is off — nothing persisted,
        the running system unchanged."""
        if not self._enabled():
            return None
        if source not in SOURCES:
            raise ValueError(f"unknown news source {source!r}; must be one of {sorted(SOURCES)}")
        now = _now_epoch() if now is None else float(now)

        body = _body_of(engram_or_event)
        if not body:
            return None
        mag = _surprise_of(engram_or_event, surprise)
        if source == "expectation" and mag < EXPECTATION_SURPRISE_FLOOR_BITS:
            return None   # routine closure — not news (the by-exception floor)

        # Materialize the news engram and commit it through the single writer (§I6). An Engram
        # passed in is WRAPPED (linked), not mutated — its kind stays what it was; the news atom
        # is its own memory of "this was worth telling".
        links = [engram_or_event.id] if isinstance(engram_or_event, Engram) else []
        eg = Engram(kind="news", body=body, provenance="experienced",
                    encoded_at=encoded_at or engram.EncodedAt(), links=links)
        survivor = self.consolidator.commit(eg.validate())

        state = self._load_state()
        self._prune_expired(state, now)
        existing = next((it for it in state["items"] if it.id == survivor.id), None)
        if existing is not None:
            # The Consolidator merged this into an already-queued item (a near-restatement) —
            # the event re-occurred, so its slot renews rather than duplicating (bound holds).
            existing.created_ts = now
            existing.expires_ts = now + NEWS_TTL_S
            existing.surprise = max(existing.surprise, mag)
            item = existing
        else:
            item = NewsItem(id=survivor.id, body=body, source=source,
                            created_ts=now, expires_ts=now + NEWS_TTL_S, surprise=mag)
            state["items"].append(item)
        self._evict_over_bound(state, now)
        self._save_state(state)
        return item if any(it.id == item.id for it in state["items"]) else None

    # --- 2. presence-gated surfacing ------------------------------------------------------------
    def surface(self, presence: bool, budget_chars: int = SURFACE_BUDGET_CHARS_DEFAULT, *,
                now: Optional[float] = None) -> list[NewsItem]:
        """The ranked news digest — ONLY under presence. `presence` is the listening-hold /
        chat-focus signal, passed in by the caller; absence returns [] unconditionally (before
        any other check): deferred communication never interrupts absence. THAT is the gate.

        Under presence: expired items are pruned (a surfaced-then-expired one settles as
        not-engaged), the rest are ranked by the engagement model, and items are returned
        best-first until `budget_chars` of body is spent. Surfaced items stay queued (still news
        until Dean's response or expiry settles them) but are stamped `surfaced_ts`."""
        if not presence:
            return []          # absence is never interrupted — regardless of flag or queue depth
        if not self._enabled():
            return []
        now = _now_epoch() if now is None else float(now)

        state = self._load_state()
        self._prune_expired(state, now)
        w = state["weights"]
        ranked = sorted(state["items"], key=lambda it: self._score(it, w, now), reverse=True)

        out: list[NewsItem] = []
        spent = 0
        for it in ranked:
            cost = len(it.body)
            if out and spent + cost > max(0, int(budget_chars)):
                break          # budget spent; always yield at least the top item if any fit at all
            if not out and cost > max(0, int(budget_chars)):
                break
            out.append(it)
            spent += cost
            if it.surfaced_ts is None:
                it.surfaced_ts = now
        self._save_state(state)
        return out

    # --- 3. the outcome signal — Dean's actual response -----------------------------------------
    def record_outcome(self, item_id: str, engaged: bool, *,
                       now: Optional[float] = None) -> bool:
        """Settle one item on Dean's ACTUAL response: replied/acted → engaged=True; explicitly
        ignored → engaged=False (expiry-after-surfacing settles the silent case automatically).
        Applies one bounded step to the engagement weights (§0.6) and retires the item from the
        queue — a settled item is no longer news. Returns True if the item was found and settled.

        DARK GATE: no-op returning False when the flag is off."""
        if not self._enabled():
            return False
        now = _now_epoch() if now is None else float(now)
        state = self._load_state()
        item = next((it for it in state["items"] if it.id == item_id), None)
        if item is None:
            return False
        self._apply_outcome(state, item, engaged=bool(engaged), now=now)
        state["items"] = [it for it in state["items"] if it.id != item_id]
        self._save_state(state)
        return True

    # --- read-only views ------------------------------------------------------------------------
    def items(self, *, now: Optional[float] = None) -> list[NewsItem]:
        """The current live queue (expired items dropped from the view; no state written —
        settlement of surfaced-expired items happens on the write paths)."""
        now = _now_epoch() if now is None else float(now)
        return [it for it in self._load_state()["items"] if now < it.expires_ts]

    def weights(self) -> dict:
        """The engagement model's current weights — the operator-visible surface §0.6 asks for
        (a later cutover renders this on the dashboard; read-only here)."""
        return json.loads(json.dumps(self._load_state()["weights"]))
