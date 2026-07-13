"""Pillars 2.1: the engram — the atom of the memory economy (PILLARS_PLAN §2, PILLARS_TODO 2.1).

The memory pillar's first principle (§2): memory is not eight stores, it is ONE economy with a
lifecycle — experience → episode → consolidated knowledge/skill/identity — and the stores are
stages of digestion. This module builds the ATOM of that economy (the `Engram`) and the three
digestive stages, modelled on the hippocampal–neocortical system:

  - HOT TRACE      — the tick's working set. In-memory, tick-scoped, cleared each tick. Nothing
                     here is durable; it is scratch space for what the current tick is handling.
  - EPISODIC RING  — the hippocampus: fast, plastic, BOUNDED episodic encoding. A FIFO ring on
                     disk (jsonl). New experience lands here first; forgetting is a feature (§M-3),
                     so the ring is bounded and evicts its oldest when full.
  - LONG-TERM      — the neocortex: slow, stable, consolidated knowledge, in the house style of the
                     knowledge store (jsonl + npy vector sidecar + json index). CRITICAL (§I6): the
                     long-term store has EXACTLY ONE writer, the `Consolidator`. The store object
                     itself exposes read/recall openly but NO public append/write — arbitrary code
                     cannot bolt an entry into long-term memory. Every long-term write flows through
                     `Consolidator.commit(engram)` / `.merge(...)`, so consolidation policy (dedup,
                     strength, provenance) lives in one place and cannot be bypassed.

The engram itself carries the economy's currency (§M-1, §M-2):
  - strength   — EARNED usefulness (0..1), compounding recency + frequency + emotional salience at
                 encoding. Recall ranking and retention both key on it. The recall-utility loop that
                 raises/lowers it is phase 2.3 (the bet ledger); this module just holds + persists it.
  - provenance — `experienced | told | inherited` (§M-2): "I saw it" vs "I was told" vs "a letter
                 from a previous self" (nuggets). Source monitoring, so confidence can be discounted
                 by how the memory was acquired.
  - confidence — 0..1, how sure we are of the body's truth. Contradiction lowers it (§M-2, later).
  - encoded_at — the EMOTIONAL STAMP (§M-1): {tick, felt, arousal, valence} read from neuromod at
                 encoding. Flashbulb memory — high-arousal episodes resist forgetting. Phase 2.2
                 reads the live neuromod state; this module just carries the stamp.

Ships DARK behind `config.pillars_memory_engram_enabled` (default False). This module is a pure
LIBRARY — it is NOT imported by eidos.py or the tick loop. Phase 2.2 (memory_manager.py) is what
wires it in and flips the flag; with the flag off nothing in the running system changes.

Doctrine bindings (PILLARS_PLAN §0):
  §0.2  No line of code names the behavior it hopes to produce — this builds the MECHANISM (an atom
        with earned strength, three bounded stages, one consolidator) and "memory improving over
        time" is what a creature running the recall-utility loop over these stages does.
  §0.4  Every constant is derived or a DECLARED knob with a one-line justification.
  §I6   One consolidator is the single writer of every long-term store.
"""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

# --- Declared knobs (§0.4: each a labeled design knob with its one-line justification) -----------
EPISODIC_RING_MAX = 2400        # declared: FIFO episodic ring capacity. Matched to episodes.py's
                                # _MAX_EPISODES (the existing episode store this ring succeeds) — at
                                # ~1 episode per acting tick that is days of memory (~600 KB), not
                                # hours, and a whole-file scan stays sub-millisecond.
LONGTERM_MERGE_THRESHOLD = 0.85  # declared: on commit, an incoming engram whose body overlaps an
                                # existing long-term engram at/above this coefficient is MERGED into
                                # it rather than added (pattern separation, §2: similar-but-distinct
                                # stays distinct; near-restatements do not bloat the store). Set just
                                # above knowledge.store_entry's 0.65 dedup floor because long-term is
                                # the CONSOLIDATED tier — only a near-identical restatement merges.
