"""Pillars 2.2: the memory manager — importer + 4-layer recall + exploration slot
(PILLARS_PLAN §2 M-1/M-2, PILLARS_TODO 2.2).

The engram atom and the three digestive stages already exist (`engram.py`, phase 2.1). This module
is the MANAGER that sits on top of them: it (a) MIGRATES the legacy stores into engrams, (b) RECALLS
from long-term memory through the 4-layer cascade the old episodic store implemented — ranking by
`relevance × strength` — and (c) stamps every fresh encoding with the live EMOTIONAL state read from
the neuromod organ. It is the seam between "eight stores" and "one economy".

It is a pure LIBRARY (PILLARS_PLAN §0, PILLARS_TODO 2.2 discipline):
  - It is NOT imported by eidos.py, context.py, or the tick loop. Nothing runs it in production.
  - Gated behind `config.pillars_memory_manager_enabled` (default False). With the flag off the
    running system is byte-for-byte unchanged; the manager only does anything when a test (or the
    later cutover phase) drives it directly.
  - It writes long-term ONLY through the `Consolidator` (§I6) — never touching `LongTermStore`'s
    name-mangled writer. The manager is a CLIENT of the single writer, not a second one.
  - The import is READ-ONLY on the legacy stores. `episodes.jsonl`, `knowledge/`, and
    `preserved_nuggets.toml` are left byte-for-byte untouched until a later cutover flag flips; the
    manager only reads them and writes engrams into the (separate) long-term store.

Doctrine bindings (PILLARS_PLAN §0):
  §0.2  The mechanism, not the behavior: this builds a recall cascade + an exploration allocation;
        "memory that improves and doesn't echo-chamber" is what a creature running it does.
  §0.4  Every constant here is a DECLARED module knob with a one-line justification (the two live
        tuning inputs — enable + explore ratio — come off the config object, never hard-coded).
  §6    The Matthew effect: strength-ranked recall is a rich-get-richer loop. Every recall set
        therefore reserves a small EXPLORATION slot for a low-strength engram ranking alone buries,
        so a buried memory can still earn its way back (norepinephrine's explore/exploit role).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Optional

import engram
from engram import Consolidator, Engram, EncodedAt, LongTermStore, INHERITED_STRENGTH_FLOOR
from episodes import STEP_CHARS, SUMMARY_CHARS, clean_fragment

# --- Declared knobs (§0.4: each a labeled design knob with its one-line justification) -----------
RECALL_DEFAULT_BUDGET_CHARS = 4000  # declared: default char budget for a recall set when a caller
                                    # names none. ~1k tokens — recall IS how working memory is
                                    # repopulated after a dream (remember-via-retrieval); 1200 was
                                    # starvation. Still a fraction of the window, never dominates, but
                                    # carry the exact-match layer plus one exploration sample.
SEMANTIC_TOP_K = 12                 # declared: how many candidates the semantic layer pulls from
                                    # long-term before the manager re-ranks by relevance×strength and
                                    # budgets. A shortlist, not the whole store — the store's own
                                    # vector recall is relevance-ranked; the manager only re-weights
                                    # the top of it by strength, so a dozen candidates is ample.
EXPLORE_STRENGTH_CEILING = 0.5      # declared: an engram counts as a "low-strength" exploration
                                    # candidate only at/below this. Set at STRENGTH_DEFAULT (0.5, the
                                    # neutral seed) so the exploration slot surfaces memories that have
                                    # not yet EARNED recall — never one already proven strong (that
                                    # one wins on rank and needs no help).
ENCODE_AROUSAL_GAIN = 0.3           # declared: arousal-modulated encoding (§M-1 — the EncodedAt
                                    # docstring's "strength is seeded against it", finally seeded).
                                    # Birth strength = default + gain×(arousal − neutral): a flat
                                    # tick births at 0.35 and is the FIRST thing decay+prune forgets;
                                    # a spiked moment births at 0.65 and persists. This is the
                                    # observation salience gate implemented as pressure — nothing is
                                    # dropped at the door; the trivial just loses the economy.
ENCODE_AROUSAL_NEUTRAL = 0.5        # declared: the arousal level that births at exactly the neutral
                                    # STRENGTH_DEFAULT — the midpoint of the [0,1] arousal channel.

# --- §3 the wisdom calling convention (WISDOM_PLAN §3) --------------------------------------------
# The decision block is retrieval-AS-ANSWER: at most three items — THE CASE, THE GUARDRAIL, THE OFFER
# — rendered decision-shaped near the decision point, and closed with a FIXED frame. The frame is
# verbatim from §3 (WIS5): precedents are the creature's OWN, not orders, and transfer must be
# verified, never assumed — a wrong case confidently injected is worse than nothing.
WISDOM_HEADER = "## Before you act"
WISDOM_FRAME = ("These are YOUR precedents, not orders — verify they transfer before leaning on "
                "them.")
WISDOM_BLOCK_MAX_CHARS = 700        # declared: fallback block budget when config carries none (§3:
                                    # 700 @16k, 1400 @32k — read from config so §0's flip scales it;
                                    # this constant is only the getattr default, never the live knob).
WISDOM_RECALL_MIN_SIM = 0.55        # declared: fallback similarity floor when config carries none —
                                    # the platform gate (WIS5). Below it wisdom stays silent.
# Provenance → the one-word source mark every wisdom item carries (§M-2 source monitoring): the
# creature must know whether a precedent is first-hand, hearsay, an inherited letter, or a dream.
_PROVENANCE_MARK = {
    "experienced": "experienced",
    "told": "told",
    "inherited": "inherited",
    "dreamed": "dreamed",
}

# Legacy knowledge category -> engram kind (§2 schema). `reflections` has no engram kind of its own;
# a reflection is a consolidated declarative belief, so it lands as a `fact`.
_CATEGORY_KIND = {
    "facts": "fact",
    "procedures": "procedure",
    "errors": "error",
    "reflections": "fact",
}

# A marker key placed in engram.stats so a re-import can recognize an already-imported legacy record
# and skip it (idempotency), and so the recall cascade can key on the original situation. stats is a
# free-form dict (engram.validate only requires it be a dict), so this rides along without a schema
# change.
_SRC_KEY = "src"            # provenance of the import: "episodes" | "knowledge" | "nuggets"
_SRC_ID_KEY = "src_id"      # the legacy record's stable id (dedup key for idempotent re-import)
_SITUATION_KEY = "situation"  # the episode's situation key "<objective id>|<step>" (recall cascade)


# =================================================================================================
# Situation-key helpers (mirror episodes.py so an imported episode recalls exactly as it used to)
# =================================================================================================
def _obj_of(situation: str) -> str:
    """The objective id half of a situation key '<objective id>|<step>'."""
    return situation.split("|", 1)[0] if situation else ""


def _step_of(situation: str) -> str:
    """The normalized-step half of a situation key '<objective id>|<step>' (episodes._step_of)."""
    return situation.split("|", 1)[1] if "|" in situation else situation


# =================================================================================================
# The manager
# =================================================================================================
class MemoryManager:
    """The unifying seam over the engram economy. Owns a `Consolidator` (the single long-term
    writer) and reads the live neuromod state for the emotional stamp. Construct it once per config;
    it is stateless beyond that (all durable state lives in the engram stores)."""

    def __init__(self, config, *, consolidator: Optional[Consolidator] = None,
                 neuromod: Any = None):
        self.config = config
        self.consolidator = consolidator or Consolidator(config)
        # The neuromod organ (nervous/neuromod.py) — read at encode time for the emotional stamp.
        # Optional/fail-open: if unavailable we stamp neutral affect (arousal=0, valence=0). A test
        # injects a mock; production wiring (a later phase) passes the live organ.
        self._neuromod = neuromod

    @property
    def store(self) -> LongTermStore:
        return self.consolidator.store

    @property
    def enabled(self) -> bool:
        """The dark flag. With it off the manager is inert — a caller that respects the gate does
        nothing in production. (Tests drive the manager directly regardless of the flag, exactly as
        the engram tests drive the stores directly.)"""
        return bool(getattr(self.config, "pillars_memory_manager_enabled", False))

    # ---------------------------------------------------------------------------------------------
    # Emotional stamp (§M-1: flashbulb memory — read live arousal/valence at the moment of encoding)
    # ---------------------------------------------------------------------------------------------
    def _read_affect(self) -> tuple[float, float]:
        """Read (arousal, valence) from the neuromod organ. Fail-open to neutral (0.0, 0.0) if the
        organ is absent or a read raises — a missing affect channel must never block encoding."""
        organ = self._neuromod
        if organ is None:
            return 0.0, 0.0
        try:
            arousal = float(getattr(organ, "arousal", 0.0))
            valence = float(getattr(organ, "valence", 0.0))
            return arousal, valence
        except Exception:  # noqa: BLE001 - affect is best-effort; encoding proceeds neutral
            return 0.0, 0.0

    def _stamp(self, *, tick: int = 0, felt: str = "") -> EncodedAt:
        """Build the emotional stamp for a fresh encoding from the LIVE neuromod state (§M-1)."""
        arousal, valence = self._read_affect()
        return EncodedAt(tick=int(tick), felt=str(felt or ""), arousal=arousal, valence=valence)

    def encode(self, kind: str, body: str, *, tick: int = 0, felt: str = "",
               provenance: str = "experienced", **fields) -> Engram:
        """Encode a NEW engram and promote it to long-term through the consolidator. The emotional
        stamp is read LIVE from neuromod here (not passed in) — that is the whole point of stamping
        at encode time. With the salience flag on and a live neuromod organ, the stamp also SEEDS
        birth strength (arousal-modulated encoding, §M-1) — unless the caller set strength
        explicitly (the importer's floors, a prediction's declared seed). Returns the engram now
        living in long-term (the merge survivor on a near restatement, else the fresh engram)."""
        stamp = self._stamp(tick=tick, felt=felt)
        if ("strength" not in fields and self._neuromod is not None
                and getattr(self.config, "pillars_encode_salience_enabled", False)):
            seeded = (engram.STRENGTH_DEFAULT
                      + ENCODE_AROUSAL_GAIN * (stamp.arousal - ENCODE_AROUSAL_NEUTRAL))
            fields["strength"] = max(0.0, min(1.0, seeded))
        eg = Engram(kind=kind, body=body, provenance=provenance,
                    encoded_at=stamp, **fields)
        return self.consolidator.commit(eg)

    # ---------------------------------------------------------------------------------------------
    # Importer (PILLARS_TODO 2.2: migrate the legacy stores; read-only on the originals; idempotent)
    # ---------------------------------------------------------------------------------------------
    def import_all(self) -> dict:
        """Migrate every legacy store into engrams. Read-only on the originals (they are left
        untouched until a later cutover flag flips). Idempotent — a legacy record already imported
        (recognized by its src+src_id marker in long-term) is skipped, so re-running never
        duplicates. Returns a per-store count of NEWLY imported engrams."""
        return {
            "episodes": self.import_episodes(),
            "knowledge": self.import_knowledge(),
            "nuggets": self.import_nuggets(),
        }

    def _already_imported(self) -> set[tuple[str, str]]:
        """The (src, src_id) markers already present in long-term — the idempotency ledger."""
        seen: set[tuple[str, str]] = set()
        for e in self.store.load():
            src = e.stats.get(_SRC_KEY)
            sid = e.stats.get(_SRC_ID_KEY)
            if src and sid:
                seen.add((str(src), str(sid)))
        return seen

    def import_episodes(self) -> int:
        """`episodes.jsonl` → `episode` engrams. The legacy file is READ ONLY. Each record's situation
        key is carried in stats so the recall cascade keys on it exactly as episodes.recall did.
        Idempotent via a per-record src_id (tick+key+sig — stable for a given logged episode)."""
        path = self.config.workspace / "episodes.jsonl"
        records = _read_jsonl(path)
        if not records:
            return 0
        seen = self._already_imported()
        egs = []
        for r in records:
            src_id = _episode_src_id(r)
            if ("episodes", src_id) in seen:
                continue
            body = _episode_body(r)
            if not body.strip():
                continue
            stats = {
                _SRC_KEY: "episodes",
                _SRC_ID_KEY: src_id,
                _SITUATION_KEY: str(r.get("key", "")),
            }
            egs.append(Engram(
                kind="episode",
                body=body,
                provenance="experienced",   # an episode is first-hand lived experience (§M-2)
                encoded_at=EncodedAt(tick=int(r.get("tick", 0) or 0)),
                stats=stats,
            ))
            seen.add(("episodes", src_id))
        self.consolidator.commit_many(egs)   # ONE load + ONE rewrite for the batch (not O(n) per record)
        return len(egs)

    def import_knowledge(self) -> int:
        """`knowledge/` → `fact`/`procedure`/`error` engrams (category → kind). READ ONLY on the
        knowledge store — reads the index for metadata and the entry files for full bodies. Idempotent
        via the knowledge entry id. `reflections` map to `fact` (a consolidated belief has no kind of
        its own in the §2 schema)."""
        try:
            import knowledge
        except Exception:  # noqa: BLE001 - knowledge module optional in a bare test env
            return 0
        try:
            index = knowledge.load_index(self.config)
        except Exception:  # noqa: BLE001
            index = []
        if not index:
            return 0
        seen = self._already_imported()
        egs = []
        for item in index:
            entry_id = str(item.get("id", ""))
            if not entry_id or ("knowledge", entry_id) in seen:
                continue
            category = str(item.get("category", "facts"))
            kind = _CATEGORY_KIND.get(category, "fact")
            body = _knowledge_body(self.config, knowledge, entry_id, item)
            if not body.strip():
                continue
            egs.append(Engram(
                kind=kind,
                body=body,
                provenance="experienced",   # the creature learned/derived it first-hand (§M-2)
                encoded_at=EncodedAt(tick=int(item.get("source_tick", 0) or 0)),
                stats={_SRC_KEY: "knowledge", _SRC_ID_KEY: entry_id},
            ))
            seen.add(("knowledge", entry_id))
        self.consolidator.commit_many(egs)   # ONE load + ONE rewrite for the batch (not O(n) per record)
        return len(egs)

    def import_nuggets(self) -> int:
        """`preserved_nuggets.toml` → engrams with provenance='inherited' and a strength FLOOR (a
        letter from a previous self, §M-2). READ ONLY on the toml (loaded via seed_knowledge, which
        already just reads it). The strength floor keeps a fresh creature from forgetting its
        bootstrap knowledge before it has had a chance to earn recall. Idempotent via a content-hash
        src_id (nuggets have no persistent id of their own)."""
        try:
            import seed_knowledge
            nuggets = seed_knowledge.load_nuggets()
            nuggets += seed_knowledge.load_nuggets(seed_knowledge.LOCAL_PATH, optional=True)
        except Exception:  # noqa: BLE001 - nuggets file optional / may be absent
            return 0
        if not nuggets:
            return 0
        seen = self._already_imported()
        egs = []
        for (category, _tags, content) in nuggets:
            content = (content or "").strip()
            if not content:
                continue
            src_id = _nugget_src_id(content)
            if ("nuggets", src_id) in seen:
                continue
            kind = _CATEGORY_KIND.get(str(category), "fact")
            egs.append(Engram(
                kind=kind,
                body=content,
                provenance="inherited",             # a nugget IS the inherited letter (§M-2)
                strength=INHERITED_STRENGTH_FLOOR,  # seeded above neutral so it is not forgotten early
                encoded_at=EncodedAt(),
                stats={_SRC_KEY: "nuggets", _SRC_ID_KEY: src_id},
            ))
            seen.add(("nuggets", src_id))
        self.consolidator.commit_many(egs)   # ONE load + ONE rewrite for the batch (not O(n) per record)
        return len(egs)

    # ---------------------------------------------------------------------------------------------
    # Recall — the 4-layer cascade (exact → cross-objective → same-objective → semantic)
    # ---------------------------------------------------------------------------------------------
    def _relevance_map(self, query: str, situation: Optional[str],
                       entries: list[Engram]) -> dict[str, float]:
        """The 4-layer cascade relevance in [0,1] per candidate id — the ONE truth both `recall`
        (prose set) and `recall_scored`/`wisdom_block` (§3 calling convention) rank on, so the
        decision block can never disagree with the prose recall about how well a memory matches.

        Each candidate keeps the MAX relevance across the layers: exact situation (1.0) >
        cross-objective step (0.85) > same-objective (0.7) > semantic token-overlap (≤0.6) and the
        store's vector recall (0.55). The situation layers always outrank a bare resemblance."""
        obj = _obj_of(situation or "")
        step = _step_of(situation or "")
        relevance: dict[str, float] = {}

        def _bump(eid: str, r: float) -> None:
            if r > relevance.get(eid, 0.0):
                relevance[eid] = r

        for e in entries:
            e_sit = str(e.stats.get(_SITUATION_KEY, ""))
            if situation:
                if e_sit and e_sit == situation:
                    _bump(e.id, 1.0)                                  # layer 1: exact
                elif step and e_sit and _step_of(e_sit) == step:
                    _bump(e.id, 0.85)                                 # layer 2: cross-objective
                elif obj and e_sit and _obj_of(e_sit) == obj:
                    _bump(e.id, 0.7)                                  # layer 3: same-objective
            if query:
                ov = engram._overlap(query, e.body)                  # layer 4: semantic (token overlap)
                if ov > 0:
                    _bump(e.id, min(0.6, ov * 0.6))                  # capped below the situation layers

        # The store's own semantic recall (embedding/vector-aware, mock-aware) — pattern completion
        # beyond bare token overlap. A vector hit the token-overlap layer missed still earns semantic
        # relevance (0.55), capped below the situation layers so a situation match always outranks a
        # bare resemblance.
        if query:
            for e in self.store.recall(query, top_k=SEMANTIC_TOP_K):
                _bump(e.id, 0.55)
        return relevance

    def _score(self, e: Engram, relevance: dict[str, float]) -> float:
        """rank key = relevance × strength (§M-1), tilted toward the present when the recency flag
        is on: the factor is floored (engram.RECENCY_FLOOR) so age re-orders near-ties, never buries
        an earned memory — forgetting stays decay+prune's job."""
        s = relevance[e.id] * float(e.strength)
        if bool(getattr(self.config, "pillars_recall_recency_enabled", False)):
            s *= engram.recency_factor(e.created)
        return s

    def recall(self, query: str, *, situation: Optional[str] = None,
               budget_chars: int = RECALL_DEFAULT_BUDGET_CHARS,
               explore_ratio: Optional[float] = None) -> list[Engram]:
        """Return the engrams that should shape the current decision, ranked by relevance×strength
        and fit to a char budget, with one exploration slot reserved.

        The 4-layer cascade (ported from episodes.recall's match order):
          1. EXACT           — engrams whose situation key == the current situation.
          2. CROSS-OBJECTIVE — same normalized STEP under any objective (objective ids churn; the
                               step is the stable part of a situation).
          3. SAME-OBJECTIVE  — same objective id (any step under the goal I'm pursuing).
          4. SEMANTIC        — vector/overlap resemblance from the long-term store (pattern
                               completion): a partial cue pulls up the whole.
        The cascade is ADDITIVE and de-duplicated: each layer contributes candidates the earlier
        layers missed, so a strong semantic match is not thrown away just because an exact match also
        exists. Candidates are then ranked by `relevance × strength` (§M-1: earned usefulness is half
        the ranking key), one EXPLORATION slot is reserved (§6, anti-Matthew), and the set is fit to
        `budget_chars`.
        """
        query = (query or "").strip()
        entries = self.store.load()
        if not entries:
            return []

        relevance = self._relevance_map(query, situation, entries)
        candidates = [e for e in entries if e.id in relevance]
        if not candidates:
            return []

        ranked = sorted(candidates, key=lambda e: self._score(e, relevance), reverse=True)

        # --- budget first, then the exploration slot INSIDE the budget (§6, anti-Matthew) ----------
        # The slot used to be spliced into the candidate list before budgeting, which parked it at
        # index ~n·(1−ratio) — any realistic char budget cut it long before that, and the sim-days
        # harness caught the slot silently vanishing under production-shaped recalls (promotions → 0
        # from day 2). The seat must be reserved within what the budget actually returns.
        ratio = self.effective_explore_ratio() if explore_ratio is None else explore_ratio
        fitted = _fit_to_budget(ranked, budget_chars)
        out = self._reserve_exploration_slot(ranked, fitted, budget_chars, ratio)

        # §3 SETTLEMENT PLUMBING (WISDOM_PLAN §3): the `## Before you act` block IS recall — its
        # case/guardrail items must ride the SAME recall-bet machinery the prose set rides (eidos.py's
        # _Pillars.recall_block wagers on exactly `manager.recall(...)`'s output via `self.injected`).
        # So when the calling-convention flag is on, we GUARANTEE the wisdom-selected engrams are in
        # the returned set even if the char budget would have cut them — a bet the creature was shown
        # but never registered is a hole §2/§5 can't settle. Flag off → this is a no-op (byte-
        # identical). The scored candidates are already computed above; reuse the same relevance.
        if bool(getattr(self.config, "wisdom_recall_enabled", False)):
            scored = [(e, relevance[e.id]) for e in ranked]
            wis, _sims = self._wisdom_select(scored)
            have = {e.id for e in out}
            for e in wis:
                if e.id not in have:
                    out.append(e)
                    have.add(e.id)
        return out

    def effective_explore_ratio(self) -> float:
        """The recall exploration ratio actually used: config.pillars_recall_explore_ratio × the
        genome's explore_recall gene (openness — genome.py, congenital personality as pressure).
        FAIL-OPEN: with no genome file / no module the gene is exactly 1.0, so the bare config
        value stands byte-identically. Applied here — where the constant is READ — because the
        genome shapes perception (what recall digs up), never the ledger."""
        ratio = float(self.config.pillars_recall_explore_ratio)
        try:
            from genome import gene
            return ratio * gene(self.config, "explore_recall")
        except Exception:  # noqa: BLE001 - the genome must never break recall
            return ratio

    def _reserve_exploration_slot(self, ranked: list[Engram], fitted: list[Engram],
                                  budget_chars: int, explore_ratio: float) -> list[Engram]:
        """Reserve a low-strength EXPLORATION seat inside the BUDGETED recall set (§6). Strength-ranked
        recall is rich-get-richer: a memory that ranking buries can never be recalled, so can never
        earn back its strength, so stays buried — an echo chamber you cannot detect from inside. To
        break it, ONE low-strength but relevant engram that the budgeted ranking excluded is promoted
        into the returned set, its seat paid for by exploit's LAST seat (the lowest-ranked fitted item
        is dropped until the sample fits the char budget). `explore_ratio` > 0 turns the slot on
        (config.pillars_recall_explore_ratio, default 0.15 — norepinephrine's explore/exploit balance);
        exactly one sample is promoted per recall regardless of ratio size — the point is that the
        allocation must never round (or get budget-cut) to zero.
        """
        try:
            ratio = float(explore_ratio)
        except (TypeError, ValueError):
            ratio = 0.0
        if ratio <= 0.0 or len(ranked) < 2:
            return fitted

        # The buried, low-strength, still-relevant candidate: lowest strength among those the
        # budgeted set excluded. This is precisely what pure ranking-under-budget would never show.
        fitted_ids = {e.id for e in fitted}
        buried = [e for e in ranked if e.id not in fitted_ids
                  and float(e.strength) <= EXPLORE_STRENGTH_CEILING]
        if not buried:
            return fitted  # every low-strength candidate already made the set — nothing is buried
        sample = min(buried, key=lambda e: float(e.strength))

        # Pay for the seat: drop exploit's lowest-ranked items until the sample fits the budget.
        # At least one exploit item always stays — exploration accompanies recall, never replaces it.
        kept = list(fitted)
        if budget_chars is not None and budget_chars > 0:
            used = sum(len(e.body) for e in kept)
            while len(kept) > 1 and used + len(sample.body) > budget_chars:
                used -= len(kept.pop().body)
            if used + len(sample.body) > budget_chars:
                return kept  # a budget too small for even (1 exploit + sample): exploit wins the seat
        return kept + [sample]

    # ---------------------------------------------------------------------------------------------
    # §3 The wisdom calling convention — retrieval as answer, not reading material
    # ---------------------------------------------------------------------------------------------
    def recall_scored(self, query: str, *, situation: Optional[str] = None
                      ) -> list[tuple[Engram, float]]:
        """The cascade candidates paired with their similarity (the §3 gate reads it), ranked exactly
        as `recall` ranks — SAME `_relevance_map` + `_score`, so the decision block and the prose
        recall never disagree about a match. Returns `(engram, similarity)` descending; similarity is
        the raw cascade relevance in [0,1] (the gate compares it to `wisdom_recall_min_sim`, WIS5).
        The store's own recall is consulted just as in `recall`, so a bare vector hit still scores.
        Empty when the store is empty or nothing matches (graceful absence)."""
        query = (query or "").strip()
        entries = self.store.load()
        if not entries:
            return []
        relevance = self._relevance_map(query, situation, entries)
        candidates = [e for e in entries if e.id in relevance]
        if not candidates:
            return []
        ranked = sorted(candidates, key=lambda e: self._score(e, relevance), reverse=True)
        return [(e, relevance[e.id]) for e in ranked]

    def wisdom_block(self, query: str, *, situation: Optional[str] = None,
                     affordances: Optional[list[dict]] = None) -> tuple[str, list[Engram]]:
        """Render the §3 `## Before you act` decision block and return `(text, injected)` — `injected`
        is the engram items the block actually surfaced, so the caller registers them on the SAME
        recall-bet machinery the prose recall rides (the block IS recall — §2/§5 want its settlement).

        At most three items, each provenance-marked, each drawn from the ranked cascade:
          (a) THE CASE     — the single best situation-matched EPISODE with a verified (successful)
                             outcome, rendered action-first: "Here before (sim 0.83): `X` → worked …".
          (b) THE GUARDRAIL — the matched `strategy` engram as a one-line imperative, VERBATIM.
          (c) THE OFFER    — the top skill affordance as a one-line invocation offer (reuses the
                             caller's ranking — never re-ranked here).
        Closed with the fixed verify-transfer frame (WIS5). Empty sections are omitted; the whole
        block is absent (text='', injected=[]) when nothing qualifies.

        Platform-gated (WIS5, WIS7):
          - `wisdom_recall_enabled` off → ('' , []) unconditionally (flag-dark, byte-identical).
          - renders ONLY when the best-match similarity ≥ `wisdom_recall_min_sim`.
          - hard char cap at `wisdom_block_max_chars` (never exceeded; the frame is kept, items are
            dropped tail-first to fit).
        Never raises — a memory fault must never break the tick (fail to '' , [])."""
        if not bool(getattr(self.config, "wisdom_recall_enabled", False)):
            return "", []                                   # WIS7: flag-dark, byte-identical
        try:
            return self._wisdom_block(query, situation, affordances)
        except Exception as e:  # noqa: BLE001 - wisdom is additive; never break the tick (WIS8 fail-open)
            import logging
            logging.getLogger("eidos.memory").warning("wisdom_block failed: %s", e)
            return "", []

    def _wisdom_select(self, scored: list[tuple[Engram, float]]
                       ) -> tuple[list[Engram], dict[str, float]]:
        """Pick the engram items the §3 block would surface — THE CASE (best floor-clearing episode
        with a verified outcome) then THE GUARDRAIL (best floor-clearing `strategy`) — and their
        sims. ONE truth shared by `_wisdom_block` (render) and `recall` (bet coverage), so the block
        can never surface an engram the ledger didn't see, nor vice-versa. Returns (engrams, sims by
        id). Below-floor candidates are excluded (WIS5 gate). The affordance OFFER is not an engram,
        so it is chosen by `_wisdom_block` alone."""
        min_sim = float(getattr(self.config, "wisdom_recall_min_sim", WISDOM_RECALL_MIN_SIM))
        if not scored or scored[0][1] < min_sim:
            return [], {}     # gate: the BEST match must clear the floor, else silence
        eligible = [(e, sim) for (e, sim) in scored if sim >= min_sim]
        picked: list[Engram] = []
        sims: dict[str, float] = {}
        case = next((e for (e, _s) in eligible
                     if e.kind == "episode" and _verified_success(e)), None)
        if case is not None:
            picked.append(case)
            sims[case.id] = next(s for (e, s) in eligible if e.id == case.id)
        guard = next((e for (e, _s) in eligible if e.kind == "strategy"), None)
        if guard is not None:
            picked.append(guard)
            sims[guard.id] = next(s for (e, s) in eligible if e.id == guard.id)
        return picked, sims

    def _wisdom_block(self, query: str, situation: Optional[str],
                      affordances: Optional[list[dict]]) -> tuple[str, list[Engram]]:
        scored = self.recall_scored(query, situation=situation)
        picked, sims = self._wisdom_select(scored)   # WIS5 gate lives inside _wisdom_select

        injected: list[Engram] = []
        items: list[str] = []   # rendered lines, in fixed order: case, guardrail, offer
        for e in picked:
            if e.kind == "episode":                                  # (a) THE CASE
                items.append(_render_case(e, sims[e.id]))
            elif e.kind == "strategy":                               # (b) THE GUARDRAIL
                items.append(_render_guardrail(e))
            injected.append(e)

        # (c) THE OFFER — the top skill affordance, one-line invocation offer. The affordance ranking
        # is the caller's (skills.skill_affordances); we consume its head, never re-rank it. An
        # affordance is a skill offer, not an engram — nothing to bet on it, so it never joins injected.
        if affordances:
            offer = _render_offer(affordances[0])
            if offer:
                items.append(offer)

        if not items:
            return "", []   # gate cleared but no case/guardrail/offer took shape → graceful absence

        max_chars = int(getattr(self.config, "wisdom_block_max_chars", WISDOM_BLOCK_MAX_CHARS))
        return _fit_wisdom_block(items, injected, max_chars)


