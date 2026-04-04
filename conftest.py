"""Pytest configuration — custom markers, hooks, and --live flag."""

import logging
import os
import shutil
import time
from pathlib import Path

import pytest

_session_start = None
_RPI4_MULTIPLIER = 7.0

_FIXTURES_DIR = Path(__file__).parent / "tests" / "fixtures"


@pytest.fixture
def knowledge_fixture(tmp_path):
    """Copy the seed knowledge store into a temp workspace for testing.

    Returns the Config-style workspace path (tmp_path / "workspace").
    The knowledge dir is at workspace/knowledge/.
    """
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    src = _FIXTURES_DIR / "knowledge"
    dst = workspace / "knowledge"
    shutil.copytree(str(src), str(dst))
    return workspace

_session_start = None
_RPI4_MULTIPLIER = 7.0


def pytest_addoption(parser):
    parser.addoption("--live", action="store_true", default=False,
                     help="Run tests against real LM Studio endpoint")


def pytest_configure(config):
    config.addinivalue_line("markers", "slow: long-running simulation tests (deselect with -m 'not slow')")
    config.addinivalue_line("markers", "live: tests that call real LM Studio endpoint (deselect with -m 'not live')")
    if config.getoption("--live", default=False):
        os.environ["KAIROS_TEST_LIVE"] = "1"
        # Show LLM token usage in terminal
        llm_logger = logging.getLogger("kairos.llm")
        llm_logger.setLevel(logging.INFO)
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter("  %(name)s %(message)s"))
        llm_logger.addHandler(handler)


def pytest_unconfigure(config):
    os.environ.pop("KAIROS_TEST_LIVE", None)


def pytest_sessionstart(session):
    global _session_start
    _session_start = time.time()


def pytest_terminal_summary(terminalreporter, exitstatus, config):
    """Print RPi 4 projected runtime after test results."""
    if _session_start is None:
        return
    duration = time.time() - _session_start
    rpi_est = duration * _RPI4_MULTIPLIER
    terminalreporter.write_sep("=", "RPi 4 Performance Estimate")
    terminalreporter.write_line(
        f"  Mac wall time : {duration:>7.1f}s"
    )
    terminalreporter.write_line(
        f"  RPi 4 estimate: {rpi_est:>7.1f}s  ({rpi_est/60:.1f}m)  [x{_RPI4_MULTIPLIER:.0f} multiplier]"
    )
    if rpi_est > 600:
        terminalreporter.write_line(
            "  ⚠  Consider using `pytest -m 'not slow'` on the Pi for faster iteration."
        )
    terminalreporter.write_line("")
