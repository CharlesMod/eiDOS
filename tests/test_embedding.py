"""Tests for embedding module (Phase 5).

Uses mock_mode=True for deterministic testing without ONNX runtime.
Live tests with real model are marked @pytest.mark.slow and require
the model to be downloaded via setup_embedding.py first.
"""

import json
import os
import sys
import tempfile
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

import unittest
from unittest.mock import patch
import numpy as np

from config import Config
from embedding import (
    mock_embed_texts,
    embed_and_store,
    semantic_search,
    _load_vectors,
    _save_vectors,
    model_available,
)


class TestMockEmbedder(unittest.TestCase):
    """Tests for the deterministic hash-based mock embedder."""

    def test_returns_correct_shape(self):
        texts = ["hello world", "testing embeddings"]
        vecs = mock_embed_texts(texts)
        self.assertEqual(vecs.shape, (2, 384))

    def test_deterministic(self):
        texts = ["same text each time"]
        v1 = mock_embed_texts(texts)
        v2 = mock_embed_texts(texts)
        np.testing.assert_array_equal(v1, v2)

    def test_different_texts_different_vectors(self):
        v1 = mock_embed_texts(["hello"])
        v2 = mock_embed_texts(["goodbye"])
        # Should not be equal
        self.assertFalse(np.allclose(v1, v2))

    def test_unit_normalized(self):
        vecs = mock_embed_texts(["test normalization", "another text", "third one"])
        norms = np.linalg.norm(vecs, axis=1)
        np.testing.assert_allclose(norms, 1.0, atol=1e-6)

    def test_single_text(self):
        vecs = mock_embed_texts(["just one"])
        self.assertEqual(vecs.shape, (1, 384))

    def test_custom_dimension(self):
        vecs = mock_embed_texts(["test"], dim=128)
        self.assertEqual(vecs.shape, (1, 128))

    def test_similar_texts_have_some_similarity(self):
        """Texts with overlapping hash bytes won't necessarily be similar,
        but the mock should at least produce valid vectors."""
        v1 = mock_embed_texts(["the cat sat on the mat"])
        v2 = mock_embed_texts(["the dog sat on the mat"])
        # Both should be valid unit vectors
        self.assertAlmostEqual(float(np.linalg.norm(v1)), 1.0, places=5)
        self.assertAlmostEqual(float(np.linalg.norm(v2)), 1.0, places=5)