# =================================================================================================
# §3 wisdom-block render helpers (module-level; the block FORMAT is the manager's to own)
# =================================================================================================
def _prov(e: Engram) -> str:
    """The one-word provenance mark for a wisdom item (§M-2 source monitoring)."""
    return _PROVENANCE_MARK.get(str(getattr(e, "provenance", "") or ""), "experienced")


def _verified_success(e: Engram) -> bool:
    """A case is only offered on a VERIFIED outcome. The importer renders an episode body as
    '… `tool` succeeded.' on a win and '… failed …' otherwise, and may stamp an explicit
    stats['outcome']/stats['verified']. Treat an explicit success stat as authoritative; else read
    the rendered body. A failure episode is never THE CASE (that is a scar for the guardrail path,
    not a 'do this' precedent)."""
    st = e.stats or {}
    if "verified" in st:
        return bool(st.get("verified"))
    outcome = str(st.get("outcome", "") or "").lower()
    if outcome:
        return "succeed" in outcome or outcome in ("worked", "success", "ok")
    body = (e.body or "").lower()
    if "succeeded" in body or "→ worked" in body or "worked" in body:
        return True
    return False


def _render_case(e: Engram, sim: float) -> str:
    """Action-first case line: 'Here before (sim 0.83): ran `X` → worked[, fixed in N ticks]. [prov]'.
    Reuses whatever the episode body already carries; a `fix_ticks` stat, when present, adds the
    '(fixed in N ticks)' clause the plan calls for."""
    body = (e.body or "").strip()
    tail = ""
    ft = (e.stats or {}).get("fix_ticks")
    try:
        if ft is not None and int(ft) > 0:
            tail = f" (fixed in {int(ft)} tick{'s' if int(ft) != 1 else ''})"
    except (TypeError, ValueError):
        tail = ""
    return f"- THE CASE — Here before (sim {sim:.2f}): {body}{tail} [{_prov(e)}]"


