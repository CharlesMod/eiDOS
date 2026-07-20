"""WISDOM_PLAN §6 — lineage: the species gets smarter when the individual resets.

Pins (the cross-stream contract; §W invariants binding):
  - SELECTION RULE: strategy/procedure/fact engrams enter the heirloom ONLY when replay-validated
    (stats["replay_learned"] > 0) OR positive bet utility (stats["credit_sum"] > 0); unvalidated
    ones stay out (WIS1 — earned experience only, keys read defensively). Scars = error engrams
    above the scar floor enter; weak errors do not. Bounded at the cap, best-utility-first.
  - HEIRLOOM SHAPE: each record {kind, body, provenance_chain, stats_summary, exported_ts}; the
    header carries the generational metrics (name, birth/retire ts, level, goals_completed,
    quests_passed).
  - BEST-EFFORT: a corrupt long-term store never crashes the export (it degrades to what it can
    read); fresh_slate is never blocked.
  - IMPORT (seed_knowledge): heirloom items stamped provenance='inherited' at the told/inherited
    strength FLOOR (the exact discount the nugget importer uses); inherited reflexes land DISARMED
    as `proposed`, never armed (WIS1 across generations). Idempotent re-seed (no duplicates).
    --no-heirloom skips inheritance.
  - fresh_slate.sh passes `bash -n`; heirlooms/README.md is written.

No services / tick loop / GPU — temp workspaces only. Embeddings mock-aware (config.mock_mode).
"""

import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest

from config import Config
import engram
from engram import Engram, Consolidator, LongTermStore, INHERITED_STRENGTH_FLOOR
import legacy
import seed_knowledge

_REPO = Path(__file__).parent.parent


# --- helpers -------------------------------------------------------------------------------------

def _cfg(tmp_path, *, sub: str = "workspace") -> Config:
    cfg = Config()
    cfg.workspace_dir = str(tmp_path / sub)
    cfg.mock_mode = True
    (tmp_path / sub).mkdir(parents=True, exist_ok=True)
    return cfg


def _commit(cfg, engrams):
    Consolidator(cfg).commit_many(engrams)


def _validated_strategy(body="never restart llama-swap during a sleep job"):
    return Engram(kind="strategy", body=body, stats={"credit_sum": 2.3, "recall_count": 4})


def _replay_procedure(body="to free VRAM stop dashboard then llama-swap service"):
    return Engram(kind="procedure", body=body, stats={"replay_learned": 1})


def _unvalidated_fact(body="the router lives behind the television set"):
    return Engram(kind="fact", body=body, stats={})   # no credit, no replay → not exported


def _scar(body="the space heater trips the fuse box breaker", strength=0.8):
    return Engram(kind="error", body=body, strength=strength)


def _weak_error(body="a one-off typo in a filename once", strength=0.2):
    return Engram(kind="error", body=body, strength=strength)


def _reflex(rid="rx1", status="armed", fired=9, failed=0):
    return {"id": rid, "trigger": {"situation_key": "k"}, "action": {"tool": "noop", "args": {}},
            "provenance": {"episode_ids": ["e1"], "successes": 5}, "status": status,
            "fired_count": fired, "failed_count": failed}


def _write_reflexes(cfg, reflexes):
    sd = cfg.state_dir
    sd.mkdir(parents=True, exist_ok=True)
    (sd / "reflexes.json").write_text(json.dumps({"reflexes": reflexes}), encoding="utf-8")