class TestVectorStore(unittest.TestCase):
    """Tests for disk-based vector storage."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.config = Config()
        self.config.workspace_dir = os.path.join(self.tmp, "workspace")
        os.makedirs(self.config.workspace_dir)

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_save_and_load(self):
        vectors = np.random.randn(5, 384).astype(np.float32)
        ids = ["id_1", "id_2", "id_3", "id_4", "id_5"]
        _save_vectors(self.config, vectors, ids)

        loaded_vecs, loaded_ids = _load_vectors(self.config)
        np.testing.assert_array_almost_equal(loaded_vecs, vectors)
        self.assertEqual(loaded_ids, ids)

    def test_load_missing_returns_none(self):
        vecs, ids = _load_vectors(self.config)
        self.assertIsNone(vecs)
        self.assertEqual(ids, [])

    def test_load_mismatched_lengths_returns_none(self):
        """If vectors.npy and vector_ids.json are out of sync, return None."""
        self.config.knowledge_dir.mkdir(parents=True, exist_ok=True)
        np.save(str(self.config.knowledge_dir / "vectors.npy"),
                np.zeros((5, 384)))
        (self.config.knowledge_dir / "vector_ids.json").write_text(
            json.dumps(["id_1", "id_2"]))  # only 2 IDs for 5 vectors

        vecs, ids = _load_vectors(self.config)
        self.assertIsNone(vecs)

    def test_overwrite(self):
        vectors1 = np.random.randn(3, 384).astype(np.float32)
        _save_vectors(self.config, vectors1, ["a", "b", "c"])

        vectors2 = np.random.randn(5, 384).astype(np.float32)
        _save_vectors(self.config, vectors2, ["x", "y", "z", "w", "v"])

        loaded_vecs, loaded_ids = _load_vectors(self.config)
        self.assertEqual(len(loaded_ids), 5)
        self.assertEqual(loaded_vecs.shape[0], 5)


class TestEmbedAndStore(unittest.TestCase):
    """Tests for embed_and_store using mock mode."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.config = Config()
        self.config.workspace_dir = os.path.join(self.tmp, "workspace")
        os.makedirs(self.config.workspace_dir)
        self.config.mock_mode = True

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _populate_knowledge(self):
        """Store some knowledge entries for embedding tests."""
        from knowledge import store_entry, rebuild_index, _invalidate_bm25_cache
        store_entry(self.config, "pip needs --break-system-packages on Bookworm",
                    tags=["pip", "bookworm"], category="facts")
        store_entry(self.config, "DHT22 CRC errors when wire exceeds 3 meters",
                    tags=["dht22", "gpio"], category="errors")
        store_entry(self.config, "Use systemctl --user for non-root services",
                    tags=["systemd", "services"], category="procedures")
        rebuild_index(self.config)
        _invalidate_bm25_cache()

    def test_embed_all_entries(self):
        self._populate_knowledge()
        count = embed_and_store(self.config)
        self.assertEqual(count, 3)

        # Verify vectors on disk
        vecs, ids = _load_vectors(self.config)
        self.assertEqual(vecs.shape[0], 3)
        self.assertEqual(len(ids), 3)

    def test_embed_empty_store(self):
        count = embed_and_store(self.config)
        self.assertEqual(count, 0)

    def test_embed_specific_entries(self):
        self._populate_knowledge()
        # First embed all
        embed_and_store(self.config)
        vecs1, ids1 = _load_vectors(self.config)
        self.assertEqual(len(ids1), 3)

        # Add a new entry
        from knowledge import store_entry, rebuild_index, _invalidate_bm25_cache
        store_entry(self.config, "Solar panel produces 6W on clear days",
                    tags=["solar", "power"], category="facts")
        rebuild_index(self.config)
        _invalidate_bm25_cache()

        # Embed only the new entry
        from knowledge import load_index
        index = load_index(self.config)
        new_ids = [e["id"] for e in index if "solar" in e.get("content_preview", "").lower()]
        count = embed_and_store(self.config, entry_ids=new_ids)
        self.assertEqual(count, 1)

        # Should now have 4 vectors total
        vecs2, ids2 = _load_vectors(self.config)
        self.assertEqual(len(ids2), 4)

    def test_embed_when_not_mock_and_no_model(self):
        """When mock_mode is False and no ONNX model, should return 0."""
        self._populate_knowledge()
        self.config.mock_mode = False
        count = embed_and_store(self.config)
        self.assertEqual(count, 0)


