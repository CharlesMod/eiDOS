"""The `remind` primitive (OPERATOR_DIRECTIVES §"The `remind` primitive", OD4/OD5) — pinned.

Covers: parse_when across every accepted form + garbage; the set/due/pop lifecycle; the bound
(a full store is a TYPED refusal, never a silent drop — ARCH #4); idempotent re-set; backward-clock
safety (a future fire_ts never fires; a clock jump backward loses nothing); corrupt-file fail-open;
the `remind` tool's honest success/failure shapes; and flag-off byte-identical (the tool is absent
from the registry and NO reminders file is written).
"""

import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import reminders
import tools
from config import Config


class _Base(unittest.TestCase):
    def setUp(self):
        self.cfg = Config()
        self.cfg.workspace_dir = tempfile.mkdtemp()
        (Path(self.cfg.workspace_dir) / "workspace").mkdir(parents=True, exist_ok=True)
        self.cfg.reminders_enabled = True
        self.cfg.reminders_max_pending = 32

    def _store_path(self):
        return self.cfg.state_dir / reminders.STATE_NAME


# --------------------------------------------------------------------------------------------------
# parse_when
# --------------------------------------------------------------------------------------------------

class TestParseWhen(unittest.TestCase):
    def test_relative_simple(self):
        now = time.time()
        self.assertAlmostEqual(reminders.parse_when("90s") - now, 90, delta=2)
        self.assertAlmostEqual(reminders.parse_when("10m") - now, 600, delta=2)
        self.assertAlmostEqual(reminders.parse_when("2h") - now, 7200, delta=2)
        self.assertAlmostEqual(reminders.parse_when("3d") - now, 3 * 86400, delta=2)

    def test_relative_composite(self):
        now = time.time()
        self.assertAlmostEqual(reminders.parse_when("1h30m") - now, 5400, delta=2)
        self.assertAlmostEqual(reminders.parse_when("2h15m30s") - now, 8130, delta=2)
        self.assertAlmostEqual(reminders.parse_when("1d12h") - now, 86400 + 12 * 3600, delta=2)

    def test_whitespace_tolerant(self):
        now = time.time()
        self.assertAlmostEqual(reminders.parse_when("10 m") - now, 600, delta=2)

    def test_clock_time_future_today_or_tomorrow(self):
        # "at HH:MM" and bare "HH:MM" both resolve to the NEXT occurrence — always in the future.
        for spec in ("at 22:15", "22:15", "at 9:05"):
            ts = reminders.parse_when(spec)
            self.assertIsNotNone(ts, spec)
            self.assertGreater(ts, time.time(), spec)
            # within the next 24h
            self.assertLessEqual(ts - time.time(), 24 * 3600 + 5, spec)

    def test_iso_timestamp(self):
        # A concrete far-future ISO time parses to that instant.
        ts = reminders.parse_when("2099-01-02T03:04:05")
        self.assertIsNotNone(ts)
        from datetime import datetime
        self.assertEqual(datetime.fromtimestamp(ts).year, 2099)

    def test_iso_space_and_z(self):
        self.assertIsNotNone(reminders.parse_when("2099-01-02 03:04"))
        self.assertIsNotNone(reminders.parse_when("2099-01-02T03:04:05Z"))

    def test_iso_bare_date(self):
        ts = reminders.parse_when("2099-01-02")
        self.assertIsNotNone(ts)

    def test_garbage_returns_none(self):
        for bad in ("", "   ", "later", "soon", "meet at 3pm", "abc", "m", "h",
                    "0m", "0s", "-5m", "25:00", "12:99", None, "in a while"):
            self.assertIsNone(reminders.parse_when(bad), repr(bad))


# --------------------------------------------------------------------------------------------------
# set / due / pop lifecycle
# --------------------------------------------------------------------------------------------------