STRENGTH_DEFAULT = 0.5          # declared: a newly-encoded engram starts at neutral usefulness — it
                                # has neither earned recall nor been shown useless; the bet ledger
                                # (2.3) moves it from here.
CONFIDENCE_DEFAULT = 0.7        # declared: default trust in a freshly-encoded body. Above neutral
                                # (most direct experience is trustworthy) but not certain — leaves
                                # headroom for contradiction to lower it (§M-2).
INHERITED_STRENGTH_FLOOR = 0.6  # declared: a `told`/`inherited` engram (a nugget — a letter from a
                                # previous self, §M-2) is seeded ABOVE neutral so a fresh creature
                                # does not immediately forget its bootstrap knowledge before it has
                                # had a chance to earn recall. 2.2's importer uses this floor.
RECENCY_HALFLIFE_S = 7 * 86400.0  # declared: recall's recency half-life. A week separates "this
                                # era" from "history": a device re-scanned today should outrank a
                                # stale note about it, but a memory is not stale by lunchtime —
                                # days, not hours, is the scale the creature's world changes on.
RECENCY_FLOOR = 0.5             # declared: the recency factor's floor — age can at most HALVE a
                                # memory's rank, never bury it. Strength (earned usefulness) stays
                                # the dominant key; time is a tilt toward the present, not a second
                                # forgetting mechanism (decay+prune already own forgetting).

# Valid engram kinds (§2 schema). A closed set — the taxonomy of what a memory can BE. Kept as a
# frozenset (not an Enum) to match the house's plain-string typing (see episodes.fail_kind,
# knowledge.CATEGORIES) and to serialize as bare strings.
KINDS = frozenset({
    "episode",     # a lived situation→action→outcome (the episodic ring's native content)
    "fact",        # a consolidated declarative truth (neocortical knowledge)
    "procedure",   # how to do a thing (procedural knowledge; skills link here)
    "error",       # a known failure pattern (decays slower — scars persist, §2.3)
    "prediction",  # an open expectation awaiting closure (the expectation ledger, §M-4)
    "news",        # something worth telling Dean, held until presence (§M-5)
    "identity",    # a self-model fact — who the creature is
})

# Valid provenance values (§M-2: source monitoring — "I saw it" vs "I was told").
PROVENANCE = frozenset({
    "experienced",  # first-hand: the creature lived it
    "told",         # second-hand: Dean / an afferent / a general's report said so
    "inherited",    # a nugget: a letter from a previous self (pre-wipe bootstrap knowledge)
    "dreamed",      # sleep-distilled hypothesis (pitfall #5): confidence-capped until corroborated
})



def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _new_id() -> str:
    """A stable, collision-free engram id. uuid4 hex — engrams are created across ticks, sleep
    jobs, and the importer; a content-hash id would collide on identical bodies, and we want each
    encoding to be a distinct atom until the consolidator decides to merge them."""
    return uuid.uuid4().hex


def recency_factor(created_iso: str, *, now: Optional[float] = None,
                   halflife_s: float = RECENCY_HALFLIFE_S,
                   floor: float = RECENCY_FLOOR) -> float:
    """A recall-ranking multiplier in [floor, 1.0] from a record's age: 1.0 at birth, exponential
    half-life decay toward the floor. Shared by every recall ranker that weights time (the engram
    cascade, knowledge's BM25) so "recent" means one thing across the memory economy. FAIL-OPEN: an
    unparseable/missing timestamp scores 1.0 — a record without a birthday is never penalized."""
    try:
        import calendar
        born = calendar.timegm(time.strptime(created_iso.strip(), "%Y-%m-%dT%H:%M:%SZ"))
    except (ValueError, AttributeError, TypeError, OverflowError):
        return 1.0
    age = max(0.0, (time.time() if now is None else float(now)) - born)
    if halflife_s <= 0:
        return 1.0
    factor = 0.5 ** (age / float(halflife_s))
    return max(float(floor), min(1.0, factor))


