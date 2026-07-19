"""Strategy memory (ReasoningBank, SOTA #3): a closed quest/objective is distilled into a
`strategy`-kind guardrail engram and surfaced through the existing recall cascade.

Covers the distiller in isolation (gating, LLM path, malformed/raise → template fallback, bounds)
AND the store integration (the new kind validates, commits through the single Consolidator, and is
recalled by the live MemoryManager cascade). Offline: mock_mode embedder, no LLM/GPU/tick loop —
the distiller is handed a fake `llm` callable or None.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import strategy
from config import Config
from engram import Engram, KINDS, Consolidator, LongTermStore
from memory_manager import MemoryManager


def _cfg(tmp_path, *, mock: bool = True) -> Config:
    cfg = Config()
    cfg.workspace_dir = str(tmp_path / "workspace")
    cfg.mock_mode = mock
    (tmp_path / "workspace").mkdir(parents=True, exist_ok=True)
    return cfg


def _closure(**kw) -> dict:
    base = {"title": "scan the lan for hosts", "why": "map the network",
            "outcome": "done", "reason": "", "success": True,
            "situation": "obj7|scan the lan", "trajectory": ""}
    base.update(kw)
    return base


# --- flag + schema ---------------------------------------------------------------------------------

class TestFlagAndKind:
    def test_flag_defaults_off(self):
        assert Config().pillars_strategy_memory_enabled is False

    def test_flag_enabled_on_this_repo_config(self):
        # config.toml ships it live; a fresh checkout stays dark via the dataclass default above.
        import config
        assert config.load_config("config.toml").pillars_strategy_memory_enabled is True

    def test_strategy_is_a_valid_engram_kind(self):
        assert "strategy" in KINDS
        Engram(kind="strategy", body="When X: do Y", provenance="experienced").validate()


# --- the distiller ---------------------------------------------------------------------------------

class TestShouldDistill:
    def test_skips_empty_title(self):
        assert strategy.should_distill(_closure(title="")) is False

    def test_skips_merged_close(self):
        assert strategy.should_distill(_closure(outcome="merged")) is False

    def test_keeps_a_real_close(self):
        assert strategy.should_distill(_closure(outcome="released", success=False)) is True

    def test_loss_encodes_stronger_than_win(self):
        assert strategy.strength_for(_closure(success=False)) > strategy.strength_for(_closure(success=True))


class TestTemplateFallback:
    def test_template_used_when_no_llm(self):
        body = strategy.distill_strategy(_closure(success=True, reason="a ping sweep"), llm=None)
        assert body.startswith("When ")
        assert "reuse" in body.lower()

    def test_release_template_is_a_dead_end_guardrail(self):
        body = strategy.distill_strategy(
            _closure(success=False, outcome="released", reason="6 tries, no progress"), llm=None)
        assert "dead end" in body.lower()

    def test_body_is_bounded(self):
        long = "x" * 500
        body = strategy.distill_strategy(_closure(title=long, reason=long, success=False, outcome="abandoned"),
                                         llm=None)
        assert len(body) <= strategy.STRATEGY_BODY_MAX

    def test_trivial_close_returns_none(self):
        assert strategy.distill_strategy(_closure(outcome="merged"), llm=None) is None


class TestLLMPath:
    def test_valid_llm_line_is_used(self):
        def fake_llm(messages, grammar=None):
            assert grammar and "TRIGGER" in grammar          # grammar-constrained
            return "TRIGGER: scanning a quiet lan || PRINCIPLE: a ping sweep before a port scan"
        body = strategy.distill_strategy(_closure(), llm=fake_llm)
        assert body == "When scanning a quiet lan: a ping sweep before a port scan"

    def test_malformed_llm_output_falls_back_to_template(self):
        def fake_llm(messages, grammar=None):
            return "sorry I could not comply"
        body = strategy.distill_strategy(_closure(success=True, reason="worked"), llm=fake_llm)
        assert body.startswith("When ")                      # template shape, not a crash
        assert "scan the lan" in body                        # template used the title

    def test_raising_llm_falls_back_to_template(self):
        def fake_llm(messages, grammar=None):
            raise RuntimeError("model down")
        body = strategy.distill_strategy(_closure(success=False, outcome="abandoned", reason="blocked"),
                                         llm=fake_llm)
        assert body.startswith("When ")                      # fail-open, never raises

    def test_parse_strategy_valid_and_malformed(self):
        good, dropped = strategy.parse_strategy("TRIGGER: a || PRINCIPLE: b")
        assert good == {"trigger": "a", "principle": "b"} and dropped == []
        bad, dropped = strategy.parse_strategy("no delimiter here")
        assert bad is None and dropped


# --- store integration: commit + recall ------------------------------------------------------------

class TestStoreIntegration:
    def test_strategy_engram_commits_and_recalls(self, tmp_path):
        cfg = _cfg(tmp_path)
        mgr = MemoryManager(cfg)
        body = strategy.distill_strategy(_closure(success=True, reason="a ping sweep found hosts"), llm=None)
        mgr.encode("strategy", body, tick=5, strength=strategy.STRATEGY_STRENGTH_WIN,
                   stats={"situation": "obj7|scan the lan"})
        hits = mgr.recall("scan the lan for hosts", situation="obj7|scan the lan", explore_ratio=0.0)
        assert any(h.kind == "strategy" for h in hits), "guardrail should surface via the recall cascade"

    def test_repeated_guardrail_merges_not_duplicates(self, tmp_path):
        cfg = _cfg(tmp_path)
        con = Consolidator(cfg)
        eg = Engram(kind="strategy", body="When scanning the lan: a ping sweep beats a full port scan",
                    provenance="experienced", strength=0.6)
        con.commit(eg)
        con.commit(Engram(kind="strategy", body="When scanning the lan: a ping sweep beats a full port scan",
                          provenance="experienced", strength=0.6))
        strat = [e for e in LongTermStore(cfg).load() if e.kind == "strategy"]
        assert len(strat) == 1, "a repeated guardrail should merge, not duplicate"


if __name__ == "__main__":
    import pytest
    pytest.main([__file__, "-v"])
