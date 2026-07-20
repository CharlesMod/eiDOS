"""OPERATOR_DIRECTIVES — the System hears Charlie and makes it the creature's priority focus.

Pins the core: an operator request becomes a persistent, preemptive origin:"operator" objective
(it does NOT evaporate after one tick), the System (Administrator role) does the classification,
chatter creates nothing, and the whole path is flag-dark. The `remind` deferral is exercised only
where reminders is present; here reminders_enabled stays off so the objective path is isolated.
"""

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import administrator as adm
import objectives
from config import Config


def _cfg(**over):
    c = Config()
    c.workspace_dir = tempfile.mkdtemp()
    c.workspace.mkdir(parents=True, exist_ok=True)
    c.state_dir.mkdir(parents=True, exist_ok=True)
    c.pillars_administrator_enabled = True
    c.operator_directives_enabled = True
    for k, v in over.items():
        setattr(c, k, v)
    return c


def _llm(payload: dict):
    """A mock System-role llm: ignores messages/grammar, returns the fixed JSON."""
    return lambda messages, grammar: json.dumps(payload)


class TestClassify(unittest.TestCase):
    def test_request_becomes_directive(self):
        c = _cfg()
        llm = _llm({"is_request": True, "title": "scan the local network",
                    "why": "charlie wants to know what's here", "deferral": ""})
        d = adm.classify_operator_message(c, llm, "hey, can you look at the network?")
        self.assertIsNotNone(d)
        self.assertEqual(d["title"], "scan the local network")

    def test_chatter_returns_none(self):
        c = _cfg()
        llm = _llm({"is_request": False, "title": "", "why": "", "deferral": ""})
        self.assertIsNone(adm.classify_operator_message(c, llm, "nice work, buddy"))

    def test_empty_message_and_flag_off(self):
        c = _cfg()
        llm = _llm({"is_request": True, "title": "do a thing", "why": "w", "deferral": ""})
        self.assertIsNone(adm.classify_operator_message(c, llm, "   "))
        c.operator_directives_enabled = False
        self.assertIsNone(adm.classify_operator_message(c, llm, "look at the network"))

    def test_llm_error_fails_open(self):
        c = _cfg()
        def boom(messages, grammar):
            raise RuntimeError("mind offline")
        self.assertIsNone(adm.classify_operator_message(c, boom, "look at the network"))

    def test_deferral_carried_through(self):
        c = _cfg()
        llm = _llm({"is_request": True, "title": "check in with charlie",
                    "why": "he asked", "deferral": "10m"})
        d = adm.classify_operator_message(c, llm, "check in with me in 10 minutes")
        self.assertEqual(d["deferral"], "10m")


class TestApplyAndPersist(unittest.TestCase):
    def test_directive_preempts_and_persists(self):
        c = _cfg()
        # creature has its own self-goal first
        objectives.add(c, "organize the nest", "it feels good", priority=5, tick=1)
        d = {"title": "scan the local network", "why": "charlie asked", "deferral": ""}
        obj = adm.apply_operator_directive(c, d, tick=2, source_key="dash_1.md")
        self.assertIsNotNone(obj)
        self.assertEqual(obj["origin"], "operator")
        # preempts: it is the active focus now
        self.assertEqual(objectives.get_active(c)["title"], "scan the local network")
        # persists: 100 stalled failing ticks would park/kill a self-goal; an operator goal endures
        for t in range(3, 120):
            objectives.record_tick(c, made_progress=False, tool_failed=True, tick_number=t)
        act = objectives.get_active(c)
        self.assertIsNotNone(act)
        self.assertEqual(act["origin"], "operator")
        self.assertEqual(act["state"], "active")

    def test_reissue_is_idempotent(self):
        c = _cfg()
        d = {"title": "check the printer", "why": "charlie", "deferral": ""}
        a = adm.apply_operator_directive(c, d, tick=1)
        b = adm.apply_operator_directive(c, d, tick=2)
        self.assertEqual(a["id"], b["id"])
        live = [o for o in objectives.list_objectives(c) if o["state"] == "active"]
        self.assertEqual(len([o for o in live if o["title"] == "check the printer"]), 1)

    def test_done_closes_it_normally(self):
        c = _cfg()
        adm.apply_operator_directive(c, {"title": "map the LAN", "why": "w"}, tick=1)
        done = objectives.mark_done(c, "map the LAN")
        self.assertTrue(done)


class TestDeferralSchedulesReminder(unittest.TestCase):
    def test_deferred_directive_sets_a_reminder_that_fires(self):
        import reminders
        import time as _t
        c = _cfg(reminders_enabled=True)
        d = {"title": "check in with charlie", "why": "he asked", "deferral": "10m"}
        obj = adm.apply_operator_directive(c, d, tick=1)
        self.assertIsNotNone(obj)
        pend = reminders.pending(c)
        self.assertEqual(len(pend), 1)
        self.assertEqual(pend[0]["origin"], "operator")
        # nothing due yet; due after the fire time
        self.assertEqual(reminders.due(c, _t.time()), [])
        fired = reminders.due(c, pend[0]["fire_ts"] + 1)
        self.assertEqual(len(fired), 1)
        self.assertEqual(fired[0]["note"], "check in with charlie")

    def test_no_deferral_schedules_nothing(self):
        import reminders
        c = _cfg(reminders_enabled=True)
        adm.apply_operator_directive(c, {"title": "scan lan", "why": "w", "deferral": ""}, tick=1)
        self.assertEqual(reminders.pending(c), [])


class TestFlagDark(unittest.TestCase):
    def test_off_is_byte_identical_no_directive(self):
        c = _cfg(operator_directives_enabled=False)
        llm = _llm({"is_request": True, "title": "x", "why": "y", "deferral": ""})
        self.assertIsNone(adm.classify_operator_message(c, llm, "do x"))
        # objectives untouched
        self.assertEqual(objectives.list_objectives(c), [])


if __name__ == "__main__":
    unittest.main()
