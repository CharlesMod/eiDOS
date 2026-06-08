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
    # Store content in index for BM25 search without reading files
    entry = dict(meta)
    entry["content_preview"] = content[:500]
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
                entry["content_preview"] = body[:500]
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


def _invalidate_bm25_cache():
    global _bm25_instance, _bm25_corpus_ids
    _bm25_instance = None
    _bm25_corpus_ids = []


def _build_bm25(config: Config):
    """Build/rebuild BM25 index from the knowledge index."""
    global _bm25_instance, _bm25_corpus_ids
    from rank_bm25 import BM25Okapi

    index = load_index(config)
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

    if _bm25_instance is None or len(_bm25_corpus_ids) != len(index):
        _build_bm25(config)

    if _bm25_instance is None:
        return []

    tokens = query.lower().split()
    if not tokens:
        return []

    scores = _bm25_instance.get_scores(tokens)

    # Pair scores with IDs, sort descending
    scored = sorted(zip(scores, _bm25_corpus_ids), key=lambda x: x[0], reverse=True)

    # Build result list from index (lookup by ID)
    index_map = {item["id"]: item for item in index}
    results = []
    for score, entry_id in scored[:top_k]:
        # score == 0 means no token overlap at all; negative scores can occur
        # with small corpora (BM25 IDF goes negative when df >= N/2) but still
        # indicate genuine term matches, so we keep them.
        if score == 0:
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
