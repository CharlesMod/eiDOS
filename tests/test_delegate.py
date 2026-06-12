"""Delegate tool (delegate.py) — pi coding agent as a background job.

Pins the contract: typed validation gates with ZERO side effects before they pass,
the Kairos-repo cwd hard-deny (the allowlist can never override it), the spawn shape
(stdin=DEVNULL — pi hangs forever on an open non-TTY stdin — detached process group,
PYTHONUTF8), ledger integration through the shared waiter path, the delegate-specific
collect ceiling + reaped delivery, compact result formatting, and sandbox pruning.
"""

import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).parent.parent))

import delegate
import tools
from config import Config
from tools import collect_finished_jobs, _read_jobs, _write_jobs


def _mkconfig(enabled=True, **over):
    config = Config()
    config.workspace_dir = tempfile.mkdtemp()
    config.delegate_enabled = enabled
    for k, v in over.items():
        setattr(config, k, v)
    return config


def _fake_pi(config) -> str:
    """A real file on disk so _resolve_pi accepts it without PATH lookup."""
    p = Path(config.workspace_dir) / "pi.cmd"
    p.write_text("@echo off\n")
    return str(p)


class TestDelegateValidation(unittest.TestCase):
    """Every gate returns a typed failure and NEVER spawns."""

    def setUp(self):
        self.config = _mkconfig()
        self.config.delegate_pi_path = _fake_pi(self.config)

    def _no_spawn(self, args):
        with patch.object(delegate.subprocess, "Popen") as popen:
            r = delegate.tool_delegate(args, self.config)
            popen.assert_not_called()
        return r

    def test_empty_args_is_args(self):
        r = self._no_spawn({})
        self.assertFalse(r.success)
        self.assertEqual(r.fail_kind, "args")

    def test_disabled_is_blocked(self):
        self.config.delegate_enabled = False
        r = self._no_spawn({"task": "do a thing"})
        self.assertFalse(r.success)
        self.assertEqual(r.fail_kind, "blocked")

    def test_bad_mode_is_args(self):
        r = self._no_spawn({"task": "do a thing", "mode": "yolo"})
        self.assertFalse(r.success)
        self.assertEqual(r.fail_kind, "args")

    def test_unresolvable_pi_is_exec(self):
        self.config.delegate_pi_path = ""
        with patch.object(delegate.shutil, "which", return_value=None), \
             patch.object(delegate, "_PI_FALLBACK", r"Z:\nope\pi.cmd"):
            r = self._no_spawn({"task": "do a thing"})
        self.assertFalse(r.success)
        self.assertEqual(r.fail_kind, "exec")

    def test_concurrent_delegate_is_blocked(self):
        _write_jobs(self.config, [{"name": "dlg_busy", "kind": "delegate",
                                   "status": "running", "intent": "prior task"}])
        r = self._no_spawn({"task": "another thing"})
        self.assertFalse(r.success)
        self.assertEqual(r.fail_kind, "blocked")
        self.assertIn("dlg_busy", r.output)

    def test_continue_unknown_job_is_args(self):
        r = self._no_spawn({"task": "more", "continue_job": "dlg_never_existed"})
        self.assertFalse(r.success)
        self.assertEqual(r.fail_kind, "args")


class TestDelegateCwdPolicy(unittest.TestCase):
    """The Kairos repo is off-limits even when allowlisted; allowed roots work."""

    def setUp(self):
        self.config = _mkconfig()
        self.config.delegate_pi_path = _fake_pi(self.config)

    def _denied(self, cwd):
        with patch.object(delegate.subprocess, "Popen") as popen:
            r = delegate.tool_delegate({"task": "t", "cwd": cwd}, self.config)
            popen.assert_not_called()
        self.assertFalse(r.success)
        self.assertEqual(r.fail_kind, "blocked")

    def test_repo_root_denied(self):
        self._denied(str(delegate.REPO_ROOT))

    def test_repo_subdir_denied(self):
        self._denied(str(delegate.REPO_ROOT / "tests"))

    def test_repo_denied_even_when_allowlisted(self):
        self.config.delegate_allowed_dirs = [str(delegate.REPO_ROOT)]
        self._denied(str(delegate.REPO_ROOT))

    def test_case_variant_repo_denied(self):
        self._denied(str(delegate.REPO_ROOT).upper() if os.name == "nt"
                     else str(delegate.REPO_ROOT))

    def test_random_dir_denied_without_allowlist(self):
        self._denied(tempfile.mkdtemp())

    def test_allowlisted_dir_allowed(self):
        d = tempfile.mkdtemp()
        self.config.delegate_allowed_dirs = [d]
        self.assertEqual(delegate._cwd_denied(self.config, Path(d) / "sub"), "")

    def test_default_sandbox_allowed(self):
        sandbox = delegate._delegate_root(self.config) / "dlg_x"
        self.assertEqual(delegate._cwd_denied(self.config, sandbox), "")


