"""Sentence embedding via ONNX — optional semantic search for the knowledge store.

Uses all-MiniLM-L6-v2 (384-dim vectors, ~90MB ONNX model).  The model is
loaded on demand and can be kept resident or loaded/unloaded per dream cycle
depending on available RAM.

When ONNX Runtime is not installed this module degrades gracefully: all
public functions return empty results without error.
"""

import json
import logging
import os
from pathlib import Path
from typing import Optional

import numpy as np

from config import Config

logger = logging.getLogger("eidos.embedding")

# ---------------------------------------------------------------------------
# Tokeniser helpers (basic whitespace + subword approximation)
# ---------------------------------------------------------------------------
# MiniLM expects WordPiece tokens but shipping the full tokenizer is heavy.
# We use a lightweight regex tokenizer and let ONNX handle the rest via the
# model's own tokenizer when available.  For the ONNX path we feed raw text
# and the model's token_type_ids / attention_mask inputs.

_model_session = None
_model_path: Optional[str] = None

# ---------------------------------------------------------------------------
# Vector store on disk (numpy .npy files)
# ---------------------------------------------------------------------------

def _vectors_path(config: Config) -> Path:
    return config.knowledge_dir / "vectors.npy"


def _ids_path(config: Config) -> Path:
    return config.knowledge_dir / "vector_ids.json"


def _load_vectors(config: Config) -> tuple:
    """Load stored vectors and their corresponding entry IDs.

    Returns (vectors_np_array, id_list) or (None, []) if not available.
    """
    vp = _vectors_path(config)
    ip = _ids_path(config)
    if not vp.exists() or not ip.exists():
        return None, []
    try:
        vectors = np.load(str(vp))
        ids = json.loads(ip.read_text())
        if vectors.shape[0] != len(ids):
            logger.warning("embedding: vectors/ids length mismatch, ignoring store")
            return None, []
        return vectors, ids
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        logger.warning("embedding: failed to load vectors: %s", exc)
        return None, []


def _save_vectors(config: Config, vectors: "np.ndarray", ids: list[str]) -> None:
    """Atomically save vectors and IDs to disk."""
    config.knowledge_dir.mkdir(parents=True, exist_ok=True)
    vp = _vectors_path(config)
    ip = _ids_path(config)

    # Atomic write via temp files
    tmp_v = vp.with_suffix(".tmp.npy")
    tmp_i = ip.with_suffix(".tmp.json")
    try:
        np.save(str(tmp_v), vectors)
        tmp_i.write_text(json.dumps(ids))
        os.replace(str(tmp_v), str(vp))
        os.replace(str(tmp_i), str(ip))
    except Exception:
        for f in (tmp_v, tmp_i):
            try:
                f.unlink(missing_ok=True)
            except OSError:
                pass
        raise


# ---------------------------------------------------------------------------
# Model management
# ---------------------------------------------------------------------------

def _default_model_dir() -> Path:
    """Default location for the ONNX model."""
    return Path("models") / "all-MiniLM-L6-v2"


def model_available(config: Config) -> bool:
    """Check if the ONNX model files exist on disk."""
    model_dir = getattr(config, "embedding_model_dir", None)
    if model_dir:
        d = Path(model_dir)
    else:
        d = _default_model_dir()
    return (d / "model.onnx").exists()


def load_model(config: Config) -> bool:
    """Load the ONNX model into memory.  Returns True on success."""
    global _model_session, _model_path

    try:
        import onnxruntime as ort  # noqa: F811
    except ImportError:
        logger.info("embedding: onnxruntime not installed — semantic search disabled")
        return False

    model_dir = getattr(config, "embedding_model_dir", None)
    if model_dir:
        d = Path(model_dir)
    else:
        d = _default_model_dir()

    onnx_path = d / "model.onnx"
    if not onnx_path.exists():
        logger.info("embedding: model not found at %s", onnx_path)
        return False

    if _model_session is not None and _model_path == str(onnx_path):
        return True  # Already loaded

    try:
        sess_opts = ort.SessionOptions()
        sess_opts.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
        sess_opts.intra_op_num_threads = 2  # leave cores free for the agent
        _model_session = ort.InferenceSession(str(onnx_path), sess_opts)
        _model_path = str(onnx_path)
        logger.info("embedding: loaded model from %s", onnx_path)
        return True
    except Exception as exc:
        logger.warning("embedding: failed to load model: %s", exc)
        _model_session = None
        _model_path = None
        return False