# =================================================================================================
class TestSelectionRule:
    def test_validated_in_unvalidated_out(self, tmp_path):
        cfg = _cfg(tmp_path)
        _commit(cfg, [_validated_strategy(), _replay_procedure(), _unvalidated_fact()])
        vol = legacy.export_heirloom(cfg, out_dir=str(tmp_path / "heirlooms"))
        assert vol is not None
        _hdr, recs = legacy.read_heirloom(vol)
        bodies = {r["body"] for r in recs}
        assert any("llama-swap" in b for b in bodies)          # strategy: positive credit_sum → in
        assert any("free VRAM" in b for b in bodies)           # procedure: replay_learned>0 → in
        assert not any("television" in b for b in bodies)      # fact: no utility → OUT (WIS1)

    def test_scar_in_weak_error_out(self, tmp_path):
        cfg = _cfg(tmp_path)
        _commit(cfg, [_scar(), _weak_error()])
        vol = legacy.export_heirloom(cfg, out_dir=str(tmp_path / "heirlooms"))
        assert vol is not None
        _hdr, recs = legacy.read_heirloom(vol)
        bodies = {r["body"] for r in recs}
        assert any("fuse box" in b for b in bodies)            # scar above floor → in
        assert not any("one-off typo" in b for b in bodies)    # below scar floor → OUT

    def test_replay_learned_alone_qualifies(self, tmp_path):
        # replay_learned>0 with zero credit_sum still exports (the OR half of the rule).
        cfg = _cfg(tmp_path)
        _commit(cfg, [Engram(kind="fact", body="a hard-won fact", stats={"replay_learned": 2})])
        _hdr, recs = legacy.read_heirloom(legacy.export_heirloom(cfg, out_dir=str(tmp_path / "h")))
        assert any("hard-won" in r["body"] for r in recs)

    def test_absent_keys_are_unvalidated(self, tmp_path):
        # Defensive read: an engram with a garbage/None stat value is treated as unvalidated, not a crash.
        cfg = _cfg(tmp_path)
        _commit(cfg, [Engram(kind="strategy", body="garbage stat guardrail",
                             stats={"credit_sum": None, "replay_learned": "oops"})])
        vol = legacy.export_heirloom(cfg, out_dir=str(tmp_path / "h"))
        # Nothing validated and no scars/reflexes → nothing published.
        assert vol is None

    def test_cap_and_best_first(self, tmp_path):
        cfg = _cfg(tmp_path)
        # More validated engrams than the cap, with monotonically increasing credit → the top-cap
        # by utility must be kept and ordered best-first.
        egs = [Engram(kind="fact", body=f"validated fact number {i:04d}",
                      stats={"credit_sum": float(i)}) for i in range(1, 30)]
        _commit(cfg, egs)
        small_cap = 5
        orig = legacy.HEIRLOOM_MAX_RECORDS
        legacy.HEIRLOOM_MAX_RECORDS = small_cap
        try:
            _hdr, recs = legacy.read_heirloom(legacy.export_heirloom(cfg, out_dir=str(tmp_path / "h")))
        finally:
            legacy.HEIRLOOM_MAX_RECORDS = orig
        assert len(recs) == small_cap                          # bounded
        credits = [r["stats_summary"]["credit_sum"] for r in recs]
        assert credits == sorted(credits, reverse=True)        # best-first
        assert credits[0] == 29.0                              # the highest-utility one survived


# =================================================================================================
class TestHeirloomShape:
    def test_record_and_header_shape(self, tmp_path):
        cfg = _cfg(tmp_path)
        # Seed a persona + quests so the header metrics are populated.
        import persona
        p = persona.load_persona(cfg.workspace)
        p["name"] = "Sprocket"
        p["level"] = 3
        p["goals_completed"] = 7
        p["born"] = "2026-07-01T00:00:00Z"
        persona.save_persona(cfg.workspace, p)
        _commit(cfg, [_validated_strategy()])

        vol = legacy.export_heirloom(cfg, out_dir=str(tmp_path / "heirlooms"))
        assert vol.name.startswith("sprocket-")                # slugified name in the filename
        hdr, recs = legacy.read_heirloom(vol)

        # header carries the generational metrics
        assert hdr["creature"] == "Sprocket"
        assert hdr["level"] == 3
        assert hdr["goals_completed"] == 7
        assert hdr["birth_ts"] == "2026-07-01T00:00:00Z"
        assert hdr["retire_ts"]                                # a retirement timestamp is stamped
        assert hdr["quests_passed"] == 0
        assert hdr["record_count"] == len(recs)

        # each record carries the full shape
        r = recs[0]
        assert set(r) >= {"kind", "body", "provenance_chain", "stats_summary", "exported_ts"}
        assert r["provenance_chain"]["ancestor"] == "Sprocket"
        assert r["provenance_chain"]["original_provenance"] == "experienced"

    def test_reflex_registry_carried_with_provenance(self, tmp_path):
        cfg = _cfg(tmp_path)
        _write_reflexes(cfg, [_reflex(rid="rx-alpha", status="armed", fired=12)])
        _hdr, recs = legacy.read_heirloom(legacy.export_heirloom(cfg, out_dir=str(tmp_path / "h")))
        reflexes = [r for r in recs if r["kind"] == "reflex"]
        assert len(reflexes) == 1
        assert reflexes[0]["body"]["id"] == "rx-alpha"
        assert reflexes[0]["provenance_chain"]["source_status"] == "armed"

    def test_nothing_earned_writes_no_volume(self, tmp_path):
        cfg = _cfg(tmp_path)
        _commit(cfg, [_unvalidated_fact(), _weak_error()])     # nothing qualifies
        assert legacy.export_heirloom(cfg, out_dir=str(tmp_path / "h")) is None