class TestLifecycle(_Base):
    def test_set_persists_and_returns_dict(self):
        r = reminders.set_reminder(self.cfg, "check backup", fire_ts=time.time() + 100)
        self.assertEqual(r["note"], "check backup")
        self.assertEqual(r["origin"], "creature")
        self.assertFalse(r["fired"])
        self.assertTrue(r["id"])
        self.assertTrue(self._store_path().exists())
        # survives a reload (a different call re-reads the file — restart-survival in miniature)
        pend = reminders.pending(self.cfg)
        self.assertEqual(len(pend), 1)
        self.assertEqual(pend[0]["note"], "check backup")

    def test_due_pops_only_passed(self):
        now = time.time()
        past = reminders.set_reminder(self.cfg, "past", fire_ts=now - 10)
        reminders.set_reminder(self.cfg, "future", fire_ts=now + 1000)
        fired = reminders.due(self.cfg, now)
        self.assertEqual([f["note"] for f in fired], ["past"])
        self.assertTrue(fired[0]["fired"])
        # the future one is still pending, the past one is consumed (swept)
        pend = reminders.pending(self.cfg)
        self.assertEqual([p["note"] for p in pend], ["future"])

    def test_due_fires_exactly_once(self):
        now = time.time()
        reminders.set_reminder(self.cfg, "once", fire_ts=now - 5)
        first = reminders.due(self.cfg, now)
        self.assertEqual(len(first), 1)
        second = reminders.due(self.cfg, now)
        self.assertEqual(second, [])   # already popped — never fires twice

    def test_cancel(self):
        r = reminders.set_reminder(self.cfg, "cancel me", fire_ts=time.time() + 100)
        self.assertTrue(reminders.cancel(self.cfg, r["id"]))
        self.assertEqual(reminders.pending(self.cfg), [])
        self.assertFalse(reminders.cancel(self.cfg, r["id"]))     # already gone
        self.assertFalse(reminders.cancel(self.cfg, "nope"))

    def test_pending_sorted_soonest_first(self):
        now = time.time()
        reminders.set_reminder(self.cfg, "late", fire_ts=now + 900)
        reminders.set_reminder(self.cfg, "soon", fire_ts=now + 10)
        reminders.set_reminder(self.cfg, "mid", fire_ts=now + 300)
        self.assertEqual([p["note"] for p in reminders.pending(self.cfg)], ["soon", "mid", "late"])

    def test_source_key_and_origin_preserved(self):
        r = reminders.set_reminder(self.cfg, "obj deadline", fire_ts=time.time() + 50,
                                   origin="operator", source_key="obj-42")
        self.assertEqual(r["origin"], "operator")
        self.assertEqual(r["source_key"], "obj-42")


# --------------------------------------------------------------------------------------------------
# bound enforcement — a full store is a TYPED refusal (ARCH #4)
# --------------------------------------------------------------------------------------------------

class TestBound(_Base):
    def test_full_store_typed_refusal(self):
        self.cfg.reminders_max_pending = 3
        now = time.time()
        for i in range(3):
            reminders.set_reminder(self.cfg, f"r{i}", fire_ts=now + 100 + i)
        with self.assertRaises(reminders.ReminderError) as ctx:
            reminders.set_reminder(self.cfg, "one too many", fire_ts=now + 500)
        self.assertEqual(ctx.exception.kind, "blocked")
        # the refusal wrote nothing extra
        self.assertEqual(len(reminders.pending(self.cfg)), 3)

    def test_fired_entries_do_not_count_against_bound(self):
        # A fired-but-not-yet-swept entry must not block a new set.
        self.cfg.reminders_max_pending = 1
        now = time.time()
        reminders.set_reminder(self.cfg, "old", fire_ts=now - 5)
        reminders.due(self.cfg, now)              # fires + sweeps "old"
        # store is now empty of pending → a new one seats
        r = reminders.set_reminder(self.cfg, "new", fire_ts=now + 100)
        self.assertEqual(r["note"], "new")

    def test_bad_time_typed_refusal(self):
        with self.assertRaises(reminders.ReminderError) as ctx:
            reminders.set_reminder(self.cfg, "x", fire_ts=float("nan"))
        self.assertEqual(ctx.exception.kind, "args")
        with self.assertRaises(reminders.ReminderError) as ctx2:
            reminders.set_reminder(self.cfg, "x", fire_ts="notanumber")
        self.assertEqual(ctx2.exception.kind, "args")
        with self.assertRaises(reminders.ReminderError) as ctx3:
            reminders.set_reminder(self.cfg, "x", fire_ts=time.time() + 10 * 366 * 86400)
        self.assertEqual(ctx3.exception.kind, "args")

    def test_empty_note_refused(self):
        with self.assertRaises(reminders.ReminderError) as ctx:
            reminders.set_reminder(self.cfg, "   ", fire_ts=time.time() + 10)
        self.assertEqual(ctx.exception.kind, "args")