def unload_model() -> None:
    """Release the ONNX model from memory."""
    global _model_session, _model_path
    _model_session = None
    _model_path = None
    logger.info("embedding: model unloaded")


def is_loaded() -> bool:
    return _model_session is not None


# ---------------------------------------------------------------------------
# Embedding
# ---------------------------------------------------------------------------

def embed_texts(texts: list[str]) -> Optional["np.ndarray"]:
    """Embed a list of texts using the loaded model.

    Returns numpy array of shape (N, 384) or None if model not loaded.
    Uses the ONNX model's built-in tokenizer (expects input_ids, attention_mask,
    token_type_ids inputs).
    """
    if _model_session is None:
        return None

    try:
        from tokenizers import Tokenizer  # huggingface tokenizers, lightweight
    except ImportError:
        # Fallback: use a simple word-piece approximation
        return _embed_texts_simple(texts)

    model_dir = Path(_model_path).parent
    tok_path = model_dir / "tokenizer.json"
    if not tok_path.exists():
        return _embed_texts_simple(texts)

    tokenizer = Tokenizer.from_file(str(tok_path))
    tokenizer.enable_padding(pad_id=0, pad_token="[PAD]")
    tokenizer.enable_truncation(max_length=128)

    encodings = tokenizer.encode_batch(texts)
    input_ids = np.array([e.ids for e in encodings], dtype=np.int64)
    attention_mask = np.array([e.attention_mask for e in encodings], dtype=np.int64)
    token_type_ids = np.zeros_like(input_ids)

    outputs = _model_session.run(
        None,
        {
            "input_ids": input_ids,
            "attention_mask": attention_mask,
            "token_type_ids": token_type_ids,
        },
    )

    # Mean pooling over token embeddings, masked
    token_embeddings = outputs[0]  # (N, seq_len, 384)
    mask_expanded = attention_mask[:, :, np.newaxis].astype(np.float32)
    summed = (token_embeddings * mask_expanded).sum(axis=1)
    counts = mask_expanded.sum(axis=1).clip(min=1e-9)
    sentence_embeddings = summed / counts

    # L2 normalise
    norms = np.linalg.norm(sentence_embeddings, axis=1, keepdims=True).clip(min=1e-9)
    return sentence_embeddings / norms


def _embed_texts_simple(texts: list[str]) -> Optional["np.ndarray"]:
    """Fallback: feed texts as simple whitespace-tokenized input.

    This only works with models that accept string inputs.
    For most ONNX models exported from sentence-transformers, the proper
    tokenizer path above is needed.  Returns None if this approach fails.
    """
    # Try the model's input names to see if it accepts raw strings
    input_names = [inp.name for inp in _model_session.get_inputs()]
    if "input_ids" in input_names:
        # Needs proper tokenizer — can't do simple fallback
        logger.warning("embedding: tokenizer not available, cannot embed")
        return None

    # Some ONNX exports accept 'text' directly
    if "text" in input_names:
        try:
            outputs = _model_session.run(None, {"text": np.array(texts)})
            embeddings = outputs[0]
            norms = np.linalg.norm(embeddings, axis=1, keepdims=True).clip(min=1e-9)
            return embeddings / norms
        except Exception as exc:
            logger.warning("embedding: simple text input failed: %s", exc)
            return None

    return None


# ---------------------------------------------------------------------------
# Mock embedder for testing
# ---------------------------------------------------------------------------

def mock_embed_texts(texts: list[str], dim: int = 384) -> "np.ndarray":
    """Deterministic bag-of-words embedding for tests.  No model needed.

    Each word gets a consistent pseudo-random direction vector (seeded by the
    word hash).  The text embedding is the sum of word vectors, L2-normalised.
    Similar texts with overlapping words will have positive cosine similarity.
    """
    import hashlib
    vectors = []
    for text in texts:
        acc = np.zeros(dim, dtype=np.float64)
        words = text.lower().split()
        if not words:
            words = ["__empty__"]
        for word in words:
            seed = int.from_bytes(hashlib.sha256(word.encode()).digest()[:4], "little")
            rng = np.random.RandomState(seed)
            acc += rng.randn(dim)
        norm = np.linalg.norm(acc)
        if norm > 0:
            acc /= norm
        vectors.append(acc.astype(np.float32))
    return np.array(vectors)