def _render_guardrail(e: Engram) -> str:
    """The matched strategy engram as a one-line imperative, VERBATIM — the guardrail body is already
    a distilled trigger→principle (strategy.py); we do not paraphrase it, only mark its provenance."""
    body = " ".join((e.body or "").split()).strip()   # collapse whitespace to keep it one line
    return f"- THE GUARDRAIL — {body} [{_prov(e)}]"


def _render_offer(aff: dict) -> str:
    """The top affordance as a one-line invocation offer. `aff` is a skill_affordances dict
    ({name, ...}); we offer its call, never re-rank."""
    name = str((aff or {}).get("name", "") or "").strip()
    if not name:
        return ""
    return f"- THE OFFER — the skill `{name}` fits this; call <{name}>…</{name}> before authoring anew."


def _fit_wisdom_block(items: list[str], injected: list[Engram], max_chars: int
                      ) -> tuple[str, list[Engram]]:
    """Assemble header + items + frame under a HARD char cap (WIS5 budget: `wisdom_block_max_chars`,
    NEVER exceeded). The header and frame are non-negotiable; items are dropped TAIL-FIRST (offer,
    then guardrail, then case) until the whole block fits. `injected` is trimmed in lockstep so a
    dropped engram is NOT registered as a bet (the ledger must see exactly what the creature was
    shown). If even header + frame + the single most-relevant item still overflows, the block is
    OMITTED (return '' , []) — a HARD cap, unlike the prose recall's soft over-budget-single rule:
    WIS5's doctrine is that an oversized/uncertain injection is worse than nothing, and silence keeps
    the byte budget honest for the §3 shave."""
    kept_items = list(items)
    while kept_items:
        block = _assemble_wisdom(kept_items)
        if max_chars is None or max_chars <= 0 or len(block) <= max_chars:
            # trim injected to only the engram items still present (offer carries no engram, so the
            # engram items are a prefix of kept_items in the same order they were appended).
            kept_injected = injected[:_engram_count(kept_items, len(injected))]
            return block, kept_injected
        kept_items.pop()   # drop the tail item and retry under the hard cap
    return "", []          # even one item overflows the cap → silence (WIS5: worse than nothing)