# --------------------------------------------------------------------------------------------------
# idempotent re-set
# --------------------------------------------------------------------------------------------------

class TestIdempotent(_Base):
    def test_exact_reset_is_idempotent(self):
        ts = time.time() + 100
        a = reminders.set_reminder(self.cfg, "same", fire_ts=ts, source_key="k1")
        b = reminders.set_reminder(self.cfg, "same", fire_ts=ts, source_key="k1")
        self.assertEqual(a["id"], b["id"])                 # returns the SAME reminder
        self.assertEqual(len(reminders.pending(self.cfg)), 1)   # exactly one seated

    def test_differing_source_key_is_distinct(self):
        ts = time.time() + 100
        reminders.set_reminder(self.cfg, "same", fire_ts=ts, source_key="k1")
        reminders.set_reminder(self.cfg, "same", fire_ts=ts, source_key="k2")
        self.assertEqual(len(reminders.pending(self.cfg)), 2)

    def test_differing_time_is_distinct(self):
        now = time.time()
        reminders.set_reminder(self.cfg, "same", fire_ts=now + 100)
        reminders.set_reminder(self.cfg, "same", fire_ts=now + 200)
        self.assertEqual(len(reminders.pending(self.cfg)), 2)


# --------------------------------------------------------------------------------------------------
# backward-clock safety (OD5)
# --------------------------------------------------------------------------------------------------

class TestClockSafety(_Base):
    def test_future_never_fires(self):
        now = time.time()
        reminders.set_reminder(self.cfg, "future", fire_ts=now + 10_000)
        self.assertEqual(reminders.due(self.cfg, now), [])
        # even a now_ts far past the SET time but before fire_ts doesn't fire
        self.assertEqual(reminders.due(self.cfg, now + 5_000), [])
        self.assertEqual(len(reminders.pending(self.cfg)), 1)

    def test_backward_clock_jump_loses_nothing(self):
        now = time.time()
        reminders.set_reminder(self.cfg, "keep me", fire_ts=now + 100)
        # clock jumps BACKWARD (now_ts far in the past) — nothing fires, nothing is lost
        self.assertEqual(reminders.due(self.cfg, now - 100_000), [])
        self.assertEqual(len(reminders.pending(self.cfg)), 1)
        # when the clock catches up past fire_ts, it fires normally
        fired = reminders.due(self.cfg, now + 200)
        self.assertEqual([f["note"] for f in fired], ["keep me"])


# --------------------------------------------------------------------------------------------------
# corrupt-file fail-open (OD4)
# --------------------------------------------------------------------------------------------------

class TestFailOpen(_Base):
    def test_corrupt_file_reads_empty(self):
        self.cfg.state_dir.mkdir(parents=True, exist_ok=True)
        self._store_path().write_text("{ this is not json", encoding="utf-8")
        self.assertEqual(reminders.pending(self.cfg), [])
        self.assertEqual(reminders.due(self.cfg, time.time()), [])
        # and a set still works (the next write heals the file)
        r = reminders.set_reminder(self.cfg, "healed", fire_ts=time.time() + 10)
        self.assertEqual(r["note"], "healed")
        self.assertEqual(len(reminders.pending(self.cfg)), 1)

    def test_missing_file_reads_empty(self):
        self.assertFalse(self._store_path().exists())
        self.assertEqual(reminders.pending(self.cfg), [])
        self.assertEqual(reminders.due(self.cfg, time.time()), [])


# --------------------------------------------------------------------------------------------------
# the `remind` tool — honest success / failure shapes (ARCH #4)
# --------------------------------------------------------------------------------------------------

