"""Tests for the long-term knowledge store (knowledge.py)."""

import json
import os
from pathlib import Path

import pytest

from config import Config
from knowledge import (
    CATEGORIES,
    store_entry,
    read_entry,
    delete_entry,
    count_entries,
    load_index,
    rebuild_index,
    search_bm25,
    search_tags,
    format_recalled,
    _parse_frontmatter,
    _invalidate_bm25_cache,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_config(workspace: Path) -> Config:
    """Create a Config pointing at the given workspace."""
    cfg = Config()
    cfg.workspace_dir = str(workspace)
    cfg.knowledge_enabled = True
    return cfg


def _reset_caches():
    """Reset module-level caches between tests."""
    import knowledge
    knowledge._index_cache = None
    knowledge._index_mtime = 0.0
    _invalidate_bm25_cache()


# ---------------------------------------------------------------------------
# Frontmatter parsing
# ---------------------------------------------------------------------------

class TestFrontmatter:
    def test_valid(self):
        text = "---\nid: foo\ntags: [a, b]\n---\nBody text here."
        meta, body = _parse_frontmatter(text)
        assert meta["id"] == "foo"
        assert meta["tags"] == ["a", "b"]
        assert body == "Body text here."

    def test_no_frontmatter(self):
        text = "Just plain content."
        meta, body = _parse_frontmatter(text)
        assert meta == {}
        assert body == text

    def test_corrupt_yaml(self):
        text = "---\n: [invalid yaml\n---\nBody"
        meta, body = _parse_frontmatter(text)
        assert meta == {}
        assert body == text


# ---------------------------------------------------------------------------
# CRUD with fixture data (pre-populated knowledge store)
# ---------------------------------------------------------------------------

class TestCRUDWithFixtures:
    def setup_method(self):
        _reset_caches()

    def test_load_fixture_index(self, knowledge_fixture):
        cfg = _make_config(knowledge_fixture)
        index = load_index(cfg)
        assert len(index) == 13
        ids = {e["id"] for e in index}
        assert "pip_bookworm_flag" in ids
        assert "network_diagnostics" in ids
        assert "dht22_crc_errors" in ids

    def test_read_entry(self, knowledge_fixture):
        cfg = _make_config(knowledge_fixture)
        entry = read_entry(cfg, "pip_bookworm_flag")
        assert entry is not None
        assert "break-system-packages" in entry["body"]
        assert entry["meta"]["category"] == "facts"

    def test_read_entry_missing(self, knowledge_fixture):
        cfg = _make_config(knowledge_fixture)
        assert read_entry(cfg, "nonexistent_id") is None

    def test_count_entries(self, knowledge_fixture):
        cfg = _make_config(knowledge_fixture)
        assert count_entries(cfg) == 13

    def test_delete_entry(self, knowledge_fixture):
        cfg = _make_config(knowledge_fixture)
        assert count_entries(cfg) == 13
        _reset_caches()
        deleted = delete_entry(cfg, "tmp_full_npm")
        assert deleted is True
        _reset_caches()
        assert count_entries(cfg) == 12
        # File should be gone
        assert not (knowledge_fixture / "knowledge" / "errors" / "tmp_full_npm.md").exists()

    def test_delete_nonexistent(self, knowledge_fixture):
        cfg = _make_config(knowledge_fixture)
        assert delete_entry(cfg, "does_not_exist") is False


# ---------------------------------------------------------------------------
# CRUD with fresh store (empty)
# ---------------------------------------------------------------------------

class TestCRUDFresh:
    def setup_method(self):
        _reset_caches()

    def test_store_and_read(self, tmp_path):
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        cfg = _make_config(workspace)

        entry_id = store_entry(
            cfg,
            content="Python 3.11 is available on Bookworm via apt.",
            tags=["python", "apt", "bookworm"],
            category="facts",
            confidence="verified",
            source_goal="Test goal",
            source_tick=1,
        )

        assert entry_id  # non-empty
        _reset_caches()

        # Index updated
        index = load_index(cfg)
        assert len(index) == 1
        assert index[0]["id"] == entry_id
        assert index[0]["tags"] == ["python", "apt", "bookworm"]

        # File exists
        path = cfg.knowledge_dir / "facts" / f"{entry_id}.md"
        assert path.exists()
        text = path.read_text()
        assert "Python 3.11" in text

        # Read back
        entry = read_entry(cfg, entry_id)
        assert entry is not None
        assert "Python 3.11" in entry["body"]

    def test_store_invalid_category_defaults_to_facts(self, tmp_path):
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        cfg = _make_config(workspace)

        entry_id = store_entry(cfg, "test content", ["tag1"], category="bogus")
        _reset_caches()
        index = load_index(cfg)
        assert index[0]["category"] == "facts"

    def test_store_multiple(self, tmp_path):
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        cfg = _make_config(workspace)

        ids = []
        for i in range(5):
            distinct = [
                "The octoprint server answers on port 5000",
                "Boss prefers terse spoken replies in the morning",
                "The living-room lamp is a Tuya smart plug",
                "Gemma decodes at roughly fifty tokens per second",
                "The garage camera streams RTSP on channel one",
            ]
            eid = store_entry(cfg, distinct[i], [f"tag{i}"], category="facts")
            ids.append(eid)
            _reset_caches()

        assert count_entries(cfg) == 5
        assert len(set(ids)) == 5  # all unique IDs

    def test_empty_store_count(self, tmp_path):
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        cfg = _make_config(workspace)
        assert count_entries(cfg) == 0

    def test_empty_store_search(self, tmp_path):
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        cfg = _make_config(workspace)
        results = search_bm25(cfg, "anything")
        assert results == []


# ---------------------------------------------------------------------------
# Index rebuild
# ---------------------------------------------------------------------------

class TestIndexRebuild:
    def setup_method(self):
        _reset_caches()

    def test_rebuild_from_files(self, knowledge_fixture):
        cfg = _make_config(knowledge_fixture)
        # Delete the index
        cfg.knowledge_index_path.unlink()
        _reset_caches()
        assert count_entries(cfg) == 0

        # Rebuild
        n = rebuild_index(cfg)
        assert n == 13
        _reset_caches()
        assert count_entries(cfg) == 13

    def test_rebuild_empty_store(self, tmp_path):
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        cfg = _make_config(workspace)
        n = rebuild_index(cfg)
        assert n == 0


# ---------------------------------------------------------------------------
# BM25 search
# ---------------------------------------------------------------------------

class TestBM25Search:
    def setup_method(self):
        _reset_caches()

    def test_search_pip(self, knowledge_fixture):
        cfg = _make_config(knowledge_fixture)
        results = search_bm25(cfg, "pip install error", top_k=5)
        assert len(results) > 0
        # pip-related entries should rank high
        top_ids = [r["id"] for r in results[:3]]
        assert any("pip" in rid for rid in top_ids)

    def test_search_dht22_sensor(self, knowledge_fixture):
        cfg = _make_config(knowledge_fixture)
        results = search_bm25(cfg, "DHT22 temperature sensor reading", top_k=5)
        assert len(results) > 0
        top_ids = [r["id"] for r in results[:3]]
        assert any("dht22" in rid for rid in top_ids)

    def test_search_network(self, knowledge_fixture):
        cfg = _make_config(knowledge_fixture)
        results = search_bm25(cfg, "network connectivity troubleshooting", top_k=5)
        assert len(results) > 0
        top_ids = [r["id"] for r in results[:3]]
        assert any("network" in rid for rid in top_ids)

    def test_search_no_results_for_garbage(self, knowledge_fixture):
        cfg = _make_config(knowledge_fixture)
        results = search_bm25(cfg, "zzzzxxxxxxxxxqqqq", top_k=5)
        assert results == []

    def test_search_respects_top_k(self, knowledge_fixture):
        cfg = _make_config(knowledge_fixture)
        results = search_bm25(cfg, "python pip install packages", top_k=2)
        assert len(results) <= 2

    def test_search_returns_scores(self, knowledge_fixture):
        cfg = _make_config(knowledge_fixture)
        results = search_bm25(cfg, "pip install error bookworm", top_k=5)
        assert all("score" in r for r in results)
        # Scores should be descending
        scores = [r["score"] for r in results]
        assert scores == sorted(scores, reverse=True)

    def test_search_disabled(self, knowledge_fixture):
        cfg = _make_config(knowledge_fixture)
        cfg.knowledge_enabled = False
        results = search_bm25(cfg, "pip install")
        assert results == []

    def test_search_empty_query(self, knowledge_fixture):
        cfg = _make_config(knowledge_fixture)
        results = search_bm25(cfg, "")
        assert results == []


# ---------------------------------------------------------------------------
# Tag search
# ---------------------------------------------------------------------------

class TestTagSearch:
    def setup_method(self):
        _reset_caches()

    def test_search_by_tag(self, knowledge_fixture):
        cfg = _make_config(knowledge_fixture)
        results = search_tags(cfg, ["pip"])
        assert len(results) > 0
        for r in results:
            assert "pip" in r["tags"]

    def test_search_multiple_tags(self, knowledge_fixture):
        cfg = _make_config(knowledge_fixture)
        results = search_tags(cfg, ["gpio", "sensor"])
        assert len(results) > 0

    def test_search_no_matching_tags(self, knowledge_fixture):
        cfg = _make_config(knowledge_fixture)
        results = search_tags(cfg, ["zzzznonexistent"])
        assert results == []

    def test_search_disabled(self, knowledge_fixture):
        cfg = _make_config(knowledge_fixture)
        cfg.knowledge_enabled = False
        results = search_tags(cfg, ["pip"])
        assert results == []


# ---------------------------------------------------------------------------
# Format recalled knowledge for context injection
# ---------------------------------------------------------------------------

class TestFormatRecalled:
    def test_format_entries(self, knowledge_fixture):
        cfg = _make_config(knowledge_fixture)
        _reset_caches()
        results = search_bm25(cfg, "pip install", top_k=3)
        text = format_recalled(results, max_chars=1200)
        assert len(text) > 0
        assert len(text) <= 1200
        assert "[FACT]" in text or "[ERROR]" in text

    def test_format_empty(self):
        assert format_recalled([]) == ""

    def test_format_respects_budget(self, knowledge_fixture):
        cfg = _make_config(knowledge_fixture)
        _reset_caches()
        results = search_bm25(cfg, "raspberry pi python pip", top_k=10)
        text = format_recalled(results, max_chars=200)
        assert len(text) <= 200

    def test_format_shows_confidence(self, knowledge_fixture):
        cfg = _make_config(knowledge_fixture)
        _reset_caches()
        # Search for something with verified entries
        results = search_bm25(cfg, "pip install bookworm", top_k=5)
        text = format_recalled(results, max_chars=2000)
        assert "[verified]" in text