# ---------------------------------------------------------------------------
# Embed & store (for dream cycle)
# ---------------------------------------------------------------------------

def embed_and_store(config: Config, entry_ids: list[str] = None) -> int:
    """Embed knowledge entries and save vectors to disk.

    If entry_ids is None, embeds all entries in the index.
    If entry_ids is provided, only embeds those entries (appending to existing).
    Returns the number of entries embedded.

    Uses mock embedder when config.mock_mode is True or ONNX not available
    but embedding is enabled — this supports testing without ONNX.
    """
    from knowledge import load_index

    index = load_index(config)
    if not index:
        return 0

    # Filter to requested IDs if specified
    if entry_ids is not None:
        id_set = set(entry_ids)
        entries = [e for e in index if e["id"] in id_set]
    else:
        entries = list(index)

    if not entries:
        return 0

    # Build texts to embed
    texts = []
    ids = []
    for entry in entries:
        text = " ".join(entry.get("tags", []))
        text += " " + entry.get("content_preview", "")
        texts.append(text)
        ids.append(entry["id"])

    # Get embeddings
    if config.mock_mode:
        vectors = mock_embed_texts(texts)
    else:
        vectors = embed_texts(texts)
        if vectors is None:
            logger.info("embedding: model not available, skipping embed_and_store")
            return 0

    # Merge with existing vectors if we're appending specific entries
    if entry_ids is not None:
        existing_vectors, existing_ids = _load_vectors(config)
        if existing_vectors is not None:
            # Remove any existing vectors for these IDs (re-embed)
            keep_mask = [eid not in id_set for eid in existing_ids]
            if any(keep_mask):
                kept_vecs = existing_vectors[keep_mask]
                kept_ids = [eid for eid, keep in zip(existing_ids, keep_mask) if keep]
                vectors = np.vstack([kept_vecs, vectors])
                ids = kept_ids + ids
            # else: all existing were being re-embedded, just use new

    _save_vectors(config, vectors, ids)
    logger.info("embedding: stored %d vectors", len(ids))
    return len(entries)


# ---------------------------------------------------------------------------
# Semantic search
# ---------------------------------------------------------------------------

def semantic_search(
    config: Config,
    query_text: str,
    top_k: int = 5,
) -> list[dict]:
    """Search the knowledge store by semantic similarity.

    Returns list of dicts with 'id', 'score', and fields from the index,
    sorted by descending cosine similarity.
    """
    if not query_text.strip():
        return []

    # Embed the query
    if config.mock_mode:
        query_vec = mock_embed_texts([query_text])[0]
    else:
        vecs = embed_texts([query_text])
        if vecs is None:
            return []
        query_vec = vecs[0]

    # Load stored vectors
    stored_vectors, stored_ids = _load_vectors(config)
    if stored_vectors is None or len(stored_ids) == 0:
        return []

    # Cosine similarity (vectors are already L2-normalised)
    scores = stored_vectors @ query_vec

    # Top-k
    if len(scores) <= top_k:
        top_indices = np.argsort(-scores)
    else:
        # Partial sort for efficiency
        top_indices = np.argpartition(-scores, top_k)[:top_k]
        top_indices = top_indices[np.argsort(-scores[top_indices])]

    # Build results from index
    from knowledge import load_index
    index = load_index(config)
    index_map = {item["id"]: item for item in index}

    results = []
    for idx in top_indices:
        score = float(scores[idx])
        if score <= 0:
            continue
        entry_id = stored_ids[idx]
        item = index_map.get(entry_id)
        if item:
            results.append({
                "id": item["id"],
                "category": item.get("category", "facts"),
                "tags": item.get("tags", []),
                "confidence": item.get("confidence", "tentative"),
                "content_preview": item.get("content_preview", ""),
                "score": score,
            })

    return results