class TestDelegateSpawn(unittest.TestCase):
    """The spawn shape: argv, stdin, process group, env, ledger entry, task file."""

    def setUp(self):
        self.config = _mkconfig()
        self.config.delegate_pi_path = _fake_pi(self.config)

    def _spawn(self, args):
        proc = MagicMock(pid=4242)
        with patch.object(delegate.subprocess, "Popen", return_value=proc) as popen, \
             patch.object(delegate.threading, "Thread") as thread:
            r = delegate.tool_delegate(args, self.config)
        self.assertTrue(r.success, r.output)
        thread.return_value.start.assert_called_once()
        return popen.call_args, r

    def test_spawn_contract(self):
        (argv,), kw = self._spawn({"task": "build a poller", "mode": "code",
                                   "name": "poller"})[0]
        self.assertIs(kw["stdin"], subprocess.DEVNULL)
        self.assertEqual(kw["env"]["PYTHONUTF8"], "1")
        if os.name == "nt":
            self.assertTrue(kw["creationflags"] & subprocess.CREATE_NEW_PROCESS_GROUP)
        else:
            self.assertTrue(kw["start_new_session"])
        for flag in ("-p", "--mode", "json", "--session-dir", "-a",
                     "--append-system-prompt"):
            self.assertIn(flag, argv)
        # code mode: full tools — no --tools restriction
        self.assertNotIn("--tools", argv)
        # sandbox cwd sits inside the Kairos tree -> pi must not ingest Kairos CLAUDE.md
        self.assertIn("--no-context-files", argv)
        at_arg = [a for a in argv if a.startswith("@")]
        self.assertEqual(len(at_arg), 1)
        self.assertEqual(Path(at_arg[0][1:]).read_text(encoding="utf-8"),
                         "build a poller")

    def test_research_mode_restricts_tools(self):
        (argv,), _kw = self._spawn({"task": "investigate", "name": "inv"})[0]
        self.assertIn("--tools", argv)
        self.assertIn(delegate._RESEARCH_TOOLS, argv)

    def test_allowlisted_cwd_keeps_context_files(self):
        d = tempfile.mkdtemp()
        self.config.delegate_allowed_dirs = [d]
        (argv,), kw = self._spawn({"task": "t", "mode": "code", "cwd": d,
                                   "name": "ext"})[0]
        self.assertNotIn("--no-context-files", argv)
        self.assertEqual(delegate._norm(kw["cwd"]), delegate._norm(d))

    def test_ledger_entry_shape(self):
        self._spawn({"task": "build a poller", "mode": "code", "name": "poller"})
        jobs = _read_jobs(self.config)
        self.assertEqual(len(jobs), 1)
        j = jobs[0]
        self.assertEqual(j["kind"], "delegate")
        self.assertEqual(j["name"], "dlg_poller")
        self.assertEqual(j["pid"], 4242)
        self.assertTrue(j["waited"])
        self.assertTrue(j["exit_path"])
        self.assertEqual(j["status"], "running")

    def test_continue_resumes_session(self):
        self._spawn({"task": "first", "mode": "code", "name": "poller"})
        # finish the first run so concurrency-1 doesn't block the follow-up
        jobs = _read_jobs(self.config)
        jobs[0]["status"] = "completed"
        _write_jobs(self.config, jobs)
        (argv,), _kw = self._spawn({"task": "more", "continue_job": "dlg_poller"})[0]
        self.assertIn("--continue", argv)
        jobs = _read_jobs(self.config)
        self.assertEqual(jobs[-1]["name"], "dlg_poller-r2")
        self.assertEqual(jobs[-1]["mode"], "code")  # inherited, not re-defaulted


class TestDelegateCollect(unittest.TestCase):
    """collect_finished_jobs: delegate-specific ceiling, reaped delivery, failed pruning."""

    def setUp(self):
        self.config = _mkconfig()
        self.config.delegate_timeout_s = 600.0

    def test_overdue_delegate_times_out_and_delivers(self):
        _write_jobs(self.config, [{
            "name": "dlg_old", "kind": "delegate", "status": "running", "pid": 99999,
            "started_ts": time.time() - 700, "notified": False, "waited": True,
            "output_path": "", "intent": "t"}])
        with patch.object(tools, "_kill_pid_tree") as kill:
            fins = collect_finished_jobs(self.config)
        kill.assert_called_once()
        self.assertEqual(len(fins), 1)
        self.assertEqual(fins[0]["status"], "timed_out")

    def test_running_delegate_under_ceiling_left_alone(self):
        _write_jobs(self.config, [{
            "name": "dlg_live", "kind": "delegate", "status": "running", "pid": 99999,
            "started_ts": time.time() - 60, "notified": False, "waited": True,
            "output_path": ""}])
        with patch.object(tools, "_kill_pid_tree") as kill:
            fins = collect_finished_jobs(self.config)
        kill.assert_not_called()
        self.assertEqual(fins, [])

    def test_reaped_delegate_delivered_once(self):
        _write_jobs(self.config, [{
            "name": "dlg_zap", "kind": "delegate", "status": "reaped", "pid": 1,
            "started_ts": time.time() - 30, "notified": False, "output_path": ""}])
        fins = collect_finished_jobs(self.config)
        self.assertEqual([f["name"] for f in fins], ["dlg_zap"])
        self.assertEqual(collect_finished_jobs(self.config), [])

    def test_reaped_async_not_delivered(self):
        # Only delegates announce their reaping (resume hint); async stays silent as before.
        _write_jobs(self.config, [{
            "name": "j1", "kind": "async", "status": "reaped", "pid": 1,
            "started_ts": time.time() - 30, "notified": False, "output_path": ""}])
        self.assertEqual(collect_finished_jobs(self.config), [])

    def test_notified_failed_job_prunes(self):
        # the _JOB_DONE fix: failed+notified jobs must stop accumulating in jobs.json
        _write_jobs(self.config, [
            {"name": f"f{i}", "kind": "async", "status": "failed", "pid": 1,
             "started_ts": i, "notified": True, "output_path": ""}
            for i in range(20)])
        collect_finished_jobs(self.config)
        self.assertLessEqual(len(_read_jobs(self.config)), 15)