# ============================================================================================
# The emotional stamp (§M-1: flashbulb memory — the salience of the moment of encoding)
# ============================================================================================
@dataclass
class EncodedAt:
    """When and how-it-felt at the moment of encoding. `felt` is the neuromod felt-state label;
    arousal/valence are the affective coordinates (2.2 reads them live from neuromod). This stamp
    is what makes high-arousal memories resist forgetting — strength is seeded and decayed against
    it (§2.3), not against wall-clock alone."""
    tick: int = 0
    felt: str = ""
    arousal: float = 0.0
    valence: float = 0.0

    def to_dict(self) -> dict:
        return {"tick": self.tick, "felt": self.felt,
                "arousal": self.arousal, "valence": self.valence}

    @staticmethod
    def from_dict(d: Optional[dict]) -> "EncodedAt":
        d = d or {}
        return EncodedAt(
            tick=int(d.get("tick", 0)),
            felt=str(d.get("felt", "")),
            arousal=float(d.get("arousal", 0.0)),
            valence=float(d.get("valence", 0.0)),
        )


# ============================================================================================
# The engram — the atom of the memory economy (§2 schema)
# ============================================================================================
@dataclass
class Engram:
    """One unit of memory. Carries the economy's currency (strength), its source (provenance +
    confidence), its emotional stamp (encoded_at), its associations (links) and its earned-usefulness
    bookkeeping (stats). Serialization round-trips exactly (to_dict → from_dict → identity);
    `validate()` rejects a malformed engram before it can be persisted or committed."""
    kind: str
    body: str
    provenance: str = "experienced"
    confidence: float = CONFIDENCE_DEFAULT
    strength: float = STRENGTH_DEFAULT
    encoded_at: EncodedAt = field(default_factory=EncodedAt)
    links: list[str] = field(default_factory=list)          # ids of associated engrams
    stats: dict = field(default_factory=lambda: {           # earned-usefulness bookkeeping (§M-1)
        "recall_count": 0,          # times this engram has been injected into a decision
        "last_recalled_tick": 0,    # recency anchor for strength decay
        "credit_sum": 0.0,          # decaying sum of settled bet credit (the bet ledger, 2.3, writes this)
    })
    id: str = field(default_factory=_new_id)
    created: str = field(default_factory=_now)

    # --- serialization ------------------------------------------------------------------------
    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "kind": self.kind,
            "body": self.body,
            "provenance": self.provenance,
            "confidence": self.confidence,
            "strength": self.strength,
            "encoded_at": self.encoded_at.to_dict(),
            "links": list(self.links),
            "stats": dict(self.stats),
            "created": self.created,
        }

    @staticmethod
    def from_dict(d: dict) -> "Engram":
        if not isinstance(d, dict):
            raise ValueError("engram record must be a dict")
        try:
            kind = d["kind"]
            body = d["body"]
        except KeyError as e:
            raise ValueError(f"engram record missing required field: {e}") from e
        eg = Engram(
            kind=kind,
            body=body,
            provenance=d.get("provenance", "experienced"),
            confidence=float(d.get("confidence", CONFIDENCE_DEFAULT)),
            strength=float(d.get("strength", STRENGTH_DEFAULT)),
            encoded_at=EncodedAt.from_dict(d.get("encoded_at")),
            links=list(d.get("links") or []),
            stats=dict(d.get("stats") or {}),
            id=d.get("id") or _new_id(),
            created=d.get("created") or _now(),
        )
        # Fill any missing stats keys so the shape is stable across schema growth.
        eg.stats.setdefault("recall_count", 0)
        eg.stats.setdefault("last_recalled_tick", 0)
        eg.stats.setdefault("credit_sum", 0.0)
        return eg

    # --- validation ---------------------------------------------------------------------------
    def validate(self) -> "Engram":
        """Raise ValueError if this engram is malformed. Returns self so callers can chain. This is
        the durable-boundary check — the stores call it before persisting, the consolidator before
        committing, so a bad engram never reaches disk or long-term."""
        if self.kind not in KINDS:
            raise ValueError(f"invalid kind {self.kind!r}; must be one of {sorted(KINDS)}")
        if self.provenance not in PROVENANCE:
            raise ValueError(f"invalid provenance {self.provenance!r}; must be one of {sorted(PROVENANCE)}")
        if not isinstance(self.body, str) or not self.body.strip():
            raise ValueError("engram body must be a non-empty string")
        for name, v in (("confidence", self.confidence), ("strength", self.strength)):
            if not isinstance(v, (int, float)) or not (0.0 <= float(v) <= 1.0):
                raise ValueError(f"{name} must be a float in [0, 1], got {v!r}")
        if not isinstance(self.links, list) or not all(isinstance(x, str) for x in self.links):
            raise ValueError("links must be a list of engram ids (strings)")
        if not isinstance(self.stats, dict):
            raise ValueError("stats must be a dict")
        return self

    def is_valid(self) -> bool:
        try:
            self.validate()
            return True
        except ValueError:
            return False