class TestRemindTool(_Base):
    def test_success_names_when(self):
        r = tools.tool_remind({"in": "10m", "note": "check backup"}, self.cfg)
        self.assertTrue(r.success, r.output)
        self.assertEqual(r.fail_kind, "")
        self.assertIn("check backup", r.output)
        self.assertEqual(len(reminders.pending(self.cfg)), 1)

    def test_at_form(self):
        r = tools.tool_remind({"at": "22:15", "note": "sleep cycle"}, self.cfg)
        self.assertTrue(r.success, r.output)
        self.assertEqual(len(reminders.pending(self.cfg)), 1)

    def test_bad_time_typed_failure(self):
        r = tools.tool_remind({"in": "later", "note": "x"}, self.cfg)
        self.assertFalse(r.success)
        self.assertEqual(r.fail_kind, "args")
        self.assertEqual(reminders.pending(self.cfg), [])   # nothing written

    def test_full_store_typed_failure(self):
        self.cfg.reminders_max_pending = 1
        tools.tool_remind({"in": "10m", "note": "first"}, self.cfg)
        r = tools.tool_remind({"in": "20m", "note": "second"}, self.cfg)
        self.assertFalse(r.success)
        self.assertEqual(r.fail_kind, "blocked")
        self.assertEqual(len(reminders.pending(self.cfg)), 1)

    def test_no_when_typed_failure(self):
        r = tools.tool_remind({"note": "x"}, self.cfg)
        self.assertFalse(r.success)
        self.assertEqual(r.fail_kind, "args")

    def test_both_when_typed_failure(self):
        r = tools.tool_remind({"in": "10m", "at": "22:15", "note": "x"}, self.cfg)
        self.assertFalse(r.success)
        self.assertEqual(r.fail_kind, "args")

    def test_in_underscore_key_accepted(self):
        # model_dump serializes the field as `in_`; the handler reads both.
        r = tools.tool_remind({"in_": "5m", "note": "y"}, self.cfg)
        self.assertTrue(r.success, r.output)

    def test_args_model_forbids_extra(self):
        from pydantic import ValidationError
        with self.assertRaises(ValidationError):
            tools.RemindArgs.model_validate({"in": "10m", "note": "x", "bogus": 1})

    def test_args_model_one_of_in_at(self):
        from pydantic import ValidationError
        with self.assertRaises(ValidationError):
            tools.RemindArgs.model_validate({"note": "x"})            # neither
        with self.assertRaises(ValidationError):
            tools.RemindArgs.model_validate({"in": "1m", "at": "2:00", "note": "x"})  # both


# --------------------------------------------------------------------------------------------------
# render_pending
# --------------------------------------------------------------------------------------------------

class TestRenderPending(_Base):
    def test_empty_is_blank(self):
        self.assertEqual(tools.render_pending(self.cfg), "")

    def test_bounded_and_soonest_first(self):
        now = time.time()
        for i in range(5):
            reminders.set_reminder(self.cfg, f"note{i}", fire_ts=now + 100 * (i + 1))
        line = tools.render_pending(self.cfg, max_items=3)
        self.assertTrue(line.startswith("⏳ pending reminders:"))
        self.assertIn("note0", line)          # soonest shown
        self.assertIn("(+2 more)", line)       # 5 total, 3 shown

    def test_off_flag_blank(self):
        self.cfg.reminders_enabled = False
        self.assertEqual(tools.render_pending(self.cfg), "")


# --------------------------------------------------------------------------------------------------
# flag-off: byte-identical — tool absent, no file written
# --------------------------------------------------------------------------------------------------

class TestFlagOff(_Base):
    def setUp(self):
        super().setUp()
        self.cfg.reminders_enabled = False

    def test_register_absent_when_off(self):
        # ensure a possibly-registered tool is cleared, then confirm the flag-off state
        tools.register_reminders_tool(self.cfg)
        self.assertNotIn("remind", tools.visible_tools(self.cfg))
        self.assertNotIn("remind", tools.TOOLS)

    def test_register_present_when_on(self):
        self.cfg.reminders_enabled = True
        try:
            self.assertTrue(tools.register_reminders_tool(self.cfg))
            self.assertIn("remind", tools.TOOLS)
            self.assertIn("remind", tools.visible_tools(self.cfg))
            self.assertIn("remind", tools._TOOL_ARG_MODELS)
        finally:
            self.cfg.reminders_enabled = False
            tools.register_reminders_tool(self.cfg)   # restore global registry for other tests

    def test_remind_in_flag_registered_builtins(self):
        self.assertIn("remind", tools._FLAG_REGISTERED_BUILTINS)
        self.assertIn("remind", tools._EVER_BUILTIN_NAMES)

    def test_direct_dispatch_dark_writes_nothing(self):
        # A direct dispatch while dark is a typed blocked refusal and touches no file.
        r = tools.tool_remind({"in": "10m", "note": "x"}, self.cfg)
        self.assertFalse(r.success)
        self.assertEqual(r.fail_kind, "blocked")
        self.assertFalse(self._store_path().exists())


if __name__ == "__main__":
    unittest.main()
