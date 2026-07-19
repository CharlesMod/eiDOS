"""The dashboard Start/GO/Pause/STOP buttons also govern the embedding model's systemd unit
(llama-embedding.service), so its resident GPU VRAM tracks the creature's run state. The mind
(llama-swap) is intentionally NOT touched here — it idle-unloads on its own.

These cover the gating (disabled / unset / unsafe / non-POSIX are no-ops), the exact systemctl
invocation, failure reporting, and that pause→stop / resume→start are actually wired.
"""

import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent.parent))

import dashboard
from config import Config


def _cfg(**kw):
    c = Config()
    c.workspace_dir = tempfile.mkdtemp()
    c.workspace.mkdir(parents=True, exist_ok=True)
    c.knowledge_embedding_enabled = True
    c.embedding_service = "llama-embedding.service"
    for k, v in kw.items():
        setattr(c, k, v)
    return c


def _recorder(calls, rc=0, stderr=""):
    def run(argv, **kwargs):
        calls.append(list(argv))
        return SimpleNamespace(returncode=rc, stdout="", stderr=stderr)
    return run


class TestEmbedderGating(unittest.TestCase):
    """The service is only touched when it makes sense — otherwise a silent, call-free no-op."""

    def test_noop_when_embeddings_disabled(self):
        cfg = _cfg(knowledge_embedding_enabled=False)
        calls = []
        with mock.patch("subprocess.run", _recorder(calls)):
            self.assertEqual(dashboard._set_embedder_running(cfg, True), "")
        self.assertEqual(calls, [])

    def test_noop_when_no_service_name(self):
        cfg = _cfg(embedding_service="")
        calls = []
        with mock.patch("subprocess.run", _recorder(calls)):
            self.assertEqual(dashboard._set_embedder_running(cfg, True), "")
        self.assertEqual(calls, [])

    def test_noop_on_windows(self):
        cfg = _cfg()
        calls = []
        with mock.patch("os.name", "nt"), mock.patch("subprocess.run", _recorder(calls)):
            self.assertEqual(dashboard._set_embedder_running(cfg, True), "")
        self.assertEqual(calls, [])

    def test_rejects_unsafe_service_name(self):
        cfg = _cfg(embedding_service="evil; rm -rf /")
        calls = []
        with mock.patch("subprocess.run", _recorder(calls)):
            note = dashboard._set_embedder_running(cfg, True)
        self.assertIn("unsafe", note)
        self.assertEqual(calls, [])   # never shelled out


class TestEmbedderInvocation(unittest.TestCase):

    def test_start_calls_systemctl_start_nonblocking(self):
        cfg = _cfg()
        calls = []
        with mock.patch("subprocess.run", _recorder(calls)):
            note = dashboard._set_embedder_running(cfg, True)
        self.assertEqual(calls, [["sudo", "-n", "systemctl", "--no-block",
                                  "start", "llama-embedding.service"]])
        self.assertIn("start requested", note)

    def test_stop_calls_systemctl_stop(self):
        cfg = _cfg()
        calls = []
        with mock.patch("subprocess.run", _recorder(calls)):
            note = dashboard._set_embedder_running(cfg, False)
        self.assertEqual(calls[0][4], "stop")
        self.assertIn("stop requested", note)

    def test_failure_is_reported_not_raised(self):
        cfg = _cfg()
        with mock.patch("subprocess.run", _recorder([], rc=5, stderr="Failed: Unit not found.")):
            note = dashboard._set_embedder_running(cfg, True)
        self.assertIn("failed", note)
        self.assertIn("Unit not found", note)

    def test_timeout_is_swallowed(self):
        import subprocess
        cfg = _cfg()

        def boom(*a, **k):
            raise subprocess.TimeoutExpired(cmd="systemctl", timeout=15)

        with mock.patch("subprocess.run", boom):
            note = dashboard._set_embedder_running(cfg, True)
        self.assertIn("error", note)   # non-fatal note, no exception


class TestControlWiring(unittest.TestCase):
    """pause frees the embedder VRAM; resume brings it back — via the real control endpoints."""

    def test_pause_stops_and_resume_starts(self):
        cfg = _cfg()
        verbs = []

        def run(argv, **kwargs):
            if "systemctl" in argv:
                verbs.append(argv[argv.index("systemctl") + 2])  # start|stop
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        with mock.patch("subprocess.run", run):
            dashboard._ctrl_pause(cfg)
            dashboard._ctrl_resume(cfg)
        self.assertEqual(verbs, ["stop", "start"])

    def test_disabled_config_leaves_control_endpoints_untouched(self):
        # The default creature (embeddings off / no unit) must not shell out on pause/resume.
        cfg = _cfg(knowledge_embedding_enabled=False)
        calls = []
        with mock.patch("subprocess.run", _recorder(calls)):
            dashboard._ctrl_pause(cfg)
            dashboard._ctrl_resume(cfg)
        self.assertEqual(calls, [])


if __name__ == "__main__":
    unittest.main()
