"""Long-term knowledge store — persistent facts on the SD card.

Entries are small markdown files with YAML front-matter, stored under
workspace/knowledge/{category}/.  A JSON index caches metadata for fast
lookup.  BM25 is used for keyword search at tick time (no embedding model
required).
"""

import json
import logging
import os
import re
import tempfile
import time
from pathlib import Path
from typing import Optional

import yaml

from config import Config
from atomicio import replace_with_retry

logger = logging.getLogger("eidos.knowledge")

# Valid category names — doubles as subdirectory names
CATEGORIES = ("facts", "procedures", "errors", "reflections")


# ---------------------------------------------------------------------------
# Semantic novelty / near-duplicate detection (token Jaccard — cheap, no embedding model)
# ---------------------------------------------------------------------------

_IP_RE = re.compile(r"\d+\.\d+\.\d+\.\d+")
_STOP = set("the a an is are was were be been being at on in of to for and or with via from this that "
            "it its you your i we are has have had will would can could should it's about as by an".split())


def _content_toks(s: str) -> set:
    """Meaningful content tokens — punctuation-stripped, stopwords + very short tokens dropped — so
    rewordings of the same fact share most of them (Jaccard over raw tokens missed this badly)."""
    return {t for t in re.findall(r"[a-z0-9]+", (s or "").lower()) if len(t) >= 3 and t not in _STOP}


def _ips(s: str) -> set:
    return set(_IP_RE.findall(s or ""))


def most_similar(config, content: str):
    """(best_score, best_id) of the most similar existing entry, using an OVERLAP coefficient over
    content tokens gated by SUBJECT (IP): two facts about different IPs are never the same fact (so
    '.45 web UI' and '.46 web UI' stay distinct), but rewordings about the same subject score high.
    Powers near-dup dedup AND the novelty signal for goal-tension (low score = genuinely new info)."""
    ct, ips = _content_toks(content), _ips(content)
    if not ct:
        return (0.0, None)
    best = (0.0, None)
    for e in load_index(config):
        cp = e.get("content_preview") or ""
        eips = _ips(cp)
        if ips and eips and not (ips & eips):
            continue  # different named subjects → different facts
        ect = _content_toks(cp)
        if not ect:
            continue
        s = len(ct & ect) / min(len(ct), len(ect))  # overlap coefficient
        if s > best[0]:
            best = (s, e.get("id"))
    return best


def is_novel(config, content: str, threshold: float = 0.65) -> bool:
    """True if `content` is not a near-duplicate of anything already known (new information)."""
    return most_similar(config, content)[0] < threshold


def text_overlap(a: str, b: str) -> float:
    """Overlap COEFFICIENT over content tokens (|A∩B| / min(|A|,|B|)) — the containment-style
    similarity the knowledge/skill economies use, where a short fact fully inside a longer one IS
    a duplicate. Subject (IP) gating: two strings naming different IPs never overlap. 0.0 when
    either is empty. NOTE: this scores 1.0 for any subset, so it is WRONG for 'are these the same
    commitment?' (a subset title is often a smaller/different goal) — use token_jaccard for that."""
    ta, tb = _content_toks(a), _content_toks(b)
    if not ta or not tb:
        return 0.0
    ia, ib = _ips(a), _ips(b)
    if ia and ib and not (ia & ib):
        return 0.0
    return len(ta & tb) / min(len(ta), len(tb))