class TestDelegateResultFormat(unittest.TestCase):

    def setUp(self):
        self.config = _mkconfig()
        self.job_dir = delegate._delegate_root(self.config) / "dlg_x"
        self.job_dir.mkdir(parents=True)
        self.out = Path(self.config.workspace_dir) / "dlg_x.out"

    def _job(self, status="completed", **over):
        j = {"name": "dlg_x", "kind": "delegate", "mode": "code", "status": status,
             "started_ts": time.time() - 120, "output_path": str(self.out),
             "job_dir": str(self.job_dir), "cwd": str(self.job_dir)}
        j.update(over)
        return j

    def test_completed_digest_files_and_resume(self):
        events = [
            {"type": "tool_execution_start", "toolName": "write",
             "args": {"path": "hello.txt"}},
            {"type": "message_update",
             "message": {"role": "assistant",
                         "content": [{"type": "text",
                                      "text": "Created hello.txt with the date."}]}},
        ]
        self.out.write_text("\n".join(json.dumps(e) for e in events), encoding="utf-8")
        text, ok = delegate.format_result_observation(self.config, self._job())
        self.assertTrue(ok)
        self.assertIn("OK", text)
        self.assertIn("Created hello.txt", text)
        self.assertIn("hello.txt", text)
        self.assertIn("continue_job", text)
        self.assertLessEqual(len(text), 1600)
        self.assertTrue((self.job_dir / "result.md").exists())

    def test_garbage_output_falls_back_to_tail(self):
        self.out.write_text("node:internal boom\nstack stack stack", encoding="utf-8")
        text, ok = delegate.format_result_observation(self.config, self._job())
        self.assertTrue(ok)
        self.assertIn("not parseable", text)
        self.assertIn("boom", text)

    def test_failed_carries_exit_code(self):
        self.out.write_text("Error: provider exploded", encoding="utf-8")
        text, ok = delegate.format_result_observation(
            self.config, self._job(status="failed", exit_code=3))
        self.assertFalse(ok)
        self.assertIn("exit 3", text)

    def test_timed_out_mentions_resume(self):
        text, ok = delegate.format_result_observation(
            self.config, self._job(status="timed_out"))
        self.assertFalse(ok)
        self.assertIn("TIMED OUT", text)
        self.assertIn("continue_job", text)

    def test_reaped_mentions_resume(self):
        text, ok = delegate.format_result_observation(
            self.config, self._job(status="reaped"))
        self.assertFalse(ok)
        self.assertIn("INTERRUPTED", text)
        self.assertIn("continue_job", text)


class TestDelegatePrune(unittest.TestCase):

    def test_prune_keeps_newest_and_running(self):
        config = _mkconfig(delegate_max_sessions=3)
        root = delegate._delegate_root(config)
        root.mkdir(parents=True)
        now = time.time()
        for i in range(6):
            d = root / f"dlg_{i}"
            d.mkdir()
            os.utime(d, (now - 600 + i * 60, now - 600 + i * 60))
        _write_jobs(config, [{"name": "dlg_0", "kind": "delegate",
                              "status": "running"}])  # oldest, but running
        delegate._prune_old_jobs(config)
        kept = sorted(p.name for p in root.iterdir() if p.is_dir())
        self.assertIn("dlg_0", kept)          # running job's dir survives
        self.assertIn("dlg_5", kept)          # newest survive
        self.assertNotIn("dlg_1", kept)       # oldest non-running pruned


class TestRegistry(unittest.TestCase):

    def test_delegate_registered_and_builtin(self):
        self.assertIn("delegate", tools.TOOLS)
        self.assertIn("delegate", tools._BUILTIN_TOOL_NAMES)

    def test_dispatch_with_empty_args_is_safe(self):
        # Config() default has delegate disabled -> typed block, no side effects.
        from parser import ToolCall
        config = Config()
        config.workspace_dir = tempfile.mkdtemp()
        r = tools.execute_tool(ToolCall(tool="delegate", args={}, raw=""), config)
        self.assertFalse(r.success)
        self.assertEqual(r.fail_kind, "blocked")


if __name__ == "__main__":
    unittest.main()