# =================================================================================================
class TestBestEffort:
    def test_corrupt_longterm_store_does_not_raise(self, tmp_path):
        cfg = _cfg(tmp_path)
        # Write a garbage long-term jsonl where the store lives.
        lt = engram._longterm_jsonl_path(cfg)
        lt.parent.mkdir(parents=True, exist_ok=True)
        lt.write_text("this is not json\n{broken\n", encoding="utf-8")
        # Add a valid reflex so there is SOMETHING to publish despite the corrupt engram store.
        _write_reflexes(cfg, [_reflex(rid="rx1")])
        vol = legacy.export_heirloom(cfg, out_dir=str(tmp_path / "h"))   # must not raise
        assert vol is not None                                          # the reflex still published
        _hdr, recs = legacy.read_heirloom(vol)
        assert all(r["kind"] == "reflex" for r in recs)

    def test_missing_persona_fails_open_to_default_name(self, tmp_path):
        cfg = _cfg(tmp_path)
        _commit(cfg, [_validated_strategy()])
        vol = legacy.export_heirloom(cfg, out_dir=str(tmp_path / "h"))
        hdr, _recs = legacy.read_heirloom(vol)
        assert hdr["creature"]                                # some name; never crashes on absence

    def test_read_heirloom_tolerates_garbage_lines(self, tmp_path):
        bad = tmp_path / "bad.jsonl"
        bad.write_text('{"header": {"creature": "x"}}\nnot json\n{"kind":"fact","body":"ok"}\n',
                       encoding="utf-8")
        hdr, recs = legacy.read_heirloom(bad)
        assert hdr["creature"] == "x"
        assert len(recs) == 1 and recs[0]["body"] == "ok"


# =================================================================================================
class TestSeedImport:
    def _make_shelf(self, tmp_path):
        """Export a volume from a donor creature; return the heirlooms dir path."""
        donor = _cfg(tmp_path, sub="donor_ws")
        _commit(donor, [_validated_strategy(), _scar()])
        _write_reflexes(donor, [_reflex(rid="rx1", status="armed", fired=9)])
        shelf = tmp_path / "heirlooms"
        legacy.export_heirloom(donor, out_dir=str(shelf))
        return shelf

    def test_import_stamps_inherited_at_the_discount(self, tmp_path):
        shelf = self._make_shelf(tmp_path)
        nb = _cfg(tmp_path, sub="newborn_ws")
        r = seed_knowledge.import_heirloom(nb, out_dir=str(shelf))
        assert r["engrams"] == 2
        egs = LongTermStore(nb).load()
        assert egs and all(e.provenance == "inherited" for e in egs)      # provenance stamp
        assert all(e.strength == INHERITED_STRENGTH_FLOOR for e in egs)   # the told/inherited discount

    def test_inherited_reflexes_land_disarmed(self, tmp_path):
        shelf = self._make_shelf(tmp_path)
        nb = _cfg(tmp_path, sub="newborn_ws")
        r = seed_knowledge.import_heirloom(nb, out_dir=str(shelf))
        assert r["reflexes"] == 1
        rx = json.loads((nb.state_dir / "reflexes.json").read_text())["reflexes"]
        assert len(rx) == 1
        assert rx[0]["status"] == "proposed"       # DISARMED, never armed (WIS1 across generations)
        assert rx[0]["fired_count"] == 0           # ancestor's firing counters zeroed for the new body
        assert rx[0]["inherited_from"]             # provenance of whose reflex this was

    def test_reseed_is_idempotent(self, tmp_path):
        shelf = self._make_shelf(tmp_path)
        nb = _cfg(tmp_path, sub="newborn_ws")
        r1 = seed_knowledge.import_heirloom(nb, out_dir=str(shelf))
        n_eng = len(LongTermStore(nb).load())
        r2 = seed_knowledge.import_heirloom(nb, out_dir=str(shelf))   # re-run
        assert r2["engrams"] == 0 and r2["reflexes"] == 0            # no new writes
        assert len(LongTermStore(nb).load()) == n_eng               # engram count stable
        rx = json.loads((nb.state_dir / "reflexes.json").read_text())["reflexes"]
        assert len(rx) == 1                                          # reflex not duplicated

    def test_empty_shelf_imports_nothing(self, tmp_path):
        nb = _cfg(tmp_path, sub="newborn_ws")
        r = seed_knowledge.import_heirloom(nb, out_dir=str(tmp_path / "empty_shelf"))
        assert r == {"engrams": 0, "reflexes": 0, "volume": None}

    def test_latest_volume_is_chosen(self, tmp_path):
        # Two volumes on the shelf; the date-stamped filename sort picks the newest.
        shelf = tmp_path / "heirlooms"
        shelf.mkdir(parents=True)
        (shelf / "alpha-20260101.jsonl").write_text(
            '{"header":{"creature":"alpha"}}\n{"kind":"fact","body":"old"}\n', encoding="utf-8")
        (shelf / "beta-20260720.jsonl").write_text(
            '{"header":{"creature":"beta"}}\n{"kind":"fact","body":"new"}\n', encoding="utf-8")
        assert legacy.latest_heirloom(str(shelf)).name == "beta-20260720.jsonl"


