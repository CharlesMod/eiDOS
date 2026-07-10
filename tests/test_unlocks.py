"""Tool unlocks (unlocks.py) — the growing body's single source of truth. Offline unit tests.

Pins (TOOL_PROGRESSION.md, PILLARS_PLAN §0):
  - the UNIT TABLE: canonical grant order, every tool in exactly one unit, registers valid;
  - the newborn floor is ALWAYS present — no config, no file, corrupt file, post-grant;
  - grant(): idempotent, persisted atomically, logged, unknown units refused;
  - milestone adjudication: typed quests.Criterion over the stats dict (sleeps.total,
    quests.passed) — glue judges, quest-issuance units are never self-granted;
  - the I8 senses hold: criterion met + probe falsy → PENDING (recorded, retried), the grant
    lands only the tick the probe answers True — a granted limb that 500s is a felt lie;
  - the felt moment: one-shot announcement queue, rendered-flags persisted — survives reload,
    a crash between grant and render never eats the moment, migration seeds are silent;
  - corrupt-file fail-open: newborn floor only (never empty, never full), nothing raises,
    seed_from_evidence() re-seeds over the corpse (the documented recovery);
  - evidence seeding: prior adjudicated facts → their units, fresh slate → newborn only;
  - the ledger firewall: unlocks.py is never imported by persona.py / level_gates.py
    (capability, never the ledger — same pattern as test_genome's firewall).

No services / tick loop / GPU — temp workspaces only (eiDOS is live on this machine).
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from config import Config
from unlocks import (
    EVIDENCE_KEYS, LOG_MAX, NEWBORN_UNIT_ID, REGISTER_BODY, REGISTER_SYSTEM, STATE_NAME,
    STATE_VERSION, UNIT_IDS, UNITS, UnlockState, adjudicate, grant, granted_tools,
    newborn_tools, pop_unannounced, seed_from_evidence, unit,
)

_ROOT = Path(__file__).parent.parent

# The canonical ladder (TOOL_PROGRESSION.md) — pinned so a table edit is a conscious act.
_LADDER = ("body", "memory", "skillcraft", "foresight", "senses", "resolve", "workshop",
           "commission")
_NEWBORN = frozenset({"bash", "write_file", "read_file",
                      "note_append", "note_read", "note_list", "note_close", "check_tools"})


# --- helpers -------------------------------------------------------------------------------------

def _cfg(tmp_path, *, mkdir: bool = True) -> Config:
    cfg = Config()
    cfg.workspace_dir = str(tmp_path / "workspace")
    cfg.mock_mode = True
    if mkdir:
        cfg.workspace.mkdir(parents=True, exist_ok=True)
    return cfg


def _state_doc(cfg) -> dict:
    return json.loads((cfg.state_dir / STATE_NAME).read_text(encoding="utf-8"))


def _stats(sleeps=0, quests_passed=0) -> dict:
    """The typed stats dict shape quest glue adjudicates (paths sleeps.total / quests.passed)."""
    return {"sleeps": {"total": sleeps}, "quests": {"passed": quests_passed}}


# =================================================================================================
class TestUnitTable:
    """The ladder as data: canonical order, exact membership, no tool in two units."""

    def test_canonical_grant_order(self):
        assert UNIT_IDS == _LADDER

    def test_every_tool_in_exactly_one_unit(self):
        seen: dict[str, str] = {}
        for u in UNITS:
            for t in u.tools:
                assert t not in seen, f"{t!r} appears in both {seen[t]!r} and {u.id!r}"
                seen[t] = u.id

    def test_exact_unit_membership(self):
        """Pin every rung's tools verbatim (TOOL_PROGRESSION's table) — aliases travel together."""
        by_id = {u.id: set(u.tools) for u in UNITS}
        assert by_id["body"] == set(_NEWBORN)
        assert by_id["memory"] == {"memorize", "recall"}
        assert by_id["skillcraft"] == {"create_skill", "edit_skill", "list_skills",
                                       "rollback_skill", "manual"}
        assert by_id["foresight"] == {"predict"}
        assert by_id["senses"] == {"speak", "vision", "see"}
        assert by_id["resolve"] == {"objective_add", "objective_done",
                                    "objective_block", "objective_list"}
        assert by_id["workshop"] == {"delegate"}

    def test_criteria_and_service_gates(self):
        """Milestone units carry criteria; issuance/reward units carry NONE (the System's window
        is the moment); only senses is service-gated (I8)."""
        by_id = {u.id: u for u in UNITS}
        assert by_id["body"].criterion is None
        assert by_id["memory"].criterion is not None
        assert by_id["senses"].criterion is not None
        for uid in ("skillcraft", "foresight", "resolve", "workshop"):
            assert by_id[uid].criterion is None, f"{uid} must be quest-granted, never self-adjudicated"
        for u in UNITS:
            expect = "voice" if u.id == "senses" else None
            assert u.requires_service == expect

    def test_registers_and_announcements(self):
        by_id = {u.id: u for u in UNITS}
        assert by_id["body"].announce == ""                       # being born is not an event
        assert by_id["memory"].register == REGISTER_BODY          # a maturation, never a payment
        assert by_id["senses"].register == REGISTER_BODY
        for uid in ("skillcraft", "foresight", "resolve", "workshop"):
            assert by_id[uid].register == REGISTER_SYSTEM         # the System pays capability
        for u in UNITS:
            if u.id == NEWBORN_UNIT_ID:
                continue
            assert u.announce, f"{u.id} grants silently — the felt moment is lost"
            for t in u.tools:
                assert t in u.announce, f"{u.id}'s announcement never names {t!r}"

    def test_unit_lookup(self):
        assert unit("memory") is not None and unit("memory").id == "memory"
        assert unit("no_such_unit") is None


# =================================================================================================
class TestNewbornFloor:
    """The floor is table data, not state: present with no config, no file, corrupt file."""

    def test_no_config_is_newborn_only(self):
        assert granted_tools(None) == _NEWBORN

    def test_no_file_is_newborn_only_and_creates_nothing(self, tmp_path):
        cfg = _cfg(tmp_path, mkdir=False)
        assert granted_tools(cfg) == _NEWBORN
        assert not Path(cfg.workspace_dir).exists()       # the read side never creates anything

    def test_newborn_tools_matches_body_unit(self):
        assert newborn_tools() == _NEWBORN
        assert newborn_tools()                             # never empty

    def test_floor_survives_every_grant(self, tmp_path):
        cfg = _cfg(tmp_path)
        for uid in _LADDER[1:]:
            grant(cfg, uid, "test")
        assert _NEWBORN <= granted_tools(cfg)


# =================================================================================================
class TestGrant:
    def test_grant_is_idempotent_and_persisted(self, tmp_path):
        cfg = _cfg(tmp_path)
        assert grant(cfg, "memory", "milestone") is True
        assert grant(cfg, "memory", "milestone") is False          # idempotent — no double grant
        assert {"memorize", "recall"} <= granted_tools(cfg)
        # A fresh read of the books (new process) still shows the grant.
        doc = _state_doc(cfg)
        assert doc["v"] == STATE_VERSION
        assert doc["granted"]["memory"]["source"] == "milestone"
        assert doc["granted"]["memory"]["ts"]

    def test_unknown_unit_refused(self, tmp_path):
        cfg = _cfg(tmp_path)
        assert grant(cfg, "wings", "test") is False
        assert granted_tools(cfg) == _NEWBORN
        assert grant(None, "memory", "test") is False              # fail-open, never raises

    def test_grant_is_logged(self, tmp_path):
        cfg = _cfg(tmp_path)
        grant(cfg, "workshop", "quest:genesis-03-make-it-yours")
        log = _state_doc(cfg)["log"]
        entry = next(e for e in log if e["event"] == "grant" and e["unit"] == "workshop")
        assert entry["source"] == "quest:genesis-03-make-it-yours"
        assert entry["ts"]

    def test_log_stays_bounded(self, tmp_path):
        cfg = _cfg(tmp_path)
        state = UnlockState(cfg)
        for i in range(LOG_MAX + 50):
            state.record("pending", "senses", reason=f"r{i}")
        state.save()
        assert len(_state_doc(cfg)["log"]) == LOG_MAX

    def test_grant_write_is_atomic_no_tmp_left(self, tmp_path):
        cfg = _cfg(tmp_path)
        grant(cfg, "memory", "milestone")
        assert not (cfg.state_dir / (STATE_NAME + ".tmp")).exists()
        assert not list(cfg.state_dir.glob("*.tmp"))


# =================================================================================================
class TestAdjudicate:
    """Glue judges (§0.5): typed criteria over the stats dict; issuance units never self-grant."""

    def test_memory_lands_on_first_sleep(self, tmp_path):
        cfg = _cfg(tmp_path)
        assert adjudicate(cfg, _stats(sleeps=0)) == []
        assert granted_tools(cfg) == _NEWBORN
        assert adjudicate(cfg, _stats(sleeps=1)) == ["memory"]
        assert {"memorize", "recall"} <= granted_tools(cfg)
        assert adjudicate(cfg, _stats(sleeps=5)) == []             # idempotent — already granted

    def test_issuance_units_never_self_grant(self, tmp_path):
        """However rich the life, skillcraft/foresight/resolve/workshop arrive ONLY through
        grant() from the quest seams — adjudicate has no criterion to fire."""
        cfg = _cfg(tmp_path)
        landed = adjudicate(cfg, _stats(sleeps=99, quests_passed=99),
                            probe=lambda s: True)
        assert set(landed) == {"memory", "senses", "commission"}   # milestones only
        for t in ("create_skill", "predict", "objective_add", "delegate"):
            assert t not in granted_tools(cfg)

    def test_broken_stats_never_grant(self, tmp_path):
        cfg = _cfg(tmp_path)
        assert adjudicate(cfg, {}) == []
        assert adjudicate(cfg, None) == []
        assert adjudicate(cfg, {"sleeps": "corrupt"}) == []
        assert granted_tools(cfg) == _NEWBORN

    def test_no_config_fails_open(self):
        assert adjudicate(None, _stats(sleeps=3)) == []


# =================================================================================================
class TestSensesPendingUntilProbe:
    """I8: the criterion can be met for days while the organ is down — the grant holds PENDING
    and lands only the tick the injected probe answers True (voice is down on Sprinter today)."""

    def test_pending_when_probe_absent(self, tmp_path):
        cfg = _cfg(tmp_path)
        assert adjudicate(cfg, _stats(sleeps=2, quests_passed=1)) == ["memory"]
        assert "speak" not in granted_tools(cfg)
        doc = _state_doc(cfg)
        assert "senses" in doc["pending"]
        assert "voice" in doc["pending"]["senses"]                 # the reason names the organ

    def test_pending_when_probe_false_or_raising(self, tmp_path):
        cfg = _cfg(tmp_path)
        adjudicate(cfg, _stats(sleeps=2, quests_passed=1), probe=lambda s: False)
        assert "senses" in _state_doc(cfg)["pending"]

        def _boom(_s):
            raise OSError("connection refused")
        adjudicate(cfg, _stats(sleeps=2, quests_passed=1), probe=_boom)
        assert "senses" in _state_doc(cfg)["pending"]              # an erroring probe = unreachable
        assert "speak" not in granted_tools(cfg)

    def test_grant_lands_when_probe_answers_and_pending_clears(self, tmp_path):
        cfg = _cfg(tmp_path)
        adjudicate(cfg, _stats(sleeps=2, quests_passed=1))         # held
        probed = []
        landed = adjudicate(cfg, _stats(sleeps=2, quests_passed=1),
                            probe=lambda s: probed.append(s) or True)
        assert landed == ["senses"]
        assert probed == ["voice"]                                 # the probe is asked BY NAME
        assert {"speak", "vision", "see"} <= granted_tools(cfg)
        assert "senses" not in _state_doc(cfg)["pending"]
        # The felt moment queues only on the landing tick, body register.
        notes = pop_unannounced(cfg)
        senses = next(n for n in notes if n["unit"] == "senses")
        assert senses["register"] == REGISTER_BODY

    def test_criterion_alone_is_not_enough(self, tmp_path):
        """A live probe with an unmet criterion grants nothing — reachability never substitutes
        for lived evidence."""
        cfg = _cfg(tmp_path)
        assert adjudicate(cfg, _stats(sleeps=1, quests_passed=0),
                          probe=lambda s: True) == ["memory"]
        assert "speak" not in granted_tools(cfg)
        assert "senses" not in _state_doc(cfg)["pending"]          # not even pending — not earned


# =================================================================================================
class TestFeltMoment:
    """One-shot announcements with persisted rendered-flags."""

    def test_pop_returns_once_and_survives_reload(self, tmp_path):
        cfg = _cfg(tmp_path)
        grant(cfg, "memory", "milestone")
        # Crash between grant and render: the grant persisted first, so a FRESH read of the
        # books (new process) still finds the moment waiting.
        notes = pop_unannounced(cfg)
        assert notes == [{"unit": "memory", "register": REGISTER_BODY,
                          "text": "[overnight, new words settled in you: memorize, recall]"}]
        assert pop_unannounced(cfg) == []                          # one-shot
        # And the rendered-flag itself is persisted: another fresh read replays nothing.
        assert "memory" in _state_doc(cfg)["announced"]
        assert pop_unannounced(cfg) == []

    def test_system_register_pays_capability(self, tmp_path):
        cfg = _cfg(tmp_path)
        grant(cfg, "workshop", "quest:genesis-03")
        notes = pop_unannounced(cfg)
        assert len(notes) == 1
        assert notes[0]["register"] == REGISTER_SYSTEM
        assert notes[0]["text"].startswith("[SYSTEM]")
        assert "delegate" in notes[0]["text"]

    def test_canonical_order_and_body_never_announced(self, tmp_path):
        cfg = _cfg(tmp_path)
        grant(cfg, "workshop", "t")
        grant(cfg, "memory", "t")
        grant(cfg, NEWBORN_UNIT_ID, "t")
        notes = pop_unannounced(cfg)
        assert [n["unit"] for n in notes] == ["memory", "workshop"]   # table order, no "body"

    def test_no_grants_nothing_to_pop(self, tmp_path):
        cfg = _cfg(tmp_path)
        assert pop_unannounced(cfg) == []
        assert pop_unannounced(None) == []


# =================================================================================================
class TestCorruptFailOpen:
    """Corrupt books read as the newborn floor — never empty, never full — and the documented
    recovery is a re-seed from evidence."""

    def test_corrupt_file_is_newborn_only_nothing_raises(self, tmp_path):
        cfg = _cfg(tmp_path)
        cfg.state_dir.mkdir(parents=True, exist_ok=True)
        (cfg.state_dir / STATE_NAME).write_text("{not json", encoding="utf-8")
        tools = granted_tools(cfg)
        assert tools == _NEWBORN                                   # never empty, never full
        assert pop_unannounced(cfg) == []
        assert adjudicate(cfg, _stats(sleeps=0)) == []

    def test_corrupt_values_read_as_fresh_books(self, tmp_path):
        cfg = _cfg(tmp_path)
        cfg.state_dir.mkdir(parents=True, exist_ok=True)
        (cfg.state_dir / STATE_NAME).write_text(
            json.dumps({"v": 1, "granted": {"memory": "yes"}, "pending": [], "announced": 3}),
            encoding="utf-8")
        assert granted_tools(cfg) == _NEWBORN

    def test_unknown_units_in_books_are_ignored(self, tmp_path):
        """A hand-edited (or future-version) file can never grant a unit outside the table."""
        cfg = _cfg(tmp_path)
        cfg.state_dir.mkdir(parents=True, exist_ok=True)
        (cfg.state_dir / STATE_NAME).write_text(
            json.dumps({"v": 1, "granted": {"wings": {"ts": "x", "source": "edit"},
                                            "memory": {"ts": "x", "source": "edit"}},
                        "pending": {}, "announced": [], "log": []}),
            encoding="utf-8")
        tools = granted_tools(cfg)
        assert {"memorize", "recall"} <= tools
        assert tools == _NEWBORN | {"memorize", "recall"}

    def test_reseed_over_the_corpse(self, tmp_path):
        cfg = _cfg(tmp_path)
        cfg.state_dir.mkdir(parents=True, exist_ok=True)
        (cfg.state_dir / STATE_NAME).write_text("\x00garbage", encoding="utf-8")
        seeded = seed_from_evidence(cfg, {"sleeps": 3, "live_skills": 1})
        assert seeded == ["body", "memory", "skillcraft"]
        assert {"memorize", "create_skill"} <= granted_tools(cfg)
        assert _state_doc(cfg)["v"] == STATE_VERSION               # healthy books again


# =================================================================================================
class TestEvidenceSeeding:
    """Migration is load-or-birth: prior adjudicated evidence → organs; fresh slate → newborn."""

    def test_fresh_slate_seeds_newborn_only(self, tmp_path):
        cfg = _cfg(tmp_path)
        assert seed_from_evidence(cfg, {}) == ["body"]
        assert granted_tools(cfg) == _NEWBORN
        assert (cfg.state_dir / STATE_NAME).exists()               # the books now exist (load-or-birth)

    def test_full_evidence_grants_every_unit(self, tmp_path):
        cfg = _cfg(tmp_path)
        seeded = seed_from_evidence(cfg, {k: 1 for k in EVIDENCE_KEYS})
        assert tuple(seeded) == _LADDER                            # canonical order, all rungs
        all_tools = frozenset(t for u in UNITS for t in u.tools)
        assert granted_tools(cfg) == all_tools

    def test_partial_evidence_and_sources(self, tmp_path):
        cfg = _cfg(tmp_path)
        seeded = seed_from_evidence(cfg, {"sleeps": 2, "objectives": True, "nonsense": 9})
        assert seeded == ["body", "memory", "resolve"]
        doc = _state_doc(cfg)
        assert doc["granted"]["memory"]["source"] == "evidence:sleeps"
        assert doc["granted"]["resolve"]["source"] == "evidence:objectives"
        assert doc["granted"]["body"]["source"] == "born"
        assert "predict" not in granted_tools(cfg)

    def test_seeded_grants_are_silent(self, tmp_path):
        """The moments were already lived — migration never replays them as announcements."""
        cfg = _cfg(tmp_path)
        seed_from_evidence(cfg, {k: 1 for k in EVIDENCE_KEYS})
        assert pop_unannounced(cfg) == []

    def test_seeding_is_idempotent_and_never_downgrades(self, tmp_path):
        cfg = _cfg(tmp_path)
        grant(cfg, "workshop", "quest:genesis-03")
        assert seed_from_evidence(cfg, {"sleeps": 1}) == ["body", "memory"]
        assert seed_from_evidence(cfg, {"sleeps": 1}) == []        # second pass: nothing new
        assert "delegate" in granted_tools(cfg)                    # the earlier grant survives
        assert seed_from_evidence(None, {"sleeps": 1}) == []       # fail-open


# =================================================================================================
class TestLedgerFirewall:
    """Capability, never the ledger: the earning rules must not know the body exists."""

    def test_ledger_files_never_import_unlocks(self):
        for fname in ("persona.py", "level_gates.py"):
            src = (_ROOT / fname).read_text(encoding="utf-8")
            assert "import unlocks" not in src and "from unlocks" not in src, \
                f"{fname} imports unlocks — capability has crossed into the ledger"

    def test_unlocks_imports_no_ledger(self):
        """The import points one way: unlocks.py may use quests.Criterion (the predicate TYPE),
        but never persona / level_gates / bets / expectations (the earning rules)."""
        src = (_ROOT / "unlocks.py").read_text(encoding="utf-8")
        for banned in ("import persona", "from persona", "import level_gates",
                       "from level_gates", "import bets", "from bets",
                       "import expectations", "from expectations"):
            assert banned not in src, f"unlocks.py contains {banned!r} — the firewall is breached"
