"""Client side of the GPU speech-gate (see ARCHITECTURE_PRINCIPLES.md #1).

One GPU. When the house-model tick and TTS synthesis overlap, both run ~2x slower. So before its
model request, the tick calls `yield_to_speech()`: a SINGLE blocking GET to the dashboard that
returns the instant in-progress synthesis finishes (the server wakes it via `notify`, event-driven
— no polling, no fixed cooldown). Bounded by `cap_s` so the tick can never hang; any error (e.g.
dashboard not running, or eidos run standalone) -> return immediately and proceed.
"""
import json
import urllib.request


def yield_to_speech(config, cap_s: float = 8.0) -> dict:
    """Block until the GPU is free of TTS synthesis, or `cap_s` elapses. Returns the gate state
    ({"idle","timed_out","active",...}); never raises — on any failure it reports idle so the
    tick proceeds without delay."""
    port = getattr(config, "dashboard_port", 8099)
    url = f"http://127.0.0.1:{port}/api/gpu/wait?cap={cap_s}"
    try:
        # client timeout a hair above the server-side cap so the server's bounded wait drives timing
        with urllib.request.urlopen(url, timeout=cap_s + 3.0) as r:
            return json.loads(r.read().decode("utf-8"))
    except Exception:  # noqa: BLE001 - dashboard down / standalone / network: never block the tick
        return {"idle": True, "timed_out": False, "active": 0, "error": True}