def token_jaccard(a: str, b: str) -> float:
    """Jaccard (|A∩B| / |A∪B|) over content tokens — the SYMMETRIC similarity, using the same one
    tokenizer. Unlike the overlap coefficient it PENALISES divergent scope: extra tokens in either
    string lower the score, so an elaboration of a goal ('Skill Library' vs 'Skill Library
    Foundation' = 0.67) stays high while a distinct larger commitment ('Skill Library' vs 'Skill
    Library Governance Board' = 0.5) drops below a merge bar. No IP gating — goals rarely denote
    hosts, and a dotted-quad version string must not force two goals apart. 0.0 when either empty."""
    ta, tb = _content_toks(a), _content_toks(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


# ---------------------------------------------------------------------------
# Entry data helpers
# ---------------------------------------------------------------------------

def _make_id(content: str, category: str) -> str:
    """Generate a filesystem-safe ID from content + category."""
    # Take first ~60 chars, collapse to alphanum + underscores
    slug = re.sub(r"[^a-z0-9]+", "_", content[:60].lower()).strip("_")
    if not slug:
        slug = "entry"
    ts = time.strftime("%Y%m%d_%H%M%S", time.gmtime())
    return f"{slug}_{ts}"


def _parse_frontmatter(text: str) -> tuple[dict, str]:
    """Parse YAML front-matter delimited by --- lines.

    Returns (metadata_dict, body_text).  Returns ({}, full_text) on
    missing or corrupt front-matter.
    """
    if not text.startswith("---"):
        return {}, text
    parts = text.split("---", 2)
    if len(parts) < 3:
        return {}, text
    try:
        meta = yaml.safe_load(parts[1])
        if not isinstance(meta, dict):
            return {}, text
        return meta, parts[2].strip()
    except yaml.YAMLError:
        return {}, text


def _render_entry(meta: dict, body: str) -> str:
    """Render an entry as markdown with YAML front-matter."""
    fm = yaml.dump(meta, default_flow_style=False, sort_keys=False).strip()
    return f"---\n{fm}\n---\n{body}\n"


# ---------------------------------------------------------------------------
# CRUD operations
# ---------------------------------------------------------------------------

def ensure_dirs(config: Config) -> None:
    """Create knowledge directory tree if missing."""
    for cat in CATEGORIES:
        (config.knowledge_dir / cat).mkdir(parents=True, exist_ok=True)


# Confidence as a rank, so a CORRECTION can outrank the belief it fixes. Free-string values map to
# the nearest rung. An UNKNOWN label ranks at the MIDDLE (1), never the floor: we must not let a
# merely-"likely" restatement silently downgrade a belief whose (non-vocabulary) label we can't
# read — only a clearly-higher confidence supersedes it.
_CONF_RANK = {"tentative": 0, "hypothesis": 0, "guess": 0, "unsure": 0, "low": 0, "weak": 0,
              "likely": 1, "probable": 1, "plausible": 1, "medium": 1, "moderate": 1,
              "confident": 2, "high": 2, "strong": 2,
              "verified": 3, "certain": 3, "confirmed": 3, "validated": 3, "definite": 3}
_CONF_UNKNOWN = 1   # a label we don't recognise sits mid — supersedable only by a clearly-higher one


def _conf_rank(c: str) -> int:
    return _CONF_RANK.get((c or "").strip().lower(), _CONF_UNKNOWN)


def _supersede_entry(config: Config, entry_id: str, content: str, tags: list[str],
                     confidence: str) -> bool:
    """Overwrite an existing entry's content/confidence/tags in place (the CORRECTION path). A mind
    must be able to fix a wrong memory; the dedup guard used to return the stale entry unchanged,
    so a higher-confidence correction was silently swallowed (a 'verified: X was never true' could
    not replace a 'tentative: X'). Rewrites the .md and the index entry, keeps the id/created."""
    index = load_index(config)
    for item in index:
        if item.get("id") != entry_id:
            continue
        cat = item.get("category", "facts")
        path = config.knowledge_dir / cat / f"{entry_id}.md"
        merged_tags = sorted(set(item.get("tags", [])) | set(tags))
        now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        # Bi-temporal correction (SOTA#10 idea, no external dep): record the PRIOR belief and WHEN it
        # was superseded instead of silently dropping it — the correction becomes non-destructive and
        # auditable ("I used to think X until T, now Y"), which is temporal self-consistency, part of
        # not-going-off-track. Only the newest link is kept (bounded, not a full history log).
        prior = {"content_preview": (item.get("content_preview") or "")[:300],
                 "confidence": item.get("confidence", ""),
                 "at": item.get("updated") or item.get("created") or now}
        meta = {"id": entry_id, "category": cat, "tags": merged_tags, "confidence": confidence,
                "source_goal": item.get("source_goal", ""), "source_tick": item.get("source_tick", 0),
                "created": item.get("created", now), "updated": now,
                "superseded_at": now, "prior": prior}
        try:
            ensure_dirs(config)
            fd, tmp = tempfile.mkstemp(dir=str(config.knowledge_dir / cat), prefix=".kn_", suffix=".tmp")
            with os.fdopen(fd, "w") as f:
                f.write(_render_entry(meta, content))
            replace_with_retry(tmp, str(path))
        except Exception:  # noqa: BLE001 - a failed correction must not corrupt the store
            return False
        item.update({"tags": merged_tags, "confidence": confidence, "updated": now,
                     "content_preview": content[:1500], "superseded_at": now, "prior": prior})
        _write_index(config, index)
        _invalidate_bm25_cache()
        logger.info("superseded knowledge entry %s → confidence=%s", entry_id, confidence)
        return True
    return False


def store_entry(
    config: Config,
    content: str,
    tags: list[str],
    category: str = "facts",
    confidence: str = "tentative",
    source_goal: str = "",
    source_tick: int = 0,
) -> str:
    """Write a knowledge entry to disk and update the index.

    Returns the entry ID.
    """
    if category not in CATEGORIES:
        category = "facts"
    tags = [t.strip().lower() for t in tags if t.strip()]

    # Reject near-duplicates at the source (token-similarity, not just exact). The store bloated to
    # 265 entries for one small LAN because the agent kept re-writing the same fact in different words
    # (7+ "MQTT broker at .25" restatements). If a near-identical entry exists we DON'T grow the store
    # — but a higher-CONFIDENCE near-dup is a CORRECTION, not a restatement: overwrite the stale entry
    # in place so a mind can fix its own memory (else 'verified: X was never locked' was swallowed by
    # the 'tentative: X is locked' it meant to replace). Same-or-lower confidence → return unchanged.
    sim, sid = most_similar(config, content)
    if sid and sim >= float(config.knowledge_dedup_threshold):
        existing = next((e for e in load_index(config) if e.get("id") == sid), None)
        if existing is not None and _conf_rank(confidence) > _conf_rank(existing.get("confidence")):
            if _supersede_entry(config, sid, content, tags, confidence):
                return sid
        return sid

    entry_id = _make_id(content, category)
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    meta = {
        "id": entry_id,
        "category": category,
        "tags": tags,
        "confidence": confidence,
        "source_goal": source_goal,
        "source_tick": source_tick,
        "created": now,
        "updated": now,
    }

    ensure_dirs(config)
    entry_path = config.knowledge_dir / category / f"{entry_id}.md"
    entry_text = _render_entry(meta, content)

    # Atomic write
    fd, tmp_path = tempfile.mkstemp(
        dir=str(config.knowledge_dir / category),
        prefix=".kn_",
        suffix=".tmp",
    )
    try:
        with os.fdopen(fd, "w") as f:
            f.write(entry_text)
        replace_with_retry(tmp_path, str(entry_path))
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise

    # Update index
    _add_to_index(config, meta, content)
    logger.info("stored knowledge entry %s [%s] tags=%s", entry_id, category, tags)
    return entry_id


def read_entry(config: Config, entry_id: str) -> Optional[dict]:
    """Read a single entry by ID.  Returns dict with 'meta' and 'body', or None."""
    index = load_index(config)
    for item in index:
        if item["id"] == entry_id:
            cat = item.get("category", "facts")
            path = config.knowledge_dir / cat / f"{entry_id}.md"
            try:
                text = path.read_text()
                meta, body = _parse_frontmatter(text)
                return {"meta": meta, "body": body}
            except OSError:
                return None
    return None


def delete_entry(config: Config, entry_id: str) -> bool:
    """Delete an entry by ID.  Returns True if deleted."""
    index = load_index(config)
    found = None
    for item in index:
        if item["id"] == entry_id:
            found = item
            break
    if not found:
        return False

    # Remove file
    cat = found.get("category", "facts")
    path = config.knowledge_dir / cat / f"{entry_id}.md"
    try:
        path.unlink()
    except FileNotFoundError:
        pass

    # Update index
    new_index = [i for i in index if i["id"] != entry_id]
    _write_index(config, new_index)
    _invalidate_bm25_cache()
    return True


def count_entries(config: Config) -> int:
    """Count entries in the index."""
    return len(load_index(config))


# ---------------------------------------------------------------------------
# Index management
# ---------------------------------------------------------------------------

_index_cache: Optional[list[dict]] = None
_index_mtime: float = 0.0


def load_index(config: Config) -> list[dict]:
    """Load the index from disk, with in-memory cache."""
    global _index_cache, _index_mtime
    idx_path = config.knowledge_index_path
    try:
        mtime = idx_path.stat().st_mtime
    except OSError:
        _index_cache = []
        _index_mtime = 0
        return []

    if _index_cache is not None and mtime == _index_mtime:
        return _index_cache

    try:
        _index_cache = json.loads(idx_path.read_text())
        _index_mtime = mtime
    except (json.JSONDecodeError, OSError):
        _index_cache = []
        _index_mtime = 0

    return _index_cache


def _write_index(config: Config, index: list[dict]) -> None:
    """Atomically write the index to disk."""
    global _index_cache, _index_mtime
    ensure_dirs(config)
    tmp = config.knowledge_index_path.with_suffix(".tmp")
    tmp.write_text(json.dumps(index, indent=2))
    replace_with_retry(tmp, config.knowledge_index_path)
    _index_cache = index
    try:
        _index_mtime = config.knowledge_index_path.stat().st_mtime
    except OSError:
        _index_mtime = 0


def _add_to_index(config: Config, meta: dict, content: str) -> None:
    """Add an entry's metadata + content snippet to the index."""
    index = load_index(config)
    # Store content in index for BM25 search without reading files. Generous cap so concise
    # nuggets are kept in full (the dashboard renders this; recall is separately budgeted).
    entry = dict(meta)
    entry["content_preview"] = content[:1500]
    index.append(entry)
    _write_index(config, index)
    _invalidate_bm25_cache()


def rebuild_index(config: Config) -> int:
    """Rebuild index.json from all entry files on disk.

    Returns the number of entries indexed.
    """
    entries = []
    ensure_dirs(config)
    for cat in CATEGORIES:
        cat_dir = config.knowledge_dir / cat
        for path in sorted(cat_dir.glob("*.md")):
            try:
                text = path.read_text()
                meta, body = _parse_frontmatter(text)
                if not meta.get("id"):
                    meta["id"] = path.stem
                meta.setdefault("category", cat)
                entry = dict(meta)
                entry["content_preview"] = body[:1500]
                entries.append(entry)
            except OSError:
                continue
    _write_index(config, entries)
    logger.info("rebuilt knowledge index: %d entries", len(entries))
    return len(entries)


# ---------------------------------------------------------------------------
# BM25 search
# ---------------------------------------------------------------------------

_bm25_instance = None
_bm25_corpus_ids: list[str] = []
_bm25_mtime: float = -1.0  # the _index_mtime this BM25 was built against (-1 = never built)


def _invalidate_bm25_cache():
    global _bm25_instance, _bm25_corpus_ids, _bm25_mtime
    _bm25_instance = None
    _bm25_corpus_ids = []
    _bm25_mtime = -1.0


def _build_bm25(config: Config):
    """Build/rebuild BM25 index from the knowledge index."""
    global _bm25_instance, _bm25_corpus_ids, _bm25_mtime
    from rank_bm25 import BM25Okapi

    index = load_index(config)
    _bm25_mtime = _index_mtime  # stamp the corpus with the index version it reflects
    if not index:
        _bm25_instance = None
        _bm25_corpus_ids = []
        return

    corpus = []
    ids = []
    for item in index:
        # Tokenize: tags + content preview
        text = " ".join(item.get("tags", []))
        text += " " + item.get("content_preview", "")
        text += " " + item.get("category", "")
        tokens = text.lower().split()
        corpus.append(tokens)
        ids.append(item["id"])

    _bm25_instance = BM25Okapi(corpus)
    _bm25_corpus_ids = ids


def search_bm25(config: Config, query: str, top_k: int = 5) -> list[dict]:
    """Search the knowledge store by BM25 relevance.

    Returns list of dicts with 'id', 'category', 'tags', 'content_preview',
    'score', sorted by descending score.
    """
    if not config.knowledge_enabled:
        return []

    index = load_index(config)
    if not index:
        return []

    # Rebuild when the instance is cold, the corpus SIZE changed, OR the index CONTENT changed at
    # the same length (a cross-process supersede/correction edits an entry in place — length-only
    # invalidation would keep ranking against the stale text). _index_mtime is refreshed by the
    # load_index call above.
    if (_bm25_instance is None or len(_bm25_corpus_ids) != len(index)
            or _bm25_mtime != _index_mtime):
        _build_bm25(config)

    if _bm25_instance is None:
        return []

    tokens = query.lower().split()
    if not tokens:
        return []

    scores = _bm25_instance.get_scores(tokens)

    # Build result list from index (lookup by ID)
    index_map = {item["id"]: item for item in index}

    # Recency tilt (flag-gated): BM25 is timeless, but the world is not — a device re-scanned
    # today should outrank a stale note about it. The shared engram.recency_factor is floored,
    # so age re-orders near-ties without burying an old entry that is the only real match.
    if getattr(config, "pillars_recall_recency_enabled", False):
        import engram as _engram
        scores = [s * _engram.recency_factor(index_map.get(eid, {}).get("created", ""))
                  if s > 0 else s
                  for s, eid in zip(scores, _bm25_corpus_ids)]

    # Pair scores with IDs, sort descending
    scored = sorted(zip(scores, _bm25_corpus_ids), key=lambda x: x[0], reverse=True)
    # Relative noise floor: BM25 scores are corpus-scaled (no absolute threshold works), but
    # a result far below the TOP hit is filler, not relevance — a generic step like "explore"
    # used to pull arbitrary old facts into context just for having one weak term in common.
    floor = scored[0][0] * 0.2 if scored and scored[0][0] > 0 else None
    results = []
    for score, entry_id in scored[:top_k]:
        # score == 0 means no token overlap at all; negative scores can occur
        # with small corpora (BM25 IDF goes negative when df >= N/2) but still
        # indicate genuine term matches, so we keep them (only when nothing positive ranks).
        if score == 0:
            continue
        if floor is not None and score < floor:
            continue
        item = index_map.get(entry_id)
        if item:
            results.append({
                "id": item["id"],
                "category": item.get("category", "facts"),
                "tags": item.get("tags", []),
                "confidence": item.get("confidence", "tentative"),
                "content_preview": item.get("content_preview", ""),
                "score": float(score),
            })

    return results


def search_tags(config: Config, tags: list[str], top_k: int = 5) -> list[dict]:
    """Search by tag intersection.  Returns entries matching any given tag."""
    if not config.knowledge_enabled:
        return []

    index = load_index(config)
    tags_lower = {t.lower() for t in tags}
    matches = []
    for item in index:
        item_tags = {t.lower() for t in item.get("tags", [])}
        overlap = len(tags_lower & item_tags)
        if overlap > 0:
            matches.append((overlap, item))

    matches.sort(key=lambda x: x[0], reverse=True)
    results = []
    for overlap, item in matches[:top_k]:
        results.append({
            "id": item["id"],
            "category": item.get("category", "facts"),
            "tags": item.get("tags", []),
            "confidence": item.get("confidence", "tentative"),
            "content_preview": item.get("content_preview", ""),
            "score": float(overlap),
        })
    return results


def recent_learned(config: Config, limit: int = 12) -> list[dict]:
    """The agent's most-recent LEARNED knowledge, newest-first, de-duplicated.

    'Learned' = anything it discovered/distilled at runtime (memorize, dream extraction); it
    EXCLUDES bootstrap seeds (source_goal == 'seed'). This is the deterministic 'world model'
    surface — what eiDOS actually knows is always shown, instead of being hidden behind a BM25
    query that (keyed on the static goal) only ever returned generic seeds. Cures write-only memory.
    """
    idx = load_index(config)
    learned = [e for e in idx if (e.get("source_goal") or "") != "seed"]
    learned.sort(key=lambda e: e.get("created", ""), reverse=True)  # newest first
    out, seen = [], set()
    for e in learned:
        key = " ".join((e.get("content_preview") or "").lower().split())[:90]
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(e)
        if len(out) >= limit:
            break
    return out


def format_recalled(entries: list[dict], max_chars: int = 1200) -> str:
    """Format recalled knowledge entries for injection into the context window."""
    if not entries:
        return ""

    lines = []
    total = 0
    for e in entries:
        cat_label = e.get("category", "fact").upper().rstrip("S")  # "facts" → "FACT"
        tags = ", ".join(e.get("tags", []))
        conf = e.get("confidence", "")
        content = e.get("content_preview", "").strip()
        line = f"[{cat_label}] ({tags}) {content}"
        if conf and conf != "tentative":
            line += f" [{conf}]"

        if total + len(line) > max_chars:
            break
        lines.append(line)
        total += len(line) + 1  # +1 for newline

    return "\n".join(lines)