def _engram_count(kept_items: list[str], n_injected: int) -> int:
    """How many of the surviving items are engram-backed (THE CASE / THE GUARDRAIL, which were
    appended before THE OFFER). The offer line starts '- THE OFFER'; every earlier surviving item is
    engram-backed, so the count is min(non-offer survivors, n_injected)."""
    non_offer = sum(1 for it in kept_items if not it.startswith("- THE OFFER"))
    return min(non_offer, n_injected)


def _assemble_wisdom(items: list[str]) -> str:
    """Header → items → the fixed verify-transfer frame (WIS5)."""
    return "\n".join([WISDOM_HEADER, *items, WISDOM_FRAME])


# =================================================================================================
# Legacy-record → engram-body helpers (module-level; the body FORMAT is the manager's to own)
# =================================================================================================
def _read_jsonl(path: Path) -> list[dict]:
    """Best-effort jsonl read (house convention: skip corrupt lines, never raise). Read-only."""
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    out: list[dict] = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            d = json.loads(line)
        except (ValueError, json.JSONDecodeError):
            continue
        if isinstance(d, dict):
            out.append(d)
    return out


def _episode_src_id(rec: dict) -> str:
    """A stable idempotency id for a logged episode. tick+key+sig identifies the specific logged
    decision; two genuinely distinct episodes never collide, and re-importing the same file yields
    the same id, so the import skips it the second time."""
    return f"{rec.get('tick', '')}|{rec.get('key', '')}|{rec.get('sig') or rec.get('tool', '')}"