def _overlap(a: str, b: str) -> float:
    """Content-token overlap coefficient in [0, 1] — the same cheap, embedding-free similarity the
    knowledge store uses for dedup (knowledge.most_similar). Consolidator merge policy keys on it so
    a near-restatement of an existing long-term engram merges rather than bloating the store."""
    ta = {t for t in a.lower().split() if len(t) >= 3}
    tb = {t for t in b.lower().split() if len(t) >= 3}
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / min(len(ta), len(tb))


# ============================================================================================
# Stage 1 — the hot trace (the tick's working set; in-memory, cleared each tick)
# ============================================================================================
class HotTrace:
    """Tick-scoped scratch memory. Purely in-RAM, never persisted; `clear()` is called at the top
    of each tick (by 2.2's wiring). It is the working set the current tick is reasoning over —
    what pattern completion pulled up, what afferents just arrived — before any of it is decided to
    be worth remembering. Nothing durable lives here."""

    def __init__(self):
        self._items: list[Engram] = []

    def add(self, engram: Engram) -> Engram:
        engram.validate()
        self._items.append(engram)
        return engram

    def all(self) -> list[Engram]:
        return list(self._items)

    def clear(self) -> None:
        self._items.clear()

    def __len__(self) -> int:
        return len(self._items)


# ============================================================================================
# Stage 2 — the episodic ring (the hippocampus; bounded, FIFO, jsonl-persisted)
# ============================================================================================
def _episodic_path(config) -> Path:
    return config.workspace / "engram_episodic.jsonl"


class EpisodicRing:
    """Fast plastic episodic encoding — the hippocampal stage. A BOUNDED ring (§M-3: forgetting is a
    feature): at capacity, the OLDEST engram is evicted FIFO when a new one is encoded. Persisted as
    jsonl (append-per-encode, then whole-file trim to the cap — the episodes.py pattern), so an
    episode survives a restart but the ring never grows without bound.

    Writes here are OPEN (the episodic tier is meant to fill freely from lived experience); only the
    long-term tier is single-writer-gated. Consolidation (2.4's sleep replay) reads this ring and
    the consolidator promotes the high-strength / high-surprise ones into long-term."""

    def __init__(self, config, *, max_items: int = EPISODIC_RING_MAX):
        self.config = config
        self.max_items = int(max_items)

    def _read_raw(self) -> list[dict]:
        try:
            lines = _episodic_path(self.config).read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            return []
        out: list[dict] = []
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except (ValueError, json.JSONDecodeError):
                continue
        return out

    def load(self) -> list[Engram]:
        """The ring, oldest-first. Corrupt lines are skipped (best-effort read, house convention)."""
        out: list[Engram] = []
        for d in self._read_raw():
            try:
                out.append(Engram.from_dict(d))
            except ValueError:
                continue
        return out

    def encode(self, engram: Engram) -> Engram:
        """Append an engram to the ring, evicting the oldest if over capacity. FIFO — the ring's
        newest `max_items` are what survive. Atomic whole-file rewrite when a trim is needed;
        cheap append otherwise."""
        engram.validate()
        self.config.workspace.mkdir(parents=True, exist_ok=True)
        path = _episodic_path(self.config)
        # Append is the hot path; only rewrite-to-trim when we cross the cap.
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(engram.to_dict(), ensure_ascii=False) + "\n")
        self._trim()
        return engram

    def _trim(self) -> None:
        """Keep only the newest max_items lines — FIFO eviction of the oldest. Atomic temp+replace."""
        path = _episodic_path(self.config)
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            return
        # Drop blanks so the cap counts real records, not whitespace.
        lines = [ln for ln in lines if ln.strip()]
        if len(lines) <= self.max_items:
            return
        kept = lines[-self.max_items:]
        tmp = path.with_suffix(".jsonl.tmp")
        tmp.write_text("\n".join(kept) + "\n", encoding="utf-8")
        tmp.replace(path)

    def __len__(self) -> int:
        return len(self._read_raw())


