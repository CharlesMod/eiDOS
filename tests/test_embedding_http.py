"""The HTTP embedding backend (semantic recall via a resident llama.cpp --embedding server).

Semantic recall was OFF because the only backend was ONNX, and onnxruntime's CUDA EP is a gamble on
the Blackwell GPU. The Sprinter fix serves a small GGUF embedding model (nomic, 768-dim) through the
CUDA-built llama.cpp already running the mind, and embed_texts POSTs to it. This pins the client:
endpoint routing, query/document prefixing, L2 normalisation, graceful failure, and the dimension
guard — all WITHOUT a live server (urlopen is mocked).

No services / GPU — a stub HTTP layer only.
"""
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent.parent))

import numpy as np

import embedding
from config import Config


def _fake_urlopen(captured):
    """A urlopen stand-in that records the request body and returns unit vectors of the right dim."""
    def _open(req, timeout=None):
        body = json.loads(req.data.decode("utf-8"))
        captured.append(body)
        n = len(body["input"])
        vecs = [[float(i + 1)] + [0.0] * 767 for i in range(n)]   # 768-dim, distinct, un-normalised
        payload = {"data": [{"embedding": v} for v in vecs]}

        class _Resp:
            def __enter__(self_): return self_
            def __exit__(self_, *a): return False
            def read(self_): return json.dumps(payload).encode("utf-8")
        return _Resp()
    return _open


class _Base(unittest.TestCase):
    def setUp(self):
        self.cfg = Config()
        self.cfg.workspace_dir = tempfile.mkdtemp()
        self.cfg.mock_mode = False
        self.cfg.embedding_endpoint = "http://127.0.0.1:8082"
        self.cfg.embedding_model = "nomic-embed-text-v1.5"
        self.cfg.embedding_query_prefix = "search_query: "
        self.cfg.embedding_doc_prefix = "search_document: "


class TestHttpBackend(_Base):
    def test_routes_to_endpoint_and_normalises(self):
        cap = []
        with mock.patch("urllib.request.urlopen", _fake_urlopen(cap)):
            vecs = embedding.embed_texts(["hello", "world"], config=self.cfg, is_query=False)
        self.assertEqual(vecs.shape, (2, 768))
        # L2-normalised: each row has unit norm
        np.testing.assert_allclose(np.linalg.norm(vecs, axis=1), [1.0, 1.0], rtol=1e-5)

    def test_query_vs_document_prefix(self):
        cap = []
        with mock.patch("urllib.request.urlopen", _fake_urlopen(cap)):
            embedding.embed_texts(["find this"], config=self.cfg, is_query=True)
            embedding.embed_texts(["store this"], config=self.cfg, is_query=False)
        self.assertEqual(cap[0]["input"], ["search_query: find this"])
        self.assertEqual(cap[1]["input"], ["search_document: store this"])
        self.assertEqual(cap[0]["model"], "nomic-embed-text-v1.5")

    def test_failure_degrades_to_none(self):
        def _boom(req, timeout=None):
            raise OSError("connection refused")
        with mock.patch("urllib.request.urlopen", _boom):
            self.assertIsNone(embedding.embed_texts(["x"], config=self.cfg, is_query=True))

    def test_count_mismatch_returns_none(self):
        def _short(req, timeout=None):
            class _R:
                def __enter__(self_): return self_
                def __exit__(self_, *a): return False
                def read(self_): return json.dumps({"data": [{"embedding": [1.0] * 768}]}).encode()
            return _R()
        with mock.patch("urllib.request.urlopen", _short):
            self.assertIsNone(embedding.embed_texts(["a", "b"], config=self.cfg))

    def test_no_endpoint_falls_through_to_onnx_path(self):
        # No endpoint + no ONNX model loaded → None (the pre-existing behaviour, untouched).
        self.cfg.embedding_endpoint = ""
        self.assertIsNone(embedding.embed_texts(["x"], config=self.cfg))


class TestDimensionGuard(_Base):
    def test_semantic_search_skips_on_dim_mismatch(self):
        # Store 384-dim vectors (old model), then query with the 768-dim backend → no crash, [].
        import knowledge
        knowledge.store_entry(self.cfg, "the broker is at 10.0.0.5", tags=["broker"])
        embedding._save_vectors(self.cfg, np.random.rand(1, 384).astype(np.float32),
                                [knowledge.load_index(self.cfg)[0]["id"]])
        cap = []
        with mock.patch("urllib.request.urlopen", _fake_urlopen(cap)):
            hits = embedding.semantic_search(self.cfg, "where is the broker", top_k=3)
        self.assertEqual(hits, [])       # degraded to no-semantic (BM25 carries), never raised


if __name__ == "__main__":
    unittest.main()