class TestSemanticSearch(unittest.TestCase):
    """Tests for semantic search using mock embeddings."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.config = Config()
        self.config.workspace_dir = os.path.join(self.tmp, "workspace")
        os.makedirs(self.config.workspace_dir)
        self.config.mock_mode = True

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _populate_and_embed(self):
        from knowledge import store_entry, rebuild_index, _invalidate_bm25_cache
        store_entry(self.config, "pip needs --break-system-packages on Bookworm",
                    tags=["pip", "bookworm"], category="facts")
        store_entry(self.config, "DHT22 CRC errors when wire exceeds 3 meters",
                    tags=["dht22", "gpio"], category="errors")
        store_entry(self.config, "Use systemctl --user for non-root services",
                    tags=["systemd", "services"], category="procedures")
        rebuild_index(self.config)
        _invalidate_bm25_cache()
        embed_and_store(self.config)

    def test_search_returns_results(self):
        self._populate_and_embed()
        results = semantic_search(self.config, "pip install packages", top_k=3)
        self.assertGreater(len(results), 0)
        # Each result should have expected fields
        for r in results:
            self.assertIn("id", r)
            self.assertIn("score", r)
            self.assertIn("category", r)
            self.assertIn("content_preview", r)

    def test_search_empty_query(self):
        self._populate_and_embed()
        results = semantic_search(self.config, "", top_k=3)
        self.assertEqual(results, [])

    def test_search_no_vectors(self):
        results = semantic_search(self.config, "anything", top_k=3)
        self.assertEqual(results, [])

    def test_search_respects_top_k(self):
        self._populate_and_embed()
        results = semantic_search(self.config, "pip install", top_k=1)
        self.assertLessEqual(len(results), 1)

    def test_search_scores_are_float(self):
        self._populate_and_embed()
        results = semantic_search(self.config, "DHT22 sensor GPIO", top_k=3)
        for r in results:
            self.assertIsInstance(r["score"], float)

    def test_search_not_mock_no_model_returns_empty(self):
        """When mock_mode is False and no ONNX model, search returns []."""
        self._populate_and_embed()
        self.config.mock_mode = False
        results = semantic_search(self.config, "test query", top_k=3)
        self.assertEqual(results, [])


class TestDreamPrefetch(unittest.TestCase):
    """Tests for the dream_prefetch integration in compaction.py."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.config = Config()
        self.config.workspace_dir = os.path.join(self.tmp, "workspace")
        os.makedirs(self.config.workspace_dir)
        self.config.mock_mode = True
        self.config.knowledge_embedding_enabled = True

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _populate_and_embed(self):
        from knowledge import store_entry, rebuild_index, _invalidate_bm25_cache
        store_entry(self.config, "pip needs --break-system-packages on Bookworm",
                    tags=["pip", "bookworm"], category="facts")
        store_entry(self.config, "DHT22 CRC errors when wire exceeds 3 meters",
                    tags=["dht22", "gpio"], category="errors")
        rebuild_index(self.config)
        _invalidate_bm25_cache()
        embed_and_store(self.config)

    def test_prefetch_writes_cache(self):
        from compaction import dream_prefetch
        self._populate_and_embed()

        count = dream_prefetch(self.config, "Set up pip on Bookworm", "Step 1: install pip")
        self.assertGreater(count, 0)

        cache_path = self.config.workspace / "recall_cache.md"
        self.assertTrue(cache_path.exists())
        self.assertGreater(len(cache_path.read_text()), 0)

    def test_prefetch_empty_goal(self):
        from compaction import dream_prefetch
        self._populate_and_embed()

        count = dream_prefetch(self.config, "", "")
        self.assertEqual(count, 0)

    def test_prefetch_no_vectors(self):
        from compaction import dream_prefetch
        count = dream_prefetch(self.config, "test goal", "test plan")
        # Should embed entries (there are none), then search returns empty
        self.assertEqual(count, 0)

    def test_prefetch_clears_stale_cache(self):
        from compaction import dream_prefetch
        cache_path = self.config.workspace / "recall_cache.md"
        cache_path.write_text("stale cached data")

        # No knowledge entries → no results → cache should be cleared
        count = dream_prefetch(self.config, "test goal", "test plan")
        self.assertEqual(count, 0)
        self.assertFalse(cache_path.exists())


class TestRecallCacheInContext(unittest.TestCase):
    """Tests for recall_cache.md integration in context.py intelligence section."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.config = Config()
        self.config.workspace_dir = os.path.join(self.tmp, "workspace")
        os.makedirs(self.config.workspace_dir)
        self.config.mock_mode = True
        self.config.briefing_model = True

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_cache_included_in_intelligence(self):
        from context import _build_intelligence_section
        # Write a recall cache file
        cache_path = self.config.workspace / "recall_cache.md"
        cache_path.write_text("[FACT] (pip, bookworm) pip needs --break-system-packages")

        result = _build_intelligence_section(self.config, "Set up pip", "Step 1")
        self.assertIn("pip needs --break-system-packages", result)

    def test_no_cache_still_works(self):
        from context import _build_intelligence_section
        # No cache file — should still work (BM25 only)
        result = _build_intelligence_section(self.config, "test", "plan")
        # May be empty if no knowledge entries, but shouldn't crash
        self.assertIsInstance(result, str)

    def test_cache_respects_budget(self):
        from context import _build_intelligence_section
        cache_path = self.config.workspace / "recall_cache.md"
        # Write oversized cache
        cache_path.write_text("x" * 5000)
        self.config.context_intelligence_max_chars = 200

        result = _build_intelligence_section(self.config, "goal", "plan")
        self.assertLessEqual(len(result), 200)


class TestModelAvailable(unittest.TestCase):

    def test_not_available_by_default(self):
        config = Config()
        config.embedding_model_dir = "/nonexistent/path"
        self.assertFalse(model_available(config))


if __name__ == "__main__":
    unittest.main()