# ============================================================================================
# Stage 3 — the long-term store (the neocortex; jsonl + npy vectors + index; READ-OPEN)
# ============================================================================================
# The store is deliberately WRITE-CLOSED: it exposes recall/read publicly, but its append is a
# NAME-MANGLED private method (`__append`) reachable only from inside the class body — i.e. only the
# Consolidator (which holds the store and calls the module-level writer) can add to it. External code
# has no public method to write long-term memory (§I6). This is the API-shape enforcement of "one
# consolidator is the single writer."

def _longterm_jsonl_path(config) -> Path:
    return config.knowledge_dir / "engram_longterm.jsonl"


def _longterm_index_path(config) -> Path:
    return config.knowledge_dir / "engram_longterm_index.json"


def _longterm_vectors_path(config) -> Path:
    return config.knowledge_dir / "engram_longterm_vectors.npy"


def _longterm_vector_ids_path(config) -> Path:
    return config.knowledge_dir / "engram_longterm_vector_ids.json"


class LongTermStore:
    """The consolidated (neocortical) tier: jsonl records + a json index + an npy vector sidecar, in
    the house style of the knowledge store (knowledge.py + embedding.py). READ is open — anyone may
    `recall()` or `load()`. WRITE is closed: the only mutator is `__append`, name-mangled so it is
    unreachable except from a `Consolidator` that goes through the module-level `_commit_to_store`
    bridge (see below). There is intentionally NO public `add`/`store`/`write` (§I6)."""

    def __init__(self, config):
        self.config = config

    # --- read (open) --------------------------------------------------------------------------
    def load(self) -> list[Engram]:
        try:
            lines = _longterm_jsonl_path(self.config).read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            return []
        out: list[Engram] = []
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(Engram.from_dict(json.loads(line)))
            except (ValueError, json.JSONDecodeError):
                continue
        return out

    def get(self, engram_id: str) -> Optional[Engram]:
        for e in self.load():
            if e.id == engram_id:
                return e
        return None

    def __len__(self) -> int:
        return len(self.load())

    def recall(self, query: str, *, top_k: int = 5) -> list[Engram]:
        """Semantic recall over long-term memory, ranked by relevance. Mock-aware and fail-open,
        exactly like knowledge.semantic_search: under mock_mode it uses the deterministic hash
        embedder; with no model and no vectors it degrades to a cheap token-overlap fallback so
        recall never hard-fails. (Ranking by relevance × strength is the manager's job in 2.2; this
        surface returns the relevance-ranked candidates.)"""
        query = (query or "").strip()
        if not query:
            return []
        entries = self.load()
        if not entries:
            return []
        vecs, ids = self._load_vectors()
        qv = self._embed(query)
        if vecs is not None and qv is not None and len(ids):
            import numpy as np
            scores = vecs @ qv
            by_id = {e.id: e for e in entries}
            ranked = sorted(zip(scores, ids), key=lambda x: float(x[0]), reverse=True)
            out: list[Engram] = []
            for score, eid in ranked:
                if float(score) <= 0:
                    continue
                e = by_id.get(eid)
                if e is not None:
                    out.append(e)
                if len(out) >= top_k:
                    break
            if out:
                return out
        # Fallback: token-overlap relevance (embedding-free), so recall works in tests/no-model runs.
        scored = sorted(entries, key=lambda e: _overlap(query, e.body), reverse=True)
        return [e for e in scored if _overlap(query, e.body) > 0][:top_k]

    # --- embedding helpers (mock-aware / fail-open, mirroring embedding.py) --------------------
    def _embed(self, text: str):
        try:
            import embedding
            return embedding.embed_query(self.config, text)
        except Exception:  # noqa: BLE001 - embedding is best-effort; recall falls back to overlap
            return None

    def _load_vectors(self):
        vp, ip = _longterm_vectors_path(self.config), _longterm_vector_ids_path(self.config)
        if not vp.exists() or not ip.exists():
            return None, []
        try:
            import numpy as np
            v = np.load(str(vp))
            ids = json.loads(ip.read_text(encoding="utf-8"))
            if v.shape[0] != len(ids):
                return None, []
            return v, ids
        except Exception:  # noqa: BLE001
            return None, []

    # --- write (CLOSED — name-mangled; only _commit_to_store reaches it) ----------------------
    def __append(self, engrams: list[Engram]) -> None:
        """The ONLY mutator of the long-term store. Whole-file atomic rewrite of jsonl + index (both
        cheap, no network); the vector sidecar is synced INCREMENTALLY (only new ids are embedded —
        see __sync_vectors). Private + name-mangled: external code cannot call this (there is no public
        write method), so every long-term write is funnelled through the Consolidator (§I6)."""
        for e in engrams:
            e.validate()
        self.config.knowledge_dir.mkdir(parents=True, exist_ok=True)
        # jsonl (source of truth)
        jp = _longterm_jsonl_path(self.config)
        body = "\n".join(json.dumps(e.to_dict(), ensure_ascii=False) for e in engrams)
        tmp = jp.with_suffix(".jsonl.tmp")
        tmp.write_text(body + ("\n" if body else ""), encoding="utf-8")
        tmp.replace(jp)
        # index (lightweight metadata mirror, knowledge.py convention)
        idx = [{"id": e.id, "kind": e.kind, "provenance": e.provenance,
                "strength": e.strength, "confidence": e.confidence,
                "preview": e.body[:200], "created": e.created} for e in engrams]
        ip = _longterm_index_path(self.config)
        tmp_i = ip.with_suffix(".json.tmp")
        tmp_i.write_text(json.dumps(idx, ensure_ascii=False), encoding="utf-8")
        tmp_i.replace(ip)
        # vector sidecar (embedding-gated + mock-aware; skipped when no embedder — recall falls back)
        self.__sync_vectors(engrams)

    def __sync_vectors(self, engrams: list[Engram], *, force: bool = False) -> None:
        """Bring the npy vector sidecar in line with the current long-term set, embedding ONLY the
        engrams that don't yet have a stored vector (matched by id) and REUSING the cached vector for
        the rest. Engram bodies are immutable per id (merge/update_strength never rewrite a body), so
        an id already in the sidecar is already correctly embedded — this keeps an ordinary
        single-engram commit at exactly ONE embed call instead of re-embedding all N every tick (the
        scaling bottleneck: N embed HTTP calls per acting tick). Mirrors embedding.embed_and_store's
        id-keyed incremental merge and embedding._save_vectors' atomic temp+replace.

        Best-effort: if no embedder is available for a NEW engram (no model, embedding off) it removes
        any stale sidecar so recall uses the token-overlap fallback. `force=True` ignores the cache
        and re-embeds everything — the repair/migration path (see rebuild_vectors)."""
        try:
            import numpy as np
            cache: dict[str, "np.ndarray"] = {}
            if not force:
                prev_v, prev_ids = self._load_vectors()
                if prev_v is not None:
                    cache = {eid: prev_v[i] for i, eid in enumerate(prev_ids)}
            vecs = []
            ids = []
            dim: Optional[int] = None
            for e in engrams:
                v = cache.get(e.id)
                if v is None:
                    emb = self._embed(e.body)          # only NEW ids reach the embed service
                    if emb is None:
                        vecs = []      # no embedder for a new engram — abandon the sidecar (fall back to overlap)
                        break
                    v = np.asarray(emb, dtype=np.float32)
                v = np.asarray(v, dtype=np.float32).reshape(1, -1)
                # Dimension guard: a swapped embedding model makes cached vectors incompatible (vstack
                # would raise). Drop the stale cache and re-embed from this engram onward.
                if dim is not None and v.shape[1] != dim:
                    return self.__sync_vectors(engrams, force=True)
                dim = v.shape[1]
                vecs.append(v)
                ids.append(e.id)
            vp, ip = _longterm_vectors_path(self.config), _longterm_vector_ids_path(self.config)
            if vecs:
                arr = np.vstack(vecs)
                tmp_v = vp.with_suffix(".tmp.npy")
                tmp_i = ip.with_suffix(".tmp.json")
                np.save(str(tmp_v), arr)
                tmp_i.write_text(json.dumps(ids), encoding="utf-8")
                import os
                os.replace(str(tmp_v), str(vp))
                os.replace(str(tmp_i), str(ip))
            else:
                for p in (vp, ip):
                    try:
                        p.unlink(missing_ok=True)
                    except OSError:
                        pass
        except Exception:  # noqa: BLE001 - the vector sidecar is an optimization; recall degrades gracefully
            pass

    def rebuild_vectors(self) -> None:
        """Repair/migration: re-embed EVERY long-term engram from scratch, discarding the cached
        sidecar. O(n) embed calls — for one-shot recovery (corrupt/stale sidecar, swapped embedding
        model), NOT the per-tick write path. Reads engram CONTENT only from the jsonl source of truth
        and rewrites the derived vector index; it adds/removes no engrams, so it does not breach the
        single-writer contract (§I6)."""
        self.__sync_vectors(self.load(), force=True)