# =================================================================================================
class TestNoHeirloomFlag:
    def test_no_heirloom_flag_skips_inheritance(self, tmp_path, monkeypatch, capsys):
        # Build a shelf and a config, then drive main() with --no-heirloom; nothing inherited.
        donor = _cfg(tmp_path, sub="donor_ws")
        _commit(donor, [_validated_strategy()])
        shelf = _REPO / "heirlooms"   # main() reads the repo-side shelf via legacy.latest_heirloom

        nb = _cfg(tmp_path, sub="newborn_ws")
        # Stub load_config so main() targets our newborn workspace, and count import_heirloom calls.
        monkeypatch.setattr(seed_knowledge, "load_config", lambda *_a, **_k: nb)
        monkeypatch.setattr(seed_knowledge, "load_nuggets", lambda *a, **k: [])   # skip nugget writes
        called = {"n": 0}

        def _spy(*a, **k):
            called["n"] += 1
            return {"engrams": 0, "reflexes": 0, "volume": None}

        monkeypatch.setattr(seed_knowledge, "import_heirloom", _spy)
        monkeypatch.setattr(sys, "argv", ["seed_knowledge.py", "--no-heirloom"])
        seed_knowledge.main()
        assert called["n"] == 0                                # import path not taken
        assert "--no-heirloom" in capsys.readouterr().out

    def test_main_without_flag_calls_import(self, tmp_path, monkeypatch):
        nb = _cfg(tmp_path, sub="newborn_ws")
        monkeypatch.setattr(seed_knowledge, "load_config", lambda *_a, **_k: nb)
        monkeypatch.setattr(seed_knowledge, "load_nuggets", lambda *a, **k: [])
        called = {"n": 0}

        def _spy(*a, **k):
            called["n"] += 1
            return {"engrams": 0, "reflexes": 0, "volume": None}

        monkeypatch.setattr(seed_knowledge, "import_heirloom", _spy)
        monkeypatch.setattr(sys, "argv", ["seed_knowledge.py"])
        seed_knowledge.main()
        assert called["n"] == 1                                # default: inheritance runs


# =================================================================================================
class TestArtifacts:
    def test_readme_written_alongside_volume(self, tmp_path):
        cfg = _cfg(tmp_path)
        _commit(cfg, [_validated_strategy()])
        shelf = tmp_path / "heirlooms"
        legacy.export_heirloom(cfg, out_dir=str(shelf))
        readme = shelf / "README.md"
        assert readme.exists()
        text = readme.read_text()
        assert "bookshelf" in text.lower() and "inherited" in text.lower()

    def test_repo_readme_exists(self):
        # The committed lineage bookshelf ships a README explaining what heirlooms are.
        assert (_REPO / "heirlooms" / "README.md").exists()

    def test_fresh_slate_passes_bash_n(self):
        script = _REPO / "scripts" / "fresh_slate.sh"
        assert script.exists()
        res = subprocess.run(["bash", "-n", str(script)], capture_output=True, text=True)
        assert res.returncode == 0, res.stderr

    def test_fresh_slate_exports_before_wipe(self):
        # The publish step must precede the reset step (retirement is the last chance to publish).
        text = (_REPO / "scripts" / "fresh_slate.sh").read_text()
        assert "export_heirloom" in text
        # The EXECUTABLE reset invocation (not the header-comment mention of --keep-knowledge) must
        # follow the export.
        reset_call = 'reset_eidos.py --yes\n'
        assert text.index("export_heirloom") < text.index(reset_call)
