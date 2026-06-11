"""Client side of the GPU speech-gate (see ARCHITECTURE_PRINCIPLES.md #1).

One GPU. When the house-model tick and TTS synthesis overlap, both run ~2x slower. So before its
model request, the tick calls `yield_to_speech()`: a SINGLE blocking GET to the dashboard that
holds while speech audio is actively streaming and returns the instant it finishes (the server
wakes it via `notify` — event-driven, no polling). No duration guess: the gate tracks liveness,
so it holds for the WHOLE utterance however long, and only releases early if synthesis stalls
(no audio for `stall_s`) or hits the `max_s` backstop. Any error (dashboard down, eidos run
standalone) -> return immediately and proceed; never block the tick on a failure.
"""
import json
import os
import urllib.request

# Set by the test harness (conftest) so no test ever reaches a live dashboard: a real eidos
# talks to :8099 for these channels, and that port may hold the live v1 dashboard, whose
# control long-poll would hang a run_loop-driving test. With this set, both channels take their
# fail-open path immediately. Never set in production.
_NO_DASHBOARD = "EIDOS_NO_DASHBOARD"

# Kept in sync with the dashboard's gate defaults so client + server agree on the bounds.
GPU_STARTUP_S = 12.0  # generous wait for speech to START (variable TTFA: warmup, contention)
GPU_STALL_S = 5.0     # once audio flows, a gap this long => wedged mid-stream, stop yielding
GPU_MAX_S = 60.0      # absolute ceiling; the tick can never block longer than ~this


def yield_to_speech(config, stall_s: float = GPU_STALL_S, max_s: float = GPU_MAX_S,
                    startup_s: float = GPU_STARTUP_S) -> dict:
    """Block until the GPU is free of TTS synthesis (or it never starts / stalls / hits the backstop).
    Returns the gate state ({"idle","reason","active",...}); never raises — on any failure it reports
    idle so the tick proceeds without delay."""
    if os.environ.get(_NO_DASHBOARD):
        return {"idle": True, "reason": "no_dashboard", "active": 0}
    # The GPU speech-gate moved to the standalone voice service (phase 8.3); control_wait below
    # still targets the dashboard. Both on 127.0.0.1 (same host as eidos).
    port = getattr(config, "voice_port", 8098)
    url = f"http://127.0.0.1:{port}/api/gpu/wait?stall={stall_s}&max={max_s}&startup={startup_s}"
    try:
        # socket timeout sits just above the server's own bounded wait, so the server controls timing
        with urllib.request.urlopen(url, timeout=max_s + 5.0) as r:
            return json.loads(r.read().decode("utf-8"))
    except Exception:  # noqa: BLE001 - dashboard down / standalone / network: never block the tick
        return {"idle": True, "reason": "error", "active": 0, "error": True}


# --- Control-change channel client (phase 4; ARCHITECTURE_PRINCIPLES.md #1) ----------------
# The reverse direction: the dashboard produces control state (pause/resume, listening hold,
# chat arrival) and notifies a seq counter; eidos makes ONE long-poll here instead of nap-polling
# three sentinel files on timers. Fail-open: any error -> None, and the caller falls back to the
# old bounded nap so a dashboard outage never strands the loop. After a failure we skip attempts
# for a cooldown so offline runs (tests, standalone) don't hammer connection-refused.

_CTRL_FAIL_COOLDOWN_S = 30.0
_ctrl_down_until = 0.0


def control_wait(config, since: int, max_s: float = 25.0):
    """Block (server-side) until control state changes past `since` or `max_s` elapses.
    Returns {"seq", "paused", "held", "interventions"} — or None if the channel is down."""
    global _ctrl_down_until
    import time as _t
    if os.environ.get(_NO_DASHBOARD):
        return None
    if _t.monotonic() < _ctrl_down_until:
        return None
    port = getattr(config, "dashboard_port", 8099)
    url = f"http://127.0.0.1:{port}/api/control/wait?since={int(since)}&max_s={float(max_s)}"
    try:
        with urllib.request.urlopen(url, timeout=float(max_s) + 5.0) as r:
            return json.loads(r.read().decode("utf-8"))
    except Exception:  # noqa: BLE001 - dashboard down / standalone: fall back to nap-polling
        _ctrl_down_until = _t.monotonic() + _CTRL_FAIL_COOLDOWN_S
        return None