def _commit_to_store(store: "LongTermStore", engrams: list[Engram]) -> None:
    """The single bridge into LongTermStore's name-mangled writer. Only the Consolidator calls this;
    keeping it a module-level function (rather than a public method) means the store's class surface
    exposes no write to external callers — the API SHAPE enforces single-writer (§I6)."""
    # Reach the name-mangled private method deliberately — this bridge IS the sanctioned single writer.
    store._LongTermStore__append(engrams)  # noqa: SLF001 - the one sanctioned write path


# ============================================================================================
# The Consolidator — the SINGLE WRITER of long-term memory (§I6)
# ============================================================================================
class Consolidator:
    """The one writer of the long-term store (§I6, PILLARS_PLAN §2: "one consolidator is the single
    writer of every long-term store"). All promotion of episodic/hot engrams into consolidated
    long-term memory goes through `commit()` / `merge()`. Consolidation policy — pattern-separation
    dedup (near-restatements merge rather than duplicate), strength/provenance carry-through — lives
    here, in one place, and cannot be bypassed because the store exposes no other write.

    The heavier consolidation JOBS (sharp-wave-ripple replay of high-strength engrams, gist
    extraction, synaptic downscaling) are the sleep engine's (2.4); this class is the WRITE GATE
    they all funnel through."""

    def __init__(self, config, *, store: Optional[LongTermStore] = None,
                 merge_threshold: float = LONGTERM_MERGE_THRESHOLD):
        self.config = config
        self.store = store or LongTermStore(config)
        self.merge_threshold = float(merge_threshold)

    def commit(self, engram: Engram) -> Engram:
        """Promote one engram into long-term memory. Pattern separation (§2): if it near-restates an
        existing long-term engram (overlap ≥ merge_threshold), MERGE into that one instead of adding
        a duplicate; otherwise append it. Returns the engram now living in long-term (the survivor on
        a merge, else the committed engram)."""
        engram.validate()
        existing = self.store.load()
        for e in existing:
            if e.kind == engram.kind and _overlap(engram.body, e.body) >= self.merge_threshold:
                return self.merge(e, engram)
        _commit_to_store(self.store, existing + [engram])
        return engram

    def merge(self, keeper: Engram, incoming: Engram) -> Engram:
        """Fold `incoming` into `keeper` (pattern separation kept them apart until they proved to be
        the same memory). The survivor keeps the STRONGER strength/confidence (a corroborated memory
        is at least as strong as either witness), unions their links, and sums their recall credit —
        consolidation should reinforce, not dilute. Persists the merged long-term set."""
        keeper.validate()
        incoming.validate()
        keeper.strength = max(keeper.strength, incoming.strength)
        keeper.confidence = max(keeper.confidence, incoming.confidence)
        # Corroboration (§M-2, pitfall #5's exit door): a DREAMED keeper restated by a non-dream
        # witness has met reality — it takes the witness's source grade and sheds the hypothesis
        # stamp. SCOPED to dreamed only: other grades keep the keeper's provenance, because source
        # monitoring is history and the bet ledger's inherited strength-floor semantics depend on
        # 'inherited' surviving a merge (a nugget must not lose its floor by being confirmed once).
        if keeper.provenance == "dreamed" and incoming.provenance != "dreamed":
            keeper.provenance = incoming.provenance
        if keeper.stats.get("dreamed") and not incoming.stats.get("dreamed"):
            keeper.stats.pop("dreamed", None)
        keeper.links = list(dict.fromkeys(list(keeper.links) + list(incoming.links)))
        keeper.stats["recall_count"] = int(keeper.stats.get("recall_count", 0)) + int(incoming.stats.get("recall_count", 0))
        keeper.stats["credit_sum"] = float(keeper.stats.get("credit_sum", 0.0)) + float(incoming.stats.get("credit_sum", 0.0))
        keeper.stats["last_recalled_tick"] = max(int(keeper.stats.get("last_recalled_tick", 0)),
                                                 int(incoming.stats.get("last_recalled_tick", 0)))
        current = self.store.load()
        merged = [keeper if e.id == keeper.id else e for e in current]
        if keeper.id not in {e.id for e in current}:
            merged.append(keeper)   # keeper wasn't yet in the store (committing two fresh near-dups)
        _commit_to_store(self.store, merged)
        return keeper

    def update_strength(self, engram_id: str, new_strength: float, *,
                        recalled_tick: Optional[int] = None,
                        credit_delta: float = 0.0) -> Optional[Engram]:
        """Re-persist a long-term engram's earned strength + recall bookkeeping — the write half of
        the recall-utility loop (the bet ledger in 2.3 computes the new strength; this commits it).
        Mutates stats (recall_count, last_recalled_tick, credit_sum) and re-persists through the
        single writer. Returns the updated engram, or None if the id is not in long-term."""
        current = self.store.load()
        target = next((e for e in current if e.id == engram_id), None)
        if target is None:
            return None
        target.strength = max(0.0, min(1.0, float(new_strength)))
        target.stats["recall_count"] = int(target.stats.get("recall_count", 0)) + 1
        target.stats["credit_sum"] = float(target.stats.get("credit_sum", 0.0)) + float(credit_delta)
        if recalled_tick is not None:
            target.stats["last_recalled_tick"] = int(recalled_tick)
        target.validate()
        _commit_to_store(self.store, current)
        return target
