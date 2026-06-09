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
import urllib.request

# Kept in sync with the dashboard's gate defaults so client + server agree on the bounds.
GPU_STARTUP_S = 12.0  # generous wait for speech to START (variable TTFA: warmup, contention)
GPU_STALL_S = 5.0     # once audio flows, a gap this long => wedged mid-stream, stop yielding
GPU_MAX_S = 60.0      # absolute ceiling; the tick can never block longer than ~this


def yield_to_speech(config, stall_s: float = GPU_STALL_S, max_s: float = GPU_MAX_S,
                    startup_s: float = GPU_STARTUP_S) -> dict:
    """Block until the GPU is free of TTS synthesis (or it never starts / stalls / hits the backstop).
    Returns the gate state ({"idle","reason","active",...}); never raises — on any failure it reports
    idle so the tick proceeds without delay."""
    port = getattr(config, "dashboard_port", 8099)
    url = f"http://127.0.0.1:{port}/api/gpu/wait?stall={stall_s}&max={max_s}&startup={startup_s}"
    try:
        # socket timeout sits just above the server's own bounded wait, so the server controls timing
        with urllib.request.urlopen(url, timeout=max_s + 5.0) as r:
            return json.loads(r.read().decode("utf-8"))
    except Exception:  # noqa: BLE001 - dashboard down / standalone / network: never block the tick
        return {"idle": True, "reason": "error", "active": 0, "error": True}