def _episode_body(rec: dict) -> str:
    """Render an episode record as a readable situation→action→outcome digest (episodes.py's schema).
    The situation KEY is carried separately in stats for the recall cascade; the body is the prose a
    recall would inject into context — so the step/summary shards are cleaned HERE too: legacy
    records predate the source cleaning and carry plan-list markers and mid-word hard slices. A step
    that cleans away entirely gets no "While ," shard."""
    step = clean_fragment(_step_of(str(rec.get("key", ""))), STEP_CHARS)
    tool = str(rec.get("tool", "") or "")
    ok = bool(rec.get("success"))
    fail_kind = str(rec.get("fail_kind", "") or "")
    summary = clean_fragment(str(rec.get("summary", "") or ""), SUMMARY_CHARS)
    outcome = "succeeded" if ok else (f"failed ({fail_kind})" if fail_kind else "failed")
    parts = []
    if step:
        parts.append(f"While {step},")
    parts.append(f"`{tool or 'action'}` {outcome}.")
    if summary:
        parts.append(summary)
    return " ".join(p for p in parts if p).strip()


def _knowledge_body(config, knowledge_mod, entry_id: str, index_item: dict) -> str:
    """Full body of a knowledge entry: read the entry file for the complete content, falling back to
    the index's content_preview if the file is unavailable. Read-only on the store."""
    try:
        entry = knowledge_mod.read_entry(config, entry_id)
        if entry and isinstance(entry, dict):
            body = entry.get("body") or ""
            if isinstance(body, str) and body.strip():
                return body.strip()
    except Exception:  # noqa: BLE001 - fall back to the index preview
        pass
    return str(index_item.get("content_preview", "") or "").strip()


def _nugget_src_id(content: str) -> str:
    """A stable idempotency id for a nugget. Nuggets have no persistent id, so we hash the content —
    the same nugget text always yields the same id, so a re-import of an unchanged toml is a no-op."""
    import hashlib
    return "nugget-" + hashlib.sha1(content.encode("utf-8")).hexdigest()[:16]


def _fit_to_budget(engrams: list[Engram], budget_chars: int) -> list[Engram]:
    """Return the leading engrams whose bodies fit within `budget_chars` (the ranking already put the
    most valuable first, so a prefix is the right cut). Always returns at least the top engram even if
    it alone exceeds the budget — an over-budget single recall is more useful than an empty set."""
    if budget_chars is None or budget_chars <= 0:
        return list(engrams)
    out: list[Engram] = []
    used = 0
    for e in engrams:
        cost = len(e.body)
        if out and used + cost > budget_chars:
            continue
        out.append(e)
        used += cost
        if used >= budget_chars:
            break
    return out
